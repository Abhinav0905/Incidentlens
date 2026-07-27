"""World construction: the architecture graph as a 3D scene.

The dependency layout is the same one the web UI and the classic renderer use
(``studio.frames.compute_layout``), mapped onto a ground plane. X runs from
user-facing services (left) into deep dependencies (right), Z spreads the rows
of each dependency column, Y is up. Everything downstream — camera fitting,
edge paths, particles — works in these world units.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from incidentlens.domain.models import ArchitectureGraph
from incidentlens.studio import frames

# One graph-pixel of the 2D layout equals this many world units.
WORLD_PER_PX = 1.0 / 72.0
# The 2D layout packs rows tighter than columns; stretch Z so 3D labels breathe.
Z_STRETCH = 1.8

NODE_HEIGHT = 0.62  # slab height (world units)
NODE_DEPTH = 1.05  # slab depth along Z
EDGE_LIFT = 0.10  # edges float just above the floor
CRITICAL_LIFT = 0.42  # how high a critical node rises
WARNING_LIFT = 0.16


@dataclass(frozen=True)
class Node3D:
    name: str
    owner: str
    user_facing: bool
    cx: float  # world X of the slab centre
    cz: float  # world Z of the slab centre
    half_w: float
    half_d: float = NODE_DEPTH / 2.0
    height: float = NODE_HEIGHT


@dataclass(frozen=True)
class Edge3D:
    src: str  # service (depends on dst)
    dst: str  # dependency
    points: list[tuple[float, float, float]]  # sampled centreline, world coords
    length: float
    key: tuple[str, str] = field(default=("", ""))  # sorted pair for propagation match


@dataclass(frozen=True)
class World:
    nodes: dict[str, Node3D]
    edges: list[Edge3D]
    bounds: tuple[float, float, float, float]  # min_x, min_z, max_x, max_z


def _bezier(
    p0: tuple[float, float, float],
    p1: tuple[float, float, float],
    p2: tuple[float, float, float],
    p3: tuple[float, float, float],
    samples: int,
) -> list[tuple[float, float, float]]:
    pts: list[tuple[float, float, float]] = []
    for i in range(samples + 1):
        t = i / samples
        mt = 1.0 - t
        a = mt * mt * mt
        b = 3.0 * mt * mt * t
        c = 3.0 * mt * t * t
        d = t * t * t
        pts.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
                a * p0[2] + b * p1[2] + c * p2[2] + d * p3[2],
            )
        )
    return pts


def _polyline_length(points: list[tuple[float, float, float]]) -> float:
    total = 0.0
    for a, b in zip(points, points[1:], strict=False):
        total += math.dist(a, b)
    return total


def build_world(architecture: ArchitectureGraph) -> World:
    layout = frames.compute_layout(architecture)

    # Centre the whole scene on the origin so the camera math stays simple.
    xs = [b.x for b in layout.boxes.values()] + [b.x + b.w for b in layout.boxes.values()]
    ys = [b.y for b in layout.boxes.values()] + [b.y + b.h for b in layout.boxes.values()]
    mid_x = (min(xs) + max(xs)) / 2.0
    mid_y = (min(ys) + max(ys)) / 2.0

    nodes: dict[str, Node3D] = {}
    for svc in architecture.services:
        box = layout.boxes[svc.name]
        cx = (box.x + box.w / 2.0 - mid_x) * WORLD_PER_PX
        cz = (box.y + box.h / 2.0 - mid_y) * WORLD_PER_PX * Z_STRETCH
        half_w = max(0.85, box.w * WORLD_PER_PX / 2.0)
        nodes[svc.name] = Node3D(
            name=svc.name,
            owner=svc.owner or "",
            user_facing=svc.user_facing,
            cx=cx,
            cz=cz,
            half_w=half_w,
        )

    edges: list[Edge3D] = []
    for svc in architecture.services:
        src = nodes.get(svc.name)
        if src is None:
            continue
        for dep in svc.depends_on:
            dst = nodes.get(dep)
            if dst is None:
                continue
            p0 = (src.cx + src.half_w, EDGE_LIFT, src.cz)
            p3 = (dst.cx - dst.half_w, EDGE_LIFT, dst.cz)
            bend = max(0.9, (p3[0] - p0[0]) * 0.45)
            p1 = (p0[0] + bend, EDGE_LIFT, p0[2])
            p2 = (p3[0] - bend, EDGE_LIFT, p3[2])
            pts = _bezier(p0, p1, p2, p3, samples=30)
            edges.append(
                Edge3D(
                    src=svc.name,
                    dst=dep,
                    points=pts,
                    length=_polyline_length(pts),
                    key=tuple(sorted((svc.name, dep))),
                )
            )

    min_x = min(n.cx - n.half_w for n in nodes.values())
    max_x = max(n.cx + n.half_w for n in nodes.values())
    min_z = min(n.cz - n.half_d for n in nodes.values())
    max_z = max(n.cz + n.half_d for n in nodes.values())
    return World(nodes=nodes, edges=edges, bounds=(min_x, min_z, max_x, max_z))


def point_along(edge: Edge3D, t: float) -> tuple[float, float, float]:
    """Position at parameter t in [0, 1] along the sampled centreline."""
    if t <= 0.0:
        return edge.points[0]
    if t >= 1.0:
        return edge.points[-1]
    target = t * edge.length
    walked = 0.0
    for a, b in zip(edge.points, edge.points[1:], strict=False):
        seg = math.dist(a, b)
        if walked + seg >= target and seg > 0.0:
            k = (target - walked) / seg
            return (
                a[0] + (b[0] - a[0]) * k,
                a[1] + (b[1] - a[1]) * k,
                a[2] + (b[2] - a[2]) * k,
            )
        walked += seg
    return edge.points[-1]
