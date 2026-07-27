"""The global clock of the video.

Narration beats (each with a measured audio duration) are laid out on one
continuous time axis. From that single schedule everything else is derived:
when each node changes state, when a propagation edge ignites, what the
wall-clock readout shows, which caption is on screen and how faded in it is.
The renderer then never touches the analysis directly — it asks this module
what the world looks like at second ``t``.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime

from incidentlens.domain.models import IncidentAnalysis
from incidentlens.studio import frames
from incidentlens.studio.cinema.easing import clamp
from incidentlens.studio.narration import Narration, NarrationBeat

STATE_BLEND = 0.8  # seconds for a node to morph into its new state
CAPTION_FADE = 0.45


@dataclass(frozen=True)
class BeatSpan:
    index: int
    beat: NarrationBeat
    start: float
    duration: float
    timestamp: datetime | None  # incident wall-clock this beat narrates

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass(frozen=True)
class StateChange:
    at: float  # video seconds
    state: str  # "warning" | "critical" | "recovery"


class Timeline:
    def __init__(
        self,
        analysis: IncidentAnalysis,
        narration: Narration,
        durations: list[float],
        *,
        min_hold: float = 3.2,
        gap: float = 0.55,
        tail: float = 1.6,
    ) -> None:
        if len(durations) != len(narration.beats):
            raise ValueError("one duration per narration beat required")
        self.analysis = analysis
        self.narration = narration

        spans: list[BeatSpan] = []
        cursor = 0.0
        for i, (beat, audio_len) in enumerate(zip(narration.beats, durations, strict=True)):
            hold = max(min_hold, audio_len + gap)
            spans.append(
                BeatSpan(
                    index=i,
                    beat=beat,
                    start=cursor,
                    duration=hold,
                    timestamp=self._beat_timestamp(analysis, beat),
                )
            )
            cursor += hold
        self.spans = spans
        self.total = cursor + tail
        self._starts = [s.start for s in spans]

        # Node state changes pinned to beat starts. fold_states() is the same
        # cumulative state logic the web UI replays, so video and browser agree.
        self.node_changes: dict[str, list[StateChange]] = {}
        prev: dict[str, str] = {}
        first_degraded: dict[str, float] = {}
        for span in spans:
            idx = span.beat.timeline_index
            if idx < 0:
                continue
            folded, _ = frames.fold_states(analysis, idx)
            for node, state in folded.items():
                if prev.get(node) != state:
                    self.node_changes.setdefault(node, []).append(
                        StateChange(at=span.start, state=state)
                    )
                    if state in ("warning", "critical"):
                        first_degraded.setdefault(node, span.start)
            prev = folded

        # A propagation edge ignites when both of its endpoints have degraded.
        self.edge_ignition: dict[tuple[str, str], float] = {}
        for step in analysis.propagation:
            a, b = step.from_service, step.to_service
            if a in first_degraded and b in first_degraded:
                key: tuple[str, str] = tuple(sorted((a, b)))  # type: ignore[assignment]
                at = max(first_degraded[a], first_degraded[b])
                current = self.edge_ignition.get(key)
                self.edge_ignition[key] = min(current, at) if current is not None else at

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _beat_timestamp(analysis: IncidentAnalysis, beat: NarrationBeat) -> datetime | None:
        if beat.kind == "intro":
            return analysis.timeline[0].timestamp if analysis.timeline else None
        if beat.kind == "outro":
            return analysis.detected_at
        if 0 <= beat.timeline_index < len(analysis.timeline):
            return analysis.timeline[beat.timeline_index].timestamp
        return None

    def span_at(self, t: float) -> BeatSpan:
        i = bisect.bisect_right(self._starts, t) - 1
        return self.spans[max(0, min(i, len(self.spans) - 1))]

    def node_state_at(self, node: str, t: float) -> tuple[str, str, float, float]:
        """(current, previous, blend 0..1, seconds since the change)."""
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
        prev_state = changes[idx - 1].state if idx > 0 else "healthy"
        since = t - active.at
        return active.state, prev_state, clamp(since / STATE_BLEND), since

    def edge_active_at(self, key: tuple[str, str], t: float) -> tuple[bool, float]:
        """(active?, seconds since ignition)."""
        at = self.edge_ignition.get(key)
        if at is None or t < at:
            return False, 0.0
        return True, t - at

    def caption_alpha(self, span: BeatSpan, t: float) -> float:
        into = t - span.start
        left = span.end - t
        return clamp(into / CAPTION_FADE) * clamp(left / CAPTION_FADE)

    def clock_at(self, t: float) -> str:
        """Wall clock readout; ticks between beat timestamps during transitions."""
        span = self.span_at(t)
        target = span.timestamp
        if target is None:
            return ""
        prev_ts = None
        if span.index > 0:
            prev_ts = self.spans[span.index - 1].timestamp
        if prev_ts is None or prev_ts >= target:
            shown = target
        else:
            k = clamp((t - span.start) / 1.2)
            shown = prev_ts + (target - prev_ts) * k
        return shown.strftime("%H:%M:%S") + " UTC"

    def progress_at(self, t: float) -> float:
        return clamp(t / max(0.001, self.total))

    def internal_window(self) -> tuple[float, float, float] | None:
        """(dive start, fail-beat start, dive end) when the narration carries
        an internal act; None otherwise."""
        return self._dive_window("internal", "internal_fail")

    def module_window(self) -> tuple[float, float, float] | None:
        """(dive start, fail-beat start, dive end) for module dependencies."""
        return self._dive_window("module", "module_fail")

    def symbol_window(self) -> tuple[float, float, float] | None:
        """(dive start, fail-beat start, dive end) for the function-level act."""
        return self._dive_window("symbol", "symbol_fail")

    def _dive_window(
        self, kind_prefix: str, fail_kind: str
    ) -> tuple[float, float, float] | None:
        spans = [s for s in self.spans if s.beat.kind.startswith(kind_prefix)]
        if not spans:
            return None
        start = spans[0].start
        end = spans[-1].end
        fail = next(
            (s.start for s in spans if s.beat.kind == fail_kind),
            spans[-1].start,
        )
        return start, fail, end
