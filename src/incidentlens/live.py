"""Live mode: watch real logs, detect a failure burst, produce the movie.

``incidentlens watch`` runs this loop:

    tail log files -> parse new lines -> count error-level events in a
    sliding wall-clock window -> when the burst threshold trips, wait for
    the burst to settle -> run the deterministic analysis over the recent
    telemetry -> render the narrated cinematic MP4 -> write briefing +
    analysis JSON next to it -> cool down.

Detection is intentionally boring: N error-level lines within M seconds of
*arrival* (wall clock, not log timestamps, so replayed or backfilled logs
still trigger). The evidence discipline lives in the analysis engine; this
module only decides when to wake it.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from incidentlens.connectors.logfile import LogFileConnector, LogSource
from incidentlens.domain.errors import NoIncidentDetected
from incidentlens.domain.models import ArchitectureGraph, IncidentAnalysis, TelemetryEvent
from incidentlens.engines.deterministic import DeterministicAnalysisEngine

ERROR_LEVELS = {"ERROR", "CRITICAL", "FATAL"}


@dataclass(frozen=True)
class WatchSettings:
    window_seconds: float = 90.0
    error_threshold: int = 3
    cooldown_seconds: float = 300.0
    settle_seconds: float = 5.0
    poll_seconds: float = 2.0
    lookback_events: int = 1200


@dataclass
class VideoOptions:
    voice: str = "auto"
    narration_mode: str = "template"
    model: str | None = None  # None -> narration default
    provider: str | None = None  # None -> inferred from the model id
    style: str = "cinematic"
    profile: str = "high"
    fps: int | None = None
    intro_video: str | None = None
    publish_genblaze: bool = False
    upload_b2: bool = False
    upload_genblaze_b2: bool = False


def pick_voice(name: str):
    """Resolve an explicit provider or the best configured automatic voice."""
    from incidentlens.studio.pipeline import build_voice

    return build_voice(name)


def load_config(path: str | Path) -> tuple[ArchitectureGraph, list[LogSource], WatchSettings, Path]:
    """Read incidentlens.config.json (as written by ``incidentlens discover``)."""
    path = Path(path).resolve()
    root = path.parent
    data = json.loads(path.read_text(encoding="utf-8"))

    arch_file = root / data.get("architecture", "incidentlens.arch.json")
    architecture = ArchitectureGraph.model_validate(
        json.loads(arch_file.read_text(encoding="utf-8"))
    )

    sources = [
        LogSource(service=entry["service"], pattern=entry["path"], root=root)
        for entry in data.get("logs", [])
    ]

    w = data.get("watch", {})
    settings = WatchSettings(
        window_seconds=float(w.get("window_seconds", 90)),
        error_threshold=int(w.get("error_threshold", 3)),
        cooldown_seconds=float(w.get("cooldown_seconds", 300)),
    )
    out_dir = root / w.get("out_dir", "incidentlens-videos")
    return architecture, sources, settings, out_dir


def analyze_events(
    events: list[TelemetryEvent], architecture: ArchitectureGraph
) -> IncidentAnalysis:
    return DeterministicAnalysisEngine().analyze(events, architecture)


def write_companions(analysis: IncidentAnalysis, video_path: Path) -> tuple[Path, Path]:
    """Briefing markdown + full analysis JSON next to the video."""
    base = video_path.with_suffix("")
    briefing = Path(f"{base}.briefing.md")
    hypotheses = "\n".join(
        f"- **{h.title}** — {h.status.value}, confidence {h.confidence:.2f}. {h.explanation}"
        for h in analysis.hypotheses
    )
    actions = "\n".join(
        f"{a.priority}. {a.action}\n   - why: {a.reason}\n   - risk: {a.risk}"
        for a in sorted(analysis.recommended_actions, key=lambda a: a.priority)
    )
    gaps = "\n".join(f"- {m}" for m in analysis.missing_evidence) or "- none recorded"
    briefing.write_text(
        f"# {analysis.title}\n\n"
        f"`{analysis.incident_id}` · started {analysis.started_at.isoformat()} · "
        f"detected {analysis.detected_at.isoformat()}\n\n"
        f"## Engineer briefing\n\n{analysis.engineer_briefing}\n\n"
        f"## Customer impact\n\n{analysis.customer_impact}\n\n"
        f"## Hypotheses\n\n{hypotheses}\n\n"
        f"## Recommended actions\n\n{actions}\n\n"
        f"## Missing evidence\n\n{gaps}\n",
        encoding="utf-8",
    )
    analysis_json = Path(f"{base}.analysis.json")
    analysis_json.write_text(
        json.dumps(analysis.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return briefing, analysis_json


def load_code_graphs_near(anchor: str | Path) -> dict:
    """incidentlens.codegraph.json sitting next to the arch/config file, if any."""
    from incidentlens.connectors.code_graph import load_code_graphs

    anchor = Path(anchor).resolve()
    base = anchor if anchor.is_dir() else anchor.parent
    candidate = base / "incidentlens.codegraph.json"
    if candidate.is_file():
        try:
            return load_code_graphs(candidate)
        except (ValueError, OSError):
            return {}
    return {}


def render_incident(
    events: list[TelemetryEvent],
    architecture: ArchitectureGraph,
    out_dir: Path,
    options: VideoOptions,
    *,
    code_graphs: dict | None = None,
    progress=None,
) -> tuple[IncidentAnalysis, Path]:
    from incidentlens.studio.narration import DEFAULT_MODEL
    from incidentlens.studio.pipeline import produce_video_from_analysis

    analysis = analyze_events(events, architecture)
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = out_dir / f"{analysis.incident_id}.mp4"
    result = produce_video_from_analysis(
        analysis,
        architecture,
        video_path,
        code_graphs=code_graphs,
        voice=pick_voice(options.voice),
        narration_mode=options.narration_mode,
        model=options.model or DEFAULT_MODEL,
        provider=options.provider,
        style=options.style,
        profile=options.profile,
        fps=options.fps,
        intro_video=options.intro_video,
        publish_genblaze=options.publish_genblaze,
        upload=options.upload_b2,
        upload_genblaze_b2=options.upload_genblaze_b2,
        source_label="live-logs",
        progress=progress,
    )
    write_companions(analysis, result.path)
    if code_graphs:
        from incidentlens.studio.graphview import render_code_graph_html
        from incidentlens.studio.mermaid import write_mermaid

        base = Path(result.path.with_suffix(""))
        render_code_graph_html(
            code_graphs,
            Path(f"{base}.code-graph.html"),
            analysis=analysis,
            subtitle=f"{architecture.system} · incident {analysis.incident_id}",
        )
        # Mermaid of the failing service: symbol level, focused on the failure.
        trace = analysis.internal_trace
        failing_service = trace.service if trace else None
        graph = code_graphs.get(failing_service) if failing_service else None
        if graph is not None:
            write_mermaid(
                graph, Path(f"{base}.code-graph.mmd"),
                level="symbol", analysis=analysis,
            )
    return analysis, result.path


@dataclass
class BurstDetector:
    """N error-level events within a sliding wall-clock window."""

    window_seconds: float = 90.0
    threshold: int = 3
    _arrivals: deque = field(default_factory=deque)

    def feed(self, events: list[TelemetryEvent], now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        for event in events:
            level = str(event.attributes.get("level", "")).upper()
            if level in ERROR_LEVELS:
                self._arrivals.append(now)
        cutoff = now - self.window_seconds
        while self._arrivals and self._arrivals[0] < cutoff:
            self._arrivals.popleft()
        return len(self._arrivals) >= self.threshold

    def reset(self) -> None:
        self._arrivals.clear()


class IncidentWatcher:
    """The long-running loop behind ``incidentlens watch``."""

    def __init__(
        self,
        sources: list[LogSource],
        architecture: ArchitectureGraph,
        out_dir: Path,
        settings: WatchSettings | None = None,
        options: VideoOptions | None = None,
        code_graphs: dict | None = None,
        log=print,
    ) -> None:
        self.connector = LogFileConnector(sources, architecture)
        self.architecture = architecture
        self.out_dir = out_dir
        self.settings = settings or WatchSettings()
        self.options = options or VideoOptions()
        self.code_graphs = code_graphs or {}
        self.detector = BurstDetector(
            window_seconds=self.settings.window_seconds,
            threshold=self.settings.error_threshold,
        )
        self.buffer: list[TelemetryEvent] = []
        self._cooldown_until = 0.0
        self._log = log

    def poll(self) -> bool:
        """Ingest new lines; True when a failure burst just tripped the threshold."""
        new_events = self.connector.fetch_events()
        if new_events:
            self.buffer.extend(new_events)
            self.buffer.sort(key=lambda e: e.timestamp)
            if len(self.buffer) > self.settings.lookback_events:
                self.buffer = self.buffer[-self.settings.lookback_events:]
        tripped = self.detector.feed(new_events)
        return tripped and time.monotonic() >= self._cooldown_until

    def produce(self) -> tuple[IncidentAnalysis, Path] | None:
        try:
            analysis, video = render_incident(
                list(self.buffer), self.architecture, self.out_dir, self.options,
                code_graphs=self.code_graphs,
                progress=lambda p: self._log(f"\r  rendering… {p * 100:5.1f}%", end=""),
            )
        except NoIncidentDetected:
            self._log("burst detected but the engine found no incident; continuing")
            return None
        self._log("")  # newline after the progress line
        self._cooldown_until = time.monotonic() + self.settings.cooldown_seconds
        self.detector.reset()
        return analysis, video

    def run_forever(self) -> None:
        names = ", ".join(sorted({s.service for s in self.connector.sources}))
        self._log(f"watching logs for: {names}")
        self._log(
            f"trigger: {self.settings.error_threshold} error-level lines within "
            f"{self.settings.window_seconds:.0f}s · videos -> {self.out_dir}"
        )
        while True:
            if self.poll():
                self._log("failure burst detected — letting it settle…")
                time.sleep(self.settings.settle_seconds)
                self.poll()  # swallow the rest of the burst
                produced = self.produce()
                if produced:
                    analysis, video = produced
                    self._log(f"incident {analysis.incident_id}: {analysis.title}")
                    self._log(f"video written: {video}")
                    self._log(f"briefing:      {video.with_suffix('')}.briefing.md")
            time.sleep(self.settings.poll_seconds)
