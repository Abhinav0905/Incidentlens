from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from incidentlens.connectors.synthetic import SyntheticConnector
from incidentlens.engines.deterministic import DeterministicAnalysisEngine
from incidentlens.services.incidents import IncidentService
from incidentlens.studio import genblaze as genblaze_module
from incidentlens.studio.genblaze import (
    _b2_region_from_endpoint,
    _build_b2_sink,
    publish_video,
)
from incidentlens.studio.voice import GenblazeOpenAIVoice

genblaze_core = pytest.importorskip("genblaze_core")
pytest.importorskip("genblaze_openai")
pytest.importorskip("genblaze_s3")

Asset = genblaze_core.Asset
KeyStrategy = genblaze_core.KeyStrategy
Pipeline = genblaze_core.Pipeline
PipelineResult = genblaze_core.PipelineResult
SyncProvider = genblaze_core.SyncProvider
parse_manifest = genblaze_core.parse_manifest


class _FakeOpenAITTSProvider(SyncProvider):
    """Provider-shaped WAV writer; deliberately leaves sha256 unset."""

    name = "fake-openai-tts"

    def __init__(
        self,
        api_key: str | None = None,
        output_dir: str | Path | None = None,
        **_kwargs: Any,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.output_dir = Path(output_dir or ".")

    def generate(self, step, config=None):
        del config
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out = self.output_dir / f"{step.step_id}.wav"
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 800)
        step.assets.append(Asset(url=out.resolve().as_uri(), media_type="audio/wav"))
        return step


def _analysis():
    connector = SyntheticConnector("gateway-auth-rejection")
    return IncidentService(
        connector=connector,
        engine=DeterministicAnalysisEngine(),
    ).analyze()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_video(path: Path) -> None:
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x0a0d13:s=320x180:r=8:d=0.5",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            "0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def test_genblaze_voice_hashes_every_audio_asset_before_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import genblaze_openai

    monkeypatch.setattr(
        genblaze_openai,
        "OpenAITTSProvider",
        _FakeOpenAITTSProvider,
    )
    voice = GenblazeOpenAIVoice(api_key="test", voice="coral")
    outputs = [tmp_path / "a.wav", tmp_path / "b.wav"]

    durations = voice.synthesize_many(["First beat.", "Second beat."], outputs)

    assert durations == pytest.approx([0.1, 0.1], abs=0.02)
    assert all(path.is_file() for path in outputs)
    assert voice.latest_manifest is not None
    assert voice.latest_manifest.verify()
    assert all(
        asset.sha256
        for step in voice.latest_manifest.run.steps
        for asset in step.assets
    )
    assert [
        asset.duration
        for step in voice.latest_manifest.run.steps
        for asset in step.assets
    ] == pytest.approx([0.1, 0.1], abs=0.02)
    assert all(
        step.prompt_visibility.value == "private"
        for step in voice.latest_manifest.run.steps
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_local_genblaze_publication_preserves_mp4_bytes_and_writes_manifest(
    tmp_path: Path,
) -> None:
    video = tmp_path / "incident.mp4"
    _write_video(video)
    before = _sha256(video)

    published = publish_video(
        video,
        analysis=_analysis(),
        source_label="test",
        upload_b2=False,
    )

    assert published.embed_method == "sidecar"
    assert published.manifest_path.is_file()
    assert _sha256(video) == before
    manifest = parse_manifest(
        json.loads(published.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest.verify()
    assert manifest.run.steps[0].assets[0].sha256 == before
    assert manifest.run.steps[0].metadata["source"] == "incidentlens-renderer"
    assert manifest.run.steps[0].metadata["analysis_sha256"]


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://s3.us-west-004.backblazeb2.com", "us-west-004"),
        ("https://s3.eu-central-003.backblazeb2.com", "eu-central-003"),
        ("https://example.invalid", None),
        (None, None),
    ],
)
def test_b2_region_is_derived_from_legacy_endpoint(
    endpoint: str | None,
    expected: str | None,
) -> None:
    assert _b2_region_from_endpoint(endpoint) == expected


def test_b2_sink_accepts_legacy_secret_and_endpoint_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import genblaze_core
    import genblaze_s3

    calls: dict[str, Any] = {}

    class FakeBackend:
        @classmethod
        def for_backblaze(cls, bucket, **kwargs):
            calls["backend"] = {"bucket": bucket, **kwargs}
            return object()

    class FakeSink:
        def __init__(self, backend, **kwargs):
            calls["sink"] = {"backend": backend, **kwargs}

    monkeypatch.setattr(genblaze_s3, "S3StorageBackend", FakeBackend)
    monkeypatch.setattr(genblaze_core, "ObjectStorageSink", FakeSink)
    monkeypatch.setenv("B2_BUCKET", "incidentlens-demo")
    monkeypatch.setenv("B2_KEY_ID", "key-id")
    monkeypatch.setenv("B2_APPLICATION_KEY", "legacy-secret")
    monkeypatch.delenv("B2_APP_KEY", raising=False)
    monkeypatch.delenv("B2_REGION", raising=False)
    monkeypatch.setenv(
        "B2_ENDPOINT_URL",
        "https://s3.us-east-005.backblazeb2.com",
    )

    sink = _build_b2_sink()

    assert isinstance(sink, FakeSink)
    assert calls["backend"] == {
        "bucket": "incidentlens-demo",
        "region": "us-east-005",
        "key_id": "key-id",
        "app_key": "legacy-secret",
        "public_url_base": None,
        "auto_lifecycle": False,
    }
    assert calls["sink"]["prefix"] == "incidentlens"
    assert calls["sink"]["key_strategy"] is KeyStrategy.HIERARCHICAL


def test_b2_sink_rejects_conflicting_secret_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("B2_BUCKET", "incidentlens-demo")
    monkeypatch.setenv("B2_KEY_ID", "key-id")
    monkeypatch.setenv("B2_APP_KEY", "standard-secret")
    monkeypatch.setenv("B2_APPLICATION_KEY", "different-secret")

    with pytest.raises(RuntimeError, match="different values"):
        _build_b2_sink()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_genblaze_b2_publication_closes_sink_and_stages_local_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / "incident.mp4"
    _write_video(video)
    captured: dict[str, Any] = {}

    class FakeSink:
        closed = False

        def close(self) -> None:
            self.closed = True

    sink = FakeSink()
    real_ingest = Pipeline.ingest

    def fake_ingest(*, assets, source, source_metadata, sink, name, tenant_id):
        captured["staged_path"] = Path(assets[0].url.removeprefix("file://"))
        captured["staged_exists"] = captured["staged_path"].is_file()
        captured["sink"] = sink
        return real_ingest(
            assets=assets,
            source=source,
            source_metadata=source_metadata,
            sink=None,
            name=name,
            tenant_id=tenant_id,
        )

    monkeypatch.setattr(genblaze_module, "_build_b2_sink", lambda: sink)
    monkeypatch.setattr(Pipeline, "ingest", staticmethod(fake_ingest))

    published = publish_video(
        video,
        analysis=_analysis(),
        source_label="test",
        upload_b2=True,
    )

    assert captured["staged_exists"]
    assert captured["staged_path"].parent != video.parent
    assert captured["sink"] is sink
    assert sink.closed
    assert published.embed_method == "inline"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_genblaze_b2_publication_accepts_embed_sidecar_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video = tmp_path / "incident.mp4"
    _write_video(video)

    class FakeSink:
        def close(self) -> None:
            pass

    real_ingest = Pipeline.ingest

    def fake_ingest(*, assets, source, source_metadata, sink, name, tenant_id):
        return real_ingest(
            assets=assets,
            source=source,
            source_metadata=source_metadata,
            sink=None,
            name=name,
            tenant_id=tenant_id,
        )

    def fake_save(self, path, *, embed=True, policy=None):
        del self, path, embed, policy
        return SimpleNamespace(method="sidecar")

    monkeypatch.setattr(genblaze_module, "_build_b2_sink", FakeSink)
    monkeypatch.setattr(Pipeline, "ingest", staticmethod(fake_ingest))
    monkeypatch.setattr(PipelineResult, "save", fake_save)

    published = publish_video(
        video,
        analysis=_analysis(),
        source_label="test",
        upload_b2=True,
    )

    assert published.embed_method == "sidecar"
    assert published.manifest_path.is_file()
