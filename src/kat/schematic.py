"""Schematic model on top of the lossless S-expression tree.

Coordinates are schematic millimetres (y grows downwards). Library coordinates have
y growing upwards; ``Placement.to_sheet`` converts.
"""

from __future__ import annotations

import copy
import math
import re
import uuid as uuidlib
from collections import Counter, defaultdict
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


def key(p: tuple[float, float]) -> tuple[int, int]:
    """Hashable point identity (0.01 mm); all drawn points sit on the 1.27 mm grid."""
    return round(p[0] * 100), round(p[1] * 100)


class SegIndex:
    """Orthogonal wire segments by line, for fast "point on a wire's interior" tests."""

    def __init__(self, segs):
        self.v: dict[int, list] = defaultdict(list)
        self.h: dict[int, list] = defaultdict(list)
        for a, b in segs:
            if abs(a[0] - b[0]) < EPS:
                self.v[round(a[0] * 100)].append((min(a[1], b[1]), max(a[1], b[1])))
            elif abs(a[1] - b[1]) < EPS:
                self.h[round(a[1] * 100)].append((min(a[0], b[0]), max(a[0], b[0])))

    def interior(self, p) -> bool:
        return (any(lo + EPS < p[1] < hi - EPS for lo, hi in self.v.get(round(p[0] * 100), ()))
                or any(lo + EPS < p[0] < hi - EPS for lo, hi in self.h.get(round(p[1] * 100), ())))


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
        self._pin_cache: dict = {}
        self._sym_cache: dict = {}
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
        cache = self._sym_cache
        for n in find_all(self.tree, "symbol"):
            # the child lists are edited in place, so references to them stay valid;
            # only `mirror` is removed/inserted and is looked up every time
            hit = cache.get(id(n))
            if hit is None or hit[0] is not n:
                hit = cache[id(n)] = (n, find(n, "at"), find(n, "unit"), prop(n, "Reference"))
            _, at, unit, ref = hit
            mir = find(n, "mirror")
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
        pl = sym.place
        ck = (id(sym.node), sym.unit, pl.x, pl.y, pl.rot, pl.mirror)
        hit = self._pin_cache.get(ck)
        if hit and hit[0] is sym.node and hit[1] is sdef:
            return [dict(p) for p in hit[2]]
        out = self._pins(sym, sdef)
        self._pin_cache[ck] = (sym.node, sdef, out)
        return [dict(p) for p in out]

    def _pins(self, sym: PlacedSymbol, sdef: list) -> list[dict]:
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
        to the right of tall bodies, above/below wide ones, and never on a side with pins. Centre-justified so the result
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
        # keep the fields off the side where pins (and their labels) are
        dirs = {(round(p["dir"][0]), round(p["dir"][1])) for p in self.pins(sym)}
        if tall and (1, 0) in dirs and not dirs & {(0, 1), (0, -1)}:
            tall = False
        elif not tall and dirs & {(0, 1), (0, -1)} and (1, 0) not in dirs:
            tall = True
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
                    drag: bool = True, allow_contact: bool = False) -> None:
        self.move_symbols([dict(ref=ref, x=x, y=y, dx=dx, dy=dy, rot=rot, mirror=mirror, unit=unit)],
                          drag, allow_contact)

    def move_symbols(self, moves: list[dict], drag: bool = True, allow_contact: bool = False) -> None:
        """Move several parts *simultaneously*: every decision (what is carried, what is
        dragged, what would touch) is made on the drawing before any part moves. Each move
        is a dict with ref and optionally x, y (absolute), dx, dy, rot, mirror, unit.

        Unless ``allow_contact``, refuses when a pin would land on another part's pin, a
        wire, a label or a power symbol it does not carry: that contact would join nets
        silently (and a later move would drag wires between them)."""
        todo = []
        for m in moves:
            syms = self.find_symbols(m["ref"], m.get("unit"))
            if len(syms) > 1:
                raise env.KatError(f"{m['ref']} has several units placed; pass unit")
            s = syms[0]
            x, y = m.get("x"), m.get("y")
            nx = snap(x if x is not None else s.place.x + (m.get("dx") or 0))
            ny = snap(y if y is not None else s.place.y + (m.get("dy") or 0))
            rot = s.place.rot if m.get("rot") is None else float(m["rot"])
            mir = s.place.mirror if m.get("mirror") is None else m["mirror"]
            new = PlacedSymbol(s.node, s.ref, s.lib_id, s.unit, Placement(nx, ny, rot, mir))
            old_pins = {p["number"]: (p["x"], p["y"]) for p in self.pins(s)}
            new_pins = {p["number"]: (p["x"], p["y"]) for p in self._pins(new, self.lib_symbol(s.lib_id))}
            todo.append((s, new, old_pins, new_pins,
                         m.get("rot") is not None or m.get("mirror") is not None))
        if len({id(s.node) for s, *_ in todo}) < len(todo):
            raise env.KatError("a part is listed twice")
        moving = {id(s.node) for s, *_ in todo}

        # --- plan on the unchanged drawing ---
        carried: set[int] = set()
        boundary_ids: set[int] = set()
        translate, drags, contacts = [], [], []
        carried_items: set[int] = set()
        if drag:
            others = {key((p["x"], p["y"])) for o in self.symbols()
                      if id(o.node) not in moving and not o.ref.startswith("#") for p in self.pins(o)}
            moving_old = defaultdict(list)
            for s, _, old_pins, new_pins, _ in todo:
                for n, old in old_pins.items():
                    moving_old[key(old)].append((s.ref, (new_pins[n][0] - old[0], new_pins[n][1] - old[1])))
            for s, _, old_pins, new_pins, _ in todo:
                for n, old in old_pins.items():
                    new_p = new_pins[n]
                    delta = (new_p[0] - old[0], new_p[1] - old[1])
                    if key(old) in others:
                        contacts.append((old, new_p))  # pin on a staying part's pin: keep it
                        continue
                    if len({(round(d[0], 3), round(d[1], 3)) for _, d in moving_old[key(old)]}) > 1:
                        refs = sorted({r for r, _ in moving_old[key(old)]})
                        raise env.KatError(f"{' and '.join(refs)} touch pin on pin at {old[0]:g},{old[1]:g} "
                                           "and move apart; detach them first")
                    if abs(delta[0]) < EPS and abs(delta[1]) < EPS:
                        continue
                    taken, points, boundary = self.private_run(old, s.node, carried)
                    marks, powers = self._items_at(points, exclude=s.node)
                    # each item moves once, even when stacked pins (e.g. an exposed pad) reach it
                    items = [n for n in marks + [o.node for o in powers] if id(n) not in carried_items]
                    carried |= {id(w) for w in taken}
                    boundary_ids |= {id(w) for _, w in boundary}
                    carried_items |= {id(n) for n in items}
                    translate.append((taken, items, delta))
                    for bp, _ in boundary:
                        if not any(same(d[0], bp) for d in drags):
                            drags.append((bp, (bp[0] + delta[0], bp[1] + delta[1])))

        if not allow_contact:
            self._check_contacts(todo, moving, carried | boundary_ids, carried_items)

        # --- apply ---
        for s, new, _, _, reorient in todo:
            old_fields = {id(p): (float(find(p, "at")[1]) - s.place.x, float(find(p, "at")[2]) - s.place.y)
                          for p in find_all(s.node, "property")}
            pl = new.place
            at = find(s.node, "at")
            at[1:] = [num(pl.x), num(pl.y), num(pl.rot)]
            s.node[:] = [c for c in s.node if head(c) != "mirror"]
            if pl.mirror:
                s.node.insert(3, [Sym("mirror"), Sym(pl.mirror)])
            s.place = pl
            if reorient:
                self.autoplace_fields(s)
            else:
                for p in find_all(s.node, "property"):
                    ox, oy = old_fields[id(p)]
                    fa = find(p, "at")
                    fa[1], fa[2] = num(pl.x + ox), num(pl.y + oy)
        for wires, nodes, (dx, dy) in translate:
            for w in wires:
                for xy in find_all(find(w, "pts"), "xy"):
                    xy[1], xy[2] = num(float(xy[1]) + dx), num(float(xy[2]) + dy)
            for n in nodes:
                for m in [n] + (find_all(n, "property") if head(n) == "symbol" else []):
                    a = find(m, "at")
                    a[1], a[2] = num(float(a[1]) + dx), num(float(a[2]) + dy)
        for old, new_p in drags:
            self._drag_wire_ends(old, new_p, skip=carried)
        for old, new_p in contacts:
            self.route(old, new_p, first="v")

    def _check_contacts(self, todo, moving: set[int], own_wires: set[int], own_items: set[int]) -> None:
        """Raise if a moved pin would touch something it does not bring along."""
        taken: dict[tuple[int, int], tuple[int, str]] = {}  # point -> (owner node id, what)
        for o in self.symbols():
            if id(o.node) in moving or id(o.node) in own_items:
                continue
            for p in self.pins(o):
                taken.setdefault(key((p["x"], p["y"])), (id(o.node), f"pin {o.ref}.{p['number']}"))
        for s, _, _, new_pins, _ in todo:
            for n, q in new_pins.items():
                taken.setdefault(key(q), (id(s.node), f"pin {s.ref}.{n}"))
        foreign = [(w, seg) for w, seg in zip(self.wires(), self.segments()) if id(w) not in own_wires]
        for _, (a, b) in foreign:
            taken.setdefault(key(a), (0, "a wire end"))
            taken.setdefault(key(b), (0, "a wire end"))
        for kind in ("label", "global_label", "hierarchical_label"):
            for n in find_all(self.tree, kind):
                if id(n) not in own_items:
                    at = find(n, "at")
                    taken.setdefault(key((float(at[1]), float(at[2]))), (0, f"label {n[1]}"))
        index = SegIndex([seg for _, seg in foreign])
        for s, _, old_pins, new_pins, _ in todo:
            for n, q in new_pins.items():
                if same(q, old_pins[n]):
                    continue  # this pin stays where it is
                owner, hit = taken.get(key(q), (0, None))
                if owner == id(s.node):
                    hit = None  # own pin (stacked pins of one unit, e.g. an exposed pad)
                if hit is None and index.interior(q):
                    hit = "a wire"
                if hit:
                    raise env.KatError(
                        f"{s.ref}.{n} would land on {hit} at {q[0]:g},{q[1]:g} and join nets; "
                        "move that first, move both at once (move_many), or allow_contact=True")

    def private_run(self, start: tuple[float, float], owner: list | None = None,
                    skip: set[int] = frozenset()):
        """Walk the wires hanging *only* off ``start`` (a pin of symbol node ``owner``): up to
        the first point that is another part's pin, a branch (3+ wire ends) or a T-join.
        Returns (private wires, points reached, boundary wires as (near point, wire)).
        Stacked identical wires count as one wire (and are all taken or all boundary)."""
        groups: dict[tuple, list] = {}
        for w, seg in zip(self.wires(), self.segments()):
            if id(w) not in skip:
                groups.setdefault(tuple(sorted((key(seg[0]), key(seg[1])))), [seg, []])[1].append(w)
        anchors = {key((p["x"], p["y"])) for o in self.symbols()
                   if o.node is not owner and not o.ref.startswith("#") for p in self.pins(o)}
        ends = Counter(pk for gk in groups for pk in gk)
        at_end = defaultdict(list)
        for g in groups.values():
            for k in {key(g[0][0]), key(g[0][1])}:
                at_end[k].append(g)
        index = SegIndex([g[0] for g in groups.values()])
        points, taken, frontier, boundary, seen = [start], [], [start], [], set()
        while frontier:
            p = frontier.pop()
            for g in at_end.get(key(p), ()):
                if id(g) in seen:
                    continue
                seg, ws = g
                q = seg[1] if same(seg[0], p) else seg[0]
                private = (key(q) not in anchors
                           and ends[key(q)] <= 2
                           and not index.interior(q)
                           and len(taken) < 20)
                seen.add(id(g))
                if not private:
                    boundary += [(p, w) for w in ws]
                    continue
                taken += ws
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
        out = {"pin": f"{ref}.{pin}", "wires": len(taken) + len(boundary), "labels": len(marks),
               "power_symbols": [o.ref for o in powers]}
        flags = [o.ref for o in powers if o.lib_id == "power:PWR_FLAG"]
        if flags:
            out["warning"] = (f"removed {', '.join(flags)} (PWR_FLAG): if it was the net's only flag, "
                              "ERC reports power_pin_not_driven; add one with power('PWR_FLAG', ...) "
                              "or run sync, which re-adds it")
        return out

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
        want: dict[tuple[int, int], tuple[float, float]] = {}
        index = SegIndex(segs)
        for p in ends + pin_pts:
            if key(p) not in want and index.interior(p):
                want[key(p)] = p
        degree = Counter(key(p) for p in ends + pin_pts)
        for p in ends:
            if degree[key(p)] >= 3:
                want.setdefault(key(p), p)
        self.tree[:] = [c for c in self.tree if head(c) != "junction"]
        for x, y in want.values():
            self._insert(_sexp(f'(junction (at {num(x)} {num(y)}) (diameter 0) (color 0 0 0 0) '
                               f'(uuid "{new_uuid()}"))'))

    # --- sheet: paper, title block, free text ------------------------------------

    def set_paper(self, paper: str, portrait: bool = False) -> None:
        node = find(self.tree, "paper")
        new = [Sym("paper"), paper] + ([Sym("portrait")] if portrait else [])
        if node is None:
            self.tree.insert(4, new)
        else:
            node[:] = new

    def set_title_block(self, **fields: str | None) -> dict:
        """title, date, rev, company, comment1..comment9; None leaves a field as it is,
        "" clears it."""
        tb = find(self.tree, "title_block")
        if tb is None:
            tb = [Sym("title_block")]
            idx = next(i for i, c in enumerate(self.tree) if head(c) == "paper") + 1
            self.tree.insert(idx, tb)
        for name, value in fields.items():
            if value is None:
                continue
            if name.startswith("comment"):
                n = int(name[7:])
                old = next((c for c in find_all(tb, "comment") if str(c[1]) == str(n)), None)
                if old is not None:
                    tb.remove(old)
                if value:
                    tb.append([Sym("comment"), num(n), value])
            else:
                old = find(tb, name)
                if old is not None:
                    tb.remove(old)
                if value:
                    tb.append([Sym(name), value])
        order = ["title", "date", "rev", "company", "comment"]
        tb[1:] = sorted(tb[1:], key=lambda c: (order.index(head(c)), int(str(c[1])) if head(c) == "comment" else 0))
        return {head(c) if head(c) != "comment" else f"comment{c[1]}": str(c[-1]) for c in tb[1:]}

    def add_text(self, text: str, x: float, y: float, size: float = 1.27, angle: float = 0,
                 bold: bool = False) -> list:
        esc = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        style = " (bold yes)" if bold else ""
        node = _sexp(f'(text "{esc}" (exclude_from_sim no) (at {num(snap(x))} {num(snap(y))} {num(angle)}) '
                     f'(effects (font (size {num(size)} {num(size)}){style}) (justify left bottom)) '
                     f'(uuid "{new_uuid()}"))')
        self._insert(node)
        return node

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
