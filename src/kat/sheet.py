"""Editing and inspection verbs on one schematic sheet: the library behind the CLI.

A ``Sheet`` wraps the low-level :class:`kat.schematic.Schematic`. Every method returns
the JSON-able dict (or list) that the matching ``kat`` command prints. Points are
``"REF.PIN"`` (pin number, or a unique pin name) or ``(x, y)`` in mm; ``"X,Y"`` strings
are accepted too. Use :func:`kat.edit` to get a sheet that is saved once at the end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import env, library
from .schematic import Placement, Schematic
from .sexpr import find_all
from .syncer import _power_dir

Point = Union[str, tuple[float, float]]


def xy(text: str) -> tuple[float, float]:
    """``"X,Y"`` -> ``(x, y)``."""
    try:
        x, y = (float(v) for v in text.split(","))
    except ValueError:
        raise ValueError(f"expected X,Y in mm, got {text!r}") from None
    return x, y


class Sheet:
    def __init__(self, path: Path | str | Schematic):
        self.schematic = path if isinstance(path, Schematic) else Schematic(Path(path))

    @property
    def path(self) -> Path:
        return self.schematic.path

    def save(self) -> None:
        """Write the file through ``kicad-cli sch upgrade`` (validation + KiCad formatting)."""
        self.schematic.save()

    # --- helpers --------------------------------------------------------------

    def point(self, p: Point) -> tuple[float, float]:
        """Resolve ``"REF.PIN"``, ``"X,Y"`` or ``(x, y)`` to sheet coordinates."""
        if not isinstance(p, str):
            x, y = p
            return float(x), float(y)
        if "," in p:
            return xy(p)
        if "." not in p:
            raise env.KatError(f"expected REF.PIN or X,Y, got {p!r}")
        ref, pin = p.split(".", 1)
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
        """Placed symbols, labels, wires and the number of junctions."""
        s = self.schematic
        syms = [{k: v for k, v in self._info(x).items() if k != "pins"} for x in s.symbols()]
        return {"symbols": syms, "labels": s.labels(),
                "wires": [[list(p), list(q)] for p, q in s.segments()],
                "junctions": len(find_all(s.tree, "junction"))}

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

    def nc(self, at: Point) -> dict:
        """No-connect flag."""
        p = self.point(at)
        self.schematic.add_no_connect(*p)
        return {"no_connect": p}

    # --- rearranging ------------------------------------------------------------

    def move(self, ref: str, to: Point | None = None, by: Point | None = None,
             rot: float | None = None, mirror: str | None = None, unit: int | None = None,
             drag: bool = True) -> list[dict]:
        """Move, rotate or mirror a part. What hangs only off it is carried along; wires
        into shared nets are dragged (unless ``drag=False``)."""
        s = self.schematic
        tx, ty = self.point(to) if to is not None else (None, None)
        dx, dy = (xy(by) if isinstance(by, str) else by) if by is not None else (0, 0)
        s.move_symbol(ref, tx, ty, dx, dy, rot, mirror, unit, drag=drag)
        s.fix_junctions()
        return [self._info(x) for x in s.find_symbols(ref, unit)]

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

    def detach(self, pin: str) -> dict:
        """Disconnect ``REF.PIN`` from everything drawn at it (own wire run, labels, power
        symbols, flags)."""
        if "." not in pin:
            raise env.KatError(f"expected REF.PIN, got {pin!r}")
        return self.schematic.detach(*pin.split(".", 1))

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
