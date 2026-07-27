"""Easing and interpolation helpers for the cinematic renderer.

Every animation in the video is a pure function of time, so all of these are
stateless. Inputs are clamped; callers never have to guard the edges.
"""

from __future__ import annotations

import math


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def smoothstep(t: float) -> float:
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)


def ease_in_out_quint(t: float) -> float:
    """The camera curve: long gentle tails, decisive middle."""
    t = clamp(t)
    if t < 0.5:
        return 16.0 * t**5
    return 1.0 - ((-2.0 * t + 2.0) ** 5) / 2.0


def ease_out_cubic(t: float) -> float:
    t = clamp(t)
    return 1.0 - (1.0 - t) ** 3


def ease_out_back(t: float, overshoot: float = 1.70158) -> float:
    """Settles with a small overshoot — used for the failure 'lift'."""
    t = clamp(t)
    c3 = overshoot + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + overshoot * (t - 1.0) ** 2


def pulse(time_s: float, hz: float = 1.1, lo: float = 0.35, hi: float = 1.0) -> float:
    """Steady sinusoidal pulse in [lo, hi] — the heartbeat of a critical node."""
    s = 0.5 + 0.5 * math.sin(2.0 * math.pi * hz * time_s)
    return lerp(lo, hi, s)


def mix_color(
    a: tuple[int, int, int], b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    t = clamp(t)
    return (
        int(round(lerp(a[0], b[0], t))),
        int(round(lerp(a[1], b[1], t))),
        int(round(lerp(a[2], b[2], t))),
    )


def scale_color(c: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    return (
        max(0, min(255, int(c[0] * k))),
        max(0, min(255, int(c[1] * k))),
        max(0, min(255, int(c[2] * k))),
    )


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def with_alpha(c: tuple[int, int, int], alpha: float) -> tuple[int, int, int, int]:
    return (c[0], c[1], c[2], max(0, min(255, int(round(alpha * 255)))))


def frac(x: float) -> float:
    return x - math.floor(x)
