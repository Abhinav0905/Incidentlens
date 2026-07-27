"""Frame rendering for the incident video.

This is the server-side port of the browser replay in ``static/app.js``:
the same dependency-graph layout and the same per-frame state folding, drawn
as a self-contained SVG per beat and rasterized to PNG. All styling is inlined
(no CSS classes, no animations) so the rasterizer renders exactly what we mean.
"""

from __future__ import annotations

from dataclasses import dataclass

import cairosvg  # type: ignore[import-untyped]

from incidentlens.domain.models import ArchitectureGraph, IncidentAnalysis
from incidentlens.studio.theme import PALETTE

# Graph-space geometry (matches app.js constants).
NODE_H = 44.0
COL_GAP = 220.0
ROW_GAP = 74.0
PAD = 28.0

# Canvas geometry.
CANVAS_W = 1280
CANVAS_H = 720
GRAPH_BOX = (48.0, 104.0, CANVAS_W - 48.0, CANVAS_H - 236.0)  # x0, y0, x1, y1
MAX_SCALE = 1.7

_SEVERITY_RANK = {"healthy": 0, "info": 0, "warning": 1, "critical": 2, "recovery": 3}


@dataclass(frozen=True)
class _NodeBox:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class GraphLayout:
    boxes: dict[str, _NodeBox]
    width: float
    height: float


def _node_width(name: str) -> float:
    return max(128.0, len(name) * 7.6 + 26.0)


def compute_layout(architecture: ArchitectureGraph) -> GraphLayout:
    services = architecture.services
    by_name = {s.name: s for s in services}
    depth: dict[str, int] = {}

    queue: list[str] = []
    for s in services:
        if s.user_facing:
            depth[s.name] = 0
            queue.append(s.name)
    while queue:
        name = queue.pop(0)
        d = depth[name]
        for dep in by_name[name].depends_on:
            if dep not in by_name:
                continue
            if dep not in depth or d + 1 > depth[dep]:
                depth[dep] = d + 1
                queue.append(dep)

    changed = True
    while changed:
        changed = False
        for s in services:
            if s.name in depth:
                continue
            deps = [d for d in s.depends_on if d in depth]
            if deps:
                depth[s.name] = max(depth[d] for d in deps) + 1
                changed = True
    max_known = max(depth.values()) if depth else 0
    for s in services:
        depth.setdefault(s.name, max_known + 1)

    cols: dict[int, list[str]] = {}
    for s in services:
        cols.setdefault(depth[s.name], []).append(s.name)
    depths = sorted(cols)
    max_rows = max((len(cols[d]) for d in depths), default=1)
    height = PAD * 2 + max_rows * NODE_H + (max_rows - 1) * (ROW_GAP - NODE_H)

    boxes: dict[str, _NodeBox] = {}
    for col_idx, d in enumerate(depths):
        bucket = cols[d]
        col_h = len(bucket) * NODE_H + (len(bucket) - 1) * (ROW_GAP - NODE_H)
        start_y = (height - col_h) / 2
        for row_idx, name in enumerate(bucket):
            boxes[name] = _NodeBox(
                x=PAD + col_idx * COL_GAP,
                y=start_y + row_idx * ROW_GAP,
                w=_node_width(name),
                h=NODE_H,
            )
    width = PAD * 2 + (len(depths) - 1) * COL_GAP + 160
    return GraphLayout(boxes=boxes, width=width, height=height)


def fold_states(
    analysis: IncidentAnalysis, upto_index: int
) -> tuple[dict[str, str], dict[str, int]]:
    """Node states and first-degraded index after replaying frames 0..upto_index."""
    node_state: dict[str, str] = {}
    failed_at: dict[str, int] = {}
    for i in range(0, upto_index + 1):
        frame = analysis.timeline[i]
        sev = frame.severity.value
        for svc in frame.services:
            if sev == "recovery":
                node_state[svc] = "recovery"
            elif sev in ("warning", "critical"):
                if node_state.get(svc) == "recovery":
                    continue
                current = node_state.get(svc)
                if current is None or _SEVERITY_RANK[sev] >= _SEVERITY_RANK[current]:
                    node_state[svc] = sev
                failed_at.setdefault(svc, i)
    return node_state, failed_at


def active_edges(
    analysis: IncidentAnalysis, failed_at: dict[str, int]
) -> set[tuple[str, str]]:
    """Active propagation edges as direction-insensitive sorted pairs.

    A drawn edge runs service -> dependency, while a propagation step is
    recorded cause -> effect, so the two can point opposite ways. Matching on
    the sorted pair lets either orientation light up the same edge.
    """
    edges: set[tuple[str, str]] = set()
    for step in analysis.propagation:
        if step.from_service in failed_at and step.to_service in failed_at:
            edges.add(tuple(sorted((step.from_service, step.to_service))))  # type: ignore[arg-type]
    return edges


def changed_services(analysis: IncidentAnalysis, index: int) -> set[str]:
    if index < 0 or index >= len(analysis.timeline):
        return set()
    frame = analysis.timeline[index]
    if frame.severity.value in ("warning", "critical", "recovery"):
        return set(frame.services)
    return set()


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def _state_colors(state: str) -> tuple[str, str]:
    """Return (stroke, fill) for a node state."""
    if state == "critical":
        return PALETTE["critical"], PALETTE["critical_fill"]
    if state == "warning":
        return PALETTE["warning"], PALETTE["warning_fill"]
    if state == "recovery":
        return PALETTE["recovery"], PALETTE["recovery_fill"]
    return PALETTE["healthy"], PALETTE["panel"]


def render_frame_svg(
    analysis: IncidentAnalysis,
    architecture: ArchitectureGraph,
    layout: GraphLayout,
    *,
    timeline_index: int,
    clock_label: str,
    caption_title: str,
    caption_body: str,
    evidence_label: str,
    progress: float,
) -> str:
    node_state, failed_at = fold_states(analysis, timeline_index)
    live_edges = active_edges(analysis, failed_at)
    highlight = changed_services(analysis, timeline_index)

    gx0, gy0, gx1, gy1 = GRAPH_BOX
    scale = min((gx1 - gx0) / layout.width, (gy1 - gy0) / layout.height, MAX_SCALE)
    draw_w = layout.width * scale
    draw_h = layout.height * scale
    off_x = gx0 + ((gx1 - gx0) - draw_w) / 2
    off_y = gy0 + ((gy1 - gy0) - draw_h) / 2

    def px(x: float) -> float:
        return off_x + x * scale

    def py(y: float) -> float:
        return off_y + y * scale

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS_W} {CANVAS_H}">',
        f'<rect width="{CANVAS_W}" height="{CANVAS_H}" fill="{PALETTE["bg"]}"/>',
    ]

    # Header.
    parts.append(
        f'<text x="48" y="52" fill="{PALETTE["text"]}" font-family="sans-serif" '
        f'font-size="24" font-weight="600">{_esc(analysis.title)}</text>'
    )
    parts.append(
        f'<text x="48" y="78" fill="{PALETTE["dim"]}" font-family="monospace" '
        f'font-size="14">{_esc(analysis.incident_id)} · AI-generated voice</text>'
    )
    parts.append(
        f'<text x="{CANVAS_W - 48}" y="60" fill="{PALETTE["accent"]}" '
        f'font-family="monospace" font-size="22" text-anchor="end">'
        f"{_esc(clock_label)}</text>"
    )

    # Edges first, then arrowheads, then nodes on top.
    edge_layer: list[str] = []
    for svc in architecture.services:
        src = layout.boxes.get(svc.name)
        if src is None:
            continue
        for dep in svc.depends_on:
            dst = layout.boxes.get(dep)
            if dst is None:
                continue
            x1 = px(src.x + src.w)
            y1 = py(src.y + src.h / 2)
            x2 = px(dst.x)
            y2 = py(dst.y + dst.h / 2)
            bend = max(40.0 * scale, (x2 - x1) / 2)
            is_live = tuple(sorted((svc.name, dep))) in live_edges
            color = PALETTE["accent"] if is_live else "#39435a"
            wdt = 3.0 if is_live else 1.6
            opacity = 1.0 if is_live else 0.5
            edge_layer.append(
                f'<path d="M {x1:.1f} {y1:.1f} C {x1 + bend:.1f} {y1:.1f}, '
                f'{x2 - bend:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}" '
                f'stroke="{color}" stroke-width="{wdt}" fill="none" '
                f'opacity="{opacity}"/>'
            )
            # Arrowhead as a small triangle at the target (edges run left->right).
            ah = 6.0
            edge_layer.append(
                f'<path d="M {x2:.1f} {y2:.1f} L {x2 - ah * 1.6:.1f} {y2 - ah:.1f} '
                f'L {x2 - ah * 1.6:.1f} {y2 + ah:.1f} z" fill="{color}" '
                f'opacity="{opacity}"/>'
            )
    parts.extend(edge_layer)

    fs = max(11.0, 12.0 * scale)
    ofs = max(9.0, 10.0 * scale)
    for svc in architecture.services:
        box = layout.boxes[svc.name]
        state = node_state.get(svc.name, "healthy")
        stroke, fill = _state_colors(state)
        x = px(box.x)
        y = py(box.y)
        w = box.w * scale
        h = box.h * scale
        if svc.name in highlight:
            parts.append(
                f'<rect x="{x - 4:.1f}" y="{y - 4:.1f}" width="{w + 8:.1f}" '
                f'height="{h + 8:.1f}" rx="11" fill="none" stroke="{stroke}" '
                f'stroke-width="2" opacity="0.55"/>'
            )
        sw = 2.4 if state != "healthy" else 1.6
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="8" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
        )
        parts.append(
            f'<text x="{x + 12:.1f}" y="{y + h / 2 - 2:.1f}" fill="{PALETTE["text"]}" '
            f'font-family="monospace" font-size="{fs:.0f}">{_esc(svc.name)}</text>'
        )
        owner = (svc.owner or "")
        if svc.user_facing:
            owner = f"{owner} · user-facing".strip(" ·")
        if owner:
            parts.append(
                f'<text x="{x + 12:.1f}" y="{y + h - 8:.1f}" fill="{PALETTE["dim"]}" '
                f'font-family="monospace" font-size="{ofs:.0f}">{_esc(owner)}</text>'
            )

    # Caption panel.
    cap_y = CANVAS_H - 208
    parts.append(
        f'<rect x="40" y="{cap_y}" width="{CANVAS_W - 80}" height="152" rx="12" '
        f'fill="{PALETTE["panel"]}" stroke="{PALETTE["line"]}" stroke-width="1"/>'
    )
    parts.append(
        f'<text x="64" y="{cap_y + 34}" fill="{PALETTE["text"]}" '
        f'font-family="sans-serif" font-size="19" font-weight="600">'
        f"{_esc(caption_title)}</text>"
    )
    body_lines = _wrap(caption_body, 96)[:3]
    ty = cap_y + 62
    for line in body_lines:
        parts.append(
            f'<text x="64" y="{ty}" fill="{PALETTE["dim"]}" '
            f'font-family="sans-serif" font-size="15">{_esc(line)}</text>'
        )
        ty += 22
    if evidence_label:
        parts.append(
            f'<text x="64" y="{cap_y + 138}" fill="{PALETTE["accent"]}" '
            f'font-family="monospace" font-size="12">{_esc(evidence_label)}</text>'
        )

    # Progress bar.
    bar_x, bar_w = 40, CANVAS_W - 80
    bar_y = CANVAS_H - 34
    parts.append(
        f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="6" rx="3" '
        f'fill="{PALETTE["panel_2"]}"/>'
    )
    filled = int(bar_w * max(0.0, min(1.0, progress)))
    parts.append(
        f'<rect x="{bar_x}" y="{bar_y}" width="{filled}" height="6" rx="3" '
        f'fill="{PALETTE["accent"]}"/>'
    )

    parts.append("</svg>")
    return "".join(parts)


def rasterize(svg: str, out_path: str) -> None:
    cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        write_to=out_path,
        output_width=CANVAS_W,
        output_height=CANVAS_H,
    )
