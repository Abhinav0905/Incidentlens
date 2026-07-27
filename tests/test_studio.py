from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.domain.models import (
    ArchitectureGraph,
    InternalStage,
    ServiceInternals,
    ServiceNode,
    SourceType,
    TelemetryEvent,
)
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService
from incidentlens.studio import frames
from incidentlens.studio.narration import Narration, NarrationBeat, build_narration
from incidentlens.studio.voice import SilentVoice, audio_duration


def _analysis(scenario: str = "checkout-secret-rotation"):
    connector = SyntheticConnector(scenario)
    engine = DeterministicAnalysisEngine()
    analysis = IncidentService(connector=connector, engine=engine).analyze()
    return connector.fetch_architecture(), analysis


def test_template_narration_covers_every_timeline_beat() -> None:
    _, analysis = _analysis()
    narration = build_narration(analysis, mode="template")

    assert narration.beats[0].kind == "intro"
    assert narration.beats[-1].kind == "outro"

    event_indices = sorted(b.timeline_index for b in narration.beats if b.kind == "event")
    assert event_indices == list(range(len(analysis.timeline)))


def test_template_narration_lines_are_speakable() -> None:
    _, analysis = _analysis()
    narration = build_narration(analysis, mode="template")
    for beat in narration.beats:
        assert beat.text.strip()
        assert len(beat.text) <= 320  # keep lines short enough for TTS


def test_summaries_never_cut_a_spoken_word_in_half() -> None:
    from incidentlens.engines.deterministic import _short
    from incidentlens.studio.narration import _first_sentence

    text = "alpha bravo charlie delta"
    assert _short(text, 18) == "alpha bravo…"
    assert _first_sentence(text, 18) == "alpha bravo…"


def test_customer_impact_narrates_the_user_visible_message() -> None:
    _, analysis = _analysis()
    impact = next(frame for frame in analysis.timeline if frame.title == "Customer impact")
    analysis.customer_impact = (
        "frontend returned HTTP 502. Users see 'Assistant is unavailable'; "
        "several more requests were rejected."
    )
    impact.description = analysis.customer_impact

    narration = build_narration(analysis, mode="template")
    beat = next(
        beat
        for beat in narration.beats
        if beat.kind == "event" and beat.timeline_index == analysis.timeline.index(impact)
    )
    assert beat.text == "Customers are now affected. Users see 'Assistant is unavailable'."


def test_outro_frames_root_cause_as_a_lead_not_a_verdict() -> None:
    _, analysis = _analysis()
    narration = build_narration(analysis, mode="template")
    outro = next(b for b in narration.beats if b.kind == "outro")
    # The narration must not assert the inferred cause as settled fact.
    assert "verify" in outro.text.lower() or "points to" in outro.text.lower()


def test_template_narration_humanizes_model_blocked_403() -> None:
    model_id = "us.modelhost.smart-tier-5-1-20250929-v1:0"
    architecture = ArchitectureGraph(
        system="hary-platform",
        services=[
            ServiceNode(
                name="hary-ai",
                internals=ServiceInternals(
                    entry="request",
                    stages=[
                        InternalStage(name="request", modules=["hary.transport.routes"]),
                        InternalStage(
                            name="llm-client", modules=["hary.graph.nodes.agent"]
                        ),
                    ],
                    edges=[("request", "llm-client")],
                ),
            )
        ],
    )
    events = [
        TelemetryEvent(
            id="startup",
            source_type=SourceType.LOG,
            source="hary-ai",
            timestamp=datetime(2026, 7, 23, 18, 40, tzinfo=timezone.utc),
            detail="Model client warmed and ready.",
            attributes={"level": "INFO", "logger": "startup"},
        ),
        TelemetryEvent(
            id="blocked-403",
            source_type=SourceType.LOG,
            source="hary-ai",
            timestamp=datetime(2026, 7, 23, 18, 50, tzinfo=timezone.utc),
            detail=(
                "[graph:agent] LLM call FAILED after 3 attempts: Error code: 403 - "
                "{'type': 'model_blocked', 'is_gateway_error': False, "
                "'status_code': 403, 'error': {'message': \"Model "
                f"'{model_id}' is not allowed for this virtual key\"}}, "
                "'extra_fields': {'provider': 'model-provider'}}"
            ),
            attributes={"level": "ERROR", "logger": "hary.graph.nodes.agent"},
        ),
    ]
    analysis = DeterministicAnalysisEngine().analyze(events, architecture)

    narration = build_narration(analysis, mode="template")
    spoken = " ".join(beat.text for beat in narration.beats)
    lowered = spoken.lower()

    assert "not allowed for the active virtual key" in lowered
    assert model_id not in spoken
    assert "model_blocked" not in spoken
    assert "{'type'" not in spoken
    assert "status_code" not in spoken
    assert "extra_fields" not in spoken


def test_template_narration_humanizes_invalid_model_identifier() -> None:
    model_id = "us.modelhst.fast-tier-4-5-20251001-v1:0"
    architecture = ArchitectureGraph(
        system="hary-platform",
        services=[ServiceNode(name="hary-ai")],
    )
    events = [
        TelemetryEvent(
            id="baseline",
            source_type=SourceType.LOG,
            source="hary-ai",
            timestamp=datetime(2026, 7, 23, 9, 5, tzinfo=timezone.utc),
            detail="Model registry ready.",
            attributes={"level": "INFO"},
        ),
        TelemetryEvent(
            id="model-change",
            source_type=SourceType.LOG,
            source="hary-ai",
            timestamp=datetime(2026, 7, 23, 9, 9, tzinfo=timezone.utc),
            detail=(
                "Config change: FAST_TIER_MODEL_ID environment variable set to "
                f"'{model_id}'."
            ),
            attributes={"level": "INFO"},
        ),
        TelemetryEvent(
            id="invalid-model",
            source_type=SourceType.LOG,
            source="hary-ai",
            timestamp=datetime(2026, 7, 23, 9, 12, tzinfo=timezone.utc),
            detail=(
                "ModelProvider InvokeModel failed: ValidationException - "
                f"The provided model identifier is invalid: '{model_id}'."
            ),
            attributes={"level": "ERROR"},
        ),
    ]
    analysis = DeterministicAnalysisEngine().analyze(events, architecture)

    narration = build_narration(analysis, mode="template")
    spoken = " ".join(beat.text for beat in narration.beats)

    assert model_id not in spoken
    assert "configured model identifier changes" in spoken
    assert "rejected the configured model identifier as invalid" in spoken


def test_fold_states_marks_origin_critical_by_the_end() -> None:
    _, analysis = _analysis()
    states, failed_at = frames.fold_states(analysis, len(analysis.timeline) - 1)
    assert states["payment-api"] == "critical"
    assert states["frontend"] == "critical"
    # payment-api degrades before frontend.
    assert failed_at["payment-api"] < failed_at["frontend"]


def test_active_edges_are_direction_insensitive() -> None:
    _, analysis = _analysis()
    _, failed_at = frames.fold_states(analysis, len(analysis.timeline) - 1)
    edges = frames.active_edges(analysis, failed_at)
    # Drawn as order-api -> payment-api (dependency), propagation is the reverse;
    # the sorted pair must still be marked active.
    assert tuple(sorted(("payment-api", "order-api"))) in edges


def test_recovery_state_appears_for_cache_scenario() -> None:
    _, analysis = _analysis("cache-stampede")
    states, _ = frames.fold_states(analysis, len(analysis.timeline) - 1)
    assert "recovery" in states.values()


def test_layout_places_user_facing_service_leftmost() -> None:
    architecture, _ = _analysis()
    layout = frames.compute_layout(architecture)
    user_facing = [s.name for s in architecture.services if s.user_facing]
    leftmost_x = min(box.x for box in layout.boxes.values())
    for name in user_facing:
        assert layout.boxes[name].x == leftmost_x


def test_silent_voice_writes_measurable_audio(tmp_path) -> None:
    out = tmp_path / "silence.wav"
    duration = SilentVoice().synthesize("a short spoken line for timing", out)
    assert out.exists()
    assert duration >= 2.0
    assert abs(audio_duration(out) - duration) < 0.1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_video_produces_a_playable_mp4(tmp_path) -> None:
    pytest.importorskip("cairosvg")
    from incidentlens.studio.render import render_video

    architecture, analysis = _analysis()
    # Trim to three beats to keep the test fast; the pipeline itself is unchanged.
    beats = [
        NarrationBeat(kind="intro", timeline_index=-1, title="Intro", text="Reconstructing."),
        NarrationBeat(
            kind="event",
            timeline_index=2,
            title="Origin",
            text="The payment service fails.",
            evidence_ids=["log-003"],
        ),
        NarrationBeat(kind="outro", timeline_index=5, title="Check", text="Verify the secret."),
    ]
    narration = Narration(incident_id=analysis.incident_id, title=analysis.title, beats=beats)

    intro = tmp_path / "intro.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x0a0d13:s=320x180:r=8:d=0.5",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(intro),
        ],
        check=True,
    )
    out = tmp_path / "clip.mp4"
    render_video(
        analysis,
        architecture,
        narration,
        SilentVoice(),
        out,
        fps=8,
        intro_video=intro,
    )

    assert out.exists()
    assert out.stat().st_size > 0
    duration = audio_duration(out)
    assert duration > 0.5
