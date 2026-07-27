"""Read the published incident library back out of Backblaze B2.

The write side lives in ``studio.library`` and runs offline, where the renderer
and the Genblaze SDK are installed. This is the read side, and it is deliberately
dependency-free: the bucket is public, so the catalog and every bundle file are
plain HTTPS GETs over the standard library. The hosted service therefore needs no
boto3, no Genblaze, and — importantly — **no B2 credentials**.

That is what makes B2 the library the application reads from rather than a place
it once wrote to.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

CATALOG_FILE = "incidents.jsonl"
DEFAULT_BASE = "https://s3.us-east-005.backblazeb2.com/Hackproject"
CACHE_SECONDS = 120.0
TIMEOUT = 8.0

# Files a bundle may contain, and how to label them in the UI.
BUNDLE_FILES = {
    "replay.mp4": "Narrated replay",
    "poster.jpg": "Poster frame",
    "analysis.json": "Full analysis document",
    "briefing.md": "Engineer briefing",
    "code-graph.mmd": "Mermaid call graph",
    "provenance.genblaze.json": "Genblaze manifest (video)",
    "narration.genblaze.json": "Genblaze manifest (narration)",
}


def public_base() -> str:
    """Public URL prefix of the B2 bucket, without a trailing slash."""
    return os.environ.get("INCIDENTLENS_B2_PUBLIC_BASE", DEFAULT_BASE).rstrip("/")


@dataclass
class _Cache:
    at: float = 0.0
    entries: list[dict[str, Any]] = field(default_factory=list)


_cache = _Cache()


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "IncidentLens"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        data: bytes = response.read()
    return data


def _url(*parts: str) -> str:
    return "/".join([public_base(), *(p.strip("/") for p in parts)])


def fetch_catalog(*, force: bool = False) -> list[dict[str, Any]]:
    """The incident catalog, read from B2 and cached briefly.

    Returns an empty list when the bucket is unreachable — the gallery degrades
    to "no incidents published yet" instead of erroring.
    """
    now = time.monotonic()
    if not force and _cache.entries and (now - _cache.at) < CACHE_SECONDS:
        return _cache.entries
    try:
        raw = _get(_url(CATALOG_FILE)).decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError):
        return _cache.entries
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    for entry in entries:
        prefix = entry.get("prefix", "")
        entry["poster_url"] = _url(prefix, "poster.jpg")
        entry["video_url"] = _url(prefix, "replay.mp4")
        entry["files"] = {
            name: _url(prefix, name) for name in BUNDLE_FILES if name in BUNDLE_FILES
        }
    _cache.at, _cache.entries = now, entries
    return entries


def find_incident(prefix: str) -> dict[str, Any] | None:
    for entry in fetch_catalog():
        if entry.get("prefix") == prefix:
            return entry
    return None


def fetch_provenance(prefix: str) -> dict[str, Any] | None:
    """The Genblaze manifests for one incident, summarised for display.

    Pulls both manifests straight from B2 and reduces them to the facts worth
    showing: which provider and model produced the narration, how many steps,
    and the canonical hash that binds the media to the analysis.
    """
    summary: dict[str, Any] = {}
    for name, label in (
        ("provenance.genblaze.json", "video"),
        ("narration.genblaze.json", "narration"),
    ):
        try:
            manifest = json.loads(_get(_url(prefix, name)).decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        run = manifest.get("run") or {}
        steps = run.get("steps") or []
        summary[label] = {
            "canonical_hash": manifest.get("canonical_hash"),
            "manifest_uri": manifest.get("manifest_uri"),
            "manifest_url": _url(prefix, name),
            "step_count": len(steps),
            "providers": sorted({s["provider"] for s in steps if s.get("provider")}),
            "models": sorted({s["model"] for s in steps if s.get("model")}),
            "modalities": sorted({s["modality"] for s in steps if s.get("modality")}),
            "step_types": sorted({s["step_type"] for s in steps if s.get("step_type")}),
        }
    return summary or None


__all__ = [
    "BUNDLE_FILES",
    "fetch_catalog",
    "fetch_provenance",
    "find_incident",
    "public_base",
]
