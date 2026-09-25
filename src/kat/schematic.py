"""Schematic model on top of the lossless S-expression tree.

Coordinates are schematic millimetres (y grows downwards). Library coordinates have
y growing upwards; ``Placement.to_sheet`` converts.
"""

from __future__ import annotations

import copy
import math
import re
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path

from . import env, library
from .sexpr import Sym, dumps, find, find_all, head, is_yes, loads, num, prop

GRID = 1.27
EPS = 1e-3
KICAD_FORMAT = "20260306"


def new_uuid() -> str:
    return str(uuidlib.uuid4())


def snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS


@dataclass
class Placement:
    x: float
    y: float
    rot: float = 0
    mirror: str = ""  # "", "x" or "y"

    def to_sheet(self, lx: float, ly: float) -> tuple[float, float]:
        """Library point -> sheet point (KiCad: flip y, rotate, then mirror)."""
        x, y = lx, -ly
        a = math.radians(self.rot)
        c, s = round(math.cos(a)), round(math.sin(a))
        x, y = x * c + y * s, -x * s + y * c
        if self.mirror == "x":
            y = -y
        elif self.mirror == "y":
            x = -x
        return round(self.x + x, 4), round(self.y + y, 4)


@dataclass
class PlacedSymbol:
    node: list
    ref: str
    lib_id: str
    unit: int
    place: Placement

    @property
    def uuid(self) -> str:
        return str(find(self.node, "uuid")[1])

    def field(self, name: str) -> str | None:
        p = prop(self.node, name)
        return str(p[2]) if p else None


def _sexp(text: str) -> list:
    return loads(text)


class Schematic:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.tree = loads(self.path.read_text(encoding="utf-8"))
        if head(self.tree) != "kicad_sch":
            raise env.KatError(f"{path} is not a KiCad schematic")

    # --- file level -----------------------------------------------------------

    @staticmethod
    def create(path: Path, paper: str = "A4") -> "Schematic":
        path = Path(path)
        path.write_text(dumps(_sexp(
            f'(kicad_sch (version {KICAD_FORMAT}) (generator "eeschema") (generator_version "10.0") '
            f'(uuid "{new_uuid()}") (paper "{paper}") (lib_symbols) '
            f'(sheet_instances (path "/" (page "1"))))')) + "\n", encoding="utf-8")
        normalize(path)
        return Schematic(path)

    def save(self) -> None:
        self.path.write_text(dumps(self.tree) + "\n", encoding="utf-8")
        normalize(self.path)
        self.tree = loads(self.path.read_text(encoding="utf-8"))

    @property
    def root_uuid(self) -> str:
        return str(find(self.tree, "uuid")[1])

    @property
    def project_name(self) -> str:
        pros = list(self.path.parent.glob("*.kicad_pro"))
        return pros[0].stem if len(pros) == 1 else self.path.stem

    def _insert(self, node: list) -> None:
        """Insert before sheet_instances/embedded_fonts, after items of the same kind."""
        tail = {"sheet_instances", "symbol_instances", "embedded_fonts"}
        idx = len(self.tree)
        for i, c in enumerate(self.tree):
            if head(c) in tail:
                idx = i
                break
        self.tree.insert(idx, node)

    # --- symbols --------------------------------------------------------------

    def lib_symbol(self, lib_id: str) -> list | None:
        ls = find(self.tree, "lib_symbols")
        for s in find_all(ls, "symbol"):
            if s[1] == lib_id:
                return s
        return None

    def ensure_lib_symbol(self, lib_id: str) -> list:
        if (s := self.lib_symbol(lib_id)) is not None:
            return s
        s = library.resolve(lib_id, self.path.parent)
        find(self.tree, "lib_symbols").append(s)
        return s

    def symbols(self) -> list[PlacedSymbol]:
        out = []
        for n in find_all(self.tree, "symbol"):
            at = find(n, "at")
            mir = find(n, "mirror")
            unit = find(n, "unit")
            ref = prop(n, "Reference")
            out.append(PlacedSymbol(
                node=n, ref=str(ref[2]) if ref else "?", lib_id=str(find(n, "lib_id")[1]),
                unit=int(str(unit[1])) if unit else 1,
                place=Placement(float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0,
                                str(mir[1]) if mir else "")))
        return out

    def find_symbols(self, ref: str, unit: int | None = None) -> list[PlacedSymbol]:
        found = [s for s in self.symbols() if s.ref == ref and (unit is None or s.unit == unit)]
        if not found:
            raise env.KatError(f"no symbol {ref}" + (f" unit {unit}" if unit else ""))
        return found

    def pins(self, sym: PlacedSymbol) -> list[dict]:
        sdef = self.lib_symbol(sym.lib_id)
        out = []
        for p in library.pins(sdef):
            if p.unit not in (0, sym.unit):
                continue
            x, y = sym.place.to_sheet(p.x, p.y)
            # library pin angle points from the connection end into the body; outward is opposite
            a = math.radians(p.angle + 180)
            d = Placement(0, 0, sym.place.rot, sym.place.mirror).to_sheet(round(math.cos(a)), round(math.sin(a)))
            out.append({"number": p.number, "name": p.name, "type": p.etype, "x": x, "y": y,
                        "unit": sym.unit, "dir": [d[0] or 0.0, d[1] or 0.0]})
        return out

    def pin(self, ref: str, pin: str) -> tuple[float, float]:
        """Sheet position of ``pin`` (number, or name if unambiguous) on any unit of ``ref``."""
        by_name = []
        for s in self.find_symbols(ref):
            for p in self.pins(s):
                if p["number"] == pin:
                    return p["x"], p["y"]
                if p["name"] == pin:
                    by_name.append(p)
        if len(by_name) == 1:
            return by_name[0]["x"], by_name[0]["y"]
        if by_name:
            raise env.KatError(f"pin name {pin!r} on {ref} is ambiguous; use the pin number")
        raise env.KatError(f"{ref} has no pin {pin!r} (placed units only)")

    def next_ref(self, prefix: str) -> str:
        used = {s.ref for s in self.symbols()}
        n = 1
        while f"{prefix}{n}" in used:
            n += 1
        return f"{prefix}{n}"

    def add_symbol(self, lib_id: str, x: float, y: float, rot: float = 0, mirror: str = "",
                   unit: int = 1, ref: str | None = None, value: str | None = None,
                   footprint: str | None = None) -> PlacedSymbol:
        if rot not in (0, 90, 180, 270):
            raise env.KatError("rotation must be 0, 90, 180 or 270")
        sdef = self.ensure_lib_symbol(lib_id)
        if unit > library.unit_count(sdef):
            raise env.KatError(f"{lib_id} has only {library.unit_count(sdef)} unit(s)")
        x, y = snap(x), snap(y)
        prefix = str(prop(sdef, "Reference")[2])
        if ref is None:
            ref = self.next_ref(prefix)
        elif any(s.ref == ref and s.unit == unit for s in self.symbols()):
            raise env.KatError(f"{ref} unit {unit} already placed")
        place = Placement(x, y, rot, mirror)
        pin_nodes = []
        for p in library.pins(sdef):
            if p.unit in (0, unit):
                pin_nodes.append([Sym("pin"), p.number, [Sym("uuid"), new_uuid()]])
        node: list = [Sym("symbol"), [Sym("lib_id"), lib_id],
                      [Sym("at"), num(x), num(y), num(rot)]]
        if mirror:
            node.append([Sym("mirror"), Sym(mirror)])
        node += [[Sym("unit"), Sym(str(unit))], [Sym("body_style"), Sym("1")],
                 [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")],
                 [Sym("on_board"), Sym("yes")], [Sym("in_pos_files"), Sym("yes")],
                 [Sym("dnp"), Sym("no")], [Sym("uuid"), new_uuid()]]
        overrides = {"Reference": ref, "Value": value, "Footprint": footprint}
        for lp in find_all(sdef, "property"):
            name = str(lp[1])
            if name.startswith("ki_"):
                continue
            p = copy.deepcopy(lp)
            if overrides.get(name) is not None:
                p[2] = overrides[name]
            at = find(p, "at")
            sx, sy = place.to_sheet(float(at[1]), float(at[2]))
            at[1:] = [num(sx), num(sy), at[3] if len(at) > 3 else Sym("0")]
            node.append(p)
        node += pin_nodes
        node.append(_sexp(f'(instances (project "{self.project_name}" (path "/{self.root_uuid}" '
                          f'(reference "{ref}") (unit {unit}))))'))
        self._insert(node)
        sym = PlacedSymbol(node, ref, lib_id, unit, place)
        self.autoplace_fields(sym)
        return sym

    def body_box(self, sym: PlacedSymbol) -> tuple[float, float, float, float] | None:
        box = library.outline(self.lib_symbol(sym.lib_id), sym.unit)
        if box is None:
            return None
        pts = [sym.place.to_sheet(box[0], box[1]), sym.place.to_sheet(box[2], box[3])]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def autoplace_fields(self, sym: PlacedSymbol) -> None:
        """Put visible Reference/Value (and other shown fields) horizontally beside the body:
        to the right of tall bodies, above/below wide ones. Centre-justified so the result
        does not depend on the symbol's rotation or mirroring."""
        box = self.body_box(sym)
        if box is None:
            return
        shown = [p for p in find_all(sym.node, "property") if not is_yes(find(p, "hide"))]
        if not shown:
            return
        # stored angle is relative to the symbol: 90 on a rotated symbol reads horizontally
        angle = Sym("90") if sym.place.rot in (90, 270) else Sym("0")
        x0, y0, x1, y1 = box
        if x1 - x0 < GRID * 2:
            # pin-only unit (e.g. an op-amp's power unit): leave room for the pin names
            x1 += 2.54
        tall = (y1 - y0) >= (x1 - x0)
        for i, p in enumerate(shown):
            size = _font_size(p)
            w = text_width(str(p[2]), size)
            if tall:
                cx = x1 + GRID + w / 2
                cy = (y0 + y1) / 2 + (i - (len(shown) - 1) / 2) * size * 2
            else:
                cx = (x0 + x1) / 2
                cy = (y0 - GRID - size) if i == 0 else (y1 + GRID + size * (2 * i - 1))
            at = find(p, "at")
            at[1:] = [num(cx), num(cy), angle]
            eff = find(p, "effects")
            eff[:] = [c for c in eff if head(c) != "justify"]

    def place_power_text(self, sym: PlacedSymbol, direction: tuple[float, float]) -> None:
        """Put a power symbol's visible fields beyond its tip, along ``direction``
        (the way it points), so the text never lands on the part it is attached to."""
        box = self.body_box(sym)
        if box is None:
            return
        angle = Sym("90") if sym.place.rot in (90, 270) else Sym("0")
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        for p in find_all(sym.node, "property"):
            if is_yes(find(p, "hide")):
                continue
            size = _font_size(p)
            w = text_width(str(p[2]), size)
            if direction[0]:
                x = (box[2] + GRID + w / 2) if direction[0] > 0 else (box[0] - GRID - w / 2)
                y = cy
            else:
                x = cx
                y = (box[3] + GRID + size / 2) if direction[1] > 0 else (box[1] - GRID - size / 2)
            at = find(p, "at")
            at[1:] = [num(x), num(y), angle]
            eff = find(p, "effects")
            eff[:] = [c for c in eff if head(c) != "justify"]

    def set_field(self, ref: str, name: str, value: str | None = None,
                  at: tuple[float, float] | None = None, angle: float | None = None,
                  hide: bool | None = None, unit: int | None = None) -> None:
        targets = self.find_symbols(ref, unit)
        if at is not None and len(targets) > 1:
            raise env.KatError(f"{ref} has several units placed; pass --unit with --at")
        for s in targets:
            p = prop(s.node, name)
            if p is None:
                if value is None:
                    raise env.KatError(f"{ref} has no field {name!r}")
                p = _sexp(f'(property "{name}" "" (at {s.place.x} {s.place.y} 0) (hide yes) '
                          f'(effects (font (size 1.27 1.27))))')
                last = max(i for i, c in enumerate(s.node) if head(c) == "property")
                s.node.insert(last + 1, p)
            if value is not None:
                p[2] = value
                if name == "Reference":
                    for path in find_all(find(find(s.node, "instances"), "project"), "path"):
                        find(path, "reference")[1] = value
            fat = find(p, "at")
            if at is not None:
                fat[1], fat[2] = num(at[0]), num(at[1])
            if angle is not None:
                fat[3:] = [num(angle)]
            if hide is not None:
                p[:] = [c for c in p if head(c) != "hide"]
                p.insert(4, [Sym("hide"), Sym("yes" if hide else "no")])

    def remove_symbol(self, ref: str, unit: int | None = None) -> int:
        doomed = {id(s.node) for s in self.find_symbols(ref, unit)}
        self.tree[:] = [c for c in self.tree if id(c) not in doomed]
        self._prune_lib_symbols()
        return len(doomed)

    def _prune_lib_symbols(self) -> None:
        used = {s.lib_id for s in self.symbols()}
        ls = find(self.tree, "lib_symbols")
        ls[:] = [c for c in ls if head(c) != "symbol" or c[1] in used]

    def move_symbol(self, ref: str, x: float | None = None, y: float | None = None,
                    dx: float = 0, dy: float = 0, rot: float | None = None,
                    mirror: str | None = None, unit: int | None = None,
                    drag: bool = True) -> None:
        syms = self.find_symbols(ref, unit)
        if len(syms) > 1:
            raise env.KatError(f"{ref} has several units placed; pass --unit")
        s = syms[0]
        old_pins = {p["number"]: (p["x"], p["y"]) for p in self.pins(s)}
        old_fields = {id(p): (float(find(p, "at")[1]) - s.place.x, float(find(p, "at")[2]) - s.place.y)
                      for p in find_all(s.node, "property")}
        nx = snap(x if x is not None else s.place.x + dx)
        ny = snap(y if y is not None else s.place.y + dy)
        at = find(s.node, "at")
        at[1:] = [num(nx), num(ny), num(rot if rot is not None else s.place.rot)]
        s.node[:] = [c for c in s.node if head(c) != "mirror"]
        new_mirror = s.place.mirror if mirror is None else mirror
        if new_mirror:
            s.node.insert(3, [Sym("mirror"), Sym(new_mirror)])
        s.place = Placement(nx, ny, float(at[3]), new_mirror)
        if rot is not None or mirror is not None:
            self.autoplace_fields(s)
        else:
            for p in find_all(s.node, "property"):
                ox, oy = old_fields[id(p)]
                fa = find(p, "at")
                fa[1], fa[2] = num(nx + ox), num(ny + oy)
        if drag:
            new_pins = {p["number"]: (p["x"], p["y"]) for p in self.pins(s)}
            # real parts only: power symbols / flags on a pin are carried along instead
            others = [(p["x"], p["y"]) for o in self.symbols()
                      if o.node is not s.node and not o.ref.startswith("#") for p in self.pins(o)]
            carried: set[int] = set()
            drags: list[tuple[tuple[float, float], tuple[float, float]]] = []
            for num_, old in old_pins.items():
                new = new_pins[num_]
                if any(same(old, o) for o in others):
                    # pin sat directly on another pin: keep that contact with a new wire
                    self.route(old, new, first="v")
                    continue
                ids, d = self._carry(s, old, (new[0] - old[0], new[1] - old[1]), carried)
                carried |= ids
                drags += d
            for old, new in drags:
                self._drag_wire_ends(old, new, skip=carried)

    def private_run(self, start: tuple[float, float], owner: list | None = None,
                    skip: set[int] = frozenset()):
        """Walk the wires hanging *only* off ``start`` (a pin of symbol node ``owner``): up to
        the first point that is another part's pin, a branch (3+ wire ends) or a T-join.
        Returns (private wires, points reached, boundary wires as (near point, wire))."""
        wires = [(w, seg) for w, seg in zip(self.wires(), self.segments()) if id(w) not in skip]
        anchors = [(p["x"], p["y"]) for o in self.symbols()
                   if o.node is not owner and not o.ref.startswith("#") for p in self.pins(o)]
        ends = [e for _, seg in wires for e in seg]
        points, taken, frontier, boundary = [start], [], [start], []
        while frontier:
            p = frontier.pop()
            for w, seg in wires:
                if any(w is t for t in taken) or not (same(seg[0], p) or same(seg[1], p)):
                    continue
                q = seg[1] if same(seg[0], p) else seg[0]
                private = (not any(same(q, a) for a in anchors)
                           and sum(same(q, e) for e in ends) <= 2
                           and not any(_on_interior(q, s2) for _, s2 in wires)
                           and len(taken) < 20)
                if not private:
                    if not any(b[1] is w for b in boundary):
                        boundary.append((p, w))
                    continue
                taken.append(w)
                points.append(q)
                frontier.append(q)
        return taken, points, boundary

    def _items_at(self, points: list[tuple[float, float]], exclude: list | None = None):
        """Labels/no-connects and power-type symbols (#PWR, #FLG) located on ``points``."""
        marks = [n for kind in ("label", "global_label", "hierarchical_label", "no_connect")
                 for n in find_all(self.tree, kind)
                 if any(same((float(find(n, "at")[1]), float(find(n, "at")[2])), p) for p in points)]
        powers = [o for o in self.symbols() if o.ref.startswith("#") and o.node is not exclude
                  and any(same((pp["x"], pp["y"]), p) for pp in self.pins(o) for p in points)]
        return marks, powers

    def _carry(self, moving: PlacedSymbol, start: tuple[float, float],
               delta: tuple[float, float], already: set[int]
               ) -> tuple[set[int], list[tuple[tuple[float, float], tuple[float, float]]]]:
        """Translate what hangs *only* off this pin: wires up to the first point shared with
        other circuitry, plus labels, power symbols and no-connect flags on them. Wires that
        continue into shared circuitry are returned as drags (their near end follows)."""
        if abs(delta[0]) < EPS and abs(delta[1]) < EPS:
            return set(), []
        shift = lambda p: (round(p[0] + delta[0], 4), round(p[1] + delta[1], 4))
        taken, points, boundary = self.private_run(start, moving.node, already)
        drags = []
        for p, _ in boundary:
            if not any(same(d[0], p) for d in drags):
                drags.append((p, shift(p)))
        for w in taken:
            for xy in find_all(find(w, "pts"), "xy"):
                xy[1], xy[2] = num(float(xy[1]) + delta[0]), num(float(xy[2]) + delta[1])
        marks, powers = self._items_at(points, exclude=moving.node)
        for n in marks + [n for o in powers for n in [o.node] + find_all(o.node, "property")]:
            at = find(n, "at")
            at[1], at[2] = num(float(at[1]) + delta[0]), num(float(at[2]) + delta[1])
        return {id(w) for w in taken}, drags

    def pin_owner(self, ref: str, pin: str) -> tuple[PlacedSymbol, dict]:
        for s in self.find_symbols(ref):
            for p in self.pins(s):
                if p["number"] == pin:
                    return s, p
        matches = [(s, p) for s in self.find_symbols(ref) for p in self.pins(s) if p["name"] == pin]
        if len(matches) == 1:
            return matches[0]
        raise env.KatError(f"{ref} has no pin {pin!r}" if not matches else
                           f"pin name {pin!r} on {ref} is ambiguous; use the pin number")

    def detach(self, ref: str, pin: str) -> dict:
        """Disconnect a pin: remove its private wire run plus the wire leading from it into
        shared circuitry, and labels / power symbols / flags / no-connects on that run."""
        owner, p = self.pin_owner(ref, pin)
        start = (p["x"], p["y"])
        foreign = [o.ref for o in self.symbols() if o.node is not owner.node and not o.ref.startswith("#")
                   and any(same((q["x"], q["y"]), start) for q in self.pins(o))]
        if foreign:
            raise env.KatError(f"{ref}.{pin} touches {foreign} directly; move one of the parts first")
        taken, points, boundary = self.private_run(start, owner.node)
        marks, powers = self._items_at(points, exclude=owner.node)
        doomed = {id(w) for w in taken} | {id(w) for _, w in boundary} | {id(n) for n in marks} \
            | {id(o.node) for o in powers}
        self.tree[:] = [c for c in self.tree if id(c) not in doomed]
        self._prune_lib_symbols()
        self.fix_junctions()
        return {"pin": f"{ref}.{pin}", "wires": len(taken) + len(boundary), "labels": len(marks),
                "power_symbols": [o.ref for o in powers]}

    def prune_dangling(self) -> int:
        """Repeatedly remove wires with a free end and labels / flags / power symbols that
        touch nothing. Returns the number of removed items."""
        removed = 0
        while True:
            segs = list(zip(self.wires(), self.segments()))
            pin_pts = [(p["x"], p["y"], id(s.node)) for s in self.symbols() for p in self.pins(s)]
            label_pts = [(l["x"], l["y"]) for l in self.labels()]
            nc_pts = [(float(find(n, "at")[1]), float(find(n, "at")[2]))
                      for n in find_all(self.tree, "no_connect")]

            def touched(pt, by_wire=None, by_node=None) -> bool:
                for w, seg in segs:
                    if w is by_wire:
                        continue
                    if same(seg[0], pt) or same(seg[1], pt) or _on_interior(pt, seg):
                        return True
                if any(same(pt, (x, y)) for x, y, n in pin_pts if n != by_node):
                    return True
                return by_wire is not None and any(same(pt, q) for q in label_pts + nc_pts)

            doomed = set()
            for w, seg in segs:
                if not touched(seg[0], by_wire=w) or not touched(seg[1], by_wire=w):
                    doomed.add(id(w))
            for kind in ("label", "global_label", "hierarchical_label", "no_connect"):
                for n in find_all(self.tree, kind):
                    at = find(n, "at")
                    if not touched((float(at[1]), float(at[2]))):
                        doomed.add(id(n))
            for o in self.symbols():
                if o.ref.startswith("#") and not any(
                        touched((p["x"], p["y"]), by_node=id(o.node)) for p in self.pins(o)):
                    doomed.add(id(o.node))
            if not doomed:
                break
            self.tree[:] = [c for c in self.tree if id(c) not in doomed]
            removed += len(doomed)
        self._prune_lib_symbols()
        self.fix_junctions()
        return removed

    def _drag_wire_ends(self, old: tuple[float, float], new: tuple[float, float],
                        skip: set[int] = frozenset()) -> None:
        if same(old, new):
            return
        for w in self.wires():
            if id(w) in skip:
                continue
            pts = find(w, "pts")
            xy = find_all(pts, "xy")
            ends = [(float(p[1]), float(p[2])) for p in xy]
            for i, e in enumerate(ends):
                if not same(e, old):
                    continue
                other = ends[1 - i]
                horizontal = abs(other[1] - old[1]) < EPS
                xy[i][1], xy[i][2] = num(new[0]), num(new[1])
                if abs(other[0] - new[0]) > EPS and abs(other[1] - new[1]) > EPS:
                    corner = (new[0], other[1]) if horizontal else (other[0], new[1])
                    xy[i][1], xy[i][2] = num(corner[0]), num(corner[1])
                    self.add_wire([corner, new], junctions=False)

    # --- wires, labels, junctions -------------------------------------------

    def wires(self) -> list[list]:
        return find_all(self.tree, "wire")

    def segments(self) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        out = []
        for w in self.wires():
            xy = find_all(find(w, "pts"), "xy")
            out.append(((float(xy[0][1]), float(xy[0][2])), (float(xy[1][1]), float(xy[1][2]))))
        return out

    def add_wire(self, points: list[tuple[float, float]], junctions: bool = True) -> list[list]:
        pts = [(snap(x), snap(y)) for x, y in points]
        added = []
        for a, b in zip(pts, pts[1:]):
            if same(a, b):
                continue
            w = _sexp(f'(wire (pts (xy {num(a[0])} {num(a[1])}) (xy {num(b[0])} {num(b[1])})) '
                      f'(stroke (width 0) (type default)) (uuid "{new_uuid()}"))')
            self._insert(w)
            added.append(w)
        if junctions:
            self.fix_junctions()
        return added

    def route(self, a: tuple[float, float], b: tuple[float, float],
              via: list[tuple[float, float]] | None = None, first: str = "h") -> list[list]:
        """Orthogonal path a -> via... -> b; each leg bends once (horizontal first if first=='h')."""
        pts = [a, *(via or []), b]
        path = [pts[0]]
        for p, q in zip(pts, pts[1:]):
            if abs(p[0] - q[0]) > EPS and abs(p[1] - q[1]) > EPS:
                path.append((q[0], p[1]) if first == "h" else (p[0], q[1]))
            path.append(q)
        return self.add_wire(path)

    def add_label(self, text: str, x: float, y: float, angle: float = 0,
                  kind: str = "label", shape: str = "passive") -> list:
        x, y = snap(x), snap(y)
        justify = {0: "left bottom", 90: "left bottom", 180: "right bottom", 270: "right bottom"}
        esc = text.replace("\\", "\\\\").replace('"', '\\"')
        if kind == "global_label":
            just = "left" if angle in (0, 90) else "right"
            node = _sexp(f'(global_label "{esc}" (shape {shape}) (at {num(x)} {num(y)} {num(angle)}) '
                         f'(fields_autoplaced yes) (effects (font (size 1.27 1.27)) (justify {just})) '
                         f'(uuid "{new_uuid()}") (property "Intersheetrefs" "${{INTERSHEET_REFS}}" '
                         f'(at {num(x)} {num(y)} 0) (hide yes) (effects (font (size 1.27 1.27)))))')
        else:
            node = _sexp(f'(label "{esc}" (at {num(x)} {num(y)} {num(angle)}) '
                         f'(effects (font (size 1.27 1.27)) (justify {justify.get(int(angle), "left bottom")})) '
                         f'(uuid "{new_uuid()}"))')
        self._insert(node)
        return node

    def labels(self) -> list[dict]:
        out = []
        for kind in ("label", "global_label", "hierarchical_label"):
            for n in find_all(self.tree, kind):
                at = find(n, "at")
                out.append({"kind": kind, "text": str(n[1]), "x": float(at[1]), "y": float(at[2]),
                            "angle": float(at[3]) if len(at) > 3 else 0, "uuid": str(find(n, "uuid")[1])})
        return out

    def add_no_connect(self, x: float, y: float) -> None:
        self._insert(_sexp(f'(no_connect (at {num(snap(x))} {num(snap(y))}) (uuid "{new_uuid()}"))'))

    def connection_points(self) -> list[tuple[float, float]]:
        pts = []
        for s in self.symbols():
            pts += [(p["x"], p["y"]) for p in self.pins(s)]
        for a, b in self.segments():
            pts += [a, b]
        pts += [(l["x"], l["y"]) for l in self.labels()]
        return pts

    def fix_junctions(self) -> None:
        """Junction wherever a wire end meets the interior of another wire, or where three
        or more wire ends / pins meet. Removes junctions that no longer qualify."""
        segs = self.segments()
        ends = [p for s in segs for p in s]
        pin_pts = [(p["x"], p["y"]) for s in self.symbols() for p in self.pins(s)]
        want: list[tuple[float, float]] = []

        def add(p):
            if not any(same(p, q) for q in want):
                want.append(p)

        for p in ends + pin_pts:
            if any(_on_interior(p, s) for s in segs):
                add(p)
        for p in ends:
            degree = sum(same(p, q) for q in ends) + sum(same(p, q) for q in pin_pts)
            if degree >= 3:
                add(p)
        self.tree[:] = [c for c in self.tree if head(c) != "junction"]
        for x, y in want:
            self._insert(_sexp(f'(junction (at {num(x)} {num(y)}) (diameter 0) (color 0 0 0 0) '
                               f'(uuid "{new_uuid()}"))'))

    def remove_by_uuid(self, u: str) -> int:
        before = len(self.tree)
        self.tree[:] = [c for c in self.tree
                        if not (isinstance(c, list) and find(c, "uuid") and str(find(c, "uuid")[1]) == u)]
        return before - len(self.tree)

    def remove_wires_at(self, x: float, y: float) -> int:
        p = (snap(x), snap(y))
        doomed = {id(w) for w, seg in zip(self.wires(), self.segments())
                  if same(seg[0], p) or same(seg[1], p) or _on_interior(p, seg)}
        self.tree[:] = [c for c in self.tree if id(c) not in doomed]
        return len(doomed)


def _on_interior(p, seg) -> bool:
    (ax, ay), (bx, by) = seg
    if same(p, seg[0]) or same(p, seg[1]):
        return False
    if abs(ax - bx) < EPS:
        return abs(p[0] - ax) < EPS and min(ay, by) < p[1] < max(ay, by)
    if abs(ay - by) < EPS:
        return abs(p[1] - ay) < EPS and min(ax, bx) < p[0] < max(ax, bx)
    return False


def _font_size(p: list) -> float:
    font = find(find(p, "effects") or [], "font")
    size = find(font or [], "size")
    return float(size[1]) if size else 1.27


def text_width(text: str, size: float = 1.27) -> float:
    """Rough width of KiCad's stroke font (mm)."""
    return len(text) * size * 0.85 + size * 0.2


def normalize(path: Path) -> None:
    """Let KiCad re-save the file: validates it loads and applies KiCad's own formatting."""
    env.run_cli("sch", "upgrade", "--force", str(path))
