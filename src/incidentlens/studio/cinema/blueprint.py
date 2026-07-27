"""Repository-derived HLD and LLD blueprint acts.

The compact 3D dependency scenes are useful when an analysis only carries the
small amount of code context stored on :class:`InternalTrace`.  When a full
``CodeGraph`` is available, however, the movie can open the service like a
design document:

* package boxes containing every selected module (HLD), and
* module boxes containing functions plus nested class boxes (LLD).

These scenes are rendered directly with Pillow.  There is no Mermaid browser,
Graphviz binary, or network dependency in the video path.  A small 2D camera
establishes the whole blueprint, glides to the failing package/module, and
lands on the evidence-backed locus while the existing narration windows play.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from incidentlens.domain.models import (
    CodeGraph,
    CodeModule,
    CodeSymbol,
    IncidentAnalysis,
    InternalTrace,
)
from incidentlens.studio.cinema import palette
from incidentlens.studio.cinema.easing import (
    clamp,
    ease_in_out_quint,
    mix_color,
    pulse,
    smoothstep,
    with_alpha,
)
from incidentlens.studio.cinema.engine import RenderSpec
from incidentlens.studio.cinema.fonts import fit_text, font
from incidentlens.studio.evidence import module_failure_is_log_confirmed
from incidentlens.studio.graphview import incident_overlay

_MODULE_LIMIT = 160
_SYMBOL_LIMIT = 140

_ROLE_COLOR: dict[str, tuple[int, int, int]] = {
    "endpoint": (140, 168, 218),
    "client": (227, 167, 60),
    "config": (82, 199, 184),
    "middleware": (167, 139, 218),
    "graph-node": (63, 182, 139),
    "logic": (107, 119, 148),
    "test": (138, 106, 106),
    "module": (86, 98, 126),
}


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


@dataclass(frozen=True)
class BlueprintNode:
    key: str
    label: str
    rect: Rect
    group: str
    kind: str
    module: str
    role: str = "module"
    lineno: int = 0
    loc: int = 0


@dataclass(frozen=True)
class GroupBox:
    key: str
    label: str
    rect: Rect
    depth: int = 0
    parent: str | None = None


@dataclass(frozen=True)
class BlueprintEdge:
    src: str
    dst: str
    kind: str

    @property
    def key(self) -> tuple[str, str]:
        return self.src, self.dst


@dataclass(frozen=True)
class BlueprintLayout:
    mode: str  # "module" | "symbol"
    title: str
    stats: str
    world: Rect
    nodes: dict[str, BlueprintNode]
    groups: list[GroupBox]
    edges: list[BlueprintEdge]
    fail_key: str
    focus_group: str
    path_nodes: tuple[str, ...]
    impact_nodes: tuple[str, ...]
    focus_edges: frozenset[tuple[str, str]]
    impact_edges: frozenset[tuple[str, str]]
    failure_confirmed: bool


@dataclass(frozen=True)
class _GroupPlan:
    key: str
    label: str
    members: tuple[CodeModule, ...]
    width: float
    height: float
    node_width: float
    columns: int


@dataclass(frozen=True)
class _CameraStop:
    at: float
    cx: float
    cy: float
    zoom: float


def _package(module: str, depth: int = 2) -> str:
    parts = module.split(".")
    return ".".join(parts[:depth]) if len(parts) > depth else module


def _relative_label(name: str, group: str) -> str:
    if name.startswith(group + "."):
        return name[len(group) + 1 :]
    return name.rsplit(".", 1)[-1] or name


def _module_for_symbol(graph: CodeGraph, qualname: str | None) -> str | None:
    if not qualname:
        return None
    symbol = next((item for item in graph.symbols if item.qualname == qualname), None)
    return symbol.module if symbol is not None else None


def _dependency_levels(names: set[str], edges: set[tuple[str, str]]) -> dict[str, int]:
    """SCC-safe left-to-right levels, with a small pure-Python fallback."""
    try:
        import networkx as nx  # type: ignore[import-untyped]

        graph = nx.DiGraph()
        graph.add_nodes_from(sorted(names))
        graph.add_edges_from(sorted(edges))
        condensed = nx.condensation(graph)
        component_level: dict[int, int] = {}
        for component in nx.topological_sort(condensed):
            component_level[component] = max(
                (component_level[parent] + 1 for parent in condensed.predecessors(component)),
                default=0,
            )
        mapping = condensed.graph["mapping"]
        levels = {name: component_level[mapping[name]] for name in names}
    except ImportError:
        incoming = {name: 0 for name in names}
        adjacency: dict[str, list[str]] = {}
        for src, dst in edges:
            incoming[dst] += 1
            adjacency.setdefault(src, []).append(dst)
        roots = deque(sorted(name for name, count in incoming.items() if count == 0))
        levels = {name: 0 for name in names}
        seen = set(roots)
        while roots:
            src = roots.popleft()
            for dst in sorted(adjacency.get(src, [])):
                levels[dst] = max(levels[dst], levels[src] + 1)
                if dst not in seen:
                    seen.add(dst)
                    roots.append(dst)

    highest = max(levels.values(), default=0)
    if highest > 5:
        levels = {name: int(round(level * 5.0 / highest)) for name, level in levels.items()}
    return levels


def _reverse_dependents(graph: CodeGraph, fail: str) -> tuple[list[str], set[tuple[str, str]]]:
    incoming: dict[str, list[str]] = {}
    for edge in graph.edges:
        incoming.setdefault(edge.dst, []).append(edge.src)
    distance = {fail: 0}
    queue: deque[str] = deque([fail])
    while queue:
        node = queue.popleft()
        for dependent in sorted(incoming.get(node, [])):
            if dependent not in distance:
                distance[dependent] = distance[node] + 1
                queue.append(dependent)
    nodes = sorted(
        (node for node in distance if node != fail),
        key=lambda node: (distance[node], node),
    )
    impact_edges = {
        (edge.src, edge.dst)
        for edge in graph.edges
        if edge.src in distance and edge.dst in distance and distance[edge.src] > distance[edge.dst]
    }
    return nodes, impact_edges


def _module_plan(key: str, modules: list[CodeModule]) -> _GroupPlan:
    labels = [_relative_label(module.name, key) for module in modules]
    longest = max((len(label) for label in labels), default=8)
    node_width = clamp(longest * 21.0 + 72.0, 245.0, 430.0)
    count = len(modules)
    columns = 1 if count <= 4 else 2 if count <= 12 else 3
    rows = math.ceil(count / columns)
    gap_x, gap_y = 42.0, 38.0
    width = 74.0 + columns * node_width + max(0, columns - 1) * gap_x
    height = 116.0 + rows * 84.0 + max(0, rows - 1) * gap_y
    return _GroupPlan(
        key=key,
        label=key,
        members=tuple(modules),
        width=width,
        height=height,
        node_width=node_width,
        columns=columns,
    )


def build_module_blueprint(
    graph: CodeGraph,
    analysis: IncidentAnalysis,
    *,
    max_nodes: int = _MODULE_LIMIT,
) -> BlueprintLayout | None:
    """Build the package-grouped HLD with an incident overlay."""
    trace = analysis.internal_trace
    if trace is None or not trace.failing_module:
        return None
    overlay = incident_overlay(analysis, {graph.service: graph}) or {}
    fail = str(overlay.get("failing_module") or trace.failing_module)
    known = {module.name for module in graph.modules}
    if fail not in known:
        return None

    ranked = sorted(
        graph.modules,
        key=lambda module: (
            module.name != fail,
            -module.blast_radius,
            -module.fan_in,
            module.name,
        ),
    )
    selected = ranked[:max_nodes]
    selected_names = {module.name for module in selected}
    path = [
        name for name in dict.fromkeys(overlay.get("path_modules", [])) if name in selected_names
    ]

    by_group: dict[str, list[CodeModule]] = {}
    for module in selected:
        by_group.setdefault(_package(module.name), []).append(module)
    for modules in by_group.values():
        modules.sort(key=lambda module: module.name)
    plans = {key: _module_plan(key, modules) for key, modules in by_group.items()}

    group_edges = {
        (_package(edge.src), _package(edge.dst))
        for edge in graph.edges
        if edge.src in selected_names
        and edge.dst in selected_names
        and _package(edge.src) != _package(edge.dst)
    }
    levels = _dependency_levels(set(plans), group_edges)
    ordered_plans = sorted(
        plans.values(),
        key=lambda plan: (levels.get(plan.key, 0), plan.key),
    )
    total_area = sum(plan.width * plan.height for plan in ordered_plans)
    target_width = max(
        max((plan.width for plan in ordered_plans), default=900.0),
        math.sqrt(total_area * 4.0) + 450.0,
    )
    placement: dict[str, tuple[float, float]] = {}
    row_gap, col_gap = 95.0, 130.0
    cursor_x, cursor_y, row_height = 50.0, 50.0, 0.0
    world_width = 0.0
    for plan in ordered_plans:
        if cursor_x > 50.0 and cursor_x + plan.width > target_width + 50.0:
            cursor_x = 50.0
            cursor_y += row_height + row_gap
            row_height = 0.0
        placement[plan.key] = (cursor_x, cursor_y)
        cursor_x += plan.width + col_gap
        row_height = max(row_height, plan.height)
        world_width = max(world_width, cursor_x - col_gap + 50.0)
    world_height = max(900.0, cursor_y + row_height + 50.0)
    world_width = max(900.0, world_width)

    groups: list[GroupBox] = []
    nodes: dict[str, BlueprintNode] = {}
    for plan in ordered_plans:
        gx, gy = placement[plan.key]
        group_rect = Rect(gx, gy, plan.width, plan.height)
        groups.append(GroupBox(plan.key, plan.label, group_rect))
        for index, module in enumerate(plan.members):
            row, column = divmod(index, plan.columns)
            nx = gx + 37.0 + column * (plan.node_width + 42.0)
            ny = gy + 74.0 + row * (84.0 + 38.0)
            nodes[module.name] = BlueprintNode(
                key=module.name,
                label=_relative_label(module.name, plan.key),
                rect=Rect(nx, ny, plan.node_width, 84.0),
                group=plan.key,
                kind=module.kind,
                module=module.name,
                role=module.kind,
                loc=module.loc,
            )

    edges = [
        BlueprintEdge(edge.src, edge.dst, edge.kind)
        for edge in graph.edges
        if edge.src in nodes and edge.dst in nodes
    ]
    focus_edges = {
        edge.key
        for edge in edges
        if edge.src == fail or edge.dst == fail or (edge.src in path and edge.dst in path)
    }
    impacts, impact_edges = _reverse_dependents(graph, fail)
    impacts = [node for node in impacts if node in nodes]
    return BlueprintLayout(
        mode="module",
        title="MODULE BLUEPRINT · INCIDENT OVERLAY",
        stats=(f"{len(nodes)} modules · {len(edges)} edges · {len(groups)} package groups · HLD"),
        world=Rect(0.0, 0.0, world_width, world_height),
        nodes=nodes,
        groups=groups,
        edges=edges,
        fail_key=fail,
        focus_group=_package(fail),
        path_nodes=tuple(path),
        impact_nodes=tuple(impacts),
        focus_edges=frozenset(focus_edges),
        impact_edges=frozenset(impact_edges),
        failure_confirmed=module_failure_is_log_confirmed(trace, analysis),
    )


def _focused_symbols(
    graph: CodeGraph,
    trace: InternalTrace,
    max_nodes: int,
) -> tuple[list[CodeSymbol], str]:
    fail = trace.failing_symbol or ""
    focus_module = _module_for_symbol(graph, fail) or trace.failing_module or ""
    if not focus_module:
        return [], ""
    symbol_by_name = {symbol.qualname: symbol for symbol in graph.symbols}
    focus_names = {symbol.qualname for symbol in graph.symbols if symbol.module == focus_module}
    include_modules = {focus_module}
    for edge in graph.symbol_edges:
        if edge.src in focus_names and edge.dst in symbol_by_name:
            include_modules.add(symbol_by_name[edge.dst].module)
        if edge.dst in focus_names and edge.src in symbol_by_name:
            include_modules.add(symbol_by_name[edge.src].module)
    for qualname in list(trace.failing_symbol_callers) + list(trace.failing_symbol_callees):
        symbol = symbol_by_name.get(qualname)
        if symbol is not None:
            include_modules.add(symbol.module)

    degree: dict[str, int] = {}
    for edge in graph.symbol_edges:
        degree[edge.src] = degree.get(edge.src, 0) + 1
        degree[edge.dst] = degree.get(edge.dst, 0) + 1
    symbols = [
        symbol
        for symbol in graph.symbols
        if symbol.module in include_modules and symbol.kind != "module"
    ]
    symbols.sort(
        key=lambda symbol: (
            symbol.qualname != fail,
            symbol.module != focus_module,
            -degree.get(symbol.qualname, 0),
            symbol.qualname,
        )
    )
    return symbols[:max_nodes], focus_module


def _leaf_label(symbol: CodeSymbol) -> str:
    return symbol.name if symbol.kind == "class" else f"{symbol.name}()"


def _symbol_node_width(symbols: list[CodeSymbol]) -> float:
    longest = max((len(_leaf_label(symbol)) for symbol in symbols), default=8)
    return clamp(longest * 20.0 + 68.0, 250.0, 440.0)


def _grid_size(count: int, node_width: float, max_columns: int = 4) -> tuple[int, float, float]:
    if count <= 0:
        return 0, 0.0, 0.0
    columns = min(max_columns, max(1, math.ceil(math.sqrt(count * 1.35))))
    rows = math.ceil(count / columns)
    width = columns * node_width + max(0, columns - 1) * 42.0
    height = rows * 82.0 + max(0, rows - 1) * 38.0
    return columns, width, height


@dataclass(frozen=True)
class _SymbolModulePlan:
    module: str
    symbols: tuple[CodeSymbol, ...]
    loose: tuple[CodeSymbol, ...]
    classes: tuple[tuple[str, tuple[CodeSymbol, ...]], ...]
    width: float
    height: float
    node_width: float
    loose_columns: int
    loose_height: float


def _symbol_module_plan(module: str, symbols: list[CodeSymbol]) -> _SymbolModulePlan:
    node_width = _symbol_node_width(symbols)
    by_parent: dict[str, list[CodeSymbol]] = {}
    loose: list[CodeSymbol] = []
    for symbol in symbols:
        if symbol.parent:
            by_parent.setdefault(symbol.parent, []).append(symbol)
        elif symbol.kind == "class":
            by_parent.setdefault(symbol.qualname, []).append(symbol)
        else:
            loose.append(symbol)
    loose.sort(key=lambda symbol: symbol.qualname)
    classes: list[tuple[str, tuple[CodeSymbol, ...]]] = []
    for parent, members in sorted(by_parent.items()):
        members.sort(key=lambda symbol: (symbol.kind != "class", symbol.qualname))
        classes.append((parent, tuple(members)))

    loose_columns, loose_width, loose_height = _grid_size(len(loose), node_width)
    class_widths: list[float] = []
    class_heights: list[float] = []
    for _parent, class_members in classes:
        _cols, width, height = _grid_size(len(class_members), node_width, max_columns=2)
        class_widths.append(width + 64.0)
        class_heights.append(height + 104.0)
    classes_width = max(class_widths, default=0.0)
    classes_height = sum(class_heights) + max(0, len(classes) - 1) * 48.0
    content_width = max(loose_width, classes_width, node_width)
    content_height = loose_height
    if loose and classes:
        content_height += 56.0
    content_height += classes_height
    return _SymbolModulePlan(
        module=module,
        symbols=tuple(symbols),
        loose=tuple(loose),
        classes=tuple(classes),
        width=content_width + 88.0,
        height=content_height + 126.0,
        node_width=node_width,
        loose_columns=loose_columns,
        loose_height=loose_height,
    )


def _shelf_positions(
    plans: list[_SymbolModulePlan],
) -> tuple[dict[str, tuple[float, float]], float, float]:
    total_area = sum(plan.width * plan.height for plan in plans)
    target = max(max((plan.width for plan in plans), default=900.0), math.sqrt(total_area * 4.0))
    positions: dict[str, tuple[float, float]] = {}
    x, y, row_height = 45.0, 45.0, 0.0
    max_right = 0.0
    for plan in plans:
        if x > 45.0 and x + plan.width > target + 45.0:
            x = 45.0
            y += row_height + 100.0
            row_height = 0.0
        positions[plan.module] = (x, y)
        x += plan.width + 105.0
        row_height = max(row_height, plan.height)
        max_right = max(max_right, x - 105.0)
    return positions, max_right + 45.0, y + row_height + 45.0


def build_symbol_blueprint(
    graph: CodeGraph,
    analysis: IncidentAnalysis,
    *,
    max_nodes: int = _SYMBOL_LIMIT,
) -> BlueprintLayout | None:
    """Build the LLD: functions inside classes inside modules."""
    trace = analysis.internal_trace
    if trace is None or not trace.failing_symbol:
        return None
    symbols, focus_module = _focused_symbols(graph, trace, max_nodes)
    selected = {symbol.qualname: symbol for symbol in symbols}
    if trace.failing_symbol not in selected:
        return None

    by_module: dict[str, list[CodeSymbol]] = {}
    for symbol in symbols:
        by_module.setdefault(symbol.module, []).append(symbol)
    plans = [_symbol_module_plan(module, members) for module, members in by_module.items()]
    plans.sort(
        key=lambda plan: (
            plan.module != focus_module,
            -(plan.width * plan.height),
            plan.module,
        )
    )
    positions, world_width, world_height = _shelf_positions(plans)

    groups: list[GroupBox] = []
    nodes: dict[str, BlueprintNode] = {}
    for plan in plans:
        mx, my = positions[plan.module]
        module_rect = Rect(mx, my, plan.width, plan.height)
        groups.append(GroupBox(plan.module, plan.module, module_rect))
        content_x = mx + 44.0
        cursor_y = my + 76.0

        for index, symbol in enumerate(plan.loose):
            row, column = divmod(index, max(1, plan.loose_columns))
            nx = content_x + column * (plan.node_width + 42.0)
            ny = cursor_y + row * (82.0 + 38.0)
            nodes[symbol.qualname] = BlueprintNode(
                key=symbol.qualname,
                label=_leaf_label(symbol),
                rect=Rect(nx, ny, plan.node_width, 82.0),
                group=plan.module,
                kind=symbol.kind,
                module=symbol.module,
                role=symbol.role,
                lineno=symbol.lineno,
                loc=symbol.loc,
            )
        cursor_y += plan.loose_height
        if plan.loose and plan.classes:
            cursor_y += 56.0

        for parent, members in plan.classes:
            columns, content_width, content_height = _grid_size(
                len(members), plan.node_width, max_columns=2
            )
            class_width = content_width + 64.0
            class_height = content_height + 104.0
            class_x = content_x + max(0.0, (plan.width - 88.0 - class_width) / 2.0)
            class_rect = Rect(class_x, cursor_y, class_width, class_height)
            class_name = parent.rsplit(".", 1)[-1]
            groups.append(
                GroupBox(
                    key=parent,
                    label=f"class {class_name}",
                    rect=class_rect,
                    depth=1,
                    parent=plan.module,
                )
            )
            for index, symbol in enumerate(members):
                row, column = divmod(index, max(1, columns))
                nx = class_x + 32.0 + column * (plan.node_width + 42.0)
                ny = cursor_y + 64.0 + row * (82.0 + 38.0)
                nodes[symbol.qualname] = BlueprintNode(
                    key=symbol.qualname,
                    label=_leaf_label(symbol),
                    rect=Rect(nx, ny, plan.node_width, 82.0),
                    group=parent,
                    kind=symbol.kind,
                    module=symbol.module,
                    role=symbol.role,
                    lineno=symbol.lineno,
                    loc=symbol.loc,
                )
            cursor_y += class_height + 48.0

    edges = [
        BlueprintEdge(edge.src, edge.dst, edge.kind)
        for edge in graph.symbol_edges
        if edge.src in nodes and edge.dst in nodes
    ]
    fail = trace.failing_symbol
    focus_edges = {edge.key for edge in edges if edge.src == fail or edge.dst == fail}
    direct = {
        name
        for edge in edges
        if edge.key in focus_edges
        for name in (edge.src, edge.dst)
        if name != fail
    }
    # The broader resolved-call network is revealed too, but the direct
    # caller/callee edges remain brightest around the candidate.
    all_edges = {edge.key for edge in edges}
    return BlueprintLayout(
        mode="symbol",
        title="FUNCTION BLUEPRINT · STATIC CALL GRAPH",
        stats=(
            f"{len(nodes)} symbols · {len(edges)} resolved calls · {len(by_module)} modules · LLD"
        ),
        world=Rect(0.0, 0.0, world_width, world_height),
        nodes=nodes,
        groups=groups,
        edges=edges,
        fail_key=fail,
        focus_group=focus_module,
        path_nodes=tuple(sorted(direct)),
        impact_nodes=(),
        focus_edges=frozenset(all_edges),
        impact_edges=frozenset(),
        failure_confirmed=False,
    )


def _curve_points(a: Rect, b: Rect, samples: int = 24) -> list[tuple[float, float]]:
    dx, dy = b.cx - a.cx, b.cy - a.cy
    if abs(dx) >= abs(dy):
        sign = 1.0 if dx >= 0.0 else -1.0
        start = (a.cx + sign * a.w / 2.0, a.cy)
        end = (b.cx - sign * b.w / 2.0, b.cy)
        bend = max(55.0, abs(end[0] - start[0]) * 0.48)
        c1 = (start[0] + sign * bend, start[1])
        c2 = (end[0] - sign * bend, end[1])
    else:
        sign = 1.0 if dy >= 0.0 else -1.0
        start = (a.cx, a.cy + sign * a.h / 2.0)
        end = (b.cx, b.cy - sign * b.h / 2.0)
        bend = max(48.0, abs(end[1] - start[1]) * 0.48)
        c1 = (start[0], start[1] + sign * bend)
        c2 = (end[0], end[1] - sign * bend)
    points: list[tuple[float, float]] = []
    for index in range(samples):
        t = index / (samples - 1)
        mt = 1.0 - t
        x = mt**3 * start[0] + 3.0 * mt**2 * t * c1[0] + 3.0 * mt * t**2 * c2[0] + t**3 * end[0]
        y = mt**3 * start[1] + 3.0 * mt**2 * t * c1[1] + 3.0 * mt * t**2 * c2[1] + t**3 * end[1]
        points.append((x, y))
    return points


class BlueprintScene:
    """A deterministic 2D scene with the same ``scene_frame`` movie contract."""

    def __init__(
        self,
        analysis: IncidentAnalysis,
        layout: BlueprintLayout,
        window: tuple[float, float, float],
        spec: RenderSpec,
    ) -> None:
        self.analysis = analysis
        self.layout = layout
        self._window = window
        self.spec = spec
        self.ssf = spec.ss_size[1] / 1080.0
        self._background = self._build_background()
        self._edge_order = {
            edge.key: index
            for index, edge in enumerate(
                sorted(
                    layout.edges,
                    key=lambda edge: (
                        edge.key not in layout.focus_edges,
                        edge.src,
                        edge.dst,
                    ),
                )
            )
        }
        self._path_order = {node: index for index, node in enumerate(layout.path_nodes)}
        self._impact_order = {node: index for index, node in enumerate(layout.impact_nodes)}
        self._fail_at = window[1] + (0.65 if layout.mode == "module" else 0.8)
        self._reveal_start = window[0] + 0.9
        self._reveal_end = max(self._reveal_start + 0.8, window[1] - 0.35)
        self._camera = self._build_camera()

    def _build_background(self) -> Image.Image:
        width, height = self.spec.ss_size
        y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        top = np.array(palette.BG_TOP, dtype=np.float32)
        bottom = np.array(palette.BG_BOTTOM, dtype=np.float32)
        gradient = top[None, None, :] * (1.0 - y[..., None]) + bottom[None, None, :] * y[..., None]
        xs = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
        ys = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
        vignette = 1.0 - 0.15 * np.clip((xs * xs * 1.1 + ys * ys) / 2.1, 0.0, 1.0) ** 1.5
        gradient = gradient * vignette[..., None]
        rng = np.random.default_rng(19 if self.layout.mode == "module" else 23)
        gradient += rng.uniform(-1.0, 1.0, gradient.shape).astype(np.float32)
        return Image.fromarray(np.clip(gradient, 0, 255).astype(np.uint8), "RGB")

    def _panel_geometry(self) -> tuple[Rect, Rect]:
        width, height = self.spec.ss_size
        u = self.ssf
        panel = Rect(
            48.0 * u,
            145.0 * u,
            width - 96.0 * u,
            height - 447.0 * u,
        )
        body = Rect(
            panel.x + 14.0 * u,
            panel.y + 62.0 * u,
            panel.w - 28.0 * u,
            panel.h - 76.0 * u,
        )
        return panel, body

    def _fit_zoom(self, rect: Rect, body: Rect, coverage: float) -> float:
        world = self.layout.world
        base = min(body.w / max(1.0, world.w), body.h / max(1.0, world.h))
        target = min(
            body.w * coverage / max(1.0, rect.w),
            body.h * coverage / max(1.0, rect.h),
        )
        return clamp(target / max(0.001, base), 1.0, 3.6)

    def _build_camera(self) -> tuple[_CameraStop, ...]:
        start, fail_beat, end = self._window
        _panel, body = self._panel_geometry()
        world = self.layout.world
        focus_group = next(
            (group.rect for group in self.layout.groups if group.key == self.layout.focus_group),
            world,
        )
        fail_rect = self.layout.nodes[self.layout.fail_key].rect
        group_zoom = self._fit_zoom(focus_group, body, 0.74)
        node_zoom = clamp(
            self._fit_zoom(fail_rect, body, 0.24),
            group_zoom,
            3.4 if self.layout.mode == "module" else 3.6,
        )
        handoff_rect = focus_group
        if self.layout.mode == "module" and self.layout.impact_nodes:
            context = [
                self.layout.nodes[key].rect
                for key in (self.layout.fail_key, *self.layout.impact_nodes)
                if key in self.layout.nodes
            ]
            left = min(rect.x for rect in context)
            top = min(rect.y for rect in context)
            right = max(rect.right for rect in context)
            bottom = max(rect.bottom for rect in context)
            handoff_rect = Rect(left, top, right - left, bottom - top)
            handoff_zoom = min(
                1.55,
                self._fit_zoom(handoff_rect, body, 0.82),
            )
        else:
            handoff_zoom = max(1.15, group_zoom * 0.88)
        pre = max(1.0, fail_beat - start)
        handoff_at = min(
            end - 0.8,
            fail_beat + max(2.0, (end - fail_beat) * 0.62),
        )
        return (
            _CameraStop(start, world.cx, world.cy, 0.92),
            _CameraStop(start + pre * 0.28, world.cx, world.cy, 1.0),
            _CameraStop(
                start + pre * 0.68,
                focus_group.cx,
                focus_group.cy,
                group_zoom,
            ),
            _CameraStop(fail_beat + 0.45, fail_rect.cx, fail_rect.cy, node_zoom),
            _CameraStop(
                max(fail_beat + 1.2, handoff_at),
                handoff_rect.cx,
                handoff_rect.cy,
                handoff_zoom,
            ),
        )

    def _camera_at(self, t: float) -> _CameraStop:
        if t <= self._camera[0].at:
            return self._camera[0]
        for left, right in zip(self._camera, self._camera[1:], strict=False):
            if t <= right.at:
                k = ease_in_out_quint((t - left.at) / max(0.001, right.at - left.at))
                return _CameraStop(
                    at=t,
                    cx=left.cx + (right.cx - left.cx) * k,
                    cy=left.cy + (right.cy - left.cy) * k,
                    zoom=left.zoom + (right.zoom - left.zoom) * k,
                )
        return self._camera[-1]

    def _node_colors(
        self, node: BlueprintNode, t: float
    ) -> tuple[tuple[int, int, int], tuple[int, int, int], bool]:
        role_stroke = _ROLE_COLOR.get(node.role, _ROLE_COLOR["module"])
        fill = (24, 30, 43)
        stroke = mix_color((66, 78, 103), role_stroke, 0.58)
        selected = False

        path_at = self._reveal_start
        if node.key in self._path_order:
            count = max(1, len(self._path_order) - 1)
            path_at += (self._reveal_end - self._reveal_start) * self._path_order[node.key] / count
            k = smoothstep((t - path_at) / 0.55)
            fill = mix_color(fill, palette.STATE_TOP["recovery"], k)
            stroke = mix_color(stroke, palette.STATE_STROKE["recovery"], k)

        if node.key == self.layout.fail_key:
            selected = True
            enter_at = self._reveal_start + (self._reveal_end - self._reveal_start) * 0.58
            enter = smoothstep((t - enter_at) / 0.55)
            fill = mix_color(fill, palette.STATE_TOP["recovery"], enter)
            stroke = mix_color(stroke, palette.STATE_STROKE["recovery"], enter)
            state = "critical" if self.layout.failure_confirmed else "warning"
            fail = smoothstep((t - self._fail_at) / 0.6)
            fill = mix_color(fill, palette.STATE_TOP[state], fail)
            stroke = mix_color(stroke, palette.STATE_STROKE[state], fail)

        if node.key in self._impact_order:
            at = self._fail_at + 0.45 + self._impact_order[node.key] * 0.16
            k = smoothstep((t - at) / 0.55)
            fill = mix_color(fill, palette.STATE_TOP["warning"], k * 0.76)
            stroke = mix_color(stroke, palette.STATE_STROKE["warning"], k)
        return fill, stroke, selected

    def _edge_progress(self, edge: BlueprintEdge, t: float) -> tuple[float, str]:
        if edge.key in self.layout.impact_edges and t >= self._fail_at + 0.25:
            index = self._edge_order.get(edge.key, 0)
            return clamp((t - self._fail_at - 0.25 - index * 0.025) / 0.7), "warning"
        if edge.key not in self.layout.focus_edges:
            return 0.0, "idle"
        total = max(1, len(self.layout.focus_edges) - 1)
        index = self._edge_order.get(edge.key, 0)
        at = (
            self._reveal_start + (self._reveal_end - self._reveal_start) * min(index, total) / total
        )
        return clamp((t - at) / 0.75), "recovery"

    @staticmethod
    def _transform_rect(
        rect: Rect, camera: _CameraStop, scale: float, width: int, height: int
    ) -> Rect:
        return Rect(
            (rect.x - camera.cx) * scale + width / 2.0,
            (rect.y - camera.cy) * scale + height / 2.0,
            rect.w * scale,
            rect.h * scale,
        )

    @staticmethod
    def _transform_points(
        points: list[tuple[float, float]],
        camera: _CameraStop,
        scale: float,
        width: int,
        height: int,
    ) -> list[tuple[float, float]]:
        return [
            (
                (x - camera.cx) * scale + width / 2.0,
                (y - camera.cy) * scale + height / 2.0,
            )
            for x, y in points
        ]

    @staticmethod
    def _visible(rect: Rect, width: int, height: int, pad: float = 20.0) -> bool:
        return not (
            rect.right < -pad or rect.x > width + pad or rect.bottom < -pad or rect.y > height + pad
        )

    def _draw_groups(
        self,
        draw: ImageDraw.ImageDraw,
        camera: _CameraStop,
        scale: float,
        width: int,
        height: int,
    ) -> None:
        for group in sorted(self.layout.groups, key=lambda item: item.depth):
            rect = self._transform_rect(group.rect, camera, scale, width, height)
            if not self._visible(rect, width, height):
                continue
            depth = group.depth
            fill = (16, 22, 33, 190) if depth == 0 else (21, 27, 40, 218)
            outline = (
                with_alpha(palette.PANEL_LINE, 0.76)
                if depth == 0
                else with_alpha(palette.ACCENT, 0.48)
            )
            radius = max(4, int((16 if depth == 0 else 12) * self.ssf))
            line_width = max(1, int((2.0 if depth == 0 else 1.5) * self.ssf))
            draw.rounded_rectangle(
                (rect.x, rect.y, rect.right, rect.bottom),
                radius=radius,
                fill=fill,
                outline=outline,
                width=line_width,
            )
            label_size = int(
                clamp(
                    (30.0 if depth == 0 else 26.0) * scale,
                    12.0 * self.ssf,
                    (25.0 if depth == 0 else 21.0) * self.ssf,
                )
            )
            label_font = font("mono-bold" if depth == 0 else "mono", label_size)
            draw.text(
                (rect.x + 18.0 * self.ssf, rect.y + 13.0 * self.ssf),
                group.label,
                font=label_font,
                fill=with_alpha(
                    palette.TEXT if depth == 0 else palette.ACCENT,
                    0.84,
                ),
            )

    @staticmethod
    def _draw_dashed(
        draw: ImageDraw.ImageDraw,
        points: list[tuple[float, float]],
        fill: tuple[int, int, int, int],
        width: int,
    ) -> None:
        for index in range(0, len(points) - 1, 2):
            draw.line(
                (points[index], points[index + 1]),
                fill=fill,
                width=width,
            )

    def _draw_edges(
        self,
        draw: ImageDraw.ImageDraw,
        glow_draw: ImageDraw.ImageDraw,
        camera: _CameraStop,
        scale: float,
        width: int,
        height: int,
        t: float,
    ) -> None:
        for edge in self.layout.edges:
            src = self.layout.nodes[edge.src]
            dst = self.layout.nodes[edge.dst]
            points = self._transform_points(
                _curve_points(src.rect, dst.rect),
                camera,
                scale,
                width,
                height,
            )
            if not any(
                -30.0 <= x <= width + 30.0 and -30.0 <= y <= height + 30.0 for x, y in points
            ):
                continue
            base_width = max(1, int(clamp(3.2 * scale, 1.0, 2.2 * self.ssf)))
            if edge.kind in ("import", "dynamic", "dynamic-import"):
                self._draw_dashed(
                    draw,
                    points,
                    with_alpha(palette.EDGE_IDLE, 0.34),
                    base_width,
                )
            else:
                draw.line(
                    points,
                    fill=with_alpha(palette.EDGE_IDLE, 0.32),
                    width=base_width,
                    joint="curve",
                )

            progress, state = self._edge_progress(edge, t)
            if progress <= 0.001:
                continue
            count = max(2, int(math.ceil(len(points) * progress)))
            active_points = points[:count]
            color = palette.STATE_STROKE[state]
            active_width = max(2, int(clamp(5.0 * scale, 1.7, 3.3 * self.ssf)))
            draw.line(
                active_points,
                fill=with_alpha(color, 0.9),
                width=active_width,
                joint="curve",
            )
            glow_draw.line(
                active_points,
                fill=with_alpha(color, 0.64),
                width=active_width * 4,
                joint="curve",
            )
            if progress < 0.96 or len(active_points) < 3:
                continue
            tip = active_points[-1]
            previous = active_points[-3]
            angle = math.atan2(tip[1] - previous[1], tip[0] - previous[0])
            arrow = max(4.0, 9.0 * self.ssf)
            draw.polygon(
                [
                    tip,
                    (
                        tip[0] - arrow * math.cos(angle - 0.45),
                        tip[1] - arrow * math.sin(angle - 0.45),
                    ),
                    (
                        tip[0] - arrow * math.cos(angle + 0.45),
                        tip[1] - arrow * math.sin(angle + 0.45),
                    ),
                ],
                fill=with_alpha(color, 0.95),
            )

    def _draw_nodes(
        self,
        draw: ImageDraw.ImageDraw,
        glow_draw: ImageDraw.ImageDraw,
        camera: _CameraStop,
        scale: float,
        width: int,
        height: int,
        t: float,
    ) -> dict[str, Rect]:
        screen_nodes: dict[str, Rect] = {}
        for node in self.layout.nodes.values():
            rect = self._transform_rect(node.rect, camera, scale, width, height)
            if not self._visible(rect, width, height):
                continue
            screen_nodes[node.key] = rect
            fill, stroke, selected = self._node_colors(node, t)
            radius = max(3, int(10.0 * self.ssf))
            line_width = max(1, int(clamp(4.2 * scale, 1.4, 3.0 * self.ssf)))
            if selected and t >= self._fail_at - 0.2:
                state = "critical" if self.layout.failure_confirmed else "warning"
                glow_color = palette.STATE_STROKE[state]
                grow = (7.0 + 4.0 * pulse(t, hz=0.85)) * self.ssf
                glow_draw.rounded_rectangle(
                    (
                        rect.x - grow,
                        rect.y - grow,
                        rect.right + grow,
                        rect.bottom + grow,
                    ),
                    radius=radius + int(grow),
                    outline=with_alpha(glow_color, 0.78),
                    width=max(2, line_width * 4),
                )
            draw.rounded_rectangle(
                (rect.x, rect.y, rect.right, rect.bottom),
                radius=radius,
                fill=with_alpha(fill, 0.97),
                outline=with_alpha(stroke, 0.98),
                width=line_width,
            )
            label_size = int(
                clamp(
                    27.0 * scale,
                    11.0 * self.ssf,
                    25.0 * self.ssf,
                )
            )
            label_font = fit_text(
                draw,
                node.label,
                "mono-bold" if selected else "mono",
                label_size,
                max(10, int(rect.w - 22.0 * self.ssf)),
            )
            bbox = draw.textbbox((0, 0), node.label, font=label_font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            draw.text(
                (
                    rect.cx - text_w / 2.0,
                    rect.cy - text_h / 2.0 - bbox[1],
                ),
                node.label,
                font=label_font,
                fill=with_alpha(palette.TEXT, 0.94),
            )
        return screen_nodes

    def _draw_annotation(
        self,
        draw: ImageDraw.ImageDraw,
        screen_nodes: dict[str, Rect],
        width: int,
        height: int,
        t: float,
    ) -> None:
        node_rect = screen_nodes.get(self.layout.fail_key)
        if node_rect is None:
            return
        alpha = smoothstep((t - self._fail_at - 0.25) / 0.5)
        alpha *= smoothstep((self._window[2] - t) / 0.45)
        if self.layout.mode == "module":
            # Hold the evidence card on the locus, then clear it before the
            # camera pulls back to reveal the full amber blast-radius overlay.
            alpha *= smoothstep((self._fail_at + 5.2 - t) / 0.75)
        if alpha <= 0.01:
            return
        u = self.ssf
        card_w = min(width - int(24 * u), int(610 * u))
        card_h = int(116 * u)
        if node_rect.cx < width * 0.54:
            x = min(width - card_w - int(12 * u), node_rect.right + int(28 * u))
            anchor_x = x
        else:
            x = max(int(12 * u), node_rect.x - card_w - int(28 * u))
            anchor_x = x + card_w
        y = clamp(
            node_rect.cy - card_h / 2.0,
            12.0 * u,
            height - card_h - 12.0 * u,
        )
        state = "critical" if self.layout.failure_confirmed else "warning"
        color = palette.STATE_STROKE[state]
        draw.line(
            (node_rect.cx, node_rect.cy, anchor_x, y + card_h / 2.0),
            fill=with_alpha(color, 0.7 * alpha),
            width=max(1, int(2 * u)),
        )
        draw.rounded_rectangle(
            (x, y, x + card_w, y + card_h),
            radius=int(12 * u),
            fill=(12, 16, 24, int(244 * alpha)),
            outline=with_alpha(color, 0.92 * alpha),
            width=max(1, int(2 * u)),
        )
        draw.rectangle(
            (x, y, x + int(6 * u), y + card_h),
            fill=with_alpha(color, alpha),
        )
        if self.layout.mode == "module":
            heading = (
                "LOG-CONFIRMED MODULE"
                if self.layout.failure_confirmed
                else "STATIC MODULE ATTRIBUTION"
            )
            node = self.layout.nodes[self.layout.fail_key]
            detail = (
                f"{node.loc} LOC · blast radius {len(self.layout.impact_nodes)} "
                "potential dependents"
            )
        else:
            heading = "STATIC CANDIDATE · NO STACK FRAME"
            node = self.layout.nodes[self.layout.fail_key]
            detail = f"line {node.lineno or '?'} · {node.kind} · runtime frame unconfirmed"
        draw.text(
            (x + int(22 * u), y + int(14 * u)),
            heading,
            font=font("mono-bold", int(14 * u)),
            fill=with_alpha(color, 0.96 * alpha),
        )
        qual_font = fit_text(
            draw,
            self.layout.fail_key,
            "mono-bold",
            int(18 * u),
            card_w - int(42 * u),
        )
        draw.text(
            (x + int(22 * u), y + int(43 * u)),
            self.layout.fail_key,
            font=qual_font,
            fill=with_alpha(palette.TEXT, 0.98 * alpha),
        )
        draw.text(
            (x + int(22 * u), y + int(78 * u)),
            detail,
            font=font("mono", int(13 * u)),
            fill=with_alpha(palette.DIM, 0.9 * alpha),
        )

    def _draw_panel_header(self, img: Image.Image, panel: Rect, body: Rect) -> None:
        draw = ImageDraw.Draw(img, "RGBA")
        u = self.ssf
        draw.rounded_rectangle(
            (panel.x, panel.y, panel.right, panel.bottom),
            radius=int(18 * u),
            fill=(12, 17, 26, 224),
            outline=with_alpha(palette.PANEL_LINE, 0.92),
            width=max(1, int(2 * u)),
        )
        draw.line(
            (
                panel.x,
                body.y - int(10 * u),
                panel.right,
                body.y - int(10 * u),
            ),
            fill=with_alpha(palette.PANEL_LINE, 0.72),
            width=max(1, int(1.5 * u)),
        )
        title_font = font("mono-bold", int(20 * u))
        draw.text(
            (panel.x + int(22 * u), panel.y + int(20 * u)),
            self.layout.title,
            font=title_font,
            fill=with_alpha(palette.TEXT, 0.96),
        )
        stats_font = font("mono", int(13 * u))
        stats_width = draw.textlength(self.layout.stats, font=stats_font)
        draw.text(
            (
                panel.right - int(22 * u) - stats_width,
                panel.y + int(24 * u),
            ),
            self.layout.stats,
            font=stats_font,
            fill=with_alpha(palette.DIM, 0.88),
        )

    def _draw_grid(self, image: Image.Image) -> None:
        draw = ImageDraw.Draw(image, "RGBA")
        width, height = image.size
        spacing = max(18, int(34 * self.ssf))
        line_width = max(1, int(self.ssf))
        for x in range(0, width, spacing):
            draw.line(
                (x, 0, x, height),
                fill=with_alpha(palette.GRID_LINE, 0.20),
                width=line_width,
            )
        for y in range(0, height, spacing):
            draw.line(
                (0, y, width, y),
                fill=with_alpha(palette.GRID_LINE, 0.20),
                width=line_width,
            )

    def scene_frame(self, t: float) -> Image.Image:
        img = self._background.copy()
        panel, body = self._panel_geometry()
        self._draw_panel_header(img, panel, body)

        graph_layer = Image.new("RGBA", (max(1, int(body.w)), max(1, int(body.h))), (0, 0, 0, 0))
        self._draw_grid(graph_layer)
        draw = ImageDraw.Draw(graph_layer, "RGBA")
        glow = Image.new("RGBA", graph_layer.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow, "RGBA")
        camera = self._camera_at(t)
        world = self.layout.world
        base_scale = (
            min(
                graph_layer.width / max(1.0, world.w),
                graph_layer.height / max(1.0, world.h),
            )
            * 0.94
        )
        scale = base_scale * camera.zoom
        self._draw_groups(draw, camera, scale, graph_layer.width, graph_layer.height)
        self._draw_edges(
            draw,
            glow_draw,
            camera,
            scale,
            graph_layer.width,
            graph_layer.height,
            t,
        )
        screen_nodes = self._draw_nodes(
            draw,
            glow_draw,
            camera,
            scale,
            graph_layer.width,
            graph_layer.height,
            t,
        )
        blurred = glow.filter(ImageFilter.GaussianBlur(radius=8.0 * self.ssf))
        graph_layer = Image.alpha_composite(blurred, graph_layer)
        draw = ImageDraw.Draw(graph_layer, "RGBA")
        self._draw_annotation(draw, screen_nodes, graph_layer.width, graph_layer.height, t)
        img.paste(
            graph_layer,
            (int(body.x), int(body.y)),
            graph_layer,
        )
        return img


class ModuleBlueprintScene(BlueprintScene):
    pass


class SymbolBlueprintScene(BlueprintScene):
    pass


__all__ = [
    "BlueprintLayout",
    "BlueprintScene",
    "GroupBox",
    "ModuleBlueprintScene",
    "Rect",
    "SymbolBlueprintScene",
    "build_module_blueprint",
    "build_symbol_blueprint",
]
