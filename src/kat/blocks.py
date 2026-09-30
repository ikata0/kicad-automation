"""The block library: each block is a project of its own, designed and checked once, then
used by designs (SKiDL function) and stamped onto their boards (layout).

    blocks/RC/
      RC.py            SKiDL: the block function, and under __main__ a test harness
      RC.kicad_pro     the block's project (text_variables: parameter defaults)
      RC.kicad_sch     the harness and the block, synced from RC.net
      RC.kicad_pcb     the block's layout; the harness parts are its environment
      RC.block.json    type, instance, ports, params, environment zones, last check
      RC_REPORT.md     the block's own journal

The library is ``KAT_BLOCKS``, else the nearest ``blocks/`` folder in the given place (or
the working directory) or above it.

    kat block new RC  ->  edit RC.py  ->  kat block build RC  ->  lay out RC.kicad_pcb
    ->  kat block check RC  ->  in the design: rc = kat.blocks.use("RC"); on its board:
    b.stamp("RC", {"/CH_A/RC1": (x, y)})
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import env

__all__ = ["build", "check", "find", "library", "list_blocks", "new", "use"]

HARNESS = "DUT"

TEMPLATE = '''"""Block {name}: what it does. Ports: inp, out, gnd. Parameters: r_val."""
import os
import sys

os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool, subcircuit  # noqa: E402


@subcircuit
def {func}(inp, out, gnd, r_val="10k"):
    """The block. Every part gets a tag= (unique inside the block)."""
    r = Part("Device", "R", value=r_val, footprint="Resistor_SMD:R_0603_1608Metric", tag="r")
    inp += r[1]
    r[2] += out


if __name__ == "__main__":
    # the test harness: the block's environment on its own board (not stamped)
    import kat
    set_default_tool(KICAD10)
    inp, out, gnd = Net("IN"), Net("OUT"), Net("GND")
    j = Part("Connector_Generic", "Conn_01x03", tag="j",
             footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
    j[1] += inp
    j[2] += out
    j[3] += gnd
    {func}(inp=inp, out=out, gnd=gnd, tag="{harness}")
    net = sys.argv[1] if len(sys.argv) > 1 else "{name}.net"
    generate_netlist(file_=net)
    kat.skidl_blocks.write(net)
'''


def library(start: Path | str | None = None) -> Path:
    """The block library folder: ``KAT_BLOCKS``, else ``blocks/`` in ``start`` (default:
    the working directory) or its parents. KatError if there is none."""
    if os.environ.get("KAT_BLOCKS"):
        return Path(os.environ["KAT_BLOCKS"])
    here = Path(start or Path.cwd()).resolve()
    here = here if here.is_dir() else here.parent
    for d in (here, *here.parents):
        if d.name == "blocks" and d.is_dir():
            return d
        if (d / "blocks").is_dir():
            return d / "blocks"
    raise env.KatError(f"no blocks/ folder in {here} or above (or set KAT_BLOCKS)")


def find(name: str, lib: Path | str | None = None) -> Path:
    """The folder of block ``name``; KatError if it does not exist."""
    d = (Path(lib) if lib else library()) / name
    if not (d / f"{name}.py").exists():
        raise env.KatError(f"no block {name!r} in {d.parent} (kat block new {name})")
    return d


def _files(d: Path) -> dict[str, Path]:
    n = d.name
    return {k: d / f"{n}{ext}" for k, ext in (
        ("py", ".py"), ("sch", ".kicad_sch"), ("pcb", ".kicad_pcb"), ("net", ".net"),
        ("info", ".block.json"), ("block", "_block.kicad_sch"), ("flat", "_flat.kicad_sch"))}


def _info(d: Path) -> dict:
    f = _files(d)["info"]
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _write_info(d: Path, info: dict) -> None:
    _files(d)["info"].write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


def new(name: str, lib: Path | str | None = None) -> dict:
    """Create block ``name`` in the library (``blocks/`` in the working directory if there
    is no library yet): its project, a SKiDL template with a test harness and the block
    file. Refuses an existing block."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
        raise env.KatError("a block name is a Python identifier (letters, digits, _)")
    try:
        root = Path(lib) if lib else library()
    except env.KatError:
        root = Path.cwd() / "blocks"
    d = root / name
    f = _files(d)
    if f["py"].exists():
        raise env.KatError(f"block {name} exists in {root}")
    from . import project
    project.new(f["sch"])
    f["py"].write_text(TEMPLATE.format(name=name, func=name.lower(), harness=HARNESS),
                       encoding="utf-8")
    _write_info(d, {"name": name, "type": name.lower(), "instance": HARNESS, "environment": []})
    return {"block": name, "dir": str(d), "files": {k: str(v) for k, v in f.items() if v.exists()},
            "next": f"edit {f['py'].name}, then kat block build {name}"}


def build(name: str, lib: Path | str | None = None, force: bool = False) -> dict:
    """Run the block's harness (``NAME.py`` -> ``NAME.net`` + block record), sync the
    schematic and the board, and store type, ports and parameters in the block file.
    ``force`` passes the board sync gate (e.g. parts without MPN while developing)."""
    import kat
    d = find(name, lib)
    f = _files(d)
    proc = subprocess.run([sys.executable, str(f["py"]), str(f["net"])], cwd=d,
                          capture_output=True, text=True)
    if proc.returncode != 0 or not f["net"].exists():
        raise env.KatError(f"{f['py'].name} failed: {(proc.stderr or proc.stdout).strip()[-1500:]}")
    from .netlist import blocks_file
    rec = json.loads(blocks_file(f["net"]).read_text(encoding="utf-8"))
    info = _info(d)
    inst = info.get("instance", HARNESS)
    if inst not in rec["instances"]:
        raise env.KatError(f"the harness in {f['py'].name} has no block instance tag={inst!r} "
                           f"(found: {sorted(rec['instances']) or 'none'})")
    me = rec["instances"][inst]
    from .syncer import power_names
    power = power_names()
    info.update(type=me["type"], params=me["params"],
                ports=sorted(k for k, v in me["ports"].items() if not (isinstance(v, str) and v in power)),
                power=sorted(k for k, v in me["ports"].items() if isinstance(v, str) and v in power))
    _write_info(d, info)
    from . import syncer_hier
    # blocks used inside this one that the library has are sheets too (nested blocks)
    known = {json.loads(p.read_text(encoding="utf-8")).get("type") for p in d.parent.glob("*/*.block.json")
             if p.parent != d}
    inner = {i["type"] for p, i in rec["instances"].items() if p.startswith(inst + "/")} & known
    sch = syncer_hier.run(f["sch"], f["net"], sorted({me["type"]} | inner), generate={inst: f["block"]})
    if sch["parameters"]:                 # the defaults, when the block is opened on its own
        pro = f["sch"].with_suffix(".kicad_pro")
        data = json.loads(pro.read_text(encoding="utf-8"))
        data["text_variables"] = {**data.get("text_variables", {}), **sch["parameters"]}
        pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    flat = _build_flat(d, rec)
    pcb = kat.pcb.sync(f["pcb"], force=force)
    return {"block": name, "type": me["type"], "ports": info["ports"], "power": info["power"],
            "params": info["params"],
            "warnings": rec["warnings"], **({"flat": flat} if flat else {}),
            "schematic": {k: sch.get(k) for k in ("added", "removed", "updated", "parameters")} | {
                "ok": (sch.get("check") or {}).get("ok")},
            "board": {k: pcb.get(k) for k in ("added", "removed", "updated", "check")}}


def _children(d: Path, rec: dict) -> dict[str, list[str]]:
    """The inner block instances drawn as sheets in the block: {library block: [paths]}."""
    from .schematic import Schematic
    f = _files(d)
    inst = _info(d).get("instance", HARNESS)
    names = {b.ref for b in Schematic(f["block"]).sheet_boxes()}
    by_type = {json.loads(p.read_text(encoding="utf-8")).get("type"): p.parent.name
               for p in d.parent.glob("*/*.block.json")}
    out: dict[str, list[str]] = {}
    for n in sorted(names):
        path = f"{inst}/{n}"
        out.setdefault(by_type[rec["instances"][path]["type"]], []).append("/" + path)
    return out


def content_hash(path: Path) -> str:
    """Hash of a file's content. For a schematic, KiCad's per-project instance data is
    left out (``(instances ...)`` and each placed symbol's Reference, which every project
    using the sheet rewrites on annotation): a design annotating a library sheet does not
    change the block."""
    import hashlib
    path = Path(path)
    if path.suffix != ".kicad_sch":
        return hashlib.sha1(path.read_bytes()).hexdigest()
    from .sexpr import dumps, find, head, loads

    def strip(node):
        out = [strip(c) if isinstance(c, list) else c for c in node
               if not (isinstance(c, list) and head(c) in ("instances", "sheet_instances", "symbol_instances"))]
        if head(node) == "symbol" and find(node, "lib_id") is not None:
            out = [[*c[:2], "", *c[3:]] if isinstance(c, list) and head(c) == "property"
                   and len(c) > 2 and str(c[1]) == "Reference" else c for c in out]
        return out
    return hashlib.sha1(dumps(strip(loads(path.read_text(encoding="utf-8")))).encode()).hexdigest()


def _child_hashes(d: Path, children: dict) -> dict[str, str]:
    return {c: content_hash(sheet_of(c, near=d)[0]) for c in children}


def _build_flat(d: Path, rec: dict) -> dict | None:
    """``NAME_flat.kicad_sch``: the whole block on one sheet, for flat stamps of a block
    that contains sheets. The first time, each inner instance is stamped from its own
    block where its box is in the block's drawing (spread out for the bigger drawing) and
    the block's own parts go where they are there; later builds keep the arrangement,
    sync the changes and re-stamp only instances whose block drawing changed."""
    import kat
    from . import hierarchy, sch_stamp
    from .schematic import Schematic, snap
    f = _files(d)
    if not f["block"].exists() or not _has_sheets(f["block"]):
        return None
    info = _info(d)
    inst = info.get("instance", HARNESS)
    children = _children(d, rec)
    times = _child_hashes(d, children)
    first = not f["flat"].exists()
    warnings: list[str] = []
    if first:
        Schematic.create(f["flat"])
    sync = kat.sync(f["flat"], f["net"])
    restamp = list(children) if first else [c for c in children if times[c] != info.get("flat", {}).get(
        "children", {}).get(c)]
    if first:
        s = kat.load(f["flat"]).schematic
        block = Schematic(f["block"])
        boxes = {b["name"]: b for b in hierarchy.sheets(block)}
        rooms = {}
        for c, paths in children.items():
            for p in paths:
                size, off, _ = sch_stamp.room_of(s, c, p)
                rooms[p] = (boxes[p.rsplit("/", 1)[1]], size, off)
        extra = [(b["at"], b["size"], size) for b, size, _ in rooms.values()]

        def spread(x: float, y: float) -> tuple[float, float]:
            ex = sum(max(sz[0] - bs[0], 0) for at, bs, sz in extra if at[0] + bs[0] <= x + 1e-6)
            ey = sum(max(sz[1] - bs[1], 0) for at, bs, sz in extra if at[1] + bs[1] <= y + 1e-6)
            return x + ex, y + ey

        spots = {p: spread(*b["at"]) for p, (b, _, _) in rooms.items()}
        own = {x.field("SKiDL Tag"): x for x in block.symbols() if (x.field("Block") or "") == inst}
        moves = []
        for x in s.symbols():
            src = own.get(x.field("SKiDL Tag") or "")
            if src is not None and (x.field("Block") or "") == inst:
                moves.append({"ref": x.ref, "unit": x.unit, "to": spread(src.place.x, src.place.y),
                              "rot": src.place.rot, "mirror": src.place.mirror or ""})
        # everything to the page's upper left, below the frame headings
        pts = list(spots.values()) + [m["to"] for m in moves]
        sx, sy = 38.1 - min(p[0] for p in pts), 38.1 - min(p[1] for p in pts)
        # the paper the spread drawing needs (plus a column for the harness on the right)
        ends = [(spots[p][0] + sx + size[0], spots[p][1] + sy + size[1]) for p, (_, size, _) in rooms.items()]
        ends += [(m["to"][0] + sx + 25.4, m["to"][1] + sy + 25.4) for m in moves]
        right, bottom = max(e[0] for e in ends), max(e[1] for e in ends)
        paper = _paper_for(right + 76.2, bottom + 38.1)
        harness = [x for x in s.symbols() if not x.ref.startswith("#")
                   and (x.field("Block") or "") != inst and not (x.field("Block") or "").startswith(inst + "/")]
        with kat.edit(f["flat"]) as sh:
            sh.page(paper)
            if moves:
                try:
                    sh.move_many([dict(m, to=(m["to"][0] + sx, m["to"][1] + sy)) for m in moves])
                except env.KatError as e:          # stays where sync put it; make_room decides
                    warnings.append(f"the block's own parts stay where sync put them: {e}")
            if harness:                            # the harness to the right of the drawing
                try:
                    sh.move_many([{"ref": x.ref, "unit": x.unit, "to": (snap(right + 25.4), snap(38.1 + 25.4 * k))}
                                  for k, x in enumerate(harness)])
                except env.KatError as e:
                    warnings.append(f"the harness stays where sync put it: {e}")
        for c, paths in children.items():
            at = [(spots[p][0] + sx + rooms[p][2][0], spots[p][1] + sy + rooms[p][2][1]) for p in paths]
            kat.stamp(f["flat"], c, paths, at=at, design=f["net"], make_room=True)
    else:
        for c in restamp:
            kat.stamp(f["flat"], c, children[c], design=f["net"], make_room=True)
    check = kat.check(f["flat"], f["net"])
    info = _info(d)
    info["flat"] = {"file": f["flat"].name, "children": times}
    _write_info(d, info)
    return {"file": f["flat"].name, "created": first, "restamped": restamp if not first else [],
            "synced": {k: len(sync.get(k) or []) for k in ("added", "removed", "updated")},
            "ok": check["netlist_vs_expected"]["match"] and check["erc"]["errors"] == 0,
            "off_page": len(check["off_page"]), **({"warnings": warnings} if warnings else {})}


def _paper_for(w: float, h: float) -> str:
    """The smallest ISO paper (landscape) that holds ``w`` x ``h`` mm, A0 at most."""
    from .syncer import PAPER
    for name in ("A4", "A3", "A2", "A1", "A0"):
        pw, ph = PAPER[name]
        if w <= pw and h <= ph:
            return name
    return "A0"


def _env_refs(d: Path) -> set[str]:
    from .pcb.board import Board
    inst = _info(d).get("instance", HARNESS)
    out = set()
    for fp in Board(_files(d)["pcb"]).footprints():
        block = fp.field("Block") or ""
        if not fp.board_only and not (block == inst or block.startswith(inst + "/")):
            out.add(fp.ref)
    return out


def check(name: str, lib: Path | str | None = None, require: tuple[str, ...] = ("MPN",)) -> dict:
    """Is the block ready to be stamped? The schematic check against the harness netlist,
    the gate's part data, and the board check (DRC, parity). Missing connections to the
    harness (environment) parts are expected and not counted. The result and the files'
    times are stored in the block file; ``stamp`` warns when the block changed since."""
    import kat
    from . import gate
    d = find(name, lib)
    f = _files(d)
    for k in ("sch", "pcb", "net"):
        if not f[k].exists():
            raise env.KatError(f"{f[k].name} is missing: kat block build {name}")
    sch = kat.check(f["sch"], f["net"])
    g = gate.run(f["sch"], f["net"], require)
    board = kat.pcb.check(f["pcb"])
    envs = _env_refs(d)

    def to_env(item) -> bool:
        return any((m := re.search(r" of (\S+)", i["description"])) and m.group(1) in envs
                   for i in item["items"])

    open_ = [u for u in board["unconnected_items"] if not to_env(u)]
    # the harness is test equipment: its parts need no part data (MPN)
    g["errors"] = [e for e in g.get("errors", []) if e.get("ref") not in envs]
    g["ready"] = not g["errors"]
    ports = _check_ports(d, f["pcb"])
    ok = bool(sch["ok"] and g["ready"] and board["drc_errors"] == 0 and board["parity"] == 0
              and not open_ and board["outline"] and not ports)
    info = _info(d)
    flat, warnings = None, []
    if f["block"].exists() and _has_sheets(f["block"]):
        if not f["flat"].exists():
            raise env.KatError(f"{f['flat'].name} is missing: kat block build {name}")
        c = kat.check(f["flat"], f["net"])
        flat = {"ok": bool(c["netlist_vs_expected"]["match"] and c["erc"]["errors"] == 0),
                "erc_errors": c["erc"]["errors"], "clean": c["clean"]}
        ok = ok and flat["ok"]
        from .netlist import blocks_file
        rec = json.loads(blocks_file(f["net"]).read_text(encoding="utf-8"))
        seen = info.get("flat", {}).get("children", {})
        newer = [c for c, h in _child_hashes(d, _children(d, rec)).items() if h != seen.get(c)]
        if newer:
            warnings.append(f"{', '.join(newer)} changed after the flat drawing was made: kat block build {name}")
    keys = tuple(k for k in ("py", "sch", "block", "pcb") if f[k].exists()) + (("flat",) if flat else ())
    info["checked"] = {"ok": ok, "time": dt.datetime.now().isoformat(timespec="seconds"),
                       "hashes": {k: content_hash(f[k]) for k in keys}}
    _write_info(d, info)
    return {"block": name, "ok": ok, "schematic_ok": sch["ok"], **({"flat": flat} if flat else {}),
            **({"warnings": warnings} if warnings else {}), "gate": g["ready"],
            "gate_errors": g.get("errors", []), "drc_errors": board["drc_errors"],
            "drc_warnings": board["drc_warnings"], "parity": board["parity"],
            "unconnected": len(open_), "unconnected_to_environment": len(board["unconnected_items"]) - len(open_),
            "environment_parts": sorted(envs), "unconnected_items": open_,
            **({"port_problems": ports} if ports else {})}


PORT_BAND = 2.5     # mm: a port point lies at most this far inside the outline's edge


def _check_ports(d: Path, pcb: Path) -> list[str]:
    """Problems of the block's port points (``kat.pcb.block_port``)."""
    from .pcb.board import Board
    from .pcb.geometry import inside
    info = _info(d)
    pts = info.get("port_points", [])
    if not pts:
        return []
    b = Board(pcb)
    box = b.outline_box()
    names = set(info.get("ports", [])) | set(info.get("power", []))
    out = []
    for q in pts:
        x, y = q["x"], q["y"]
        on = next((v["net"] for v in b.vias() if abs(v["at"][0] - x) < 0.01 and abs(v["at"][1] - y) < 0.01), None)
        if on is None:
            on = next((p.net for p in b.pads() if inside(p.outline, (x, y))), None)
        if names and q["port"] not in names:
            out.append(f"port {q['port']}: the block has no such port ({', '.join(sorted(names))})")
        if on is None:
            out.append(f"port {q['port']}: no via or pad at {x:g},{y:g} any more")
        elif on != q["net"]:
            out.append(f"port {q['port']}: the copper at {x:g},{y:g} is on {on}, not {q['net']}")
        if box and not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
            out.append(f"port {q['port']}: {x:g},{y:g} is outside the outline")
        elif box and min(x - box[0], box[2] - x, y - box[1], box[3] - y) > PORT_BAND:
            out.append(f"port {q['port']}: {x:g},{y:g} is more than {PORT_BAND:g} mm inside the outline's edge")
    return out


def stale(d: Path) -> str | None:
    """Why block folder ``d`` is not known to be clean, or None."""
    info = _info(d)
    c = info.get("checked")
    if not c:
        return "never checked (kat block check)"
    if not c["ok"]:
        return "its last check failed"
    f = _files(d)
    if "hashes" not in c:
        return "checked by an older kat (kat block check)"
    changed = [k for k in ("py", "sch", "block", "pcb", "flat") if f[k].exists() and content_hash(f[k]) != c["hashes"].get(k)]
    return f"changed since its last clean check ({', '.join(changed)})" if changed else None


def list_blocks(lib: Path | str | None = None) -> dict:
    """The blocks of the library: type, ports, parameters and check state."""
    root = Path(lib) if lib else library()
    out = []
    for d in sorted(p for p in root.iterdir() if (p / f"{p.name}.py").exists()):
        info = _info(d)
        out.append({"block": d.name, "type": info.get("type"), "ports": info.get("ports"),
                    "params": info.get("params"), "checked": (info.get("checked") or {}).get("time"),
                    "state": stale(d) or "clean"})
    return {"library": str(root), "blocks": out}


def use(name: str, lib: Path | str | None = None):
    """The block's SKiDL function, for a design script: ``rc = kat.blocks.use("RC")``,
    then ``rc(inp=a, out=b, gnd=gnd, tag="CH_A")``. The harness does not run."""
    d = find(name, lib)
    info = _info(d)
    spec = importlib.util.spec_from_file_location(f"kat_block_{name}", _files(d)["py"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    func = getattr(mod, info.get("type") or name.lower(), None)
    if func is None:
        raise env.KatError(f"{name}.py has no function {info.get('type') or name.lower()!r}")
    return func


def sheet_of(name_or_path: Path | str, near: Path | str | None = None) -> tuple[Path, str | None]:
    """A block schematic from a block name (looked up from ``near``) or a path, and the
    block instance on it (from the block file)."""
    p = Path(name_or_path)
    if p.suffix == ".kicad_sch" or p.exists():
        info = p.with_suffix(".block.json")
        return p, (json.loads(info.read_text(encoding="utf-8")).get("instance") if info.exists() else None)
    d = find(str(name_or_path), library(near))
    f = _files(d)
    inst = _info(d).get("instance", HARNESS)
    if f["block"].exists() and _has_sheets(f["block"]):
        if not f["flat"].exists():
            raise env.KatError(f"block {d.name} contains sheets and has no flat drawing yet: "
                               f"kat block build {d.name}")
        return f["flat"], inst
    return (f["block"] if f["block"].exists() else f["sch"]), inst


def _has_sheets(sch: Path) -> bool:
    from .schematic import Schematic
    return bool(Schematic(sch).sheet_boxes())


def board_of(name_or_path: Path | str, near: Path | str | None = None) -> tuple[Path, str | None]:
    """A block board from a block name (looked up from ``near``) or a path, and the block
    instance on it (from the block file)."""
    p = Path(name_or_path)
    if p.suffix == ".kicad_pcb" or p.exists():
        info = json.loads(p.with_suffix(".block.json").read_text(encoding="utf-8")) if p.with_suffix(".block.json").exists() else {}
        return p, info.get("instance")
    d = find(str(name_or_path), library(near))
    return _files(d)["pcb"], _info(d).get("instance", HARNESS)
