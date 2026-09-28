"""Editing and inspection verbs on one schematic sheet: the library behind the CLI.

A ``Sheet`` wraps the low-level :class:`kat.schematic.Schematic`. Every method returns
the JSON-able dict (or list) that the matching ``kat`` command prints. Points are
``"REF.PIN"`` (pin number, or a unique pin name) or ``(x, y)`` in mm; ``"X,Y"`` strings
are accepted too. Use :func:`kat.edit` to get a sheet that is saved once at the end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import env, hierarchy, library
from .schematic import Placement, Schematic
from .sexpr import find, find_all
from .syncer import _power_dir

Point = Union[str, tuple[float, float]]


def xy(text: str) -> tuple[float, float]:
    """``"X,Y"`` -> ``(x, y)``."""
    try:
        x, y = (float(v) for v in text.split(","))
    except ValueError:
        raise ValueError(f"expected X,Y in mm, got {text!r}") from None
    return x, y


def _tidy_results(cls):
    """Every public method returns rounded, JSON-able data (as the CLI prints it)."""
    import functools
    for name, f in list(vars(cls).items()):
        if callable(f) and not name.startswith("_") and name not in ("save", "point"):
            def wrap(f=f):
                @functools.wraps(f)
                def inner(*a, **k):
                    return env.tidy(f(*a, **k))
                return inner
            setattr(cls, name, wrap())
    return cls


@_tidy_results
class Sheet:
    """A schematic sheet with the editing verbs (add, wire, connect, move, ...). Use it
    inside ``with kat.edit(SCH) as s:``; saved once through ``kicad-cli sch upgrade``.
    Points are ``"REF.PIN"`` or ``(x, y)`` in mm, y grows downwards."""

    def __init__(self, path: Path | str | Schematic):
        self.schematic = path if isinstance(path, Schematic) else Schematic(Path(path))

    @property
    def path(self) -> Path:
        """The .kicad_sch file."""
        return self.schematic.path

    def save(self) -> None:
        """Write the file through ``kicad-cli sch upgrade`` (validation + KiCad formatting)."""
        self.schematic.save()

    # --- helpers --------------------------------------------------------------

    def point(self, p: Point) -> tuple[float, float]:
        """Resolve ``"REF.PIN"``, ``"SHEETNAME.PIN"`` (a sheet box's pin), ``"X,Y"`` or
        ``(x, y)`` to sheet coordinates."""
        if not isinstance(p, str):
            x, y = p
            return float(x), float(y)
        if "," in p:
            return xy(p)
        if "." not in p:
            raise env.KatError(f"expected REF.PIN, SHEET.PIN or X,Y, got {p!r}")
        ref, pin = p.split(".", 1)
        if not any(s.ref == ref for s in self.schematic.symbols()):
            for sh in hierarchy.sheets(self.schematic):
                if sh["name"] == ref:
                    if pin not in sh["pins"]:
                        raise env.KatError(f"sheet {ref} has no pin {pin!r} ({', '.join(sh['pins'])})")
                    return sh["pins"][pin]["x"], sh["pins"][pin]["y"]
        return self.schematic.pin(ref, pin)

    def _info(self, sym) -> dict:
        s = self.schematic
        return {"ref": sym.ref, "lib_id": sym.lib_id, "unit": sym.unit,
                "x": sym.place.x, "y": sym.place.y, "rot": sym.place.rot, "mirror": sym.place.mirror,
                "value": sym.field("Value"), "footprint": sym.field("Footprint"),
                "body": [round(v, 2) for v in (s.body_box(sym) or ())],
                "pins": s.pins(sym)}

    def _stub(self, at: tuple[float, float], stub: Point | None) -> tuple[float, float]:
        if not stub:
            return at
        dx, dy = xy(stub) if isinstance(stub, str) else stub
        end = (at[0] + dx, at[1] + dy)
        self.schematic.add_wire([at, end])
        return end

    # --- inspection -----------------------------------------------------------

    def pins(self, ref: str, unit: int | None = None) -> list[dict]:
        """Placement, fields, body box and pins (sheet position, outward ``dir``) of a part."""
        return [self._info(x) for x in self.schematic.find_symbols(ref, unit)]

    def list(self) -> dict:
        """Placed symbols, sheet boxes, labels, wires and the number of junctions."""
        s = self.schematic
        syms = [{k: v for k, v in self._info(x).items() if k != "pins"} for x in s.symbols()]
        return {"symbols": syms, "sheets": [{"name": b.ref, "x": b.at[0], "y": b.at[1], "size": list(b.size)}
                                           for b in s.sheet_boxes()],
                "labels": s.labels(),
                "wires": [[list(p), list(q)] for p, q in s.segments()],
                "junctions": len(find_all(s.tree, "junction"))}

    def near(self, at: Point, radius: float = 5.0) -> list[dict]:
        """Everything within ``radius`` mm of a point, nearest first: parts (by body box),
        sheet boxes, pins (sheet pins as ``NAME.PIN``), wires, labels, junctions, no-connects and free texts, with their uuids."""
        import math
        from .schematic import _on_interior  # noqa: F401
        s = self.schematic
        x, y = self.point(at)

        def box_dist(b):
            return math.hypot(max(b[0] - x, 0, x - b[2]), max(b[1] - y, 0, y - b[3]))

        def seg_dist(a, b):
            (ax, ay), (bx, by) = a, b
            L2 = (bx - ax) ** 2 + (by - ay) ** 2
            u = 0 if L2 == 0 else max(0, min(1, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / L2))
            return math.hypot(ax + u * (bx - ax) - x, ay + u * (by - ay) - y)

        found = []
        for sym in s.symbols():
            b = s.body_box(sym)
            if b and (d := box_dist(b)) <= radius:
                found.append({"kind": "power" if sym.ref.startswith("#") else "part", "ref": sym.ref,
                              "unit": sym.unit, "value": sym.field("Value"), "uuid": sym.uuid, "dist": d})
            for p in s.pins(sym):
                if (d := math.hypot(p["x"] - x, p["y"] - y)) <= radius:
                    found.append({"kind": "pin", "pin": f"{sym.ref}.{p['number']}", "name": p["name"],
                                  "x": p["x"], "y": p["y"], "dist": d})
        for bx in s.sheet_boxes():
            if (d := box_dist(bx.box())) <= radius:
                found.append({"kind": "sheet", "name": bx.ref, "uuid": str(find(bx.node, "uuid")[1]), "dist": d})
            for pn, (px, py) in bx.pins().items():
                if (d := math.hypot(px - x, py - y)) <= radius:
                    found.append({"kind": "pin", "pin": f"{bx.ref}.{pn}", "x": px, "y": py, "dist": d})
        for w, (a, b) in zip(s.wires(), s.segments()):
            if (d := seg_dist(a, b)) <= radius:
                found.append({"kind": "wire", "from": list(a), "to": list(b),
                              "uuid": str(find(w, "uuid")[1]), "dist": d})
        for l in s.labels():
            if (d := math.hypot(l["x"] - x, l["y"] - y)) <= radius:
                found.append({"kind": l["kind"], "text": l["text"], "x": l["x"], "y": l["y"],
                              "uuid": l["uuid"], "dist": d})
        for kind in ("junction", "no_connect", "text"):
            for n in find_all(s.tree, kind):
                a = find(n, "at")
                if (d := math.hypot(float(a[1]) - x, float(a[2]) - y)) <= radius:
                    item = {"kind": kind, "x": float(a[1]), "y": float(a[2]),
                            "uuid": str(find(n, "uuid")[1]), "dist": d}
                    if kind == "text":
                        item["text"] = str(n[1])
                    found.append(item)
        # ties (a power symbol sitting on a pin): pins first, a part's pin before the power
        # symbol's; never file order, which follows KiCad's random uuids
        return sorted(found, key=lambda f: (round(f["dist"], 3), f["kind"] != "pin",
                                            f.get("pin", f.get("ref", "")).startswith("#"),
                                            f.get("pin", f.get("ref", ""))))

    # --- sheet ----------------------------------------------------------------

    def page(self, paper: str, portrait: bool = False) -> dict:
        """Change the paper size (A4, A3, A2, A1, A0, A..E, USLetter, ...)."""
        from .syncer import PAPER
        if paper not in PAPER:
            raise env.KatError(f"unknown paper {paper!r}; one of {', '.join(PAPER)}")
        self.schematic.set_paper(paper, portrait)
        return {"paper": paper, "portrait": portrait}

    def title(self, title: str | None = None, date: str | None = None, rev: str | None = None,
              company: str | None = None, comments: dict[int, str] | None = None) -> dict:
        """Fill the title block; ``None`` keeps a field, ``""`` clears it. ``comments``
        maps comment number (1-9) to text."""
        extra = {f"comment{n}": v for n, v in (comments or {}).items()}
        if any(not 1 <= int(k[7:]) <= 9 for k in extra):
            raise env.KatError("comment numbers are 1..9")
        return self.schematic.set_title_block(title=title, date=date, rev=rev, company=company, **extra)

    def text(self, text: str, at: Point, size: float = 1.27, angle: float = 0,
             bold: bool = False) -> dict:
        """Free text (notes, headings) at a point; not connected to anything. Remove it with
        ``rm(uuid=...)``."""
        x, y = self.point(at)
        node = self.schematic.add_text(text, x, y, size, angle, bold)
        return {"text": text, "x": x, "y": y, "uuid": str(find(node, "uuid")[1])}

    # --- drawing --------------------------------------------------------------

    def add(self, lib_id: str, x: float, y: float, rot: float = 0, mirror: str = "",
            unit: int = 1, ref: str | None = None, value: str | None = None,
            footprint: str | None = None) -> dict:
        """Place a library symbol. Prefer `sync` for parts that come from the design."""
        sym = self.schematic.add_symbol(lib_id, x, y, rot, mirror or "", unit, ref, value, footprint)
        return self._info(sym)

    def wire(self, *points: Point) -> dict:
        """Wire polyline through the points."""
        pts = [self.point(p) for p in points]
        return {"wires": len(self.schematic.add_wire(pts)), "points": pts}

    def connect(self, a: Point, b: Point, via: list[Point] = (), first: str = "h") -> dict:
        """Orthogonal wire from a to b; each leg bends at most once (``first`` = "h" or "v")."""
        p, q = self.point(a), self.point(b)
        return {"wires": len(self.schematic.route(p, q, [self.point(v) for v in via], first)),
                "from": p, "to": q}

    def label(self, name: str, at: Point, stub: Point | None = None, angle: float = 0,
              glob: bool = False, shape: str = "passive") -> dict:
        """Net label at ``at``, or at the end of a wire stub (dx, dy) drawn from it."""
        s = self.schematic
        end = self._stub(self.point(at), stub)
        s.add_label(name, end[0], end[1], angle, "global_label" if glob else "label", shape)
        s.fix_junctions()
        return {"label": name, "x": end[0], "y": end[1]}

    def power(self, name: str, at: Point, stub: Point | None = None, rot: float = 0) -> dict:
        """Power symbol (GND, +3V3, PWR_FLAG, ... or Library:Symbol) whose pin sits on the
        target, after an optional stub."""
        s = self.schematic
        end = self._stub(self.point(at), stub)
        lib_id = name if ":" in name else f"power:{name}"
        sdef = s.ensure_lib_symbol(lib_id)
        pin = library.pins(sdef)[0]
        off = Placement(0, 0, rot).to_sheet(pin.x, pin.y)
        sym = s.add_symbol(lib_id, end[0] - off[0], end[1] - off[1], rot)
        s.place_power_text(sym, _power_dir(s, sym))
        s.fix_junctions()
        return {"ref": sym.ref, "lib_id": lib_id, "x": end[0], "y": end[1]}

    def sheet(self, name: str, file: str, at: Point, size: Point | None = None,
              left: list[str] | dict[str, str] = (), right: list[str] | dict[str, str] = (),
              fields: dict[str, str] | None = None) -> dict:
        """Sheet box (a hierarchical instance) named ``name`` with its top-left corner at
        ``at``, pointing to ``file`` (relative to this sheet's folder; created empty if
        missing). Pins on the left/right edge, 2.54 mm apart: a list of names or
        {name: shape}; each must match a ``port`` inside. ``fields`` are the instance's
        parameters, used inside as ``${NAME}``. ``size`` (w, h) mm defaults to fit the pins.
        Returns the pin points; wire them with ``"NAME.PIN"``. Run
        ``kat.hierarchy.annotate(root)`` after changing the hierarchy."""
        x, y = self.point(at)
        w, h = (self.point(size) if size else (None, None))
        return hierarchy.add_sheet(self.schematic, name, file, x, y, w, h, left, right, fields)

    def port(self, name: str, at: Point, angle: float = 180, shape: str = "passive") -> dict:
        """Hierarchical label: a connection point of this sheet, seen as the pin ``name``
        on the sheet boxes that use this file. ``angle`` 180: text to the left of the
        point, 0: to the right. ``shape``: input, output, bidirectional, tri_state, passive."""
        x, y = self.point(at)
        node = hierarchy.add_port(self.schematic, name, x, y, angle, shape)
        self.schematic.fix_junctions()
        return {"port": name, "x": float(find(node, "at")[1]), "y": float(find(node, "at")[2])}

    def stamp(self, block: str, instance: str, at: Point, design: str) -> dict:
        """Copy a block's checked drawing (library name or .kicad_sch) onto the instance
        ``instance`` (Block path, e.g. ``/CH_A``) of this flat sheet, with the top-left
        corner of the block region at ``at``. ``design``: the design netlist, which names
        the labels and completes the connections afterwards. Prefer ``kat.stamp``, which
        also checks the result against the design and undoes a wrong one."""
        from . import sch_stamp
        return sch_stamp.stamp(self.schematic, block, instance, self.point(at), design)

    def frame(self, block: str, title: str | None = None, margin: float = 3.81) -> dict:
        """Dashed frame and heading ("CH_A (block type)") around a block instance's parts;
        drawing it again replaces it."""
        from . import sch_stamp
        return sch_stamp.frame(self.schematic, block, title, margin)

    def sheet_box(self, name: str, size: Point | None = None, left: list[str] | None = None,
                  right: list[str] | None = None) -> dict:
        """Reshape sheet box ``name`` in place (the uuid stays): ``size`` (w, h) mm; pins
        listed in ``left`` / ``right`` go to that edge in that order (unlisted pins keep
        their side, below). Pin wiring comes along, mirrored when a pin changes sides.
        Move the box itself with :meth:`move`."""
        res = hierarchy.set_box(self.schematic, name, self.point(size) if size is not None else None,
                                left, right)
        box = next(b for b in self.sheets() if b["name"] == name)
        return {**res, **({"warning": w} if (w := self._covers(box)) else {})}

    def sheets(self) -> list[dict]:
        """Sheet boxes on this sheet: name, file, position, size, parameters and pins."""
        return [{k: (str(v) if k == "path" else v) for k, v in sh.items() if k != "node"}
                for sh in hierarchy.sheets(self.schematic)]

    def nc(self, at: Point) -> dict:
        """No-connect flag."""
        p = self.point(at)
        self.schematic.add_no_connect(*p)
        return {"no_connect": p}

    # --- rearranging ------------------------------------------------------------

    def move(self, ref: str, to: Point | None = None, by: Point | None = None,
             rot: float | None = None, mirror: str | None = None, unit: int | None = None,
             drag: bool = True, allow_contact: bool = False) -> list[dict]:
        """Move, rotate or mirror a part, or move a sheet box (``ref`` = its sheet name; its
        pins and fields come along, the uuid stays). What hangs only off it is carried along; wires
        into shared nets are dragged (unless ``drag=False``). Refuses to put a pin onto
        another part's pin, a wire or a label (that would join nets) unless
        ``allow_contact``; use :meth:`move_many` to rearrange parts that swap places."""
        return self.move_many([dict(ref=ref, to=to, by=by, rot=rot, mirror=mirror, unit=unit)],
                              drag, allow_contact)

    def move_many(self, moves: list[dict] | dict[str, dict], drag: bool = True,
                  allow_contact: bool = False) -> list[dict]:
        """Move several parts at once, like a block move: each decision is taken on the
        drawing before anything moves, so parts may take each other's places.

        ``moves``: ``[{"ref": "R1", "to": (x, y), "rot": 90}, {"ref": "U1", "unit": 2,
        "by": (0, 5.08)}, ...]`` or ``{"R1": {"to": (x, y)}, ...}``."""
        if isinstance(moves, dict):
            moves = [dict(v, ref=k) for k, v in moves.items()]
        plan = []
        for m in moves:
            unknown = set(m) - {"ref", "to", "by", "rot", "mirror", "unit"}
            if unknown:
                raise env.KatError(f"move of {m.get('ref')}: unknown keys {sorted(unknown)}")
            to, by = m.get("to"), m.get("by")
            tx, ty = self.point(to) if to is not None else (None, None)
            dx, dy = (xy(by) if isinstance(by, str) else by) if by is not None else (0, 0)
            plan.append(dict(ref=m["ref"], x=tx, y=ty, dx=dx, dy=dy, rot=m.get("rot"),
                             mirror=m.get("mirror"), unit=m.get("unit")))
        s = self.schematic
        s.move_symbols(plan, drag=drag, allow_contact=allow_contact)
        s.fix_junctions()
        boxes = {b["name"]: b for b in self.sheets()}
        out = []
        for m in plan:
            if not any(x.ref == m["ref"] for x in s.symbols()) and m["ref"] in boxes:
                b = boxes[m["ref"]]
                out.append({"sheet": b["name"], "x": b["at"][0], "y": b["at"][1], "size": b["size"],
                            **({"warning": w} if (w := self._covers(b)) else {})})
            else:
                out += [self._info(x) for x in s.find_symbols(m["ref"], m["unit"])]
        return out

    def _covers(self, box: dict) -> str | None:
        """A warning when a sheet box overlaps parts or other boxes."""
        s = self.schematic
        x1, y1 = box["at"]
        x2, y2 = x1 + box["size"][0], y1 + box["size"][1]
        hit = [x.ref for x in s.symbols() if (b := s.body_box(x)) and b[0] < x2 and x1 < b[2] and b[1] < y2 and y1 < b[3]]
        hit += [o.ref for o in s.sheet_boxes() if o.ref != box["name"]
                and (b := o.box())[0] < x2 and x1 < b[2] and b[1] < y2 and y1 < b[3]]
        return f"sheet box {box['name']} overlaps {', '.join(sorted(set(hit)))}" if hit else None

    def set(self, ref: str, field: str, value: str | None = None, at: Point | None = None,
            angle: float | None = None, hide: bool | None = None, unit: int | None = None,
            autoplace: bool = False) -> dict:
        """Change a field's text, position, angle or visibility (created hidden if missing)."""
        s = self.schematic
        s.set_field(ref, field, value, self.point(at) if at is not None else None, angle, hide, unit)
        if autoplace:
            for sym in s.find_symbols(ref, unit):
                s.autoplace_fields(sym)
        return {"ref": value if field == "Reference" and value else ref, "field": field}

    def detach(self, target: str) -> dict:
        """Disconnect ``REF.PIN`` from everything drawn at it (own wire run, labels, power
        symbols, flags). A bare ``REF`` detaches every pin of every placed unit."""
        s = self.schematic
        if "." in target:
            return s.detach(*target.split(".", 1))
        pins = sorted({p["number"] for sym in s.find_symbols(target) for p in s.pins(sym)},
                      key=lambda n: (len(n), n))
        results = [s.detach(target, n) for n in pins]
        out = {"part": target, "pins": len(pins), "wires": sum(r["wires"] for r in results),
               "labels": sum(r["labels"] for r in results),
               "power_symbols": [p for r in results for p in r["power_symbols"]]}
        warnings = [r["warning"] for r in results if "warning" in r]
        if warnings:
            out["warning"] = "; ".join(warnings)
        return out

    def rm(self, ref: str | None = None, unit: int | None = None, uuid: str | None = None,
           wires_at: Point | None = None) -> dict:
        """Remove a part (all units unless ``unit``), an item by UUID, or the wires at a point."""
        s = self.schematic
        if uuid:
            n = s.remove_by_uuid(uuid)
        elif wires_at is not None:
            n = s.remove_wires_at(*self.point(wires_at))
        elif ref:
            n = s.remove_symbol(ref, unit)
        else:
            raise env.KatError("rm needs a reference, uuid or wires_at")
        s.fix_junctions()
        return {"removed": n}
