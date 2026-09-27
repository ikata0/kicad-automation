"""KiCad 10 board file (.kicad_pcb) on the lossless s-expression model.

Nets live on the pads by name (no net table in KiCad 10). Every save goes through
``kicad-cli pcb upgrade``: KiCad validates the file and writes its own formatting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import env
from ..sexpr import Sym, dumps, find, find_all, head, loads, num
from . import footprints as fps
from .geometry import Box, box_of, pad_outline, to_board, union
from .finish import FinishMixin
from .placement import PlacementMixin
from .setup import SetupMixin
from .structure import StructureMixin

FORMAT = "20260206"


def _f(node, key, i=1, default=0.0) -> float:
    n = find(node, key)
    return float(n[i]) if n is not None and len(n) > i else default


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
    def outline(self) -> list[tuple[float, float]]:
        """The pad's copper outline as a polygon in board mm (circles and ovals as 16-gons)."""
        return pad_outline(self.shape, self.x, self.y, self.w, self.h, self.angle)

    @property
    def box(self) -> Box:
        """Bounding box (x0, y0, x1, y1) of the pad's copper in board mm."""
        return box_of(self.outline)

    def on(self, layer: str) -> bool:
        """True if the pad has copper on ``layer`` (wildcards such as *.Cu resolved)."""
        return any(l == layer or (l.startswith("*.") and layer.endswith(l[1:]))
                   or (l == "F&B.Cu" and layer in ("F.Cu", "B.Cu")) for l in self.layers)


class Footprint:
    """A footprint on the board. Position ``x``, ``y`` in mm, ``rot`` in degrees, ``side``
    "F" or "B"; ``ref``, ``value``, ``lib_id``, ``path`` (link to the schematic symbol)."""

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

    def set_field(self, name: str, value: str, hide: bool = True) -> None:
        """Set a property; a new one is hidden on the fab layer of the footprint's side."""
        p = self._prop(name)
        if p is not None:
            p[2] = value
            return
        from ..schematic import new_uuid
        effects = [Sym("effects"), [Sym("font"), [Sym("size"), Sym("1"), Sym("1")],
                                    [Sym("thickness"), Sym("0.15")]]]
        if self.side == "B":
            effects.append([Sym("justify"), Sym("mirror")])
        at = [Sym("at"), Sym("0"), Sym("0")] + ([num(self.rot)] if self.rot else [])
        new = [Sym("property"), name, value, at, [Sym("layer"), f"{self.side}.Fab"]]
        if hide:
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
        layer = f"{self.side}.{layer_suffix}"
        pts = []
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
                pts += [(cx - r, cy - r), (cx + r, cy + r)]
                continue
            local = [(float(n[1]), float(n[2])) for k in ("start", "mid", "end") if (n := find(g, k))]
            if h == "fp_rect" and len(local) == 2:
                (x0, y0), (x1, y1) = local
                local = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            if h == "fp_poly":
                local = [(float(xy[1]), float(xy[2])) for xy in find_all(find(g, "pts") or [], "xy")]
            pts += [self.to_board(x, y) for x, y in local]
        return box_of(pts) if pts else None

    def courtyard(self) -> Box | None:
        """Courtyard bounding box in board mm, or None without a courtyard."""
        return self.graphics_box("CrtYd")

    def box(self) -> Box | None:
        """Courtyard, or the pads' extent for footprints without one."""
        return self.courtyard() or union(p.box for p in self.pads())

    def place(self, x: float | None = None, y: float | None = None, rot: float | None = None) -> None:
        """Set position (mm) and/or rotation (degrees) without any check; the checked way
        is ``Board.move``."""
        at = find(self.node, "at")
        if x is not None:
            at[1] = num(x)
        if y is not None:
            at[2] = num(y)
        if rot is not None:
            fps.set_rotation(self.node, rot)

    def flip(self) -> None:
        """Move to the other side, mirrored like KiCad's flip (y mirror, same position)."""
        rot = self.rot
        fps.set_rotation(self.node, 0)
        fps.mirror(self.node)
        fps.set_rotation(self.node, rot)

    def info(self) -> dict:
        """ref, value, footprint, x, y, rot, side, box as a dict."""
        return {"ref": self.ref, "value": self.value, "footprint": self.lib_id,
                "x": self.x, "y": self.y, "rot": self.rot, "side": self.side,
                "box": list(b) if (b := self.box()) else None}


class Board(SetupMixin, PlacementMixin, StructureMixin, FinishMixin):
    """A KiCad 10 board file, edited in memory and saved once through ``kicad-cli pcb
    upgrade``. Use it inside ``with kat.pcb.edit(PCB) as b:``. Coordinates in mm, y grows
    downwards, angles in degrees. Pads are named ``"REF.NUMBER"`` (``"U1.8"``).
    Methods by phase: setup (outline, layers, plane, zone, keepout, netclass, rules,
    mounting_hole), placement (move, move_many, place_near, align, distribute, blocks,
    replicate), structure (track, via, fanout, stitch, lock, remove_tracks), finishing
    (place_refs, board_text, title_from_schematic, fiducials)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.tree = loads(self.path.read_text(encoding="utf-8"))
        if head(self.tree) != "kicad_pcb":
            raise env.KatError(f"{path} is not a KiCad board")
        self._pro_edits: list = []
        self._rules_text: str | None = None

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
        ``kicad-cli pcb upgrade``; ``kat.pcb.edit`` calls this for you."""
        self._write_project_files()
        self.path.write_text(dumps(self.tree) + "\n", encoding="utf-8")
        normalize(self.path)
        self.tree = loads(self.path.read_text(encoding="utf-8"))

    @property
    def project_dir(self) -> Path:
        """The directory holding the board (and its project files)."""
        return self.path.parent

    # --- footprints -----------------------------------------------------------------

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
        """Vias: at, size, drill, net, layers, locked, uuid, node."""
        out = []
        for n in find_all(self.tree, "via"):
            at, net = find(n, "at"), find(n, "net")
            out.append({"at": (float(at[1]), float(at[2])), "size": _f(n, "size"),
                        "drill": _f(n, "drill"), "net": str(net[1]) if net is not None else "",
                        "layers": tuple(str(l) for l in find(n, "layers")[1:]),
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
