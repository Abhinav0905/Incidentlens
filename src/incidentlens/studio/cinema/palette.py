"""Colors for the cinematic renderer.

Derived from the product palette in ``studio.theme`` so the video, the web
replay and the classic renderer all read as one product — then extended with
the per-face shades the 3D slabs need.
"""

from __future__ import annotations

from incidentlens.studio.cinema.easing import hex_rgb
from incidentlens.studio.theme import PALETTE

RGB = tuple[int, int, int]

BG_TOP: RGB = (10, 13, 19)
BG_BOTTOM: RGB = (17, 21, 31)
GRID_LINE: RGB = (36, 45, 63)
FLOOR_GLOW: RGB = (120, 150, 210)
SHADOW: RGB = (4, 5, 8)

TEXT: RGB = hex_rgb(PALETTE["text"])
DIM: RGB = hex_rgb(PALETTE["dim"])
ACCENT: RGB = hex_rgb(PALETTE["accent"])
PANEL_LINE: RGB = hex_rgb(PALETTE["line"])

EDGE_IDLE: RGB = (74, 88, 116)
EDGE_ACTIVE: RGB = (255, 122, 92)
PARTICLE_IDLE: RGB = (150, 178, 228)
PARTICLE_ACTIVE: RGB = (255, 148, 108)

STATE_STROKE: dict[str, RGB] = {
    "healthy": (104, 128, 164),
    "warning": hex_rgb(PALETTE["warning"]),
    "critical": hex_rgb(PALETTE["critical"]),
    "recovery": hex_rgb(PALETTE["recovery"]),
    "dormant": (56, 66, 86),
}

STATE_TOP: dict[str, RGB] = {
    "healthy": (38, 47, 66),
    "warning": (96, 74, 30),
    "critical": (118, 40, 34),
    "recovery": (22, 84, 76),
    "dormant": (24, 29, 41),
}

STATE_LABEL: dict[str, str] = {
    "warning": "degraded",
    "critical": "failing",
    "recovery": "recovering",
}

EDGE_TRACE: RGB = (94, 214, 186)  # the healthy request pulse inside a service
PARTICLE_TRACE: RGB = (150, 236, 214)

SEVERITY_COLOR: dict[str, RGB] = {
    "info": ACCENT,
    "warning": STATE_STROKE["warning"],
    "critical": STATE_STROKE["critical"],
    "recovery": STATE_STROKE["recovery"],
}


def face_shades(top: RGB) -> tuple[RGB, RGB, RGB]:
    """(top, front, side) shades for a slab, light from the upper left."""
    front = (int(top[0] * 0.72), int(top[1] * 0.72), int(top[2] * 0.74))
    side = (int(top[0] * 0.52), int(top[1] * 0.52), int(top[2] * 0.56))
    return top, front, side
