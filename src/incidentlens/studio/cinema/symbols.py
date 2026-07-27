"""The low-level act: inside the failing *function*, its call graph.

The stage dive (``internal.py``) shows the service's pipeline at the module /
graph-node granularity — the high-level design. This act goes one level
deeper, to the same resolution as the symbol Mermaid: the failing
``module.Class.method`` at the centre, the functions it calls fanned out
around it, grouped by their owning class/module. The static call links
illuminate teal and the selected symbol turns amber as a candidate locus.
Without a stack frame or function span, it is not presented as the exact
runtime frame that raised.

Everything reuses the macro engine's drawing (slabs, edges, particles, bloom).
The differences are the state source (a pulse over the call graph instead of
the incident timeline), the cool trace colour, the camera plan, and a code
annotation pinned to the failing symbol. All inputs come from
``analysis.internal_trace`` — no code graph needs threading through the
render pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import ImageDraw

from incidentlens.domain.models import (
    ArchitectureGraph,
    IncidentAnalysis,
    InternalTrace,
    ServiceNode,
)
from incidentlens.studio.cinema import palette
from incidentlens.studio.cinema.camera import CameraKey, CameraTrack, Projector
from incidentlens.studio.cinema.easing import clamp, with_alpha
from incidentlens.studio.cinema.engine import CinematicScene, RenderSpec
from incidentlens.studio.cinema.timeline import StateChange
from incidentlens.studio.cinema.world import Edge3D

# A method carries its class; a bare function carries its module tail.
_MAX_CALLEES = 6  # keep the fan-out legible


def _module_tail(module: str) -> str:
    parts = [p for p in module.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) > 2 else module


def display_name(qualname: str, module: str) -> str:
    """``module.Class.method`` -> ``Class.method``; ``module.func`` -> ``func``."""
    if qualname.startswith(module + "."):
        tail = qualname[len(module) + 1 :]
    else:
        tail = qualname.rsplit(".", 1)[-1]
    return tail or qualname


@dataclass(frozen=True)
class SymbolNodeMeta:
    qualname: str
    module: str
    is_failing: bool


@dataclass(frozen=True)
class SymbolView:
    graph: ArchitectureGraph
    fail_node: str  # display name of the failing symbol
    callers: list[str]
    callees: list[str]
    path: list[str]  # callers, fail_node, then callees in draw order
    meta: dict[str, SymbolNodeMeta]  # display name -> meta


def build_symbol_view(trace: InternalTrace) -> SymbolView | None:
    """The failing symbol + the functions it calls, as an ArchitectureGraph.

    Returns ``None`` when the trace has no fine-grained context worth a dive
    (no failing symbol, or nothing it calls that we can name).
    """
    if not trace.failing_symbol:
        return None
    fail_module = trace.failing_module or trace.failing_symbol.rsplit(".", 1)[0]
    callees = list(trace.failing_symbol_callees)[:_MAX_CALLEES]
    callers = list(trace.failing_symbol_callers)[:3]
    if not callees and not callers:
        return None

    meta: dict[str, SymbolNodeMeta] = {}
    used: set[str] = set()

    def add(qualname: str, is_failing: bool) -> str:
        module = qualname.rsplit(".", 1)[0]
        # methods live at module.Class.method, so strip one more for the module
        symbols_module = module
        # Heuristic: if the second-to-last segment is CapWords it's a class,
        # and the true module is one level up.
        parts = qualname.split(".")
        if len(parts) >= 3 and parts[-2][:1].isupper():
            symbols_module = ".".join(parts[:-2])
        name = display_name(qualname, symbols_module)
        if name in used:  # disambiguate collisions with the module tail
            name = f"{name} ({_module_tail(symbols_module)})"
        used.add(name)
        meta[name] = SymbolNodeMeta(
            qualname=qualname, module=symbols_module, is_failing=is_failing
        )
        return name

    fail_node = add(trace.failing_symbol, is_failing=True)
    caller_nodes = [add(c, is_failing=False) for c in callers]
    callee_nodes = [add(c, is_failing=False) for c in callees]

    services: list[ServiceNode] = []
    # callers (if any) sit upstream and point into the failing symbol
    for name in caller_nodes:
        services.append(
            ServiceNode(
                name=name, owner=_module_tail(meta[name].module),
                depends_on=[fail_node], user_facing=True,
            )
        )
    services.append(
        ServiceNode(
            name=fail_node, owner=_module_tail(meta[fail_node].module),
            depends_on=list(callee_nodes),
            user_facing=not caller_nodes,  # entry when nothing calls it statically
        )
    )
    for name in callee_nodes:
        services.append(
            ServiceNode(name=name, owner=_module_tail(meta[name].module), depends_on=[])
        )

    graph = ArchitectureGraph(
        system=f"inside {display_name(trace.failing_symbol, fail_module)}",
        services=services,
    )
    path = caller_nodes + [fail_node] + callee_nodes
    return SymbolView(
        graph=graph,
        fail_node=fail_node,
        callers=caller_nodes,
        callees=callee_nodes,
        path=path,
        meta=meta,
    )


class SymbolTimeline:
    """Pulse-driven state source over the call graph (duck-typed for the scene).

    Static callers and callees illuminate teal to reveal code structure. The
    attributed symbol then turns amber in sync with the candidate-locus beat;
    no symbol turns red because the trace contains no runtime stack frame.
    """

    def __init__(self, view: SymbolView, window: tuple[float, float, float]) -> None:
        start, fail_beat, end = window
        self.view = view
        self.start, self.end = start, end
        fail = view.fail_node

        # Callers arrive first, then the candidate symbol "enters" (teal), then
        # its callees fan out; the candidate turns amber at the second beat.
        # Split the path into upstream (callers) and downstream (callees).
        idx = view.path.index(fail)
        upstream = view.path[:idx]
        downstream = view.path[idx + 1 :]

        first = start + 1.0
        enter = start + max(1.4, (fail_beat - start) * 0.35)
        last = start + max(2.2, (fail_beat - start) * 0.86)
        fail_at = fail_beat + 0.8

        arrivals: dict[str, float] = {}
        for i, node in enumerate(upstream):
            step = (enter - first) / max(1, len(upstream)) if upstream else 0.0
            arrivals[node] = first + i * step
        span = last - enter
        for i, node in enumerate(downstream):
            step = span / max(1, len(downstream) - 1) if len(downstream) > 1 else 0.0
            arrivals[node] = enter + 0.3 + i * step

        self._enter_at = enter
        self._fail_at = fail_at
        self._arrivals = arrivals

        # Teal reveals static structure; amber explicitly means inference.
        self.node_changes: dict[str, list[StateChange]] = {}
        for node, at in arrivals.items():
            self.node_changes[node] = [StateChange(at=at, state="recovery")]
        self.node_changes[fail] = [
            StateChange(at=enter, state="recovery"),
            StateChange(at=fail_at, state="warning"),
        ]

        # edges light as the pulse crosses them; the callee edges belong to the
        # failing symbol fanning out (fail -> callee), callers point in.
        self._edge_start: dict[tuple[str, str], float] = {}
        for node in downstream:
            key = tuple(sorted((fail, node)))
            self._edge_start[key] = enter + 0.2
        for node in upstream:
            key = tuple(sorted((node, fail)))
            self._edge_start[key] = arrivals.get(node, first)

    # ---- the CinematicScene state interface --------------------------------

    def node_state_at(self, node: str, t: float) -> tuple[str, str, float, float]:
        changes = self.node_changes.get(node)
        if not changes:
            return "healthy", "healthy", 1.0, 1e9
        idx = -1
        for i, change in enumerate(changes):
            if change.at <= t:
                idx = i
            else:
                break
        if idx == -1:
            return "healthy", "healthy", 1.0, 1e9
        active = changes[idx]
        prev = changes[idx - 1].state if idx > 0 else "healthy"
        since = t - active.at
        return active.state, prev, clamp(since / 0.5), since

    def edge_active_at(self, key: tuple[str, str], t: float) -> tuple[bool, float]:
        at = self._edge_start.get(key)
        if at is None or t < at:
            return False, 0.0
        return True, t - at


class SymbolScene(CinematicScene):
    """CinematicScene over the call-graph view, states from the pulse."""

    def __init__(
        self,
        analysis: IncidentAnalysis,
        trace: InternalTrace,
        view: SymbolView,
        window: tuple[float, float, float],
        spec: RenderSpec,
    ) -> None:
        self._view = view
        self._trace = trace
        self._window = window
        self._symbol_state = SymbolTimeline(view, window)
        super().__init__(analysis, view.graph, self._symbol_state, spec)

    # camera: establish wide over the call graph, push toward the failing
    # symbol, land on it as it turns red
    def _build_camera_track(self) -> CameraTrack:
        start, fail_beat, end = self._window
        fail = self._view.fail_node
        callees = [n for n in self._view.path if n != fail]
        keys = [
            CameraKey(time=start, state=self._key_for([], index=1, wide=True),
                      transition=0.0),
            CameraKey(
                time=start + max(1.6, (fail_beat - start) * 0.5),
                state=self._key_for([fail] + callees[:2], index=2,
                                    wide=len(callees) > 5),
                transition=1.6,
            ),
            CameraKey(
                time=fail_beat,
                state=self._key_for([fail], index=3, wide=False),
                transition=1.3,
            ),
            CameraKey(
                time=max(end - 1.4, fail_beat + 1.0),
                state=self._key_for([], index=4, wide=True),
                transition=1.4,
            ),
        ]
        return CameraTrack(keys, drift_zoom=0.03, drift_yaw=1.0)

    # Cool pulse only: the candidate symbol is amber, never confirmed-red.
    def _edge_colors(self, edge: Edge3D, t: float):
        return palette.EDGE_TRACE, palette.PARTICLE_TRACE

    def _particles_reversed(self) -> bool:
        return False  # the request travels with the flow, symbol -> callees

    def _sublabel(self, node, state: str, stroke):
        meta = self._view.meta.get(node.name)
        if meta is None:
            return "", palette.DIM
        if meta.is_failing and state == "warning":
            return "candidate · static", palette.STATE_STROKE["warning"]
        if meta.is_failing:
            return "selected by static rank", palette.STATE_STROKE["recovery"]
        if state == "recovery":
            return (
                f"static call · {_module_tail(meta.module)}",
                palette.STATE_STROKE["recovery"],
            )
        return _module_tail(meta.module), palette.DIM

    # a floor plate + corner brackets: the "opened casing" of the function
    def _draw_scene_extras(self, draw: ImageDraw.ImageDraw, gdraw: ImageDraw.ImageDraw,
                           proj: Projector, t: float) -> None:
        min_x, min_z, max_x, max_z = self.world.bounds
        pad_x, pad_z = 1.6, 1.9
        corners = np.array(
            [
                [min_x - pad_x, 0.008, min_z - pad_z],
                [max_x + pad_x, 0.008, min_z - pad_z],
                [max_x + pad_x, 0.008, max_z + pad_z],
                [min_x - pad_x, 0.008, max_z + pad_z],
            ],
            dtype=np.float64,
        )
        screen, depth = proj.project(corners)
        if np.any(depth <= 0.25):
            return
        quad = [(float(x), float(y)) for x, y in screen]
        draw.polygon(quad, fill=with_alpha(palette.FLOOR_GLOW, 0.05))
        width = max(1, int(1.4 * self.ssf))
        for i in range(4):
            a, b = quad[i], quad[(i + 1) % 4]
            for point, other in ((a, b), (b, a)):
                bx = point[0] + (other[0] - point[0]) * 0.12
                by = point[1] + (other[1] - point[1]) * 0.12
                draw.line((point[0], point[1], bx, by),
                          fill=with_alpha(palette.ACCENT, 0.4), width=width)

    def _draw_scene_overlay(self, draw: ImageDraw.ImageDraw, gdraw: ImageDraw.ImageDraw,
                            proj: Projector, t: float) -> None:
        self._draw_symbol_annotation(draw, proj, t)

    def _draw_particles(
        self,
        draw: ImageDraw.ImageDraw,
        gdraw: ImageDraw.ImageDraw,
        proj: Projector,
        t: float,
    ) -> None:
        """Static call edges sweep into view but do not mimic runtime traffic."""

    def _draw_symbol_annotation(self, draw: ImageDraw.ImageDraw, proj: Projector,
                                t: float) -> None:
        """File, candidate role and logged defect pinned above the symbol."""
        trace = self._trace
        appear = self._symbol_state._fail_at + 0.5
        fade_in = clamp((t - appear) / 0.45)
        fade_out = clamp((self._window[2] - t) / 0.45)
        alpha = fade_in * fade_out
        if alpha <= 0.01:
            return
        node = self.world.nodes.get(self._view.fail_node)
        if node is None:
            return
        sx, sy, dz = proj.project_one(node.cx, node.height + 1.15, node.cz)
        if dz <= 0.3:
            return
        from incidentlens.studio.cinema.fonts import font

        meta = self._view.meta[self._view.fail_node]
        size = int(15 * self.ssf)
        mono = font("mono-bold", size)
        mono_s = font("mono", int(size * 0.85))
        file_line = f"{meta.module.replace('.', '/')}.py :: {self._view.fail_node}"
        lines: list[tuple[str, tuple[int, int, int], object]] = [
            (file_line, palette.STATE_STROKE["warning"], mono),
            ("candidate locus · static · no stack frame", palette.TEXT, mono_s),
        ]
        value = _offending_value_from_trace(trace)
        if value:
            lines.append((f"logged invalid value: {value}", palette.TEXT, mono_s))
        if self._view.callers:
            lines.append(
                (
                    "← called by " + " · ".join(self._view.callers[:3]),
                    palette.DIM,
                    mono_s,
                )
            )
        if self._view.callees:
            lines.append(
                (
                    "→ calls " + " · ".join(self._view.callees[:3]),
                    palette.DIM,
                    mono_s,
                )
            )
        pad = int(10 * self.ssf)
        line_h = int(size * 1.45)
        widths = [draw.textlength(text, font=f) for text, _c, f in lines]
        box_w = max(widths) + pad * 2
        box_h = line_h * len(lines) + pad * 2 - int(size * 0.3)
        margin = int(24 * self.ssf)
        left = sx - box_w - margin
        x0 = (
            left
            if left >= margin
            else min(self.spec.ss_size[0] - box_w - margin, sx + margin)
        )
        y0 = sy - box_h - int(6 * self.ssf)
        draw.rounded_rectangle(
            (x0, y0, x0 + box_w, y0 + box_h), radius=int(8 * self.ssf),
            fill=(12, 15, 22, int(215 * alpha)),
            outline=with_alpha(palette.STATE_STROKE["warning"], 0.75 * alpha),
            width=max(1, int(1.6 * self.ssf)),
        )
        ty = y0 + pad
        for text, color, f in lines:
            draw.text((x0 + pad, ty), text, font=f, fill=with_alpha(color, alpha))
            ty += line_h


def _offending_value_from_trace(trace: InternalTrace) -> str | None:
    """The quoted id/path in the failing stage detail (e.g. a blocked model id)."""
    from incidentlens.studio.narration import _offending_value

    failing = next(
        (s for s in trace.stages if s.stage == trace.failing_stage), None
    )
    if failing is None:
        return None
    return _offending_value(failing.detail)
