"""Mermaid views of a service's code graph.

Two levels, both hierarchical (Mermaid ``subgraph``) and color-coded
(``classDef`` per functional role):

* **module** — packages become subgraphs, modules become nodes. The overview.
* **symbol** — modules become subgraphs, classes nest inside them, and methods
  / functions are the leaf nodes: methods-in-classes-in-modules. Scoped to a
  focus (a module or ``module.Class.method``) plus its immediate callers and
  callees, because the full call graph of a real service is too dense to read
  whole.

Coupled clusters (import/call cycles) are flagged; when an incident analysis
is supplied, logged module failure is red, a static symbol candidate is amber,
and the traversed module path glows teal — matching the video.

Output is a ``.mmd`` source file (paste into mermaid.live, the Mermaid Chart
connector, GitHub, or VS Code) and, optionally, a small self-viewing HTML.
"""

from __future__ import annotations

from pathlib import Path

from incidentlens.domain.models import CodeGraph, CodeSymbol, IncidentAnalysis

# classDef palette — mirrors the interactive view's KIND_COLOR / overlay colors
_ROLE_STYLE = {
    "endpoint": "fill:#16233b,stroke:#8ca8da,color:#dbe4f3",
    "client": "fill:#3a2a12,stroke:#e3a73c,color:#f2e3c9",
    "config": "fill:#123230,stroke:#52c7b8,color:#cdeae6",
    "middleware": "fill:#241b3a,stroke:#a78bda,color:#e2d9f5",
    "graph-node": "fill:#12321f,stroke:#3fb68b,color:#cdeacd",
    "logic": "fill:#1b212b,stroke:#55627e,color:#c9d1de",
    "test": "fill:#241b1b,stroke:#8a6a6a,color:#e0cccc",
    "module": "fill:#0f1420,stroke:#39435a,color:#8a93a3",
}
_OVERLAY_STYLE = {
    "failing": "fill:#4a1512,stroke:#e05b4d,color:#ffffff,stroke-width:3px",
    "candidate": (
        "fill:#3a2a12,stroke:#e3a73c,color:#f2e3c9,"
        "stroke-width:3px,stroke-dasharray:5 3"
    ),
    "path": "fill:#0f3a33,stroke:#52c7b8,color:#dffaf4,stroke-width:2px",
    "cycle": "fill:#3a2a12,stroke:#e3a73c,color:#f2e3c9,stroke-width:2px,stroke-dasharray:5 3",
}


def _esc(text: str) -> str:
    """Make a label safe inside a Mermaid ``["..."]``."""
    return text.replace('"', "'").replace("[", "(").replace("]", ")").strip()


def _cls(role: str) -> str:
    """Mermaid classDef identifiers must be alphanumeric — no hyphens."""
    return role.replace("-", "_")


class _Ids:
    def __init__(self) -> None:
        self._map: dict[str, str] = {}

    def get(self, key: str) -> str:
        if key not in self._map:
            self._map[key] = f"n{len(self._map)}"
        return self._map[key]

    def known(self, key: str) -> bool:
        return key in self._map


def _preamble(direction: str) -> list[str]:
    lines = [f"flowchart {direction}"]
    for name, style in {**_ROLE_STYLE, **_OVERLAY_STYLE}.items():
        lines.append(f"  classDef {_cls(name)} {style};")
    return lines


# ---------------------------------------------------------------- module level


def _package(module: str, depth: int) -> str:
    parts = module.split(".")
    return ".".join(parts[:depth]) if len(parts) > depth else module


def render_module_mermaid(
    graph: CodeGraph,
    *,
    analysis: IncidentAnalysis | None = None,
    group_depth: int = 2,
    max_nodes: int = 160,
    direction: str = "LR",
) -> str:
    """Package-grouped module network. Ranked by blast radius when capped."""
    from incidentlens.studio.graphview import incident_overlay

    overlay = incident_overlay(analysis, {graph.service: graph}) if analysis else None
    fail_mod = (overlay or {}).get("failing_module")
    failure_confirmed = bool((overlay or {}).get("module_failure_confirmed"))
    path_mods = set((overlay or {}).get("path_modules", []))

    modules = sorted(graph.modules, key=lambda m: -m.blast_radius)[:max_nodes]
    keep = {m.name for m in modules}
    cycle_members = {n for c in graph.cycles for n in c}

    ids = _Ids()
    groups: dict[str, list[str]] = {}
    for m in modules:
        groups.setdefault(_package(m.name, group_depth), []).append(m.name)

    body: list[str] = []
    classes: list[str] = []
    for pkg in sorted(groups):
        gid = "g_" + pkg.replace(".", "_")
        body.append(f'  subgraph {gid}["{_esc(pkg)}"]')
        for name in sorted(groups[pkg]):
            nid = ids.get(name)
            label = name[len(pkg) + 1:] if name.startswith(pkg + ".") else name
            body.append(f'    {nid}["{_esc(label or name)}"]')
            if name == fail_mod:
                cls = "failing" if failure_confirmed else "candidate"
            elif name in path_mods:
                cls = "path"
            elif name in cycle_members:
                cls = "cycle"
            else:
                cls = next((m.kind for m in modules if m.name == name), "module")
                cls = cls if cls in _ROLE_STYLE else "module"
            classes.append(f"  class {nid} {_cls(cls)};")
        body.append("  end")

    links: list[str] = []
    for e in graph.edges:
        if e.src in keep and e.dst in keep:
            arrow = "-.->" if e.kind == "import" else "-->"
            links.append(f"  {ids.get(e.src)} {arrow} {ids.get(e.dst)}")

    return "\n".join(_preamble(direction) + body + sorted(set(links)) + classes)


# ---------------------------------------------------------------- symbol level


def _focus_modules(
    graph: CodeGraph, focus: str | None, analysis: IncidentAnalysis | None
) -> set[str]:
    """The modules to draw at symbol level: the focus module + neighbours."""
    if focus is None and analysis is not None and analysis.internal_trace:
        focus = analysis.internal_trace.failing_module
    if focus is None:  # no anchor: the highest blast-radius module
        focus = max(graph.modules, key=lambda m: m.blast_radius).name if graph.modules else ""

    focus_mods = {focus} if any(m.name == focus for m in graph.modules) else set()
    if not focus_mods:  # focus was a symbol qualname
        focus_mods = {s.module for s in graph.symbols if s.qualname == focus or s.module == focus}
    if not focus_mods:
        focus_mods = {focus}

    focus_syms = {s.qualname for s in graph.symbols if s.module in focus_mods}
    neighbours: set[str] = set(focus_mods)
    for e in graph.symbol_edges:
        if e.src in focus_syms:
            neighbours.add(e.dst.rsplit(".", 1)[0])
            neighbours |= {s.module for s in graph.symbols if s.qualname == e.dst}
        if e.dst in focus_syms:
            neighbours |= {s.module for s in graph.symbols if s.qualname == e.src}
    return neighbours


def render_symbol_mermaid(
    graph: CodeGraph,
    *,
    focus: str | None = None,
    analysis: IncidentAnalysis | None = None,
    max_nodes: int = 140,
    direction: str = "LR",
) -> str:
    """Methods-in-classes-in-modules, scoped to the focus and its neighbours."""
    trace = analysis.internal_trace if analysis else None
    fail_sym = trace.failing_symbol if trace else None

    include_mods = _focus_modules(graph, focus, analysis)
    syms = [s for s in graph.symbols if s.module in include_mods and s.kind != "module"]
    # rank so the cap keeps the most-connected symbols
    conn: dict[str, int] = {}
    for e in graph.symbol_edges:
        conn[e.src] = conn.get(e.src, 0) + 1
        conn[e.dst] = conn.get(e.dst, 0) + 1
    syms.sort(key=lambda s: (s.qualname != fail_sym, -conn.get(s.qualname, 0)))
    syms = syms[:max_nodes]
    keep = {s.qualname for s in syms}
    by_module: dict[str, list[CodeSymbol]] = {}
    for s in syms:
        by_module.setdefault(s.module, []).append(s)

    ids = _Ids()
    body: list[str] = []
    classes: list[str] = []

    def emit_node(s: CodeSymbol) -> None:
        nid = ids.get(s.qualname)
        suffix = "" if s.kind == "class" else "()"
        body.append(f'      {nid}["{_esc(s.name)}{suffix}"]')
        if s.qualname == fail_sym:
            cls = "candidate"
        else:
            cls = s.role if s.role in _ROLE_STYLE else "logic"
        classes.append(f"  class {nid} {_cls(cls)};")

    for module in sorted(by_module):
        mid = "m_" + module.replace(".", "_")
        body.append(f'  subgraph {mid}["{_esc(module)}"]')
        members = by_module[module]
        classes_here = {s.qualname: s for s in members if s.kind == "class"}
        methods_by_class: dict[str, list[CodeSymbol]] = {}
        for s in members:
            if s.parent is not None:
                methods_by_class.setdefault(s.parent, []).append(s)
        loose = [s for s in members if s.parent is None and s.kind != "class"]
        for qualname in sorted(classes_here.keys() | methods_by_class.keys()):
            c = classes_here.get(qualname)
            class_name = c.name if c is not None else qualname.rsplit(".", 1)[-1]
            cid = "c_" + qualname.replace(".", "_")
            body.append(f'    subgraph {cid}["class {_esc(class_name)}"]')
            if c is not None:
                emit_node(c)
            for meth in methods_by_class.get(qualname, []):
                emit_node(meth)
            body.append("    end")
        for s in loose:
            emit_node(s)
        body.append("  end")

    links: list[str] = []
    for e in graph.symbol_edges:
        if e.src in keep and e.dst in keep:
            arrow = "-.->" if e.kind == "dynamic" else "==>" if e.kind == "construct" else "-->"
            links.append(f"  {ids.get(e.src)} {arrow} {ids.get(e.dst)}")

    header = _preamble(direction)
    if not links and not body:
        header.append(f'  empty["no resolved calls in scope for {_esc(focus or graph.service)}"]')
    return "\n".join(header + body + sorted(set(links)) + classes)


# --------------------------------------------------------------------- writers

_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>IncidentLens · Mermaid code graph</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{margin:0;background:#0e1116;color:#e6e9ef;font:14px -apple-system,'Segoe UI',sans-serif}
header{padding:12px 16px;border-bottom:1px solid #263042;display:flex;gap:12px;align-items:center}
header b{font-size:15px}header span{color:#8a93a3;font:12px monospace}
#diagram{padding:16px;overflow:auto}
pre.src{white-space:pre-wrap;background:#151a22;border:1px solid #263042;border-radius:8px;
padding:12px;color:#8a93a3;font:12px monospace;margin:16px}
button{background:#1b212b;border:1px solid #263042;color:#c9d1de;padding:6px 12px;
border-radius:8px;cursor:pointer;font:12px monospace}
</style></head><body>
<header><b>IncidentLens · code graph</b><span>__SUB__</span>
<button onclick="navigator.clipboard.writeText(document.getElementById('src').textContent)">
copy .mmd</button>
</header>
<div id="diagram"><pre class="mermaid">__SRC__</pre></div>
<pre class="src" id="src" hidden>__SRC__</pre>
<script type="module">
// Mermaid is loaded from a CDN here for a one-click viewer. The canonical,
// no-network artifact is the .mmd file and the interactive HTML (studio).
try{
  const m=await import('https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs');
  m.default.initialize({startOnLoad:true,theme:'dark',securityLevel:'loose',
    flowchart:{htmlLabels:true,curve:'basis'}});
}catch(e){
  document.getElementById('diagram').innerHTML=
   '<pre class="src">Mermaid CDN unavailable offline. The .mmd source:\\n\\n'+
   document.getElementById('src').textContent.replace(/</g,'&lt;')+'</pre>';
}
</script></body></html>
"""


def write_mermaid(
    graph: CodeGraph,
    out_path: str | Path,
    *,
    level: str = "module",
    focus: str | None = None,
    analysis: IncidentAnalysis | None = None,
    emit_html: bool = True,
) -> tuple[Path, Path | None]:
    """Write the ``.mmd`` (and optional viewer HTML). Returns (mmd, html|None)."""
    if level == "symbol":
        source = render_symbol_mermaid(graph, focus=focus, analysis=analysis)
        sub = f"{graph.service} · symbol level · focus={focus or 'auto'}"
    else:
        source = render_module_mermaid(graph, analysis=analysis)
        sub = f"{graph.service} · module level · {len(graph.modules)} modules"

    mmd = Path(out_path)
    mmd.parent.mkdir(parents=True, exist_ok=True)
    mmd.write_text(source + "\n", encoding="utf-8")

    html_path: Path | None = None
    if emit_html:
        html_path = mmd.with_suffix(".html")
        html = _HTML.replace("__SUB__", _esc(sub)).replace("__SRC__", source)
        html_path.write_text(html, encoding="utf-8")
    return mmd, html_path
