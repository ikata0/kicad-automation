"""Placement on the board: moves checked against courtyards, alignment, parts next to a pin,
functional blocks (the ``Block`` field set in SKiDL) and replication of a block's
arrangement onto the other blocks. Mixed into ``Board``."""

from __future__ import annotations

import math

from .. import env
from .geometry import Box, overlap, union

BLOCK = "Block"
TAG = "SKiDL Tag"
GAP = 0.25  # courtyard to courtyard, mm


class PlacementMixin:
    # --- queries ---------------------------------------------------------------------------

    def collisions(self, refs: list[str] | None = None, gap: float = 0.0) -> list[dict]:
        """Courtyard overlaps between footprints on the same side (optionally only those
        involving ``refs``)."""
        fps = [(f, f.box()) for f in self.footprints() if f.box()]
        want = set(refs) if refs else None
        out = []
        for i, (a, ba) in enumerate(fps):
            for b, bb in fps[i + 1:]:
                if a.side != b.side or (want and a.ref not in want and b.ref not in want):
                    continue
                if overlap(ba, bb, gap - 1e-6):
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
             side: str | None = None, allow_overlap: bool = False) -> dict:
        """Move one footprint (position, rotation, side). Refused if its courtyard then
        overlaps another one on the same side, unless ``allow_overlap``."""
        return self.move_many({ref: {"to": to, "rot": rot, "side": side}}, allow_overlap)

    def move_many(self, moves: dict, allow_overlap: bool = False) -> dict:
        """Move several footprints at once ({ref: {to, rot, side}}); collisions are checked
        on the final arrangement, so parts may swap places."""
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

    def flip(self, ref: str, allow_overlap: bool = False) -> dict:
        """Move ``ref`` to the other side at the same position; refused on a courtyard
        overlap unless ``allow_overlap``. Returns like ``move``."""
        f = self.footprint(ref)
        return self.move(ref, side="B" if f.side == "F" else "F", allow_overlap=allow_overlap)

    def align(self, refs: list[str], axis: str = "y", value: float | None = None) -> dict:
        """Put the footprints' origins on one line: ``axis`` "y" = same y (a row), "x" = same
        x (a column); ``value`` defaults to the first footprint's."""
        fs = [self.footprint(r) for r in refs]
        v = value if value is not None else (fs[0].y if axis == "y" else fs[0].x)
        return self.move_many({f.ref: {"to": (f.x, v) if axis == "y" else (v, f.y)} for f in fs})

    def distribute(self, refs: list[str], start: tuple[float, float], pitch: tuple[float, float],
                   rot: float | None = None) -> dict:
        """Place footprints in the given order from ``start``, one ``pitch`` (dx, dy) apart."""
        return self.move_many({r: {"to": (start[0] + i * pitch[0], start[1] + i * pitch[1]), "rot": rot}
                               for i, r in enumerate(refs)})

    def place_near(self, ref: str, target: str, gap: float = 0.3, allow_overlap: bool = False) -> dict:
        """Put a two-pad part (decoupling capacitor, pull-up) next to the pad ``target``
        ("U1.8"): outside the target part's courtyard on the side the pad faces, oriented
        along that direction, with its pad on the target's net nearest to the target pad.
        Tries the spot and then sideways steps until no courtyard overlaps."""
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
                                side=host.side, allow_overlap=allow_overlap)
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
        """Block name -> references (the ``Block`` field, set in SKiDL)."""
        out: dict[str, list[str]] = {}
        for f in self.footprints():
            name = f.field(BLOCK)
            if name:
                out.setdefault(name, []).append(f.ref)
        return out

    def block_box(self, name: str) -> Box:
        """Bounding box (mm) of the courtyards of the block ``name``."""
        refs = self.blocks().get(name)
        if not refs:
            raise env.KatError(f"no footprints with {BLOCK} = {name!r}")
        return union(self.footprint(r).box() for r in refs)

    def move_block(self, name: str, to: tuple[float, float] | None = None,
                   by: tuple[float, float] | None = None, allow_overlap: bool = False) -> dict:
        """Move a block as a unit: its box's top-left corner ``to`` a point, or ``by`` (dx, dy)."""
        box = self.block_box(name)
        dx, dy = by if by else (to[0] - box[0], to[1] - box[1])
        moves = {r: {"to": (self.footprint(r).x + dx, self.footprint(r).y + dy)}
                 for r in self.blocks()[name]}
        return self.move_many(moves, allow_overlap)

    def _block_key(self, ref: str, block: str) -> str:
        """Identity of a part inside its block: its SKiDL tag with the block name taken
        out (ch2_r_in -> *_r_in), else footprint + order."""
        f = self.footprint(ref)
        tag = f.field(TAG) or ""
        if tag and block in tag:
            return f"tag:{tag.replace(block, '*')}"
        return ""

    def replicate(self, template: str, targets: dict[str, tuple[float, float]] | list[str],
                  allow_overlap: bool = False) -> dict:
        """Arrange each target block like ``template``: same relative positions, rotations
        and sides. ``targets``: {block: (x, y) of its top-left corner} or a list (keep each
        block's current corner). Parts pair up by SKiDL tag with the block name removed,
        otherwise by footprint and reference order."""
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
        res = self.move_many(moves, allow_overlap)
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
