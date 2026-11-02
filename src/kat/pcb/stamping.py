"""Stamping a checked block layout into a design: the block's placement, copper, keep-outs,
graphics and board-only parts are copied onto one instance of the block. Mixed into
``Board``.

A block board is the layout of one block, checked on its own (DRC clean). An instance is
named by its path, ``/CH_A`` or ``/CH_A/RC1``, the same way for both kinds of design:

- **hierarchical**: the instance is a sheet of the design's schematic. A part pairs with
  the block part whose footprint path is the end of its own path (the block's schematic
  file is shared, so symbol uuids are the same).
- **flat**: the instance is a SKiDL block (``Block`` field ``CH_A``). A part pairs with the
  block part that has the same tag relative to its instance (``CH_A/RC1/r`` ↔
  ``DUT/RC1/r``). On the block board, parts outside the block instance (a test harness,
  e.g. a connector) are its *environment* and are not copied, nor is copper touching them.

Nets are renamed through the paired pads; an instance wired differently from the block is
refused. Zones named in the block's ``environment`` (the block file ``NAME.block.json``,
see ``set_environment``) are what the block was checked against, e.g. a GND plane, and are
not copied. Everything copied goes into a KiCad group ``"/CH_A (NAME)"``; stamping the same
instance again replaces it.

On the bottom side (``side="bottom"``) the block is mirrored about its outline's vertical
axis (KiCad's left/right flip), then turned, and its layers go through a layer map
(``layers.copper_map``): footprints flip, copper and zones change layers, via spans map
(a, b) -> (map(b), map(a)).
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace

from .. import env
from ..schematic import new_uuid
from ..sexpr import Sym, find, find_all, head, num
from . import footprints as fps
from . import layers as lay
from .geometry import box_of, hull, inside, overlap, point_seg, rotate

COPPER = ("segment", "arc", "via", "zone")
GRAPHICS = ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve", "gr_text",
            "gr_text_box")


# --- the block file -----------------------------------------------------------------------

def block_file(block_pcb: Path | str) -> Path:
    """``NAME.block.json`` next to the block board."""
    return Path(block_pcb).with_suffix(".block.json")


def block_info(block_pcb: Path | str) -> dict:
    """The block file's content (``{}`` if there is none)."""
    f = block_file(block_pcb)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def set_environment(block_pcb: Path | str, zones: list[str]) -> dict:
    """Name the zones of a block board that are its test environment (e.g. the plane it
    was checked against): ``stamp`` does not copy them. Written to the block file."""
    info = block_info(block_pcb)
    info["environment"] = sorted(set(zones))
    block_file(block_pcb).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def set_port(block_pcb: Path | str, port: str, at: tuple[float, float], net: str | None = None) -> dict:
    """Mark where port ``port`` leaves the block board: the via or pad at ``at`` (mm),
    or the end of a track that leaves at the outline (``kind`` track: on the edge or
    within the copper-edge clearance plus half its width; ``layer`` recorded), on the
    port's net. Stored in the block file; ``kat block check`` checks it (still there, on
    its net, near the outline's edge) and ``stamp`` reports it on the design
    (``Board.ports``), so the design is routed to known points."""
    from .board import Board
    b = Board(block_pcb)
    x, y = at
    layer = None
    hit = next((("via", v["net"]) for v in b.vias()
                if abs(v["at"][0] - x) < 0.01 and abs(v["at"][1] - y) < 0.01), None)
    if hit is None:
        hit = next((("pad", p.net) for p in b.pads() if inside(p.outline, (x, y))), None)
    if hit is None:
        t = _track_end(b, (x, y))
        if t is not None:
            box = b.outline_box()
            reach = (b.board_minimums().get("min_copper_edge_clearance", 0) or 0) + t["width"] / 2 + 0.01
            d = min(x - box[0], box[2] - x, y - box[1], box[3] - y) if box else None
            if d is None or not -0.01 <= d <= reach:
                raise env.KatError(f"the track end at {x:g},{y:g} is {d:g} mm inside the outline: a track "
                                   f"is a port only where it leaves at the edge (within {reach:g} mm); "
                                   "else end it on a via" if d is not None else "the block board has no outline")
            hit, layer = ("track", t["net"]), t["layer"]
    if hit is None:
        raise env.KatError(f"no via, pad or track end at {x:g},{y:g} on {Path(block_pcb).name}: "
                           "place the port's via first")
    kind, on = hit
    if net and on != net:
        raise env.KatError(f"the {kind} at {x:g},{y:g} is on {on or 'no net'}, not {net}")
    info = block_info(block_pcb)
    pts = [q for q in info.get("port_points", []) if q["port"] != port]
    pts.append({"port": port, "net": on, "x": x, "y": y, "kind": kind, **({"layer": layer} if layer else {})})
    info["port_points"] = sorted(pts, key=lambda q: q["port"])
    block_file(block_pcb).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return {"port": port, "net": on, "kind": kind, "at": [x, y], **({"layer": layer} if layer else {})}


def _track_end(b, at: tuple[float, float]) -> dict | None:
    """The track or arc of board ``b`` that ends at ``at`` (0.01 mm), or None."""
    return next((t for t in b.tracks() if any(abs(e[0] - at[0]) < 0.01 and abs(e[1] - at[1]) < 0.01
                                              for e in (t["start"], t["end"]))), None)


def stamps_file(pcb: Path | str) -> Path:
    """``<board>.stamps.json``: where each instance was stamped (block, at, rot) and its ports."""
    return Path(pcb).with_suffix(".stamps.json")


# --- helpers ------------------------------------------------------------------------------

def _norm(instance: str) -> str:
    return "/" + instance.strip("/")


class _Place:
    """Where a block board's items go. Top side: the block origin (top-left of ``box``)
    to ``at``, turned by ``rot``. Bottom side: first mirrored about the box's vertical
    axis, so ``at`` is the top-left corner of the flipped outline as seen from the top.
    ``layer``: layer name -> layer name."""

    def __init__(self, box, at, rot: float, bottom: bool = False, layer=None):
        self.box, self.at, self.rot, self.bottom = box, at, rot, bottom
        self.layer = layer or (lambda name: name)

    def __call__(self, x: float, y: float) -> tuple[float, float]:
        lx = self.box[2] - x if self.bottom else x - self.box[0]
        dx, dy = rotate(lx, y - self.box[1], self.rot)
        return round(self.at[0] + dx, 6), round(self.at[1] + dy, 6)

    def text_angle(self, a: float) -> float:
        """A board text's angle (KiCad flips text left/right to -angle)."""
        return ((-a if self.bottom else a) + self.rot) % 360

    def fp_angle(self, a: float) -> float:
        """A footprint's angle (KiCad's left/right flip: 180 - angle)."""
        return ((180 - a if self.bottom else a) + self.rot) % 360

    def side(self, side: str) -> str:
        return ("B" if side == "F" else "F") if self.bottom else side

    def box_of(self, b) -> tuple[float, float, float, float]:
        return box_of([self(x, y) for x in (b[0], b[2]) for y in (b[1], b[3])])

    def item(self, c: list, order: list[str]) -> None:
        """Move a copied track, via, zone or graphic in place; ``order``: the design's
        copper layers, top first (a via's span is written top first)."""
        if self.rot % 90:
            _as_polygon(c, self.text_angle)
        _move_points(c, self, self.text_angle)
        _map_layers(c, self.layer)
        if head(c) == "via":
            ls = find(c, "layers")
            if ls is not None and all(str(t) in order for t in ls[1:]):
                ls[1:] = sorted(ls[1:], key=lambda t: order.index(str(t)))
        if self.bottom:
            fps.swap_sided(c)
            eff = find(c, "effects")
            if eff is not None and head(c) in ("gr_text", "gr_text_box"):
                just = find(eff, "justify")
                if just is None:
                    eff.append([Sym("justify"), Sym("mirror")])
                elif any(str(t) == "mirror" for t in just[1:]):
                    just[:] = [t for t in just if str(t) != "mirror"]
                    if len(just) == 1:
                        eff.remove(just)
                else:
                    just.append(Sym("mirror"))

    def footprint(self, c: list, src: list) -> None:
        """Place a copied footprint ``c`` (a copy of ``src``): flipped on the bottom side."""
        sat = find(src, "at")
        a = float(sat[3]) if len(sat) > 3 else 0.0
        if self.bottom:
            fps.set_rotation(c, 0)
            fps.mirror(c, swap=self.layer)
        fps.set_rotation(c, self.fp_angle(a))
        cat = find(c, "at")
        cat[1], cat[2] = (num(v) for v in self(float(sat[1]), float(sat[2])))


def _as_polygon(c: list, angle) -> None:
    """A board rectangle or text box as its corner points, in place, since start and end cannot
    describe one turned by other than 90 degrees; a text box also gets its turned angle."""
    if head(c) not in ("gr_rect", "gr_text_box") or find(c, "start") is None:
        return
    s, e = find(c, "start"), find(c, "end")
    (x0, y0), (x1, y1) = (float(s[1]), float(s[2])), (float(e[1]), float(e[2]))
    i = c.index(s)
    c.remove(s)
    c.remove(e)
    c.insert(i, [Sym("pts"), *([Sym("xy"), num(x), num(y)] for x, y in
                               ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))])
    if head(c) == "gr_rect":
        c[0] = Sym("gr_poly")
        return
    old = find(c, "angle")
    if old is None:
        c.insert(i + 1, [Sym("angle"), num(angle(0.0))])
    else:
        old[1] = num(angle(float(old[1])))


def _move_points(node: list, f, angle) -> None:
    """Points through ``f``; the angle of a text's ``at`` through ``angle``."""
    for c in node:
        if not isinstance(c, list):
            continue
        h = head(c)
        if h in ("start", "end", "mid", "center", "at", "xy") and len(c) >= 3:
            c[1], c[2] = (num(v) for v in f(float(c[1]), float(c[2])))
            if h == "at" and len(c) >= 4 and not isinstance(c[3], list):
                c[3] = num(angle(float(c[3])))
            elif h == "at" and head(node) in ("gr_text", "gr_text_box") and (a := angle(0.0)):
                c.append(num(a))
        elif h == "pts" or h not in ("net", "layer", "layers", "uuid"):
            _move_points(c, f, angle)


def _map_layers(node: list, f) -> None:
    for c in node:
        if not isinstance(c, list):
            continue
        if head(c) in ("layer", "layers"):
            for j in range(1, len(c)):
                if isinstance(c[j], str) and not isinstance(c[j], Sym):
                    c[j] = f(c[j])
        _map_layers(c, f)


def _taken(fp) -> list[tuple[str, list]]:
    """(side, polygon) a part takes: its courtyard on its side, and each through-hole pad and
    hole on the other side too, one polygon each, so the space between distant holes stays
    free. Without a courtyard: the hull of its pads, or for holes only (guide holes) each hole.
    Polygons turn with the part: at 45 degrees they are no bounding boxes."""
    other = "B" if fp.side == "F" else "F"
    pads = fp.pads()
    thru = [p for p in pads if p.kind in ("thru_hole", "np_thru_hole")]
    holes = [hull(p.outline + (p.hole_outline or [])) for p in thru]
    court = fp.courtyard_outline()
    if court:
        own = [court]
    elif thru and all(p.kind == "np_thru_hole" for p in pads):
        own = holes
    else:
        own = [hull(q for p in pads for q in p.outline)] if pads else []
    return [(fp.side, p) for p in own] + [(other, p) for p in holes]


def _refuse_locked(src, pairs: dict, instance: str, f: "_Place") -> None:
    """KatError if the stamp would move a locked part of the instance (one already where
    the stamp puts it is fine, so stamping again in place still works)."""
    held = []
    for sf in src.footprints():
        tf = pairs.get(sf.uuid)
        if tf is None or not tf.locked:
            continue
        x, y = f(sf.x, sf.y)
        if (abs(tf.x - x) > 1e-3 or abs(tf.y - y) > 1e-3 or tf.side != f.side(sf.side)
                or abs((f.fp_angle(sf.rot) - tf.rot + 180) % 360 - 180) > 1e-3):
            held.append(tf.ref)
    if held:
        raise env.KatError(f"{instance}: {', '.join(sorted(held))} locked where the stamp would move "
                           "them: unlock them first (b.footprint(ref).unlock()), or stamp elsewhere")


def _netmap(src, pairs: dict, instance: str) -> dict[str, str]:
    """Block net -> the instance's net, through the paired pads; KatError if the instance is
    wired differently from the block."""
    out: dict[str, str] = {}
    for sf in src.footprints():
        tf = pairs.get(sf.uuid)
        if tf is None:
            continue
        for sp in sf.pads():
            if not sp.net:
                continue
            tp = tf.pad(sp.number)
            if out.setdefault(sp.net, tp.net) != tp.net:
                raise env.KatError(f"net {sp.net} maps to {out[sp.net]} and {tp.net}: "
                                   f"{instance} is wired differently from the block")
    return out


def _placed_ports(info: dict, f: "_Place", netmap: dict) -> dict:
    """The block file's port points through a placement: {port: {x, y, net[, layer]}}."""
    out = {}
    for q in info.get("port_points", []):
        x, y = f(q["x"], q["y"])
        out[q["port"]] = {"x": x, "y": y, "net": netmap.get(q["net"], q["net"]),
                          **({"layer": f.layer(q["layer"])} if q.get("layer") else {})}
    return out


EDGES = {0: "right", 45: "top-right", 90: "top", 135: "top-left", 180: "left", 225: "bottom-left",
         270: "bottom", 315: "bottom-right"}


def port_order(box, f: "_Place", ports: dict) -> dict[str, list[str]]:
    """Ports per edge of the placed outline, as seen from the top: {edge: [port, ...]}. Edges
    are named by the way they face (``top``, ``right``, ``bottom``, ``left``; at 45 degrees
    ``top-right``, ...); each list runs left to right, on ``left``/``right`` top to bottom. A
    port belongs to the nearest edge."""
    corners = [f(x, y) for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]
    cx, cy = sum(c[0] for c in corners) / 4, sum(c[1] for c in corners) / 4
    edges = []
    for a, b in zip(corners, corners[1:] + corners[:1]):
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        deg = round(math.degrees(math.atan2(cy - my, mx - cx)) / 45) * 45 % 360    # y grows downwards
        edges.append((EDGES[deg], a, b))
    out: dict[str, list[tuple]] = {}
    for name, q in ports.items():
        edge = min(edges, key=lambda e: point_seg((q["x"], q["y"]), e[1], e[2]))[0]
        key = (q["y"], q["x"]) if edge in ("left", "right") else (q["x"], q["y"])
        out.setdefault(edge, []).append((key, name))
    return {e: [n for _, n in sorted(v)] for e, v in sorted(out.items())}


def _collision_text(rows: list[dict], order: list[str]) -> str:
    """The refusal for collisions found while stamping ``order``: the first rows, and every
    colliding pair when there are several."""
    from collections import Counter
    from . import cells
    names = sorted({r["i"] for r in rows}, key=order.index)
    pairs = Counter((r["i"], r["j"]) for r in rows)
    return (f"{', '.join(names)} collide{'s' if len(names) == 1 else ''} with stamped instances: "
            + "; ".join(cells.text(r) for r in rows[:8])
            + (f" (+{len(rows) - 8} more)" if len(rows) > 8 else "")
            + (f"; colliding pairs: {', '.join(f'{a} / {b} ({n})' for (a, b), n in pairs.items())}"
               if len(pairs) > 1 else "")
            + (f"; stamped in the order {', '.join(order)}" if len(order) > 1 else "")
            + ": stamp at a larger pitch (kat pcb cell BLOCK --min-pitch x)")


def _uuid(node: list) -> str | None:
    u = find(node, "uuid")
    return str(u[1]) if u is not None else None


def _fresh(node: list) -> str:
    u = new_uuid()
    find(node, "uuid")[1] = u
    return u


def _joined(instance: str, name: str) -> str:
    """Zone name of a copy: ``/CH_A: out node``; nested ``/CH_A`` + ``/RC1: out node``."""
    if name.startswith("/") and ": " in name:
        return instance + name
    return f"{instance}: {name}"


def _group_name(instance: str, name: str) -> str:
    return instance + name if name.startswith("/") else f"{instance} {name}"


def _harness_copper(src, block_pads: list, env_pads: list, nets: set[str],
                    keep: set[tuple[float, float]] = frozenset()) -> set[int]:
    """Copper of the block board that serves only its harness: on ``nets`` (port nets),
    the segments touching a harness pad, then every segment or arc with an end that
    reaches nothing (no block pad, via or other kept segment), until none is left; then
    the vias left with no copper (``keep``: points of port vias, kept anyway). Returns
    the ids of the nodes not to stamp."""
    def pt(n, key):
        v = find(n, key)
        return (round(float(v[1]), 4), round(float(v[2]), 4))

    def net(n):
        v = find(n, "net")
        return str(v[1]) if v is not None else ""

    segs = [n for n in src.tree if head(n) in ("segment", "arc") and net(n) in nets]
    vias = [n for n in src.tree if head(n) == "via" and net(n) in nets]
    pads = [(p.net, p) for p in block_pads if p.net in nets]
    drop = {id(n) for n in segs if any(inside(p.outline, pt(n, k)) for p in env_pads for k in ("start", "end"))}

    def held(q, n, lay) -> bool:
        if any(pn == net(n) and inside(p.outline, q) for pn, p in pads):
            return True
        if any(id(v) not in drop and pt(v, "at") == q for v in vias):
            return True
        return any(m is not n and id(m) not in drop and str(find(m, "layer")[1]) == lay
                   and q in (pt(m, "start"), pt(m, "end")) for m in segs)

    changed = True
    while changed:
        changed = False
        for n in segs:
            if id(n) in drop:
                continue
            lay = str(find(n, "layer")[1])
            if not all(held(pt(n, k), n, lay) for k in ("start", "end")):
                drop.add(id(n))
                changed = True
        for v in vias:
            q = pt(v, "at")
            if id(v) in drop or q in keep:
                continue
            had = [n for n in segs if q in (pt(n, "start"), pt(n, "end"))]
            if had and all(id(n) in drop for n in had) and not any(inside(p.outline, q) for _, p in pads):
                drop.add(id(v))
                changed = True
    return drop


def _item_layers(node: list) -> set[str]:
    out = set()
    for key in ("layer", "layers"):
        n = find(node, key)
        if n is not None:
            out |= {str(l) for l in n[1:] if isinstance(l, str) or not isinstance(l, list)}
    return {l for l in out if l.endswith(".Cu") and not l.startswith("*")}


class StampMixin:
    # --- instances ------------------------------------------------------------------------

    def _design_sheet(self) -> Path:
        return self.path.with_suffix(".kicad_sch")

    def _hier_prefix(self, instance: str) -> str | None:
        """uuid path of a sheet instance of the design's schematic, None if there is none."""
        from .. import hierarchy
        sch = self._design_sheet()
        if not sch.exists():
            return None
        try:
            return hierarchy.instance(sch, instance)["uuids"]
        except env.KatError:
            return None

    @staticmethod
    def _source_instance(src, name: str | None) -> str:
        """The block instance on a block board (flat): ``name``, or the only top-level
        ``Block`` there, or "" if the parts carry no Block (the whole board is the block)."""
        tops = {f.field("Block").split("/")[0] for f in src.footprints()
                if not f.board_only and f.field("Block")}
        if name is not None:
            return name.strip("/")
        if len(tops) > 1:
            raise env.KatError(f"the block board has several block instances {sorted(tops)}: "
                               f"name one with source_instance=")
        return tops.pop() if tops else ""

    def _pairs(self, src, instance: str, source_instance: str | None) -> tuple[dict, list, str]:
        """(source footprint uuid -> target Footprint, environment footprints, mode)."""
        from ..syncer import natural_key
        prefix = self._hier_prefix(instance)
        parts = [f for f in src.footprints() if not f.board_only]
        pairs: dict[str, object] = {}
        environment = []
        if prefix is not None:
            mode = "hierarchical"
            by_path = {f.path: f for f in self.footprints() if f.path}
            # a library block board: the block is the sheet instance /DUT, the rest harness
            sprefix = ""
            if source_instance and src.path.with_suffix(".kicad_sch").exists():
                from .. import hierarchy
                try:
                    sprefix = hierarchy.instance(src.path.with_suffix(".kicad_sch"), source_instance)["uuids"]
                except env.KatError:
                    sprefix = ""
            if sprefix:
                environment = [sf for sf in parts if not sf.path.startswith(sprefix + "/")]
                parts = [sf for sf in parts if sf.path.startswith(sprefix + "/")]
            for sf in parts:
                tf = by_path.get(prefix + sf.path[len(sprefix):])
                if tf is None:
                    raise env.KatError(f"{sf.ref}: no part {prefix + sf.path[len(sprefix):]} in {instance} "
                                       f"(is {instance} an instance of this block?)")
                pairs[sf.uuid] = tf
            inside_ = [f for f in self.footprints() if f.path.startswith(prefix + "/")]
        else:
            mode = "flat"
            name = instance.strip("/")
            sname = self._source_instance(src, source_instance)

            def local(f, root: str) -> str | None:
                tag, block = f.field("SKiDL Tag") or "", f.field("Block") or ""
                if root and not (block == root or block.startswith(root + "/")):
                    return None
                if root and tag.startswith(root + "/"):
                    return tag[len(root) + 1:]
                return tag or None

            hier_keys = _hier_source_keys(src, sname)

            keys: dict[str, object] = {}
            for sf in parts:
                k = hier_keys.get(sf.uuid, None) if hier_keys else local(sf, sname)
                if k is None:
                    environment.append(sf)
                    continue
                if k in keys:
                    raise env.KatError(f"block parts {keys[k].ref} and {sf.ref} share the tag {k!r}")
                keys[k] = sf
            if not keys:
                raise env.KatError("the block board has no parts with SKiDL tags to pair by")
            inside_ = [f for f in self.footprints() if not f.board_only and
                       ((f.field("Block") or "") == name or (f.field("Block") or "").startswith(name + "/"))]
            if not inside_:
                raise env.KatError(f"no instance {instance}: no sheet of that name and no parts "
                                   f"with Block = {name}")
            by_key = {local(f, name): f for f in inside_}
            for k, sf in keys.items():
                tf = by_key.get(k)
                if tf is None:
                    raise env.KatError(f"{sf.ref}: no part with tag {name}/{k} in {instance} "
                                       f"(is {instance} an instance of this block?)")
                pairs[sf.uuid] = tf
        extra = sorted({f.ref for f in inside_} - {f.ref for f in pairs.values()}, key=natural_key)
        if extra:
            raise env.KatError(f"{instance} has parts the block does not have: {', '.join(extra)}")
        return pairs, environment, mode

    def instances(self, block_pcb: Path | str, source_instance: str | None = None) -> list[str]:
        """Instances of a block in this design, as paths (``/CH_A``): sheets using the
        block's schematic file (hierarchical), or blocks whose parts pair with the block
        board's parts (flat)."""
        from .. import blocks as lib
        from .. import hierarchy
        from .board import Board
        block_pcb, lib_instance = lib.board_of(block_pcb, near=self.path)
        source_instance = source_instance or lib_instance
        sch = self._design_sheet()
        bsch = {block_pcb.with_suffix(".kicad_sch").resolve(),
                block_pcb.with_name(block_pcb.stem + "_block.kicad_sch").resolve()}
        if sch.exists():
            found = [i["path"] for i in hierarchy.tree(sch)
                     if Path(i["file"]).resolve() in bsch and i["path"] != "/"]
            if found:
                return found
        src = Board(block_pcb)
        out = []
        for name in sorted(self.blocks()):
            try:
                self._pairs(src, "/" + name, source_instance)
            except env.KatError:
                continue
            out.append("/" + name)
        return out

    # --- stamping ---------------------------------------------------------------------------

    def stamp(self, block_pcb: Path | str, instance: str | dict, at: tuple[float, float] | None = None,
              rot: float = 0.0, lock: bool = True, source_instance: str | None = None,
              make_room: bool = False, side: str | None = None, layer_map: str | dict | None = None,
              allow_asymmetric: bool = False, anchor: str | None = None) -> dict:
        """Copy a checked block layout (a block board, or a block name from the library)
        onto one instance of the block: parts placed as on
        the block board, tracks, vias, zones, keep-outs, graphics and board-only parts
        copied with the instance's nets. ``at``: where the block board's origin (top-left
        of its outline) goes, mm; the block turns about that point by ``rot`` (a multiple of 45
        degrees, counter-clockwise), so at 90 it lies above ``at``. ``anchor="top-left"``:
        ``at`` is the top-left corner of the turned outline's bounding box instead (None: as
        recorded for the instance, else ``"origin"``). The result gives the turned bounds as
        ``outline`` (x0, y0, x1, y1 mm) and the point the origin went to as ``origin``;
        ``stamp_preview`` tells the same without stamping. Several instances at once:
        ``instance={"/CH_A": (x, y), "/CH_B": ((x, y), 180)}`` (or ``((x, y), rot, side)``):
        the result does not depend on their order. All of them come off the board first, so
        they may swap places and only their final geometry counts; then they go on in ``order``
        (natural order of their names, returned), and every collision is reported at once. If
        anything is refused, the board stays as it was. Copied copper is
        locked (``lock=False`` leaves it free), so routing keeps it. Stamping again
        replaces the earlier copy. Parts of the design outside block instances that would
        overlap the block's parts (courtyards on the same side; through-hole pads on both)
        are refused, or with ``make_room`` pushed out of the way. Stamped instances are
        judged per layer (copper with clearance, same net allowed; courtyards per side;
        keep-outs), so neighbouring blocks may interlock; a collision is refused
        (``stamp_collisions``, ``kat.pcb.min_pitch``). Parts of block instances not stamped
        yet are no obstacles. Locked parts of the instance are refused where the stamp would
        move them. The design's net classes
        on the block's nets must not be stricter than the block's.

        ``side="bottom"``: the block flipped to the other side, mirrored about its
        vertical axis and then turned by ``rot`` (as seen from the top); ``at`` is the
        top-left corner of the flipped outline. ``layer_map``: "auto" (the mirror map,
        refused on an asymmetric stack-up unless ``allow_asymmetric``), "roles", or
        explicit ``"F.Cu=B.Cu,In1.Cu=In2.Cu"``. ``side``/``layer_map`` None: as recorded
        for the instance by an earlier stamp, else top / auto. The design's layer rules
        (``layer_rule``) are checked on the copied copper. Returns pairs, renamed nets,
        side, layer map, origin, outline and counts; several instances: ``instances`` (those
        results, in ``order``) and ``order``."""
        if isinstance(instance, dict):
            wanted = []
            for name, where in instance.items():
                pos, r, sd = where, rot, side
                if isinstance(where[0], (tuple, list)):
                    pos, r, sd = (*where, side)[:3] if len(where) == 2 else where
                wanted.append((name, pos, r, sd))
            return self._stamp_batch(block_pcb, wanted, lock, source_instance, make_room, layer_map,
                                     allow_asymmetric, anchor)
        return self._stamp_batch(block_pcb, [(instance, at, rot, side)], lock, source_instance, make_room,
                                 layer_map, allow_asymmetric, anchor)["instances"][0]

    def _stamp_batch(self, block_pcb, wanted: list[tuple], lock: bool, source_instance: str | None,
                     make_room: bool, layer_map, allow_asymmetric: bool, anchor: str | None) -> dict:
        """Stamp (instance, at, rot, side) entries: all off the board, then on in natural order,
        collisions gathered and reported together; on any refusal the board is restored."""
        from ..syncer import natural_key
        where = {}
        for name, pos, r, sd in wanted:
            if _norm(name) in where:
                raise env.KatError(f"{_norm(name)} is named twice in one stamp")
            where[_norm(name)] = (pos, r, sd)
        order = sorted(where, key=natural_key)
        saved = copy.deepcopy(self.tree), dict(self._stamps)
        try:
            for name in order:
                self._unstamp(name)
            done, rows = [], []
            for name in order:
                pos, r, sd = where[name]
                done.append(self._stamp_one(block_pcb, name, pos, r, lock, source_instance, make_room, sd,
                                            layer_map, allow_asymmetric, anchor, rows))
            if rows:
                raise env.KatError(_collision_text(rows, order))
        except Exception:
            self.tree[:] = saved[0]
            self._stamps = saved[1]
            raise
        return {"instances": done, "order": order}

    def _stamp_one(self, block_pcb, instance: str, at, rot: float, lock: bool, source_instance: str | None,
                   make_room: bool, side: str | None, layer_map, allow_asymmetric: bool, anchor: str | None,
                   collisions: list[dict]) -> dict:
        """One instance of ``stamp``; its collisions with stamped instances go to ``collisions``."""
        from . import cells, coupling
        plan = self._stamp_plan(block_pcb, instance, at, rot, side, layer_map, allow_asymmetric,
                                source_instance, anchor)
        block_pcb, side, bottom, layer_map, warnings = (plan.block_pcb, plan.side, plan.bottom,
                                                        plan.layer_map, plan.warnings)
        src, block, info, pairs, environment, mode = (plan.src, plan.block, plan.info, plan.pairs,
                                                      plan.environment, plan.mode)
        cmap, order, f = plan.cmap, plan.order, plan.f
        env_zones = set(info.get("environment", []))
        _refuse_locked(src, pairs, instance, f)
        moved = self._room_for(src, pairs, instance, f, make_room)
        self._unstamp(instance)

        # parts
        netmap = _netmap(src, pairs, instance)
        env_pads = [p for ef in environment for p in ef.pads()]
        env_nets = {p.net for p in env_pads if p.net}
        members: dict[str, str] = {}          # source uuid -> target uuid (for groups)
        placed = {}
        for sf in src.footprints():
            if sf.uuid not in pairs:
                continue
            tf = pairs[sf.uuid]
            if tf.lib_id != sf.lib_id:
                raise env.KatError(f"{sf.ref} -> {tf.ref}: footprint {sf.lib_id} vs {tf.lib_id}")
            if tf.side != f.side(sf.side):          # top again after a bottom stamp: F/B swap
                tf.flip(f.layer if bottom else None)
            x, y = f(sf.x, sf.y)
            tf.place(x, y, f.fp_angle(sf.rot))
            for sp, tp in zip(sf.pads(), tf.pads()):        # the block's own pad connections
                if sp.number == tp.number:
                    tp.set_zone_connection(sp.zone_connection)
            placed[sf.ref] = tf.ref
            members[sf.uuid] = tf.uuid
        # net classes: the block was checked with its own. More clearance here would make
        # its copper DRC errors; another width only changes what the router lays next
        stricter, other = [], []
        for sn, tn in sorted(netmap.items()):
            sr, tr = src.net_rules(sn), self.net_rules(tn)
            if tr["clearance"] > sr["clearance"] + 1e-9:
                stricter.append(f"{tn} ({tr['netclass']}: clearance {tr['clearance']:g} > block {sr['clearance']:g})")
            elif any(abs(sr[k] - tr[k]) > 1e-9 for k in ("track", "clearance")):
                other.append(f"{tn} ({tr['netclass']}: track {tr['track']:g}, clearance {tr['clearance']:g}; "
                             f"block {sr['track']:g}, {sr['clearance']:g})")
        if stricter:
            raise env.KatError(f"the design's net classes ask more clearance than {block} was routed "
                               f"with: {'; '.join(stricter[:8])}: give these nets classes whose clearance "
                               f"matches the block's (netclass), or re-route the block with the design's")
        if other:
            warnings.append(f"net classes differ from the block's (no DRC error; the router uses "
                            f"them outside the block): {'; '.join(other[:8])}")
        target_nets = {p.net for p in self.pads() if p.net} | {
            str(find(z, "net")[1]) for z in self.zones() if find(z, "net") is not None}
        self._ungroup([tf.uuid for tf in pairs.values()])

        def net_of(name: str) -> str | None:
            if name in netmap:
                return netmap[name]
            if name in env_nets:
                return None
            if name in target_nets:
                return name
            raise env.KatError(f"block net {name!r} has no pads in the block and no net of that "
                               f"name in the design")

        # the harness's routes (the block routed with its test connector): not stamped,
        # not even the part of them that ends at a block pad (a stub)
        block_pads = [p for sf in src.footprints() if sf.uuid in pairs for p in sf.pads()]
        port_points = {(round(v["x"], 4), round(v["y"], 4)) for v in info.get("port_points", [])}
        harness = _harness_copper(src, block_pads, env_pads, env_nets & set(netmap), port_points)

        counts = {"footprints": len(placed), "tracks": 0, "vias": 0, "zones": 0, "keepouts": 0,
                  "graphics": 0, "board_parts": 0, "skipped_environment": 0}
        copies = []
        for n in list(src.tree):
            h = head(n)
            if h in COPPER:
                if h == "zone":
                    zname = find(n, "name")
                    if zname is not None and str(zname[1]) in env_zones:
                        counts["skipped_environment"] += 1
                        continue
                c = copy.deepcopy(n)
                net = find(c, "net")
                if net is not None and str(net[1]):
                    mapped = net_of(str(net[1]))
                    if mapped is None:
                        counts["skipped_environment"] += 1
                        continue
                    net[1] = mapped
                if id(n) in harness:
                    counts["skipped_environment"] += 1
                    continue
                if h == "zone":
                    c[:] = [x for x in c if head(x) != "filled_polygon"]
                    zname = find(c, "name")
                    if zname is not None:
                        zname[1] = _joined(instance, str(zname[1]))
                    else:
                        c.insert(2, [Sym("name"), _joined(instance, "zone")])
                    counts["keepouts" if find(c, "keepout") is not None else "zones"] += 1
                else:
                    counts["vias" if h == "via" else "tracks"] += 1
                    if lock and find(c, "locked") is None:
                        c.insert(next(i for i, x in enumerate(c) if isinstance(x, list)
                                      and head(x) in ("layer", "layers")), [Sym("locked"), Sym("yes")])
            elif h in GRAPHICS:
                gl = find(n, "layer")
                if gl is not None and str(gl[1]) == "Edge.Cuts":
                    continue
                c = copy.deepcopy(n)
                counts["graphics"] += 1
            elif h == "footprint":
                if not any(str(x) == "board_only" for x in (find(n, "attr") or [])[1:]):
                    continue
                c = copy.deepcopy(n)
                ref = next(p for p in find_all(c, "property") if str(p[1]) == "Reference")
                ref[2] = self._next_board_ref(str(ref[2]).rstrip("0123456789") or "H",
                                              {str(r[2]) for _, d in copies if head(d) == "footprint"
                                               for r in find_all(d, "property") if str(r[1]) == "Reference"})
                counts["board_parts"] += 1
            else:
                continue
            if h == "footprint":            # origin and rotation (and side): its insides are local
                f.footprint(c, n)
            else:
                f.item(c, order)
                if h == "via":
                    _check_via(c, order)
            copies.append((n, c))

        # the design's layer rules (IN* only on the component side's outer layer, ...)
        bad = coupling.layer_findings(self, [c for _, c in copies if head(c) in COPPER])
        if bad:
            raise env.KatError(f"{instance} on the {side} side breaks the design's layer rules: "
                               + "; ".join(x["message"] for x in bad[:6]))
        # stamped neighbours: per layer, so blocks may interlock (cells.py)
        mine = cells.items([c for _, c in copies if head(c) in COPPER or head(c) == "footprint"]
                           + [tf.node for tf in pairs.values()], order)
        collisions += cells.against(self, instance, mine, skip=frozenset(tf.uuid for tf in pairs.values()))
        for n, c in copies:
            members[_uuid(n)] = _fresh(c)
            self._insert(c)

        groups = self._copy_groups(src, instance, block + (", bottom" if bottom else ""), members)
        ports = _placed_ports(info, f, netmap)
        spec = layer_map if isinstance(layer_map, str) or layer_map is None else dict(layer_map)
        self._stamps[instance] = {"block": str(block_pcb), "at": list(at), "rot": rot, "side": side,
                                  "anchor": plan.anchor, "origin": list(plan.origin), "outline": plan.outline,
                                  "layer_spec": spec, "layer_map": cmap, "ports": ports}
        shown = {k: v for k, v in cmap.items() if k != v}
        return {"instance": instance, "block": block, "mode": mode, "at": list(at), "rot": rot,
                "side": side, "anchor": plan.anchor, "origin": list(plan.origin), "outline": plan.outline,
                **({"layer_map": shown} if shown else {}),
                **({"ports": ports} if ports else {}),
                **({"made_room": moved} if moved else {}),
                "pairs": placed, "nets": {k: v for k, v in netmap.items() if k != v},
                "groups": groups, **counts, **({"warnings": warnings} if warnings else {})}

    def _stamp_plan(self, block_pcb, instance: str | None, at, rot: float, side: str | None, layer_map,
                    allow_asymmetric: bool, source_instance: str | None, anchor: str | None) -> SimpleNamespace:
        """Where a stamp would go, without changing anything: the block board, the instance's
        pairs (none for ``instance`` None), the layer map and the placement."""
        from .. import blocks as lib
        from .board import Board
        if at is None:
            raise env.KatError("stamp needs at=(x, y)")
        if rot % 45:
            raise env.KatError("stamp turns blocks by multiples of 45 degrees only")
        block_pcb, lib_instance = lib.board_of(block_pcb, near=self.path)
        source_instance = source_instance or lib_instance
        earlier = self._stamp_record(instance) if instance else {}
        side = side or earlier.get("side", "top")
        if side not in ("top", "bottom"):
            raise env.KatError(f"side {side!r}: top or bottom")
        bottom = side == "bottom"
        anchor = anchor or earlier.get("anchor", "origin")
        if anchor not in ("origin", "top-left"):
            raise env.KatError(f"anchor {anchor!r}: origin or top-left")
        if layer_map is None:
            layer_map = earlier.get("layer_spec") if earlier.get("side") == side else None
        warnings = []
        info = block_info(block_pcb)
        if info.get("type") and (why := lib.stale(block_pcb.parent)):
            warnings.append(f"block {block_pcb.stem}: {why}")
        src = Board(block_pcb)
        block = block_pcb.stem
        pairs, environment, mode = self._pairs(src, instance, source_instance) if instance else ({}, [], None)

        # layers: the block may only use copper the design has, through the layer map
        cmap = lay.copper_map(layer_map, bottom, self, src, self.path, block_pcb, allow_asymmetric)
        if bottom and info.get("type") and (why := lib.bottom_state(block_pcb.parent, cmap)):
            warnings.append(f"block {block}: {why}")
        used = set()
        for n in src.tree:
            if head(n) in ("segment", "arc", "zone", "via"):
                used |= _item_layers(n)
        for sf in src.footprints():
            if sf.uuid in pairs or sf.board_only:
                used |= {l for p in find_all(sf.node, "pad") for l in _item_layers(p)}
        missing = sorted(l for l in used if l not in cmap)
        if missing:
            raise env.KatError(f"the block uses copper layers the design lacks: {', '.join(missing)}")
        lfn = lay.layer_fn(cmap, bottom)

        box = src.outline_box() or _parts_box(src, pairs or {f.uuid: f for f in src.footprints()})
        origin = (float(at[0]), float(at[1]))
        if anchor == "top-left":
            b = _Place(box, origin, rot, bottom, lfn).box_of(box)
            origin = (round(2 * origin[0] - b[0], 6), round(2 * origin[1] - b[1], 6))
        f = _Place(box, origin, rot, bottom, lfn)
        return SimpleNamespace(block_pcb=block_pcb, side=side, bottom=bottom, layer_map=layer_map,
                               warnings=warnings, src=src, block=block, info=info, pairs=pairs,
                               environment=environment, mode=mode, cmap=cmap, order=self.copper_layers(),
                               box=box, f=f, anchor=anchor, origin=origin,
                               outline=[round(v, 6) for v in f.box_of(box)])

    def stamp_preview(self, block_pcb: Path | str, at: tuple[float, float], rot: float = 0.0,
                      instance: str | None = None, side: str | None = None,
                      layer_map: str | dict | None = None, allow_asymmetric: bool = False,
                      source_instance: str | None = None, anchor: str | None = None) -> dict:
        """Where a stamp would put the block, as seen on this board, without changing it (same
        arguments and refusals of the placement as ``stamp``; collisions are not checked).
        Without ``instance`` the ports carry the block's own net names, so pins can be assigned
        before the design has the instance. Returns instance, block, at, rot, side, anchor,
        ``origin``, ``outline`` (bounds x0, y0, x1, y1 mm), ``ports`` ({port: {x, y, net[,
        layer]}}), ``order`` (ports per edge, ``port_order``), ``layer_map`` and ``warnings``."""
        name = _norm(instance) if instance else None
        plan = self._stamp_plan(block_pcb, name, at, rot, side, layer_map, allow_asymmetric,
                                source_instance, anchor)
        netmap = _netmap(plan.src, plan.pairs, name) if name else {}
        ports = _placed_ports(plan.info, plan.f, netmap)
        shown = {k: v for k, v in plan.cmap.items() if k != v}
        return {"instance": name, "block": plan.block, "at": list(at), "rot": rot, "side": plan.side,
                "anchor": plan.anchor, "origin": list(plan.origin), "outline": plan.outline,
                "ports": ports, "order": port_order(plan.box, plan.f, ports),
                **({"layer_map": shown} if shown else {}),
                **({"warnings": plan.warnings} if plan.warnings else {})}

    def stamp_collisions(self, instances: list[str] | None = None) -> list[dict]:
        """Collisions between stamped instances, pairwise per layer (``cells``): copper
        closer than its clearance (same net allowed), overlapping courtyards per side,
        copper or parts in a keep-out, pours of different nets overlapping at one priority.
        Rows: ``i``, ``j`` (instances), ``layer``,
        ``kind``, ``a``/``b`` (items), ``nets``, ``gap``/``need`` (mm), ``x``, ``y``.
        ``instances``: only collisions of these."""
        from . import cells
        return cells.collisions(self, instances)

    def _stamp_record(self, instance: str) -> dict:
        """What ``<board>.stamps.json`` (or this session) records for an instance."""
        f = stamps_file(self.path)
        saved = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        return {**saved, **self._stamps}.get(_norm(instance), {})

    def ports(self, instance: str | None = None) -> dict:
        """Port points of stamped instances on this board (``set_port`` on the block
        board): {instance: {port: {x, y, net}}}, or one instance's {port: ...}."""
        f = stamps_file(self.path)
        saved = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        known = {k: v["ports"] for k, v in {**saved, **self._stamps}.items()}
        if instance is None:
            return known
        if _norm(instance) not in known:
            raise env.KatError(f"{instance} is not stamped on {self.path.name}")
        return known[_norm(instance)]

    def _save_stamps(self) -> None:
        if not self._stamps:
            return
        f = stamps_file(self.path)
        saved = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        f.write_text(json.dumps({**saved, **self._stamps}, indent=2, sort_keys=True) + "\n",
                     encoding="utf-8")
        self._stamps = {}

    def _room_for(self, src, pairs: dict, instance: str, f: "_Place", make_room: bool) -> list[dict]:
        """Refuse (or with ``make_room`` move) the design's parts outside any block instance
        that overlap where the block's parts will go: courtyards on the same side,
        through-hole pads on both. Stamped instances are judged per layer afterwards
        (``cells.against``), so neighbouring blocks may interlock."""
        own = {tf.uuid for tf in pairs.values()}
        stamped = set()
        for g in self._groups():
            if not str(g[1]).startswith(f"{instance} ("):
                stamped |= {str(u) for u in (find(g, "members") or [None])[1:]}
        after = [(f.side(side), [f(*q) for q in poly])   # (side, polygon) of the block's parts, placed
                 for sf in src.footprints() if sf.uuid in pairs for side, poly in _taken(sf)]

        def top_level(fp) -> bool:
            return not fp.field("Block") and fp.path.count("/") <= 1 and fp.uuid not in stamped

        from .cells import polygons_overlap
        obstacles = [fp for fp in self.footprints() if fp.uuid not in own and top_level(fp)
                     and any(s1 == s2 and polygons_overlap(p1, p2) for s1, p1 in _taken(fp) for s2, p2 in after)]
        if not obstacles:
            return []
        fixed = [fp.ref for fp in obstacles if fp.uuid in stamped or fp.locked]
        if not make_room or fixed:
            why = f" ({', '.join(fixed)} locked or stamped: move the stamp)" if fixed and make_room else ""
            raise env.KatError(f"{instance} would overlap {', '.join(sorted(fp.ref for fp in obstacles))}"
                               f"{why}: move them first, pass make_room=True, or stamp elsewhere")
        moved = []
        blocked = [(side, box_of(p)) for side, p in after]
        for fp in obstacles:
            mine = [(side, box_of(p)) for side, p in _taken(fp)]
            others = [(side, box_of(p)) for o in self.footprints() if o.uuid not in own and o.node is not fp.node
                      and (top_level(o) or o.uuid in stamped) for side, p in _taken(o)]
            best = None
            for ux, uy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                d = 0.5
                while d < 200:
                    if not any(s1 == s2 and overlap((b[0] + ux * d, b[1] + uy * d, b[2] + ux * d, b[3] + uy * d), x, 0.25)
                               for s1, b in mine for s2, x in blocked + others):
                        if best is None or d < best[0]:
                            best = (d, ux * d, uy * d)
                        break
                    d += 0.5
            if best is None:
                raise env.KatError(f"no room to move {fp.ref} out of {instance}'s way")
            fp.place(fp.x + best[1], fp.y + best[2])
            moved.append({"ref": fp.ref, "by": [best[1], best[2]]})
        return moved

    def _next_board_ref(self, prefix: str, also: set[str] = frozenset()) -> str:
        used = {f.ref for f in self.footprints()} | set(also)
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    # --- groups ---------------------------------------------------------------------------

    def _groups(self) -> list[list]:
        return find_all(self.tree, "group")

    def _ungroup(self, uuids: list[str]) -> None:
        """Take items out of any group (KiCad allows one group per item)."""
        want = set(uuids)
        for g in self._groups():
            m = find(g, "members")
            if m is not None:
                m[1:] = [u for u in m[1:] if str(u) not in want]

    def _unstamp(self, instance: str) -> int:
        """Remove an earlier stamp of ``instance``: its group, sub-groups, and the copied
        copper, graphics and board-only parts in them (the design's parts stay)."""
        groups = {_uuid(g): g for g in self._groups()}
        roots = [g for g in groups.values() if str(g[1]).startswith(f"{instance} (")]
        if not roots:
            return 0
        drop_groups, drop_items = set(), set()
        todo = list(roots)
        while todo:
            g = todo.pop()
            drop_groups.add(_uuid(g))
            for u in (find(g, "members") or [None])[1:]:
                u = str(u)
                if u in groups and u not in drop_groups:
                    todo.append(groups[u])
                else:
                    drop_items.add(u)
        keep = []
        removed = 0
        for n in self.tree:
            h = head(n)
            u = _uuid(n) if isinstance(n, list) else None
            if h == "group" and u in drop_groups:
                removed += 1
                continue
            if u in drop_items and (h in COPPER or h in GRAPHICS or (
                    h == "footprint" and any(str(t) == "board_only" for t in (find(n, "attr") or [])[1:]))):
                removed += 1
                continue
            keep.append(n)
        self.tree[:] = keep
        self._ungroup(list(drop_groups | drop_items))   # a parent group of a nested stamp
        return removed

    def _copy_groups(self, src, instance: str, block: str, members: dict[str, str]) -> list[str]:
        """The instance group with the block board's own groups copied inside it."""
        sgroups = {_uuid(g): g for g in find_all(src.tree, "group")}
        new_uuid_of = {u: new_uuid() for u in sgroups}
        grouped = set()
        names = []
        for u, g in sgroups.items():
            mem = []
            for m in (find(g, "members") or [None])[1:]:
                m = str(m)
                if m in new_uuid_of:
                    mem.append(new_uuid_of[m])
                    grouped.add(m)
                elif m in members:
                    mem.append(members[m])
                    grouped.add(m)
            name = _group_name(instance, str(g[1]))
            names.append(name)
            self._insert([Sym("group"), name, [Sym("uuid"), new_uuid_of[u]], [Sym("members"), *mem]])
        top = [new_uuid_of[u] for u in sgroups if not any(
            u in {str(m) for m in (find(g2, "members") or [None])[1:]} for g2 in sgroups.values())]
        top += [t for s, t in members.items() if s not in grouped]
        name = f"{instance} ({block})"
        self._insert([Sym("group"), name, [Sym("uuid"), new_uuid()], [Sym("members"), *top]])
        return [name] + names


def _check_via(c: list, order: list[str]) -> None:
    """A blind, buried or micro via after the layer map: still a span the stack-up
    allows (a µvia joins adjacent layers, one of them outer; a blind via reaches an outer
    layer). Drill and annular ring are judged by the design's DRC."""
    kind = str(c[1]) if isinstance(c[1], Sym) else "through"
    ls = [str(t) for t in (find(c, "layers") or [None])[1:]]
    if kind == "through" or len(ls) != 2 or not all(l in order for l in ls):
        return
    i, j = sorted(order.index(l) for l in ls)
    outer = i == 0 or j == len(order) - 1
    at = find(c, "at")
    where = f"{kind} via at {float(at[1]):g},{float(at[2]):g} ({ls[0]}-{ls[1]})"
    if kind == "micro" and (j - i != 1 or not outer):
        raise env.KatError(f"{where}: a µvia must join an outer layer and the one next to it")
    if kind == "blind" and not outer:
        raise env.KatError(f"{where}: a blind via must reach an outer layer")


def flip_board(pcb: Path | str, layer_map: str | dict | None = None, allow_asymmetric: bool = False) -> dict:
    """Turn a whole board over in place, as a stamp on the bottom side would: every item
    mirrored about the outline's vertical axis (same outline box), footprints flipped,
    layers through the layer map (``auto``: the mirror map on the board's own stack-up).
    Used by ``kat block check --sides both`` on a scratch copy; not saved here. Returns
    the board, the copper map and the point transform."""
    from .board import Board
    b = Board(pcb)
    box = b.outline_box()
    if box is None:
        raise env.KatError(f"{Path(pcb).name} has no outline to flip about")
    cmap = lay.copper_map(layer_map, True, b, b, pcb, pcb, allow_asymmetric)
    order = b.copper_layers()
    f = _Place(box, box[:2], 0.0, True, lay.layer_fn(cmap, True))
    for n in b.tree:
        h = head(n)
        if h == "footprint":
            f.footprint(n, copy.deepcopy(n))
        elif h in COPPER or h in GRAPHICS:
            if h == "zone":
                n[:] = [x for x in n if head(x) != "filled_polygon"]
            f.item(n, order)
            if h == "via":
                _check_via(n, order)
    return {"board": b, "layer_map": cmap, "point": f}


def _hier_source_keys(src, sname: str) -> dict[str, str]:
    """For a block board whose schematic has sheets (a library block with nested blocks):
    footprint uuid -> key relative to the block instance, ``RC1/r`` (sheet path + the
    part's own tag), because a shared sheet file gives every instance the same fields.
    Empty if the schematic has no sheets below the block instance."""
    from .. import hierarchy
    sch = src.path.with_suffix(".kicad_sch")
    if not sname or not sch.exists():
        return {}
    tree = hierarchy.tree(sch)
    base = "/" + sname
    below = [i for i in tree if i["path"] == base or i["path"].startswith(base + "/")]
    if not any(i["path"].startswith(base + "/") for i in below):
        return {}
    out = {}
    for f in src.footprints():
        if f.board_only or not f.path:
            continue
        inst = max((i for i in below if f.path.startswith(i["uuids"] + "/")),
                   key=lambda i: len(i["uuids"]), default=None)
        if inst is None:
            continue                                  # the harness: environment
        tag, block = f.field("SKiDL Tag") or "", f.field("Block") or ""
        seg = block.split("/")[0]
        own = tag[len(seg) + 1:] if seg and tag.startswith(seg + "/") else tag
        rel = inst["path"][len(base):].strip("/")
        out[f.uuid] = f"{rel}/{own}" if rel else own
    return out


def _parts_box(src, pairs) -> tuple[float, float, float, float]:
    boxes = [f.box() for f in src.footprints() if f.uuid in pairs and f.box()]
    if not boxes:
        raise env.KatError("the block board has no outline and no parts")
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))
