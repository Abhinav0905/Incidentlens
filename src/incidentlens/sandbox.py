"""Let a visitor try IncidentLens on telemetry that is theirs, not ours.

Two inputs, both ending in the same place — a real ``IncidentAnalysis`` produced by
the deterministic engine:

* **Paste log lines.** Parsed by the same ``logfile`` connector the live watcher
  uses. Needs no model, no key, and no network.
* **Describe an incident in a sentence.** A language model writes structured
  telemetry matching that description, and the engine reconstructs it.

The second path is labelled "synthesised" everywhere it surfaces. The model writes
the *input*; it never writes the conclusion. Every hypothesis, confidence and
citation still comes from the deterministic engine, which is the whole point — you
can invent an incident and watch the engine decline to overreach on it.

Because the endpoint is public and spends money, it is rate limited per IP, capped
globally per day, and fails closed when unconfigured.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_PROMPT_CHARS = 500
MAX_LOG_CHARS = 20_000
MAX_LOG_LINES = 400
SYNTH_MODEL = os.environ.get("INCIDENTLENS_SANDBOX_MODEL", "gpt-4o-mini")
SYNTH_MAX_TOKENS = 2600


class SandboxError(RuntimeError):
    """A visitor-facing problem: bad input, disabled feature, or a spend cap."""


class SandboxDisabled(SandboxError):
    """No model credential is configured."""


class RateLimited(SandboxError):
    """Per-IP or global cap reached."""


# ----------------------------------------------------------------- spend guards


@dataclass
class _Limits:
    per_ip_per_hour: int
    daily: int


def _limits() -> _Limits:
    return _Limits(
        per_ip_per_hour=int(os.environ.get("INCIDENTLENS_SANDBOX_PER_IP_PER_HOUR", "10")),
        daily=int(os.environ.get("INCIDENTLENS_SANDBOX_DAILY_LIMIT", "200")),
    )


_by_ip: dict[str, deque[float]] = defaultdict(deque)
_today: list[Any] = [None, 0]  # [date, count]


def check_quota(client: str) -> None:
    """Raise ``RateLimited`` when this caller, or the day, has had enough."""
    limits = _limits()
    now = time.monotonic()

    hits = _by_ip[client]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= limits.per_ip_per_hour:
        raise RateLimited(
            f"This sandbox allows {limits.per_ip_per_hour} generated scenarios per hour. "
            "Pasting your own log lines has no limit."
        )

    day = datetime.now(UTC).date()
    if _today[0] != day:
        _today[0], _today[1] = day, 0
    if _today[1] >= limits.daily:
        raise RateLimited(
            "The shared daily budget for generated scenarios is spent. "
            "Pasting your own log lines still works."
        )

    hits.append(now)
    _today[1] += 1


def quota_state() -> dict[str, int]:
    limits = _limits()
    day = datetime.now(UTC).date()
    used = _today[1] if _today[0] == day else 0
    return {
        "daily_limit": limits.daily,
        "daily_used": used,
        "daily_remaining": max(0, limits.daily - used),
        "per_ip_per_hour": limits.per_ip_per_hour,
    }


def _client_available() -> bool:
    """Is the model client installed? (``pip install '.[sandbox]'``)"""
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def enabled() -> bool:
    """Generation is only usable with both a credential and the client library."""
    return bool(os.environ.get("OPENAI_API_KEY")) and _client_available()


# ------------------------------------------------------------- paste-your-logs


_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|CRITICAL|FATAL)\b")


def looks_like_logs(text: str) -> bool:
    """Cheap heuristic: does this look like log lines rather than prose?"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    with_level = sum(1 for ln in lines if _LEVEL.search(ln))
    return with_level >= max(1, len(lines) // 4)


def parse_log_text(text: str, *, service: str = "service") -> list[Any]:
    """Turn pasted log text into telemetry events, via the live connector."""
    from incidentlens.connectors.logfile import LogFileConnector, LogSource

    if len(text) > MAX_LOG_CHARS:
        raise SandboxError(f"Log text is limited to {MAX_LOG_CHARS:,} characters.")
    lines = [ln for ln in text.splitlines() if ln.strip()][:MAX_LOG_LINES]
    if not lines:
        raise SandboxError("No log lines found.")

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="incidentlens-sandbox-") as tmp:
        path = Path(tmp) / f"{service}.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        from incidentlens.domain.models import ArchitectureGraph, ServiceNode

        architecture = ArchitectureGraph(
            system="pasted-logs",
            services=[ServiceNode(name=service, user_facing=True)],
        )
        source = LogSource(service=service, pattern=str(path))
        return LogFileConnector([source], architecture).fetch_events()


# ------------------------------------------------------------ synthesise-a-scenario


_SYSTEM = """You generate SYNTHETIC production telemetry for a demo of an incident \
reconstruction tool. You do not diagnose anything — you only write the raw signals a \
real system would have emitted.

Return ONLY a JSON object:
{
  "system": "<short platform name, kebab-case>",
  "services": [
    {"name": "<kebab-case>", "depends_on": ["<name>", ...], "user_facing": true|false,
     "owner": "<team>"}
  ],
  "events": [
    {"id": "log-001", "source_type": "log", "source": "<service name>",
     "timestamp": "2026-07-27T09:12:04Z",
     "detail": "<a realistic single log line>",
     "attributes": {"level": "INFO|WARN|ERROR", "logger": "<module.path>"}}
  ]
}

Rules:
- 5 to 7 services with a believable dependency chain; exactly one user_facing.
- 14 to 18 events, ordered in time, spanning about 12 minutes.
- "source_type" must be exactly one of: "log", "metric", "deployment", "architecture".
  No other value is accepted.
- Exactly one service is the origin of the failure. At least two of its events must
  have attributes.level "ERROR", and their "logger" must be a plausible dotted module
  path inside that service.
- Include one event with "source_type": "deployment" on the origin service, a minute
  or two before the first error, describing the deploy or config change that caused
  it. This is what lets the analysis attribute a root cause instead of reporting an
  unexplained failure, so it is not optional.
- Metrics must be machine-readable, not prose. Emit at least ONE metric as TWO
  events on the same service with the same metric name — a baseline and a degraded
  reading — shaped like this:
    {"id": "metric-001", "source_type": "metric", "source": "<service>",
     "timestamp": "...", "detail": "consumer_lag_seconds = 2",
     "attributes": {"metric": "consumer_lag_seconds", "value": 2}}
    {"id": "metric-002", "source_type": "metric", "source": "<service>",
     "timestamp": "...", "detail": "consumer_lag_seconds = 91",
     "attributes": {"metric": "consumer_lag_seconds", "value": 91}}
  "value" must be a NUMBER, not a string. The degraded reading must be at least
  three times the baseline AND at least 5 higher, or the change is treated as noise.
- Include a user-visible symptom on the user_facing service.
- Leave one dependency completely silent — no events at all — so the analysis has a
  genuine gap to report.
- detail strings must read like real production log lines: error classes, codes,
  durations, counts, ids. Never placeholders.
- Every "source" must be one of the service names you declared.
"""


def synthesise_events(prompt: str) -> tuple[Any, list[Any]]:
    """Ask a model for structured telemetry matching ``prompt``.

    Returns ``(architecture, events)``. Raises ``SandboxError`` on anything the
    caller should see as a message rather than a stack trace.
    """
    prompt = prompt.strip()
    if not prompt:
        raise SandboxError("Describe the incident you want to see reconstructed.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SandboxError(f"Keep the description under {MAX_PROMPT_CHARS} characters.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SandboxDisabled(
            "Scenario generation is not configured on this deployment. "
            "Paste your own log lines instead — that path needs no model."
        )
    if not _client_available():
        raise SandboxDisabled(
            "This deployment has a credential but not the model client "
            "(pip install '.[sandbox]'). Paste your own log lines instead."
        )

    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"The incident to generate telemetry for: {prompt}"},
    ]

    # One repair round. Small models routinely omit the change event or the
    # ERROR levels, and without those the engine can only report "unexplained
    # failure" — technically honest, but a poor demonstration of what it can do.
    for attempt in (1, 2):
        try:
            completion = client.chat.completions.create(
                model=SYNTH_MODEL,
                max_tokens=SYNTH_MAX_TOKENS,
                temperature=0.7,
                response_format={"type": "json_object"},
                messages=messages,  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001 - surface provider errors as messages
            raise SandboxError(f"The model call failed: {type(exc).__name__}") from exc

        content = (completion.choices[0].message.content or "").strip()
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            if attempt == 2:
                raise SandboxError(
                    "The model did not return usable JSON. Try rephrasing."
                ) from exc
            messages.append(
                {
                    "role": "user",
                    "content": "That was not valid JSON. Return only the JSON object.",
                }
            )
            continue

        missing = _missing_requirements(payload)
        if not missing or attempt == 2:
            return _to_domain(payload)
        messages += [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (
                    "That telemetry is incomplete: " + "; ".join(missing) + ". "
                    "Return the corrected JSON object, keeping everything that was fine."
                ),
            },
        ]

    raise SandboxError("Could not generate usable telemetry. Try rephrasing.")


def _missing_requirements(payload: dict[str, Any]) -> list[str]:
    """What the model left out that the engine needs to reason well."""
    events = payload.get("events") or []
    problems: list[str] = []

    def level(event: dict[str, Any]) -> str:
        return str((event.get("attributes") or {}).get("level", "")).upper()

    errors = [e for e in events if level(e) in {"ERROR", "CRITICAL", "FATAL"}]
    if len(errors) < 2:
        problems.append("it needs at least two ERROR-level events on the origin service")
    if not any(e.get("source_type") == "deployment" for e in events):
        problems.append(
            'it needs one event with "source_type": "deployment" describing the deploy '
            'or config change that preceded the failure (the value must be exactly '
            '"deployment" — "change" is not an accepted source_type)'
        )
    if not any((e.get("attributes") or {}).get("logger") for e in errors):
        problems.append(
            'each ERROR event needs an attributes.logger holding a dotted module path, '
            "so the failure can be attributed to a module"
        )
    if not _has_comparable_metric(events):
        problems.append(
            "it needs a metric emitted TWICE on the same service with the same "
            'attributes.metric name and a NUMERIC attributes.value — a baseline and a '
            "degraded reading at least 3x higher and at least 5 greater — otherwise no "
            "anomaly can be detected"
        )
    if len(events) < 12:
        problems.append(f"it has only {len(events)} events; produce 14 to 18")
    return problems


def _has_comparable_metric(events: list[dict[str, Any]]) -> bool:
    """Is there a metric series the engine could actually call anomalous?

    Mirrors ``_detect_metric_anomalies``: values are grouped by (source, metric name)
    and a rise only counts at >=3x the baseline and >=5 absolute, so a single reading
    or a prose description can never trip it.
    """
    series: dict[tuple[str, str], list[float]] = {}
    for event in events:
        if event.get("source_type") != "metric":
            continue
        attributes = event.get("attributes") or {}
        value = attributes.get("value")
        if not isinstance(value, int | float) or isinstance(value, bool):
            continue
        name = attributes.get("metric") or str(event.get("detail", ""))[:40]
        series.setdefault((str(event.get("source")), str(name)), []).append(float(value))

    for values in series.values():
        if len(values) < 2:
            continue
        baseline = values[0]
        for value in values[1:]:
            rose = (value >= baseline * 3 and value - baseline >= 5) or (
                baseline < 1 and value >= 5
            )
            fell = baseline >= 5 and value <= baseline / 3
            if rose or fell:
                return True
    return False


def _to_domain(payload: dict[str, Any]) -> tuple[Any, list[Any]]:
    """Validate the model's JSON into real domain objects.

    Anything malformed is dropped rather than trusted: unknown services, bad
    timestamps and unparseable events cannot reach the engine.
    """
    from incidentlens.domain.models import ArchitectureGraph, ServiceNode, TelemetryEvent

    raw_services = payload.get("services") or []
    if not raw_services:
        raise SandboxError("The model returned no services.")
    names = {s.get("name") for s in raw_services if s.get("name")}

    services = [
        ServiceNode(
            name=s["name"],
            depends_on=[d for d in (s.get("depends_on") or []) if d in names],
            user_facing=bool(s.get("user_facing")),
            owner=s.get("owner") or None,
        )
        for s in raw_services
        if s.get("name")
    ]
    architecture = ArchitectureGraph(
        system=str(payload.get("system") or "synthesised-platform"),
        services=services,
    )

    events: list[Any] = []
    dropped: list[str] = []
    base = datetime.now(UTC) - timedelta(minutes=20)
    for index, raw in enumerate(payload.get("events") or []):
        ident = str(raw.get("id") or f"synth-{index:03d}")
        if raw.get("source") not in names:
            dropped.append(f"{ident}: source {raw.get('source')!r} is not a declared service")
            continue
        stamp = raw.get("timestamp")
        when: datetime
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            when = base + timedelta(seconds=45 * index)
        attributes = raw.get("attributes")
        try:
            events.append(
                TelemetryEvent(
                    id=ident,
                    source_type=str(raw.get("source_type") or "log"),
                    source=str(raw["source"]),
                    timestamp=when,
                    detail=str(raw.get("detail") or "").strip()[:600],
                    attributes=attributes if isinstance(attributes, dict) else {},
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            # Never silent. An invalid source_type once discarded every deployment
            # event here, and because the drop was silent the only visible symptom
            # was the engine reporting "unexplained failure" on every scenario.
            reason = str(exc).splitlines()[0][:120]
            dropped.append(f"{ident}: {reason}")
            continue

    if dropped:
        _last_dropped[:] = dropped
    else:
        _last_dropped.clear()

    if len(events) < 4:
        detail = "; ".join(dropped[:4]) if dropped else "no events were returned"
        raise SandboxError(
            f"The model returned too few usable events ({len(events)}). {detail}"
        )
    return architecture, events


# Diagnostics for the most recent synthesis, surfaced in the API response so a
# malformed-event drop can never again be invisible.
_last_dropped: list[str] = []


def last_dropped() -> list[str]:
    return list(_last_dropped)


__all__ = [
    "MAX_LOG_CHARS",
    "MAX_PROMPT_CHARS",
    "RateLimited",
    "SandboxDisabled",
    "SandboxError",
    "check_quota",
    "last_dropped",
    "enabled",
    "looks_like_logs",
    "parse_log_text",
    "quota_state",
    "synthesise_events",
]
