"""Cinematic renderer: frame contract, determinism, timeline state math."""

from __future__ import annotations

import pytest

pytest.importorskip("numpy")
pytest.importorskip("PIL")

from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService
from incidentlens.studio.cinema import CinematicScene, Timeline
from incidentlens.studio.cinema.engine import RenderSpec
from incidentlens.studio.narration import build_narration

SPEC = RenderSpec(width=640, height=360, fps=12, supersample=1.0)


def _scene(scenario: str = "gateway-auth-rejection"):
    connector = SyntheticConnector(scenario)
    architecture = connector.fetch_architecture()
    analysis = IncidentService(
        connector=connector, engine=DeterministicAnalysisEngine()
    ).analyze()
    narration = build_narration(analysis, mode="template")
    durations = [3.0] * len(narration.beats)
    timeline = Timeline(analysis, narration, durations)
    return CinematicScene(analysis, architecture, timeline, SPEC), timeline


def test_frames_have_the_requested_geometry() -> None:
    scene, _ = _scene()
    frame = scene.frame(1.0)
    assert frame.size == (640, 360)
    assert frame.mode == "RGB"


def test_identical_time_renders_identical_pixels() -> None:
    scene, _ = _scene()
    assert scene.frame(5.0).tobytes() == scene.frame(5.0).tobytes()


def test_scene_changes_as_the_incident_unfolds() -> None:
    scene, timeline = _scene()
    healthy = scene.frame(timeline.spans[0].start + 1.0)
    failing = scene.frame(timeline.spans[3].start + 1.5)
    assert healthy.tobytes() != failing.tobytes()


def test_timeline_states_follow_the_beats() -> None:
    _, timeline = _scene()
    # before anything happens the origin is healthy
    state, _, _, _ = timeline.node_state_at("hary-ai", 0.5)
    assert state == "healthy"
    # by the end of the video the origin is critical and stays there
    end = timeline.total - 0.5
    state, _, blend, _ = timeline.node_state_at("hary-ai", end)
    assert state == "critical"
    assert blend == 1.0


def test_propagation_edges_ignite_after_both_ends_degrade() -> None:
    _, timeline = _scene()
    key = tuple(sorted(("hary-ai", "hary-bff")))
    active_early, _ = timeline.edge_active_at(key, 0.5)
    active_late, since = timeline.edge_active_at(key, timeline.total - 0.5)
    assert not active_early
    assert active_late
    assert since > 0.0


def test_total_duration_covers_all_beats() -> None:
    _, timeline = _scene()
    assert timeline.total >= sum(s.duration for s in timeline.spans)
    assert timeline.spans[0].start == 0.0
