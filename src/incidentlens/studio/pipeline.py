"""End-to-end video production.

``produce_incident_video`` keeps the original contract — bundled scenario name
in, narrated MP4 out. ``produce_video_from_analysis`` is the same tail of the
pipeline for callers that already hold an analysis: the live log watcher, the
``analyze`` command, or any custom connector.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.domain.models import ArchitectureGraph, CodeGraph, IncidentAnalysis
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService
from incidentlens.studio.narration import DEFAULT_MODEL, build_narration
from incidentlens.studio.render import ProgressFn, render_video
from incidentlens.studio.voice import (
    ElevenLabsVoice,
    GenblazeOpenAIVoice,
    OfflineVoice,
    OpenAIVoice,
    PiperVoice,
    SilentVoice,
    Voice,
)


@dataclass(frozen=True)
class VideoResult:
    path: Path
    scenario: str
    incident_id: str
    beats: int
    url: str | None = None
    manifest_path: Path | None = None
    manifest_hash: str | None = None
    manifest_uri: str | None = None
    narration_manifest_path: Path | None = None


def build_voice(name: str) -> Voice:
    name = name.lower()
    if name == "auto":
        openai_voice = OpenAIVoice()
        if openai_voice.available():
            return openai_voice
        piper = PiperVoice()
        if piper.available():
            return piper
        offline = OfflineVoice()
        return offline if offline.available() else SilentVoice()
    if name == "offline":
        return OfflineVoice()
    if name == "openai":
        return OpenAIVoice()
    if name == "genblaze":
        return GenblazeOpenAIVoice()
    if name == "piper":
        return PiperVoice()
    if name == "elevenlabs":
        return ElevenLabsVoice()
    if name == "silent":
        return SilentVoice()
    raise ValueError(
        f"unknown voice: {name!r} "
        "(choose auto, genblaze, openai, offline, piper, elevenlabs or silent)"
    )


def produce_video_from_analysis(
    analysis: IncidentAnalysis,
    architecture: ArchitectureGraph,
    out_path: str | Path,
    *,
    code_graphs: Mapping[str, CodeGraph] | None = None,
    voice: Voice | str = "offline",
    narration_mode: str = "template",
    model: str = DEFAULT_MODEL,
    provider: str | None = None,
    style: str = "cinematic",
    profile: str = "high",
    fps: int | None = None,
    intro_video: str | Path | None = None,
    publish_genblaze: bool = False,
    upload: bool = False,
    upload_genblaze_b2: bool = False,
    source_label: str = "live",
    progress: ProgressFn | None = None,
) -> VideoResult:
    if code_graphs:
        from incidentlens.connectors.code_graph import enrich_trace_with_code

        enrich_trace_with_code(analysis, dict(code_graphs))
    narration = build_narration(
        analysis, mode=narration_mode, model=model, provider=provider
    )
    voice_impl = build_voice(voice) if isinstance(voice, str) else voice
    trace = analysis.internal_trace
    code_graph = code_graphs.get(trace.service) if code_graphs and trace else None

    out = render_video(
        analysis, architecture, narration, voice_impl, out_path,
        code_graph=code_graph,
        fps=fps, style=style, profile=profile, intro_video=intro_video,
        progress=progress,
    )

    url: str | None = None
    manifest_path: Path | None = None
    manifest_hash: str | None = None
    manifest_uri: str | None = None
    narration_manifest_path: Path | None = None
    if upload and upload_genblaze_b2:
        raise ValueError("choose either legacy B2 upload or Genblaze B2 publishing")

    narration_manifest = getattr(voice_impl, "latest_manifest", None)
    if publish_genblaze or upload_genblaze_b2:
        from incidentlens.studio.genblaze import publish_video

        published = publish_video(
            out,
            analysis=analysis,
            source_label=source_label,
            narration_manifest=narration_manifest,
            upload_b2=upload_genblaze_b2,
        )
        url = published.asset_url if upload_genblaze_b2 else None
        manifest_path = published.manifest_path
        manifest_hash = published.canonical_hash
        manifest_uri = published.manifest_uri
        narration_manifest_path = published.narration_manifest_path
    elif upload:
        from incidentlens.studio.storage import BackblazeB2Storage

        url = BackblazeB2Storage().upload(out, key=f"{analysis.incident_id}.mp4")

    return VideoResult(
        path=out,
        scenario=source_label,
        incident_id=analysis.incident_id,
        beats=len(narration.beats),
        url=url,
        manifest_path=manifest_path,
        manifest_hash=manifest_hash,
        manifest_uri=manifest_uri,
        narration_manifest_path=narration_manifest_path,
    )


def produce_incident_video(
    scenario: str,
    out_path: str | Path,
    *,
    voice: Voice | str = "offline",
    narration_mode: str = "template",
    model: str = DEFAULT_MODEL,
    provider: str | None = None,
    style: str = "cinematic",
    profile: str = "high",
    fps: int | None = None,
    intro_video: str | Path | None = None,
    publish_genblaze: bool = False,
    upload: bool = False,
    upload_genblaze_b2: bool = False,
    progress: ProgressFn | None = None,
) -> VideoResult:
    connector = SyntheticConnector(scenario)
    architecture = connector.fetch_architecture()
    analysis = IncidentService(
        connector=connector, engine=DeterministicAnalysisEngine()
    ).analyze()
    code_graphs = connector.fetch_code_graphs()
    return produce_video_from_analysis(
        analysis,
        architecture,
        out_path,
        code_graphs=code_graphs,
        voice=voice,
        narration_mode=narration_mode,
        model=model,
        provider=provider,
        style=style,
        profile=profile,
        fps=fps,
        intro_video=intro_video,
        publish_genblaze=publish_genblaze,
        upload=upload,
        upload_genblaze_b2=upload_genblaze_b2,
        source_label=scenario,
        progress=progress,
    )
