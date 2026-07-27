"""Small evidence predicates shared by narration and visual renderers."""

from __future__ import annotations

from incidentlens.domain.models import IncidentAnalysis, InternalTrace, StageStatus


def _logger_matches(module: str, logger: object) -> bool:
    value = str(logger or "").strip()
    return value == module or value.startswith(module + ".")


def module_failure_is_log_confirmed(
    trace: InternalTrace,
    analysis: IncidentAnalysis | None = None,
) -> bool:
    """Whether linked failure evidence names the selected module's logger.

    File ingestion includes the logger in both structured attributes and a
    human-readable ``[module]`` suffix. Other connectors may retain only the
    structured field, so confirmation must accept either representation.
    """
    if not trace.failing_module or not trace.failing_stage:
        return False
    failing = next(
        (stage for stage in trace.stages if stage.stage == trace.failing_stage),
        None,
    )
    if failing is None or failing.status != StageStatus.FAILED:
        return False

    module = trace.failing_module
    marker = f"[{module}]"
    if marker in failing.detail:
        return True
    if analysis is None:
        return False

    linked = set(failing.evidence_ids)
    for event in analysis.evidence:
        if event.id not in linked:
            continue
        if _logger_matches(module, event.attributes.get("logger")):
            return True
        if marker in event.detail:
            return True
    return False


def module_has_linked_success(
    trace: InternalTrace,
    module: str,
    stage_name: str | None,
    analysis: IncidentAnalysis | None = None,
) -> bool:
    """Whether a successful traced stage has evidence from this exact module."""
    if not stage_name or stage_name not in trace.path:
        return False
    stage = next((item for item in trace.stages if item.stage == stage_name), None)
    if stage is None or stage.status != StageStatus.OK:
        return False

    marker = f"[{module}]"
    if marker in stage.detail:
        return True
    if analysis is None:
        return False
    linked = set(stage.evidence_ids)
    return any(
        event.id in linked
        and _logger_matches(module, event.attributes.get("logger"))
        for event in analysis.evidence
    )
