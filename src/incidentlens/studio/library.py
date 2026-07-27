"""The incident library on Backblaze B2.

Genblaze writes the *canonical* record of a run — content-addressed assets under
``runs/{tenant}/{date}/{run-id}/`` with a verifying manifest. That layout is correct
for provenance and useless for browsing: every key is a UUID.

This module writes the *human* layer beside it. One folder per incident, named for
the failure, holding everything a responder or reviewer would want:

    Hary_Part1-Gateway-Auth-Rejection/
        replay.mp4                  the narrated reconstruction
        poster.jpg                  first frame, for gallery thumbnails
        analysis.json               the full IncidentAnalysis document
        briefing.md                 what an on-call engineer reads
        code-graph.mmd              Mermaid of the failing service
        provenance.genblaze.json    Genblaze manifest for the video
        narration.genblaze.json     Genblaze manifest for the narration steps

Every object also carries queryable metadata (incident id, origin service, failing
module and symbol, leading confidence), so the gallery can build a listing from
``head()`` calls without downloading any bodies. A JSONL catalog at
``incidents.jsonl`` is the index the hosted app reads.

Everything goes through Genblaze's own ``S3StorageBackend`` — there is no direct
boto3 path in this project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CATALOG_KEY = "incidents.jsonl"


@dataclass(frozen=True)
class BundleFile:
    """One object in an incident bundle."""

    name: str
    data: bytes
    content_type: str


@dataclass
class PublishedBundle:
    prefix: str
    url: str
    keys: list[str] = field(default_factory=list)
    catalog_entry: dict[str, Any] = field(default_factory=dict)


# Words that must not be title-cased in a bundle name — a judge reads these.
ACRONYMS = {"pii", "llm", "api", "db", "http", "https", "ai", "sre", "cpu", "io", "ui"}


def _slug(text: str) -> str:
    """'pii-guardrail-crash' -> 'PII-Guardrail-Crash'."""
    parts = [p for p in text.replace("_", "-").split("-") if p]
    return "-".join(p.upper() if p.lower() in ACRONYMS else p.capitalize() for p in parts)


def bundle_prefix(scenario: str, *, series: str | None = None, part: int | None = None) -> str:
    """Human-readable B2 prefix for one incident.

    ``bundle_prefix('gateway-auth-rejection', series='Hary', part=1)``
    -> ``'Hary_Part1-Gateway-Auth-Rejection'``
    """
    name = _slug(scenario)
    if series and part:
        return f"{series}_Part{part}-{name}"
    if series:
        return f"{series}-{name}"
    return name


def _object_metadata(analysis: Any) -> dict[str, str]:
    """Small, queryable facts attached to every object in the bundle.

    S3 user metadata must be ASCII header-safe, so values are stringified and
    anything absent is omitted rather than sent as "None".
    """
    trace = analysis.internal_trace
    confidence = max((h.confidence for h in analysis.hypotheses), default=0.0)
    raw = {
        "incident-id": analysis.incident_id,
        "origin-service": getattr(trace, "service", None) if trace else None,
        "failing-module": getattr(trace, "failing_module", None) if trace else None,
        "failing-symbol": getattr(trace, "failing_symbol", None) if trace else None,
        "leading-confidence": f"{confidence:.2f}",
        "evidence-count": str(len(analysis.evidence)),
    }
    return {k: str(v) for k, v in raw.items() if v is not None}


def build_bundle(
    video: str | Path,
    analysis: Any,
    *,
    poster: str | Path | None = None,
    briefing: str | None = None,
    mermaid: str | None = None,
    provenance: str | Path | None = None,
    narration_provenance: str | Path | None = None,
) -> list[BundleFile]:
    """Assemble the files for one incident bundle. Missing pieces are skipped."""
    video = Path(video)
    files = [
        BundleFile("replay.mp4", video.read_bytes(), "video/mp4"),
        BundleFile(
            "analysis.json",
            json.dumps(analysis.model_dump(mode="json"), indent=2).encode("utf-8"),
            "application/json",
        ),
    ]
    if poster and Path(poster).is_file():
        files.append(BundleFile("poster.jpg", Path(poster).read_bytes(), "image/jpeg"))
    if briefing:
        files.append(BundleFile("briefing.md", briefing.encode("utf-8"), "text/markdown"))
    if mermaid:
        files.append(BundleFile("code-graph.mmd", mermaid.encode("utf-8"), "text/plain"))
    for name, path in (
        ("provenance.genblaze.json", provenance),
        ("narration.genblaze.json", narration_provenance),
    ):
        if path and Path(path).is_file():
            files.append(
                BundleFile(name, Path(path).read_bytes(), "application/json")
            )
    return files


def publish_bundle(
    backend: Any,
    prefix: str,
    files: list[BundleFile],
    analysis: Any,
    *,
    title: str = "",
    description: str = "",
) -> PublishedBundle:
    """Upload one incident bundle under a readable prefix.

    ``backend`` is a Genblaze ``StorageBackend`` (see ``genblaze_s3``).
    """
    metadata = _object_metadata(analysis)
    keys: list[str] = []
    for item in files:
        key = f"{prefix}/{item.name}"
        backend.put(
            key,
            item.data,
            content_type=item.content_type,
            metadata=metadata,
        )
        keys.append(key)

    video_key = f"{prefix}/replay.mp4"
    url = backend.get_durable_url(video_key)
    trace = analysis.internal_trace
    entry = {
        "incident_id": analysis.incident_id,
        "prefix": prefix,
        "title": title or analysis.title,
        "description": description,
        "started_at": analysis.started_at.isoformat(),
        "origin_service": getattr(trace, "service", None) if trace else None,
        "failing_module": getattr(trace, "failing_module", None) if trace else None,
        "failing_symbol": getattr(trace, "failing_symbol", None) if trace else None,
        "blast_radius": getattr(trace, "blast_radius", None) if trace else None,
        "evidence_count": len(analysis.evidence),
        "leading_confidence": max(
            (h.confidence for h in analysis.hypotheses), default=0.0
        ),
        "hypothesis_statuses": [h.status.value for h in analysis.hypotheses],
        "missing_evidence_count": len(analysis.missing_evidence),
        "video_url": url,
        "keys": keys,
    }
    return PublishedBundle(prefix=prefix, url=url, keys=keys, catalog_entry=entry)


def write_catalog(backend: Any, entries: list[dict[str, Any]]) -> str:
    """Write the JSONL catalog the hosted gallery reads. Returns its URL."""
    body = "\n".join(json.dumps(e) for e in entries) + "\n"
    backend.put(
        CATALOG_KEY,
        body.encode("utf-8"),
        content_type="application/x-ndjson",
        metadata={"incident-count": str(len(entries))},
    )
    return backend.get_durable_url(CATALOG_KEY)


def read_catalog(backend: Any) -> list[dict[str, Any]]:
    """Read the catalog back out of B2 — the gallery's listing call."""
    try:
        raw = backend.get(CATALOG_KEY).decode("utf-8")
    except Exception:  # noqa: BLE001 - absent or unreadable catalog is not fatal
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


__all__ = [
    "CATALOG_KEY",
    "BundleFile",
    "PublishedBundle",
    "build_bundle",
    "bundle_prefix",
    "publish_bundle",
    "read_catalog",
    "write_catalog",
]
