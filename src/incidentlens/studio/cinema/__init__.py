"""Cinematic renderer for IncidentLens Studio.

A continuous, camera-driven 3D replay of the incident — perspective
projection, animated state transitions, particle traffic, bloom — rendered in
pure Python (numpy + Pillow) and encoded by ffmpeg. See ``engine.py`` for the
frame model and ``timeline.py`` for how narration beats drive the clock.
"""

from __future__ import annotations

from incidentlens.studio.cinema.dependencies import (
    ModuleScene,
    build_module_view,
)
from incidentlens.studio.cinema.engine import PROFILES, CinematicScene, RenderSpec
from incidentlens.studio.cinema.internal import (
    DiveAct,
    InternalScene,
    MovieScene,
    build_movie_scene,
)
from incidentlens.studio.cinema.symbols import SymbolScene, build_symbol_view
from incidentlens.studio.cinema.timeline import Timeline

__all__ = [
    "CinematicScene",
    "InternalScene",
    "ModuleScene",
    "SymbolScene",
    "build_module_view",
    "build_symbol_view",
    "DiveAct",
    "MovieScene",
    "build_movie_scene",
    "RenderSpec",
    "PROFILES",
    "Timeline",
]
