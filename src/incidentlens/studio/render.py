"""Compose the incident movie.

Two styles:

* ``cinematic`` (default) — one continuous shot rendered by
  ``studio.cinema``: the camera glides across a 3D scene of the architecture
  while nodes animate through their state changes. Frames stream straight into
  ffmpeg as raw RGB; narration beats are placed on a single mixed audio track
  at their exact start times.
* ``classic`` — the original web-parity replay: one still SVG per beat with
  hard cuts. Kept for parity checks and very constrained environments
  (requires cairosvg).

ffmpeg is the only system dependency for both.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np

from incidentlens.domain.models import ArchitectureGraph, CodeGraph, IncidentAnalysis
from incidentlens.studio.narration import Narration
from incidentlens.studio.voice import Voice, audio_duration

AUDIO_RATE = 44100
LOUDNESS_FILTER = "loudnorm=I=-18:TP=-1.5:LRA=11"
ProgressFn = Callable[[float], None]


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise RuntimeError("ffmpeg not found on PATH; install it to render video")
    return binary


def _video_geometry(path: Path) -> tuple[int, int, float]:
    """Return the first video stream's width, height and frame rate."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH; it ships with ffmpeg")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    rate = str(stream["r_frame_rate"])
    numerator, _, denominator = rate.partition("/")
    fps = float(numerator) / float(denominator or "1")
    return int(stream["width"]), int(stream["height"]), fps


def _prepend_intro(ffmpeg: str, intro: Path, core: Path, out_path: Path) -> Path:
    """Normalize and prepend a reusable cinematic intro to the evidence replay.

    Intro audio is intentionally discarded. The incident narration remains the
    only spoken track, with silence under the bumper and a short visual fade at
    the hand-off.
    """
    if not intro.is_file():
        raise FileNotFoundError(f"intro video not found: {intro}")
    width, height, fps = _video_geometry(core)
    intro_seconds = audio_duration(intro)
    fade = min(0.45, max(0.1, intro_seconds / 5.0))
    fade_start = max(0.0, intro_seconds - fade)
    filter_graph = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps:.6f},setsar=1,format=yuv420p,"
        f"fade=t=out:st={fade_start:.3f}:d={fade:.3f}[intro];"
        f"[1:v]scale={width}:{height},fps={fps:.6f},setsar=1,format=yuv420p,"
        f"fade=t=in:st=0:d={fade:.3f}[core];"
        "[intro][core]concat=n=2:v=1:a=0[v];"
        f"[2:a][1:a]concat=n=2:v=0:a=1[joined];"
        f"[joined]{LOUDNESS_FILTER}[a]"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(intro),
            "-i",
            str(core),
            "-f",
            "lavfi",
            "-t",
            f"{intro_seconds:.3f}",
            "-i",
            f"anullsrc=r={AUDIO_RATE}:cl=stereo",
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-profile:v",
            "high",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(AUDIO_RATE),
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )
    return out_path


# --------------------------------------------------------------------- audio


def _decode_pcm(ffmpeg: str, path: Path) -> np.ndarray:
    """Any audio file -> (n, 2) int16 at 44.1 kHz."""
    out = subprocess.run(
        [ffmpeg, "-loglevel", "error", "-i", str(path), "-f", "s16le",
         "-ac", "2", "-ar", str(AUDIO_RATE), "pipe:"],
        check=True,
        capture_output=True,
    )
    pcm = np.frombuffer(out.stdout, dtype=np.int16)
    return pcm.reshape(-1, 2)


def _mix_narration(
    ffmpeg: str,
    beat_audio: list[Path],
    starts: list[float],
    total_seconds: float,
    out_wav: Path,
) -> Path:
    """Place each beat's audio at its scheduled start on one stereo track."""
    total_frames = int(total_seconds * AUDIO_RATE) + AUDIO_RATE // 4
    mix = np.zeros((total_frames, 2), dtype=np.int32)
    for path, start in zip(beat_audio, starts, strict=True):
        pcm = _decode_pcm(ffmpeg, path)
        offset = int(start * AUDIO_RATE)
        end = min(total_frames, offset + len(pcm))
        if end > offset:
            mix[offset:end] += pcm[: end - offset]
    clipped = np.clip(mix, -32768, 32767).astype(np.int16)
    with wave.open(str(out_wav), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(AUDIO_RATE)
        wav.writeframes(clipped.tobytes())
    return out_wav


def _synthesize_narration(
    voice: Voice,
    narration: Narration,
    work: Path,
) -> tuple[list[Path], list[float]]:
    """Generate beat audio, using a provider's batch pipeline when available."""
    beat_audio = [work / f"a{i:03d}.wav" for i in range(len(narration.beats))]
    texts = [beat.text for beat in narration.beats]
    synthesize_many = getattr(voice, "synthesize_many", None)
    if callable(synthesize_many):
        durations = list(synthesize_many(texts, beat_audio))
    else:
        durations = [
            voice.synthesize(text, audio)
            for text, audio in zip(texts, beat_audio, strict=True)
        ]
    if len(durations) != len(beat_audio):
        raise RuntimeError("voice returned an unexpected number of narration durations")
    return beat_audio, durations


# ----------------------------------------------------------------- cinematic


def _render_cinematic(
    analysis: IncidentAnalysis,
    architecture: ArchitectureGraph,
    narration: Narration,
    voice: Voice,
    out_path: Path,
    *,
    code_graph: CodeGraph | None,
    profile: str,
    fps: int | None,
    min_hold: float,
    work: Path,
    progress: ProgressFn | None,
) -> Path:
    from incidentlens.studio.cinema import Timeline, build_movie_scene
    from incidentlens.studio.cinema.engine import PROFILES

    ffmpeg = _ffmpeg()
    spec = PROFILES.get(profile)
    if spec is None:
        raise ValueError(f"unknown profile {profile!r}; options: {sorted(PROFILES)}")
    if fps:
        spec = replace(spec, fps=fps)

    beat_audio, durations = _synthesize_narration(voice, narration, work)

    timeline = Timeline(analysis, narration, durations, min_hold=min_hold)
    mix = _mix_narration(
        ffmpeg, beat_audio, [s.start for s in timeline.spans], timeline.total,
        work / "narration.wav",
    )

    scene = build_movie_scene(
        analysis, architecture, timeline, spec, code_graph=code_graph
    )
    total_frames = max(1, int(round(timeline.total * spec.fps)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    encoder = subprocess.Popen(
        [
            ffmpeg, "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{spec.width}x{spec.height}", "-r", str(spec.fps), "-i", "pipe:",
            "-i", str(mix),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE),
            "-af", LOUDNESS_FILTER,
            "-shortest", "-movflags", "+faststart",
            str(out_path),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert encoder.stdin is not None
    try:
        for i in range(total_frames):
            frame = scene.frame(i / spec.fps)
            encoder.stdin.write(frame.tobytes())
            if progress and (i % spec.fps == 0 or i == total_frames - 1):
                progress((i + 1) / total_frames)
    finally:
        encoder.stdin.close()
        stderr = encoder.stderr.read() if encoder.stderr else b""
        code = encoder.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed ({code}): {stderr.decode(errors='replace')[:800]}")
    return out_path


# ------------------------------------------------------------------- classic


def _segment(
    ffmpeg: str, image: Path, audio: Path, duration: float, fps: int, out: Path
) -> None:
    subprocess.run(
        [
            ffmpeg, "-loglevel", "error", "-y",
            "-loop", "1", "-framerate", str(fps), "-t", f"{duration:.3f}", "-i", str(image),
            "-i", str(audio),
            "-filter_complex", "[1:a]apad[a]", "-map", "0:v", "-map", "[a]",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-ar", str(AUDIO_RATE),
            str(out),
        ],
        check=True,
        capture_output=True,
    )


def _render_classic(
    analysis: IncidentAnalysis,
    architecture: ArchitectureGraph,
    narration: Narration,
    voice: Voice,
    out_path: Path,
    *,
    fps: int,
    min_hold: float,
    work: Path,
) -> Path:
    from incidentlens.studio import frames

    ffmpeg = _ffmpeg()
    layout = frames.compute_layout(architecture)
    segments: list[Path] = []
    total = len(narration.beats)
    beat_audio, durations = _synthesize_narration(voice, narration, work)
    for i, (beat, audio, spoken) in enumerate(
        zip(narration.beats, beat_audio, durations, strict=True)
    ):
        duration = max(min_hold, spoken)
        evidence_label = (
            f"evidence: {', '.join(beat.evidence_ids)}" if beat.evidence_ids else ""
        )
        svg = frames.render_frame_svg(
            analysis,
            architecture,
            layout,
            timeline_index=beat.timeline_index,
            clock_label=beat.clock_label,
            caption_title=beat.title,
            caption_body=beat.text,
            evidence_label=evidence_label,
            progress=(i + 1) / total,
        )
        image = work / f"f{i:03d}.png"
        frames.rasterize(svg, str(image))
        segment = work / f"seg{i:03d}.mp4"
        _segment(ffmpeg, image, audio, duration, fps, segment)
        segments.append(segment)

    concat_list = work / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{seg.as_posix()}'\n" for seg in segments), encoding="utf-8"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-ar", str(AUDIO_RATE),
            "-af", LOUDNESS_FILTER,
            "-movflags", "+faststart",
            str(out_path),
        ],
        check=True,
        capture_output=True,
    )
    return out_path


# ------------------------------------------------------------------- public


def render_video(
    analysis: IncidentAnalysis,
    architecture: ArchitectureGraph,
    narration: Narration,
    voice: Voice,
    out_path: str | Path,
    *,
    code_graph: CodeGraph | None = None,
    fps: int | None = None,
    style: str = "cinematic",
    profile: str = "high",
    min_hold: float = 3.2,
    work_dir: str | Path | None = None,
    intro_video: str | Path | None = None,
    progress: ProgressFn | None = None,
) -> Path:
    out_path = Path(out_path)
    tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
    if work_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="incidentlens-studio-")
        work = Path(tmp_ctx.name)
    else:
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
    try:
        render_target = work / "incident-core.mp4" if intro_video else out_path
        if style == "cinematic":
            rendered = _render_cinematic(
                analysis, architecture, narration, voice, render_target,
                code_graph=code_graph,
                profile=profile, fps=fps, min_hold=min_hold, work=work,
                progress=progress,
            )
        elif style == "classic":
            rendered = _render_classic(
                analysis, architecture, narration, voice, render_target,
                fps=fps or 24, min_hold=min(min_hold, 2.4), work=work,
            )
        else:
            raise ValueError(f"unknown style {style!r} (choose cinematic or classic)")
        if intro_video:
            return _prepend_intro(_ffmpeg(), Path(intro_video), rendered, out_path)
        return rendered
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


__all__ = ["render_video", "audio_duration"]
