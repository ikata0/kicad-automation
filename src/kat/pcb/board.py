"""KiCad 10 board file (.kicad_pcb) on the lossless s-expression model.

Nets live on the pads by name (no net table in KiCad 10). Every save goes through
``kicad-cli pcb upgrade``: KiCad validates the file and writes its own formatting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from .. import env
from ..sexpr import Sym, dumps, find, find_all, head, loads, num
from . import footprints as fps
from .geometry import (Box, box_of, capsule_points, circle_points, hull, pad_outline, rotate, rounded_rect_points,
                       to_board, trapezoid_points, union)
from .finish import FinishMixin
from .placement import PlacementMixin
from .setup import SetupMixin
from .stamping import StampMixin
from .structure import StructureMixin

FORMAT = "20260206"
ZONE_CONNECT = {"0": "none", "1": "thermal", "2": "solid", "3": "thru_hole_only"}


def _zone_connection(node: list) -> str | None:
    n = find(node, "zone_connect")
    return ZONE_CONNECT.get(str(n[1])) if n is not None else None


def _set_zone_connection(node: list, mode: str | None, before: str) -> None:
    """Set or remove a pad's or footprint's own ``(zone_connect N)``, new ones before ``before``."""
    code = {v: k for k, v in ZONE_CONNECT.items()}
    if mode is not None and mode not in code:
        raise env.KatError(f"zone connection {mode!r}: {', '.join(code)} or None")
    n = find(node, "zone_connect")
    if mode is None:
        if n is not None:
            node.remove(n)
    elif n is not None:
        n[1] = Sym(code[mode])
    else:
        ref = find(node, before)
        node.insert(node.index(ref) if ref is not None else len(node), [Sym("zone_connect"), Sym(code[mode])])


def _f(node, key, i=1, default=0.0) -> float:
    n = find(node, key)
    return float(n[i]) if n is not None and len(n) > i else default


def _grown(points, half: float, max_error: float) -> list[tuple[float, float]]:
    """A filled polygon with a stroke of half width ``half``: the outline as is, when there is
    no stroke; otherwise its convex hull grown, which is never inside the real shape."""
    if half <= 0:
        return list(points)
    return hull(q for p in points for q in circle_points(*p, half, max_error))


@dataclass
class Pad:
    """A pad in board coordinates (mm, degrees): ``ref``, ``number``, ``net``, centre ``x``,
    ``y``, absolute ``angle``, ``shape``, size ``w`` x ``h``, ``kind`` (smd, thru_hole, ...),
    copper ``layers`` and ``drill``."""
    ref: str
    number: str
    net: str
    x: float
    y: float
    angle: float
    shape: str
    w: float
    h: float
    kind: str                 # smd, thru_hole, np_thru_hole, connect
    layers: tuple[str, ...]
    drill: float | None
    node: list = field(repr=False, compare=False)

    @property
    def zone_connection(self) -> str | None:
        """This pad's own way of joining a zone of its net: ``thermal``, ``solid``, ``none`` or
        ``thru_hole_only``; None if it follows its footprint's (``Footprint.zone_connection``)
        or else the zone."""
        return _zone_connection(self.node)

    def set_zone_connection(self, mode: str | None) -> None:
        """Set ``zone_connection``; None follows the footprint or zone again."""
        _set_zone_connection(self.node, mode, "uuid")

    def _value(self, key: str, default: float = 0.0) -> float:
        n = find(self.node, key)
        return float(n[1]) if n is not None and len(n) > 1 else default

    @property
    def offset(self) -> tuple[float, float]:
        """The copper's offset from the pad's position (the hole), in the pad's own frame, mm."""
        n = find(find(self.node, "drill") or [], "offset")
        return (float(n[1]), float(n[2])) if n is not None else (0.0, 0.0)

    @property
    def center(self) -> tuple[float, float]:
        """Centre of the copper in board mm: the pad's position plus its offset, turned."""
        dx, dy = rotate(*self.offset, self.angle)
        return self.x + dx, self.y + dy

    def _to_board(self, points) -> list[tuple[float, float]]:
        ox, oy = self.offset
        cx, cy = self.x, self.y
        return [(cx + dx, cy + dy) for dx, dy in (rotate(x + ox, y + oy, self.angle) for x, y in points)]

    def polygons(self, max_error: float = 0.001) -> list[list[tuple[float, float]]]:
        """The pad's copper as polygons in board mm, never inside the real shape and at most
        ``max_error`` mm outside it: one polygon, or for a custom pad the anchor and one per
        primitive (their union is the pad). ``geometry`` is the analytic description."""
        if self.shape == "custom":
            anchor = (circle_points(0, 0, self.w / 2, max_error) if self._anchor() == "circle"
                      else rounded_rect_points(self.w, self.h))
            return [self._to_board(anchor)] + [self._to_board(p) for p in self._primitive_polygons(max_error)]
        if self.shape == "trapezoid":
            return [self._to_board(trapezoid_points(self.w, self.h, *self._delta()))]
        if self.shape == "circle":
            return [self._to_board(circle_points(0, 0, self.w / 2, max_error))]
        if self.shape == "oval":
            r, half = min(self.w, self.h) / 2, abs(self.w - self.h) / 2
            a, b = ((-half, 0), (half, 0)) if self.w >= self.h else ((0, -half), (0, half))
            return [self._to_board(capsule_points(a, b, r, max_error))]
        g = self._corners()
        return [self._to_board(rounded_rect_points(self.w, self.h, g["radius"], g["chamfer"], g["corners"], max_error))]

    def _anchor(self) -> str:
        return str(find(find(self.node, "options") or [], "anchor")[1]) if (
            find(find(self.node, "options") or [], "anchor") is not None) else "rect"

    def _delta(self) -> tuple[float, float]:
        n = find(self.node, "rect_delta")
        return (float(n[1]), float(n[2])) if n is not None else (0.0, 0.0)

    def _corners(self) -> dict:
        size = min(self.w, self.h)
        n = find(self.node, "chamfer")
        corners = tuple(str(t) for t in n[1:]) if n is not None else ()
        return {"radius": self._value("roundrect_rratio") * size if self.shape == "roundrect" else 0.0,
                "chamfer": self._value("chamfer_ratio") * size if corners else 0.0, "corners": corners}

    def _primitives(self) -> list[dict]:
        """A custom pad's primitives, numbers in mm in the pad's own frame."""
        def pt(n, key):
            v = find(n, key)
            return (float(v[1]), float(v[2])) if v is not None else None
        out = []
        for c in (find(self.node, "primitives") or [])[1:]:
            if not isinstance(c, list):
                continue
            kind = str(c[0])
            width = float(find(c, "width")[1]) if find(c, "width") is not None else 0.0
            fill = find(c, "fill")
            e = {"kind": kind.removeprefix("gr_"), "width": width,
                 "filled": fill is not None and str(fill[1]) in ("yes", "solid")}
            if kind == "gr_poly":
                e["points"] = [(float(q[1]), float(q[2])) for q in find_all(find(c, "pts") or [], "xy")]
            elif kind == "gr_curve":
                e["control_points"] = [(float(q[1]), float(q[2])) for q in find_all(find(c, "pts") or [], "xy")]
            elif kind == "gr_circle":
                ctr, end = pt(c, "center"), pt(c, "end")
                e.update(center=ctr, radius=math.hypot(end[0] - ctr[0], end[1] - ctr[1]))
            else:
                e.update({k: v for k in ("start", "mid", "end") if (v := pt(c, k)) is not None})
            out.append(e)
        return out

    def _primitive_polygons(self, max_error: float) -> list[list[tuple[float, float]]]:
        from .coupling import _arc_points
        out = []
        for e in self._primitives():
            half = e["width"] / 2
            kind = e["kind"]
            if kind == "circle":
                out.append(circle_points(*e["center"], e["radius"] + half, max_error))
            elif kind == "line":
                out.append(capsule_points(e["start"], e["end"], half, max_error))
            elif kind == "rect":
                (x0, y0), (x1, y1) = e["start"], e["end"]
                out.append(hull(q for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
                                for q in circle_points(*p, half, max_error)) if half
                           else [(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            elif kind == "arc":
                pts = _arc_points(e["start"], e["mid"], e["end"], 32)
                out += [capsule_points(a, b, half, max_error) for a, b in zip(pts, pts[1:])]
            else:                              # poly, curve: the hull of a curve's control points
                pts = e.get("points") or e.get("control_points") or []
                if pts:
                    out.append(hull(q for p in pts for q in circle_points(*p, half, max_error)) if half and kind != "poly"
                               else _grown(pts, half, max_error))
        return out

    def geometry(self) -> dict:
        """The pad's exact shape for clearance and routing code outside kat, mm and degrees:
        ``ref``, ``number``, ``net``, ``kind``, ``layers``, position ``x``, ``y``, ``angle``,
        ``shape``, size ``w``, ``h``, copper ``center`` and ``offset``; ``drill`` ({shape, w,
        h}) if it has a hole; roundrect: ``corner_radius``, ``roundrect_rratio``; chamfers:
        ``chamfer_size``, ``chamfer_ratio``, ``chamfer`` (corner names); trapezoid:
        ``rect_delta``; custom: ``anchor`` and ``primitives`` (kind, width, filled and points in
        the pad's own frame, which turn by ``angle`` and start at ``center``); KiCad 9+
        per-layer shapes: ``padstack`` ({mode, layers}); ``zone_connection`` if it has its own.
        ``polygons`` gives the same as polygons."""
        out: dict = {"ref": self.ref, "number": self.number, "net": self.net, "kind": self.kind,
                     "layers": list(self.layers), "x": self.x, "y": self.y, "angle": self.angle,
                     "shape": self.shape, "w": self.w, "h": self.h, "center": list(self.center),
                     "offset": list(self.offset)}
        d = find(self.node, "drill")
        nums = [float(t) for t in (d or [])[1:] if not isinstance(t, list) and str(t) != "oval"]
        if nums:
            oval = any(str(t) == "oval" for t in d[1:])
            out["drill"] = {"shape": "oval" if oval else "circle", "w": nums[0], "h": nums[-1]}
        g = self._corners()
        if self.shape == "roundrect":
            out.update(corner_radius=g["radius"], roundrect_rratio=self._value("roundrect_rratio"))
        if g["corners"]:
            out.update(chamfer_size=g["chamfer"], chamfer_ratio=self._value("chamfer_ratio"),
                       chamfer=list(g["corners"]))
        if self.shape == "trapezoid":
            out["rect_delta"] = list(self._delta())
        if self.shape == "custom":
            out.update(anchor=self._anchor(), primitives=self._primitives())
        stack = find(self.node, "padstack")
        if stack is not None:
            layers = {}
            for lay in find_all(stack, "layer"):
                size = find(lay, "size")
                shape = find(lay, "shape")
                layers[str(lay[1])] = {**({"shape": str(shape[1])} if shape is not None else {}),
                                       **({"w": float(size[1]), "h": float(size[2])} if size is not None else {}),
                                       **({"roundrect_rratio": float(find(lay, "roundrect_rratio")[1])}
                                          if find(lay, "roundrect_rratio") is not None else {})}
            mode = find(stack, "mode")
            out["padstack"] = {"mode": str(mode[1]) if mode is not None else "normal", "layers": layers}
        if self.zone_connection:
            out["zone_connection"] = self.zone_connection
        return out

    @property
    def outline(self) -> list[tuple[float, float]]:
        """The pad's copper outline as one polygon in board mm, simple and never smaller than
        the pad: circles and ovals as 16-gons, round rectangles and chamfers as rectangles, a
        custom pad as the hull of its anchor and primitives. ``polygons`` is exact."""
        if self.shape == "custom":
            return hull(q for p in self.polygons(0.01) for q in p)
        if self.shape == "trapezoid":
            return self.polygons()[0]
        return pad_outline(self.shape, *self.center, self.w, self.h, self.angle)

    @property
    def box(self) -> Box:
        """Bounding box (x0, y0, x1, y1) of the pad's copper in board mm."""
        return box_of(self.outline)

    def _hole(self) -> tuple[float, tuple[float, float]] | None:
        """(radius, half axis of a slot in board mm) of the drilled hole; None without one."""
        d = find(self.node, "drill")
        nums = [float(t) for t in (d or [])[1:] if not isinstance(t, list) and str(t) != "oval"]
        if not nums:
            return None
        w, h = nums[0], nums[-1]
        half = abs(w - h) / 2
        return min(w, h) / 2, rotate(half, 0, self.angle + (0 if w >= h else 90))

    @property
    def hole_outline(self) -> list[tuple[float, float]] | None:
        """The drilled hole as a polygon in board mm, a slot's length and angle included, at
        most 0.01 mm outside it; None for a pad without a hole."""
        hole = self._hole()
        if hole is None:
            return None
        r, (dx, dy) = hole
        return capsule_points((self.x - dx, self.y - dy), (self.x + dx, self.y + dy), r, 0.01)

    @property
    def hole_box(self) -> Box | None:
        """Bounding box of the drilled hole in board mm; None for a pad without a hole."""
        hole = self._hole()
        if hole is None:
            return None
        r, (dx, dy) = hole
        ex, ey = abs(dx) + r, abs(dy) + r
        return self.x - ex, self.y - ey, self.x + ex, self.y + ey

    def on(self, layer: str) -> bool:
        """True if the pad has copper on ``layer`` (wildcards such as *.Cu resolved)."""
        return any(l == layer or (l.startswith("*.") and layer.endswith(l[1:]))
                   or (l == "F&B.Cu" and layer in ("F.Cu", "B.Cu")) for l in self.layers)


class Footprint:
    """A footprint on the board. Position ``x``, ``y`` in mm, ``rot`` in degrees, ``side``
    "F" or "B"; ``ref``, ``value``, ``lib_id``, ``path`` (link to the schematic symbol);
    ``locked``, with ``lock()`` / ``unlock()``."""

    def __init__(self, node: list):
        self.node = node

    lib_id = property(lambda self: str(self.node[1]), doc="Library id, ``Library:Footprint``.")
    uuid = property(lambda self: str(find(self.node, "uuid")[1]), doc="The footprint's uuid.")
    x = property(lambda self: _f(self.node, "at", 1), doc="Origin x in board mm.")
    y = property(lambda self: _f(self.node, "at", 2), doc="Origin y in board mm (y grows downwards).")
    rot = property(lambda self: _f(self.node, "at", 3), doc="Rotation in degrees (counter-clockwise).")
    side = property(lambda self: "B" if str(find(self.node, "layer")[1]).startswith("B.") else "F",
                    doc='"F" (top) or "B" (bottom).')
    ref = property(lambda self: self.field("Reference") or "", doc="Reference designator.")
    value = property(lambda self: self.field("Value") or "", doc="Value field.")

    @property
    def board_only(self) -> bool:
        """True for parts without a schematic symbol (holes, fiducials); sync ignores them."""
        attr = find(self.node, "attr")
        return attr is not None and any(str(t) == "board_only" for t in attr[1:])

    @property
    def locked(self) -> bool:
        """True if KiCad's lock flag is set: ``Board.move`` and ``stamp`` refuse to move it."""
        n = find(self.node, "locked")
        return n is not None and (len(n) < 2 or str(n[1]) != "no")

    def lock(self) -> None:
        """Set KiCad's lock flag: the checked moves (``Board.move``, ``align``, ...) and
        ``stamp`` then refuse to move the part."""
        n = find(self.node, "locked")
        if n is None:
            self.node.insert(2, [Sym("locked"), Sym("yes")])
        else:
            n[1:] = [Sym("yes")]

    def unlock(self) -> None:
        """Clear KiCad's lock flag."""
        n = find(self.node, "locked")
        if n is not None:
            self.node.remove(n)

    @property
    def zone_connection(self) -> str | None:
        """The footprint's default way for its pads to join a zone of their net (KiCad's
        footprint-level ``zone_connect``): ``thermal``, ``solid``, ``none`` or ``thru_hole_only``;
        None if they follow the zone. A pad's own ``Pad.zone_connection`` wins over it."""
        return _zone_connection(self.node)

    def set_zone_connection(self, mode: str | None) -> None:
        """Set ``zone_connection``; None lets the pads follow the zone again."""
        _set_zone_connection(self.node, mode, "attr")

    @property
    def path(self) -> str:
        """``/<symbol uuid>``: KiCad's link from the footprint to its schematic symbol."""
        p = find(self.node, "path")
        return str(p[1]) if p else ""

    def _prop(self, name: str):
        return next((p for p in find_all(self.node, "property") if str(p[1]) == name), None)

    def field(self, name: str) -> str | None:
        """Value of the property ``name``, or None."""
        p = self._prop(name)
        return str(p[2]) if p is not None else None

    def fields(self) -> dict[str, str]:
        """All properties, name -> value."""
        return {str(p[1]): str(p[2]) for p in find_all(self.node, "property")}

    def set_field(self, name: str, value: str, hide: bool | None = None) -> None:
        """Set a property. ``hide``: True / False hides or shows it; None leaves an existing
        one as it is and hides a new one (on the fab layer of the footprint's side)."""
        p = self._prop(name)
        if p is not None:
            p[2] = value
            if hide is not None:
                p[:] = [c for c in p if not (isinstance(c, list) and head(c) == "hide")]
                eff = find(p, "effects")
                if eff is not None:
                    eff[:] = [c for c in eff if str(c) != "hide"]
                if hide:
                    p.insert(next((i for i, c in enumerate(p) if isinstance(c, list) and head(c) == "uuid"),
                                  len(p)), [Sym("hide"), Sym("yes")])
            return
        from ..schematic import new_uuid
        effects = [Sym("effects"), [Sym("font"), [Sym("size"), Sym("1"), Sym("1")],
                                    [Sym("thickness"), Sym("0.15")]]]
        if self.side == "B":
            effects.append([Sym("justify"), Sym("mirror")])
        at = [Sym("at"), Sym("0"), Sym("0")] + ([num(self.rot)] if self.rot else [])
        new = [Sym("property"), name, value, at, [Sym("layer"), f"{self.side}.Fab"]]
        if hide is not False:
            new.append([Sym("hide"), Sym("yes")])
        new += [[Sym("uuid"), new_uuid()], effects]
        props = find_all(self.node, "property")
        idx = self.node.index(props[-1]) + 1 if props else 3
        self.node.insert(idx, new)

    def remove_field(self, name: str) -> None:
        """Delete the property ``name`` if present."""
        p = self._prop(name)
        if p is not None:
            self.node.remove(p)

    def to_board(self, lx: float, ly: float) -> tuple[float, float]:
        """Footprint-local point (mm) -> board coordinates (rotation applied)."""
        return to_board(self.x, self.y, self.rot, lx, ly)

    def pads(self) -> list[Pad]:
        """All pads in board coordinates."""
        out = []
        for p in find_all(self.node, "pad"):
            at, size = find(p, "at"), find(p, "size")
            x, y = self.to_board(float(at[1]), float(at[2]))
            net = find(p, "net")
            drill = find(p, "drill")
            dval = None
            if drill is not None:
                nums = [t for t in drill[1:] if not isinstance(t, list) and str(t) != "oval"]
                dval = float(nums[0]) if nums else None
            out.append(Pad(self.ref, str(p[1]), str(net[1]) if net is not None else "", x, y,
                           float(at[3]) if len(at) > 3 else 0.0, str(p[3]),
                           float(size[1]), float(size[2]), str(p[2]),
                           tuple(str(l) for l in (find(p, "layers") or [None])[1:]), dval, p))
        return out

    def pad(self, number: str) -> Pad:
        """The pad ``number`` (a string: "1", "A3", "EP"); KatError if missing."""
        for p in self.pads():
            if p.number == number:
                return p
        raise env.KatError(f"{self.ref} has no pad {number!r}")

    def graphics_box(self, layer_suffix: str) -> Box | None:
        """Box of the footprint's graphics on its side's ``layer_suffix`` layer (e.g. CrtYd)."""
        return union(self.graphics_boxes(layer_suffix))

    def graphics_boxes(self, layer_suffix: str) -> list[Box]:
        """One box per graphic item on its side's ``layer_suffix`` layer (e.g. SilkS)."""
        layer = f"{self.side}.{layer_suffix}"
        out = []
        for g in self.node:
            h = head(g)
            if not h or not h.startswith("fp_") or h in ("fp_text", "fp_text_box"):
                continue
            lay = find(g, "layer")
            if lay is None or str(lay[1]) != layer:
                continue
            if h == "fp_circle":
                c, e = find(g, "center"), find(g, "end")
                cx, cy = self.to_board(float(c[1]), float(c[2]))
                r = ((float(e[1]) - float(c[1])) ** 2 + (float(e[2]) - float(c[2])) ** 2) ** 0.5
                out.append((cx - r, cy - r, cx + r, cy + r))
                continue
            local = [(float(n[1]), float(n[2])) for k in ("start", "mid", "end") if (n := find(g, k))]
            if h == "fp_rect" and len(local) == 2:
                (x0, y0), (x1, y1) = local
                local = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if h == "fp_poly":
                local = [(float(xy[1]), float(xy[2])) for xy in find_all(find(g, "pts") or [], "xy")]
            if local:
                out.append(box_of([self.to_board(x, y) for x, y in local]))
        return out

    def courtyard(self) -> Box | None:
        """Courtyard bounding box in board mm, or None without a courtyard."""
        return self.graphics_box("CrtYd")

    def courtyard_outline(self) -> list[tuple[float, float]] | None:
        """The courtyard as a polygon in board mm, turned with the part: a courtyard drawn as one
        polygon is that polygon (concave too, e.g. an L), else the convex hull of its graphics
        (never smaller than the drawn courtyard; a rectangle stays that rectangle); None
        without a courtyard. ``courtyard`` is its bounding box, which at 45 degrees is much
        larger."""
        from .coupling import _arc_points, _pt, _pts
        layer = f"{self.side}.CrtYd"
        mine = [g for g in self.node if (h := head(g)) and h.startswith("fp_") and h not in ("fp_text", "fp_text_box")
                and (lay := find(g, "layer")) is not None and str(lay[1]) == layer]
        if len(mine) == 1 and head(mine[0]) == "fp_poly":
            return [self.to_board(x, y) for x, y in _pts(mine[0])]
        pts: list[tuple[float, float]] = []
        for g in self.node:
            h = head(g)
            if not h or not h.startswith("fp_") or h in ("fp_text", "fp_text_box"):
                continue
            lay = find(g, "layer")
            if lay is None or str(lay[1]) != layer:
                continue
            if h == "fp_circle":
                cx, cy = _pt(find(g, "center"))
                ex, ey = _pt(find(g, "end"))
                r = math.hypot(ex - cx, ey - cy) / math.cos(math.pi / 16)      # outside the circle
                pts += [(cx + r * math.cos(k * math.pi / 8), cy + r * math.sin(k * math.pi / 8))
                        for k in range(16)]
            elif h == "fp_poly":
                pts += _pts(g)
            elif h == "fp_arc":
                pts += _arc_points(_pt(find(g, "start")), _pt(find(g, "mid")), _pt(find(g, "end")))
            elif h == "fp_rect":
                (x0, y0), (x1, y1) = _pt(find(g, "start")), _pt(find(g, "end"))
                pts += [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            else:
                pts += [_pt(n) for k in ("start", "end") if (n := find(g, k)) is not None]
        return hull(self.to_board(x, y) for x, y in pts) if pts else None

    def box(self) -> Box | None:
        """Courtyard, or the pads' extent for footprints without one."""
        return self.courtyard() or union(p.box for p in self.pads())

    def place(self, x: float | None = None, y: float | None = None, rot: float | None = None) -> None:
        """Set position (mm) and/or rotation (degrees) without any check, the lock
        included; the checked way is ``Board.move``."""
        at = find(self.node, "at")
        if x is not None:
            at[1] = num(x)
        if y is not None:
            at[2] = num(y)
        if rot is not None:
            fps.set_rotation(self.node, rot)

    def flip(self, swap=None) -> None:
        """Move to the other side, mirrored like KiCad's flip (y mirror, same position).
        ``swap``: layer name -> layer name (default: F./B. swapped)."""
        rot = self.rot
        fps.set_rotation(self.node, 0)
        fps.mirror(self.node, swap=swap or fps.swap_side)
        fps.set_rotation(self.node, rot)

    def info(self) -> dict:
        """ref, value, footprint, x, y, rot, side, locked, box as a dict."""
        return {"ref": self.ref, "value": self.value, "footprint": self.lib_id,
                "x": self.x, "y": self.y, "rot": self.rot, "side": self.side, "locked": self.locked,
                "box": list(b) if (b := self.box()) else None}


class Board(SetupMixin, PlacementMixin, StructureMixin, StampMixin, FinishMixin):
    """A KiCad 10 board file, edited in memory and saved once through ``kicad-cli pcb
    upgrade``. Use it inside ``with kat.pcb.edit(PCB) as b:``. Coordinates in mm, y grows
    downwards, angles in degrees. Pads are named ``"REF.NUMBER"`` (``"U1.8"``).
    Methods by phase: setup (outline, layers, plane, zone, keepout, netclass, rules,
    mounting_hole), placement (move, move_many, place_near, align, distribute, blocks,
    replicate), structure (track, via, fanout, stitch, lock, remove_tracks), blocks (stamp,
    instances), finishing
    (place_refs, board_text, title_from_schematic, fiducials)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.tree = loads(self.path.read_text(encoding="utf-8"))
        if head(self.tree) != "kicad_pcb":
            raise env.KatError(f"{path} is not a KiCad board")
        self._pro_edits: list = []
        self._rules_text: str | None = None
        self._gen_rules: dict[str, str] = {}
        self._route_edits: dict = {}
        self._stamps: dict = {}
        self._renamed_instances: dict[str, str] = {}
        self._profile: dict | None = None          # a changed fabrication profile, written on save

    @staticmethod
    def create(path: Path | str) -> "Board":
        """Empty two-layer board; KiCad fills in layers and setup when it normalises it."""
        path = Path(path)
        path.write_text(f'(kicad_pcb (version {FORMAT}) (generator "pcbnew") '
                        f'(generator_version "10.0"))\n', encoding="utf-8")
        normalize(path)
        return Board(path)

    def save(self) -> None:
        """Write the board (and changed .kicad_pro / .kicad_dru) and normalise it through
        ``kicad-cli pcb upgrade``; ``kat.pcb.edit`` calls this for you. Refused while the
        board is open in KiCad."""
        from ..gui import guard
        guard(self.path)
        self._write_project_files()
        self._save_stamps()
        self.path.write_text(dumps(self.tree) + "\n", encoding="utf-8")
        normalize(self.path)
        self.tree = loads(self.path.read_text(encoding="utf-8"))

    @property
    def project_dir(self) -> Path:
        """The directory holding the board (and its project files)."""
        return self.path.parent

    # --- footprints -----------------------------------------------------------------

    def tidy(self, nets: list[str] | None = None, layers: list[str] | None = None, region=None,
             steps: list[str] | None = None, corners: str = "45", spread: float | None = None,
             max_growth: float = 10.0, freeze_length: list[str] = (), include_locked: bool = False) -> dict:
        """Clean up routed tracks in memory (``kat.pcb.trackclean``; ``kat.pcb.tidy_tracks``
        also saves, runs DRC and puts back nets it made worse). Returns steps, nets and
        length {net: [before, after] mm}."""
        from . import trackclean as tc
        return tc.tidy_board(self, nets, layers, region, tuple(steps or tc.STEPS), corners, spread,
                             max_growth, tuple(freeze_length), include_locked)

    def footprints(self) -> list[Footprint]:
        """All footprints on the board."""
        return [Footprint(n) for n in find_all(self.tree, "footprint")]

    def footprint(self, ref: str) -> Footprint:
        """The footprint with reference ``ref``; KatError if missing."""
        for f in self.footprints():
            if f.ref == ref:
                return f
        raise env.KatError(f"no footprint {ref!r} on the board")

    def add_footprint(self, lib_id: str, ref: str, x: float, y: float, rot: float = 0.0,
                      side: str = "F", value: str | None = None) -> Footprint:
        """Place a library footprint at ``x``, ``y`` (mm) without any link to the schematic.
        Normally parts come from ``kat.pcb.sync``; parts without a symbol use
        ``add_board_part``."""
        node = fps.instantiate(lib_id, x, y, rot, side, self.project_dir)
        f = Footprint(node)
        f.set_field("Reference", ref)
        if value is not None:
            f.set_field("Value", value)
        self._insert(node)
        return f

    def remove_footprint(self, ref: str) -> None:
        """Delete the footprint ``ref`` (tracks on its pads stay)."""
        self.tree.remove(self.footprint(ref).node)

    def pads(self) -> list[Pad]:
        """All pads of all footprints, in board coordinates."""
        return [p for f in self.footprints() for p in f.pads()]

    # --- copper and drawings ----------------------------------------------------------

    def tracks(self) -> list[dict]:
        """Track segments and arcs: kind, start, end, width, layer, net, locked, uuid, node."""
        out = []
        for n in self.tree:
            if head(n) in ("segment", "arc"):
                s, e = find(n, "start"), find(n, "end")
                net = find(n, "net")
                out.append({"kind": head(n), "start": (float(s[1]), float(s[2])),
                            "end": (float(e[1]), float(e[2])), "width": _f(n, "width"),
                            "layer": str(find(n, "layer")[1]),
                            "net": str(net[1]) if net is not None else "",
                            "locked": find(n, "locked") is not None, "uuid": str(find(n, "uuid")[1]),
                            "node": n})
        return out

    def vias(self) -> list[dict]:
        """Vias: at, size, drill, net, layers (the two it joins), span (every copper layer it
        crosses, top first), kind (through, blind, buried, micro), locked, uuid, node."""
        from .coupling import _span
        copper = self.copper_layers()
        out = []
        for n in find_all(self.tree, "via"):
            at, net = find(n, "at"), find(n, "net")
            layers = tuple(str(l) for l in find(n, "layers")[1:])
            out.append({"at": (float(at[1]), float(at[2])), "size": _f(n, "size"),
                        "drill": _f(n, "drill"), "net": str(net[1]) if net is not None else "",
                        "layers": layers, "span": tuple(_span(list(layers), copper)),
                        "kind": str(n[1]) if isinstance(n[1], Sym) else "through",
                        "locked": find(n, "locked") is not None, "uuid": str(find(n, "uuid")[1]),
                        "node": n})
        return out

    def add_track(self, start: tuple[float, float], end: tuple[float, float], width: float,
                  layer: str, net: str) -> dict:
        """One raw track segment, unchecked. Prefer ``track``, which takes the net from the
        pads and checks clearances."""
        from ..schematic import new_uuid
        node = [Sym("segment"), [Sym("start"), num(start[0]), num(start[1])],
                [Sym("end"), num(end[0]), num(end[1])], [Sym("width"), num(width)],
                [Sym("layer"), layer], [Sym("net"), net], [Sym("uuid"), new_uuid()]]
        self._insert(node)
        return {"uuid": str(node[-1][1])}

    def zones(self) -> list[list]:
        """Zone nodes (pours and keep-outs) as s-expressions."""
        return find_all(self.tree, "zone")

    def outline_box(self) -> Box | None:
        """Extent of the board outline (Edge.Cuts drawings)."""
        pts = []
        for n in self.tree:
            h = head(n)
            if not h or not h.startswith("gr_"):
                continue
            lay = find(n, "layer")
            if lay is None or str(lay[1]) != "Edge.Cuts":
                continue
            if h == "gr_circle":
                c, e = find(n, "center"), find(n, "end")
                r = ((float(e[1]) - float(c[1])) ** 2 + (float(e[2]) - float(c[2])) ** 2) ** 0.5
                pts += [(float(c[1]) - r, float(c[2]) - r), (float(c[1]) + r, float(c[2]) + r)]
            elif h == "gr_poly":
                pts += [(float(xy[1]), float(xy[2])) for xy in find_all(find(n, "pts") or [], "xy")]
            else:
                pts += [(float(p[1]), float(p[2])) for k in ("start", "mid", "end") if (p := find(n, k))]
        return box_of(pts) if pts else None

    def outline_loops(self, max_error: float = 0.01) -> list[list[tuple[float, float]]]:
        """The closed loops of the board outline as polygons (arcs within ``max_error`` mm),
        largest first: the board edge, then its cut-outs. Empty if Edge.Cuts does not close."""
        from .geometry import arc_points, chain
        loops, segments = [], []
        for n in self.tree:
            h = head(n)
            lay = find(n, "layer") if h and h.startswith("gr_") else None
            if lay is None or str(lay[1]) != "Edge.Cuts":
                continue
            pt = lambda k, node=n: (float(find(node, k)[1]), float(find(node, k)[2]))   # noqa: E731
            if h == "gr_line":
                segments.append(("line", pt("start"), pt("end")))
            elif h == "gr_arc":
                segments.append(("arc", pt("start"), pt("mid"), pt("end")))
            elif h == "gr_circle":
                c, e = pt("center"), pt("end")
                segments.append(("circle", c, math.hypot(e[0] - c[0], e[1] - c[1])))
            elif h == "gr_rect":
                (ax, ay), (bx, by) = pt("start"), pt("end")
                x0, x1, y0, y1 = min(ax, bx), max(ax, bx), min(ay, by), max(ay, by)
                loops.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
            elif h == "gr_poly":
                pts = [(float(p[1]), float(p[2])) for p in find_all(find(n, "pts") or [], "xy")]
                if len(pts) >= 3:
                    loops.append(pts)
        closed, _ = chain(segments)
        for loop in closed:
            pts: list[tuple[float, float]] = []
            for s in loop:
                if s[0] == "circle":
                    (cx, cy), r = s[1], s[2]
                    k = max(8, math.ceil(math.pi / math.acos(max(0.0, 1 - max_error / r)))) if r > max_error else 8
                    pts = [(cx + r * math.cos(2 * math.pi * i / k), cy + r * math.sin(2 * math.pi * i / k))
                           for i in range(k)]
                elif s[0] == "arc":
                    pts += arc_points(s[1], s[2], s[3], max_error)[:-1]
                else:
                    pts.append(s[1])
            loops.append(pts)

        def area(p: list[tuple[float, float]]) -> float:
            return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(p, p[1:] + p[:1]))) / 2
        return sorted(loops, key=area, reverse=True)

    def _insert(self, node: list) -> None:
        """After the last item of the same kind, else before copper items / at the end."""
        kind = head(node)
        last = max((i for i, c in enumerate(self.tree) if head(c) == kind), default=None)
        if last is None:
            tail = ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_text", "segment",
                    "arc", "via", "zone", "group", "embedded_fonts")
            last = next((i - 1 for i, c in enumerate(self.tree) if head(c) in tail), len(self.tree) - 1)
        self.tree.insert(last + 1, node)


def normalize(path: Path) -> None:
    """Let KiCad re-save the board: validates it loads and applies KiCad's formatting."""
    env.run_cli("pcb", "upgrade", "--force", str(path))
