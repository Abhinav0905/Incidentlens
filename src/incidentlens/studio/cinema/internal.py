"""The dive act: inside the origin service, stage by stage.

When the analysis carries an ``InternalTrace``, the movie leaves the macro
architecture at the failure beat, crossfades into the service's internal
pipeline — its middleware, graph nodes and clients laid out as a smaller 3D
scene — and follows the request as a pulse: each traversed stage flashes
teal as the pulse passes, off-path branches stay dormant and dim, and at the
failing stage the pulse turns red and erupts. Module and function dives then
continue the zoom before the camera surfaces to the macro propagation story.

Everything here reuses the macro engine's drawing (slabs, edges, particles,
shockwaves, bloom); the differences are the state source (a pulse schedule
derived from the trace instead of the incident timeline), the cool color of
a healthy trace, and the camera plan.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from incidentlens.domain.models import (
    ArchitectureGraph,
    CodeGraph,
    IncidentAnalysis,
    InternalTrace,
    ServiceInternals,
    ServiceNode,
    StageStatus,
)
from incidentlens.studio.cinema import hud, palette
from incidentlens.studio.cinema.camera import CameraKey, CameraTrack, Projector
from incidentlens.studio.cinema.easing import clamp, smoothstep, with_alpha
from incidentlens.studio.cinema.engine import CinematicScene, RenderSpec
from incidentlens.studio.cinema.timeline import StateChange, Timeline
from incidentlens.studio.cinema.world import Edge3D

BLEND_IN = 0.65  # short refocus crossfade into a deeper graph
BLEND_OUT = 0.65

_STATUS_SUBLABEL = {
    StageStatus.OK: "passed",
    StageStatus.INFERRED: "traversed · inferred",
    StageStatus.FAILED: "failed here",
    StageStatus.NOT_REACHED: "never ran",
    StageStatus.DORMANT: "",
    StageStatus.UNKNOWN: "",
}


def stage_graph(service: str, internals: ServiceInternals) -> ArchitectureGraph:
    """The internal pipeline as an ArchitectureGraph the layout understands.

    Flow edges become ``depends_on`` chains (entry marked user-facing, so the
    layered layout runs entry -> deep stages left to right). Cycles — agent
    loops like ``tool_node -> agentic`` — are broken by keeping only edges
    that move forward in BFS depth from the entry; layout needs a DAG, the
    trace path never uses back-edges anyway.
    """
    entry = internals.entry_stage()
    adjacency: dict[str, list[str]] = {}
    for a, b in internals.edges:
        adjacency.setdefault(a, []).append(b)

    depth: dict[str, int] = {}
    if entry:
        queue: deque[str] = deque([entry])
        depth[entry] = 0
        while queue:
            current = queue.popleft()
            for nxt in adjacency.get(current, []):
                if nxt not in depth:
                    depth[nxt] = depth[current] + 1
                    queue.append(nxt)
    fallback = (max(depth.values()) + 1) if depth else 0
    for stage in internals.stages:
        depth.setdefault(stage.name, fallback)

    forward = [
        (a, b) for a, b in internals.edges
        if a in depth and b in depth and depth[b] > depth[a]
    ]
    outgoing: dict[str, list[str]] = {}
    for a, b in forward:
        outgoing.setdefault(a, []).append(b)

    services = [
        ServiceNode(
            name=stage.name,
            owner="",
            depends_on=sorted(set(outgoing.get(stage.name, []))),
            user_facing=(stage.name == entry),
        )
        for stage in internals.stages
    ]
    return ArchitectureGraph(system=f"inside {service}", services=services)


@dataclass(frozen=True)
class PulseSchedule:
    arrivals: dict  # stage -> seconds when the pulse reaches it
    fail_stage: str | None
    fail_at: float


class StageTimeline:
    """Duck-typed state source for CinematicScene, driven by the pulse.

    Path stages sit healthy until the pulse arrives, then flash to
    ``recovery`` (the teal "confirmed good" family); the failing stage goes
    ``critical`` in sync with the point-of-failure narration beat; everything
    off the path stays ``dormant``.
    """

    def __init__(self, trace: InternalTrace, window: tuple[float, float, float]) -> None:
        start, fail_beat, end = window
        self.trace = trace
        self.start, self.end = start, end
        path = [s for s in trace.path if s != trace.failing_stage]
        first = start + 1.1
        last = start + max(2.0, (fail_beat - start) * 0.86)
        step = (last - first) / max(1, len(path) - 1) if len(path) > 1 else 0.0
        arrivals = {stage: first + i * step for i, stage in enumerate(path)}
        fail_at = fail_beat + 0.8
        if trace.failing_stage:
            arrivals[trace.failing_stage] = fail_at
        self.schedule = PulseSchedule(arrivals, trace.failing_stage, fail_at)

        status = {t.stage: t.status for t in trace.stages}
        self._status = status
        self.node_changes: dict[str, list[StateChange]] = {}
        for stage, at in arrivals.items():
            state = "critical" if stage == trace.failing_stage else "recovery"
            self.node_changes[stage] = [StateChange(at=at, state=state)]

        path_pairs = list(zip(trace.path, trace.path[1:], strict=False))
        self._edge_start: dict[tuple[str, str], float] = {}
        self._edge_final: set = set()
        for a, b in path_pairs:
            key = tuple(sorted((a, b)))
            self._edge_start[key] = arrivals.get(a, first)
            if b == trace.failing_stage:
                self._edge_final.add(key)

    # ---- the CinematicScene state interface --------------------------------

    def node_state_at(self, node: str, t: float) -> tuple[str, str, float, float]:
        changes = self.node_changes.get(node)
        if changes:
            change = changes[0]
            if t >= change.at:
                since = t - change.at
                return change.state, "healthy", clamp(since / 0.5), since
            return "healthy", "healthy", 1.0, 1e9
        if self._status.get(node) in (StageStatus.DORMANT, StageStatus.NOT_REACHED):
            return "dormant", "dormant", 1.0, 1e9
        return "healthy", "healthy", 1.0, 1e9

    def edge_active_at(self, key: tuple[str, str], t: float) -> tuple[bool, float]:
        at = self._edge_start.get(key)
        if at is None or t < at:
            return False, 0.0
        return True, t - at

    def is_final_edge(self, key: tuple[str, str]) -> bool:
        return key in self._edge_final


class InternalScene(CinematicScene):
    """CinematicScene over the stage graph, states from the pulse."""

    def __init__(
        self,
        analysis: IncidentAnalysis,
        trace: InternalTrace,
        internals: ServiceInternals,
        window: tuple[float, float, float],
        spec: RenderSpec,
    ) -> None:
        self._stage_state = StageTimeline(trace, window)
        self._window = window
        self._trace = trace
        graph = stage_graph(trace.service, internals)
        super().__init__(analysis, graph, self._stage_state, spec)  # type: ignore[arg-type]

    # camera: establish wide, track the pulse midway, land on the failure
    def _build_camera_track(self) -> CameraTrack:
        start, fail_beat, end = self._window
        trace = self._trace
        path = trace.path or [s.name for s in self.world.nodes.values()][:1]
        mid = path[: max(2, len(path) * 2 // 3)]
        keys = [
            CameraKey(time=start, state=self._key_for([], index=1, wide=True), transition=0.0),
            CameraKey(
                time=start + max(1.6, (fail_beat - start) * 0.45),
                state=self._key_for(mid, index=2, wide=len(mid) > 6),
                transition=1.6,
            ),
        ]
        if trace.failing_stage:
            focus = [trace.failing_stage]
            if len(path) >= 2:
                focus.insert(0, path[-2])
            keys.append(
                CameraKey(
                    time=fail_beat,
                    state=self._key_for(focus, index=3, wide=False),
                    transition=1.3,
                )
            )
        keys.append(
            CameraKey(
                time=max(end - 1.4, fail_beat + 1.0),
                state=self._key_for([], index=4, wide=True),
                transition=1.4,
            )
        )
        return CameraTrack(keys, drift_zoom=0.03, drift_yaw=1.0)

    # cool pulse instead of hot propagation; the last hop burns red
    def _edge_colors(self, edge: Edge3D, t: float):
        hot_at = self._stage_state.schedule.fail_at - 0.15
        if self._stage_state.is_final_edge(edge.key) and t >= hot_at:
            return palette.EDGE_ACTIVE, palette.PARTICLE_ACTIVE
        return palette.EDGE_TRACE, palette.PARTICLE_TRACE

    def _particles_reversed(self) -> bool:
        return False  # the request travels with the flow

    def _sublabel(self, node, state: str, stroke):
        status = self._stage_state._status.get(node.name)
        if node.name == self._trace.failing_stage and state == "critical":
            return "failed here", stroke
        if status is not None:
            arrived = state in ("recovery", "critical")
            if status == StageStatus.OK and arrived:
                return "passed · logged", palette.STATE_STROKE["recovery"]
            if status == StageStatus.INFERRED and arrived:
                return "traversed · inferred", palette.DIM
            label = _STATUS_SUBLABEL.get(status, "")
            if label and status in (StageStatus.NOT_REACHED,):
                return label, palette.DIM
        return "", palette.DIM

    # a floor plate + corner brackets: the "opened casing" of the service
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
        draw.polygon(quad, fill=with_alpha(palette.FLOOR_GLOW, 0.045))
        width = max(1, int(1.4 * self.ssf))
        for i in range(4):
            a, b = quad[i], quad[(i + 1) % 4]
            for point, other in ((a, b), (b, a)):
                bx = point[0] + (other[0] - point[0]) * 0.12
                by = point[1] + (other[1] - point[1]) * 0.12
                draw.line((point[0], point[1], bx, by),
                          fill=with_alpha(palette.ACCENT, 0.4), width=width)


class FrameScene(Protocol):
    """The small scene contract used by both 3D and 2D dive renderers."""

    spec: RenderSpec

    def scene_frame(self, t: float) -> Image.Image: ...


@dataclass
class DiveAct:
    """One drill-down scene and the time window it owns on the master axis."""

    scene: FrameScene
    window: tuple[float, float, float]  # start, fail-beat, end

    def weight(self, t: float) -> float:
        """How present this dive is at ``t`` — rises in, holds, falls out.

        The crossfades are centred on the window edges (half before, half
        after), so two back-to-back dives sharing an edge sum to ~1 across the
        hand-off: the transition runs dive→dive with no macro flash between."""
        start, _fail, end = self.window
        half_in, half_out = BLEND_IN / 2.0, BLEND_OUT / 2.0
        if t < start - half_in or t > end + half_out:
            return 0.0
        rise = smoothstep((t - (start - half_in)) / BLEND_IN)
        fall = smoothstep(((end + half_out) - t) / BLEND_OUT)
        return max(0.0, min(rise, fall))


class MovieScene:
    """Macro scene plus zero or more dive acts, crossfaded on one time axis.

    Dives play in sequence (stage, module, then function). Between two adjacent
    dives the crossfade runs dive→dive so the macro world never flashes
    through; only at the outer edges does the camera surface to macro.
    """

    def __init__(self, macro: CinematicScene, dives: list[DiveAct],
                 timeline: Timeline) -> None:
        self.macro = macro
        self.dives = dives
        self.timeline = timeline
        self.spec = macro.spec

    @property
    def internal(self) -> FrameScene | None:
        """The first (stage-level) dive, for backward-compatible callers/tests."""
        return self.dives[0].scene if self.dives else None

    def blend_at(self, t: float) -> float:
        """Overall dive visibility at ``t`` (0 = pure macro, 1 = fully dived)."""
        if not self.dives:
            return 0.0
        return min(1.0, sum(d.weight(t) for d in self.dives))

    def frame(self, t: float) -> Image.Image:
        weighted = [(d.weight(t), d.scene) for d in self.dives]
        weighted = [(w, s) for w, s in weighted if w > 0.001]
        if not weighted:
            img = self.macro.scene_frame(t)
        else:
            weighted.sort(key=lambda ws: ws[0], reverse=True)
            w0, s0 = weighted[0]
            top = s0.scene_frame(t)
            if len(weighted) > 1:  # crossfade between adjacent dives
                w1, s1 = weighted[1]
                mix = w1 / (w0 + w1)
                top = Image.blend(top, s1.scene_frame(t), mix)
                # A brief depth-of-field refocus hides double labels while the
                # camera changes graph levels. HUD text is drawn afterwards.
                blur = 2.0 * self.spec.supersample * min(1.0, mix * 2.0)
                if blur > 0.05:
                    top = top.filter(ImageFilter.GaussianBlur(radius=blur))
            dive_vis = min(1.0, w0 + (weighted[1][0] if len(weighted) > 1 else 0.0))
            if dive_vis >= 0.999:
                img = top
            else:
                img = Image.blend(self.macro.scene_frame(t), top, dive_vis)
        out = img.resize((self.spec.width, self.spec.height), Image.LANCZOS)
        hud.draw_hud(out, self.timeline, t)
        return out


def build_movie_scene(
    analysis: IncidentAnalysis,
    architecture: ArchitectureGraph,
    timeline: Timeline,
    spec: RenderSpec,
    *,
    code_graph: CodeGraph | None = None,
) -> MovieScene:
    """The renderer's entry point: macro scene plus each dive when traceable."""
    from incidentlens.studio.cinema.dependencies import (
        ModuleScene,
        build_module_view,
    )
    from incidentlens.studio.cinema.symbols import SymbolScene, build_symbol_view

    macro = CinematicScene(analysis, architecture, timeline, spec)
    dives: list[DiveAct] = []
    trace = analysis.internal_trace
    service = None

    # High-level dive: inside the origin service, stage by stage.
    stage_window = timeline.internal_window()
    if trace is not None and trace.failing_stage and stage_window is not None:
        service = next(
            (s for s in architecture.services if s.name == trace.service), None
        )
        if service is not None and service.internals is not None:
            dives.append(
                DiveAct(
                    InternalScene(analysis, trace, service.internals, stage_window, spec),
                    stage_window,
                )
            )

    # Mid-level dive: the failed module and its static dependency neighborhood.
    module_window = timeline.module_window()
    if trace is not None and module_window is not None:
        module_blueprint = None
        if code_graph is not None:
            from incidentlens.studio.cinema.blueprint import (
                ModuleBlueprintScene,
                build_module_blueprint,
            )

            module_blueprint = build_module_blueprint(code_graph, analysis)
            if module_blueprint is not None:
                dives.append(
                    DiveAct(
                        ModuleBlueprintScene(
                            analysis,
                            module_blueprint,
                            module_window,
                            spec,
                        ),
                        module_window,
                    )
                )
        if module_blueprint is None:
            internals = service.internals if service is not None else None
            module_view = build_module_view(trace, internals, analysis)
            if module_view is not None:
                dives.append(
                    DiveAct(
                        ModuleScene(
                            analysis,
                            trace,
                            module_view,
                            module_window,
                            spec,
                        ),
                        module_window,
                    )
                )

    # Low-level dive: methods nested inside classes/modules when the full
    # repository graph exists; otherwise retain the compact call neighborhood.
    symbol_window = timeline.symbol_window()
    if trace is not None and symbol_window is not None:
        symbol_blueprint = None
        if code_graph is not None:
            from incidentlens.studio.cinema.blueprint import (
                SymbolBlueprintScene,
                build_symbol_blueprint,
            )

            symbol_blueprint = build_symbol_blueprint(code_graph, analysis)
            if symbol_blueprint is not None:
                dives.append(
                    DiveAct(
                        SymbolBlueprintScene(
                            analysis,
                            symbol_blueprint,
                            symbol_window,
                            spec,
                        ),
                        symbol_window,
                    )
                )
        if symbol_blueprint is None:
            view = build_symbol_view(trace)
            if view is not None:
                dives.append(
                    DiveAct(
                        SymbolScene(
                            analysis,
                            trace,
                            view,
                            symbol_window,
                            spec,
                        ),
                        symbol_window,
                    )
                )

    return MovieScene(macro, dives, timeline)
