"""Text-to-speech voices for the narration track.

``OfflineVoice`` (espeak-ng) needs no key and runs anywhere — the default and
the demo fallback. ``OpenAIVoice`` and ``ElevenLabsVoice`` provide neural
hosted speech. ``SilentVoice`` emits timed silence for CI and muted renders.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import url2pathname


def audio_duration(path: str | Path) -> float:
    """Duration in seconds. Uses ffprobe when present, else the wave header."""
    path = str(path)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            out = subprocess.run(
                [
                    ffprobe,
                    "-loglevel",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return float(out.stdout.strip())
        except (subprocess.CalledProcessError, ValueError):
            pass
    if path.endswith(".wav"):
        with contextlib.closing(wave.open(path, "rb")) as wav:
            return wav.getnframes() / float(wav.getframerate())
    raise RuntimeError(f"cannot measure duration of {path!r} without ffprobe")


class Voice(Protocol):
    def synthesize(self, text: str, out_path: str | Path) -> float:
        """Write audio for ``text`` to ``out_path`` and return its duration."""
        ...


def _estimated_seconds(text: str, wps: float = 2.7, floor: float = 2.2) -> float:
    words = max(1, len(text.split()))
    return max(floor, words / wps)


class SilentVoice:
    """Timed silence, length estimated from the text. No dependencies."""

    def __init__(self, sample_rate: int = 44100) -> None:
        self.sample_rate = sample_rate

    def synthesize(self, text: str, out_path: str | Path) -> float:
        duration = _estimated_seconds(text)
        frames = int(duration * self.sample_rate)
        with contextlib.closing(wave.open(str(out_path), "wb")) as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
        return duration


class OfflineVoice:
    """espeak-ng, offline and free. Good enough for review; robotic on purpose."""

    def __init__(self, lang: str = "en-us", words_per_minute: int = 165) -> None:
        self.lang = lang
        self.wpm = words_per_minute
        self._bin = shutil.which("espeak-ng") or shutil.which("espeak")

    def available(self) -> bool:
        return self._bin is not None

    def synthesize(self, text: str, out_path: str | Path) -> float:
        if not self._bin:
            raise RuntimeError("espeak-ng not found; install it or use another voice")
        subprocess.run(
            [self._bin, "-v", self.lang, "-s", str(self.wpm), "-w", str(out_path), text],
            check=True,
            capture_output=True,
        )
        return audio_duration(out_path)


class PiperVoice:
    """Piper — open-source neural TTS. Offline, no key, no GPU, near-natural.

    The recommended free upgrade over espeak. Install the engine and one voice::

        pip install piper-tts
        # download a voice (two files: .onnx and .onnx.json), e.g. from
        # https://huggingface.co/rhasspy/piper-voices  (en_US-lessac-medium is a
        # good, clear narrator voice)
        export INCIDENTLENS_PIPER_MODEL=/path/to/en_US-lessac-medium.onnx

    Then ``--voice piper``. ``length_scale`` > 1 slows the delivery (more
    documentary-paced); < 1 speeds it up.
    """

    def __init__(
        self,
        model: str | None = None,
        binary: str | None = None,
        length_scale: float = 1.0,
    ) -> None:
        self.model = model or os.environ.get("INCIDENTLENS_PIPER_MODEL", "")
        self._bin = (
            binary or os.environ.get("INCIDENTLENS_PIPER_BIN") or shutil.which("piper")
        )
        self.length_scale = length_scale

    def available(self) -> bool:
        return bool(self._bin and self.model and Path(self.model).is_file())

    def synthesize(self, text: str, out_path: str | Path) -> float:
        if not self._bin:
            raise RuntimeError(
                "piper not found; `pip install piper-tts` or set INCIDENTLENS_PIPER_BIN"
            )
        if not (self.model and Path(self.model).is_file()):
            raise RuntimeError(
                "PiperVoice needs a voice model — set INCIDENTLENS_PIPER_MODEL to a "
                ".onnx file (see https://huggingface.co/rhasspy/piper-voices)"
            )
        subprocess.run(
            [
                self._bin,
                "--model", self.model,
                "--output_file", str(out_path),
                "--length_scale", str(self.length_scale),
            ],
            input=text,
            text=True,
            check=True,
            capture_output=True,
        )
        return audio_duration(out_path)


DEFAULT_OPENAI_TTS_INSTRUCTIONS = (
    "Speak in a calm, warm, polite, and reassuring tone. Deliver this as a concise "
    "professional incident briefing at a measured pace. Keep technical terms clear "
    "and avoid sounding alarmed, aggressive, or theatrical."
)


class OpenAIVoice:
    """Neural TTS via OpenAI's Speech API, streamed as uncompressed WAV."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_id: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        )
        self.model_id = model_id or os.environ.get(
            "INCIDENTLENS_OPENAI_TTS_MODEL", "gpt-4o-mini-tts"
        )
        self.voice = voice or os.environ.get("INCIDENTLENS_OPENAI_VOICE", "marin")
        self.instructions = instructions or os.environ.get(
            "INCIDENTLENS_OPENAI_VOICE_INSTRUCTIONS",
            DEFAULT_OPENAI_TTS_INSTRUCTIONS,
        )
        self.base_url = (
            base_url
            if base_url is not None
            else os.environ.get("INCIDENTLENS_OPENAI_TTS_BASE_URL") or None
        )

    def available(self) -> bool:
        return bool(self.api_key)

    def synthesize(self, text: str, out_path: str | Path) -> float:
        if not self.api_key:
            raise RuntimeError("OpenAIVoice requires OPENAI_API_KEY")

        from openai import OpenAI

        if self.base_url:
            client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        else:
            client = OpenAI(api_key=self.api_key)
        with client.audio.speech.with_streaming_response.create(
            model=self.model_id,
            voice=self.voice,
            input=text,
            instructions=self.instructions,
            response_format="wav",
        ) as response:
            response.stream_to_file(Path(out_path))
        return audio_duration(out_path)


class GenblazeOpenAIVoice:
    """OpenAI TTS orchestrated through a Genblaze media pipeline.

    All narration beats are recorded in one Genblaze run, so the resulting
    manifest captures the provider, model, prompt, parameters, output hashes,
    retries, and timings for the complete voice track. The manifest is kept on
    ``latest_manifest`` for the final-video publisher to link into the MP4's
    provenance.

    Genblaze currently validates the original OpenAI voice set, so ``coral`` is
    the calm presentation default here. Direct ``OpenAIVoice`` remains
    available for newer voices and OpenAI-compatible custom base URLs.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model_id: str | None = None,
        voice: str | None = None,
        instructions: str | None = None,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        )
        self.model_id = model_id or os.environ.get(
            "INCIDENTLENS_OPENAI_TTS_MODEL", "gpt-4o-mini-tts"
        )
        self.voice = voice or os.environ.get("INCIDENTLENS_GENBLAZE_VOICE", "coral")
        self.instructions = instructions or os.environ.get(
            "INCIDENTLENS_OPENAI_VOICE_INSTRUCTIONS",
            DEFAULT_OPENAI_TTS_INSTRUCTIONS,
        )
        self.latest_manifest: Any | None = None
        self.latest_result: Any | None = None

    @staticmethod
    def sdk_available() -> bool:
        try:
            import genblaze_core  # noqa: F401
            import genblaze_openai  # noqa: F401
        except ImportError:
            return False
        return True

    def available(self) -> bool:
        return bool(self.api_key) and self.sdk_available()

    def synthesize_many(
        self,
        texts: list[str],
        out_paths: list[str | Path],
    ) -> list[float]:
        """Generate every narration beat as one provenance-linked pipeline."""
        if len(texts) != len(out_paths):
            raise ValueError("texts and out_paths must contain the same number of items")
        if not texts:
            return []
        if not self.api_key:
            raise RuntimeError("GenblazeOpenAIVoice requires OPENAI_API_KEY")
        if not self.sdk_available():
            raise RuntimeError(
                "Genblaze voice requires Python 3.11+ and "
                "`pip install 'incidentlens[genblaze]'`"
            )

        from genblaze_core import Modality, Pipeline, PromptVisibility
        from genblaze_openai import OpenAITTSProvider

        destinations = [Path(path) for path in out_paths]
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)

        provider = OpenAITTSProvider(
            api_key=self.api_key,
            output_dir=destinations[0].parent,
        )
        pipeline = Pipeline(
            "incidentlens-narration",
            project_id="incidentlens",
        ).metadata(
            application="Incident Lens",
            pipeline_role="incident_narration",
            disclosure="AI-generated voice",
        )
        for index, text in enumerate(texts):
            pipeline.step(
                provider,
                model=self.model_id,
                prompt=text,
                modality=Modality.AUDIO,
                voice=self.voice,
                response_format="wav",
                instructions=self.instructions,
                metadata={"beat_index": index},
                prompt_visibility=PromptVisibility.PRIVATE,
            )

        def hash_completed_audio(event: Any) -> None:
            """Bind provider-local WAV bytes before Genblaze finalizes its manifest."""
            for asset in event.step.assets:
                parsed = urlparse(asset.url)
                if parsed.scheme != "file":
                    continue
                path = Path(url2pathname(parsed.path))
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                asset.sha256 = digest.hexdigest()
                asset.size_bytes = path.stat().st_size
                # genblaze-openai currently probes streaming WAV duration with
                # mutagen, which can misread the container length by hours.
                # Use the same WAV-aware probe that drives beat timing so the
                # provenance describes the bytes judges can actually inspect.
                asset.duration = audio_duration(path)

        result = pipeline.run(
            timeout=90,
            max_retries=2,
            pipeline_timeout=max(120, len(texts) * 90),
            progress=False,
            raise_on_failure=True,
            on_step_complete=hash_completed_audio,
        )
        if len(result.run.steps) != len(destinations):
            raise RuntimeError(
                "Genblaze returned an unexpected number of narration steps"
            )

        durations: list[float] = []
        for step, destination in zip(result.run.steps, destinations, strict=True):
            if not step.assets:
                raise RuntimeError("Genblaze narration step returned no audio asset")
            parsed = urlparse(step.assets[0].url)
            if parsed.scheme != "file":
                raise RuntimeError(
                    f"Genblaze OpenAI TTS returned unsupported URL: {step.assets[0].url}"
                )
            source = Path(url2pathname(parsed.path))
            if source.resolve() != destination.resolve():
                shutil.copyfile(source, destination)
            durations.append(audio_duration(destination))

        if not result.manifest.verify():
            raise RuntimeError("Genblaze narration manifest failed verification")
        self.latest_result = result
        self.latest_manifest = result.manifest
        return durations

    def synthesize(self, text: str, out_path: str | Path) -> float:
        return self.synthesize_many([text], [out_path])[0]


class ElevenLabsVoice:
    """Neural TTS via ElevenLabs. Needs ELEVENLABS_API_KEY and a voice id.

    Not exercised by the test suite (it calls a paid external API). The request
    shape follows the ElevenLabs text-to-speech endpoint.
    """

    def __init__(
        self,
        voice_id: str | None = None,
        api_key: str | None = None,
        model_id: str = "eleven_turbo_v2_5",
    ) -> None:
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "")
        self.model_id = model_id

    def synthesize(self, text: str, out_path: str | Path) -> float:
        if not self.api_key or not self.voice_id:
            raise RuntimeError("ElevenLabsVoice requires ELEVENLABS_API_KEY and a voice id")
        import requests

        resp = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}",
            headers={"xi-api-key": self.api_key, "accept": "audio/mpeg"},
            json={"text": text, "model_id": self.model_id},
            timeout=60,
        )
        resp.raise_for_status()
        Path(out_path).write_bytes(resp.content)
        return audio_duration(out_path)
