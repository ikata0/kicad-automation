"""Room on a flat sheet: what lies in a region, moving it out of the way, finding free space.

``kat stamp`` needs its region free of other drawing: a stamped pin landing on another
part's wire, label or pin would join nets silently. This module answers what is in the
way, moves it out on request and finds a free spot.

An *occupant* is something that moves as a whole: a framed block instance (its parts,
their wiring and the frame), a part with the wiring that hangs only off its pins (stubs,
labels, power symbols), or a sheet box with its pin wiring. *Loose* drawing belongs to no
single occupant (a wire between two parts, a free label); it is never moved automatically.
"""

from __future__ import annotations

from collections import defaultdict

from . import env
from .checks import GLYPH, LABEL_LIFT, text_items
from .schematic import Schematic, snap, text_width
from .sexpr import find, find_all, head

Box = tuple[float, float, float, float]
GAP = 2.54          # between a moved occupant and the region, and between occupants
PAGE_MARGIN = 10.0  # the drawing frame of the page


def _union(boxes) -> Box:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def hits(a: Box, b: Box, gap: float = 0.0) -> bool:
    """True when the boxes overlap or come closer than ``gap`` mm."""
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _shifted(b: Box, dx: float, dy: float) -> Box:
    return b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy


def _label_box(n: list) -> Box:
    at = find(n, "at")
    x, y, a = float(at[1]), float(at[2]), (float(at[3]) if len(at) > 3 else 0) % 360
    w, h = text_width(str(n[1])), 1.27 * GLYPH + LABEL_LIFT
    if head(n) != "label":
        w += 2.54                                  # the global/hierarchical label's arrow
    return {0: (x, y - h, x + w, y), 180: (x - w, y - h, x, y),
            90: (x - h, y - w, x, y), 270: (x - h, y, x, y + w)}.get(int(a), (x, y - h, x + w, y))


def _frame_nodes(s: Schematic) -> dict[str, tuple[list, list | None]]:
    """Frames drawn by ``kat frame``: {instance: (rectangle, heading text)}."""
    from .sch_stamp import _block_of, _det_uuid
    names = {"/".join(b.split("/")[:i]) for x in s.symbols() if (b := _block_of(x))
             for i in range(1, b.count("/") + 2)}
    by_uuid = {str(find(n, "uuid")[1]): n for n in s.tree
               if head(n) in ("rectangle", "text") and find(n, "uuid") is not None}
    out = {}
    for name in names:
        rect = by_uuid.get(_det_uuid(f"frame:{name}"))
        if rect is not None:
            out[name] = (rect, by_uuid.get(_det_uuid(f"frame-title:{name}")))
    return out


def _frame_box(rect: list, title: list | None) -> Box:
    st, en = find(rect, "start"), find(rect, "end")
    b = (min(float(st[1]), float(en[1])), min(float(st[2]), float(en[2])),
         max(float(st[1]), float(en[1])), max(float(st[2]), float(en[2])))
    if title is not None:
        at = find(title, "at")
        x, y = float(at[1]), float(at[2])
        b = _union([b, (x, y - 1.27 * GLYPH, x + text_width(str(title[1])), y)])
    return b


class Room:
    """The occupants of one sheet, computed once; ``skip``: parts (refs) that are not
    obstacles (the instances being stamped, freed of their wiring)."""

    def __init__(self, s: Schematic, skip: set[str] = frozenset(), skip_frames: set[str] = frozenset()):
        self.s, self.skip = s, set(skip)
        fields = defaultdict(list)
        for t in text_items(s):
            if t["owner"]:
                fields[t["owner"]].append(t["box"])
        self.owned: set[int] = set()
        parts = []                                   # (ref, unit, block, extent)
        for sym in s.symbols():
            if sym.ref.startswith("#"):
                continue
            boxes, own = self._runs(s.pins(sym), sym.node, fields)
            if (b := s.body_box(sym)):
                boxes.append(b)
            boxes += fields[sym.ref]
            self.owned |= own
            if sym.ref not in self.skip:
                parts.append((sym.ref, sym.unit, sym.field("Block") or "", _union(boxes)))
        frames = {k: v for k, v in _frame_nodes(s).items() if k not in skip_frames}
        # framed block instances move as a whole (the outermost frame)
        groups: dict[str, dict] = {}
        for ref, unit, block, ext in parts:
            parts_of = [p for p in ("/".join(block.split("/")[:i]) for i in range(1, block.count("/") + 2)) if p in frames] \
                if block else []
            key = f"block {parts_of[0]}" if parts_of else ref
            g = groups.setdefault(key, {"name": key.removeprefix("block "), "kind": "block" if parts_of else "part",
                                        "parts": [], "sheets": [], "frame": parts_of[0] if parts_of else None,
                                        "boxes": []})
            g["parts"].append((ref, unit))
            g["boxes"].append(ext)
        for name, g in groups.items():
            if g["frame"]:
                g["boxes"].append(_frame_box(*frames[g["frame"]]))
        for bx in s.sheet_boxes():
            pins = [{"x": p[0], "y": p[1]} for p in bx.pins().values()]
            boxes, own = self._runs(pins, bx.node, fields)
            self.owned |= own
            texts = [(float(find(p, "at")[1]), float(find(p, "at")[2]), str(p[2])) for p in find_all(bx.node, "property")]
            boxes += [(x, y - 1.27 * GLYPH, x + text_width(t) + 5.08, y + 1.27 * GLYPH) for x, y, t in texts]
            groups[f"sheet {bx.ref}"] = {"name": bx.ref, "kind": "sheet", "parts": [], "sheets": [bx.ref],
                                         "frame": None, "boxes": boxes + [bx.box()]}
        for g in groups.values():
            g["box"] = _union(g.pop("boxes"))
        self.groups = list(groups.values())

    def _runs(self, pins, owner, fields) -> tuple[list[Box], set[int]]:
        s = self.s
        boxes, own = [], set()
        for p in pins:
            taken, points, _ = s.private_run((p["x"], p["y"]), owner)
            marks, powers = s._items_at(points, exclude=owner)
            own |= {id(n) for n in taken + marks + [o.node for o in powers]}
            boxes += [(x, y, x, y) for x, y in points]
            boxes += [_label_box(n) for n in marks if head(n) != "no_connect"]
            for o in powers:
                if (b := s.body_box(o)):
                    boxes.append(b)
                boxes += fields.get(o.ref, [])
        return boxes, own

    def loose(self, region: Box) -> list[str]:
        """Drawing in ``region`` that belongs to no occupant (and so cannot be moved away)."""
        s, out = self.s, []
        for w, (a, b) in zip(s.wires(), s.segments()):
            if id(w) not in self.owned and hits(_union([(*a, *a), (*b, *b)]), region):
                out.append(f"wire {a[0]:g},{a[1]:g} - {b[0]:g},{b[1]:g}")
        for kind in ("label", "global_label", "hierarchical_label"):
            for n in find_all(s.tree, kind):
                if id(n) not in self.owned and hits(_label_box(n), region):
                    out.append(f'label "{n[1]}" at {float(find(n, "at")[1]):g},{float(find(n, "at")[2]):g}')
        for o in s.symbols():
            if o.ref.startswith("#") and id(o.node) not in self.owned and (b := s.body_box(o)) and hits(b, region):
                out.append(f"power symbol {o.field('Value')} ({o.ref})")
        return out

    def occupants(self, region: Box) -> list[dict]:
        """The occupants whose drawing reaches into ``region``."""
        return [g for g in self.groups if hits(g["box"], region)]

    def obstacles(self, but: dict | None = None) -> list[Box]:
        return [g["box"] for g in self.groups if g is not but]


def page_box(s: Schematic) -> Box:
    from .syncer import paper_size
    w, h = paper_size(s)
    return PAGE_MARGIN, PAGE_MARGIN, w - PAGE_MARGIN, h - PAGE_MARGIN


def inside(b: Box, outer: Box) -> bool:
    return b[0] >= outer[0] - 1e-6 and b[1] >= outer[1] - 1e-6 and b[2] <= outer[2] + 1e-6 and b[3] <= outer[3] + 1e-6


def describe(g: dict) -> str:
    if g["kind"] == "part":
        return g["name"]
    return f"{g['kind']} {g['name']}"


def make_room(s: Schematic, region: Box, skip: set[str] = frozenset(), reserved: list[Box] = (),
              skip_frames: set[str] = frozenset()) -> list[dict]:
    """Move every occupant of ``region`` out of it, each by the shortest straight shift
    (right, down, left or up, on the 2.54 mm grid) that lands it on the page, clear of the
    region, of ``reserved`` boxes and of everything else. Wires into shared nets are
    dragged; a framed instance gets its frame redrawn. Returns what moved, and by how
    much. KatError when loose wiring stays in the region or an occupant finds no room."""
    from .sch_stamp import frame
    moved = []
    page = page_box(s)
    room = Room(s, skip, skip_frames)
    for g in sorted(room.occupants(region), key=lambda g: (g["box"][1], g["box"][0])):
        b = g["box"]
        blocked = [x for x in room.obstacles(g) if not hits(x, region)] + [m["box"] for m in moved] \
            + list(reserved) + [region]
        cands = []
        for ux, uy, need in ((1, 0, region[2] - b[0]), (0, 1, region[3] - b[1]),
                             (-1, 0, b[2] - region[0]), (0, -1, b[3] - region[1])):
            d = snap_up(need + GAP)
            while True:
                nb = _shifted(b, ux * d, uy * d)
                if not inside(nb, page):
                    break
                if not any(hits(nb, x, GAP) for x in blocked):
                    cands.append((d, ux * d, uy * d))
                    break
                d += 5.08
        for _, dx, dy in sorted(cands):
            moves = [dict(ref=r, unit=u, dx=dx, dy=dy) for r, u in g["parts"]] + \
                [dict(ref=n, dx=dx, dy=dy) for n in g["sheets"]]
            try:
                s.move_symbols(moves, drag=True)
            except env.KatError:
                continue                      # a pin would land on something: next candidate
            if g["frame"]:
                frame(s, g["frame"])
            moved.append({"what": describe(g), "by": [dx, dy], "box": _shifted(b, dx, dy)})
            break
        else:
            raise env.KatError(f"no room to move {describe(g)} out of the way on {s.path.name}: "
                               f"make the page bigger (kat page A3) or stamp elsewhere")
    s.fix_junctions()
    left = Room(s, skip, skip_frames)
    rest = [describe(g) for g in left.occupants(region)] + left.loose(region)
    if rest:
        raise env.KatError(f"still in the way after making room: {', '.join(rest)}")
    return [{k: v for k, v in m.items() if k != "box"} for m in moved]


def snap_up(v: float, grid: float = 2.54) -> float:
    """``v`` rounded up to the grid."""
    import math
    return round(math.ceil(v / grid - 1e-9) * grid, 4)


def free_spot(s: Schematic, size: tuple[float, float], skip: set[str] = frozenset(),
              reserved: list[Box] = (), prefer: tuple[float, float] | None = None,
              skip_frames: set[str] = frozenset()) -> tuple[float, float]:
    """Top-left corner of a free ``size`` (w, h) mm area on the page, clear of everything
    drawn and of ``reserved``: ``prefer`` if that is free, else the first free place in
    reading order (5.08 mm steps)."""
    room = Room(s, skip, skip_frames)
    page = page_box(s)
    taken = room.obstacles() + list(reserved)
    loose_boxes = [_union([(*a, *a), (*b, *b)]) for w, (a, b) in zip(s.wires(), s.segments())
                   if id(w) not in room.owned]
    taken += loose_boxes

    def free(x: float, y: float) -> bool:
        b = (x, y, x + size[0], y + size[1])
        return inside(b, page) and not any(hits(b, t, GAP) for t in taken)

    if prefer is not None and free(snap(prefer[0]), snap(prefer[1])):
        return snap(prefer[0]), snap(prefer[1])
    y = snap_up(page[1] + GAP)
    while y + size[1] <= page[3]:
        x = snap_up(page[0] + GAP)
        while x + size[0] <= page[2]:
            if free(x, y):
                return x, y
            x += 5.08
        y += 5.08
    raise env.KatError(f"no free area of {size[0]:g} x {size[1]:g} mm on {s.path.name}: make the "
                       f"page bigger (kat page A3), make room, or pass at")
