"""The module-level act: dependencies around the failed module.

This is the bridge between the service's request-path graph and the
function-level call graph.  It projects the one-hop module context already
stored on ``InternalTrace``:

    static callers -> failing module -> static dependencies

The evidence semantics are deliberately visible.  A logged request-path
module can turn teal, the log-attributed failing module turns red, direct
dependents turn amber to show *potential* impact, and static-only or dormant
context stays muted.  Static relationships never masquerade as runtime
execution.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import ImageDraw

from incidentlens.domain.models import (
    ArchitectureGraph,
    IncidentAnalysis,
    InternalTrace,
    ServiceInternals,
    ServiceNode,
    StageStatus,
)
from incidentlens.studio.cinema import palette
from incidentlens.studio.cinema.camera import CameraKey, CameraTrack, Projector
from incidentlens.studio.cinema.easing import clamp, with_alpha
from incidentlens.studio.cinema.engine import CinematicScene, RenderSpec
from incidentlens.studio.cinema.timeline import StateChange
from incidentlens.studio.cinema.world import Edge3D
from incidentlens.studio.evidence import (
    module_failure_is_log_confirmed,
    module_has_linked_success,
)

_MAX_CALLERS = 3
_MAX_DEPENDENCIES = 3


def _module_tail(module: str) -> str:
    parts = [part for part in module.split(".") if part]
    return ".".join(parts[-2:]) if len(parts) > 1 else module


@dataclass(frozen=True)
class ModuleNodeMeta:
    module: str
    role: str  # "caller" | "failing" | "dependency"
    observed: bool = False
    dormant: bool = False


@dataclass(frozen=True)
class ModuleView:
    graph: ArchitectureGraph
    fail_node: str
    callers: list[str]
    dependencies: list[str]
    meta: dict[str, ModuleNodeMeta]
    failure_confirmed: bool


def build_module_view(
    trace: InternalTrace,
    internals: ServiceInternals | None = None,
    analysis: IncidentAnalysis | None = None,
) -> ModuleView | None:
    """Build a compact, failure-centred module dependency projection."""
    if not trace.failing_module:
        return None
    caller_modules = [
        module
        for module in dict.fromkeys(trace.failing_callers)
        if module != trace.failing_module
    ][:_MAX_CALLERS]
    all_dependencies = [
        module
        for module in dict.fromkeys(trace.failing_callees)
        if module != trace.failing_module
    ]
    coupled = set(caller_modules) & set(all_dependencies)
    dependency_modules = [
        module for module in all_dependencies if module not in coupled
    ][:_MAX_DEPENDENCIES]
    if not caller_modules and not dependency_modules:
        return None

    module_stages: list[tuple[str, str]] = []
    if internals is not None:
        for stage in internals.stages:
            for module in stage.modules:
                module_stages.append((module, stage.name))
    status_by_stage = {stage.stage: stage.status for stage in trace.stages}

    def stage_for_module(module: str) -> str | None:
        matches = [
            (prefix, stage)
            for prefix, stage in module_stages
            if module == prefix or module.startswith(prefix + ".")
        ]
        return max(matches, key=lambda item: len(item[0]))[1] if matches else None

    used: set[str] = set()
    meta: dict[str, ModuleNodeMeta] = {}

    def add(module: str, role: str) -> str:
        name = _module_tail(module)
        if name in used:
            name = module
        used.add(name)
        stage = stage_for_module(module)
        status = status_by_stage.get(stage) if stage else None
        observed = (
            role in ("caller", "coupled")
            and module_has_linked_success(trace, module, stage, analysis)
        )
        dormant = status in (StageStatus.DORMANT, StageStatus.NOT_REACHED)
        meta[name] = ModuleNodeMeta(
            module=module,
            role=role,
            observed=observed,
            dormant=dormant,
        )
        return name

    fail_node = add(trace.failing_module, "failing")
    caller_nodes = [
        add(module, "coupled" if module in coupled else "caller")
        for module in caller_modules
    ]
    dependency_nodes = [
        add(module, "dependency") for module in dependency_modules
    ]

    services = [
        ServiceNode(
            name=name,
            owner="static dependent",
            depends_on=[fail_node],
            user_facing=True,
        )
        for name in caller_nodes
    ]
    services.append(
        ServiceNode(
            name=fail_node,
            owner="failed module",
            depends_on=dependency_nodes,
            user_facing=not caller_nodes,
        )
    )
    services.extend(
        ServiceNode(
            name=name,
            owner="static dependency",
            depends_on=[],
        )
        for name in dependency_nodes
    )

    return ModuleView(
        graph=ArchitectureGraph(
            system=f"dependencies of {trace.failing_module}",
            services=services,
        ),
        fail_node=fail_node,
        callers=caller_nodes,
        dependencies=dependency_nodes,
        meta=meta,
        failure_confirmed=module_failure_is_log_confirmed(trace, analysis),
    )


class ModuleTimeline:
    """Evidence-aware animation state for the module dependency projection."""

    def __init__(self, view: ModuleView, window: tuple[float, float, float]) -> None:
        start, fail_beat, end = window
        self.view = view
        self.start, self.end = start, end

        observed = [
            node for node in view.callers if view.meta[node].observed
        ]
        enter_at = start + max(1.2, (fail_beat - start) * 0.58)
        fail_at = fail_beat + 0.65
        impact_at = fail_at + 0.45
        self.enter_at = enter_at
        self.fail_at = fail_at
        self.impact_at = impact_at

        self.node_changes: dict[str, list[StateChange]] = {}
        for index, node in enumerate(observed):
            self.node_changes[node] = [
                StateChange(at=start + 0.75 + index * 0.3, state="recovery"),
                StateChange(at=impact_at, state="warning"),
            ]
        for node in view.callers:
            if node not in self.node_changes and not view.meta[node].dormant:
                self.node_changes[node] = [
                    StateChange(at=impact_at, state="warning")
                ]
        if view.failure_confirmed:
            self.node_changes[view.fail_node] = [
                StateChange(at=enter_at, state="recovery"),
                StateChange(at=fail_at, state="critical"),
            ]
        else:
            self.node_changes[view.fail_node] = [
                StateChange(at=fail_at, state="warning")
            ]

        self._edge_start: dict[tuple[str, str], float] = {}
        for index, node in enumerate(view.callers):
            key = tuple(sorted((node, view.fail_node)))
            self._edge_start[key] = start + 0.8 + index * 0.32
        for index, node in enumerate(view.dependencies):
            key = tuple(sorted((view.fail_node, node)))
            self._edge_start[key] = enter_at + 0.25 + index * 0.28
        self._impact_edges = {
            tuple(sorted((node, view.fail_node))) for node in view.callers
        }

    def node_state_at(self, node: str, t: float) -> tuple[str, str, float, float]:
        if self.view.meta.get(node, ModuleNodeMeta("", "")).dormant:
            return "dormant", "dormant", 1.0, 1e9
        changes = self.node_changes.get(node)
        if not changes:
            return "healthy", "healthy", 1.0, 1e9
        active_index = -1
        for index, change in enumerate(changes):
            if change.at <= t:
                active_index = index
            else:
                break
        if active_index < 0:
            return "healthy", "healthy", 1.0, 1e9
        active = changes[active_index]
        previous = (
            changes[active_index - 1].state
            if active_index > 0
            else "healthy"
        )
        since = t - active.at
        return active.state, previous, clamp(since / 0.5), since

    def edge_active_at(
        self, key: tuple[str, str], t: float
    ) -> tuple[bool, float]:
        at = self._edge_start.get(key)
        if at is None or t < at:
            return False, 0.0
        return True, t - at

    def is_impact_edge(self, key: tuple[str, str]) -> bool:
        return key in self._impact_edges


class ModuleScene(CinematicScene):
    """Cinematic dependency graph around the log-attributed module."""

    def __init__(
        self,
        analysis: IncidentAnalysis,
        trace: InternalTrace,
        view: ModuleView,
        window: tuple[float, float, float],
        spec: RenderSpec,
    ) -> None:
        self._trace = trace
        self._view = view
        self._window = window
        self._module_state = ModuleTimeline(view, window)
        super().__init__(analysis, view.graph, self._module_state, spec)

    def _build_camera_track(self) -> CameraTrack:
        start, fail_beat, end = self._window
        focus = [self._view.fail_node] + self._view.callers[:1]
        return CameraTrack(
            [
                CameraKey(
                    time=start,
                    state=self._key_for([], index=1, wide=True),
                    transition=0.0,
                ),
                CameraKey(
                    time=start + max(1.4, (fail_beat - start) * 0.48),
                    state=self._key_for(focus, index=2, wide=False),
                    transition=1.4,
                ),
                CameraKey(
                    time=fail_beat,
                    state=self._key_for([self._view.fail_node], index=3, wide=False),
                    transition=1.2,
                ),
                CameraKey(
                    time=max(end - 1.3, fail_beat + 1.0),
                    state=self._key_for([], index=4, wide=True),
                    transition=1.3,
                ),
            ],
            drift_zoom=0.02,
            drift_yaw=0.7,
        )

    def _edge_colors(self, edge: Edge3D, t: float):
        if (
            self._module_state.is_impact_edge(edge.key)
            and t >= self._module_state.impact_at
        ):
            amber = palette.STATE_STROKE["warning"]
            return amber, amber
        return palette.EDGE_TRACE, palette.PARTICLE_TRACE

    def _particles_reversed(self) -> bool:
        return False

    def _sublabel(self, node, state: str, stroke):
        meta = self._view.meta.get(node.name)
        if meta is None:
            return "", palette.DIM
        if meta.role == "failing":
            if state == "critical":
                return "failed · logged", palette.STATE_STROKE["critical"]
            if state == "warning":
                return "attributed · static", palette.STATE_STROKE["warning"]
            return (
                "reached · logged"
                if self._view.failure_confirmed
                else "stage-mapped module"
            ), (
                palette.STATE_STROKE["recovery"]
                if self._view.failure_confirmed
                else palette.DIM
            )
        if meta.role in ("caller", "coupled"):
            if state == "warning":
                label = (
                    "coupled · static"
                    if meta.role == "coupled"
                    else "dependent · static"
                )
                return label, palette.STATE_STROKE["warning"]
            if meta.observed and state == "recovery":
                return "on path · logged", palette.STATE_STROKE["recovery"]
            if meta.dormant:
                return "not on request path", palette.DIM
            return (
                "coupled · static"
                if meta.role == "coupled"
                else "dependent · static"
            ), palette.DIM
        return "dependency · static", palette.DIM

    def _draw_scene_extras(
        self,
        draw: ImageDraw.ImageDraw,
        gdraw: ImageDraw.ImageDraw,
        proj: Projector,
        t: float,
    ) -> None:
        min_x, min_z, max_x, max_z = self.world.bounds
        corners = np.array(
            [
                [min_x - 1.6, 0.008, min_z - 1.9],
                [max_x + 1.6, 0.008, min_z - 1.9],
                [max_x + 1.6, 0.008, max_z + 1.9],
                [min_x - 1.6, 0.008, max_z + 1.9],
            ],
            dtype=np.float64,
        )
        screen, depth = proj.project(corners)
        if np.any(depth <= 0.25):
            return
        quad = [(float(x), float(y)) for x, y in screen]
        draw.polygon(quad, fill=with_alpha(palette.FLOOR_GLOW, 0.05))
        width = max(1, int(1.4 * self.ssf))
        for index in range(4):
            a, b = quad[index], quad[(index + 1) % 4]
            for point, other in ((a, b), (b, a)):
                end_x = point[0] + (other[0] - point[0]) * 0.12
                end_y = point[1] + (other[1] - point[1]) * 0.12
                draw.line(
                    (point[0], point[1], end_x, end_y),
                    fill=with_alpha(palette.ACCENT, 0.4),
                    width=width,
                )

    def _draw_scene_overlay(
        self,
        draw: ImageDraw.ImageDraw,
        gdraw: ImageDraw.ImageDraw,
        proj: Projector,
        t: float,
    ) -> None:
        self._draw_module_annotation(draw, proj, t)

    def _draw_particles(
        self,
        draw: ImageDraw.ImageDraw,
        gdraw: ImageDraw.ImageDraw,
        proj: Projector,
        t: float,
    ) -> None:
        """Static code edges sweep into view but do not mimic runtime traffic."""

    def _draw_module_annotation(
        self,
        draw: ImageDraw.ImageDraw,
        proj: Projector,
        t: float,
    ) -> None:
        fade_in = clamp((t - (self._module_state.fail_at + 0.4)) / 0.4)
        fade_out = clamp((self._window[2] - t) / 0.45)
        alpha = fade_in * fade_out
        if alpha <= 0.01:
            return
        node = self.world.nodes.get(self._view.fail_node)
        if node is None:
            return
        sx, sy, depth = proj.project_one(
            node.cx, node.height + 1.15, node.cz
        )
        if depth <= 0.3:
            return

        from incidentlens.studio.cinema.fonts import font

        size = int(15 * self.ssf)
        mono = font("mono-bold", size)
        mono_small = font("mono", int(size * 0.85))
        proof = (
            "log-confirmed module"
            if self._view.failure_confirmed
            else "module attributed from failing stage"
        )
        locus_color = palette.STATE_STROKE[
            "critical" if self._view.failure_confirmed else "warning"
        ]
        lines: list[tuple[str, tuple[int, int, int], object]] = [
            (
                self._view.meta[self._view.fail_node].module.replace(".", "/")
                + ".py",
                locus_color,
                mono,
            ),
            (proof, palette.TEXT, mono_small),
        ]
        if self._view.callers:
            lines.append(
                (
                    f"← {len(self._view.callers)} shown dependents · static",
                    palette.STATE_STROKE["warning"],
                    mono_small,
                )
            )
        if self._trace.blast_radius:
            lines.append(
                (
                    f"blast radius: {self._trace.blast_radius} potential dependents",
                    palette.STATE_STROKE["warning"],
                    mono_small,
                )
            )

        pad = int(10 * self.ssf)
        line_height = int(size * 1.45)
        widths = [
            draw.textlength(text, font=line_font)
            for text, _color, line_font in lines
        ]
        box_width = max(widths) + pad * 2
        box_height = (
            line_height * len(lines) + pad * 2 - int(size * 0.3)
        )
        margin = int(24 * self.ssf)
        left = sx - box_width - margin
        x0 = (
            left
            if left >= margin
            else min(self.spec.ss_size[0] - box_width - margin, sx + margin)
        )
        y0 = sy - box_height - int(6 * self.ssf)
        draw.rounded_rectangle(
            (x0, y0, x0 + box_width, y0 + box_height),
            radius=int(8 * self.ssf),
            fill=(12, 15, 22, int(215 * alpha)),
            outline=with_alpha(locus_color, 0.75 * alpha),
            width=max(1, int(1.6 * self.ssf)),
        )
        text_y = y0 + pad
        for text, color, line_font in lines:
            draw.text(
                (x0 + pad, text_y),
                text,
                font=line_font,
                fill=with_alpha(color, alpha),
            )
            text_y += line_height
