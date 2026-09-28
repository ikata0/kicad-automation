"""Flat schematics with many block instances: stamp a block's checked drawing onto each
instance, and frame instances so the sheet stays readable.

``stamp`` is the schematic counterpart of ``Board.stamp``. The block's own schematic (in the
library: its harness and the block, see ``kat.blocks``) is the source. Parts pair by
``Block`` + tag, as on the board. What is copied is the drawing inside the *block region*
(the block parts' bodies and pins plus a margin): the parts' positions, rotations, mirrors
and field positions, wires, junctions, labels (renamed to the instance's nets), power
symbols, no-connects and texts. The block's harness must connect to it by labels, so no
wire crosses the region's edge. The instance's parts are detached first and, after the
copy, anything the design still misses is attached as ``kat sync`` would; the sheet's
connectivity must then equal the design (``kat.stamp`` checks and undoes otherwise).
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path

from . import env, netlist
from .schematic import Schematic, new_uuid, snap
from .sexpr import Sym, find, find_all, head, num

MARGIN = 2.54
_NS = uuid.UUID("5b0c2d1e-7a3f-4f59-9e3a-6b8f3c2d1a00")


def _det_uuid(text: str) -> str:
    return str(uuid.uuid5(_NS, text))


def _block_of(sym) -> str:
    return sym.field("Block") or ""


def _local(sym, root: str) -> str | None:
    """Identity of a symbol inside block instance ``root`` (tag relative to it), None if
    the symbol is not in that instance."""
    tag, block = sym.field("SKiDL Tag") or "", _block_of(sym)
    if root and not (block == root or block.startswith(root + "/")):
        return None
    if root and tag.startswith(root + "/"):
        return tag[len(root) + 1:]
    return tag or None


def _parts(s: Schematic) -> list:
    return [x for x in s.symbols() if not x.ref.startswith("#")]


def _source_instance(src: Schematic, name: str | None) -> str:
    if name is not None:
        return name.strip("/")
    tops = {_block_of(x).split("/")[0] for x in _parts(src) if _block_of(x)}
    if len(tops) != 1:
        raise env.KatError(f"the block schematic has {len(tops)} block instances {sorted(tops)}: "
                           f"name one with source_instance=")
    return tops.pop()


def _region(s: Schematic, syms) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for sym in syms:
        if (b := s.body_box(sym)):
            xs += [b[0], b[2]]
            ys += [b[1], b[3]]
        for p in s.pins(sym):
            xs.append(p["x"])
            ys.append(p["y"])
    return (min(xs) - MARGIN, min(ys) - MARGIN, max(xs) + MARGIN, max(ys) + MARGIN)


def _drawing(src: Schematic, block_syms, env_syms) -> tuple[list, list, tuple]:
    """The block's drawing: wires reachable from its pins (through wire ends and T-points),
    and the labels, junctions, no-connects and power symbols on them. Returns (items,
    power symbols, region box). KatError if a wire reaches a harness pin."""
    from .schematic import _on_interior, same
    segs = list(zip(src.wires(), src.segments()))
    env_pts = {(round(p["x"], 3), round(p["y"], 3)): e.ref for e in env_syms for p in src.pins(e)}
    todo = [(p["x"], p["y"]) for x in block_syms for p in src.pins(x)]
    seen_pts, wires = set(), []
    while todo:
        pt = todo.pop()
        k = (round(pt[0], 3), round(pt[1], 3))
        if k in seen_pts:
            continue
        seen_pts.add(k)
        if k in env_pts:
            raise env.KatError(f"the harness part {env_pts[k]} is wired to the block in "
                               f"{src.path.name}: connect the harness to the block by labels")
        for w, (a, b) in segs:
            if any(w is x for x in wires):
                continue
            if same(a, pt) or same(b, pt) or _on_interior(pt, (a, b)):
                wires.append(w)
                todo += [a, b]
        for w, (a, b) in segs:        # a wire ending on the interior of a collected one
            if not any(w is x for x in wires) and any(
                    _on_interior(a, s) or _on_interior(b, s) for x, s in segs if any(x is y for y in wires)):
                wires.append(w)
                todo += [a, b]
    pts = [(float(x), float(y)) for x, y in seen_pts]
    items = list(wires)
    for n in src.tree:
        if head(n) in ("junction", "no_connect", "label", "global_label", "hierarchical_label"):
            p = _points(n)[0]
            if any(same(p, q) for q in pts):
                items.append(n)
    powers = [x for x in src.symbols() if x.ref.startswith("#")
              and any(same((p["x"], p["y"]), q) for p in src.pins(x) for q in pts)]
    box = list(_region(src, block_syms))
    for x in powers:
        if (b := src.body_box(x)):
            box = [min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3])]
    for x, y in pts:
        box = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
    return items, powers, tuple(box)


def _points(node: list) -> list[tuple[float, float]]:
    pts = []
    at = find(node, "at")
    if at is not None:
        pts.append((float(at[1]), float(at[2])))
    for xy in find_all(find(node, "pts") or [], "xy"):
        pts.append((float(xy[1]), float(xy[2])))
    return pts


def _inside(p, box) -> bool:
    return box[0] - 1e-6 <= p[0] <= box[2] + 1e-6 and box[1] - 1e-6 <= p[1] <= box[3] + 1e-6


def _shift(node: list, dx: float, dy: float) -> None:
    for c in node:
        if not isinstance(c, list):
            continue
        if head(c) in ("at", "xy", "start", "end", "center", "mid") and len(c) >= 3:
            c[1], c[2] = num(float(c[1]) + dx), num(float(c[2]) + dy)
        elif head(c) not in ("uuid",):
            _shift(c, dx, dy)


def _fresh(node: list) -> None:
    for c in node:
        if isinstance(c, list):
            if head(c) == "uuid" and len(c) > 1:
                c[1] = new_uuid()
            else:
                _fresh(c)


def _pair_only(s: Schematic, block: Path | str, instance: str, source_instance: str | None = None):
    """(source schematic, its path, source instance, block symbols by key, harness symbols,
    pairs {(key, unit): (source symbol, target symbol)}); KatError if they do not pair."""
    from . import blocks as lib
    src_path, lib_instance = lib.sheet_of(block, near=s.path)
    src = Schematic(src_path)
    sname = _source_instance(src, source_instance or lib_instance)
    name = instance.strip("/")

    # pairs: (local key, unit) on both sides
    keyed = {}
    env_syms = []
    for x in _parts(src):
        k = _local(x, sname)
        if k is None:
            env_syms.append(x)
        else:
            keyed[(k, x.unit)] = x
    if not keyed:
        raise env.KatError(f"{src_path.name} has no parts in block {sname}")
    targets = {(k, x.unit): x for x in _parts(s) if (k := _local(x, name)) is not None}
    if not targets:
        raise env.KatError(f"no parts with Block = {name} on {s.path.name}")
    missing = sorted(f"{name}/{k}" for k in {k for k, _ in keyed} - {k for k, _ in targets})
    extra = sorted(x.ref for key, x in targets.items() if key not in keyed)
    if missing or extra:
        raise env.KatError(f"{instance} and block {src_path.stem} differ: "
                           f"missing {missing or '-'}, not in the block {extra or '-'}")
    pairs = {key: (sx, targets[key]) for key, sx in keyed.items()}
    for sx, tx in pairs.values():
        if sx.lib_id != tx.lib_id:
            raise env.KatError(f"{sx.ref} -> {tx.ref}: symbol {sx.lib_id} vs {tx.lib_id}")
    return src, src_path, sname, keyed, env_syms, pairs


def stamp(s: Schematic, block: Path | str, instance: str, at: tuple[float, float],
          design: Path | str, source_instance: str | None = None) -> dict:
    """Copy a block's drawing onto one instance on this (flat) sheet; see the module text.
    ``at``: where the top-left corner of the block region goes (mm). ``design``: the
    design netlist (names the labels, and completes the connections afterwards)."""
    from .syncer import Syncer, _clean, drawn_nets
    src, src_path, sname, keyed, env_syms, pairs = _pair_only(s, block, instance, source_instance)
    name = instance.strip("/")

    items, powers, region = _drawing(src, list(keyed.values()), env_syms)
    dx, dy = snap(at[0] - region[0]), snap(at[1] - region[1])

    # nets: the block's drawn names -> the design's names for this instance
    comps, dnets = netlist.read_design(design)
    design_of = {pin: _clean(n) for n, pins in dnets.items() for pin in pins}
    src_of = {pin: n for n, pins in drawn_nets(src).items() for pin in pins}
    netmap: dict[str, str] = {}
    for sx, tx in pairs.values():
        for p in src.pins(sx):
            a, b = src_of.get((sx.ref, p["number"])), design_of.get((tx.ref, p["number"]))
            if a and b and netmap.setdefault(a, b) != b:
                raise env.KatError(f"net {a} of the block maps to {netmap[a]} and {b}: "
                                   f"{instance} is wired differently from the block")

    # the instance: detach, move, copy
    for _, tx in pairs.values():
        for p in s.pins(tx):
            s.detach(tx.ref, p["number"])
    moves = [dict(ref=tx.ref, unit=tx.unit, x=sx.place.x + dx, y=sx.place.y + dy,
                  rot=sx.place.rot, mirror=sx.place.mirror or "") for sx, tx in pairs.values()]
    s.move_symbols(moves, drag=False)
    for sx, tx in pairs.values():
        tnode = next(x.node for x in s.symbols() if x.ref == tx.ref and x.unit == tx.unit)
        for sp in find_all(sx.node, "property"):
            tp = next((p for p in find_all(tnode, "property") if str(p[1]) == str(sp[1])), None)
            if tp is None:
                continue
            sat, tat = find(sp, "at"), find(tp, "at")
            tat[1:] = [num(float(sat[1]) + dx), num(float(sat[2]) + dy), *sat[3:]]
            for key in ("effects", "hide"):
                sv, tv = find(sp, key), find(tp, key)
                if sv is not None and tv is not None:
                    tv[:] = copy.deepcopy(sv)
    copied = {"wires": 0, "labels": 0, "junctions": 0, "power": 0, "other": 0}
    for n in items:
        c = copy.deepcopy(n)
        _fresh(c)
        _shift(c, dx, dy)
        h = head(c)
        if h in ("label", "global_label", "hierarchical_label"):
            text = _clean(str(c[1]))
            if text not in netmap:
                continue                                   # a label of the harness side only
            c[1] = netmap[text]
            if h == "hierarchical_label":
                c[0] = Sym("label")
                c[:] = [x for x in c if head(x) != "shape"]
            copied["labels"] += 1
        else:
            copied[{"wire": "wires", "junction": "junctions"}.get(h, "other")] += 1
        s._insert(c)
    for pw in powers:
        value = pw.field("Value") or ""
        if pw.lib_id == "power:PWR_FLAG":
            continue            # one flag per net is the sheet's business (power_flags below)
        if netmap.get(value, value) != value:
            raise env.KatError(f"power symbol {value} of the block is on net {netmap[value]} in {instance}")
        new = s.add_symbol(pw.lib_id, pw.place.x + dx, pw.place.y + dy, pw.place.rot, pw.place.mirror or "")
        for sp in find_all(pw.node, "property"):
            tp = next((p for p in find_all(new.node, "property") if str(p[1]) == str(sp[1])), None)
            if tp is not None and str(sp[1]) != "Reference":
                keep = tp[1]
                tp[:] = copy.deepcopy(sp)
                tp[1] = keep
                _shift(tp, dx, dy)
        copied["power"] += 1
    placed = (region[0] + dx, region[1] + dy, region[2] + dx, region[3] + dy)
    mine = {tx.ref for _, tx in pairs.values()}
    crowded = sorted({x.ref for x in _parts(s) if x.ref not in mine and (b := s.body_box(x))
                      and b[0] < placed[2] and placed[0] < b[2] and b[1] < placed[3] and placed[1] < b[3]})
    s.prune_dangling()                 # e.g. the stub that led to the block's PWR_FLAG
    s.fix_junctions()
    sync = Syncer(s, Path(design))
    sync.reconcile()
    sync.power_flags()
    return {"instance": "/" + name, "block": src_path.stem, "at": [at[0], at[1]],
            "pairs": {f"{name}/{k}" + (f" unit {u}" if u > 1 else ""): tx.ref for (k, u), (_, tx) in pairs.items()},
            "nets": {k: v for k, v in netmap.items() if k != v}, "copied": copied,
            "attached": sync.report["attached"], "unresolved": sync.report["unresolved"],
            **({"warnings": [f"the stamped block covers {', '.join(crowded)}: move them or "
                             f"stamp elsewhere"]} if crowded else {})}


def frame(s: Schematic, block: str, title: str | None = None, margin: float = 3.81) -> dict:
    """Dashed rectangle and a heading around the parts of block instance ``block`` (and its
    sub-blocks). Drawing it again replaces it."""
    name = block.strip("/")
    syms = [x for x in _parts(s) if _block_of(x) == name or _block_of(x).startswith(name + "/")]
    if not syms:
        raise env.KatError(f"no parts with Block = {name} on {s.path.name}")
    from .checks import text_items
    region = _region(s, syms)                       # bodies and pins, plus MARGIN
    refs = {x.ref for x in syms}
    boxes = [region]
    for sym in s.symbols():                         # power symbols on the block's pins
        if sym.ref.startswith("#") and any(_inside((p["x"], p["y"]), region) for p in s.pins(sym)):
            if (b := s.body_box(sym)):
                boxes.append(b)
            refs.add(sym.ref)
    for t in text_items(s):                         # fields, and labels on the pins' stubs
        b = t["box"]
        anchor = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
        if t["owner"] in refs or (t["owner"] is None and any(
                _inside(c, region) for c in ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3]), anchor))):
            boxes.append(b)
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    x0, y0 = snap(x0 - margin), snap(y0 - margin)
    x1, y1 = snap(x1 + margin), snap(y1 + margin)
    kind = next((t for x in syms if _block_of(x) == name and (t := x.field("Block Type"))), None)
    title = title or (f"{name} ({kind})" if kind else name)
    rid, tid = _det_uuid(f"frame:{name}"), _det_uuid(f"frame-title:{name}")
    s.remove_by_uuid(rid)
    s.remove_by_uuid(tid)
    from .schematic import _sexp
    s._insert(_sexp(f'(rectangle (start {num(x0)} {num(y0)}) (end {num(x1)} {num(y1)}) '
                    f'(stroke (width 0) (type dash)) (fill (type none)) (uuid "{rid}"))'))
    node = s.add_text(title, x0, y0 - 1.27, 1.27)             # above the frame's top edge
    find(node, "uuid")[1] = tid
    return {"block": name, "title": title, "box": [x0, y0, x1, y1]}
