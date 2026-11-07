"""Phone-zone geometry in normalised image coordinates (0..1, origin top-left)."""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = tuple[float, float]


def point_in_polygon(x: float, y: float, poly: Sequence[Point]) -> bool:
    """Even-odd ray casting. Points exactly on an edge may go either way; callers use
    ``signed_distance`` with a margin, so the boundary case never decides alone."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _segment_distance(px: float, py: float, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def signed_distance(x: float, y: float, poly: Sequence[Point]) -> float:
    """Distance to the polygon boundary: negative inside, positive outside."""
    d = min(_segment_distance(x, y, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))
    return -d if point_in_polygon(x, y, poly) else d


def polygon_area(poly: Sequence[Point]) -> float:
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def centroid(poly: Sequence[Point]) -> Point:
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))
