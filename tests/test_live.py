"""Log parsing, burst detection, discovery and the log->analysis path."""

from __future__ import annotations

import json
from datetime import timezone

from incidentlens.connectors.discovery import discover_architecture
from incidentlens.connectors.logfile import LogFileConnector, LogSource, parse_line
from incidentlens.domain.models import ArchitectureGraph, ServiceNode
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.live import BurstDetector

UTC = timezone.utc


# --------------------------------------------------------------- line parsing


def test_parses_python_logging_default_format() -> None:
    # the exact shape hary_ai's logging.basicConfig produces
    line = (
        "2026-07-17 10:23:45,123 - hary.models.llm_factory - ERROR - "
        "Gateway request rejected: HTTP 401 Unauthorized"
    )
    parsed = parse_line(line)
    assert parsed is not None
    assert parsed.level == "ERROR"
    assert parsed.logger == "hary.models.llm_factory"
    assert "401 Unauthorized" in parsed.message
    assert parsed.timestamp is not None and parsed.timestamp.year == 2026


def test_parses_json_lines() -> None:
    line = json.dumps(
        {"timestamp": "2026-07-17T10:23:45Z", "level": "error", "message": "boom", "logger": "svc"}
    )
    parsed = parse_line(line)
    assert parsed is not None
    assert parsed.level == "ERROR"
    assert parsed.message == "boom"


def test_parses_spring_boot_format() -> None:
    line = (
        "2026-07-17 10:23:45.123 ERROR 4242 --- [nio-8080-exec-1] "
        "c.a.hary.BffController : upstream call failed"
    )
    parsed = parse_line(line)
    assert parsed is not None
    assert parsed.level == "ERROR"
    assert "upstream call failed" in parsed.message


def test_unstructured_line_survives_as_info() -> None:
    parsed = parse_line("Traceback (most recent call last):")
    assert parsed is not None
    assert parsed.level == "INFO"


# ------------------------------------------------------------------- tailing


def test_log_source_tails_incrementally(tmp_path) -> None:
    log = tmp_path / "hary-ai.log"
    log.write_text(
        "2026-07-17 10:00:00,000 - main - INFO - service started\n", encoding="utf-8"
    )
    source = LogSource(service="hary-ai", pattern=str(log))
    first = source.read_new()
    assert [e.detail for e in first][0].startswith("service started")

    with log.open("a", encoding="utf-8") as fh:
        fh.write("2026-07-17 10:00:05,000 - main - ERROR - 401 Unauthorized from gateway\n")
    second = source.read_new()
    assert len(second) == 1
    assert second[0].attributes["level"] == "ERROR"
    assert source.read_new() == []  # nothing new


# ------------------------------------------------------------ burst detector


def test_burst_detector_trips_at_threshold_within_window() -> None:
    detector = BurstDetector(window_seconds=60.0, threshold=3)
    from datetime import datetime

    from incidentlens.domain.models import SourceType, TelemetryEvent

    def err(i: int) -> TelemetryEvent:
        return TelemetryEvent(
            id=f"e{i}", source_type=SourceType.LOG, source="svc",
            timestamp=datetime(2026, 7, 17, 10, 0, i, tzinfo=UTC),
            detail="boom", attributes={"level": "ERROR"},
        )

    info = TelemetryEvent(
        id="i0", source_type=SourceType.LOG, source="svc",
        timestamp=datetime(2026, 7, 17, 10, 0, 0, tzinfo=UTC),
        detail="fine", attributes={"level": "INFO"},
    )
    assert detector.feed([info, err(1)], now=100.0) is False
    assert detector.feed([err(2)], now=110.0) is False
    assert detector.feed([err(3)], now=120.0) is True
    # outside the window the old arrivals expire
    detector.reset()
    assert detector.feed([err(4)], now=500.0) is False


# ---------------------------------------------------- logs -> full analysis


def _hary_architecture() -> ArchitectureGraph:
    return ArchitectureGraph(
        system="hary-platform",
        services=[
            ServiceNode(name="hary-frontend", depends_on=["hary-bff"], user_facing=True),
            ServiceNode(name="hary-bff", depends_on=["hary-ai"]),
            ServiceNode(name="hary-ai", depends_on=["llm-gateway"]),
            ServiceNode(name="llm-gateway", depends_on=[]),
        ],
    )


def test_real_401_log_reproduces_an_incident(tmp_path) -> None:
    """The exact failure the Hary team hit: every Gateway call 401s."""
    log = tmp_path / "hary-ai.log"
    log.write_text(
        "2026-07-17 09:05:00,000 - main - INFO - Gateway transport initialised, health ok\n"
        "2026-07-17 09:09:00,000 - config.loader - INFO - Config change: Gateway virtual "
        "key credential rotated for hary team\n"
        "2026-07-17 09:12:04,000 - hary.models.llm_factory - ERROR - Gateway request "
        "rejected: HTTP 401 Unauthorized on POST /v1/chat/completions (x-bf-vk + Basic)\n"
        "2026-07-17 09:12:31,000 - hary.models.llm_factory - ERROR - Bearer retry also "
        "401 Unauthorized\n"
        "2026-07-17 09:13:20,000 - hary.graph.nodes.agent - ERROR - LLM calls failing "
        "fast; circuit breaker open\n",
        encoding="utf-8",
    )
    bff = tmp_path / "hary-bff.log"
    bff.write_text(
        "2026-07-17 09:14:05.000 WARN 10 --- [exec-4] c.a.BffChat : chat call to hary-ai "
        "timed out after retries\n",
        encoding="utf-8",
    )
    connector = LogFileConnector(
        [
            LogSource(service="hary-ai", pattern=str(log)),
            LogSource(service="hary-bff", pattern=str(bff)),
        ],
        _hary_architecture(),
    )
    events = connector.fetch_events()
    analysis = DeterministicAnalysisEngine().analyze(events, connector.fetch_architecture())

    assert "hary-ai" in analysis.affected_services
    origin_hypothesis = analysis.hypotheses[0]
    titles = " ".join(h.title.lower() for h in analysis.hypotheses)
    assert "hary-ai" in titles
    assert any(h.status.value == "inferred" for h in analysis.hypotheses)
    assert origin_hypothesis.evidence_ids  # provenance survives the log path
    # the gateway sent no telemetry; the analysis must say so
    assert any("llm-gateway" in gap for gap in analysis.missing_evidence)


# ----------------------------------------------------------------- discovery


def test_discovery_finds_services_and_external_gateway(tmp_path) -> None:
    (tmp_path / "web_frontend").mkdir()
    (tmp_path / "web_frontend" / "package.json").write_text(
        json.dumps({"name": "web", "dependencies": {"react": "^18"}}), encoding="utf-8"
    )
    ai = tmp_path / "ai_service"
    ai.mkdir()
    (ai / "pyproject.toml").write_text("[project]\nname='ai'\n", encoding="utf-8")
    (ai / ".env").write_text(
        "GATEWAY_BASE_URL=https://llm-gateway.internal/v1\n", encoding="utf-8"
    )
    graph = discover_architecture(tmp_path)
    names = {s.name for s in graph.services}
    assert "web-frontend" in names
    assert "ai-service" in names
    assert "llm-gateway" in names
    frontend = next(s for s in graph.services if s.name == "web-frontend")
    assert frontend.user_facing
    ai_node = next(s for s in graph.services if s.name == "ai-service")
    assert "llm-gateway" in ai_node.depends_on
