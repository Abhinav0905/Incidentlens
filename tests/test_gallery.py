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


# ---------------------------------------------------------------------- sandbox


def test_paste_path_needs_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real log lines are reconstructed with no credential present."""
    from incidentlens import sandbox

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(app)
    logs = "\n".join(
        [
            "2026-07-27 09:12:01.114 INFO  [main] payments.api.routes : "
            "POST /v1/charge 200 in 84 ms",
            "2026-07-27 09:13:02.410 ERROR [pool-2] payments.db.credentials : "
            "InvalidPasswordError: password authentication failed",
            "2026-07-27 09:13:05.001 ERROR [main] payments.api.routes : "
            "POST /v1/charge 502 in 3011 ms",
        ]
    )
    body = client.post(
        "/api/v1/sandbox/reconstruct", json={"logs": logs, "service": "payment-api"}
    ).json()
    assert body["synthesised"] is False
    assert "No model was involved" in body["disclosure"]
    assert body["analysis"]["hypotheses"]
    assert not sandbox.enabled()


def test_generation_is_disabled_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(app)
    response = client.post("/api/v1/sandbox/reconstruct", json={"prompt": "redis died"})
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_telemetry_without_failure_is_refused_not_invented() -> None:
    """Healthy logs must yield 422, never a fabricated incident."""
    client = TestClient(app)
    logs = "\n".join(
        [
            "2026-07-27 09:12:01 INFO app.routes : GET /health 200 in 3 ms",
            "2026-07-27 09:12:02 INFO app.routes : GET /health 200 in 2 ms",
        ]
    )
    response = client.post("/api/v1/sandbox/reconstruct", json={"logs": logs})
    assert response.status_code == 422
    assert "honest answer" in response.json()["detail"]


def test_per_ip_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from incidentlens import sandbox

    monkeypatch.setenv("INCIDENTLENS_SANDBOX_PER_IP_PER_HOUR", "2")
    sandbox._by_ip.clear()
    sandbox._today[0] = None
    sandbox.check_quota("1.2.3.4")
    sandbox.check_quota("1.2.3.4")
    with pytest.raises(sandbox.RateLimited):
        sandbox.check_quota("1.2.3.4")
    sandbox.check_quota("5.6.7.8")  # a different caller is unaffected


def test_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from incidentlens import sandbox

    monkeypatch.setenv("INCIDENTLENS_SANDBOX_PER_IP_PER_HOUR", "99")
    monkeypatch.setenv("INCIDENTLENS_SANDBOX_DAILY_LIMIT", "3")
    sandbox._by_ip.clear()
    sandbox._today[0] = None
    for i in range(3):
        sandbox.check_quota(f"ip-{i}")
    with pytest.raises(sandbox.RateLimited):
        sandbox.check_quota("ip-fresh")


def test_prompt_length_is_capped() -> None:
    from incidentlens import sandbox

    with pytest.raises(sandbox.SandboxError):
        sandbox.synthesise_events("x" * (sandbox.MAX_PROMPT_CHARS + 1))


def test_model_output_is_validated_not_trusted() -> None:
    """Unknown services and malformed events are dropped before the engine."""
    from incidentlens import sandbox

    architecture, events = sandbox._to_domain(
        {
            "system": "demo",
            "services": [
                {"name": "web", "depends_on": ["api", "ghost"], "user_facing": True},
                {"name": "api", "depends_on": []},
            ],
            "events": [
                {"id": "a", "source": "web", "timestamp": "2026-07-27T09:00:00Z",
                 "detail": "ok", "attributes": {"level": "INFO"}},
                {"id": "b", "source": "api", "timestamp": "not-a-date",
                 "detail": "bad clock", "attributes": {"level": "ERROR"}},
                {"id": "c", "source": "does-not-exist", "detail": "dropped"},
                {"id": "d", "source": "api", "timestamp": "2026-07-27T09:02:00Z",
                 "detail": "boom", "attributes": "not-a-dict"},
                {"id": "e", "source": "web", "timestamp": "2026-07-27T09:03:00Z", "detail": "x"},
            ],
        }
    )
    names = {s.name for s in architecture.services}
    assert names == {"web", "api"}
    web = next(s for s in architecture.services if s.name == "web")
    assert "ghost" not in web.depends_on, "dependency on an undeclared service must be dropped"
    assert {e.id for e in events} == {"a", "b", "d", "e"}, "unknown-source event must be dropped"
    assert next(e for e in events if e.id == "d").attributes == {}


def test_missing_requirements_flags_weak_telemetry() -> None:
    from incidentlens import sandbox

    weak = {"events": [{"source_type": "log", "attributes": {"level": "INFO"}}]}
    problems = sandbox._missing_requirements(weak)
    assert any("ERROR" in p for p in problems)
    assert any("change" in p for p in problems)
    assert any("metric" in p for p in problems)


def test_generation_needs_the_client_library_not_just_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment with a key but no model client must say so, not 500.

    This is the bug the first hosted release shipped: the serve-only image omitted
    the model client, so the import raised inside the request and FastAPI returned
    a bare Internal Server Error.
    """
    from incidentlens import sandbox

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setattr(sandbox, "_client_available", lambda: False)
    assert sandbox.enabled() is False

    client = TestClient(app)
    response = client.post("/api/v1/sandbox/reconstruct", json={"prompt": "redis died"})
    assert response.status_code == 503
    assert "model client" in response.json()["detail"]


# --------------------------------------------- sandbox: the silent-drop regression


def test_invalid_source_type_is_reported_not_silently_dropped() -> None:
    """The bug this guards: source_type "change" is not in SourceType, so every
    generated deployment event was discarded without a word, and the engine could
    only ever report an unexplained failure."""
    from incidentlens import sandbox

    payload = {
        "system": "demo",
        "services": [
            {"name": "web", "depends_on": ["api"], "user_facing": True},
            {"name": "api", "depends_on": []},
        ],
        "events": [
            {"id": "bad", "source_type": "change", "source": "api",
             "timestamp": "2026-07-27T09:00:00Z", "detail": "deployed v42"},
            {"id": "ok1", "source_type": "log", "source": "api",
             "timestamp": "2026-07-27T09:01:00Z", "detail": "boom",
             "attributes": {"level": "ERROR"}},
            {"id": "ok2", "source_type": "log", "source": "api",
             "timestamp": "2026-07-27T09:02:00Z", "detail": "boom again",
             "attributes": {"level": "ERROR"}},
            {"id": "ok3", "source_type": "log", "source": "web",
             "timestamp": "2026-07-27T09:03:00Z", "detail": "502",
             "attributes": {"level": "ERROR"}},
            {"id": "ok4", "source_type": "deployment", "source": "api",
             "timestamp": "2026-07-27T08:59:00Z", "detail": "deploy 2026.7.27"},
        ],
    }
    _arch, events = sandbox._to_domain(payload)
    ids = {e.id for e in events}
    assert "bad" not in ids, "an invalid source_type must not reach the engine"
    assert "ok4" in ids, '"deployment" is the valid spelling and must survive'

    dropped = sandbox.last_dropped()
    assert any("bad" in d for d in dropped), "the drop must be recorded, not silent"


def test_missing_requirements_demands_deployment_not_change() -> None:
    from incidentlens import sandbox

    problems = sandbox._missing_requirements(
        {"events": [{"source_type": "change", "attributes": {"level": "ERROR"}}]}
    )
    assert any('"deployment"' in p for p in problems)
    stale = 'it needs one event with "source_type": "change"'
    assert not any(p.startswith(stale) for p in problems)


def test_comparable_metric_matches_the_engine_threshold() -> None:
    """Mirrors _detect_metric_anomalies: prose and sub-3x rises must not count."""
    from incidentlens import sandbox

    prose = [{"source_type": "metric", "source": "a",
              "detail": "lag rose from 2 to 91", "attributes": {}}]
    assert sandbox._has_comparable_metric(prose) is False

    single = [{"source_type": "metric", "source": "a",
               "attributes": {"metric": "lag", "value": 91}}]
    assert sandbox._has_comparable_metric(single) is False

    shallow = [
        {"source_type": "metric", "source": "a", "attributes": {"metric": "lag", "value": 10}},
        {"source_type": "metric", "source": "a", "attributes": {"metric": "lag", "value": 12}},
    ]
    assert sandbox._has_comparable_metric(shallow) is False

    real = [
        {"source_type": "metric", "source": "a", "attributes": {"metric": "lag", "value": 2}},
        {"source_type": "metric", "source": "a", "attributes": {"metric": "lag", "value": 91}},
    ]
    assert sandbox._has_comparable_metric(real) is True


def test_ui_sample_logs_can_actually_attribute_a_cause() -> None:
    """The pasted sample is judge-facing: it must reach an inferred root cause
    rather than "unexplained failure", which means it has to carry a keyword the
    engine recognises as a change."""
    import re
    from pathlib import Path

    from incidentlens.engines.deterministic import CHANGE_KEYWORDS

    js = Path("src/incidentlens/static/app.js").read_text(encoding="utf-8")
    block = re.search(r"const SAMPLE_LOGS = \[(.*?)\]\.join", js, re.S)
    assert block, "SAMPLE_LOGS not found in app.js"
    text = block.group(1).lower()
    assert any(k in text for k in CHANGE_KEYWORDS), (
        "the sample must contain a change keyword or the demo shows an unexplained cause"
    )
