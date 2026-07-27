"""Deep scan: the code of a service as a dependency network.

Where ``internals_scan`` finds the request *pipeline*, this module maps the
*whole machine*: every internal module, what it imports, and — resolved
through import aliases — which modules it actually calls and which symbols it
calls on them. That answers the on-call questions the pipeline view can't:
"who calls the PII scanner?", "what does the LLM client depend on?",
"if this module is broken, what's the blast radius?".

Still pure ``ast``: nothing is imported or executed. Call resolution is
static and best-effort by design:

* ``from hary.models import llm_factory`` + ``llm_factory.get_llm()``
  -> call edge to ``hary.models.llm_factory`` with symbol ``get_llm``
* ``from hary.models.llm_factory import get_llm`` + ``get_llm()``
  -> the same edge
* ``import hary.models.llm_factory as lf`` + ``lf.get_llm()`` -> the same

Only modules inside the service are kept — the standard library and
third-party packages would drown the picture.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from incidentlens.connectors.internals_scan import (
    CLIENT_HINTS,
    _module_of,
    _py_files,
)
from incidentlens.domain.models import (
    CodeEdge,
    CodeGraph,
    CodeModule,
    CodeSymbol,
    IncidentAnalysis,
    InternalTrace,
    ServiceInternals,
    SymbolEdge,
)

CONFIG_HINTS = ("config", "settings", "constants", "env")
MAX_MODULES = 320


def _classify(module: str, defs: list[str], stage_of: dict[str, str]) -> str:
    stem = module.rsplit(".", 1)[-1].lower()
    if module in stage_of:
        # more specific kinds first, then the generic pipeline label
        if any(h in stem for h in CLIENT_HINTS):
            return "client"
        return "graph-node"
    if "middleware" in stem or any("Middleware" in d for d in defs):
        return "middleware"
    if stem in ("main", "app", "api", "routes", "router") or "route" in stem:
        return "endpoint"
    if any(h in stem for h in CONFIG_HINTS):
        return "config"
    if any(h in stem for h in CLIENT_HINTS):
        return "client"
    return "module"


class _ModuleScan(ast.NodeVisitor):
    """Per-file pass: imports (with aliases) then attribute/name calls."""

    def __init__(self, module: str, known: set[str]) -> None:
        self.module = module
        self.known = known  # all internal module names
        self.package = module.rsplit(".", 1)[0] if "." in module else ""
        self.alias_to_module: dict[str, str] = {}  # local name -> module
        self.symbol_origin: dict[str, tuple[str, str]] = {}  # local name -> (module, symbol)
        self.imports: set[str] = set()
        self.calls: dict[str, set[str]] = {}  # module -> symbols
        self.defs: list[str] = []

    # -------------------------------------------------------------- imports

    def _resolve(self, name: str) -> str | None:
        """Longest internal module matching ``name`` (absolute dotted path)."""
        parts = name.split(".")
        for cut in range(len(parts), 0, -1):
            candidate = ".".join(parts[:cut])
            if candidate in self.known:
                return candidate
        return None

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            target = self._resolve(alias.name)
            if target:
                self.imports.add(target)
                self.alias_to_module[alias.asname or alias.name.split(".")[0]] = target
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        base = node.module or ""
        if node.level:  # relative import -> anchor on the current package
            anchor = self.module.split(".")
            anchor = anchor[: len(anchor) - node.level]
            base = ".".join(anchor + ([base] if base else []))
        for alias in node.names:
            local = alias.asname or alias.name
            full = f"{base}.{alias.name}" if base else alias.name
            if full in self.known:  # "from pkg import submodule" — exact only
                self.imports.add(full)
                self.alias_to_module[local] = full
                continue
            origin = self._resolve(base)
            if origin:  # "from pkg.module import symbol"
                self.imports.add(origin)
                self.symbol_origin[local] = (origin, alias.name)
        self.generic_visit(node)

    # ------------------------------------------------------------- def index

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if node.col_offset == 0:
            self.defs.append(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        if node.col_offset == 0:
            self.defs.append(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        if node.col_offset == 0:
            self.defs.append(node.name)
        self.generic_visit(node)

    # ----------------------------------------------------------------- calls

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = self.alias_to_module.get(func.value.id)
            if target and target != self.module:
                self.calls.setdefault(target, set()).add(func.attr)
        elif isinstance(func, ast.Name):
            origin = self.symbol_origin.get(func.id)
            if origin and origin[0] != self.module:
                self.calls.setdefault(origin[0], set()).add(origin[1])
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        # attribute *reads* through a module alias (settings.gateway_base_url)
        if isinstance(node.value, ast.Name):
            target = self.alias_to_module.get(node.value.id)
            if target and target != self.module:
                self.calls.setdefault(target, set()).add(node.attr)
        self.generic_visit(node)


def build_code_graph(
    directory: str | Path,
    service: str,
    internals: ServiceInternals | None = None,
) -> CodeGraph | None:
    root = Path(directory)
    files = _py_files(root)
    if not files:
        return None

    by_module: dict[str, Path] = {}
    for path in files:
        module = _module_of(path, root)
        if module:
            by_module[module] = path
    known = set(by_module)
    # package names resolve too (hary.models -> its __init__)
    packages = {m.rsplit(".", 1)[0] for m in known if "." in m}
    known |= packages

    stage_of: dict[str, str] = {}
    if internals:
        for istage in internals.stages:
            for prefix in istage.modules:
                stage_of[prefix] = istage.name

    modules: list[CodeModule] = []
    edges: list[CodeEdge] = []
    trees: dict[str, ast.AST] = {}
    module_kind: dict[str, str] = {}
    module_stage: dict[str, str | None] = {}
    for module, path in sorted(by_module.items())[:MAX_MODULES]:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue
        trees[module] = tree
        scan = _ModuleScan(module, known)
        scan.visit(tree)

        stage = None
        probe = module
        while probe:
            if probe in stage_of:
                stage = stage_of[probe]
                break
            probe = probe.rsplit(".", 1)[0] if "." in probe else ""
        kind = _classify(module, scan.defs, {module: stage} if stage else {})
        module_kind[module] = kind
        module_stage[module] = stage
        modules.append(
            CodeModule(
                name=module,
                kind=kind,
                stage=stage,
                defs=scan.defs[:12],
                loc=source.count("\n") + 1,
            )
        )
        for target, symbols in sorted(scan.calls.items()):
            real = by_module.get(target)
            dst = target if real or target in known else None
            if dst and dst != module:
                edges.append(
                    CodeEdge(src=module, dst=dst, kind="call",
                             symbols=sorted(symbols)[:8], count=len(symbols))
                )
        for target in sorted(scan.imports - set(scan.calls)):
            if target != module:
                edges.append(CodeEdge(src=module, dst=target, kind="import"))

    if len(modules) < 2:
        return None

    sym_nodes, sym_edges, sym_cycles = _build_symbol_layer(
        trees, module_kind, module_stage
    )
    cycles = _annotate_structure(modules, edges)
    return CodeGraph(
        service=service,
        modules=modules,
        edges=edges,
        symbols=sym_nodes,
        symbol_edges=sym_edges,
        cycles=cycles,
        symbol_cycles=sym_cycles,
    )


def _build_symbol_layer(
    trees: dict[str, ast.AST],
    module_kind: dict[str, str],
    module_stage: dict[str, str | None],
) -> tuple[list[CodeSymbol], list[SymbolEdge], list[list[str]]]:
    """The fine-grained call graph: index every symbol, then resolve calls."""
    from incidentlens.connectors.code_symbols import (
        SymbolIndex,
        index_module,
        resolve_module,
    )
    from incidentlens.connectors.graph_analysis import find_cycles

    index = SymbolIndex(modules=set(trees))
    for module, tree in trees.items():
        index_module(
            module, tree,
            module_kind=module_kind.get(module, "module"),
            stage=module_stage.get(module),
            index=index,
        )
    edges = []
    for module, tree in trees.items():
        edges.extend(resolve_module(module, tree, index))

    symbols = list(index.by_qual.values())
    referenced = {e.src for e in edges} | {e.dst for e in edges}
    # keep module-scope nodes only when they actually take part in a call
    symbols = [
        s for s in symbols
        if s.kind != "module" or s.qualname in referenced
    ]
    kept = {s.qualname for s in symbols}
    edges = [e for e in edges if e.src in kept and e.dst in kept]

    sym_nodes = [s.qualname for s in symbols]
    sym_pairs = [(e.src, e.dst) for e in edges]
    symbol_cycles = [c for c in find_cycles(sym_nodes, sym_pairs) if len(c) > 1]
    return symbols, edges, symbol_cycles


def _annotate_structure(
    modules: list[CodeModule], edges: list[CodeEdge]
) -> list[list[str]]:
    """Fill fan-in/out, blast radius and cycle membership on ``modules`` in
    place; return the module-level coupled clusters."""
    from incidentlens.connectors.graph_analysis import (
        blast_radii,
        degrees,
        find_cycles,
    )

    names = [m.name for m in modules]
    pairs = [(e.src, e.dst) for e in edges]
    fan_out, fan_in = degrees(names, pairs)
    radii = blast_radii(names, pairs)
    clusters = find_cycles(names, pairs)
    in_cycle = {n for c in clusters if len(c) > 1 for n in c}
    for module in modules:
        module.fan_out = fan_out.get(module.name, 0)
        module.fan_in = fan_in.get(module.name, 0)
        module.blast_radius = radii.get(module.name, 0)
        module.in_cycle = module.name in in_cycle
    return [c for c in clusters if len(c) > 1]


# ------------------------------------------------------------- persistence


def save_code_graphs(graphs: dict[str, CodeGraph], path: str | Path) -> Path:
    out = Path(path)
    out.write_text(
        json.dumps(
            {"services": {name: g.model_dump() for name, g in graphs.items()}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def load_code_graphs(path: str | Path) -> dict[str, CodeGraph]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        name: CodeGraph.model_validate(data)
        for name, data in raw.get("services", {}).items()
    }


def enrich_trace_with_code(
    analysis: IncidentAnalysis, graphs: dict[str, CodeGraph]
) -> None:
    """Fill the trace's failing-module context (who calls it, what it calls).

    Mutates ``analysis.internal_trace`` in place; a no-op when there is no
    trace, no failing stage, or no code graph for the origin service.
    """
    trace = getattr(analysis, "internal_trace", None)
    if trace is None or trace.failing_stage is None:
        return
    graph = graphs.get(trace.service)
    if graph is None:
        return
    stage_modules = [m for m in graph.modules if m.stage == trace.failing_stage]
    if not stage_modules:
        return

    failing_stage = next(
        (stage for stage in trace.stages if stage.stage == trace.failing_stage),
        None,
    )
    evidence = getattr(analysis, "evidence", [])
    linked_ids = set(failing_stage.evidence_ids) if failing_stage else set()
    linked_events = [event for event in evidence if event.id in linked_ids]

    def evidence_score(module_name: str) -> int:
        marker = f"[{module_name}]"
        if failing_stage and marker in failing_stage.detail:
            return 3
        score = 0
        for event in linked_events:
            logger = str(event.attributes.get("logger") or "").strip()
            if logger == module_name or marker in event.detail:
                score = max(score, 3)
            elif logger.startswith(module_name + "."):
                score = max(score, 2)
        return score

    # Exact linked logger evidence outranks file size. LOC remains the
    # deterministic fallback when a stage maps to multiple silent modules.
    failing = max(
        stage_modules,
        key=lambda module: (
            evidence_score(module.name),
            len(module.name) if evidence_score(module.name) else 0,
            module.loc,
        ),
    )
    trace.failing_module = failing.name
    trace.failing_callers = [src for src, _ in graph.callers_of(failing.name)][:6]
    trace.failing_callees = [dst for dst, _ in graph.callees_of(failing.name)][:6]
    trace.blast_radius = failing.blast_radius
    trace.failing_in_cycle = next(
        (c for c in graph.cycles if failing.name in c), []
    )
    _enrich_failing_symbol(trace, graph, failing.name)


# Verbs that mark a function as a "doing" entrypoint (builds/fetches/invokes),
# preferred over accessors/utilities when naming the failure locus.
_ENTRY_VERBS = (
    "get", "build", "create", "make", "invoke", "call", "run", "complete",
    "generate", "resolve", "load", "connect", "request", "send", "query",
    "predict", "chat", "fetch", "init", "open", "execute", "dispatch",
)


def _enrich_failing_symbol(
    trace: InternalTrace, graph: CodeGraph, failing_module: str
) -> None:
    """Pick the most likely failure locus *inside* the module and record its
    call context.

    Static analysis can't know which line raised, so we approximate: among the
    module's public functions, prefer the one other modules depend on that also
    *does* something (a ``get_``/``build_``/``invoke_`` entrypoint) over a pure
    accessor. The honest "where the blast starts" heuristic. No-op when the
    graph predates the symbol layer (bundled, module-only graphs)."""
    candidates = [
        s for s in graph.symbols_in_module(failing_module)
        if s.kind not in ("module", "class")
        # drop dunder noise (__init__, __repr__, …) but keep __call__, which is
        # the entrypoint for callable nodes (LangGraph nodes, middleware, …)
        and (not s.name.startswith("__") or s.name == "__call__")
    ]
    if not candidates:
        return
    module_of = {s.qualname: s.module for s in graph.symbols}
    external = {s.qualname: 0 for s in candidates}
    for edge in graph.symbol_edges:
        if edge.dst in external and module_of.get(edge.src) != module_of.get(edge.dst):
            external[edge.dst] += 1

    def _is_entry(name: str) -> bool:
        low = name.lower()
        return any(low.startswith(v) or f"_{v}" in low for v in _ENTRY_VERBS)

    failing_symbol = max(
        candidates,
        key=lambda s: (
            external.get(s.qualname, 0) > 0,  # something outside depends on it
            _is_entry(s.name),                # it does work, not just returns a field
            external.get(s.qualname, 0),      # ranked by how many callers
            s.loc,                            # then by substance
        ),
    )
    trace.failing_symbol = failing_symbol.qualname
    trace.failing_symbol_role = failing_symbol.role
    trace.failing_symbol_callers = graph.symbol_callers_of(failing_symbol.qualname)[:6]
    trace.failing_symbol_callees = graph.symbol_callees_of(failing_symbol.qualname)[:6]
