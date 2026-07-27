"""The incident-library read path.

These tests never touch the network: ``gallery`` is exercised against a stubbed
HTTP fetch, so the suite stays offline and deterministic while still covering the
catalog parsing, URL construction, caching and failure behaviour.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from incidentlens import gallery
from incidentlens.api import app

CATALOG = [
    {
        "incident_id": "INC-1",
        "prefix": "Hary_Part1-Gateway-Auth-Rejection",
        "title": "gateway rejects credentials",
        "failing_module": "hary.models.llm_factory",
        "failing_symbol": "hary.models.llm_factory.get_llm_for_tier",
        "blast_radius": 6,
        "evidence_count": 16,
        "missing_evidence_count": 3,
        "hypothesis_statuses": ["confirmed", "inferred", "unknown"],
    },
    {
        "incident_id": "INC-2",
        "prefix": "Other-Architectures/Cache-Stampede",
        "title": "cold cache floods the index tier",
        "failing_module": "search.cache.readthrough",
        "failing_symbol": "search.cache.readthrough.ReadThroughCache.get_or_backfill",
        "blast_radius": 3,
        "evidence_count": 26,
        "missing_evidence_count": 2,
        "hypothesis_statuses": ["confirmed", "inferred"],
    },
]

MANIFEST = {
    "canonical_hash": "abc123def456",
    "manifest_uri": "https://example.invalid/manifest.json",
    "run": {
        "steps": [
            {
                "provider": "openai-tts",
                "model": "gpt-4o-mini-tts",
                "modality": "audio",
                "step_type": "generate",
            }
        ]
        * 14
    },
}


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    gallery._cache.at = 0.0
    gallery._cache.entries = []


def _stub(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> list[str]:
    """Replace the HTTP fetch. Returns the list of URLs requested."""
    seen: list[str] = []

    def fake_get(url: str) -> bytes:
        seen.append(url)
        if fail:
            raise OSError("bucket unreachable")
        if url.endswith(gallery.CATALOG_FILE):
            return ("\n".join(json.dumps(e) for e in CATALOG) + "\n").encode()
        if url.endswith(".genblaze.json"):
            return json.dumps(MANIFEST).encode()
        raise OSError(f"unexpected url {url}")

    monkeypatch.setattr(gallery, "_get", fake_get)
    return seen


def test_catalog_is_read_from_object_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch)
    entries = gallery.fetch_catalog()
    assert len(entries) == 2
    assert seen[0].endswith("/incidents.jsonl")
    assert seen[0].startswith(gallery.public_base())


def test_media_urls_are_derived_from_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    entry = gallery.fetch_catalog()[0]
    base = gallery.public_base()
    assert entry["video_url"] == f"{base}/Hary_Part1-Gateway-Auth-Rejection/replay.mp4"
    assert entry["poster_url"] == f"{base}/Hary_Part1-Gateway-Auth-Rejection/poster.jpg"
    assert entry["files"]["briefing.md"].endswith("/briefing.md")


def test_catalog_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub(monkeypatch)
    gallery.fetch_catalog()
    gallery.fetch_catalog()
    assert len(seen) == 1, "second call should be served from cache"
    gallery.fetch_catalog(force=True)
    assert len(seen) == 2


def test_unreachable_bucket_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, fail=True)
    assert gallery.fetch_catalog() == []


def test_provenance_summarises_the_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    summary = gallery.fetch_provenance("Hary_Part1-Gateway-Auth-Rejection")
    assert summary is not None
    video = summary["video"]
    assert video["step_count"] == 14
    assert video["providers"] == ["openai-tts"]
    assert video["models"] == ["gpt-4o-mini-tts"]
    assert video["canonical_hash"] == "abc123def456"


def test_incidents_endpoint_lists_the_library(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    client = TestClient(app)
    body = client.get("/api/v1/incidents").json()
    assert body["count"] == 2
    assert body["source"] == gallery.public_base()
    assert body["incidents"][0]["prefix"] == "Hary_Part1-Gateway-Auth-Rejection"


def test_incident_endpoint_returns_the_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    client = TestClient(app)
    response = client.get("/api/v1/incidents/Other-Architectures/Cache-Stampede")
    assert response.status_code == 200
    body = response.json()
    assert body["incident"]["incident_id"] == "INC-2"
    assert set(body["files"]) == set(gallery.BUNDLE_FILES)
    assert body["files"]["replay.mp4"]["label"] == "Narrated replay"


def test_unknown_incident_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch)
    client = TestClient(app)
    assert client.get("/api/v1/incidents/Nope").status_code == 404
    assert client.get("/api/v1/incidents/Nope/provenance").status_code == 404


def test_gallery_page_is_served() -> None:
    client = TestClient(app)
    response = client.get("/gallery")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
