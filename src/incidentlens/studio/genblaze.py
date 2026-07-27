"""Genblaze provenance and Backblaze B2 publishing for Incident Lens media.

The incident renderer is deliberately deterministic: generated-video models
must not invent service names, topology, or evidence. Genblaze orchestrates the
generative narration and then records the rendered MP4 as a provenance-linked
media artifact. When B2 is enabled, the original MP4 and its canonical
manifest are persisted through Genblaze's ``ObjectStorageSink`` before the
same manifest is embedded into the local MP4.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incidentlens.domain.models import IncidentAnalysis


@dataclass(frozen=True)
class GenblazePublishResult:
    """Paths and durable identifiers produced by Genblaze publication."""

    manifest_path: Path
    canonical_hash: str
    manifest_uri: str | None
    asset_url: str
    embed_method: str
    narration_manifest_path: Path | None = None


def sdk_available() -> bool:
    """Return whether the required Genblaze core package can be imported."""
    try:
        import genblaze_core  # noqa: F401
    except ImportError:
        return False
    return True


def _require_sdk() -> None:
    if not sdk_available():
        raise RuntimeError(
            "Genblaze publishing requires Python 3.11+ and "
            "`pip install 'incidentlens[genblaze]'`"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _analysis_digest(analysis: IncidentAnalysis) -> str:
    payload = json.dumps(
        analysis.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _probe_video(path: Path) -> dict[str, Any]:
    """Best-effort technical metadata for the Genblaze Asset."""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,r_frame_rate",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(completed.stdout)
        stream = data["streams"][0]
        numerator, _, denominator = str(stream.get("r_frame_rate", "0/1")).partition("/")
        frame_rate = float(numerator) / float(denominator or "1")
        return {
            "codec": stream.get("codec_name"),
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "frame_rate": frame_rate,
            "duration": float(data["format"]["duration"]),
        }
    except (FileNotFoundError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        return {}


def _b2_region_from_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    match = re.search(r"https?://s3\.([^.]+)\.backblazeb2\.com", endpoint)
    return match.group(1) if match else None


def build_b2_backend() -> Any:
    """Genblaze's B2 storage backend, configured from the environment.

    Public because the incident library (``studio.library``) writes its
    human-readable bundles through the same backend the provenance sink uses —
    there is no second storage path in this project.
    """
    from genblaze_s3 import S3StorageBackend

    bucket = os.environ.get("B2_BUCKET")
    key_id = os.environ.get("B2_KEY_ID")
    standard_app_key = os.environ.get("B2_APP_KEY")
    legacy_app_key = os.environ.get("B2_APPLICATION_KEY")
    if (
        standard_app_key
        and legacy_app_key
        and standard_app_key != legacy_app_key
    ):
        raise RuntimeError(
            "B2_APP_KEY and B2_APPLICATION_KEY are both set to different values"
        )
    app_key = standard_app_key or legacy_app_key
    region = os.environ.get("B2_REGION") or _b2_region_from_endpoint(
        os.environ.get("B2_ENDPOINT_URL")
    )
    missing = [
        name
        for name, value in (
            ("B2_BUCKET", bucket),
            ("B2_KEY_ID", key_id),
            ("B2_APP_KEY", app_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Genblaze B2 publishing needs " + ", ".join(missing) + " in the environment"
        )

    backend: Any = S3StorageBackend.for_backblaze(
        bucket,
        region=region,
        key_id=key_id,
        app_key=app_key,
        public_url_base=os.environ.get("B2_PUBLIC_URL_BASE"),
        auto_lifecycle=False,
    )
    return backend


def _manifest_lock() -> Any:
    """Object-lock retention for uploaded manifests, or ``None``.

    Locking the *manifest* rather than the media is the point: a replay can be
    re-rendered, but the record of what produced it must not be quietly rewritable.
    Set ``INCIDENTLENS_MANIFEST_LOCK_DAYS`` to enable (the bucket must have Object
    Lock turned on, which is irreversible and done once in the B2 console).

    GOVERNANCE is the default because it can be bypassed by a key holding
    ``bypassGovernance`` — a normal delete still fails, but you are not locked out
    of your own bucket. COMPLIANCE cannot be bypassed by anyone, including you.
    """
    raw = os.environ.get("INCIDENTLENS_MANIFEST_LOCK_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        raise RuntimeError(
            f"INCIDENTLENS_MANIFEST_LOCK_DAYS must be a whole number of days, got {raw!r}"
        ) from None
    if days <= 0:
        return None

    from datetime import UTC, datetime, timedelta

    from genblaze_core import ObjectLockConfig

    mode = os.environ.get("INCIDENTLENS_MANIFEST_LOCK_MODE", "GOVERNANCE").upper()
    if mode not in {"GOVERNANCE", "COMPLIANCE"}:
        raise RuntimeError(
            f"INCIDENTLENS_MANIFEST_LOCK_MODE must be GOVERNANCE or COMPLIANCE, got {mode!r}"
        )
    return ObjectLockConfig(
        retain_until=datetime.now(UTC) + timedelta(days=days),
        mode=mode,
    )


def _build_b2_sink() -> Any:
    """Genblaze's provenance sink over the B2 backend."""
    from genblaze_core import KeyStrategy, ObjectStorageSink

    return ObjectStorageSink(
        build_b2_backend(),
        prefix=os.environ.get("INCIDENTLENS_B2_PREFIX", "incidentlens"),
        key_strategy=KeyStrategy.HIERARCHICAL,
        manifest_lock=_manifest_lock(),
    )


def _write_narration_manifest(
    video_path: Path,
    narration_manifest: Any | None,
) -> Path | None:
    if narration_manifest is None:
        return None
    narration_path = video_path.with_suffix(".narration.genblaze.json")
    narration_path.write_text(
        narration_manifest.to_canonical_json() + "\n",
        encoding="utf-8",
    )
    return narration_path


def publish_video(
    video_path: str | Path,
    *,
    analysis: IncidentAnalysis,
    source_label: str,
    narration_manifest: Any | None = None,
    upload_b2: bool = False,
) -> GenblazePublishResult:
    """Create, persist, and embed verifiable provenance for a rendered MP4."""
    _require_sdk()
    video_path = Path(video_path).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"rendered video not found: {video_path}")

    from genblaze_core import Asset, Pipeline, Track, VideoMetadata
    from genblaze_core.media import Mp4Handler

    technical = _probe_video(video_path)
    staging: tempfile.TemporaryDirectory[str] | None = None
    asset_path = video_path
    if upload_b2:
        staging = tempfile.TemporaryDirectory(prefix="incidentlens-genblaze-")
        asset_path = Path(staging.name) / video_path.name
        shutil.copyfile(video_path, asset_path)
    asset = Asset(
        url=asset_path.as_uri(),
        media_type="video/mp4",
        sha256=_sha256_file(video_path),
        size_bytes=video_path.stat().st_size,
        width=technical.get("width"),
        height=technical.get("height"),
        duration=technical.get("duration"),
        video=VideoMetadata(
            frame_rate=technical.get("frame_rate"),
            codec=technical.get("codec"),
            has_audio=True,
            resolution=(
                f"{technical['height']}p" if technical.get("height") is not None else None
            ),
        ),
        tracks=[
            Track(kind="video", codec=technical.get("codec"), label="incident-replay"),
            Track(kind="audio", codec="aac", label="AI-generated narration"),
        ],
        metadata={
            "incident_id": analysis.incident_id,
            "artifact_role": "evidence_backed_incident_reconstruction",
        },
    )

    narration_hash = (
        getattr(narration_manifest, "canonical_hash", None)
        if narration_manifest is not None
        else None
    )
    narration_run_id = (
        getattr(getattr(narration_manifest, "run", None), "run_id", None)
        if narration_manifest is not None
        else None
    )
    confidence = max(
        (hypothesis.confidence for hypothesis in analysis.hypotheses),
        default=0.0,
    )
    source_metadata = {
        "application": "Incident Lens",
        "incident_id": analysis.incident_id,
        "source_label": source_label,
        "renderer": "incidentlens-deterministic",
        "analysis_sha256": _analysis_digest(analysis),
        "evidence_count": len(analysis.evidence),
        "timeline_steps": len(analysis.timeline),
        "leading_hypothesis_confidence": confidence,
        "voice_disclosure": "AI-generated voice",
        "narration_manifest_hash": narration_hash,
        "narration_run_id": narration_run_id,
    }
    sink = None
    try:
        sink = _build_b2_sink() if upload_b2 else None
        result = _ingest(Pipeline, sink=sink, assets=[asset], source_metadata=source_metadata,
                         analysis=analysis)
    finally:
        if sink is not None:
            sink.close()
        if staging is not None:
            staging.cleanup()
    if not result.manifest.verify():
        raise RuntimeError("Genblaze final-video manifest failed verification")

    manifest_path = video_path.with_suffix(".genblaze.json")
    manifest_path.write_text(
        result.manifest.to_canonical_json() + "\n",
        encoding="utf-8",
    )
    narration_path = _write_narration_manifest(video_path, narration_manifest)

    embed_method = "sidecar"
    if upload_b2:
        # The manifest points at the immutable, pre-embed B2 object. Embedding
        # locally after upload keeps byte-level --fetch verification valid.
        embed_result = result.save(video_path, embed=True)
        if embed_result.method == "inline":
            extracted = Mp4Handler().extract(video_path)
            if (
                not extracted.verify()
                or extracted.canonical_hash != result.manifest.canonical_hash
            ):
                raise RuntimeError(
                    "embedded Genblaze manifest failed round-trip verification"
                )
        # Genblaze may deliberately fall back to a sidecar when the container
        # cannot be modified. That is a valid, verifiable result; extraction is
        # meaningful only for a successful inline embed.
        embed_method = embed_result.method

    published_asset = result.run.steps[0].assets[0]
    return GenblazePublishResult(
        manifest_path=manifest_path,
        canonical_hash=result.manifest.canonical_hash,
        manifest_uri=result.manifest.manifest_uri,
        asset_url=published_asset.url,
        embed_method=embed_method,
        narration_manifest_path=narration_path,
    )


def _ingest(
    pipeline_cls: Any,
    *,
    sink: Any,
    assets: list[Any],
    source_metadata: dict[str, Any],
    analysis: IncidentAnalysis,
) -> Any:
    """Run the Genblaze ingest, making a retention rejection actionable.

    Manifest Object Lock needs an application key carrying ``writeFileRetentions``.
    Backblaze only grants the retention capabilities to keys created *after* Object
    Lock is enabled on the bucket, so an older key fails here with a bare
    "not entitled" that says nothing about the cause.
    """
    try:
        return pipeline_cls.ingest(
            assets=assets,
            source="incidentlens-renderer",
            source_metadata=source_metadata,
            sink=sink,
            name=f"incidentlens-{analysis.incident_id}",
            tenant_id="incidentlens",
        )
    except Exception as exc:
        if sink is not None and "not entitled" in str(exc) and _manifest_lock() is not None:
            raise RuntimeError(
                "Manifest Object Lock was requested but this application key lacks "
                "'writeFileRetentions'. Backblaze grants the retention capabilities "
                "only to keys created after Object Lock is enabled on the bucket. "
                "Create a new application key, or unset "
                "INCIDENTLENS_MANIFEST_LOCK_DAYS to publish without the lock."
            ) from exc
        raise


__all__ = [
    "build_b2_backend",
    "GenblazePublishResult",
    "publish_video",
    "sdk_available",
]
