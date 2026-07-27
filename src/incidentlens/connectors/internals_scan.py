"""Static scan of a service's code for its internal pipeline.

Reads Python source with ``ast`` — nothing is imported or executed — and
extracts the nuts and bolts a request travels through:

* **LangGraph wiring** — ``add_edge("a", "b")`` and
  ``add_conditional_edges("src", fn, {...: "dst"})`` string literals become the
  stage graph; ``START``/``END`` sentinels mark the graph's entry and exits.
* **HTTP entrypoints** — FastAPI/Flask-style route decorators
  (``@app.post("/chat")``, ``@router.post(...)``); the most request-like POST
  route becomes the entry stage.
* **Middleware chains** — ``app.add_middleware(RateLimitMiddleware)`` calls, in
  execution order (last added runs first), placed before the entrypoint.
* **Shared clients** — modules with client-ish names (llm, client, model,
  provider, transport, gateway) imported by two or more stage modules become
  fan-in stages: the pipeline's last hop before an external dependency.

Every stage carries the dotted module prefixes its log lines will be written
under, which is how live telemetry gets attributed to stages later. The output
is a proposal — ``incidentlens.arch.json`` is meant to be edited.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from incidentlens.domain.models import InternalStage, ServiceInternals

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "target", "dist",
    "build", ".pytest_cache", "tests", "test", "eval_reports", "migrations",
    "scripts", "docs", "examples",
}
MAX_FILES = 400
MAX_STAGES = 22
CLIENT_HINTS = ("llm", "client", "model", "provider", "transport", "gateway", "factory")
ROUTE_METHODS = {"post", "get", "put", "patch", "delete"}
ENTRY_HINTS = ("chat", "complet", "query", "ask", "message", "search", "predict", "invoke")


def _kebab(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (name[i - 1].islower() or name[i - 1].isdigit()):
            out.append("-")
        out.append(ch.lower())
    return "".join(out).replace("_", "-").strip("-")


def _str_arg(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in ("START", "END"):
        return node.id
    return None


@dataclass
class _Scan:
    graph_edges: list[tuple[str, str]] = field(default_factory=list)
    graph_nodes: set = field(default_factory=set)
    start_targets: list[str] = field(default_factory=list)
    routes: list[tuple[str, str, Path]] = field(default_factory=list)  # method, path, file
    middleware: list[tuple[str, Path]] = field(default_factory=list)  # class, file (add order)


class _Visitor(ast.NodeVisitor):
    def __init__(self, scan: _Scan, path: Path) -> None:
        self.scan = scan
        self.path = path

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API)
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None

        if attr == "add_edge" and len(node.args) >= 2:
            a, b = _str_arg(node.args[0]), _str_arg(node.args[1])
            if a and b:
                if a == "START" and b not in ("START", "END"):
                    self.scan.start_targets.append(b)
                elif "END" not in (a, b):
                    self.scan.graph_edges.append((a, b))
                for name in (a, b):
                    if name not in ("START", "END"):
                        self.scan.graph_nodes.add(name)

        elif attr == "add_conditional_edges" and node.args:
            src = _str_arg(node.args[0])
            mapping = next(
                (arg for arg in node.args if isinstance(arg, ast.Dict)), None
            )
            if src and src not in ("START", "END"):
                self.scan.graph_nodes.add(src)
                if mapping is not None:
                    for value in mapping.values:
                        dst = _str_arg(value)
                        if dst and dst != "END":
                            self.scan.graph_edges.append((src, dst))
                            self.scan.graph_nodes.add(dst)

        elif attr == "add_node" and node.args:
            name = _str_arg(node.args[0])
            if name:
                self.scan.graph_nodes.add(name)

        elif attr == "add_middleware" and node.args:
            cls = node.args[0]
            cls_name = (
                cls.id if isinstance(cls, ast.Name)
                else cls.attr if isinstance(cls, ast.Attribute) else None
            )
            if cls_name:
                self.scan.middleware.append((cls_name, self.path))

        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._routes(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._routes(node)
        self.generic_visit(node)

    def _routes(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                continue
            method = deco.func.attr
            if method in ROUTE_METHODS and deco.args:
                path_str = _str_arg(deco.args[0])
                if path_str:
                    self.scan.routes.append((method, path_str, self.path))


def _py_files(directory: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(directory.rglob("*.py")):
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.parts):
            continue
        try:
            if path.stat().st_size > 400_000:
                continue
        except OSError:
            continue
        files.append(path)
        if len(files) >= MAX_FILES:
            break
    return files


def _module_of(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def _stage_module(stage: str, files: list[Path], root: Path) -> list[str]:
    """Best module prefixes for a stage name (file-stem match, tightest path)."""
    flat = stage.replace("-", "_").lower()
    exact = [p for p in files if p.stem.lower() == flat]
    loose = [p for p in files if flat in p.stem.lower() or p.stem.lower() in flat]
    for bucket in (exact, loose):
        if bucket:
            best = min(bucket, key=lambda p: len(p.parts))
            return [_module_of(best, root)]
    return []


def _class_module(cls_name: str, files: list[Path], root: Path) -> list[str]:
    needle = f"class {cls_name}"
    for path in files:
        try:
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                return [_module_of(path, root)]
        except OSError:
            continue
    return []


def _imports_of(path: Path) -> set:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def scan_service_internals(directory: str | Path) -> ServiceInternals | None:
    root = Path(directory)
    files = _py_files(root)
    if not files:
        return None

    scan = _Scan()
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        _Visitor(scan, path).visit(tree)

    stages: dict[str, InternalStage] = {}
    edges: list[tuple[str, str]] = []

    def add_stage(name: str, modules: list[str], description: str = "") -> None:
        if name not in stages:
            stages[name] = InternalStage(name=name, modules=modules, description=description)

    # ---- graph nodes become stages, in first-seen edge order
    ordered_nodes: list[str] = []
    for a, b in [("", t) for t in scan.start_targets] + scan.graph_edges:
        for name in (a, b):
            if name and name not in ordered_nodes:
                ordered_nodes.append(name)
    for name in sorted(scan.graph_nodes):
        if name not in ordered_nodes:
            ordered_nodes.append(name)
    ordered_nodes = ordered_nodes[:MAX_STAGES]
    for name in ordered_nodes:
        add_stage(name, _stage_module(name, files, root), "pipeline node")
    edges.extend((a, b) for a, b in scan.graph_edges if a in stages and b in stages)

    # ---- entrypoint route
    entry_stage: str | None = None
    posts = [r for r in scan.routes if r[0] == "post"]
    candidates = [r for r in posts if any(h in r[1].lower() for h in ENTRY_HINTS)] or posts
    if candidates:
        _method, route_path, route_file = candidates[0]
        seg = next(
            (s for s in reversed(route_path.split("/")) if s and "{" not in s), "api"
        )
        entry_stage = f"{_kebab(seg)}-endpoint"
        add_stage(entry_stage, [_module_of(route_file, root)], f"POST {route_path}")
        for target in dict.fromkeys(scan.start_targets):
            if target in stages:
                edges.append((entry_stage, target))

    # ---- middleware, execution order (last added runs first), before the entry
    if entry_stage and scan.middleware:
        chain = [
            (_kebab(cls.replace("Middleware", "")), cls, path)
            for cls, path in reversed(scan.middleware)
        ][:4]
        previous: str | None = None
        for kebab, cls, _path in chain:
            add_stage(kebab, _class_module(cls, files, root), "middleware")
            if previous:
                edges.append((previous, kebab))
            previous = kebab
        if previous:
            edges.append((previous, entry_stage))
        entry_stage = chain[0][0]

    # ---- shared client modules become fan-in stages
    stage_imports: dict[str, set] = {}
    for name, st in list(stages.items()):
        found: set = set()
        for module in st.modules:
            module_file = root / (module.replace(".", "/") + ".py")
            if module_file.is_file():
                found |= _imports_of(module_file)
        stage_imports[name] = found
    usage: dict[str, list[str]] = {}
    for stage_name, imports in stage_imports.items():
        for module in imports:
            stem = module.rsplit(".", 1)[-1].lower()
            if any(h in stem for h in CLIENT_HINTS):
                if (root / (module.replace(".", "/") + ".py")).is_file():
                    usage.setdefault(module, []).append(stage_name)
    shared = sorted(
        (m for m, callers in usage.items()
         if len(callers) >= 2
         or "llm" in m.rsplit(".", 1)[-1].lower()),
        key=lambda m: -len(usage[m]),
    )[:2]
    for module in shared:
        stem = module.rsplit(".", 1)[-1]
        client_name = "llm-client" if "llm" in stem.lower() else _kebab(stem)
        add_stage(client_name, [module], "shared client")
        for caller in usage[module][:4]:
            if caller in stages and caller != client_name:
                edges.append((caller, client_name))

    if len(stages) < 3:
        return None

    # de-duplicate edges, keep insertion order
    seen = set()
    unique_edges = []
    for edge in edges:
        if edge not in seen and edge[0] != edge[1]:
            seen.add(edge)
            unique_edges.append(edge)

    return ServiceInternals(
        stages=list(stages.values()),
        edges=unique_edges,
        entry=entry_stage,
    )
