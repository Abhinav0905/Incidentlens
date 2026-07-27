"""A pinhole camera over the world, plus the choreography track.

The camera orbits a target point on the floor: spherical coordinates
(yaw around Y, pitch above the horizon, distance), perspective projection.
Between narration beats the camera glides with an ease-in-out-quint curve;
during a hold it keeps a slow push-in and yaw drift so the frame always
breathes. All of it is a pure function of time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from incidentlens.studio.cinema.easing import clamp, ease_in_out_quint, lerp


@dataclass(frozen=True)
class CameraState:
    tx: float  # look-at target on the floor
    tz: float
    distance: float
    yaw_deg: float
    pitch_deg: float
    fov_deg: float = 34.0

    def eye(self) -> np.ndarray:
        yaw = math.radians(self.yaw_deg)
        pitch = math.radians(self.pitch_deg)
        cx = self.tx + self.distance * math.cos(pitch) * math.sin(yaw)
        cy = self.distance * math.sin(pitch)
        cz = self.tz + self.distance * math.cos(pitch) * math.cos(yaw)
        return np.array([cx, cy, cz], dtype=np.float64)


class Projector:
    """Projects world points through a CameraState onto a WxH raster."""

    def __init__(self, state: CameraState, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.state = state
        eye = state.eye()
        target = np.array([state.tx, 0.0, state.tz], dtype=np.float64)
        forward = target - eye
        forward /= np.linalg.norm(forward)
        world_up = np.array([0.0, 1.0, 0.0])
        right = np.cross(forward, world_up)
        right /= np.linalg.norm(right)
        up = np.cross(right, forward)
        self._eye = eye
        self._basis = np.stack([right, up, forward])  # rows
        self.focal = (height / 2.0) / math.tan(math.radians(state.fov_deg) / 2.0)

    def project(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(N,3) world -> ((N,2) screen, (N,) view depth). Depth <=0 is behind."""
        rel = points - self._eye
        cam = rel @ self._basis.T
        depth = cam[:, 2]
        safe = np.where(depth > 1e-6, depth, 1e-6)
        sx = self.width / 2.0 + self.focal * cam[:, 0] / safe
        sy = self.height / 2.0 - self.focal * cam[:, 1] / safe
        return np.stack([sx, sy], axis=1), depth

    def project_one(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        pts, depth = self.project(np.array([[x, y, z]], dtype=np.float64))
        return float(pts[0, 0]), float(pts[0, 1]), float(depth[0])

    def scale_at(self, depth: float) -> float:
        """Screen pixels per world unit at a given view depth."""
        return self.focal / max(1e-6, depth)


SafeRect = tuple[float, float, float, float]  # x0, y0, x1, y1 as frame fractions


def fit_distance(
    state: CameraState,
    points_world: np.ndarray,
    width: int,
    height: int,
    rect: SafeRect = (0.10, 0.12, 0.90, 0.88),
) -> float:
    """Distance at which all points project inside the safe rect.

    Solved by bisection against the real projection — robust against any
    yaw/pitch/fov combination, and cheap (a few dozen projections). The rect
    lets callers keep the scene clear of HUD areas (header, caption card).
    """
    x0, y0, x1, y1 = (rect[0] * width, rect[1] * height, rect[2] * width, rect[3] * height)
    lo, hi = 2.0, 240.0
    for _ in range(28):
        mid = (lo + hi) / 2.0
        proj = Projector(replace(state, distance=mid), width, height)
        pts, depth = proj.project(points_world)
        inside = (
            np.all(depth > 0.25)
            and np.all(pts[:, 0] >= x0) and np.all(pts[:, 0] <= x1)
            and np.all(pts[:, 1] >= y0) and np.all(pts[:, 1] <= y1)
        )
        if inside:
            hi = mid
        else:
            lo = mid
    return hi


def frame_shot(
    state: CameraState,
    points_world: np.ndarray,
    width: int,
    height: int,
    rect: SafeRect,
    min_distance: float = 7.5,
) -> CameraState:
    """Fit distance, then walk the look-at target so the subject sits centred
    in the safe rect rather than the raw frame centre (which the caption card
    and header would crowd)."""
    cx_target = (rect[0] + rect[2]) / 2.0 * width
    cy_target = (rect[1] + rect[3]) / 2.0 * height
    pitch = math.radians(state.pitch_deg)

    for _ in range(3):
        dist = max(min_distance, fit_distance(state, points_world, width, height, rect))
        state = replace(state, distance=dist)
        proj = Projector(state, width, height)
        pts, depth = proj.project(points_world)
        cx = (float(pts[:, 0].min()) + float(pts[:, 0].max())) / 2.0
        cy = (float(pts[:, 1].min()) + float(pts[:, 1].max())) / 2.0
        dx_px, dy_px = cx - cx_target, cy - cy_target
        if abs(dx_px) < 2.0 and abs(dy_px) < 2.0:
            break
        s = proj.scale_at(float(depth.mean()))
        # Floor-plane directions that map to screen x and screen y.
        right = proj._basis[0]
        forward = proj._basis[2]
        right_floor = np.array([right[0], 0.0, right[2]])
        right_floor /= max(1e-9, np.linalg.norm(right_floor))
        fwd_floor = np.array([forward[0], 0.0, forward[2]])
        fwd_floor /= max(1e-9, np.linalg.norm(fwd_floor))
        # +right_floor moves content left; +fwd_floor moves content down.
        shift = (dx_px / s) * right_floor - (dy_px / (s * max(0.2, math.sin(pitch)))) * fwd_floor
        state = replace(state, tx=state.tx + float(shift[0]), tz=state.tz + float(shift[2]))
    return state


@dataclass(frozen=True)
class CameraKey:
    time: float  # when this shot's hold begins
    state: CameraState
    transition: float  # seconds of glide leading into this key


class CameraTrack:
    """Piecewise camera path: glide into each key, drift during the hold."""

    def __init__(self, keys: list[CameraKey], drift_zoom: float = 0.045,
                 drift_yaw: float = 1.6) -> None:
        if not keys:
            raise ValueError("camera track needs at least one key")
        self.keys = sorted(keys, key=lambda k: k.time)
        self.drift_zoom = drift_zoom
        self.drift_yaw = drift_yaw

    @staticmethod
    def _blend(a: CameraState, b: CameraState, t: float) -> CameraState:
        return CameraState(
            tx=lerp(a.tx, b.tx, t),
            tz=lerp(a.tz, b.tz, t),
            distance=lerp(a.distance, b.distance, t),
            yaw_deg=lerp(a.yaw_deg, b.yaw_deg, t),
            pitch_deg=lerp(a.pitch_deg, b.pitch_deg, t),
            fov_deg=lerp(a.fov_deg, b.fov_deg, t),
        )

    def _drifted(self, key: CameraKey, hold_time: float, hold_len: float) -> CameraState:
        """The state during a key's hold: slow push-in plus a yaw drift."""
        if hold_len <= 0.0:
            return key.state
        p = clamp(hold_time / hold_len)
        zoom = 1.0 - self.drift_zoom * p
        yaw = key.state.yaw_deg + self.drift_yaw * (p - 0.5)
        return replace(key.state, distance=key.state.distance * zoom, yaw_deg=yaw)

    def state_at(self, t: float) -> CameraState:
        keys = self.keys
        if t <= keys[0].time:
            return keys[0].state
        for i in range(len(keys) - 1, -1, -1):
            key = keys[i]
            if t >= key.time:
                hold_end = keys[i + 1].time if i + 1 < len(keys) else t + 1.0
                into = t - key.time
                if into < key.transition and i > 0:
                    prev = keys[i - 1]
                    prev_hold = key.time - prev.time
                    frm = self._drifted(prev, prev_hold, prev_hold)
                    k = ease_in_out_quint(into / key.transition)
                    return self._blend(frm, self._drifted(key, 0.0, 1.0), k)
                hold_len = max(0.001, hold_end - key.time)
                return self._drifted(key, into, hold_len)
        return keys[0].state
