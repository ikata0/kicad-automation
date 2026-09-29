"""Board finishing: readable reference designators, board text, title block from the
schematic, fiducials. Mixed into ``Board``; KiCad's DRC (silk_overlap, silk_over_copper,
silk_edge_clearance) judges the silkscreen."""

from __future__ import annotations

from pathlib import Path

from .. import env
from ..schematic import new_uuid, text_width
from ..sexpr import Sym, find, find_all, loads, num
from .geometry import overlap, rotate

FIDUCIAL = "Fiducial:Fiducial_1mm_Mask2mm"
SILK_TO_PAD = 0.15   # solder mask expansion + margin, mm


def _text_box(x: float, y: float, text: str, size: float, angle: float):
    w, h = text_width(text, size), size * 1.2
    if angle % 180 == 90:
        w, h = h, w
    return x - w / 2, y - h / 2, x + w / 2, y + h / 2


def _text_spec(node: list) -> tuple:
    """Position, angle, font size and mirroring of a gr_text, independent of formatting."""
    r = lambda t: round(float(str(t)), 4)
    font = find(find(node, "effects"), "font")
    size = find(font, "size") if font is not None else None
    just = find(find(node, "effects"), "justify")
    at = [r(t) for t in find(node, "at")[1:]]
    return (tuple(at + [0.0] * (3 - len(at))),
            tuple(r(t) for t in size[1:]) if size is not None else None,
            just is not None and "mirror" in [str(t) for t in just[1:]])


class FinishMixin:
    def place_refs(self, refs: list[str] | None = None, size: float = 0.8, gap: float = 0.25) -> dict:
        """Put each reference designator on the silkscreen next to its part: above, below,
        left or right of the courtyard - the first spot that clears pads, other references
        and the board edge. Text is upright (0 deg), or 90 deg for tall parts. Parts whose
        reference finds no spot are listed in ``failed`` (then shown on the fab layer only)."""
        fps = [f for f in self.footprints() if refs is None or f.ref in refs]
        pads = [(p.box, p) for p in self.pads()]
        # every part's silkscreen drawing (outlines, pin-1 marks): a reference on it is a
        # silk_overlap finding. A line drawing is its own thin box, not the whole outline
        silk = [(f.side, b) for f in self.footprints() for b in f.graphics_boxes("SilkS")]
        edge = self.outline_box()
        taken = []   # boxes of references already placed (this call and untouched ones)
        mine = {f.ref for f in fps}
        for f in self.footprints():
            if f.ref not in mine and (b := self._ref_box(f)):
                taken.append(b)
        placed, failed = [], []
        for f in fps:
            cy = f.box()
            if not cy:
                continue
            tall = (cy[3] - cy[1]) > 2.5 * (cy[2] - cy[0])
            angle = 90 if tall else 0
            cx, cm = (cy[0] + cy[2]) / 2, (cy[1] + cy[3]) / 2
            w, h = text_width(f.ref, size), size * 1.2
            tw, th = (h, w) if angle == 90 else (w, h)
            spots = [(cx, cy[1] - gap - th / 2), (cx, cy[3] + gap + th / 2),
                     (cy[0] - gap - tw / 2, cm), (cy[2] + gap + tw / 2, cm)]
            for x, y in spots:
                box = _text_box(x, y, f.ref, size, angle)
                if edge and not (edge[0] + 0.3 <= box[0] and box[2] <= edge[2] - 0.3
                                 and edge[1] + 0.3 <= box[1] and box[3] <= edge[3] - 0.3):
                    continue
                if any(overlap(box, pb, SILK_TO_PAD) for pb, p in pads if p.on(f"{f.side}.Cu") or
                       p.kind == "thru_hole"):
                    continue
                if any(overlap(box, t, 0.1) for t in taken):
                    continue
                if any(side == f.side and overlap(box, sb, 0.1) for side, sb in silk):
                    continue
                self._set_ref(f, x, y, angle, size, visible=True)
                taken.append(box)
                placed.append(f.ref)
                break
            else:
                self._set_ref(f, cx, cm, angle, size, visible=False)
                failed.append(f.ref)
        return {"placed": len(placed), "failed": failed}

    def _ref_prop(self, f):
        return next(p for p in find_all(f.node, "property") if str(p[1]) == "Reference")

    def _ref_box(self, f):
        p = self._ref_prop(f)
        if find(p, "hide") is not None and str(find(p, "hide")[1]) == "yes":
            return None
        at = find(p, "at")
        x, y = f.to_board(float(at[1]), float(at[2]))
        size = float(find(find(find(p, "effects"), "font"), "size")[1])
        return _text_box(x, y, f.ref, size, float(at[3]) if len(at) > 3 else 0)

    def _set_ref(self, f, x: float, y: float, angle: float, size: float, visible: bool) -> None:
        """Reference text at board (x, y): stored in footprint coordinates, angle absolute."""
        p = self._ref_prop(f)
        lx, ly = rotate(x - f.x, y - f.y, -f.rot)
        at = find(p, "at")
        at[1:] = [num(lx), num(ly)] + ([num(angle)] if angle else [])
        lay = find(p, "layer")
        lay[1] = f"{f.side}.SilkS" if visible else f"{f.side}.Fab"
        hide = find(p, "hide")
        if hide is not None:
            p.remove(hide)
        font = find(find(p, "effects"), "font")
        find(font, "size")[1:] = [num(size), num(size)]
        th = find(font, "thickness")
        if th is not None:
            th[1] = num(round(size * 0.15, 3))

    def board_text(self, text: str, at: tuple[float, float], size: float = 1.5,
                   layer: str = "F.SilkS", angle: float = 0) -> dict:
        """Free text on the board (name, revision, notes) centred at ``at`` (mm), ``size`` in
        mm; ``B.SilkS`` is mirrored. Safe to run again: the same text on the same layer is
        moved and resized instead of added twice. Returns ``text``, ``layer``, ``box``,
        ``status``."""
        old = next((n for n in find_all(self.tree, "gr_text") if str(n[1]) == text
                    and str(find(n, "layer")[1]) == layer), None)
        effects = [Sym("effects"), [Sym("font"), [Sym("size"), num(size), num(size)],
                                    [Sym("thickness"), num(round(size * 0.15, 3))]]]
        if layer.startswith("B."):
            effects.append([Sym("justify"), Sym("mirror")])
        node = [Sym("gr_text"), text, [Sym("at"), num(at[0]), num(at[1])] + ([num(angle)] if angle else []),
                [Sym("layer"), layer], [Sym("uuid"), new_uuid()], effects]
        if old is None:
            self._insert(node)
            status = "created"
        else:
            find(node, "uuid")[1] = find(old, "uuid")[1]
            status = "unchanged" if _text_spec(old) == _text_spec(node) else "replaced"
            if status == "replaced":
                self.tree[self.tree.index(old)] = node
        return {"text": text, "layer": layer, "box": list(_text_box(at[0], at[1], text, size, angle)),
                "status": status}

    def title_from_schematic(self, sch: Path | str | None = None) -> dict:
        """Copy the schematic's title block (title, date, revision, company, comments)."""
        sch = Path(sch) if sch else self.path.with_suffix(".kicad_sch")
        tb = find(loads(sch.read_text(encoding="utf-8")), "title_block")
        if tb is None:
            raise env.KatError(f"{sch} has no title block (kat title SCH ...)")
        mine = find(self.tree, "title_block")
        if mine is not None:
            self.tree.remove(mine)
        idx = next(i for i, c in enumerate(self.tree) if isinstance(c, list) and str(c[0]) == "paper") + 1
        self.tree.insert(idx, tb)
        return {str(c[0]) if str(c[0]) != "comment" else f"comment{c[1]}": str(c[-1])
                for c in tb[1:] if isinstance(c, list)}

    def fiducials(self, count: int = 3, side: str = "F", margin: float = 3.0) -> dict:
        """Global fiducials (board-only) near ``count`` corners of the outline (``margin`` mm
        inside), each on the nearest spot that clears every courtyard. Safe to run again:
        existing fiducials are placed anew (unchanged ones keep their identity). Returns
        ``fiducials`` [{ref, at}]."""
        edge = self.outline_box()
        if not edge:
            raise env.KatError("fiducials need a board outline")
        old = {f.ref: f for f in self.footprints() if f.board_only and f.lib_id == FIDUCIAL}
        for ref in old:
            self.remove_footprint(ref)
        corners = [(edge[0] + margin, edge[1] + margin), (edge[2] - margin, edge[3] - margin),
                   (edge[0] + margin, edge[3] - margin), (edge[2] - margin, edge[1] + margin)][:count]
        used = {f.ref for f in self.footprints()}
        placed = []
        for cx, cy in corners:
            ref = next(f"FID{i}" for i in range(1, 100) if f"FID{i}" not in used)
            f = self.add_board_part(FIDUCIAL, ref, (cx, cy), side=side)
            ok = False
            for step in range(0, 40):
                dx = (1 if cx < (edge[0] + edge[2]) / 2 else -1) * (step % 8) * 1.0
                dy = (1 if cy < (edge[1] + edge[3]) / 2 else -1) * (step // 8) * 1.0
                f.place(cx + dx, cy + dy)
                if not self.collisions([ref]):
                    ok = True
                    break
            if not ok:
                self.remove_footprint(ref)
                continue
            used.add(ref)
            prev = old.get(ref)
            if prev is not None and (prev.x, prev.y, prev.side) == (f.x, f.y, f.side):
                self.tree[self.tree.index(f.node)] = prev.node     # same spot: keep the old one
            placed.append({"ref": ref, "at": [f.x, f.y]})
        return {"fiducials": placed}
