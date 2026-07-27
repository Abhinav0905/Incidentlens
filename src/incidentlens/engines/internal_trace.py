"""Trace the request path through the origin service's internal pipeline.

Given the internal stage graph of the failing service (from ``discover`` or
written by hand) and that service's log events, work out where inside the
service the request died — and what state every other stage was in.

The evidence rule is enforced by construction:

* ``ok``       — the stage logged during the incident window (evidence IDs kept)
* ``failed``   — the stage that emitted the first error-level line
* ``inferred`` — upstream of the failure on the traversed path but silent;
                 the pipeline shape implies traversal, so it is labeled as an
                 inference, not as observed fact
* ``not-reached`` — on the path after the failing stage
* ``dormant``  — branches the traced request never took
* ``unknown``  — a stage that logged nothing and sits on no traced path
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timedelta

from incidentlens.domain.models import (
    ArchitectureGraph,
    InternalStageTrace,
    InternalTrace,
    ServiceInternals,
    SourceType,
    StageStatus,
    TelemetryEvent,
)

ERROR_LEVELS = {"ERROR", "CRITICAL", "FATAL"}
LOOKBACK = timedelta(minutes=15)

_LOGGER_SUFFIX = re.compile(r"\[([A-Za-z_][\w.]*)\]\s*$")


def _event_logger(event: TelemetryEvent) -> str:
    logger = event.attributes.get("logger")
    if isinstance(logger, str) and logger:
        return logger
    match = _LOGGER_SUFFIX.search(event.detail)
    return match.group(1) if match else ""


def _stage_for_logger(logger: str, internals: ServiceInternals) -> str | None:
    """Longest module-prefix match wins; a stage name match is the fallback."""
    if not logger:
        return None
    best: tuple[int, str] | None = None
    for stage in internals.stages:
        for prefix in stage.modules:
            if logger == prefix or logger.startswith(prefix + "."):
                if best is None or len(prefix) > best[0]:
                    best = (len(prefix), stage.name)
    if best:
        return best[1]
    last = logger.rsplit(".", 1)[-1].replace("_", "-")
    for stage in internals.stages:
        if stage.name.replace("_", "-") == last:
            return stage.name
    return None


def _shortest_path(edges: list[tuple[str, str]], src: str, dst: str) -> list[str]:
    adjacency: dict[str, list[str]] = {}
    for a, b in edges:
        adjacency.setdefault(a, []).append(b)
    queue: deque[list[str]] = deque([[src]])
    seen = {src}
    while queue:
        path = queue.popleft()
        if path[-1] == dst:
            return path
        for nxt in adjacency.get(path[-1], []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return []


def trace_internals(
    events: list[TelemetryEvent],
    architecture: ArchitectureGraph,
    origin_service: str,
    origin_ts: datetime,
) -> InternalTrace | None:
    service = next((s for s in architecture.services if s.name == origin_service), None)
    if service is None or service.internals is None or not service.internals.stages:
        return None
    internals = service.internals

    window_events = [
        e
        for e in events
        if e.source == origin_service
        and e.source_type == SourceType.LOG
        and origin_ts - LOOKBACK <= e.timestamp
    ]
    window_events.sort(key=lambda e: e.timestamp)

    evidence: dict[str, list[str]] = {}
    details: dict[str, str] = {}
    failing: str | None = None
    failing_detail = ""
    for event in window_events:
        matched = _stage_for_logger(_event_logger(event), internals)
        if matched is None:
            continue
        level = str(event.attributes.get("level", "")).upper()
        evidence.setdefault(matched, []).append(event.id)
        if level in ERROR_LEVELS and failing is None:
            failing = matched
            failing_detail = event.detail
        elif matched not in details:
            details[matched] = event.detail

    entry = internals.entry_stage()
    path: list[str] = []
    if failing and entry:
        path = _shortest_path(internals.edges, entry, failing)
        if not path and entry != failing:
            path = [entry, failing]  # graph gap; keep endpoints honest
        elif not path:
            path = [failing]

    on_path = set(path)
    traces: list[InternalStageTrace] = []
    for stage in internals.stages:
        stage_name = stage.name
        ids = evidence.get(stage_name, [])
        if stage_name == failing:
            status = StageStatus.FAILED
            detail = failing_detail
        elif stage_name in on_path:
            if ids:
                status = StageStatus.OK
                detail = details.get(stage_name, "reported telemetry during the traversal")
            else:
                status = StageStatus.INFERRED
                detail = "on the traversed path; no telemetry of its own"
        elif ids:
            status = StageStatus.OK
            detail = details.get(stage_name, "")
        elif failing is not None:
            status = StageStatus.DORMANT
            detail = "not on the traced request path"
        else:
            status = StageStatus.UNKNOWN
            detail = ""
        traces.append(
            InternalStageTrace(stage=stage_name, status=status, detail=detail, evidence_ids=ids)
        )

    # Stages strictly after the failure on the path are not-reached, not dormant.
    if failing and path:
        reached_cut = path.index(failing)
        not_reached = set(path[reached_cut + 1:])
        # successors of the failing stage that were on no path also never ran
        for a, b in internals.edges:
            if a == failing:
                not_reached.add(b)
        for trace in traces:
            if trace.stage in not_reached and trace.status in (
                StageStatus.DORMANT,
                StageStatus.INFERRED,
                StageStatus.UNKNOWN,
            ):
                trace.status = StageStatus.NOT_REACHED
                trace.detail = "never ran — the request died upstream"

    if failing is None and not any(t.evidence_ids for t in traces):
        return None  # nothing attributable inside the service; skip the act

    hop_count = max(0, len(path) - 1)
    summary = (
        f"Request traced through {hop_count} stage(s) inside {origin_service}; "
        + (f"failed at {failing}." if failing else "no failing stage attributable.")
    )
    return InternalTrace(
        service=origin_service,
        stages=traces,
        path=path,
        failing_stage=failing,
        summary=summary,
    )
