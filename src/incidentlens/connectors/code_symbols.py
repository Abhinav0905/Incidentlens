"""Fine-grained call graph: the ``module.Class.method`` layer.

Where :mod:`code_graph` maps files to files, this maps *symbols* to symbols —
every top-level function, class and method becomes a node, and a resolved call
between them becomes an edge. That is what lets the incident narrator say not
just "``llm_factory`` failed" but "``llm_factory.get_llm`` failed, called by
``agent.run`` and ``rewriter.rewrite``".

Still pure ``ast`` — nothing is imported or executed. Resolution is
best-effort and static, in the spirit of ``pyan``:

* ``foo()`` -> a top-level def in this module, or a ``from x import foo`` origin
* ``mod.foo()`` where ``mod`` is an import alias -> ``mod.foo``
* ``self.foo()`` / ``cls.foo()`` inside a class -> ``ThisClass.foo``
* ``x = Thing(); x.foo()`` -> ``Thing.foo`` (single-assignment local typing)
* ``Thing(...)`` -> a *construct* edge to the class
* ``importlib.import_module("a.b")`` / ``__import__("a.b")`` -> a *dynamic* edge
  to module ``a.b`` — the imports static scanning otherwise misses

Unresolvable calls (builtins, third-party, dynamic dispatch we can't follow)
are dropped rather than guessed. Calls that resolve to a module but not a
specific symbol land on that module's module-scope node.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from incidentlens.connectors.internals_scan import CLIENT_HINTS
from incidentlens.domain.models import CodeSymbol, SymbolEdge

CONFIG_HINTS = ("config", "settings", "constants", "env")
ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "route", "websocket", "head"}
MAX_SYMBOLS = 4000


# --------------------------------------------------------------------- helpers


def _decorator_name(node: ast.expr) -> str:
    """Dotted name of a decorator expression: ``@app.post(...)`` -> ``app.post``."""
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _loc(node: ast.AST) -> int:
    end = getattr(node, "end_lineno", None)
    start = getattr(node, "lineno", None)
    if isinstance(end, int) and isinstance(start, int):
        return end - start + 1
    return 0


def _resolve_internal(name: str, known: set[str]) -> str | None:
    """Longest internal module matching dotted ``name``."""
    parts = name.split(".")
    for cut in range(len(parts), 0, -1):
        candidate = ".".join(parts[:cut])
        if candidate in known:
            return candidate
    return None


# ------------------------------------------------------------------ symbol index


@dataclass
class SymbolIndex:
    """Everything pass 2 needs to resolve a call to a qualname."""

    modules: set[str] = field(default_factory=set)
    class_qualnames: set[str] = field(default_factory=set)
    by_qual: dict[str, CodeSymbol] = field(default_factory=dict)
    # module -> {top-level name -> qualname}
    local_defs: dict[str, dict[str, str]] = field(default_factory=dict)


def _classify_symbol(
    *, module_kind: str, name: str, kind: str, decorators: list[str]
) -> str:
    stem = module_kind
    short = decorators and decorators[-1].rsplit(".", 1)[-1].lower()
    if any(d.rsplit(".", 1)[-1].lower() in ROUTE_DECORATORS for d in decorators):
        return "endpoint"
    if kind == "class" and "middleware" in name.lower():
        return "middleware"
    if name.lower().startswith("test") or short == "test":
        return "test"
    if stem in ("endpoint", "client", "config", "graph-node", "middleware"):
        return stem
    if any(h in name.lower() for h in CLIENT_HINTS):
        return "client"
    if any(h in name.lower() for h in CONFIG_HINTS):
        return "config"
    return "logic"


def index_module(
    module: str,
    tree: ast.AST,
    *,
    module_kind: str,
    stage: str | None,
    index: SymbolIndex,
) -> None:
    """Pass 1 for one module: register its module-scope node, top-level defs,
    classes and (one level of) methods into ``index``."""
    locals_: dict[str, str] = index.local_defs.setdefault(module, {})

    # module-scope node — the home of top-level (import-time) code
    index.by_qual[module] = CodeSymbol(
        qualname=module, module=module, name=module.rsplit(".", 1)[-1],
        kind="module", role=module_kind if module_kind != "module" else "logic",
        stage=stage, lineno=0, loc=0,
    )

    def add(sym: CodeSymbol) -> None:
        if len(index.by_qual) < MAX_SYMBOLS:
            index.by_qual[sym.qualname] = sym

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qual = f"{module}.{node.name}"
            decos = [_decorator_name(d) for d in node.decorator_list]
            kind = "async-function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            locals_[node.name] = qual
            add(CodeSymbol(
                qualname=qual, module=module, name=node.name, kind=kind,
                role=_classify_symbol(module_kind=module_kind, name=node.name,
                                      kind=kind, decorators=decos),
                stage=stage, lineno=node.lineno, loc=_loc(node), decorators=decos,
            ))
        elif isinstance(node, ast.ClassDef):
            cqual = f"{module}.{node.name}"
            decos = [_decorator_name(d) for d in node.decorator_list]
            locals_[node.name] = cqual
            index.class_qualnames.add(cqual)
            add(CodeSymbol(
                qualname=cqual, module=module, name=node.name, kind="class",
                role=_classify_symbol(module_kind=module_kind, name=node.name,
                                      kind="class", decorators=decos),
                stage=stage, lineno=node.lineno, loc=_loc(node), decorators=decos,
            ))
            for sub in ast.iter_child_nodes(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mqual = f"{cqual}.{sub.name}"
                    mdecos = [_decorator_name(d) for d in sub.decorator_list]
                    mkind = ("async-method"
                             if isinstance(sub, ast.AsyncFunctionDef) else "method")
                    add(CodeSymbol(
                        qualname=mqual, module=module, name=sub.name, kind=mkind,
                        parent=cqual,
                        role=_classify_symbol(module_kind=module_kind, name=sub.name,
                                              kind=mkind, decorators=mdecos),
                        stage=stage, lineno=sub.lineno, loc=_loc(sub), decorators=mdecos,
                    ))


# --------------------------------------------------------------- import tables


def _import_tables(
    tree: ast.AST, module: str, known: set[str]
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """(alias -> internal module, local name -> (module, symbol)).

    Walks the whole tree, so imports buried inside function bodies are caught —
    the very case plain root-scope scanning misses.
    """
    alias_to_module: dict[str, str] = {}
    symbol_origin: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_internal(alias.name, known)
                if target:
                    alias_to_module[alias.asname or alias.name.split(".")[0]] = target
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative import -> anchor on the current package
                anchor = module.split(".")
                anchor = anchor[: len(anchor) - node.level]
                base = ".".join(anchor + ([base] if base else []))
            for alias in node.names:
                local = alias.asname or alias.name
                full = f"{base}.{alias.name}" if base else alias.name
                if full in known:  # from pkg import submodule
                    alias_to_module[local] = full
                    continue
                origin = _resolve_internal(base, known)
                if origin:  # from pkg.module import symbol
                    symbol_origin[local] = (origin, alias.name)
    return alias_to_module, symbol_origin


# -------------------------------------------------------------------- resolver


class _Resolver:
    """Pass 2 for one module: walk scopes and emit resolved symbol edges."""

    def __init__(
        self,
        module: str,
        index: SymbolIndex,
        alias_to_module: dict[str, str],
        symbol_origin: dict[str, tuple[str, str]],
    ) -> None:
        self.module = module
        self.idx = index
        self.alias = alias_to_module
        self.sym_origin = symbol_origin
        # (src, dst, kind) -> [count, first_lineno]
        self._edges: dict[tuple[str, str, str], list[int]] = {}

    # -- edge accumulation
    def _emit(self, src: str, dst: str, kind: str, lineno: int) -> None:
        if src == dst or dst not in self.idx.by_qual:
            return
        key = (src, dst, kind)
        slot = self._edges.get(key)
        if slot is None:
            self._edges[key] = [1, lineno]
        else:
            slot[0] += 1

    def edges(self) -> list[SymbolEdge]:
        return [
            SymbolEdge(src=s, dst=d, kind=k, count=c, lineno=ln)
            for (s, d, k), (c, ln) in self._edges.items()
        ]

    # -- callee resolution
    def _resolve(
        self, func: ast.expr, current_class: str | None, local_types: dict[str, str]
    ) -> tuple[str, str] | None:
        if isinstance(func, ast.Name):
            name = func.id
            q = self.idx.local_defs.get(self.module, {}).get(name)
            if q:
                return q, ("construct" if q in self.idx.class_qualnames else "call")
            origin = self.sym_origin.get(name)
            if origin:
                mod, sym = origin
                cand = f"{mod}.{sym}"
                if cand in self.idx.by_qual:
                    return cand, ("construct"
                                  if cand in self.idx.class_qualnames else "call")
                if mod in self.idx.modules:
                    return mod, "call"
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            base, attr = func.value.id, func.attr
            if base in ("self", "cls") and current_class:
                cand = f"{current_class}.{attr}"
                return (cand, "call") if cand in self.idx.by_qual else None
            if base in local_types:
                cand = f"{local_types[base]}.{attr}"
                return (cand, "call") if cand in self.idx.by_qual else None
            alias_mod = self.alias.get(base)
            if alias_mod:
                cand = f"{alias_mod}.{attr}"
                if cand in self.idx.by_qual:
                    return cand, ("construct"
                                  if cand in self.idx.class_qualnames else "call")
                if alias_mod in self.idx.modules:
                    return alias_mod, "call"
            origin = self.sym_origin.get(base)
            if origin:
                mod2, sym2 = origin
                full = f"{mod2}.{sym2}"
                if full in self.idx.modules:
                    cand = f"{full}.{attr}"
                    if cand in self.idx.by_qual:
                        return cand, "call"
                    return full, "call"
        return None

    def _dynamic_target(self, call: ast.Call) -> str | None:
        """A dynamic import in ``call`` -> the internal module it names, if any."""
        func = call.func
        is_dyn = (
            (isinstance(func, ast.Attribute) and func.attr == "import_module")
            or (isinstance(func, ast.Name) and func.id == "__import__")
        )
        if not is_dyn or not call.args:
            return None
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return _resolve_internal(arg.value, self.idx.modules)
        return None

    # -- scope walk
    def _handle_call(
        self, call: ast.Call, scope: str, current_class: str | None,
        local_types: dict[str, str],
    ) -> None:
        dyn = self._dynamic_target(call)
        if dyn is not None:
            self._emit(scope, dyn, "dynamic", getattr(call, "lineno", 0))
            return
        hit = self._resolve(call.func, current_class, local_types)
        if hit is not None:
            self._emit(scope, hit[0], hit[1], getattr(call, "lineno", 0))

    def _record_assign(
        self, node: ast.Assign, current_class: str | None, local_types: dict[str, str]
    ) -> None:
        """``x = Thing()`` -> remember x is a Thing, for later ``x.method()``."""
        if not isinstance(node.value, ast.Call):
            return
        hit = self._resolve(node.value.func, current_class, local_types)
        if hit and hit[1] == "construct":
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_types[target.id] = hit[0]

    def walk(
        self, node: ast.AST, scope: str, current_class: str | None,
        in_class_body: bool, local_types: dict[str, str],
    ) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if in_class_body and current_class:
                    qual = f"{current_class}.{child.name}"
                    self.walk(child, qual, current_class, False, {})
                elif scope == self.module:
                    qual = f"{self.module}.{child.name}"
                    self.walk(child, qual, None, False, {})
                else:  # nested function: attribute its calls to the enclosing scope
                    self.walk(child, scope, current_class, False, local_types)
            elif isinstance(child, ast.ClassDef):
                cqual = (f"{self.module}.{child.name}" if scope == self.module
                         else f"{current_class}.{child.name}" if current_class
                         else f"{self.module}.{child.name}")
                self.walk(child, cqual, cqual, True, {})
            elif isinstance(child, ast.Assign):
                self._record_assign(child, current_class, local_types)
                self.walk(child, scope, current_class, False, local_types)
            elif isinstance(child, ast.Call):
                self._handle_call(child, scope, current_class, local_types)
                self.walk(child, scope, current_class, in_class_body, local_types)
            else:
                self.walk(child, scope, current_class, in_class_body, local_types)


def resolve_module(
    module: str, tree: ast.AST, index: SymbolIndex
) -> list[SymbolEdge]:
    alias_to_module, symbol_origin = _import_tables(tree, module, index.modules)
    resolver = _Resolver(module, index, alias_to_module, symbol_origin)
    resolver.walk(tree, module, None, False, {})
    return resolver.edges()
