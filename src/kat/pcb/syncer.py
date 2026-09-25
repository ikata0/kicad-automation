"""Board sync: bring the schematic's parts and nets onto the board.

Source is the netlist KiCad exports from the schematic (its net names and fields are what
DRC schematic parity compares). Footprints are matched by symbol path (KiCad's own link),
then by SKiDL tag, then by reference. Placement and copper stay; copper that no longer
fits its pads is reported as stale (and removed with ``rip_stale``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import env, gate
from ..sexpr import Sym, find, find_all
from ..syncer import natural_key
from . import checks
from . import footprints as fps
from .board import Board, Footprint

TAG = "SKiDL Tag"
KEEP = {"Reference", "Value", "Footprint", "Datasheet", "Description"}  # never removed


@dataclass
class Part:
    ref: str
    value: str
    footprint: str
    path: str
    sheetname: str
    sheetfile: str
    fields: dict[str, str] = field(default_factory=dict)  # all but Reference/Value/Footprint
    dnp: bool = False
    exclude_from_bom: bool = False
    pins: dict[str, tuple[str, str]] = field(default_factory=dict)  # number -> (name, type)

    @property
    def tag(self) -> str:
        return self.fields.get(TAG, "")


def read_design(sch: Path) -> tuple[dict[str, Part], dict[tuple[str, str], str]]:
    """Board parts (by reference) and pin -> net, from the schematic's own netlist."""
    tree = gate._export(sch)
    libpins = {}
    for lp in find_all(find(tree, "libparts") or [], "libpart"):
        libpins[(str(find(lp, "lib")[1]), str(find(lp, "part")[1]))] = {
            str(find(p, "num")[1]): (str(find(p, "name")[1]), str(find(p, "type")[1]))
            for p in find_all(find(lp, "pins") or [], "pin")}
    parts = {}
    for c in find_all(find(tree, "components") or [], "comp"):
        ref = str(find(c, "ref")[1])
        props = {str(find(p, "name")[1]): (str(find(p, "value")[1]) if find(p, "value") else "")
                 for p in find_all(c, "property")}
        fields = {str(find(f, "name")[1]): (str(f[2]) if len(f) > 2 else "")
                  for f in find_all(find(c, "fields") or [], "field")}
        val, fpn = find(c, "value"), find(c, "footprint")
        sheet = find(c, "sheetpath")
        src = find(c, "libsource")
        parts[ref] = Part(
            ref=ref, value=str(val[1]) if val and len(val) > 1 else "",
            footprint=str(fpn[1]) if fpn and len(fpn) > 1 else "",
            path=str(find(sheet, "tstamps")[1]) + str(find(c, "tstamps")[1]),
            sheetname=props.get("Sheetname", ""), sheetfile=props.get("Sheetfile", ""),
            fields={k: v for k, v in fields.items() if k not in ("Reference", "Value", "Footprint")},
            dnp="dnp" in props, exclude_from_bom="exclude_from_bom" in props,
            pins=libpins.get((str(find(src, "lib")[1]), str(find(src, "part")[1])), {}))
    pin_net = {}
    for n in find_all(find(tree, "nets") or [], "net"):
        name = str(find(n, "name")[1])
        for node in find_all(n, "node"):
            pin_net[(str(find(node, "ref")[1]), str(find(node, "pin")[1]))] = name
    return parts, pin_net


def _set_child(node: list, key: str, value: str | None) -> bool:
    """Set ``(key "value")`` in a node; ``None`` removes it. True if something changed."""
    cur = find(node, key)
    if value is None:
        if cur is not None:
            node.remove(cur)
            return True
        return False
    if cur is not None:
        if str(cur[1]) == value:
            return False
        cur[1] = value
        return True
    props = find_all(node, "property")
    idx = node.index(props[-1]) + 1 if props else len(node)
    node.insert(idx, [Sym(key), value])
    return True


def _set_attr_flag(node: list, flag: str, on: bool) -> bool:
    attr = find(node, "attr")
    has = attr is not None and any(str(t) == flag for t in attr[1:])
    if has == on:
        return False
    if attr is None:
        attr = [Sym("attr")]
        props = find_all(node, "property")
        node.insert(node.index(props[-1]) + 1 if props else len(node), attr)
    if on:
        attr.append(Sym(flag))
    else:
        attr[:] = [t for t in attr if str(t) != flag]
    return True


def _point_in(poly: list[tuple[float, float]], x: float, y: float) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


class BoardSync:
    def __init__(self, pcb: Path, sch: Path):
        self.pcb, self.sch = Path(pcb), Path(sch)
        self.report: dict[str, list] = {k: [] for k in (
            "added", "updated", "replaced", "removed", "renamed", "renamed_nets",
            "stale_tracks", "unresolved")}
        self.changed = False

    # --- matching ------------------------------------------------------------------

    def _match(self, board: Board, parts: dict[str, Part]) -> dict[str, Footprint]:
        fps_ = board.footprints()
        by_path = {f.path: f for f in fps_ if f.path}
        by_tag = {f.field(TAG): f for f in fps_ if f.field(TAG)}
        by_ref = {f.ref: f for f in fps_}
        design_paths = {p.path for p in parts.values()}
        matched, used = {}, set()
        for how in ("path", "tag", "ref"):
            for ref in sorted(parts, key=natural_key):
                if ref in matched:
                    continue
                p = parts[ref]
                f = {"path": by_path.get(p.path), "tag": by_tag.get(p.tag) if p.tag else None,
                     "ref": by_ref.get(ref)}[how]
                # a footprint still linked to another design symbol is not free for a fallback
                if f is None or id(f) in used or (how != "path" and f.path in design_paths):
                    continue
                matched[ref] = f
                used.add(id(f))
        return matched

    # --- the run ---------------------------------------------------------------------

    def run(self, dry_run: bool = False, rip_stale: bool = False) -> dict:
        parts, pin_net = read_design(self.sch)
        if self.pcb.exists():
            board = Board(self.pcb)
        else:
            if dry_run:
                raise env.KatError(f"{self.pcb} does not exist (a dry run creates nothing)")
            board = Board.create(self.pcb)
            self.changed = True
        before = self._copper_view(board)
        matched = self._match(board, parts)

        kept = {id(m.node) for m in matched.values()}
        for f in board.footprints():
            if id(f.node) not in kept:
                self.report["removed"].append(f.ref)
                board.tree.remove(f.node)
                self.changed = True
        for ref in sorted(matched, key=natural_key):
            f = matched[ref]
            p = parts[ref]
            if not p.footprint:
                self.report["unresolved"].append({"ref": ref, "problem": "no footprint"})
                continue
            if f.lib_id != p.footprint:
                f = self._replace(board, f, p)
                matched[ref] = f
            self._update(f, p, pin_net)
        new = [parts[r] for r in sorted(parts, key=natural_key) if r not in matched]
        self._place_new(board, new, pin_net)
        self._copper(board, before, rip_stale)

        report = dict(self.report, changed=self.changed, board=str(self.pcb))
        if self.changed and not dry_run:
            board.save()
        if not dry_run:
            d = checks.drc(self.pcb, parity=True)
            report["check"] = {"parity": len(d["parity"]), "unconnected": len(d["unconnected"]),
                               "drc_errors": d["errors"], "drc_warnings": d["warnings"],
                               "parity_items": [v["description"] for v in d["parity"][:20]]}
        return report

    # --- parts -----------------------------------------------------------------------

    def _update(self, f: Footprint, p: Part, pin_net: dict, new: bool = False) -> None:
        """Make the footprint match the part; ``new`` parts are reported as added only."""
        def note(field_, old, value):
            if not new:
                self.report["updated"].append({"ref": p.ref, "field": field_, "from": old, "to": value})
            self.changed = True

        if f.ref != p.ref:
            self.report["renamed"].append({"from": f.ref, "to": p.ref})
            f.set_field("Reference", p.ref)
            self.changed = True
        if f.value != p.value:
            note("Value", f.value, p.value)
            f.set_field("Value", p.value)
        for k, v in p.fields.items():
            have = f.field(k)
            if have != v:
                note(k, have or "", v)
                f.set_field(k, v, hide=True)
        for k in list(f.fields()):
            if k not in KEEP and k not in p.fields:
                note(k, f.field(k), None)
                f.remove_field(k)
        for key, val in (("path", p.path), ("sheetname", p.sheetname), ("sheetfile", p.sheetfile)):
            self.changed |= _set_child(f.node, key, val)
        self.changed |= _set_attr_flag(f.node, "dnp", p.dnp)
        self.changed |= _set_attr_flag(f.node, "exclude_from_bom", p.exclude_from_bom)
        for pad in find_all(f.node, "pad"):
            num = str(pad[1])
            if not num:
                continue
            self.changed |= _set_child(pad, "net", pin_net.get((p.ref, num)))
            name, typ = p.pins.get(num, (None, None))
            self.changed |= _set_child(pad, "pinfunction", name if name and name != "~" else None)
            if typ is not None and (p.ref, num) not in pin_net:
                typ += "+no_connect"
            self.changed |= _set_child(pad, "pintype", typ)

    def _replace(self, board: Board, f: Footprint, p: Part) -> Footprint:
        node = fps.instantiate(p.footprint, f.x, f.y, f.rot, f.side, board.project_dir)
        find(node, "uuid")[1] = f.uuid  # same part: its pads keep their identity for copper checks
        new = Footprint(node)
        for k, v in f.fields().items():
            new.set_field(k, v, hide=True)
        idx = board.tree.index(f.node)
        board.tree[idx] = node
        self.report["replaced"].append({"ref": p.ref, "from": f.lib_id, "to": p.footprint})
        self.changed = True
        return new

    def _place_new(self, board: Board, new: list[Part], pin_net: dict) -> None:
        """New parts in rows to the right of the outline (or of everything placed)."""
        todo = [p for p in new if p.footprint]
        for p in new:
            if not p.footprint:
                self.report["unresolved"].append({"ref": p.ref, "problem": "no footprint"})
        if not todo:
            return
        from .geometry import union
        extent = board.outline_box() or union(f.box() for f in board.footprints())
        x0, y0 = (extent[2] + 10, extent[1]) if extent else (20.0, 20.0)
        x, y, row_h, gap, row_w = x0, y0, 0.0, 2.0, 100.0
        for p in todo:
            try:
                node = fps.instantiate(p.footprint, 0, 0, 0, "F", board.project_dir)
            except env.KatError as e:
                self.report["unresolved"].append({"ref": p.ref, "problem": str(e)})
                continue
            f = Footprint(node)
            f.set_field("Reference", p.ref)
            b = f.box() or (-1, -1, 1, 1)
            w, h = b[2] - b[0], b[3] - b[1]
            if x > x0 and x + w > x0 + row_w:
                x, y, row_h = x0, y + row_h + gap, 0.0
            f.place(round(x - b[0], 2), round(y - b[1], 2))
            x += w + gap
            row_h = max(row_h, h)
            board._insert(node)
            self._update(f, p, pin_net, new=True)
            self.report["added"].append({"ref": p.ref, "footprint": p.footprint,
                                         "at": [f.x, f.y]})
            self.changed = True

    # --- copper ----------------------------------------------------------------------

    @staticmethod
    def _copper_view(board: Board) -> dict[str, set]:
        """Old net -> set of (footprint uuid, pad number) it connected."""
        out: dict[str, set] = {}
        for f in board.footprints():
            for pad in f.pads():
                if pad.net:
                    out.setdefault(pad.net, set()).add((f.uuid, pad.number))
        return out

    def _copper(self, board: Board, before: dict[str, set], rip_stale: bool) -> None:
        now: dict[tuple, str] = {}
        pads = []
        for f in board.footprints():
            for pad in f.pads():
                now[(f.uuid, pad.number)] = pad.net
                pads.append(pad)
        # a net that vanished while all its pads went to one new net: renamed on the copper
        live_nets = {n for n in now.values() if n}
        rename = {}
        for old, members in before.items():
            targets = {now[m] for m in members if m in now}  # pads of removed parts don't vote
            if old not in live_nets and len(targets) == 1 and (t := targets.pop()):
                rename[old] = t
        items = [(t["node"], "track", t) for t in board.tracks()] + \
                [(v["node"], "via", v) for v in board.vias()] + \
                [(z, "zone", None) for z in board.zones()]
        for node, kind, _ in items:
            net = find(node, "net")
            if net is not None and str(net[1]) in rename:
                self.report["renamed_nets"].append({"from": str(net[1]), "to": rename[str(net[1])],
                                                    "kind": kind})
                net[1] = rename[str(net[1])]
                self.changed = True
        # stale: copper on a net the design no longer has, or touching a pad of another net
        stale = []
        for t in board.tracks():
            reason = None
            if t["net"] and t["net"] not in live_nets:
                reason = "net gone"
            else:
                for (x, y) in (t["start"], t["end"]):
                    for pad in pads:
                        if pad.on(t["layer"]) and pad.net != t["net"] and _point_in(pad.outline, x, y):
                            reason = f"touches {pad.ref}.{pad.number} ({pad.net or 'no net'})"
            if reason:
                stale.append((t["node"], {"kind": t["kind"], "net": t["net"], "layer": t["layer"],
                                          "start": list(t["start"]), "end": list(t["end"]),
                                          "why": reason}))
        for v in board.vias():
            if v["net"] and v["net"] not in live_nets:
                stale.append((v["node"], {"kind": "via", "net": v["net"], "at": list(v["at"]),
                                          "why": "net gone"}))
        for z in board.zones():
            net = find(z, "net")
            if net is not None and str(net[1]) and str(net[1]) not in live_nets:
                name = find(z, "name")
                stale.append((None, {"kind": "zone", "net": str(net[1]),
                                     "name": str(name[1]) if name else "", "why": "net gone"}))
        for node, info in stale:
            if rip_stale and node is not None:
                board.tree.remove(node)
                info["removed"] = True
                self.changed = True
            self.report["stale_tracks"].append(info)


def run(pcb: Path, sch: Path | None = None, dry_run: bool = False, force: bool = False,
        rip_stale: bool = False, require: tuple[str, ...] = ("MPN",)) -> dict:
    pcb = Path(pcb)
    sch = Path(sch) if sch else pcb.with_suffix(".kicad_sch")
    if not sch.exists():
        raise env.KatError(f"schematic {sch} not found")
    ready = gate.run(sch, None, require)
    summary = {"ready": ready["ready"], "by_problem": ready["by_problem"]}
    if not ready["ready"] and not force:
        return {"board": str(pcb), "gate": summary, "changed": False, "ok": False,
                "error": "schematic not ready for layout (kat ready); fix it or use force"}
    report = BoardSync(pcb, sch).run(dry_run, rip_stale)
    report["gate"] = summary
    if "check" in report:
        report["ok"] = report["check"]["parity"] == 0 and not report["unresolved"]
    return report
