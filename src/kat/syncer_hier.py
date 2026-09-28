"""Sync a SKiDL design into a hierarchical schematic: chosen block types become sheets.

``kat sync ROOT DESIGN --sheets rc,ch``: every instance of a listed block type (SKiDL
``@subcircuit``, from the block record ``DESIGN.blocks.json``) becomes a sheet box on its
parent sheet; everything else is drawn flat, as ``kat sync`` always does.

- **Sheet files come from the block library**: a sheet box points to the block's own
  checked drawing, ``blocks/NAME/NAME_block.kicad_sch`` (``kat block build`` writes it).
  It is used as it is, never redrawn by the design. Its hierarchical labels are the ports.
- **The block's own build** (``kat block build``) uses the same machinery with the block
  instance ``DUT`` *generated*: its parts are synced into ``NAME_block.kicad_sch``, the ports
  become hierarchical labels, and part values equal to a parameter become ``${param}``.
- **References follow the design**: ``annotate`` writes the design's reference for every
  instance (R5 is CH_B/RC1/r), so KiCad's netlist and the SKiDL netlist compare 1:1.
- **Checked** against the design: connectivity, and value and footprint per part. A value
  that differs between instances of one sheet file without a parameter carrying it is an
  error that names the fix.
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from . import env, hierarchy, netlist
from .schematic import Schematic, same, snap, text_width
from .sexpr import Sym, dumps, find, find_all, head, loads

SETTINGS = ".kat.json"          # <root stem>.kat.json: {"sheets": [...], "design": "..."}


# --- settings -----------------------------------------------------------------------------

def settings_file(root: Path) -> Path:
    return root.with_name(root.stem + SETTINGS)


def remembered_sheets(root: Path) -> list[str] | None:
    f = settings_file(root)
    return json.loads(f.read_text()).get("sheets") if f.exists() else None


def remember(root: Path, **values) -> None:
    """Merge ``values`` into the sheet's ``.kat.json``."""
    f = settings_file(root)
    data = json.loads(f.read_text()) if f.exists() else {}
    f.write_text(json.dumps(data | values, indent=2) + "\n")


def remembered_design(root: Path) -> Path | None:
    """The design netlist the sheet was last synced from, if it still exists."""
    f = settings_file(root)
    rel = json.loads(f.read_text()).get("design") if f.exists() else None
    p = (root.parent / rel).resolve() if rel else None
    return p if p and p.exists() else None


# --- the design, cut into scopes ----------------------------------------------------------

class Design:
    def __init__(self, path: Path, sheets: set[str]):
        self.path = Path(path)
        self.tree = loads(self.path.read_text(encoding="utf-8"))
        self.comps, self.nets = netlist.read_design(self.path)
        rec_file = netlist.blocks_file(self.path)
        if not rec_file.exists():
            raise env.KatError(f"{rec_file.name} is missing: end the SKiDL script with "
                               f"kat.skidl_blocks.write({self.path.name!r})")
        self.rec = json.loads(rec_file.read_text())["instances"]
        from .syncer import read_no_connect
        self.no_connect = read_no_connect(self.path)
        unknown = sorted(sheets - {i["type"] for i in self.rec.values()})
        if unknown:
            raise env.KatError(f"no block instances of type {', '.join(unknown)} in the design")
        self.sheets = sheets
        self.sheet_paths = sorted(p for p, i in self.rec.items() if i["type"] in sheets)
        from .syncer import _clean
        self.net_of = {pin: _clean(n) for n, pins in self.nets.items() for pin in pins}

    def owner(self, block: str, within: str) -> str:
        """The sheet instance directly below ``within`` that holds block path ``block``
        (``within`` itself if none)."""
        parts = block.split("/") if block else []
        base = within.split("/") if within else []
        for i in range(len(base) + 1, len(parts) + 1):
            p = "/".join(parts[:i])
            if p in self.sheet_paths:
                return p
        return within

    def children(self, within: str) -> list[str]:
        """Sheet instances directly below ``within`` ("" = the root)."""
        out = []
        for p in self.sheet_paths:
            if within and not p.startswith(within + "/"):
                continue
            if self.owner(p, within) == p:
                out.append(p)
        return out

    def scope(self, within: str) -> list:
        """Design parts drawn on the sheet of ``within`` (not inside a child sheet)."""
        return [c for c in self.comps.values()
                if (within == "" or c.block == within or c.block.startswith(within + "/"))
                and self.owner(c.block, within) == within]

    def reduced(self, parts: list, rename: dict[str, str], out: Path) -> Path:
        """A netlist with only ``parts`` and their pins; nets renamed by ``rename``."""
        refs = {c.ref for c in parts}
        t = copy.deepcopy(self.tree)
        comps = find(t, "components")
        comps[1:] = [c for c in comps[1:] if isinstance(c, list) and str(find(c, "ref")[1]) in refs]
        nets = find(t, "nets")
        keep = []
        for n in nets[1:]:
            if not isinstance(n, list):
                continue
            nodes = [x for x in find_all(n, "node") if str(find(x, "ref")[1]) in refs]
            if not nodes:
                continue
            name = find(n, "name")
            from .syncer import _clean
            clean = _clean(str(name[1]))
            if clean in rename:
                name[1] = rename[clean]
            n[:] = [x for x in n if head(x) != "node"] + nodes
            keep.append(n)
        nets[1:] = keep
        out.write_text(dumps(t) + "\n", encoding="utf-8")
        return out


# --- library ------------------------------------------------------------------------------

def block_sheet(kind: str, near: Path) -> Path:
    """The library's drawing of block type ``kind``: ``NAME_block.kicad_sch``."""
    from . import blocks
    lib = blocks.library(near)
    for d in sorted(p for p in lib.iterdir() if p.is_dir()):
        info = d / f"{d.name}.block.json"
        if info.exists() and json.loads(info.read_text()).get("type") == kind:
            f = d / f"{d.name}_block.kicad_sch"
            if not f.exists():
                raise env.KatError(f"block {d.name} has no sheet yet: kat block build {d.name}")
            return f
    raise env.KatError(f"no block of type {kind!r} in {lib} (kat block new NAME, with def {kind}(...))")


# --- drawing one sheet --------------------------------------------------------------------

def _without_sheets(s: Schematic):
    """Take the sheet boxes out of the tree (their files are not beside a temporary copy)
    and return a function that puts them back."""
    boxes = [(i, n) for i, n in enumerate(s.tree) if head(n) == "sheet"]
    for _, n in boxes:
        s.tree.remove(n)

    def back():
        for i, n in boxes:
            s.tree.insert(min(i, len(s.tree)), n)
    return back


def _flat(s: Schematic, design: Design, parts: list, rename: dict[str, str], power_flags: bool) -> dict:
    """The flat part of one sheet: the usual sync with a netlist cut to this sheet."""
    from .syncer import Syncer
    back = _without_sheets(s)
    try:
        with tempfile.TemporaryDirectory() as d:
            net = design.reduced(parts, rename, Path(d) / "scope.net")
            sync = Syncer(s, net, design.no_connect)
            sync.sync_parts()
            sync.reconcile()
            sync.no_connects()
            if power_flags:
                sync.power_flags()
            s.fix_junctions()
    finally:
        back()
    return sync.report


def _boxes(s: Schematic, design: Design, within: str, rename: dict[str, str], near: Path,
           report: dict, generate: dict | None = None) -> None:
    """Sheet boxes for the child instances of ``within`` on sheet ``s``, their pins wired
    to the nets by labels; boxes of instances that are gone are removed."""
    from .syncer import _clean
    want = {p.rsplit("/", 1)[-1]: p for p in design.children(within)}
    for sh in hierarchy.sheets(s):
        if sh["name"] not in want:
            _remove_box_wiring(s, sh)
            s.tree.remove(sh["node"])
            report["removed"].append(f"sheet {sh['name']}")
    have = {sh["name"]: sh for sh in hierarchy.sheets(s)}
    labelled: set[str] = set()
    relabelled: dict[str, str] = {}
    row: list = []                     # boxes placed in this run (see _next_spot)
    for name, path in sorted(want.items()):
        inst = design.rec[path]
        child = (generate or {}).get(path) or block_sheet(inst["type"], near)
        ports = hierarchy.ports(Schematic(child))
        rel = str(Path(_relpath(child, s.path.parent)))
        left, right = hierarchy.pin_sides(ports)
        fields = {k: str(v) for k, v in inst["params"].items()}
        old = have.get(name)
        if old is not None:
            # in place, so the box keeps its uuid (board paths), position, size and pin order
            changes = []
            if old["file"] != rel:
                next(p for p in find_all(old["node"], "property") if str(p[1]) == "Sheetfile")[2] = rel
                changes.append("file")
            gone = [p for p in old["pins"] if p not in ports]
            added = {p: (v["shape"], "left" if p in left else "right") for p, v in ports.items()
                     if p not in old["pins"]}
            if gone or added:
                hierarchy.set_box(s, name, add=added, remove=gone)
                changes.append("pins")
            for pn in find_all(old["node"], "pin"):
                if str(pn[1]) in ports and str(pn[2]) != ports[str(pn[1])]["shape"]:
                    pn[2] = Sym(ports[str(pn[1])]["shape"])
                    changes.append("pin shapes")
            if old["fields"] != fields:
                _set_box_fields(old["node"], fields)
                changes.append("fields")
            if changes:
                report["updated"].append(f"sheet {name} {', '.join(dict.fromkeys(changes))}")
        else:
            nets = {p: _clean(n) if isinstance(n, str) else "" for p, n in inst["ports"].items()}
            at, size, bottom = _next_spot(s, row, left, right, nets, fields)
            hierarchy.add_sheet(s, name, child, at[0], at[1], left=left, right=right, fields=fields)
            texts = [f"File: {rel}"] + [f"{k}: {v}" for k, v in fields.items()]
            reach = max(size[0] + max([text_width(nets.get(p, "")) + 5.08 for p in right] or [0]),
                        max(text_width(x) for x in texts))
            row.append((at[0] + reach, at[1], bottom))
            report["added"].append(f"sheet {name}")
        box = next(sh for sh in hierarchy.sheets(s) if sh["name"] == name)
        for pin, v in box["pins"].items():
            net = inst["ports"].get(pin)
            if net is None:
                raise env.KatError(f"{child.name} has port {pin!r}, but the {inst['type']} call "
                                   f"{path} has no such argument")
            if isinstance(net, list):
                raise env.KatError(f"port {pin} of {path} is a bus; buses are not supported")
            net = rename.get(_clean(net), _clean(net))
            old_name = _label_at(s, (v["x"], v["y"]), -1 if abs(v["x"] - box["at"][0]) < 1e-6 else 1, net)
            if old_name:
                relabelled[old_name] = net
            labelled.add(net)
    # a renamed net also ends on this sheet's own parts (a root connector): their labels
    # still carry the old name, which no port uses any more
    for old_name, net in sorted(relabelled.items()):
        if old_name in labelled:
            continue
        n_other = 0
        for kind in ("label", "global_label"):
            for n in find_all(s.tree, kind):
                if str(n[1]) == old_name:
                    n[1] = net
                    n_other += 1
        report["renamed"].append({"from": old_name, "to": net, "sheet": s.path.name,
                                  "labels": n_other})
    _attach_lone(s, design, within, rename, labelled, report)


def _remove_box_wiring(s: Schematic, sh: dict) -> None:
    """The stub and label ``_label_at`` drew on each pin of a sheet box that goes away."""
    doomed = set()
    for v in sh["pins"].values():
        pt = (v["x"], v["y"])
        for w, seg in zip(s.wires(), s.segments()):
            for a, b in (seg, seg[::-1]):
                if same(a, pt) and abs(a[1] - b[1]) < 1e-6 and abs(abs(a[0] - b[0]) - 2.54) < 1e-3:
                    doomed.add(id(w))
                    for n in find_all(s.tree, "label"):
                        at = find(n, "at")
                        if same((float(at[1]), float(at[2])), b):
                            doomed.add(id(n))
    s.tree[:] = [c for c in s.tree if id(c) not in doomed]


def _attach_lone(s: Schematic, design: Design, within: str, rename: dict[str, str],
                 nets: set[str], report: dict) -> None:
    """A part pin whose net reaches a sheet pin but has no other pin on this sheet got no
    drawing from the flat sync: give it its label now."""
    from .syncer import Syncer
    parts = design.scope(within)
    todo = []
    ends = {(round(a[0], 3), round(a[1], 3)) for seg in s.segments() for a in seg}
    ends |= {(round(l["x"], 3), round(l["y"], 3)) for l in s.labels()}
    for c in parts:
        for sym in s.find_symbols(c.ref) if any(x.ref == c.ref for x in s.symbols()) else []:
            for p in s.pins(sym):
                net = design.net_of.get((c.ref, p["number"]))
                net = rename.get(net, net)
                if net in nets and (round(p["x"], 3), round(p["y"], 3)) not in ends:
                    todo.append((c.ref, p["number"], net))
    if not todo:
        return
    back = _without_sheets(s)
    try:
        with tempfile.TemporaryDirectory() as d:
            sync = Syncer(s, design.reduced(parts, rename, Path(d) / "scope.net"))
            for ref, pin, net in todo:
                sync._attach(ref, pin, net)
                report["attached"].append({"pin": f"{ref}.{pin}", "net": net, "sheet": s.path.name})
    finally:
        back()


def _set_box_fields(node: list, fields: dict[str, str]) -> None:
    """Sheet box parameters set in place (added, changed, removed)."""
    at = find(node, "at")
    props = {str(p[1]): p for p in find_all(node, "property")}
    for k, p in props.items():
        if k not in ("Sheetname", "Sheetfile") and k not in fields:
            node.remove(p)
    last = max(i for i, c in enumerate(node) if head(c) == "property")
    for i, (k, v) in enumerate(fields.items()):
        if k in props:
            props[k][2] = v
        else:
            from .schematic import _sexp
            from .sexpr import num
            node.insert(last + 1, _sexp(f'(property "{k}" "{v}" (at {num(float(at[1]))} {num(float(at[2]) - 2.54 * (i + 2))} 0) (show_name yes) '
                                        f'(effects (font (size 1.27 1.27)) (justify left bottom)))'))


def _relpath(target: Path, base: Path) -> str:
    import os
    return os.path.relpath(Path(target).resolve(), Path(base).resolve())


def _next_spot(s: Schematic, row: list, left: dict, right: dict, nets: dict[str, str],
               fields: dict) -> tuple[tuple[float, float], tuple[float, float], float]:
    """Where a new box goes: right of the last box placed in this run, with room for both
    boxes' labels, wrapping to a new row at the page edge; the first one below everything
    drawn. ``row``: the boxes placed so far as (right end of box, labels and fields, top,
    bottom of the fields). Returns the corner, the box size and the bottom of its fields."""
    from .syncer import paper_size
    widest = max([text_width(p) for p in (*left, *right)] or [0])
    w = snap(max(15.24, 2 * widest + 7.62))
    h = snap(2.54 * (max(len(left), len(right), 1) + 1))
    below = h + 2.54 * (len(fields) + 2)                     # the fields under the box
    margin = max([text_width(nets.get(p, "")) for p in left] or [0]) + 7.62
    x0 = snap(max(25.4, margin + 12.7))
    if row:
        x2, top, _ = row[-1]
        x = snap(x2 + 5.08 + margin)
        if x + w <= paper_size(s)[0] - 25.4:
            return (x, top), (w, h), top + below
        y = snap(max(r[2] for r in row) + 12.7)
        return (x0, y), (w, h), y + below
    boxes = [b for sym in s.symbols() if (b := s.body_box(sym))] + [b.box() for b in s.sheet_boxes()]
    y = snap(max(b[3] for b in boxes) + 12.7) if boxes else 50.8
    return (x0, y), (w, h), y + below


def _label_at(s: Schematic, pin: tuple[float, float], side: int, net: str) -> str | None:
    """A stub and a label ``net`` on a sheet pin, unless one is there already. Returns the
    old name when an existing label was renamed."""
    end = (pin[0] + side * 2.54, pin[1])
    for l in s.labels():
        if l["kind"] == "label" and abs(l["x"] - end[0]) < 1e-3 and abs(l["y"] - pin[1]) < 1e-3:
            if l["text"] != net:
                for n in find_all(s.tree, "label"):
                    if find(n, "uuid") is not None and str(find(n, "uuid")[1]) == l["uuid"]:
                        n[1] = net
                return l["text"]
            return None
    s.add_wire([pin, end], junctions=False)
    s.add_label(net, end[0], end[1], 0 if side > 0 else 180)


def _ports_to_hier(s: Schematic, ports: dict[str, str], shapes: dict[str, str]) -> None:
    """Labels named like a port become hierarchical labels (the sheet's connection points)."""
    for n in list(find_all(s.tree, "label")):
        name = str(n[1])
        if name in ports:
            n[0] = Sym("hierarchical_label")
            n.insert(2, [Sym("shape"), Sym(shapes.get(name, "passive"))])


# --- the driver ---------------------------------------------------------------------------

def run(root: Path, design_path: Path, sheets: list[str] | None = None, dry_run: bool = False,
        generate: dict[str, Path] | None = None) -> dict:
    """Sync ``design_path`` into the hierarchy under ``root``. ``sheets``: the block types
    drawn as sheets (default: the remembered choice). ``generate``: {instance: file} of
    instances whose sheet is written here instead of taken from the library (the block's
    own build)."""
    root = Path(root)
    sheets = sheets if sheets is not None else remembered_sheets(root)
    if not sheets:
        raise env.KatError("which block types become sheets? --sheets TYPE,... (remembered afterwards)")
    generate = generate or {}
    design = Design(design_path, set(sheets))
    near = root.parent
    report = {"added": [], "removed": [], "updated": [], "renamed": [], "attached": [],
              "unresolved": [], "files": [], "parameters": {}}
    files: dict[Path, Schematic] = {}

    def draw(file: Path, within: str, rename: dict[str, str], is_root: bool) -> None:
        s = files.setdefault(file.resolve(), Schematic(file))
        rep = _flat(s, design, design.scope(within), rename, power_flags=is_root)
        for k in ("added", "removed", "updated", "renamed", "attached", "unresolved"):
            report[k] += [dict(x, sheet=file.name) if isinstance(x, dict) else f"{file.name}: {x}"
                          for x in rep.get(k, [])]
        for child in design.children(within):     # generated sheets first: their ports are the pins
            if child in generate:
                inst = design.rec[child]
                ports = {k: v for k, v in inst["ports"].items()
                         if isinstance(v, str) and v not in _power_names()}
                crename = {_clean_name(v): k for k, v in ports.items()}
                cfile = generate[child]
                if not cfile.exists():
                    Schematic.create(cfile)
                draw(cfile, child, crename, False)
                cs = files[cfile.resolve()]
                _ensure_ports(cs, design, child, ports)
                _ports_to_hier(cs, ports, _shapes(design, child, ports))
                report["parameters"].update(_parameters(cs, inst))
                if not dry_run:
                    cs.save()                     # the parent's box reads its ports from the file
        _boxes(s, design, within, rename, near, report, generate)
        report["files"].append(str(file))

    draw(root, "", {}, True)
    if dry_run:
        return dict(report, dry_run=True)
    for s in files.values():
        s.save()
    remember(root, sheets=sorted(sheets))
    refs = _design_refs(root, design)
    hierarchy.annotate(root, refs=refs)
    report["check"] = verify(root, design)
    return dict(report, dry_run=False)


def _clean_name(n: str) -> str:
    from .syncer import _clean
    return _clean(n)


def _power_names() -> set[str]:
    from .syncer import power_names
    return power_names()


def _shapes(design: Design, inst: str, ports: dict[str, str]) -> dict[str, str]:
    return {k: "passive" for k in ports}


def _ensure_ports(s: Schematic, design: Design, inst: str, ports: dict[str, str]) -> None:
    """Every port needs a label on its net inside the sheet, even when only one pin of the
    block is on it (a lone pin gets no label from the flat sync)."""
    from .syncer import Syncer
    have = {l["text"] for l in s.labels()}
    missing = [k for k in ports if k not in have]
    if not missing:
        return
    back = _without_sheets(s)
    try:
        with tempfile.TemporaryDirectory() as d:
            crename = {_clean_name(v): k for k, v in ports.items()}
            sync = Syncer(s, design.reduced(design.scope(inst), crename, Path(d) / "scope.net"))
            for k in missing:
                pins = sorted(p for p in sync.nets.get(k, ()) if p[0] in {c.ref for c in design.scope(inst)})
                if pins:
                    sync._attach(pins[0][0], pins[0][1], k)
    finally:
        back()


def _parameters(s: Schematic, inst: dict) -> dict[str, str]:
    """Part values equal to a parameter's value become ``${param}`` (the block's own
    build); returns {param: default}."""
    params = {k: str(v) for k, v in inst["params"].items()}
    by_value: dict[str, list[str]] = {}
    for k, v in params.items():
        by_value.setdefault(v, []).append(k)
    used = {}
    for sym in s.symbols():
        if sym.ref.startswith("#"):
            continue
        value = sym.field("Value") or ""
        names = by_value.get(value, [])
        if len(names) == 1:
            s.set_field(sym.ref, "Value", f"${{{names[0]}}}", unit=sym.unit)
            used[names[0]] = value
        elif len(names) > 1:
            raise env.KatError(f"{sym.ref} = {value} matches the parameters {', '.join(names)}: "
                               f"give them different default values")
    for sh in hierarchy.sheets(s):             # nested blocks: their parameters from ours
        for p in find_all(sh["node"], "property"):
            if str(p[1]) in ("Sheetname", "Sheetfile"):
                continue
            names = by_value.get(str(p[2]), [])
            if len(names) == 1:
                p[2] = f"${{{names[0]}}}"
                used[names[0]] = params[names[0]]
    return used


def _design_refs(root: Path, design: Design) -> dict[str, dict[str, str]]:
    """{instance path: {symbol uuid: design reference}} for ``annotate``."""
    by_tag = {c.tag: c.ref for c in design.comps.values() if c.tag}
    out: dict[str, dict[str, str]] = {}
    problems = []
    seen = set()
    for i in hierarchy.tree(root):
        s = Schematic(Path(i["file"]))
        base = i["path"].strip("/")
        for sym in s.symbols():
            if sym.ref.startswith("#"):
                continue
            tag = sym.field("SKiDL Tag") or ""
            block = sym.field("Block") or ""
            if i["path"] != "/":
                root_seg = block.split("/")[0]
                local = tag[len(root_seg) + 1:] if root_seg and tag.startswith(root_seg + "/") else tag
                want = f"{base}/{local}" if local else ""
            else:
                want = tag
            ref = by_tag.get(want)
            if ref is None:
                problems.append(f"{i['path']}: {sym.ref} (tag {want or '-'}) is not in the design")
                continue
            out.setdefault(i["path"], {})[sym.uuid] = ref
            seen.add(ref)
    missing = sorted(set(design.comps) - seen)
    if problems or missing:
        raise env.KatError("the hierarchy does not match the design: "
                           + "; ".join(problems[:10] + ([f"not drawn: {', '.join(missing[:20])}"] if missing else [])))
    return out


def verify(root: Path, design: Design) -> dict:
    """KiCad's netlist of the hierarchy against the design: connectivity, and value and
    footprint of every part."""
    from .pcb.syncer import read_design
    got = netlist.from_schematic(root)
    cmp = netlist.compare(got, design.nets)
    parts, _ = read_design(root)
    diffs = []
    for ref, c in design.comps.items():
        p = parts.get(ref)
        if p is None:
            diffs.append({"ref": ref, "problem": "missing"})
            continue
        for what, a, b in (("value", p.value, c.value), ("footprint", p.footprint, c.footprint)):
            if a != b:
                diffs.append({"ref": ref, "problem": what, "drawn": a, "design": b,
                              "hint": f"{c.block or 'the top'}: make it a parameter of the block"
                              if what == "value" else ""})
    return {"netlist_vs_expected": cmp, "parts": diffs, "ok": cmp["match"] and not diffs}
