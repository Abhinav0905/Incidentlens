from __future__ import annotations

import sys
import types
import wave
from pathlib import Path

import pytest

from incidentlens.studio.voice import (
    DEFAULT_OPENAI_TTS_INSTRUCTIONS,
    OpenAIVoice,
)


class _FakeStreamingResponse:
    def __init__(self) -> None:
        self.out_path: Path | None = None

    def __enter__(self) -> _FakeStreamingResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def stream_to_file(self, out_path: str | Path) -> None:
        self.out_path = Path(out_path)
        with wave.open(str(self.out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 800)


class _FakeSpeechCreate:
    def __init__(self, response: _FakeStreamingResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _FakeStreamingResponse:
        self.calls.append(kwargs)
        return self.response


class _FakeOpenAI:
    init_calls: list[dict[str, object]] = []
    response = _FakeStreamingResponse()
    speech_create = _FakeSpeechCreate(response)

    def __init__(self, **kwargs: object) -> None:
        self.init_calls.append(kwargs)
        streaming = types.SimpleNamespace(create=self.speech_create.create)
        speech = types.SimpleNamespace(with_streaming_response=streaming)
        self.audio = types.SimpleNamespace(speech=speech)


@pytest.fixture(autouse=True)
def _mock_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeOpenAI.init_calls = []
    _FakeOpenAI.response = _FakeStreamingResponse()
    _FakeOpenAI.speech_create = _FakeSpeechCreate(_FakeOpenAI.response)
    module = types.ModuleType("openai")
    module.OpenAI = _FakeOpenAI  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", module)


def test_openai_voice_streams_wav_with_polite_defaults(tmp_path: Path) -> None:
    out = tmp_path / "briefing.wav"
    voice = OpenAIVoice(api_key="test-key")

    duration = voice.synthesize("The payment service is recovering.", out)

    assert voice.available()
    assert duration == pytest.approx(0.1, abs=0.02)
    assert _FakeOpenAI.init_calls == [{"api_key": "test-key"}]
    assert _FakeOpenAI.speech_create.calls == [
        {
            "model": "gpt-4o-mini-tts",
            "voice": "marin",
            "input": "The payment service is recovering.",
            "instructions": DEFAULT_OPENAI_TTS_INSTRUCTIONS,
            "response_format": "wav",
        }
    ]
    assert _FakeOpenAI.response.out_path == out


def test_openai_voice_uses_dedicated_tts_base_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("INCIDENTLENS_OPENAI_TTS_BASE_URL", "https://speech.example/v1")
    voice = OpenAIVoice(
        model_id="custom-tts",
        voice="coral",
        instructions="Speak softly.",
    )

    voice.synthesize("A concise update.", tmp_path / "custom.wav")

    assert _FakeOpenAI.init_calls == [
        {"api_key": "env-key", "base_url": "https://speech.example/v1"}
    ]
    assert _FakeOpenAI.speech_create.calls[0] == {
        "model": "custom-tts",
        "voice": "coral",
        "input": "A concise update.",
        "instructions": "Speak softly.",
        "response_format": "wav",
    }


def test_openai_voice_reports_missing_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    voice = OpenAIVoice()

    assert not voice.available()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        voice.synthesize("This must not call the SDK.", tmp_path / "missing.wav")
    assert _FakeOpenAI.init_calls == []


def test_auto_voice_prefers_openai_when_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from incidentlens.studio.pipeline import build_voice

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")

    assert isinstance(build_voice("auto"), OpenAIVoice)
