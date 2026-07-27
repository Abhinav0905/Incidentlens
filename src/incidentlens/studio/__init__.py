"""IncidentLens Studio: turn an incident analysis into a narrated video.

The visuals are deterministic (the same architecture replay the web UI shows,
rendered frame by frame). The narration is the generative layer: an LLM writes
the incident story, a text-to-speech voice reads it, and the two are composed
into an MP4. Every narrated claim is still bound to the evidence in the
analysis, so the "never present a guess as fact" rule holds on the voice track
too.

Optional dependency group::

    pip install "incidentlens[studio]"

System tools required for rendering: ffmpeg (video/audio muxing) and, for the
zero-cost offline voice, espeak-ng.
"""

from __future__ import annotations

from incidentlens.studio.narration import Narration, NarrationBeat, build_narration
from incidentlens.studio.pipeline import (
    VideoResult,
    produce_incident_video,
    produce_video_from_analysis,
)
from incidentlens.studio.theme import PALETTE
from incidentlens.studio.voice import (
    ElevenLabsVoice,
    GenblazeOpenAIVoice,
    OfflineVoice,
    OpenAIVoice,
    PiperVoice,
    SilentVoice,
    Voice,
)

__all__ = [
    "Narration",
    "NarrationBeat",
    "build_narration",
    "produce_incident_video",
    "produce_video_from_analysis",
    "VideoResult",
    "Voice",
    "OpenAIVoice",
    "GenblazeOpenAIVoice",
    "OfflineVoice",
    "PiperVoice",
    "ElevenLabsVoice",
    "SilentVoice",
    "PALETTE",
]
