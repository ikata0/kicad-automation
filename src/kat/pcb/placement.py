"""Placement on the board: moves checked against courtyards, alignment, parts next to a pin,
functional blocks (the ``Block`` field set in SKiDL) and replication of a block's
arrangement onto the other blocks. Mixed into ``Board``."""

from __future__ import annotations

import math

from .. import env
from ..sexpr import find
from .geometry import Box, overlap, union
from .layers import fp_side

BLOCK = "Block"
TAG = "SKiDL Tag"
GAP = 0.25  # courtyard to courtyard, mm


class PlacementMixin:
    # --- queries ---------------------------------------------------------------------------

    def collisions(self, refs: list[str] | None = None, gap: float = 0.0) -> list[dict]:
        """Courtyard overlaps between footprints on the same side (optionally only those
        involving ``refs``; ``gap`` mm more), judged on the courtyard polygons
        (``Footprint.courtyard_outline``: a stadium or an L as drawn), as KiCad's DRC does."""
        from .cells import _Shape, polygons_overlap
        fps = [(f, f.box()) for f in self.footprints() if f.box()]
        want = set(refs) if refs else None
        outline: dict = {}

        def poly(f):
            if f.ref not in outline:
                outline[f.ref] = f.courtyard_outline()
            return outline[f.ref]
        out = []
        for i, (a, ba) in enumerate(fps):
            for b, bb in fps[i + 1:]:
                if a.side != b.side or (want and a.ref not in want and b.ref not in want):
                    continue
                if not overlap(ba, bb, gap - 1e-6):
                    continue
                pa, pb = poly(a), poly(b)
                if pa and pb and not (polygons_overlap(pa, pb) if gap <= 0
                                      else _Shape(poly=pa).distance(_Shape(poly=pb)) < gap - 1e-6):
                    continue
                out.append({"a": a.ref, "b": b.ref, "side": a.side})
        return out

    def near(self, at, radius: float = 3.0) -> list[dict]:
        """Footprints and pads within ``radius`` mm of a point (x, y) or "REF.PAD", nearest first."""
        x, y = self.point(at)
        found = []
        for f in self.footprints():
            b = f.box()
            if b:
                d = math.hypot(max(b[0] - x, 0, x - b[2]), max(b[1] - y, 0, y - b[3]))
                if d <= radius:
                    found.append({"kind": "footprint", "ref": f.ref, "side": f.side, "dist": d})
            for p in f.pads():
                d = math.hypot(p.x - x, p.y - y)
                if d <= radius:
                    found.append({"kind": "pad", "pad": f"{f.ref}.{p.number}", "net": p.net,
                                  "x": p.x, "y": p.y, "dist": d})
        return sorted(found, key=lambda f: (round(f["dist"], 3), f["kind"] != "pad"))

    def point(self, at) -> tuple[float, float]:
        """``"REF.PAD"`` -> the pad centre; ``(x, y)`` unchanged (mm)."""
        if isinstance(at, str):
            ref, _, num = at.partition(".")
            p = self.footprint(ref).pad(num)
            return p.x, p.y
        return float(at[0]), float(at[1])

    # --- moves -----------------------------------------------------------------------------

    def move(self, ref: str, to: tuple[float, float] | None = None, rot: float | None = None,
             side: str | None = None, allow_overlap: bool = False, force: bool = False) -> dict:
        """Move one footprint (position, rotation, side). Refused if its courtyard then
        overlaps another one on the same side, unless ``allow_overlap``, and if the part is
        locked, unless ``force``."""
        return self.move_many({ref: {"to": to, "rot": rot, "side": side}}, allow_overlap, force)

    def _refuse_locked(self, moves: dict, force: bool) -> None:
        """KatError if a move would change a locked part (staying put is fine: a locked part
        may be the reference of ``align``)."""
        if force:
            return
        held = []
        for ref, m in moves.items():
            f = self.footprint(ref)
            to, rot, side = m.get("to"), m.get("rot"), m.get("side")
            if f.locked and ((to and (abs(to[0] - f.x) > 1e-6 or abs(to[1] - f.y) > 1e-6))
                             or (rot is not None and abs((rot - f.rot + 180) % 360 - 180) > 1e-6)
                             or (side and side != f.side)):
                held.append(ref)
        if held:
            raise env.KatError(f"{', '.join(held)} locked: unlock first (b.footprint(ref).unlock()) "
                               "or pass force=True")

    def move_many(self, moves: dict, allow_overlap: bool = False, force: bool = False) -> dict:
        """Move several footprints at once ({ref: {to, rot, side}}); collisions are checked
        on the final arrangement, so parts may swap places. Nothing moves if one of them is
        locked, unless ``force``."""
        moves = {ref: {**m, "side": fp_side(m["side"]) if m.get("side") else None} for ref, m in moves.items()}
        self._refuse_locked(moves, force)
        undo = []
        for ref, m in moves.items():
            f = self.footprint(ref)
            undo.append((f, f.x, f.y, f.rot, f.side))
            if m.get("side") and m["side"] != f.side:
                f.flip()
            to = m.get("to")
            f.place(to[0] if to else None, to[1] if to else None, m.get("rot"))
        hits = self.collisions(list(moves))
        if hits and not allow_overlap:
            for f, x, y, rot, side in reversed(undo):
                if f.side != side:
                    f.flip()
                f.place(x, y, rot)
            raise env.KatError(f"courtyards would overlap: {hits[:8]} (allow_overlap to force)")
        return {"moved": [self.footprint(r).info() for r in moves], "overlaps": hits}

    def flip(self, ref: str, allow_overlap: bool = False, force: bool = False) -> dict:
        """Move ``ref`` to the other side at the same position; refused on a courtyard
        overlap unless ``allow_overlap``, and if locked unless ``force``. Returns like
        ``move``."""
        f = self.footprint(ref)
        return self.move(ref, side="B" if f.side == "F" else "F", allow_overlap=allow_overlap, force=force)

    def align(self, refs: list[str], axis: str = "y", value: float | None = None, force: bool = False) -> dict:
        """Put the footprints' origins on one line: ``axis`` "y" = same y (a row), "x" = same
        x (a column); ``value`` defaults to the first footprint's. A locked part that would
        move is refused unless ``force``."""
        fs = [self.footprint(r) for r in refs]
        v = value if value is not None else (fs[0].y if axis == "y" else fs[0].x)
        return self.move_many({f.ref: {"to": (f.x, v) if axis == "y" else (v, f.y)} for f in fs}, force=force)

    def distribute(self, refs: list[str], start: tuple[float, float], pitch: tuple[float, float],
                   rot: float | None = None, force: bool = False) -> dict:
        """Place footprints in the given order from ``start``, one ``pitch`` (dx, dy) apart.
        A locked part that would move is refused unless ``force``."""
        return self.move_many({r: {"to": (start[0] + i * pitch[0], start[1] + i * pitch[1]), "rot": rot}
                               for i, r in enumerate(refs)}, force=force)

    def place_free(self, ref: str, near, side: str | None = None, rot: float | None = None,
                   keepouts: list | None = None, gap: float = 0.25, max_distance: float = 20.0,
                   step: float = 0.25, force: bool = False) -> dict:
        """Put any part at the nearest legal spot around an anchor: ``near`` a part (``"U1"``,
        its courtyard's centre), a pad (``"U1.8"``) or a point (x, y). Rings of growing radius
        (``step`` mm apart, up to ``max_distance``) are tried, nearest first; a spot is legal
        when the part's courtyard (as drawn) stays ``gap`` mm from every other courtyard on
        its side, inside the board outline and out of its cut-outs, out of the board's
        footprint keep-outs on that side and of ``keepouts`` (rectangles x0, y0, x1, y1 or
        polygons), and its pads keep their clearance to other nets' copper. ``side`` F/B
        (top/bottom) and ``rot`` default to the part's. A locked part is refused unless
        ``force``. Returns ref, at, rot, side, distance (mm, its centre from the anchor) and
        tried (spots)."""
        from .cells import _Shape, polygons_overlap
        from .geometry import inside
        from .structure import _Copper
        f = self.footprint(ref)
        if f.locked and not force:
            raise env.KatError(f"{ref} locked: unlock first (b.footprint(ref).unlock()) or pass force=True")
        if isinstance(near, str):
            r, _, num = near.partition(".")
            host = self.footprint(r)
            if num:
                pad = host.pad(num)
                anchor = (pad.x, pad.y)
            else:
                hb = host.box()
                anchor = ((hb[0] + hb[2]) / 2, (hb[1] + hb[3]) / 2)
        else:
            anchor = (float(near[0]), float(near[1]))
        undo = (f.x, f.y, f.rot, f.side)
        if side is not None and fp_side(side) != f.side:
            f.flip()
        if rot is not None:
            f.place(rot=rot)
        court = f.courtyard_outline() or [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])] if (b := f.box()) else None
        if not court:
            raise env.KatError(f"{ref} has no courtyard and no pads to place by")
        cx = sum(p[0] for p in court) / len(court)
        cy = sum(p[1] for p in court) / len(court)
        pads = [(p.net, {l for l in self.copper_layers() if p.on(l)}, p.outline) for p in f.pads()]
        others = [(o.courtyard_outline() or [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])], b)
                  for o in self.footprints() if o is not f and o.side == f.side and (b := o.box())]
        loops = self.outline_loops()
        edge, holes = (loops[0], loops[1:]) if loops else (None, [])
        outer = "F.Cu" if f.side == "F" else "B.Cu"
        walls = []
        for k in keepouts or []:
            walls.append([(k[0], k[1]), (k[2], k[1]), (k[2], k[3]), (k[0], k[3])] if len(k) == 4 and
                         not isinstance(k[0], (tuple, list)) else [tuple(q) for q in k])
        from .coupling import _pts
        for z in self.zones():
            k = find(z, "keepout")
            lay = find(z, "layer") or find(z, "layers")
            if k is None or lay is None or not any(str(c[0]) == "footprints" and str(c[1]) == "not_allowed"
                                                   for c in k[1:] if isinstance(c, list) and len(c) > 1):
                continue
            if any(str(l) in (outer, "*.Cu", "F&B.Cu") for l in lay[1:]):
                walls.append(_pts(find(z, "polygon")))
        cu, cl = _Copper(self), self._clearance_fn()

        def legal(dx, dy) -> bool:
            poly = [(x + dx, y + dy) for x, y in court]
            bx = (min(p[0] for p in poly), min(p[1] for p in poly), max(p[0] for p in poly), max(p[1] for p in poly))
            if edge and not all(inside(edge, p) for p in poly):
                return False
            for o, ob in others:
                if overlap(bx, ob, gap - 1e-6) and (polygons_overlap(poly, o) if gap <= 0
                                                    else _Shape(poly=poly).distance(_Shape(poly=o)) < gap - 1e-6):
                    return False
            if any(polygons_overlap(poly, w) for w in holes + walls):
                return False
            return not any(cu.conflicts(net, lays, cl, poly=[(x + dx, y + dy) for x, y in out], skip=ref)
                           for net, lays, out in pads if lays)
        tried, r = 0, 0.0
        while r <= max_distance + 1e-9:
            n = 1 if r == 0 else max(8, math.ceil(2 * math.pi * r / step))
            for k in range(n):
                a = 2 * math.pi * k / n
                tx, ty = anchor[0] + r * math.cos(a), anchor[1] + r * math.sin(a)
                tried += 1
                dx, dy = tx - cx, ty - cy
                if legal(dx, dy):
                    f.place(round(f.x + dx, 4), round(f.y + dy, 4))
                    return {"ref": ref, "at": [f.x, f.y], "rot": f.rot, "side": f.side,
                            "distance": round(r, 3), "tried": tried}
            r += step
        if f.side != undo[3]:
            f.flip()
        f.place(undo[0], undo[1], undo[2])
        raise env.KatError(f"no legal spot for {ref} within {max_distance:g} mm of {near} ({tried} tried)")

    def place_near(self, ref: str, target: str, gap: float = 0.3, allow_overlap: bool = False,
                   force: bool = False) -> dict:
        """Put a two-pad part (decoupling capacitor, pull-up) next to the pad ``target``
        ("U1.8"): outside the target part's courtyard on the side the pad faces, oriented
        along that direction, with its pad on the target's net nearest to the target pad.
        Tries the spot and then sideways steps until no courtyard overlaps. A locked part is
        refused unless ``force``."""
        if self.footprint(ref).locked and not force:
            raise env.KatError(f"{ref} locked: unlock first (b.footprint(ref).unlock()) or pass force=True")
        tref, _, num = target.partition(".")
        host = self.footprint(tref)
        pad = host.pad(num)
        f = self.footprint(ref)
        start_rot = f.rot
        hb = host.box()
        cx, cy = (hb[0] + hb[2]) / 2, (hb[1] + hb[3]) / 2
        dx, dy = pad.x - cx, pad.y - cy
        if abs(dx) >= abs(dy):
            ux, uy = (1.0 if dx > 0 else -1.0), 0.0
        else:
            ux, uy = 0.0, (1.0 if dy > 0 else -1.0)
        # orientation: the part's pad on the target net first, pointing at the target pad
        mine = [p for p in f.pads() if p.number]
        near_pad = next((p for p in mine if p.net and p.net == pad.net), mine[0])
        best = None
        for rot in (0, 90, 180, 270):
            f.place(rot=rot)
            ps = {p.number: p for p in f.pads()}
            a = ps[near_pad.number]
            others = [p for p in ps.values() if p.number != near_pad.number]
            if not others:
                break
            vx, vy = others[0].x - a.x, others[0].y - a.y
            if abs(vx * ux + vy * uy) > 0.5 * math.hypot(vx, vy) and (vx * ux + vy * uy) > 0:
                best = rot
                break
        f.place(rot=best if best is not None else 0)
        fb = f.box()
        # distance from the part's origin to its near pad, and to its courtyard edge
        a = next(p for p in f.pads() if p.number == near_pad.number)
        off_x, off_y = a.x - f.x, a.y - f.y
        edge = {(1, 0): hb[2], (-1, 0): hb[0], (0, 1): hb[3], (0, -1): hb[1]}[(int(ux), int(uy))]
        back = {(1, 0): f.x - fb[0], (-1, 0): fb[2] - f.x, (0, 1): f.y - fb[1], (0, -1): fb[3] - f.y}[(int(ux), int(uy))]
        if ux:
            x0, y0 = edge + ux * (back + gap), pad.y - off_y
        else:
            x0, y0 = pad.x - off_x, edge + uy * (back + gap)
        step = max(fb[2] - fb[0], fb[3] - fb[1]) + GAP
        from .structure import _Copper
        cu, cl = _Copper(self), self._clearance_fn()
        for k in (0, 1, -1, 2, -2, 3, -3):
            x, y = (x0, y0 + k * step) if ux else (x0 + k * step, y0)
            try:
                res = self.move(ref, to=(round(x, 3), round(y, 3)), rot=f.rot,
                                side=host.side, allow_overlap=allow_overlap, force=force)
            except env.KatError:
                continue
            # pads must also clear copper that is already laid (vias, tracks, other pads)
            moved = self.footprint(ref)
            if any(cu.conflicts(p.net, {l for l in self.copper_layers() if p.on(l)}, cl,
                                poly=p.outline, skip=ref) for p in moved.pads() if p.number):
                continue
            return res
        f.place(rot=start_rot)
        raise env.KatError(f"no free spot for {ref} next to {target}")

    # --- blocks ----------------------------------------------------------------------------

    def blocks(self) -> dict[str, list[str]]:
        """Block name -> references (the ``Block`` field: a SKiDL subcircuit's instance
        path such as ``CH_A/RC1``, or set by hand in SKiDL). A nested block's parts also
        belong to every block above it: ``CH_A`` includes ``CH_A/RC1``."""
        out: dict[str, list[str]] = {}
        for f in self.footprints():
            name = f.field(BLOCK)
            if not name:
                continue
            levels = name.split("/")
            for i in range(1, len(levels) + 1):
                out.setdefault("/".join(levels[:i]), []).append(f.ref)
        return out

    def block_box(self, name: str) -> Box:
        """Bounding box (mm) of the courtyards of the block ``name``."""
        refs = self.blocks().get(name)
        if not refs:
            raise env.KatError(f"no footprints with {BLOCK} = {name!r}")
        return union(self.footprint(r).box() for r in refs)

    def move_block(self, name: str, to: tuple[float, float] | None = None,
                   by: tuple[float, float] | None = None, allow_overlap: bool = False,
                   force: bool = False) -> dict:
        """Move a block as a unit: its box's top-left corner ``to`` a point, or ``by`` (dx, dy).
        Refused if one of its parts is locked, unless ``force``."""
        box = self.block_box(name)
        dx, dy = by if by else (to[0] - box[0], to[1] - box[1])
        moves = {r: {"to": (self.footprint(r).x + dx, self.footprint(r).y + dy)}
                 for r in self.blocks()[name]}
        return self.move_many(moves, allow_overlap, force)

    def _block_key(self, ref: str, block: str) -> str:
        """Identity of a part inside its block: its SKiDL tag with the block name taken
        out (CH_B/RC1/r -> */RC1/r, ch2_r_in -> *_r_in), else footprint + order."""
        f = self.footprint(ref)
        tag = f.field(TAG) or ""
        if tag.startswith(block + "/"):  # tag qualified by the instance path (CH_A/RC1/r)
            return f"tag:*{tag[len(block):]}"
        if tag and block in tag:
            return f"tag:{tag.replace(block, '*')}"
        return ""

    def replicate(self, template: str, targets: dict[str, tuple[float, float]] | list[str],
                  allow_overlap: bool = False, force: bool = False) -> dict:
        """Arrange each target block like ``template``: same relative positions, rotations
        and sides. ``targets``: {block: (x, y) of its top-left corner} or a list (keep each
        block's current corner). Parts pair up by SKiDL tag with the block name removed,
        otherwise by footprint and reference order. A locked target part that would move is
        refused unless ``force``."""
        blocks = self.blocks()
        tmpl = blocks.get(template)
        if not tmpl:
            raise env.KatError(f"no footprints with {BLOCK} = {template!r}")
        tbox = self.block_box(template)
        if isinstance(targets, list):
            targets = {name: self.block_box(name)[:2] for name in targets}
        moves, pairs = {}, {}
        for name, corner in targets.items():
            refs = blocks.get(name)
            if not refs:
                raise env.KatError(f"no footprints with {BLOCK} = {name!r}")
            mapping = self._pair(template, tmpl, name, refs)
            pairs[name] = mapping
            for tr, rr in mapping.items():
                t = self.footprint(tr)
                moves[rr] = {"to": (corner[0] + t.x - tbox[0], corner[1] + t.y - tbox[1]),
                             "rot": t.rot, "side": t.side}
        res = self.move_many(moves, allow_overlap, force)
        res["pairs"] = pairs
        return res

    def _pair(self, tname: str, trefs: list[str], name: str, refs: list[str]) -> dict[str, str]:
        from ..syncer import natural_key
        by_key = {k: r for r in refs if (k := self._block_key(r, name))}
        mapping, left = {}, []
        for tr in trefs:
            k = self._block_key(tr, tname)
            if k and k in by_key:
                mapping[tr] = by_key.pop(k)
            else:
                left.append(tr)
        rest = [r for r in refs if r not in mapping.values()]
        for fp in {self.footprint(r).lib_id for r in left}:
            a = sorted((r for r in left if self.footprint(r).lib_id == fp), key=natural_key)
            b = sorted((r for r in rest if self.footprint(r).lib_id == fp), key=natural_key)
            if len(a) != len(b):
                raise env.KatError(f"blocks {tname} and {name} differ in {fp}: {a} vs {b}")
            mapping.update(zip(a, b))
        if len(mapping) != len(trefs) or len(refs) != len(trefs):
            raise env.KatError(f"blocks {tname} and {name} do not have the same parts")
        return mapping
