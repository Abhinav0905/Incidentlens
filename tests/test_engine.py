from datetime import datetime, timezone

import pytest

from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.domain.errors import NoIncidentDetected
from incidentlens.domain.models import (
    ArchitectureGraph,
    ConclusionStatus,
    ServiceNode,
    Severity,
    SourceType,
    TelemetryEvent,
)
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService

UTC = timezone.utc


def _analyze(scenario: str):  # type: ignore[no-untyped-def]
    connector = SyntheticConnector(scenario)
    return IncidentService(connector, DeterministicAnalysisEngine()).analyze()


def test_checkout_root_cause_is_the_deployment() -> None:
    result = _analyze("checkout-secret-rotation")
    root = max(
        (h for h in result.hypotheses if h.status == ConclusionStatus.INFERRED),
        key=lambda h: h.confidence,
    )
    assert "payment-api" in root.title
    assert 0.75 <= root.confidence <= 0.95
    assert "dep-001" in root.evidence_ids


def test_checkout_propagation_follows_dependencies() -> None:
    result = _analyze("checkout-secret-rotation")
    pairs = {(s.from_service, s.to_service) for s in result.propagation}
    assert ("payment-api", "order-api") in pairs
    assert ("order-api", "frontend") in pairs
    assert result.affected_services[0] == "payment-api"


def test_checkout_flags_missing_audit_logs() -> None:
    result = _analyze("checkout-secret-rotation")
    assert any("audit" in item.lower() for item in result.missing_evidence)
    unknowns = [h for h in result.hypotheses if h.status == ConclusionStatus.UNKNOWN]
    assert unknowns and all(h.confidence == 0.0 for h in unknowns)


def test_cache_scenario_produces_distinct_analysis() -> None:
    checkout = _analyze("checkout-secret-rotation")
    cache = _analyze("cache-stampede")
    assert cache.incident_id != checkout.incident_id
    assert cache.affected_services[0] == "results-cache"
    assert any(t.severity == Severity.RECOVERY for t in cache.timeline)
    root = max(
        (h for h in cache.hypotheses if h.status == ConclusionStatus.INFERRED),
        key=lambda h: h.confidence,
    )
    assert "results-cache" in root.title


def test_every_conclusion_resolves_to_evidence() -> None:
    for scenario in ("checkout-secret-rotation", "cache-stampede"):
        result = _analyze(scenario)
        known = {event.id for event in result.evidence}
        for item in result.timeline:
            assert set(item.evidence_ids) <= known
        for hypothesis in result.hypotheses:
            assert set(hypothesis.evidence_ids) <= known
        for step in result.propagation:
            assert set(step.evidence_ids) <= known


def test_timeline_is_chronological() -> None:
    result = _analyze("checkout-secret-rotation")
    stamps = [item.timestamp for item in result.timeline]
    assert stamps == sorted(stamps)


def test_no_failures_raises_no_incident() -> None:
    arch = ArchitectureGraph(system="quiet", services=[ServiceNode(name="api")])
    events = [
        TelemetryEvent(
            id="log-1",
            source_type=SourceType.LOG,
            source="api",
            timestamp=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            detail="All good.",
            attributes={"level": "INFO"},
        )
    ]
    with pytest.raises(NoIncidentDetected):
        DeterministicAnalysisEngine().analyze(events, arch)


def test_unexplained_failure_without_change_is_low_confidence() -> None:
    arch = ArchitectureGraph(
        system="mini",
        services=[ServiceNode(name="api", user_facing=True), ServiceNode(name="db")],
    )
    events = [
        TelemetryEvent(
            id="log-err",
            source_type=SourceType.LOG,
            source="db",
            timestamp=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            detail="Disk write failed.",
            attributes={"level": "ERROR"},
        )
    ]
    result = DeterministicAnalysisEngine().analyze(events, arch)
    root = result.hypotheses[0]
    assert root.status == ConclusionStatus.INFERRED
    assert root.confidence <= 0.5
    assert any("change history" in m.lower() for m in result.missing_evidence)


def test_model_blocked_403_produces_specific_inferred_cause_and_first_check() -> None:
    model_id = "us.modelhost.smart-tier-5-1-20250929-v1:0"
    arch = ArchitectureGraph(
        system="hary-platform",
        services=[ServiceNode(name="hary-ai", depends_on=["llm-gateway"])],
    )
    events = [
        TelemetryEvent(
            id="startup-warm",
            source_type=SourceType.LOG,
            source="hary-ai",
            timestamp=datetime(2026, 7, 23, 18, 40, tzinfo=UTC),
            detail="Model registry warmed and ready.",
            attributes={"level": "INFO"},
        ),
        TelemetryEvent(
            id="blocked-403",
            source_type=SourceType.LOG,
            source="hary-ai",
            timestamp=datetime(2026, 7, 23, 18, 50, tzinfo=UTC),
            detail=(
                "LLM call FAILED: Error code: 403 - {'type': 'model_blocked', "
                "'status_code': 403, 'error': {'message': \"Model "
                f"'{model_id}' is not allowed for this virtual key\"}}"
            ),
            attributes={"level": "ERROR"},
        ),
    ]

    result = DeterministicAnalysisEngine().analyze(events, arch)
    root = next(
        hypothesis
        for hypothesis in result.hypotheses
        if "model-policy" in hypothesis.title.lower()
    )

    assert root.status == ConclusionStatus.INFERRED
    assert root.confidence >= 0.9
    assert root.evidence_ids == ["blocked-403"]
    assert "does not establish" in root.explanation

    first = min(result.recommended_actions, key=lambda action: action.priority)
    assert "model allow-list" in first.action
    assert "virtual key" in first.action
    assert "model ID configured" in first.action
    assert "read-only" in first.risk.lower()

    # A healthy startup "warmed" line predates incident onset and is not recovery.
    assert not any(item.severity == Severity.RECOVERY for item in result.timeline)
