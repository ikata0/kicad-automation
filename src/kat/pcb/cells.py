"""Block cells: what a stamped block takes on each layer, collisions between stamped block
copies, and the smallest pitch at which copies of a block do not collide.

A block's **pitch** (``NAME.block.json``) is its stamping grid, separate from what it
takes. What it takes is its **per-layer footprint**, computed from the block board:
copper per copper layer (with its clearance), courtyards per side (through-hole pads and
holes on both) and keep-outs per layer. Copies may interlock (a track on an inner layer
under the neighbour, courtyards over the cell edge) as long as these do not collide:

- **copper** vs copper on the same layer closer than the nets' clearance (the larger of
  the two classes'), unless both are the same net (a shared GND);
- **courtyard** vs courtyard on the same side (overlapping; touching is fine);
- **keepout** vs what it forbids of the other copy on its layer: tracks, vias, pads,
  and on an outer layer the courtyards of that side (footprints).

Through-hole pads and vias count on every copper layer they span. Pours are no obstacle:
they keep their clearance to other nets when filled. A copy on the bottom side is
checked after its layer map.
"""

from __future__ import annotations

import fnmatch
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

from .. import env
from ..sexpr import find, head
from .coupling import _arc_points, _node_layers, _pt, _pts, _Shape
from .geometry import Index, rotate

TOL = 1e-3                  # mm: what counts as touching
COPPER_KINDS = ("track", "via", "pad")
FORBIDS = {"track": "tracks", "via": "vias", "pad": "pads", "courtyard": "footprints"}


@dataclass
class Item:
    """One thing a block takes: ``kind`` track, via, pad, courtyard or keepout; ``layer``
    a copper layer (courtyards: ``F``/``B``); ``what`` names it in a report."""
    kind: str
    layer: str
    net: str
    shape: _Shape
    what: str
    forbids: frozenset = frozenset()


def _rect(b) -> list[tuple[float, float]]:
    return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]


def items(nodes: list, copper: list[str]) -> list[Item]:
    """The items of board nodes (tracks, arcs, vias, keep-out zones, footprints) on the
    ``copper`` layers. Pours are left out (they yield to other nets)."""
    from .board import Footprint
    from .stamping import _taken
    out = []
    for n in nodes:
        h = head(n)
        net = find(n, "net") if h != "footprint" else None
        net = str(net[1]) if net is not None else ""
        if h in ("segment", "arc"):
            lay = str(find(n, "layer")[1])
            if lay not in copper:
                continue
            r = float(find(n, "width")[1]) / 2
            s, e = _pt(find(n, "start")), _pt(find(n, "end"))
            pts = _arc_points(s, _pt(find(n, "mid")), e) if h == "arc" else [s, e]
            out += [Item("track", lay, net, _Shape(a, b, r), "track") for a, b in zip(pts, pts[1:])]
        elif h == "via":
            at, r = _pt(find(n, "at")), float(find(n, "size")[1]) / 2
            out += [Item("via", l, net, _Shape(at, at, r), "via") for l in _node_layers(n, copper)]
        elif h == "zone":
            k = find(n, "keepout")
            if k is None:
                continue
            forbids = frozenset(str(c[0]) for c in k[1:]
                                if isinstance(c, list) and len(c) > 1 and str(c[1]) == "not_allowed")
            name = find(n, "name")
            what = f"keepout {name[1]}" if name is not None else "keepout"
            poly = _pts(find(n, "polygon"))
            out += [Item("keepout", l, "", _Shape(poly=poly), what, forbids) for l in _node_layers(n, copper)]
        elif h == "footprint":
            fp = Footprint(n)
            for p in fp.pads():
                out += [Item("pad", l, p.net, _Shape(poly=p.outline), f"pad {fp.ref}.{p.number}")
                        for l in copper if p.on(l)]
            out += [Item("courtyard", side, "", _Shape(poly=_rect(b)), f"courtyard {fp.ref}")
                    for side, b in _taken(fp)]
    return out


def moved(its: list[Item], f) -> list[Item]:
    """Items through a point transform ``f(x, y) -> (x, y)``."""
    def shape(s: _Shape) -> _Shape:
        if s.poly is not None:
            return _Shape(poly=[f(*p) for p in s.poly])
        return _Shape(f(*s.a), f(*s.b), s.r)
    return [replace(i, shape=shape(i.shape)) for i in its]


def _shrunk(poly, d: float):
    cx, cy = sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)
    out = []
    for x, y in poly:
        L = math.hypot(x - cx, y - cy)
        k = max(L - d, 0.0) / L if L else 0.0
        out.append((cx + (x - cx) * k, cy + (y - cy) * k))
    return out


def _clash(x: Item, y: Item, clearance, same_ok) -> tuple[str, float, float] | None:
    """(kind, gap, need) if ``x`` and ``y`` (of two different copies) collide."""
    if x.kind in COPPER_KINDS and y.kind in COPPER_KINDS:
        if x.layer != y.layer or (x.net and x.net == y.net and same_ok(x.net)):
            return None
        need = clearance(x.net, y.net)
        gap = x.shape.distance(y.shape)
        return ("copper", gap, need) if gap < need - TOL else None
    if x.kind == y.kind == "courtyard":
        if x.layer != y.layer:
            return None
        a, b = _Shape(poly=_shrunk(x.shape.poly, TOL)), _Shape(poly=_shrunk(y.shape.poly, TOL))
        if a.distance(b) > 0:
            return None
        bx, by = x.shape.box, y.shape.box
        depth = min(min(bx[2], by[2]) - max(bx[0], by[0]), min(bx[3], by[3]) - max(bx[1], by[1]))
        return "courtyard", -round(max(depth, 0.0), 3), 0.0
    if y.kind == "keepout":
        x, y = y, x
    if x.kind != "keepout" or y.kind == "keepout" or FORBIDS.get(y.kind) not in x.forbids:
        return None
    if y.kind == "courtyard":
        if {"F.Cu": "F", "B.Cu": "B"}.get(x.layer) != y.layer:
            return None
    elif x.layer != y.layer:
        return None
    return ("keepout", 0.0, 0.0) if x.shape.distance(y.shape) <= TOL else None


def clashes(a: list[Item], b: list[Item], clearance, same_ok=lambda net: True) -> list[dict]:
    """Collisions between two copies' items: one row per (layer, kind, items, nets) with
    the smallest gap (mm; negative: courtyards overlapping by that much) and the gap
    needed. ``clearance(net_a, net_b)``: mm; ``same_ok(net)``: may both copies have
    copper of this net close together (a shared net)?"""
    nets = {i.net for i in a + b}
    reach = max((clearance(n, n) for n in nets), default=0.2) + TOL
    idx = Index(2.0)
    for y in b:
        idx.add(y.shape.box, y)
    rows: dict[tuple, dict] = {}
    for x in a:
        for y in idx.query(x.shape.box, reach):
            hit = _clash(x, y, clearance, same_ok)
            if hit is None:
                continue
            kind, gap, need = hit
            layer = x.layer if x.kind != "courtyard" or y.kind == "courtyard" else y.layer
            key = (layer, kind, x.what, y.what, x.net, y.net)
            if key in rows and rows[key]["gap"] <= gap:
                continue
            ca, cb = x.shape.box, y.shape.box
            rows[key] = {"layer": layer, "kind": kind, "a": x.what, "b": y.what, "nets": [x.net, y.net],
                         "gap": round(gap, 3), "need": round(need, 3),
                         "x": round((max(ca[0], cb[0]) + min(ca[2], cb[2])) / 2, 3),
                         "y": round((max(ca[1], cb[1]) + min(ca[3], cb[3])) / 2, 3)}
    return sorted(rows.values(), key=lambda r: (r["gap"] - r["need"], r["layer"]))


def text(row: dict) -> str:
    """One collision as a line of text."""
    nets = " vs ".join(n or "no net" for n in row["nets"])
    if row["kind"] == "copper":
        why = f"{row['gap']:g} mm apart, need {row['need']:g} ({nets})"
    elif row["kind"] == "courtyard":
        why = f"overlap {-row['gap']:g} mm"
    else:
        why = "inside the keep-out"
    who = f"{row['i']} {row['a']} / {row['j']} {row['b']}" if "i" in row else f"{row['a']} / {row['b']}"
    return f"{who} on {row['layer']}: {why} at {row['x']:g},{row['y']:g}"


# --- a block's own items ------------------------------------------------------------------

def _shared(src, block_pcb: Path, patterns: list[str] | None):
    """net -> True if copies of the block share it (a power net such as GND): the globs
    ``patterns``, else KiCad's power symbol names and nets outside any sheet."""
    if patterns is not None:
        return lambda net: any(fnmatch.fnmatchcase(net, p) for p in patterns)
    try:
        from ..syncer import power_names
        power = power_names()
    except env.KatError:
        power = set()
    return lambda net: net in power or not (net.startswith("/") or net.startswith("Net-(")
                                            or net.startswith("unconnected-"))


def _stamped_uuids(src, block_pcb: Path, source_instance: str | None) -> set[str]:
    """uuids of the block board's items that ``stamp`` copies (not the environment: harness
    parts, copper serving only them, environment zones)."""
    from .. import hierarchy
    from .stamping import _harness_copper, _uuid, block_info, COPPER
    info = block_info(block_pcb)
    inst = (source_instance or info.get("instance") or "").strip("/")
    parts = [f for f in src.footprints() if not f.board_only]
    block_parts = parts
    if not inst:
        tops = {(f.field("Block") or "").split("/")[0] for f in parts if f.field("Block")}
        inst = tops.pop() if len(tops) == 1 else ""
    if inst:
        sch, prefix = src.path.with_suffix(".kicad_sch"), None
        if sch.exists():
            try:
                prefix = hierarchy.instance(sch, "/" + inst)["uuids"]
            except env.KatError:
                prefix = None
        if prefix:
            block_parts = [f for f in parts if f.path.startswith(prefix + "/")]
        else:
            block_parts = [f for f in parts if (f.field("Block") or "").split("/")[0] == inst]
        block_parts = block_parts or parts
    mine = {f.uuid for f in block_parts}
    env_parts = [f for f in parts if f.uuid not in mine]
    env_pads = [p for f in env_parts for p in f.pads()]
    block_pads = [p for f in block_parts for p in f.pads()]
    block_nets = {p.net for p in block_pads if p.net}
    env_nets = {p.net for p in env_pads if p.net}
    ports = {(round(q["x"], 4), round(q["y"], 4)) for q in info.get("port_points", [])}
    harness = _harness_copper(src, block_pads, env_pads, env_nets & block_nets, ports)
    env_zones = set(info.get("environment", []))
    keep = set(mine) | {f.uuid for f in src.footprints() if f.board_only}
    for n in src.tree:
        if head(n) not in COPPER or id(n) in harness:
            continue
        name, net = find(n, "name"), find(n, "net")
        if head(n) == "zone" and name is not None and str(name[1]) in env_zones:
            continue
        net = str(net[1]) if net is not None else ""
        if net and net in env_nets and net not in block_nets:
            continue
        keep.add(_uuid(n))
    return keep


def block_items(block_pcb: Path | str, side: str = "top", layer_map: str | dict | None = None,
                allow_asymmetric: bool = False, source_instance: str | None = None) -> dict:
    """What a block takes as stamped, in block coordinates (the outline's top-left corner
    at 0,0; on the bottom side the flipped outline's): ``items``, ``size`` (outline w, h),
    ``clearance`` and ``shared`` (functions, from the block board)."""
    from .board import Board
    from .stamping import COPPER, _uuid, flip_board
    block_pcb = Path(block_pcb)
    src = Board(block_pcb)
    box = src.outline_box()
    if box is None:
        raise env.KatError(f"{block_pcb.name} has no outline: the cell is measured from it")
    keep = _stamped_uuids(src, block_pcb, source_instance)
    b = src if side == "top" else flip_board(block_pcb, layer_map, allow_asymmetric)["board"]
    nodes = [n for n in b.tree if (head(n) in COPPER or head(n) == "footprint") and _uuid(n) in keep]
    its = moved(items(nodes, b.copper_layers()), lambda x, y: (x - box[0], y - box[1]))
    return {"items": its, "size": (box[2] - box[0], box[3] - box[1]), "clearance": src._clearance_fn(),
            "board": src}


def footprint(block_pcb: Path | str, source_instance: str | None = None) -> dict:
    """The block's per-layer footprint (mm, block coordinates, origin the outline's top-left
    corner): per copper layer the box of its copper grown by the clearance and of its
    keep-outs; per side the box of its courtyards. Boxes reaching past the outline
    (``outside``) are what interlocks with a neighbour."""
    got = block_items(block_pcb, source_instance=source_instance)
    w, h = got["size"]
    cl = got["clearance"]
    out: dict[str, dict] = {}

    def add(key, kind, b):
        e = out.setdefault(key, {})
        old = e.get(kind)
        e[kind] = list(b) if old is None else [min(old[0], b[0]), min(old[1], b[1]),
                                               max(old[2], b[2]), max(old[3], b[3])]

    for i in got["items"]:
        b = i.shape.box
        if i.kind in COPPER_KINDS:
            c = cl(i.net, i.net) / 2
            add(i.layer, "copper", (b[0] - c, b[1] - c, b[2] + c, b[3] + c))
        elif i.kind == "keepout":
            add(i.layer, "keepout", b)
        else:
            add({"F": "F.CrtYd", "B": "B.CrtYd"}[i.layer], "courtyard", b)
    for e in out.values():
        for kind, b in list(e.items()):
            e[kind] = [round(v, 3) for v in b]
            past = {"left": -b[0], "top": -b[1], "right": b[2] - w, "bottom": b[3] - h}
            past = {k: round(v, 3) for k, v in past.items() if v > TOL}
            if past:
                e.setdefault("outside", {})[kind] = past
    return {"size": [round(w, 4), round(h, 4)], "layers": out}


# --- the block's cell ---------------------------------------------------------------------

def set_pitch(block_pcb: Path | str, pitch: tuple[float, float] | None) -> dict:
    """Store the block's stamping pitch (dx, dy mm; None removes it) in its block file:
    ``kat pcb stamp`` uses it as ``--step`` when none is given."""
    from .stamping import block_file, block_info
    info = block_info(block_pcb)
    if pitch is None:
        info.pop("pitch", None)
    else:
        info["pitch"] = [round(float(pitch[0]), 4), round(float(pitch[1]), 4)]
    block_file(block_pcb).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def min_pitch(block_pcb: Path | str, axis: str = "x", center: tuple[float, float] | None = None,
              side: str = "top", layer_map: str | dict | None = None, allow_asymmetric: bool = False,
              shared: list[str] | None = None, resolution: float = 0.01,
              source_instance: str | None = None) -> dict:
    """The smallest pitch at which copies of a block, stamped in a row, do not collide
    (see the module text). ``axis`` "x"/"y": copies shifted along it (mm); "arc": copies
    turned about ``center`` (mm, block coordinates: the outline's top-left corner is 0,0)
    and the pitch is an angle (degrees). ``side``: "top", "bottom", or "alternate" (every
    second copy on the bottom, through ``layer_map``). ``shared``: globs of nets the
    copies share (default: power nets and nets outside any sheet). Neighbours further
    than one step are checked too. Returns the pitch, the outline's, what limits it (the
    collisions just below) and, for an arc, the arc length at the outline's centre."""
    if axis not in ("x", "y", "arc"):
        raise env.KatError(f"axis {axis!r}: x, y or arc")
    if axis == "arc" and center is None:
        raise env.KatError("an arc needs its centre (center=(x, y), block coordinates)")
    if side not in ("top", "bottom", "alternate"):
        raise env.KatError(f"side {side!r}: top, bottom or alternate")
    top = block_items(block_pcb, "top", source_instance=source_instance)
    w, h = top["size"]
    sides = {"top": top["items"]}
    if side != "top":
        sides["bottom"] = block_items(block_pcb, "bottom", layer_map, allow_asymmetric, source_instance)["items"]
    first = sides["bottom" if side == "bottom" else "top"]
    same_ok = _shared(top["board"], Path(block_pcb), shared)
    clearance = top["clearance"]
    allx = [v for i in first for v in (i.shape.box[0], i.shape.box[2])]
    ally = [v for i in first for v in (i.shape.box[1], i.shape.box[3])]
    reach = max(clearance(i.net, i.net) for i in first) if first else 0.2
    extent = {"x": max(allx) - min(allx), "y": max(ally) - min(ally)}

    def copy_k(k: int, p: float) -> list[Item]:
        its = sides["bottom" if side == "bottom" or (side == "alternate" and k % 2) else "top"]
        if axis == "arc":
            cx, cy = center
            return moved(its, lambda x, y: (lambda d: (cx + d[0], cy + d[1]))(rotate(x - cx, y - cy, k * p)))
        return moved(its, lambda x, y: (x + k * p, y) if axis == "x" else (x, y + k * p))

    def hits(p: float) -> list[dict]:
        if axis == "arc":
            ks = [k for k in range(1, max(2, int(360 // max(p, 1e-6)))) if k * p < 360 - 1e-9][:8]
        else:
            ks = [k for k in range(1, 64) if k == 1 or (k - 1) * p < extent[axis] + 2 * reach]
        out = []
        for k in ks:
            for r in clashes(first, copy_k(k, p), clearance, same_ok):
                out.append({**r, "i": "copy 0", "j": f"copy {k}"})
            if out:
                break
        return out

    hi, step = (180.0, 0.5) if axis == "arc" else (extent[axis] + 2 * reach + 1.0, 0.1)
    # walk up from 0 (copies interlock, so the first free pitch is not found by halving),
    # then refine between the last colliding and the first free step
    p, last_bad, found = step, 0.0, None
    while p <= hi + 1e-9:
        if hits(p):
            last_bad = p
        else:
            found = p
            break
        p = round(p + step, 6)
    if found is None:
        raise env.KatError(f"no collision-free pitch up to {hi:g}{' degrees' if axis == 'arc' else ' mm'}")
    lo, hi = last_bad, found
    while hi - lo > resolution / 4:
        m = (lo + hi) / 2
        lo, hi = (m, hi) if hits(m) else (lo, m)
    k = math.floor(lo / resolution + 1e-9)          # the first free step on the grid
    while k * resolution <= found + step and hits(round(k * resolution, 6)):
        k += 1
    pitch = round(max(k * resolution, resolution), 6)
    limit = hits(round(pitch - resolution, 6)) if pitch > resolution else []
    out = {"block": Path(block_pcb).stem, "axis": axis, "side": side, "pitch": round(pitch, 4)}
    if axis == "arc":
        radius = math.hypot(w / 2 - center[0], h / 2 - center[1])
        out.update(center=list(center), radius=round(radius, 3),
                   arc_length=round(radius * math.radians(pitch), 4))
    else:
        size = w if axis == "x" else h
        out.update(outline=round(size, 4), interlock=round(size - pitch, 4))
    out["limited_by"] = [text(r) for r in limit[:8]]
    return out


# --- stamped copies on a design -----------------------------------------------------------

def stamped_items(board, instance: str, skip: frozenset = frozenset()) -> list[Item]:
    """The items of a stamped instance on a design: what its group (and sub-groups) hold,
    without the nodes whose uuid is in ``skip``."""
    from .stamping import COPPER, _uuid
    groups = {_uuid(g): g for g in board._groups()}
    todo = [g for g in groups.values() if str(g[1]).startswith(f"{instance} (")]
    seen, members = set(), set()
    while todo:
        g = todo.pop()
        seen.add(_uuid(g))
        for u in (find(g, "members") or [None])[1:]:
            u = str(u)
            if u in groups and u not in seen:
                todo.append(groups[u])
            else:
                members.add(u)
    nodes = [n for n in board.tree if (head(n) in COPPER or head(n) == "footprint") and _uuid(n) in members - skip]
    return items(nodes, board.copper_layers())


def stamped_instances(board) -> list[str]:
    """Instances stamped on a design (``<board>.stamps.json`` and this session) whose
    group is still there."""
    import json
    from .stamping import stamps_file
    f = stamps_file(board.path)
    saved = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    names = {str(g[1]) for g in board._groups()}
    return sorted(i for i in {**saved, **board._stamps}
                  if any(n.startswith(f"{i} (") for n in names))


def _nested(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def against(board, instance: str, its: list[Item], others: list[str] | None = None,
            skip: frozenset = frozenset()) -> list[dict]:
    """Collisions of an instance's items with the other stamped instances' (``others``:
    default all of them). Same-net copper is a shared net on a design. An instance inside
    a stamped one (``/CH_A/RC1`` in ``/CH_A``) meets the rest of it; ``skip`` are the uuids
    of its own parts there."""
    cl = board._clearance_fn()
    out = []
    for o in others if others is not None else stamped_instances(board):
        if o == instance or o.startswith(instance + "/"):
            continue
        out += [{**r, "i": instance, "j": o} for r in clashes(its, stamped_items(board, o, skip), cl)]
    return out


def collisions(board, instances: list[str] | None = None) -> list[dict]:
    """Collisions between stamped instances, pairwise per layer: rows ``i``, ``j``
    (instances), ``layer``, ``kind`` (copper, courtyard, keepout), ``a``/``b`` (items),
    ``nets``, ``gap``/``need`` (mm), ``x``, ``y``."""
    names = stamped_instances(board)
    want = names if instances is None else [n for n in names if n in {"/" + i.strip("/") for i in instances}]
    its = {n: stamped_items(board, n) for n in names}
    cl = board._clearance_fn()
    out = []
    for a in want:
        for b in names:
            if _nested(a, b) or (b in want and names.index(b) <= names.index(a)):
                continue
            out += [{**r, "i": a, "j": b} for r in clashes(its[a], its[b], cl)]
    return out
