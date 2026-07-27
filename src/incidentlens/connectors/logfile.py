"""Real log files in, canonical telemetry out.

This is the connector the live workflow uses: point it at the log files your
services write (or the files you redirect their stdout into) and it turns each
line into a ``TelemetryEvent`` the analysis engine understands. No agents, no
sidecars — a file path is the integration surface.

Formats recognised per line, tried in order:

* JSON lines — ``{"timestamp": ..., "level": ..., "message": ...}`` and the
  usual aliases (``time``/``ts``/``asctime``, ``levelname``/``severity``,
  ``msg``/``event``, ``logger``/``name``).
* Python ``logging`` default — ``2026-07-17 10:23:45,123 - pkg.module - ERROR - msg``
  (this is exactly what a stock ``logging.basicConfig`` emits, including the
  Hary microservices).
* Java / Spring Boot — ``2026-07-17 10:23:45.123 ERROR 1234 --- [thread] c.a.Cls : msg``
* Bare ISO prefix — ``2026-07-17T10:23:45Z ERROR msg`` and bracketed variants.
* Level-only lines — ``ERROR: msg`` / ``[error] msg`` (uvicorn style); these
  inherit the last seen timestamp in the same file.

Lines that match nothing are kept as INFO detail attached to the previous
timestamp, so multi-line tracebacks stay with their error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from incidentlens.connectors.base import TelemetryConnector
from incidentlens.domain.models import ArchitectureGraph, SourceType, TelemetryEvent

UTC = timezone.utc

_LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"}

_PY_LOGGING = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"
    r"\s*-\s*(?P<logger>[\w.\-]+)\s*-\s*(?P<level>[A-Za-z]+)\s*-\s*(?P<msg>.*)$"
)
_SPRING = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"
    r"\s+(?P<level>[A-Z]+)\s+\d+\s+---\s+\[(?P<thread>[^\]]*)\]\s+(?P<logger>\S+)\s*:\s*(?P<msg>.*)$"
)
_ISO_PREFIX = re.compile(
    r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)\]?"
    r"\s+\[?(?P<level>[A-Za-z]+)\]?[: ]\s*(?P<msg>.*)$"
)
_LEVEL_ONLY = re.compile(r"^\[?(?P<level>[A-Za-z]+)\]?[:\-]\s+(?P<msg>.*)$")

_TS_KEYS = ("timestamp", "time", "ts", "asctime", "@timestamp", "datetime")
_LEVEL_KEYS = ("level", "levelname", "severity", "loglevel")
_MSG_KEYS = ("message", "msg", "event", "detail", "text")
_LOGGER_KEYS = ("logger", "name", "logger_name", "module")


def _parse_ts(raw: str) -> datetime | None:
    raw = raw.strip().replace(",", ".")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    for candidate in (raw, raw.replace(" ", "T")):
        try:
            ts = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts
    return None


@dataclass
class ParsedLine:
    timestamp: datetime | None
    level: str
    message: str
    logger: str = ""


def parse_line(line: str) -> ParsedLine | None:
    """Best-effort structure for one raw log line. None for blank lines."""
    line = line.rstrip("\n")
    if not line.strip():
        return None

    stripped = line.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            ts_raw = next((str(obj[k]) for k in _TS_KEYS if obj.get(k) is not None), "")
            level = next((str(obj[k]) for k in _LEVEL_KEYS if obj.get(k)), "INFO")
            msg = next((str(obj[k]) for k in _MSG_KEYS if obj.get(k)), stripped)
            logger = next((str(obj[k]) for k in _LOGGER_KEYS if obj.get(k)), "")
            return ParsedLine(_parse_ts(ts_raw), level.upper(), msg, logger)

    for pattern in (_PY_LOGGING, _SPRING, _ISO_PREFIX):
        match = pattern.match(line)
        if match:
            groups = match.groupdict()
            level = groups.get("level", "INFO").upper()
            if level not in _LEVELS:
                continue
            return ParsedLine(
                _parse_ts(groups["ts"]),
                level,
                groups.get("msg", "").strip(),
                groups.get("logger", "") or "",
            )

    match = _LEVEL_ONLY.match(line)
    if match and match.group("level").upper() in _LEVELS:
        return ParsedLine(None, match.group("level").upper(), match.group("msg").strip())

    return ParsedLine(None, "INFO", stripped)


@dataclass
class LogSource:
    """One service's log input: a path or glob, tailed incrementally."""

    service: str
    pattern: str  # file path or glob, relative to root or absolute
    root: Path = field(default_factory=Path.cwd)
    _offsets: dict[Path, int] = field(default_factory=dict)
    _last_ts: datetime | None = None
    _counter: int = 0

    def files(self) -> list[Path]:
        raw = Path(self.pattern)
        if raw.is_absolute():
            if any(ch in self.pattern for ch in "*?["):
                base = Path(raw.anchor)
                return sorted(p for p in base.glob(str(raw.relative_to(base))) if p.is_file())
            return [raw] if raw.is_file() else []
        matches = sorted(p for p in self.root.glob(self.pattern) if p.is_file())
        return matches

    def read_new(self) -> list[TelemetryEvent]:
        """Events for lines appended since the previous call (all lines, first call)."""
        events: list[TelemetryEvent] = []
        for path in self.files():
            offset = self._offsets.get(path, 0)
            try:
                size = path.stat().st_size
                if size < offset:  # rotated / truncated
                    offset = 0
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    self._offsets[path] = fh.tell()
            except OSError:
                continue
            for line in chunk.splitlines():
                parsed = parse_line(line)
                if parsed is None:
                    continue
                ts = parsed.timestamp or self._last_ts or datetime.fromtimestamp(
                    path.stat().st_mtime, tz=UTC
                )
                self._last_ts = ts
                self._counter += 1
                detail = parsed.message
                if parsed.logger:
                    detail = f"{parsed.message} [{parsed.logger}]"
                events.append(
                    TelemetryEvent(
                        id=f"{self.service}-log-{self._counter:05d}",
                        source_type=SourceType.LOG,
                        source=self.service,
                        timestamp=ts,
                        detail=detail,
                        attributes={
                            "level": parsed.level,
                            "file": str(path),
                            "logger": parsed.logger,
                        },
                    )
                )
        return events


class LogFileConnector(TelemetryConnector):
    """TelemetryConnector over a set of LogSources plus an architecture graph."""

    def __init__(self, sources: list[LogSource], architecture: ArchitectureGraph) -> None:
        self.sources = sources
        self._architecture = architecture

    def fetch_events(self) -> list[TelemetryEvent]:
        events: list[TelemetryEvent] = []
        for source in self.sources:
            events.extend(source.read_new())
        events.sort(key=lambda e: e.timestamp)
        return events

    def fetch_architecture(self) -> ArchitectureGraph:
        return self._architecture
