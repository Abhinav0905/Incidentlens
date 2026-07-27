"""The cinematic frame engine.

One continuous 3D shot of the architecture, rendered as a pure function of
time: a perspective camera glides between the services the narration is
talking about, request particles stream along dependency edges, nodes morph,
lift and pulse as their state changes, failures ripple shockwaves across the
floor, and a bloom pass makes the hot elements glow. No randomness that isn't
seeded, no state that isn't derived from ``Timeline`` — identical inputs give
identical videos.

Everything is software-rendered (numpy + Pillow) and piped to ffmpeg by the
caller, so the only system dependency stays ffmpeg itself.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from incidentlens.domain.models import ArchitectureGraph, IncidentAnalysis
from incidentlens.studio.cinema import hud, palette
from incidentlens.studio.cinema.camera import (
    CameraKey,
    CameraState,
    CameraTrack,
    Projector,
    frame_shot,
)
from incidentlens.studio.cinema.easing import (
    clamp,
    ease_out_back,
    ease_out_cubic,
    frac,
    mix_color,
    pulse,
    with_alpha,
)
from incidentlens.studio.cinema.fonts import font
from incidentlens.studio.cinema.timeline import Timeline
from incidentlens.studio.cinema.world import (
    CRITICAL_LIFT,
    WARNING_LIFT,
    Edge3D,
    Node3D,
    World,
    build_world,
    point_along,
)


@dataclass(frozen=True)
class RenderSpec:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    supersample: float = 1.5

    @property
    def ss_size(self) -> tuple[int, int]:
        w = int(round(self.width * self.supersample / 2.0)) * 2
        h = int(round(self.height * self.supersample / 2.0)) * 2
        return w, h


PROFILES: dict[str, RenderSpec] = {
    "high": RenderSpec(1920, 1080, 30, 1.5),
    "preview": RenderSpec(1280, 720, 24, 1.0),
    "ultra": RenderSpec(2560, 1440, 30, 1.5),
}


def _edge_phase(edge: Edge3D, k: int) -> float:
    digest = hashlib.md5(f"{edge.src}->{edge.dst}:{k}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


class CinematicScene:
    """Renders frame ``t`` of the incident movie."""

    def __init__(
        self,
        analysis: IncidentAnalysis,
        architecture: ArchitectureGraph,
        timeline: Timeline,
        spec: RenderSpec,
    ) -> None:
        self.analysis = analysis
        self.timeline = timeline
        self.spec = spec
        self.world: World = build_world(architecture)
        self.ssf = spec.ss_size[1] / 1080.0  # supersampled HUD-unit
        self._bg = self._build_background()
        self._all_fit_points = self._fit_points(list(self.world.nodes))
        self._track = self._build_camera_track()

    # ------------------------------------------------------------ background

    def _build_background(self) -> Image.Image:
        w, h = self.spec.ss_size
        y = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
        top = np.array(palette.BG_TOP, dtype=np.float32)
        bottom = np.array(palette.BG_BOTTOM, dtype=np.float32)
        grad = top[None, None, :] * (1.0 - y[..., None]) + bottom[None, None, :] * y[..., None]
        xs = np.linspace(-1.0, 1.0, w, dtype=np.float32)[None, :]
        ys = np.linspace(-1.0, 1.0, h, dtype=np.float32)[:, None]
        r2 = (xs * xs * 1.1 + ys * ys) / 2.1
        vignette = 1.0 - 0.16 * np.clip(r2, 0.0, 1.0) ** 1.5
        img = grad * vignette[..., None]
        # ±1 LSB ordered dither so the dark gradient never bands after H.264.
        rng = np.random.default_rng(7)
        img += rng.uniform(-1.0, 1.0, size=img.shape).astype(np.float32)
        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")

    # ------------------------------------------------------------ camera plan

    def _fit_points(self, names: list[str]) -> np.ndarray:
        pts: list[tuple[float, float, float]] = []
        for name in names:
            node = self.world.nodes[name]
            for sx in (-1.0, 1.0):
                for sz in (-1.0, 1.0):
                    pts.append(
                        (node.cx + sx * node.half_w * 1.2, 0.0, node.cz + sz * node.half_d * 2.4)
                    )
            pts.append((node.cx, node.height + 1.5, node.cz))  # label headroom
        return np.array(pts, dtype=np.float64)

    # Safe rects keep the subject clear of the header (top) and caption card
    # (bottom ~28%) — fractions of the frame: x0, y0, x1, y1.
    _RECT_WIDE = (0.09, 0.14, 0.91, 0.76)
    _RECT_FOCUS = (0.20, 0.22, 0.80, 0.66)

    def _key_for(self, names: list[str], *, index: int, wide: bool) -> CameraState:
        w, h = self.spec.ss_size
        names = [n for n in names if n in self.world.nodes] or list(self.world.nodes)
        pts = self._fit_points(names) if not wide else self._all_fit_points
        tx = float(pts[:, 0].mean())
        tz = float(pts[:, 2].mean())
        yaw = -33.0 + 10.0 * math.sin(index * 1.9)
        pitch = 32.5 + 2.5 * math.sin(index * 1.3)
        state = CameraState(tx=tx, tz=tz, distance=20.0, yaw_deg=yaw, pitch_deg=pitch)
        rect = self._RECT_WIDE if wide else self._RECT_FOCUS
        return frame_shot(state, pts, w, h, rect)

    def _build_camera_track(self) -> CameraTrack:
        keys: list[CameraKey] = []
        for span in self.timeline.spans:
            beat = span.beat
            if beat.kind == "intro":
                wide = self._key_for([], index=0, wide=True)
                start = CameraState(
                    tx=wide.tx, tz=wide.tz, distance=wide.distance * 1.45,
                    yaw_deg=wide.yaw_deg - 14.0, pitch_deg=wide.pitch_deg + 9.0,
                )
                keys.append(CameraKey(time=span.start, state=start, transition=0.0))
                keys.append(
                    CameraKey(
                        time=span.start + min(2.6, span.duration * 0.55),
                        state=wide,
                        transition=min(2.6, span.duration * 0.55),
                    )
                )
                continue
            if beat.kind == "outro":
                state = self._key_for([], index=span.index, wide=True)
            else:
                services = []
                if 0 <= beat.timeline_index < len(self.analysis.timeline):
                    services = list(self.analysis.timeline[beat.timeline_index].services)
                state = self._key_for(services, index=span.index, wide=not services)
            keys.append(
                CameraKey(
                    time=span.start,
                    state=state,
                    transition=min(1.5, span.duration * 0.42),
                )
            )
        return CameraTrack(keys)

    # ------------------------------------------------------------ frame parts

    def _draw_grid(self, draw: ImageDraw.ImageDraw, proj: Projector, cam: CameraState) -> None:
        min_x, min_z, max_x, max_z = self.world.bounds
        x_lo, x_hi = math.floor(min_x - 9), math.ceil(max_x + 9)
        z_lo, z_hi = math.floor(min_z - 9), math.ceil(max_z + 9)
        width = max(1, int(1.0 * self.ssf))
        for gx in range(x_lo, x_hi + 1):
            a = proj.project_one(gx, 0.0, z_lo)
            b = proj.project_one(gx, 0.0, z_hi)
            if a[2] <= 0.3 or b[2] <= 0.3:
                continue
            d = math.hypot(gx - cam.tx, 0.0)
            alpha = clamp(0.55 - d / 30.0, 0.06, 0.5)
            draw.line((a[0], a[1], b[0], b[1]),
                      fill=with_alpha(palette.GRID_LINE, alpha), width=width)
        for gz in range(z_lo, z_hi + 1):
            a = proj.project_one(x_lo, 0.0, gz)
            b = proj.project_one(x_hi, 0.0, gz)
            if a[2] <= 0.3 or b[2] <= 0.3:
                continue
            d = abs(gz - cam.tz)
            alpha = clamp(0.55 - d / 30.0, 0.06, 0.5)
            draw.line((a[0], a[1], b[0], b[1]),
                      fill=with_alpha(palette.GRID_LINE, alpha), width=width)

    def _floor_circle(self, proj: Projector, cx: float, cz: float, radius: float,
                      samples: int = 40) -> list[tuple[float, float]] | None:
        angles = np.linspace(0.0, 2.0 * math.pi, samples, endpoint=False)
        pts = np.stack(
            [cx + radius * np.cos(angles),
             np.full(samples, 0.02),
             cz + radius * np.sin(angles)], axis=1)
        screen, depth = proj.project(pts)
        if np.any(depth <= 0.25):
            return None
        return [(float(x), float(y)) for x, y in screen]

    def _draw_floor_glow(self, gdraw: ImageDraw.ImageDraw, proj: Projector) -> None:
        min_x, min_z, max_x, max_z = self.world.bounds
        cx, cz = (min_x + max_x) / 2.0, (min_z + max_z) / 2.0
        radius = max(max_x - min_x, max_z - min_z) * 0.72
        ring = self._floor_circle(proj, cx, cz, radius, samples=48)
        if ring:
            gdraw.polygon(ring, fill=with_alpha(palette.FLOOR_GLOW, 0.10))

    def _edge_style(self, edge: Edge3D, t: float) -> tuple[bool, float]:
        return self.timeline.edge_active_at(edge.key, t)

    def _edge_colors(
        self, edge: Edge3D, t: float
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """(active edge color, active particle color) — hot by default;
        the internal trace scene overrides this with the cool pulse."""
        return palette.EDGE_ACTIVE, palette.PARTICLE_ACTIVE

    def _particles_reversed(self) -> bool:
        """Macro propagation pressure travels dependency -> service; the
        internal request pulse travels with the flow."""
        return True

    def _draw_scene_extras(self, draw: ImageDraw.ImageDraw, gdraw: ImageDraw.ImageDraw,
                           proj: Projector, t: float) -> None:
        """Hook for subclasses, drawn under the nodes (e.g. a floor plate)."""

    def _draw_scene_overlay(self, draw: ImageDraw.ImageDraw, gdraw: ImageDraw.ImageDraw,
                            proj: Projector, t: float) -> None:
        """Hook for subclasses, drawn over the nodes (e.g. annotations)."""

    def _draw_edges(self, draw: ImageDraw.ImageDraw, gdraw: ImageDraw.ImageDraw,
                    proj: Projector, t: float) -> None:
        for edge in self.world.edges:
            pts = np.array(edge.points, dtype=np.float64)
            screen, depth = proj.project(pts)
            if np.all(depth <= 0.25):
                continue
            coords = [(float(x), float(y)) for x, y in screen]
            d_avg = float(depth.mean())
            s = proj.scale_at(d_avg)
            active, since = self._edge_style(edge, t)
            edge_color, _particle_color = self._edge_colors(edge, t)
            if not active:
                wdt = max(1, int(clamp(0.034 * s, 1.4, 3.6 * self.ssf)))
                alpha = clamp(1.45 - d_avg / 40.0, 0.30, 0.68)
                draw.line(coords, fill=with_alpha(palette.EDGE_IDLE, alpha),
                          width=wdt, joint="curve")
            else:
                sweep = ease_out_cubic(clamp(since / 0.9))
                n_draw = max(2, int(len(coords) * sweep))
                seg = coords[:n_draw]
                wdt = max(2, int(clamp(0.052 * s, 1.6, 5.2 * self.ssf)))
                draw.line(seg, fill=with_alpha(edge_color, 0.92),
                          width=wdt, joint="curve")
                gdraw.line(seg, fill=with_alpha(edge_color, 0.75),
                           width=int(wdt * 2.4), joint="curve")
            # arrowhead into the dependency
            tip, prev = screen[-1], screen[-3]
            vx, vy = tip[0] - prev[0], tip[1] - prev[1]
            norm = math.hypot(vx, vy) or 1.0
            vx, vy = vx / norm, vy / norm
            px_, py_ = -vy, vx
            ah = clamp(0.11 * s, 4.0, 15.0 * self.ssf)
            color = edge_color if active else palette.EDGE_IDLE
            draw.polygon(
                [
                    (tip[0], tip[1]),
                    (tip[0] - vx * ah + px_ * ah * 0.55, tip[1] - vy * ah + py_ * ah * 0.55),
                    (tip[0] - vx * ah - px_ * ah * 0.55, tip[1] - vy * ah - py_ * ah * 0.55),
                ],
                fill=with_alpha(color, 0.85 if active else 0.5),
            )

    def _draw_particles(self, draw: ImageDraw.ImageDraw, gdraw: ImageDraw.ImageDraw,
                        proj: Projector, t: float) -> None:
        for edge in self.world.edges:
            active, since = self._edge_style(edge, t)
            _edge_color, particle_color = self._edge_colors(edge, t)
            n = max(2, min(9, int(edge.length / (1.1 if active else 2.2))))
            speed = (2.05 if active else 0.72) / max(0.8, edge.length)
            color = particle_color if active else palette.PARTICLE_IDLE
            for k in range(n):
                u = frac(_edge_phase(edge, k) + t * speed)
                if active and self._particles_reversed():
                    u = 1.0 - u  # failure pressure travels dependency -> service
                pos = point_along(edge, u)
                sx, sy, dz = proj.project_one(*pos)
                if dz <= 0.25:
                    continue
                s = proj.scale_at(dz)
                r = clamp((0.085 if active else 0.055) * s, 1.4, 9.0 * self.ssf)
                fade = math.sin(u * math.pi) ** 0.7  # ease in/out of the endpoints
                draw.ellipse((sx - r, sy - r, sx + r, sy + r),
                             fill=with_alpha(color, (0.95 if active else 0.6) * fade))
                gr = r * (2.1 if active else 1.6)
                gdraw.ellipse((sx - gr, sy - gr, sx + gr, sy + gr),
                              fill=with_alpha(color, (0.55 if active else 0.22) * fade))

    def _draw_shockwaves(self, draw: ImageDraw.ImageDraw, gdraw: ImageDraw.ImageDraw,
                         proj: Projector, t: float) -> None:
        for name, changes in self.timeline.node_changes.items():
            node = self.world.nodes.get(name)
            if node is None:
                continue
            for change in changes:
                k = (t - change.at) / 1.35
                if not 0.0 <= k <= 1.0:
                    continue
                radius = 0.7 + 3.1 * ease_out_cubic(k)
                alpha = (1.0 - k) ** 1.4 * 0.8
                ring = self._floor_circle(proj, node.cx, node.cz, radius)
                if ring is None:
                    continue
                color = palette.STATE_STROKE.get(change.state, palette.ACCENT)
                width = max(1, int((1.0 - k) * 4.0 * self.ssf))
                draw.line(ring + ring[:1], fill=with_alpha(color, alpha), width=width,
                          joint="curve")
                gdraw.line(ring + ring[:1], fill=with_alpha(color, alpha * 0.8),
                           width=width * 3)

    # ------------------------------------------------------------------ nodes

    def _node_lift(self, state: str, prev: str, blend: float, since: float) -> float:
        target = {"critical": CRITICAL_LIFT, "warning": WARNING_LIFT}.get(state, 0.0)
        source = {"critical": CRITICAL_LIFT, "warning": WARNING_LIFT}.get(prev, 0.0)
        if target > source:
            return source + (target - source) * ease_out_back(clamp(since / 0.65))
        return source + (target - source) * ease_out_cubic(blend)

    def _draw_nodes(self, img: Image.Image, draw: ImageDraw.ImageDraw,
                    gdraw: ImageDraw.ImageDraw, proj: Projector, t: float) -> None:
        # depth-sort far to near on projected centre depth
        order = []
        for name, node in self.world.nodes.items():
            _, _, dz = proj.project_one(node.cx, node.height / 2.0, node.cz)
            order.append((dz, name))
        order.sort(reverse=True)

        labels: list[tuple[int, float, Node3D, str, float]] = []
        eye = proj.state.eye()
        for dz_center, name in order:
            if dz_center <= 0.3:
                continue
            node = self.world.nodes[name]
            state, prev, blend, since = self.timeline.node_state_at(name, t)
            k = ease_out_cubic(blend)
            top_col = mix_color(palette.STATE_TOP.get(prev, palette.STATE_TOP["healthy"]),
                                palette.STATE_TOP.get(state, palette.STATE_TOP["healthy"]), k)
            stroke = mix_color(palette.STATE_STROKE.get(prev, palette.STATE_STROKE["healthy"]),
                               palette.STATE_STROKE.get(state, palette.STATE_STROKE["healthy"]), k)
            lift = self._node_lift(state, prev, blend, since)
            top_c, front_c, side_c = palette.face_shades(top_col)

            hw, hd, hh = node.half_w, node.half_d, node.height
            y0, y1 = lift, lift + hh
            corners = np.array(
                [
                    [node.cx - hw, y0, node.cz - hd], [node.cx + hw, y0, node.cz - hd],
                    [node.cx + hw, y0, node.cz + hd], [node.cx - hw, y0, node.cz + hd],
                    [node.cx - hw, y1, node.cz - hd], [node.cx + hw, y1, node.cz - hd],
                    [node.cx + hw, y1, node.cz + hd], [node.cx - hw, y1, node.cz + hd],
                ],
                dtype=np.float64,
            )
            screen, depth = proj.project(corners)
            if np.any(depth <= 0.25):
                continue
            pt = [(float(x), float(y)) for x, y in screen]

            # shadow on the floor, offset with the light, spreading as the node lifts
            sh = self._floor_shadow(proj, node, lift)
            if sh:
                draw.polygon(sh, fill=with_alpha(palette.SHADOW, 0.42 - lift * 0.25))

            faces = {  # name -> (corner idx, outward normal)
                "z-": ((0, 1, 5, 4), (0.0, 0.0, -1.0)),
                "z+": ((3, 2, 6, 7), (0.0, 0.0, 1.0)),
                "x-": ((0, 3, 7, 4), (-1.0, 0.0, 0.0)),
                "x+": ((1, 2, 6, 5), (1.0, 0.0, 0.0)),
            }
            visible = []
            for _fname, (idx, normal) in faces.items():
                centre = corners[list(idx)].mean(axis=0)
                view = eye - centre
                if float(np.dot(view, np.array(normal))) > 0.0:
                    shade = side_c if normal[0] != 0.0 else front_c
                    d_face = float(depth[list(idx)].mean())
                    visible.append((d_face, idx, shade))
            visible.sort(reverse=True)
            for _d, idx, shade in visible:
                draw.polygon([pt[i] for i in idx], fill=shade)
            # top face + outline
            top_idx = (4, 5, 6, 7)
            draw.polygon([pt[i] for i in top_idx], fill=top_c)
            s = proj.scale_at(dz_center)
            ow = max(1, int(clamp(0.022 * s, 1.0, 3.0 * self.ssf)))
            draw.line([pt[i] for i in (4, 5, 6, 7, 4)], fill=with_alpha(stroke, 0.95),
                      width=ow, joint="curve")
            # lit front-top edge
            draw.line([pt[4], pt[5]],
                      fill=with_alpha(mix_color(stroke, (255, 255, 255), 0.35), 0.5),
                      width=max(1, ow - 1))

            # emissive: state change flash + critical heartbeat
            emissive = 0.0
            if state in ("warning", "critical", "recovery"):
                emissive = 0.55 * (1.0 - clamp(since / 1.4))
            if state == "critical":
                emissive = max(emissive, 0.34 * pulse(t, hz=1.15))
            if emissive > 0.02:
                gdraw.polygon([pt[i] for i in top_idx], fill=with_alpha(stroke, emissive))

            # labels go on top of every slab; degraded nodes get the last word
            weight = 1 if state in ("warning", "critical", "recovery") else 0
            labels.append((weight, -dz_center, node, state, lift))

        labels.sort(key=lambda item: (item[0], item[1]))
        for _w, _negdz, node, state, lift in labels:
            self._draw_label(draw, proj, node, state, lift, t)

    def _floor_shadow(self, proj: Projector, node, lift: float) -> list[tuple[float, float]] | None:
        off = 0.16 + lift * 0.5
        hw = node.half_w * (1.04 + lift * 0.18)
        hd = node.half_d * (1.10 + lift * 0.22)
        quad = np.array(
            [
                [node.cx - hw + off, 0.012, node.cz - hd + off],
                [node.cx + hw + off, 0.012, node.cz - hd + off],
                [node.cx + hw + off, 0.012, node.cz + hd + off],
                [node.cx - hw + off, 0.012, node.cz + hd + off],
            ],
            dtype=np.float64,
        )
        screen, depth = proj.project(quad)
        if np.any(depth <= 0.25):
            return None
        return [(float(x), float(y)) for x, y in screen]

    def _sublabel(self, node, state: str, stroke) -> tuple[str, tuple[int, int, int]]:
        if state in palette.STATE_LABEL:
            return palette.STATE_LABEL[state], stroke
        bits = [node.owner] if node.owner else []
        if node.user_facing:
            bits.append("user-facing")
        return " · ".join(bits), palette.DIM

    def _draw_label(self, draw: ImageDraw.ImageDraw, proj: Projector, node,
                    state: str, lift: float, t: float) -> None:
        sx, sy, dz = proj.project_one(node.cx, lift + node.height + 0.42, node.cz)
        if dz <= 0.3:
            return
        # never let scene text collide with the HUD header band
        if sy < self.spec.ss_size[1] * 0.135:
            return
        s = proj.scale_at(dz)
        size = int(clamp(0.30 * s, 11 * self.ssf, 26 * self.ssf))
        fnt = font("mono-bold", size)
        name_w = draw.textlength(node.name, font=fnt)
        dot_r = size * 0.22
        total_w = name_w + dot_r * 2 + size * 0.45
        x0 = sx - total_w / 2.0
        cy = sy - size * 0.1
        stroke = palette.STATE_STROKE.get(state, palette.STATE_STROKE["healthy"])
        if state == "critical":
            halo = dot_r * (1.6 + 0.7 * pulse(t, hz=1.15))
            draw.ellipse((x0 + dot_r - halo, cy - halo, x0 + dot_r + halo, cy + halo),
                         fill=with_alpha(stroke, 0.22))
        draw.ellipse((x0, cy - dot_r, x0 + dot_r * 2, cy + dot_r), fill=stroke)
        tx = x0 + dot_r * 2 + size * 0.45
        draw.text((tx + 1.5, sy + 1.5), node.name, font=fnt, anchor="lm",
                  fill=(0, 0, 0, 170))
        draw.text((tx, sy), node.name, font=fnt, anchor="lm",
                  fill=with_alpha(palette.TEXT, 0.97))
        # secondary line: live state while degraded, otherwise ownership
        sub, sub_col = self._sublabel(node, state, stroke)
        if sub and size >= 12 * self.ssf:
            sfnt = font("mono", int(size * 0.72))
            draw.text((sx, sy + size * 0.95), sub, font=sfnt, anchor="mm",
                      fill=with_alpha(sub_col, 0.85))

    # ------------------------------------------------------------------ frame

    def scene_frame(self, t: float) -> Image.Image:
        """The 3D scene at supersampled resolution, bloom applied, no HUD."""
        cam = self._track.state_at(t)
        ssw, ssh = self.spec.ss_size
        proj = Projector(cam, ssw, ssh)

        img = self._bg.copy()
        draw = ImageDraw.Draw(img, "RGBA")
        glow = Image.new("RGBA", (ssw, ssh), (0, 0, 0, 0))
        gdraw = ImageDraw.Draw(glow, "RGBA")

        self._draw_grid(draw, proj, cam)
        self._draw_floor_glow(gdraw, proj)
        self._draw_scene_extras(draw, gdraw, proj, t)
        self._draw_edges(draw, gdraw, proj, t)
        self._draw_particles(draw, gdraw, proj, t)
        self._draw_shockwaves(draw, gdraw, proj, t)
        self._draw_nodes(img, draw, gdraw, proj, t)
        self._draw_scene_overlay(draw, gdraw, proj, t)

        # bloom: premultiply glow, blur at 1/3 scale, screen onto the base
        g = np.asarray(glow, dtype=np.uint16)
        pm = (g[..., :3] * g[..., 3:4] // 255).astype(np.uint8)
        small = Image.fromarray(pm, "RGB").resize((ssw // 3, ssh // 3), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(radius=6 * self.ssf / 1.5))
        bloom = small.resize((ssw, ssh), Image.BILINEAR)
        return ImageChops.screen(img, bloom)

    def frame(self, t: float) -> Image.Image:
        img = self.scene_frame(t)
        out = img.resize((self.spec.width, self.spec.height), Image.LANCZOS)
        hud.draw_hud(out, self.timeline, t)
        return out
