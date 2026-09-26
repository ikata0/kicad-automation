"""Board geometry: KiCad's footprint transform, pad outlines, boxes and a spatial index.

KiCad stores a footprint's children in footprint coordinates, unrotated; for a footprint on
the bottom side they are already mirrored (y -> -y). Absolute = footprint position + the
local offset rotated by the footprint angle. Pad and text angles in the file are absolute.
Verified against kicad-cli's IPC-D-356 export (tests/test_pcb_geometry.py).
"""

from __future__ import annotations

import math
from collections import defaultdict

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in mm, y down


def rotate(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotate a local offset by a KiCad angle (counter-clockwise on screen, y down)."""
    if not deg % 360:
        return x, y
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def to_board(fx: float, fy: float, frot: float, lx: float, ly: float) -> tuple[float, float]:
    dx, dy = rotate(lx, ly, frot)
    return fx + dx, fy + dy


def rect_points(cx: float, cy: float, w: float, h: float, deg: float) -> list[tuple[float, float]]:
    return [(cx + dx, cy + dy) for dx, dy in
            (rotate(sx * w / 2, sy * h / 2, deg) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)))]


def pad_outline(shape: str, cx: float, cy: float, w: float, h: float, deg: float) -> list[tuple[float, float]]:
    """Copper outline as a polygon. Round rectangles count as rectangles (conservative),
    ovals and circles as 16-gons around their stadium; custom pads use their anchor size."""
    if shape in ("circle", "oval"):
        r = min(w, h) / 2
        half = (max(w, h) - min(w, h)) / 2  # stadium drawn along x, turned if taller than wide
        pts = []
        for i in range(16):
            a = 2 * math.pi * i / 16
            ex = half if math.cos(a) >= 0 else -half
            pts.append((r * math.cos(a) + ex, r * math.sin(a)))
        turn = deg + (0 if w >= h else 90)
        return [(cx + dx, cy + dy) for dx, dy in (rotate(x, y, turn) for x, y in pts)]
    return rect_points(cx, cy, w, h, deg)


def box_of(points) -> Box:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def union(boxes) -> Box | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def overlap(a: Box, b: Box, margin: float = 0.0) -> bool:
    """True if the boxes come closer than ``margin`` (negative: may touch by |margin|)."""
    return a[0] < b[2] + margin and b[0] < a[2] + margin and a[1] < b[3] + margin and b[1] < a[3] + margin


def box_distance(a: Box, b: Box) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0)
    dy = max(b[1] - a[3], a[1] - b[3], 0)
    return math.hypot(dx, dy)


class Index:
    """Bucket grid of boxes for "what is near here" queries."""

    def __init__(self, cell: float = 2.0):
        self.cell = cell
        self.grid: dict[tuple[int, int], list] = defaultdict(list)

    def _cells(self, b: Box):
        c = self.cell
        for i in range(math.floor(b[0] / c), math.floor(b[2] / c) + 1):
            for j in range(math.floor(b[1] / c), math.floor(b[3] / c) + 1):
                yield i, j

    def add(self, box: Box, item) -> None:
        for k in self._cells(box):
            self.grid[k].append((box, item))

    def query(self, box: Box, margin: float = 0.0) -> list:
        """Items whose box comes within ``margin`` of ``box``, each once."""
        grown = (box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin)
        seen, out = set(), []
        for k in self._cells(grown):
            for b, item in self.grid.get(k, ()):
                if id(item) not in seen and overlap(b, box, margin):
                    seen.add(id(item))
                    out.append(item)
        return out


# --- distances (clearance checks) --------------------------------------------------------

Point = tuple[float, float]


def point_seg(p: Point, a: Point, b: Point) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    L2 = (bx - ax) ** 2 + (by - ay) ** 2
    u = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / L2))
    return math.hypot(ax + u * (bx - ax) - px, ay + u * (by - ay) - py)


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segs_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def seg_seg(a: Point, b: Point, c: Point, d: Point) -> float:
    if segs_cross(a, b, c, d):
        return 0.0
    return min(point_seg(a, c, d), point_seg(b, c, d), point_seg(c, a, b), point_seg(d, a, b))


def inside(poly: list[Point], p: Point) -> bool:
    x, y = p
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def poly_seg(poly: list[Point], a: Point, b: Point) -> float:
    """Distance between a polygon (area) and a segment; 0 if they touch or overlap."""
    if inside(poly, a) or inside(poly, b):
        return 0.0
    return min(seg_seg(a, b, p, q) for p, q in zip(poly, poly[1:] + poly[:1]))
