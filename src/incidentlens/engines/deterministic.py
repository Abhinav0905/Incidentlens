"""Deterministic incident reconstruction.

This engine turns raw telemetry plus an architecture graph into an evidence-backed
incident analysis. Every rule is explicit and inspectable. Nothing here is learned,
sampled or guessed by a model; the same input always produces the same output.

Pipeline:

1. classify events into signals (baselines, changes, failures, warnings, recoveries)
2. detect metric anomalies against in-series baselines
3. locate the origin service and the incident window
4. walk the dependency graph to reconstruct failure propagation
5. score root-cause hypotheses with a documented confidence formula
6. detect missing evidence
7. generate actions, briefings and a replay script from the reconstructed facts

Confidence formula for change-correlated root causes:

    0.45 base
    + 0.25 if the first failure follows the change within 5 minutes
      (+ 0.15 if within 15 minutes instead)
    + 0.10 if the change and the first failure occur on the same service
    + 0.10 if change keywords match the failure class
      (secret/credential -> auth errors, restart/cold -> latency and misses, ...)
    capped at 0.95, because an inferred cause is never a confirmed one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from incidentlens.domain.errors import NoIncidentDetected
from incidentlens.domain.models import (
    ActionItem,
    ArchitectureGraph,
    ConclusionStatus,
    Hypothesis,
    IncidentAnalysis,
    PropagationStep,
    Severity,
    SourceType,
    TelemetryEvent,
    TimelineEvent,
)
from incidentlens.engines.base import AnalysisEngine

CHANGE_KEYWORDS = (
    "deploy",
    "rollout",
    "release",
    "restart",
    "reboot",
    "rotat",
    "config change",
    "flag flip",
    "scaled down",
    "scale-down",
    "maintenance",
    "migration",
)

RECOVERY_KEYWORDS = ("recovered", "restored", "back to normal", "warmed", "healthy again")

ERROR_LEVELS = {"ERROR", "CRITICAL", "FATAL"}

# Keyword affinity classes: if a change matches the left side and the first failure
# matches the right side, the change is a better explanation for the failure.
AFFINITY_CLASSES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "credential": (
        ("secret", "credential", "password", "key rotation", "certificate", "token"),
        ("auth", "credential", "permission", "denied", "certificate", "unauthorized"),
    ),
    "capacity": (
        ("restart", "reboot", "cold", "evict", "scale", "maintenance"),
        ("latency", "timeout", "saturat", "slow", "hit_ratio", "hit ratio", "miss", "queue"),
    ),
    "config": (
        ("config", "flag", "setting", "environment variable"),
        ("invalid", "parse", "unexpected", "not found", "missing"),
    ),
}

MECHANISM_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("timeout", "timed out"), "dependency timeouts"),
    (("retry", "retries", "retrying"), "retry amplification"),
    (("saturat", "exhaust", "pool", "utilization"), "worker or queue saturation"),
    (("latency", "slow", "p95", "p99", "_ms"), "latency propagation"),
    (("backlog", "depth", "queue"), "queue backlog growth"),
    (("http 5", "500", "502", "503", "504"), "upstream errors"),
)


@dataclass
class _Signals:
    baselines: list[TelemetryEvent] = field(default_factory=list)
    changes: list[TelemetryEvent] = field(default_factory=list)
    failures: list[TelemetryEvent] = field(default_factory=list)
    warnings: list[TelemetryEvent] = field(default_factory=list)
    recoveries: list[TelemetryEvent] = field(default_factory=list)
    anomalies: list[_Anomaly] = field(default_factory=list)


@dataclass
class _Anomaly:
    event: TelemetryEvent
    metric: str
    description: str
    baseline: float | None
    value: float
    recovered: bool = False

    @property
    def timestamp(self) -> datetime:
        return self.event.timestamp

    @property
    def source(self) -> str:
        return self.event.source


def _text(event: TelemetryEvent) -> str:
    return event.detail.lower()


def _metric_name(event: TelemetryEvent) -> str:
    name = event.attributes.get("metric")
    if isinstance(name, str) and name:
        return name
    match = re.match(r"\s*([a-zA-Z0-9_.:-]+)\s*=", event.detail)
    return match.group(1) if match else event.detail[:40]


def _metric_value(event: TelemetryEvent) -> float | None:
    value = event.attributes.get("value")
    if isinstance(value, int | float):
        return float(value)
    match = re.search(r"=\s*(-?\d+(?:\.\d+)?)", event.detail)
    return float(match.group(1)) if match else None


def _is_change(event: TelemetryEvent) -> bool:
    if event.source_type == SourceType.DEPLOYMENT:
        return True
    return event.source_type == SourceType.LOG and any(
        keyword in _text(event) for keyword in CHANGE_KEYWORDS
    )


def _is_recovery_log(event: TelemetryEvent) -> bool:
    return any(keyword in _text(event) for keyword in RECOVERY_KEYWORDS)


def _is_model_policy_failure(event: TelemetryEvent) -> bool:
    """An explicit gateway rejection of the requested model/virtual-key pairing."""
    metadata = " ".join(str(value) for value in event.attributes.values())
    text = f"{event.detail} {metadata}".lower()
    return "model_blocked" in text or (
        "model" in text and "not allowed" in text and "virtual key" in text
    )


def _single_point_anomaly(name: str, value: float) -> str | None:
    """Flag a lone metric sample with no in-series baseline. Documented heuristics only."""
    lowered = name.lower()
    if any(k in lowered for k in ("error", "fail", "retr", "auth")) and value >= 10:
        return "elevated"
    if any(k in lowered for k in ("utilization", "saturation")) and value >= 95:
        return "saturated"
    if any(k in lowered for k in ("depth", "backlog", "queue")) and value >= 1000:
        return "backlog above threshold"
    if any(k in lowered for k in ("latency", "p95", "p99")) and value >= 1000:
        return "elevated latency"
    return None


def _detect_metric_anomalies(events: list[TelemetryEvent]) -> list[_Anomaly]:
    series: dict[tuple[str, str], list[TelemetryEvent]] = {}
    for event in events:
        if event.source_type == SourceType.METRIC and _metric_value(event) is not None:
            series.setdefault((event.source, _metric_name(event)), []).append(event)

    anomalies: list[_Anomaly] = []
    for (_, name), points in series.items():
        points.sort(key=lambda item: item.timestamp)
        values = [v for v in (_metric_value(p) for p in points) if v is not None]
        baseline = values[0]
        if len(points) == 1:
            reason = _single_point_anomaly(name, baseline)
            if reason:
                anomalies.append(
                    _Anomaly(points[0], name, f"{name} {reason} at {baseline:g}", None, baseline)
                )
            continue
        anomalous_seen = False
        for point, value in zip(points[1:], values[1:], strict=True):
            rose = value >= baseline * 3 and value - baseline >= 5
            rose = rose or (baseline < 1 and value >= 5)
            fell = baseline >= 5 and value <= baseline / 3
            if rose or fell:
                direction = "rose" if rose else "fell"
                anomalies.append(
                    _Anomaly(
                        point,
                        name,
                        f"{name} {direction} from {baseline:g} to {value:g}",
                        baseline,
                        value,
                    )
                )
                anomalous_seen = True
            elif anomalous_seen and abs(value - baseline) <= max(baseline * 0.5, 1):
                anomalies.append(
                    _Anomaly(
                        point,
                        name,
                        f"{name} returned to {value:g} (baseline {baseline:g})",
                        baseline,
                        value,
                        recovered=True,
                    )
                )
    anomalies.sort(key=lambda item: item.timestamp)
    return anomalies


def _classify(events: list[TelemetryEvent]) -> _Signals:
    signals = _Signals(anomalies=_detect_metric_anomalies(events))
    active_anomalies = [a for a in signals.anomalies if not a.recovered]
    first_trouble = min(
        [a.timestamp for a in active_anomalies]
        + [
            e.timestamp
            for e in events
            if e.source_type == SourceType.LOG
            and str(e.attributes.get("level", "")).upper() in ERROR_LEVELS
        ],
        default=None,
    )
    for event in sorted(events, key=lambda item: item.timestamp):
        if event.source_type == SourceType.ARCHITECTURE:
            continue
        level = str(event.attributes.get("level", "")).upper()
        if _is_change(event):
            signals.changes.append(event)
        elif (
            event.source_type == SourceType.LOG
            and _is_recovery_log(event)
            and first_trouble is not None
            and event.timestamp >= first_trouble
        ):
            signals.recoveries.append(event)
        elif level in ERROR_LEVELS:
            signals.failures.append(event)
        elif level == "WARN" or level == "WARNING":
            signals.warnings.append(event)
        elif event.source_type == SourceType.LOG and (
            first_trouble is None or event.timestamp < first_trouble
        ):
            signals.baselines.append(event)
    return signals


@dataclass
class _FailureSignal:
    timestamp: datetime
    source: str
    summary: str
    evidence_ids: list[str]


def _failure_signals(signals: _Signals) -> list[_FailureSignal]:
    """First failure signal per service.

    A service's incident is anchored on its first *hard* failure — an error log
    or a non-recovered metric anomaly. Warnings are early strain: they fold into
    a hard failure only when within 90s of it, and can open a signal of their
    own only for a service that has no hard failure at all. This stops unrelated
    noise (a library's startup WARNINGs minutes before the real error) from
    shadowing it and making the incident invisible.
    """

    def _mk(ts: datetime, source: str, summary: str, ids: list[str]) -> _FailureSignal:
        return _FailureSignal(ts, source, summary, list(ids))

    hard: list[_FailureSignal] = [
        _mk(e.timestamp, e.source, e.detail.rstrip("."), [e.id]) for e in signals.failures
    ]
    hard += [
        _mk(a.timestamp, a.source, a.description, [a.event.id])
        for a in signals.anomalies
        if not a.recovered
    ]
    warnings: list[_FailureSignal] = [
        _mk(e.timestamp, e.source, e.detail.rstrip("."), [e.id]) for e in signals.warnings
    ]
    hard.sort(key=lambda item: (item.timestamp, item.source))
    warnings.sort(key=lambda item: (item.timestamp, item.source))

    merged: dict[str, _FailureSignal] = {}

    def _fold(existing: _FailureSignal, item: _FailureSignal) -> None:
        existing.evidence_ids.extend(item.evidence_ids)
        if len(existing.summary) < 140:
            existing.summary = f"{existing.summary}; {item.summary}"

    # hard failures establish the anchor (and the incident onset) per service
    for item in hard:
        existing = merged.get(item.source)
        if existing is None:
            merged[item.source] = _mk(item.timestamp, item.source, item.summary, item.evidence_ids)
        elif (item.timestamp - existing.timestamp) <= timedelta(seconds=90):
            _fold(existing, item)
    # warnings: fold into a hard signal within 90s, else open an early-strain signal
    for item in warnings:
        existing = merged.get(item.source)
        if existing is None:
            merged[item.source] = _mk(item.timestamp, item.source, item.summary, item.evidence_ids)
        elif abs(item.timestamp - existing.timestamp) <= timedelta(seconds=90):
            _fold(existing, item)
    return sorted(merged.values(), key=lambda item: (item.timestamp, item.source))


def _affinity_class(change: TelemetryEvent, failure_text: str) -> str | None:
    for name, (change_kw, failure_kw) in AFFINITY_CLASSES.items():
        if any(k in _text(change) for k in change_kw) and any(
            k in failure_text.lower() for k in failure_kw
        ):
            return name
    return None


def _mechanism(text: str) -> str:
    lowered = text.lower()
    for keywords, label in MECHANISM_RULES:
        if any(k in lowered for k in keywords):
            return label
    return "downstream failure"


def _short(text: str, limit: int = 110) -> str:
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1]
    boundary = len(clipped)
    if (
        clipped
        and not clipped[-1].isspace()
        and boundary < len(text)
        and not text[boundary].isspace()
        and " " in clipped
    ):
        clipped = clipped.rsplit(" ", 1)[0]
    clipped = clipped.rstrip(" ,;:-")
    return clipped + "…"


class DeterministicAnalysisEngine(AnalysisEngine):
    """Rule-based reconstruction. See module docstring for the pipeline and formula."""

    def analyze(
        self,
        events: list[TelemetryEvent],
        architecture: ArchitectureGraph,
    ) -> IncidentAnalysis:
        events = sorted(events, key=lambda item: item.timestamp)
        signals = _classify(events)
        failures = _failure_signals(signals)
        hard_failures = [
            f
            for f in failures
            if any(
                e.id in f.evidence_ids
                for e in signals.failures + [a.event for a in signals.anomalies if not a.recovered]
            )
        ]
        if not hard_failures:
            raise NoIncidentDetected(
                "No error logs or metric anomalies found in the supplied telemetry."
            )

        origin = hard_failures[0]
        origin_evidence = set(origin.evidence_ids)
        model_policy_events = [
            event
            for event in events
            if event.id in origin_evidence and _is_model_policy_failure(event)
        ]
        change, change_class, root_confidence = self._correlate_change(signals, origin)
        started_at = (
            change.timestamp
            if change and origin.timestamp - change.timestamp <= timedelta(minutes=15)
            else origin.timestamp
        )

        steps, affected = self._propagate(architecture, origin, failures)
        user_facing = {s.name for s in architecture.services if s.user_facing}
        impact_signal = next((f for f in failures if f.source in user_facing), None)
        detected_at = impact_signal.timestamp if impact_signal else origin.timestamp

        timeline = self._timeline(signals, failures, change, impact_signal, user_facing)
        hypotheses = self._hypotheses(
            events,
            signals,
            origin,
            change,
            change_class,
            root_confidence,
            steps,
            impact_signal,
            model_policy_events,
        )
        missing = self._missing_evidence(events, architecture, change, change_class)
        recovered = bool(signals.recoveries or any(a.recovered for a in signals.anomalies))
        actions = self._actions(
            origin, change, change_class, signals, recovered, bool(model_policy_events)
        )
        impact_text = self._impact_text(signals, impact_signal)

        title = self._title(architecture, origin, change, impact_signal)
        briefing = self._engineer_briefing(
            origin, change, steps, impact_text, recovered, actions, timeline
        )
        summary = self._executive_summary(architecture, change, impact_text, recovered)
        replay = self._replay(timeline, hypotheses, actions)

        # Where inside the origin service did the request die? Only possible
        # when the architecture carries the service's internal pipeline.
        from incidentlens.engines.internal_trace import trace_internals

        internal = trace_internals(events, architecture, origin.source, origin.timestamp)

        analysis = IncidentAnalysis(
            incident_id=self._incident_id(architecture.system, origin, started_at),
            title=title,
            started_at=started_at,
            detected_at=detected_at,
            affected_services=affected,
            customer_impact=impact_text,
            timeline=timeline,
            hypotheses=hypotheses,
            propagation=steps,
            evidence=events,
            recommended_actions=actions,
            engineer_briefing=briefing,
            executive_summary=summary,
            missing_evidence=missing,
            replay_script=replay,
            internal_trace=internal,
        )
        self._validate_provenance(analysis)
        return analysis

    # ------------------------------------------------------------------ correlation

    def _correlate_change(
        self, signals: _Signals, origin: _FailureSignal
    ) -> tuple[TelemetryEvent | None, str | None, float]:
        best: tuple[TelemetryEvent | None, str | None, float] = (None, None, 0.0)
        for change in signals.changes:
            gap = origin.timestamp - change.timestamp
            if gap < timedelta(0) or gap > timedelta(hours=2):
                continue
            confidence = 0.45
            if gap <= timedelta(minutes=5):
                confidence += 0.25
            elif gap <= timedelta(minutes=15):
                confidence += 0.15
            if change.source == origin.source:
                confidence += 0.10
            affinity = _affinity_class(change, origin.summary)
            if affinity:
                confidence += 0.10
            confidence = min(confidence, 0.95)
            if confidence > best[2]:
                best = (change, affinity, confidence)
        return best

    # ------------------------------------------------------------------ propagation

    def _propagate(
        self,
        architecture: ArchitectureGraph,
        origin: _FailureSignal,
        failures: list[_FailureSignal],
    ) -> tuple[list[PropagationStep], list[str]]:
        # Order-preserving dedup, not a set: when a candidate has more than one
        # already-impacted upstream, the declared order decides which one gets
        # credit for the hop. A set would leave that to hash randomisation and
        # make the narrated cascade differ between runs.
        depends_on = {
            s.name: list(dict.fromkeys(s.depends_on)) for s in architecture.services
        }
        impacted: list[str] = [origin.source]
        steps: list[PropagationStep] = []
        pending = [f for f in failures if f.source != origin.source and f.source in depends_on]
        pending.sort(key=lambda item: (item.timestamp, item.source))

        for _ in range(len(pending) + 1):
            progressed = False
            for candidate in list(pending):
                upstream = next(
                    (dep for dep in depends_on.get(candidate.source, ()) if dep in impacted),
                    None,
                )
                if upstream:
                    steps.append(
                        PropagationStep(
                            from_service=upstream,
                            to_service=candidate.source,
                            mechanism=_mechanism(candidate.summary),
                            evidence_ids=candidate.evidence_ids,
                        )
                    )
                else:
                    pusher = next(
                        (
                            svc
                            for svc in impacted
                            if candidate.source in depends_on.get(svc, ())
                        ),
                        None,
                    )
                    if pusher is None:
                        continue
                    steps.append(
                        PropagationStep(
                            from_service=pusher,
                            to_service=candidate.source,
                            mechanism=_mechanism(candidate.summary),
                            evidence_ids=candidate.evidence_ids,
                        )
                    )
                impacted.append(candidate.source)
                pending.remove(candidate)
                progressed = True
            if not progressed:
                break
        return steps, impacted

    # ------------------------------------------------------------------ timeline

    def _timeline(
        self,
        signals: _Signals,
        failures: list[_FailureSignal],
        change: TelemetryEvent | None,
        impact_signal: _FailureSignal | None,
        user_facing: set[str],
    ) -> list[TimelineEvent]:
        items: list[TimelineEvent] = []
        baseline_ids = [e.id for e in signals.baselines[:2]]
        baseline_ids += [
            a.event.id
            for a in signals.anomalies
            if a.baseline is not None and not a.recovered
        ][:1]
        if baseline_ids:
            first = min(
                [e.timestamp for e in signals.baselines[:2]] or [failures[0].timestamp]
            )
            items.append(
                TimelineEvent(
                    timestamp=first,
                    title="System healthy",
                    description="Telemetry before the incident shows normal behavior.",
                    severity=Severity.INFO,
                    services=sorted({e.source for e in signals.baselines[:2]}),
                    evidence_ids=baseline_ids,
                )
            )
        for c in signals.changes:
            items.append(
                TimelineEvent(
                    timestamp=c.timestamp,
                    title=f"Change on {c.source}",
                    description=_short(c.detail),
                    severity=Severity.WARNING,
                    services=[c.source],
                    evidence_ids=[c.id],
                )
            )
        for f in failures:
            has_error_log = any(
                e.id in f.evidence_ids for e in signals.failures
            )
            is_hard = f.source in user_facing or f is failures[0] or has_error_log
            severity = Severity.CRITICAL if is_hard else Severity.WARNING
            items.append(
                TimelineEvent(
                    timestamp=f.timestamp,
                    title=f"{f.source}: {_short(f.summary, 60)}",
                    description=_short(f.summary, 160),
                    severity=severity,
                    services=[f.source],
                    evidence_ids=f.evidence_ids,
                )
            )
        if impact_signal:
            items.append(
                TimelineEvent(
                    timestamp=impact_signal.timestamp,
                    title="Customer impact",
                    description=_short(f"User-facing failures on {impact_signal.source}: "
                                       f"{impact_signal.summary}", 160),
                    severity=Severity.CRITICAL,
                    services=[impact_signal.source],
                    evidence_ids=impact_signal.evidence_ids,
                )
            )
        recovery_ids = [e.id for e in signals.recoveries] + [
            a.event.id for a in signals.anomalies if a.recovered
        ]
        if recovery_ids:
            first_recovery = min(
                [e.timestamp for e in signals.recoveries]
                + [a.timestamp for a in signals.anomalies if a.recovered]
            )
            items.append(
                TimelineEvent(
                    timestamp=first_recovery,
                    title="Recovery",
                    description="Recovery signals observed; key metrics returned toward baseline.",
                    severity=Severity.RECOVERY,
                    services=sorted(
                        {e.source for e in signals.recoveries}
                        | {a.source for a in signals.anomalies if a.recovered}
                    ),
                    evidence_ids=recovery_ids,
                )
            )
        items.sort(key=lambda item: item.timestamp)
        return items

    # ------------------------------------------------------------------ hypotheses

    def _hypotheses(
        self,
        events: list[TelemetryEvent],
        signals: _Signals,
        origin: _FailureSignal,
        change: TelemetryEvent | None,
        change_class: str | None,
        root_confidence: float,
        steps: list[PropagationStep],
        impact_signal: _FailureSignal | None,
        model_policy_events: list[TelemetryEvent],
    ) -> list[Hypothesis]:
        hypotheses: list[Hypothesis] = []
        arch_ids = [e.id for e in events if e.source_type == SourceType.ARCHITECTURE][:1]

        if model_policy_events:
            hypotheses.append(
                Hypothesis(
                    title=f"Likely cause: gateway model-policy rejection on {origin.source}",
                    confidence=0.92,
                    status=ConclusionStatus.INFERRED,
                    explanation=(
                        "The logged 403 response explicitly reports a model-policy rejection: "
                        "the requested model is not allowed for the active virtual key. This "
                        "strongly supports an allow-list or configured-model mismatch, but the "
                        "supplied evidence does not establish which configuration is wrong."
                    ),
                    evidence_ids=[event.id for event in model_policy_events],
                )
            )

        if change is not None:
            gap_min = max(
                1, round((origin.timestamp - change.timestamp).total_seconds() / 60)
            )
            label = f" ({change_class} pattern)" if change_class else ""
            hypotheses.append(
                Hypothesis(
                    title=f"Root cause: change on {change.source} — {_short(change.detail, 70)}",
                    confidence=root_confidence,
                    status=ConclusionStatus.INFERRED,
                    explanation=(
                        f"The change preceded the first failure on {origin.source} by about "
                        f"{gap_min} minute(s), and the failure signature matches the change"
                        f"{label}. Correlation is strong but not proof."
                    ),
                    evidence_ids=[change.id] + origin.evidence_ids + arch_ids,
                )
            )
        elif not model_policy_events:
            hypotheses.append(
                Hypothesis(
                    title=f"Root cause: unexplained failure originating at {origin.source}",
                    confidence=0.45,
                    status=ConclusionStatus.INFERRED,
                    explanation=(
                        f"{origin.source} shows the earliest failure signal, but no deployment "
                        "or change event was found in the supplied telemetry to explain it."
                    ),
                    evidence_ids=origin.evidence_ids,
                )
            )

        if steps:
            chain = " → ".join([origin.source] + [s.to_service for s in steps])
            step_ids = sorted({eid for s in steps for eid in s.evidence_ids})
            hypotheses.append(
                Hypothesis(
                    title=f"Failure propagated along the dependency graph: {chain}",
                    confidence=min(0.5 + 0.1 * len(steps), 0.88),
                    status=ConclusionStatus.INFERRED,
                    explanation=(
                        "Each downstream service shows failure signals after its upstream "
                        "dependency, in an order consistent with the architecture graph."
                    ),
                    evidence_ids=step_ids,
                )
            )

        if impact_signal:
            hypotheses.append(
                Hypothesis(
                    title="Customer-facing impact",
                    confidence=0.99,
                    status=ConclusionStatus.CONFIRMED,
                    explanation=_short(
                        f"Direct evidence from {impact_signal.source}: {impact_signal.summary}.",
                        180,
                    ),
                    evidence_ids=impact_signal.evidence_ids,
                )
            )

        has_audit = any("audit" in _text(e) or "audit" in e.source.lower() for e in events)
        if change_class == "credential" and not has_audit:
            hypotheses.append(
                Hypothesis(
                    title="Identity and mechanism of the credential change",
                    confidence=0.0,
                    status=ConclusionStatus.UNKNOWN,
                    explanation="No audit logs are present, so who or what changed the "
                    "credential cannot be established from this telemetry.",
                    evidence_ids=[],
                )
            )
        if change is not None and change.source_type == SourceType.LOG:
            hypotheses.append(
                Hypothesis(
                    title=f"Change record for the {change.source} change",
                    confidence=0.0,
                    status=ConclusionStatus.UNKNOWN,
                    explanation="The change appears only in service logs. No deployment or "
                    "change-management record was found to confirm what was altered.",
                    evidence_ids=[change.id],
                )
            )

        hypotheses.sort(key=lambda h: h.confidence, reverse=True)
        return hypotheses

    # ------------------------------------------------------------------ evidence gaps

    def _missing_evidence(
        self,
        events: list[TelemetryEvent],
        architecture: ArchitectureGraph,
        change: TelemetryEvent | None,
        change_class: str | None,
    ) -> list[str]:
        missing: list[str] = []
        sources = {e.source for e in events}
        silent = sorted(s.name for s in architecture.services if s.name not in sources)
        if silent:
            missing.append("No telemetry received from: " + ", ".join(silent))
        if change_class == "credential":
            if not any("audit" in _text(e) for e in events):
                missing.append("Database or IAM audit logs")
            if change is not None and "secret" in _text(change):
                missing.append("Secrets Manager version history")
        if change is None:
            missing.append("Deployment and change history for the affected services")
        if change is not None and change.source_type == SourceType.LOG:
            missing.append(f"Change-management record for the {change.source} change")
        if not any(e.source_type == SourceType.METRIC for e in events):
            missing.append("Metrics (only logs were supplied)")
        return missing

    # ------------------------------------------------------------------ actions

    def _actions(
        self,
        origin: _FailureSignal,
        change: TelemetryEvent | None,
        change_class: str | None,
        signals: _Signals,
        recovered: bool,
        model_policy_failure: bool,
    ) -> list[ActionItem]:
        actions: list[ActionItem] = []
        backlog = any(
            "backlog" in a.metric or "depth" in a.metric or "queue" in a.metric
            for a in signals.anomalies
        )

        if model_policy_failure:
            actions.append(
                ActionItem(
                    priority=1,
                    action=(
                        "Verify the gateway model allow-list for the active virtual key and "
                        f"compare it with the model ID configured on {origin.source}."
                    ),
                    reason=(
                        "The observed 403 identifies a model-policy rejection, but does not "
                        "distinguish an allow-list issue from a configured-model mismatch."
                    ),
                    risk="Read-only configuration verification. Low risk.",
                )
            )

        if recovered:
            actions.append(
                ActionItem(
                    priority=len(actions) + 1,
                    action=f"Confirm {origin.source} and user-facing metrics are stable "
                    "at baseline for at least 30 minutes.",
                    reason="Recovery signals are present but sustained stability is unverified.",
                    risk="Read-only verification. Low risk.",
                )
            )
            if change_class == "capacity":
                actions.append(
                    ActionItem(
                        priority=len(actions) + 1,
                        action=f"Add a warm-up step to the {origin.source} restart runbook "
                        "before it takes traffic.",
                        reason="A cold restart triggered the miss storm that caused the incident.",
                        risk="Process change only. Low risk.",
                    )
                )
                actions.append(
                    ActionItem(
                        priority=len(actions) + 1,
                        action="Add request coalescing or stale-while-revalidate so a cold "
                        "cache cannot stampede the backing store.",
                        reason="Downstream services absorbed the full miss load during the "
                        "incident.",
                        risk="Code change. Medium risk; ship behind a flag.",
                    )
                )
            actions.append(
                ActionItem(
                    priority=len(actions) + 1,
                    action="Write the postmortem while evidence links in this analysis are "
                    "fresh.",
                    reason="The reconstruction above lists confirmed facts, hypotheses and "
                    "evidence gaps to fill.",
                    risk="No production risk.",
                )
            )
            return actions

        if not model_policy_failure and change_class == "credential" and change is not None:
            actions.extend(
                [
                    ActionItem(
                        priority=1,
                        action=f"Compare the credential configured on {change.source} with "
                        "what the rejecting dependency expects (database, API gateway or "
                        "external service), including the auth scheme it wants the "
                        "credential sent under.",
                        reason="The failure began minutes after a credential change.",
                        risk="Read-only validation. Low risk.",
                    ),
                    ActionItem(
                        priority=2,
                        action=f"Run a read-only authentication test from the {origin.source} "
                        "environment with a restricted identity.",
                        reason="Confirms the credential mismatch before any rollback.",
                        risk="Low risk with a restricted test identity.",
                    ),
                    ActionItem(
                        priority=3,
                        action=f"Roll back {change.source} to the last known-good version, "
                        "after human approval.",
                        reason="The service was healthy before this change.",
                        risk="Medium risk. Validate compatibility first.",
                    ),
                ]
            )
        elif not model_policy_failure and change is not None:
            actions.extend(
                [
                    ActionItem(
                        priority=1,
                        action=f"Review the change on {change.source} "
                        f"({_short(change.detail, 60)}) against the first failure signature.",
                        reason="It is the closest change preceding the failure.",
                        risk="Read-only review. Low risk.",
                    ),
                    ActionItem(
                        priority=2,
                        action=f"Prepare a revert of the {change.source} change, pending "
                        "human approval.",
                        reason="Reverting the correlated change is the fastest probable fix.",
                        risk="Medium risk. Confirm the correlation first.",
                    ),
                ]
            )
        elif not model_policy_failure:
            actions.append(
                ActionItem(
                    priority=1,
                    action=f"Inspect {origin.source} directly: resource limits, dependency "
                    "health and recent alerts.",
                    reason="No correlated change was found, so start at the earliest failing "
                    "service.",
                    risk="Read-only investigation. Low risk.",
                )
            )

        if backlog:
            actions.append(
                ActionItem(
                    priority=len(actions) + 1,
                    action="After the root cause is fixed, drain accumulated queue backlog "
                    "gradually.",
                    reason="A sudden drain can recreate the saturation this incident caused.",
                    risk="Medium risk. Rate-limit and watch worker utilization.",
                )
            )
        return actions

    # ------------------------------------------------------------------ narrative

    def _impact_text(
        self, signals: _Signals, impact_signal: _FailureSignal | None
    ) -> str:
        if impact_signal is None:
            return "No confirmed customer-facing impact in the supplied telemetry."
        for anomaly in signals.anomalies:
            if (
                anomaly.source == impact_signal.source
                and not anomaly.recovered
                and anomaly.baseline is not None
            ):
                unit = str(anomaly.event.attributes.get("unit", "")).strip()
                return (
                    f"{anomaly.metric} on {anomaly.source} rose from {anomaly.baseline:g} "
                    f"to {anomaly.value:g} {unit}".rstrip() + "."
                )
        return _short(f"{impact_signal.source}: {impact_signal.summary}.", 160)

    def _title(
        self,
        architecture: ArchitectureGraph,
        origin: _FailureSignal,
        change: TelemetryEvent | None,
        impact_signal: _FailureSignal | None,
    ) -> str:
        symptom = (
            f"{impact_signal.source} failures" if impact_signal else f"{origin.source} failure"
        )
        if change is not None:
            kind = "deployment" if change.source_type == SourceType.DEPLOYMENT else "change"
            return f"{symptom} after {change.source} {kind} ({architecture.system})"
        return f"{symptom} ({architecture.system})"

    def _engineer_briefing(
        self,
        origin: _FailureSignal,
        change: TelemetryEvent | None,
        steps: list[PropagationStep],
        impact_text: str,
        recovered: bool,
        actions: list[ActionItem],
        timeline: list[TimelineEvent],
    ) -> str:
        parts: list[str] = []
        if change is not None:
            parts.append(
                f"At {change.timestamp:%H:%M} UTC, {change.source}: "
                f"{_short(change.detail, 90)}"
            )
        parts.append(
            f"First failure at {origin.timestamp:%H:%M} UTC on {origin.source}: "
            f"{_short(origin.summary, 100)}."
        )
        if steps:
            chain = " → ".join([origin.source] + [s.to_service for s in steps])
            parts.append(f"Propagation: {chain}.")
        parts.append(f"Impact: {impact_text}")
        if recovered:
            recovery = next((t for t in timeline if t.severity == Severity.RECOVERY), None)
            if recovery:
                parts.append(f"Recovery signals from {recovery.timestamp:%H:%M} UTC.")
        if actions:
            parts.append(f"First: {actions[0].action}")
        return " ".join(parts)

    def _executive_summary(
        self,
        architecture: ArchitectureGraph,
        change: TelemetryEvent | None,
        impact_text: str,
        recovered: bool,
    ) -> str:
        cause = (
            f"a change to {change.source}" if change is not None else "an unidentified trigger"
        )
        status = (
            "The system has shown recovery signals; verification is in progress."
            if recovered
            else "Mitigation steps are listed for engineering, pending human approval."
        )
        return (
            f"The {architecture.system} experienced a customer-affecting incident that "
            f"evidence links to {cause}. {impact_text} {status}"
        )

    def _replay(
        self,
        timeline: list[TimelineEvent],
        hypotheses: list[Hypothesis],
        actions: list[ActionItem],
    ) -> list[str]:
        lines = [
            f"At {item.timestamp:%H:%M} UTC — {item.title}. {item.description}"
            for item in timeline
        ]
        top = hypotheses[0] if hypotheses else None
        if top is not None:
            lines.append(
                f"Strongest conclusion ({top.status}, {round(top.confidence * 100)}% "
                f"confidence): {top.title}."
            )
        if actions:
            lines.append(f"First recommended check: {actions[0].action}")
        return lines

    # ------------------------------------------------------------------ integrity

    def _incident_id(self, system: str, origin: _FailureSignal, started_at: datetime) -> str:
        digest = hashlib.sha1(
            f"{system}:{origin.source}:{started_at.isoformat()}".encode()
        ).hexdigest()[:6]
        return f"INC-{started_at:%Y%m%d}-{digest}"

    def _validate_provenance(self, analysis: IncidentAnalysis) -> None:
        known = {e.id for e in analysis.evidence}
        referenced: set[str] = set()
        for item in analysis.timeline:
            referenced.update(item.evidence_ids)
        for hyp in analysis.hypotheses:
            referenced.update(hyp.evidence_ids)
        for step in analysis.propagation:
            referenced.update(step.evidence_ids)
        dangling = referenced - known
        if dangling:
            raise ValueError(f"Analysis references unknown evidence ids: {sorted(dangling)}")
