"""``kat`` command line. Every command prints one JSON document to stdout.

Mutating commands load the schematic, apply the change, and save it through
``kicad-cli sch upgrade`` so the file is validated and in KiCad's own formatting.
``kat batch`` applies many commands with a single load/save.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shlex
import shutil
import sys
from pathlib import Path

from . import check as check_mod
from . import env, library, netlist, render, sync
from .schematic import Placement, Schematic
from .sexpr import find_all

MUTATING = {"add", "wire", "connect", "label", "power", "nc", "move", "set", "rm", "detach"}


# --- argument helpers ---------------------------------------------------------

def xy(text: str) -> tuple[float, float]:
    try:
        x, y = (float(v) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected X,Y in mm, got {text!r}")
    return x, y


def point(s: Schematic, text: str) -> tuple[float, float]:
    """``REF.PIN`` (pin number or unique name) or ``X,Y``."""
    if "," in text:
        return xy(text)
    if "." not in text:
        raise env.KatError(f"expected REF.PIN or X,Y, got {text!r}")
    ref, pin = text.split(".", 1)
    return s.pin(ref, pin)


def sym_info(s: Schematic, sym) -> dict:
    return {"ref": sym.ref, "lib_id": sym.lib_id, "unit": sym.unit,
            "x": sym.place.x, "y": sym.place.y, "rot": sym.place.rot, "mirror": sym.place.mirror,
            "value": sym.field("Value"), "footprint": sym.field("Footprint"),
            "body": [round(v, 2) for v in (s.body_box(sym) or ())],
            "pins": s.pins(sym)}


# --- commands on a loaded schematic --------------------------------------------

def cmd_add(s, a):
    sym = s.add_symbol(a.lib_id, a.x, a.y, a.rot, a.mirror or "", a.unit, a.ref, a.value, a.footprint)
    return sym_info(s, sym)


def cmd_wire(s, a):
    pts = [point(s, p) for p in a.points]
    return {"wires": len(s.add_wire(pts)), "points": pts}


def cmd_connect(s, a):
    p, q = point(s, a.a), point(s, a.b)
    via = [xy(v) for v in a.via or []]
    return {"wires": len(s.route(p, q, via, a.first)), "from": p, "to": q}


def _stub(s, at: tuple[float, float], stub: tuple[float, float] | None) -> tuple[float, float]:
    if not stub:
        return at
    end = (at[0] + stub[0], at[1] + stub[1])
    s.add_wire([at, end])
    return end


def cmd_label(s, a):
    end = _stub(s, point(s, a.at), a.stub)
    s.add_label(a.name, end[0], end[1], a.angle, "global_label" if a.glob else "label", a.shape)
    s.fix_junctions()
    return {"label": a.name, "x": end[0], "y": end[1]}


def cmd_power(s, a):
    """Power symbol (e.g. GND, +3V3) whose pin sits at the target (after an optional stub)."""
    end = _stub(s, point(s, a.at), a.stub)
    lib_id = a.name if ":" in a.name else f"power:{a.name}"
    sdef = s.ensure_lib_symbol(lib_id)
    pin = library.pins(sdef)[0]
    off = Placement(0, 0, a.rot).to_sheet(pin.x, pin.y)
    sym = s.add_symbol(lib_id, end[0] - off[0], end[1] - off[1], a.rot)
    s.place_power_text(sym, sync._power_dir(s, sym))
    s.fix_junctions()
    return {"ref": sym.ref, "lib_id": lib_id, "x": end[0], "y": end[1]}


def cmd_nc(s, a):
    p = point(s, a.at)
    s.add_no_connect(*p)
    return {"no_connect": p}


def cmd_move(s, a):
    to = a.to or (None, None)
    by = a.by or (0, 0)
    s.move_symbol(a.ref, to[0], to[1], by[0], by[1], a.rot, a.mirror, a.unit, drag=not a.no_drag)
    s.fix_junctions()
    return [sym_info(s, x) for x in s.find_symbols(a.ref, a.unit)]


def cmd_set(s, a):
    hide = True if a.hide else False if a.show else None
    s.set_field(a.ref, a.field, a.value, a.at, a.angle, hide, a.unit)
    if a.autoplace:
        for sym in s.find_symbols(a.ref, a.unit):
            s.autoplace_fields(sym)
    return {"ref": a.value if a.field == "Reference" and a.value else a.ref, "field": a.field}


def cmd_detach(s, a):
    if "." not in a.pin:
        raise env.KatError(f"expected REF.PIN, got {a.pin!r}")
    return s.detach(*a.pin.split(".", 1))


def cmd_rm(s, a):
    if a.uuid:
        n = s.remove_by_uuid(a.uuid)
    elif a.wires_at:
        n = s.remove_wires_at(*point(s, a.wires_at))
    elif a.ref:
        n = s.remove_symbol(a.ref, a.unit)
    else:
        raise env.KatError("rm needs REF, --uuid or --wires-at")
    s.fix_junctions()
    return {"removed": n}


# --- read-only / file-level commands -------------------------------------------

def cmd_new(a):
    target = Path(a.path)
    d, name = (target, target.name) if target.suffix == "" else (target.parent, target.stem)
    d.mkdir(parents=True, exist_ok=True)
    pro, sch = d / f"{name}.kicad_pro", d / f"{name}.kicad_sch"
    if sch.exists() and not a.force:
        raise env.KatError(f"{sch} exists (use --force to overwrite)")
    tmpl = (env.stock_symbol_dir() or Path("/nonexistent")).parent / "template" / "kicad.kicad_pro"
    if tmpl.exists():
        shutil.copy(tmpl, pro)
    elif not pro.exists():
        pro.write_text('{"meta": {"filename": "%s.kicad_pro", "version": 3}}\n' % name)
    Schematic.create(sch, a.paper)
    return {"project": str(pro), "schematic": str(sch)}


def cmd_list(a):
    s = Schematic(a.sch)
    syms = [{k: v for k, v in sym_info(s, x).items() if k != "pins"} for x in s.symbols()]
    return {"symbols": syms, "labels": s.labels(),
            "wires": [[list(p), list(q)] for p, q in s.segments()],
            "junctions": len(find_all(s.tree, "junction"))}


def cmd_pins(a):
    s = Schematic(a.sch)
    return [sym_info(s, x) for x in s.find_symbols(a.ref, a.unit)]


def cmd_lib(a):
    if a.lib_cmd == "search":
        return library.search(" ".join(a.query), a.limit)
    sdef = library.resolve(a.lib_id)
    props = {str(p[1]): str(p[2]) for p in find_all(sdef, "property")}
    units = library.unit_count(sdef)
    return {"lib_id": a.lib_id, "units": units, "properties": props,
            "body": {u: library.outline(sdef, u) for u in range(1, units + 1)},
            "pins": [vars(p) for p in library.pins(sdef)]}


def cmd_netlist(a):
    nets = netlist.from_schematic(Path(a.sch))
    return {name: sorted(pins) for name, pins in sorted(nets.items())}


def cmd_check(a):
    return check_mod.run(Path(a.sch), Path(a.expect) if a.expect else None)


def cmd_snapshot(a):
    sch = Path(a.sch)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = ("_" + "".join(c if c.isalnum() or c in "-_" else "-" for c in a.slug)) if a.slug else ""
    out = Path(a.out or sch.parent / "history") / f"{stamp}{slug}"
    result = render.snapshot(sch, out, a.zoom)
    shutil.copy(sch, out / sch.name)
    res = check_mod.run(sch, Path(a.expect) if a.expect else None)
    (out / "check.json").write_text(json.dumps(res, indent=1))
    if a.note:
        (out / "note.md").write_text(a.note + "\n")
    result.update({"dir": str(out), "ok": res["ok"], "erc_errors": res["erc"]["errors"],
                   "erc_warnings": res["erc"]["warnings"], "overlaps": len(res["overlaps"])})
    return result


def cmd_sync(a):
    return sync.run(Path(a.sch), Path(a.netlist), a.dry_run)


def cmd_batch(a):
    """Apply one mutating command per line (without the schematic argument); save once.
    Blank lines and lines starting with # are skipped. Stops at the first error without saving."""
    s = Schematic(a.sch)
    src = sys.stdin if a.script == "-" else open(a.script, encoding="utf-8")
    results = []
    for n, line in enumerate(src, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        argv = shlex.split(line)
        if argv[0] not in MUTATING:
            raise env.KatError(f"line {n}: batch supports only {sorted(MUTATING)}")
        try:
            sub = build_parser().parse_args([argv[0], "SCH", *argv[1:]])
        except SystemExit:
            raise env.KatError(f"line {n}: invalid arguments: {line}; nothing saved")
        try:
            results.append({"line": n, "result": sub.func(s, sub)})
        except env.KatError as e:
            raise env.KatError(f"line {n} ({line}): {e}; nothing saved") from e
    s.save()
    return {"applied": len(results), "results": results}


# --- parser -------------------------------------------------------------------

POINTS = """\
Points: REF.PIN (pin number, or pin name if unique on the part, e.g. U1.3, R1.2, Q1.B)
        or X,Y in millimetres. Y grows downwards. Positions snap to the 1.27 mm grid;
        place parts on multiples of 2.54 mm so pins line up."""

WORKFLOW = f"""\
Workflow (see SPEC.md):
  1. describe the circuit in SKiDL (set tag= on parts), generate design.net
  2. kat new DIR/NAME.kicad_sch            empty project, once
  3. kat sync SCH design.net               parts + connectivity from the design
  4. kat snapshot SCH --expect design.net  image + checks into history/
  5. review the PNG, then arrange:        kat batch SCH passN.kat
  6. repeat 4-5; after a design change: regenerate design.net and go to 3

Connectivity changes belong in SKiDL only. Arrangement commands must keep
`kat check --expect design.net` matching.

{POINTS}

Every command prints one JSON document. Exit code 0 = fine, 1 = checks failed,
2 = error ({{"error": ...}}). Mutating commands re-save the file through
`kicad-cli sch upgrade` (validation + KiCad formatting)."""


class _Fmt(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=100)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kat", formatter_class=_Fmt,
        description="kat - edit and verify KiCad 10 schematics from the command line.",
        epilog=WORKFLOW)
    sp = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def cmd(name, func, summary, description, examples, *, sch=True, mutating=False):
        q = sp.add_parser(name, help=summary, description=f"{summary}\n\n{description}".rstrip(),
                          epilog="Examples:\n" + "\n".join(f"  {e}" for e in examples),
                          formatter_class=_Fmt)
        if sch:
            q.add_argument("sch", metavar="SCH", help="the .kicad_sch file")
        q.set_defaults(func=func, mutating=mutating)
        return q

    # --- project, libraries, inspection ---
    q = cmd("new", cmd_new, "Create a KiCad project with an empty schematic.",
            "Copies KiCad's default project template next to the new sheet.",
            ["kat new boards/tia/tia.kicad_sch", "kat new boards/tia --paper A3"], sch=False)
    q.add_argument("path", help="DIR (project named after it) or DIR/NAME.kicad_sch")
    q.add_argument("--paper", default="A4", help="page size: A4, A3, A2, ... (default A4)")
    q.add_argument("--force", action="store_true", help="overwrite an existing schematic")

    q = cmd("lib", cmd_lib, "Search and inspect symbol libraries.",
            "Libraries come from KiCad's sym-lib-table (global + project) or the stock directory.\n"
            "`show` prints pins in *library* coordinates (y up) plus units and the body box.",
            ["kat lib search opamp dual rail-to-rail", "kat lib search npn sot-23 --limit 5",
             "kat lib show Amplifier_Operational:LTC6229xMS8E"], sch=False)
    lsp = q.add_subparsers(dest="lib_cmd", required=True, metavar="ACTION")
    s1 = lsp.add_parser("search", help="find symbols: every word must match name, description or keywords")
    s1.add_argument("query", nargs="+", help="search words")
    s1.add_argument("--limit", type=int, default=30, help="max results (default 30)")
    s2 = lsp.add_parser("show", help="units, pins and fields of one symbol")
    s2.add_argument("lib_id", help="Library:Symbol")

    cmd("list", cmd_list, "List placed symbols, labels, wires and junctions.",
        "Symbols include position, rotation, mirror, value, footprint and body box.",
        ["kat list tia.kicad_sch"])
    q = cmd("pins", cmd_pins, "Show where a part's pins are on the sheet.",
            "Per pin: number, name, electrical type, sheet position and `dir`, the outward\n"
            "direction (where a wire should leave the pin).",
            ["kat pins tia.kicad_sch U1", "kat pins tia.kicad_sch U1 --unit 2"])
    q.add_argument("ref", help="reference, e.g. U1")
    q.add_argument("--unit", type=int, help="only this unit of a multi-unit part")
    cmd("netlist", cmd_netlist, "Print the nets as KiCad sees them.",
        "Net name -> [(ref, pin), ...], exported by kicad-cli from the saved file.",
        ["kat netlist tia.kicad_sch"])

    q = cmd("check", cmd_check, "Verify a schematic.",
            "Loads it with KiCad, runs ERC, compares the netlist with --expect and reports\n"
            "drawing findings: text overlaps (estimated boxes), overlapping symbol bodies and\n"
            "items outside the page frame.\n"
            "  ok    = ERC has no errors and the netlist matches\n"
            "  clean = ok and no drawing findings\n"
            "The overlap estimate ignores pin names/numbers: always look at the snapshot too.",
            ["kat check tia.kicad_sch", "kat check tia.kicad_sch --expect tia_design.net"])
    q.add_argument("--expect", metavar="NETLIST",
                   help="KiCad s-expression netlist (e.g. from SKiDL) the drawing must match")

    q = cmd("snapshot", cmd_snapshot, "Render the schematic and record a checked history step.",
            "Writes history/<timestamp>[_SLUG]/ next to the schematic (or under --out) with:\n"
            "  <name>.svg   full page with frame (for people)\n"
            "  <name>.png   trimmed, frameless image (for review by the agent)\n"
            "  <name>.kicad_sch copy, check.json, note.md (if --note)",
            ["kat snapshot tia.kicad_sch --slug pass2 --expect tia_design.net --note \"moved R1\""])
    q.add_argument("--out", help="history root directory (default: history/ next to SCH)")
    q.add_argument("--slug", default="", help="short name appended to the timestamp")
    q.add_argument("--note", help="text saved as note.md: what changed and why")
    q.add_argument("--zoom", type=float, default=3.0, help="PNG scale factor (default 3)")
    q.add_argument("--expect", metavar="NETLIST", help="as for `check`")

    q = cmd("sync", cmd_sync, "Bring a design netlist (e.g. from SKiDL) into the schematic.",
            "The netlist is the source of truth for parts and connections; the arrangement is kept.\n"
            "  - parts are matched by their SKiDL tag, then by reference\n"
            "  - renamed parts keep their place; value/footprint changes are applied in place\n"
            "  - new parts go into a free strip below the drawing, wired with stubs + labels\n"
            "    (power nets such as GND or +3V3 get power symbols)\n"
            "  - removed parts are deleted together with wires/labels left dangling\n"
            "  - pins on the wrong net are cut loose and re-attached to the right one\n"
            "  - PWR_FLAGs are added where ERC reports undriven power nets\n"
            "Running it twice changes nothing. Review the reported spots afterwards.",
            ["kat sync tia.kicad_sch tia_design.net", "kat sync tia.kicad_sch tia_design.net --dry-run"])
    q.add_argument("netlist", metavar="NETLIST", help="KiCad s-expression netlist, e.g. SKiDL generate_netlist()")
    q.add_argument("--dry-run", action="store_true", help="report what would change, save nothing")

    q = cmd("batch", cmd_batch, "Apply many editing commands with one load and one save.",
            "SCRIPT holds one editing command per line, written without `kat` and without SCH;\n"
            "`#` starts a comment line. The first failing line aborts and nothing is saved.\n"
            f"Allowed commands: {', '.join(sorted(MUTATING))}.",
            ["kat batch tia.kicad_sch pass2.kat", "echo 'move R1 --by 0,-2.54' | kat batch tia.kicad_sch -"])
    q.add_argument("script", help="script file, or - for stdin")

    # --- editing ---
    q = cmd("add", cmd_add, "Place a library symbol.",
            "Returns the new part with its pin positions. Multi-unit parts: one call per unit\n"
            "with the same --ref. Prefer `sync` for parts that come from the design.",
            ["kat add s.kicad_sch Device:R 101.6 76.2 --rot 90 --ref R1 --value 10k",
             "kat add s.kicad_sch Amplifier_Operational:LTC6229xMS8E 127 101.6 --unit 2 --ref U1"],
            mutating=True)
    q.add_argument("lib_id", help="Library:Symbol")
    q.add_argument("x", type=float, help="X in mm"); q.add_argument("y", type=float, help="Y in mm")
    q.add_argument("--rot", type=float, default=0, choices=[0, 90, 180, 270], help="rotation, counter-clockwise")
    q.add_argument("--mirror", choices=["x", "y"], help="x: flip vertically, y: flip horizontally")
    q.add_argument("--unit", type=int, default=1, help="unit number (A=1, B=2, ...)")
    q.add_argument("--ref", help="reference (default: next free, e.g. R3)")
    q.add_argument("--value", help="value field"); q.add_argument("--footprint", help="Library:Footprint")

    q = cmd("wire", cmd_wire, "Draw a wire polyline through the given points.",
            POINTS, ["kat wire s.kicad_sch R1.2 101.6,88.9 D1.2"], mutating=True)
    q.add_argument("points", nargs="+", help="REF.PIN or X,Y, at least two")

    q = cmd("connect", cmd_connect, "Connect two points with an orthogonal wire.",
            "Each leg between consecutive points bends at most once: horizontal first (--first h)\n"
            "or vertical first (--first v). Use --via to steer around parts.\n" + POINTS,
            ["kat connect s.kicad_sch U1.1 R1.2 --via 139.7,101.6 139.7,88.9",
             "kat connect s.kicad_sch C1.1 114.3,88.9 --first v"], mutating=True)
    q.add_argument("a", help="start: REF.PIN or X,Y"); q.add_argument("b", help="end: REF.PIN or X,Y")
    q.add_argument("--via", nargs="*", default=[], metavar="X,Y", help="intermediate points")
    q.add_argument("--first", choices=["h", "v"], default="h", help="direction of the first segment of each bend")

    q = cmd("label", cmd_label, "Place a net label, optionally at the end of a short wire stub.",
            "Labels with the same text on a sheet are connected. --angle turns the text:\n"
            "0 = reads to the right, 180 = to the left, 90 = upwards, 270 = downwards.",
            ["kat label s.kicad_sch VIN R1.1 --stub 0,-2.54 --angle 90",
             "kat label s.kicad_sch TIA_OUT 149.86,101.6"], mutating=True)
    q.add_argument("name", help="net name"); q.add_argument("at", help="REF.PIN or X,Y")
    q.add_argument("--stub", type=xy, metavar="DX,DY", help="draw a wire of this offset first, label at its end")
    q.add_argument("--angle", type=float, default=0, choices=[0, 90, 180, 270], help="text direction")
    q.add_argument("--global", dest="glob", action="store_true", help="global label (connects across sheets)")
    q.add_argument("--shape", default="passive", choices=["input", "output", "bidirectional", "tri_state", "passive"],
                   help="global label shape")

    q = cmd("power", cmd_power, "Place a power symbol whose pin sits on the target point.",
            "NAME is a symbol of the `power` library (GND, +3V3, -2V5, PWR_FLAG, ...) or a full\n"
            "Library:Symbol. --rot 180 turns e.g. GND upwards.",
            ["kat power s.kicad_sch GND C2.2", "kat power s.kicad_sch PWR_FLAG C2.1 --stub -5.08,0",
             "kat power s.kicad_sch GND C3.1 --rot 180"], mutating=True)
    q.add_argument("name", help="power symbol name or Library:Symbol"); q.add_argument("at", help="REF.PIN or X,Y")
    q.add_argument("--stub", type=xy, metavar="DX,DY", help="draw a wire of this offset first")
    q.add_argument("--rot", type=float, default=0, choices=[0, 90, 180, 270], help="rotation")

    q = cmd("nc", cmd_nc, "Mark a pin as intentionally unconnected (no-connect flag).",
            "", ["kat nc s.kicad_sch U2.5"], mutating=True)
    q.add_argument("at", help="REF.PIN or X,Y")

    q = cmd("move", cmd_move, "Move, rotate or mirror a placed part.",
            "Everything that hangs only off the part moves with it: wire stubs, labels, power\n"
            "symbols, PWR_FLAGs. Wires into shared nets are dragged: their end follows with a\n"
            "bend if needed. If that leaves a step next to the part, `detach` the pin and\n"
            "`connect` it again. Multi-unit parts need --unit.",
            ["kat move s.kicad_sch R1 --by 0,-2.54", "kat move s.kicad_sch U1 --unit 3 --to 175.26,101.6",
             "kat move s.kicad_sch Q1 --rot 90 --mirror y"], mutating=True)
    q.add_argument("ref", help="reference")
    q.add_argument("--to", type=xy, metavar="X,Y", help="new absolute position")
    q.add_argument("--by", type=xy, metavar="DX,DY", help="relative displacement")
    q.add_argument("--rot", type=float, choices=[0, 90, 180, 270], help="new rotation")
    q.add_argument("--mirror", choices=["", "x", "y"], help="new mirror state ('' = none)")
    q.add_argument("--unit", type=int, help="unit of a multi-unit part")
    q.add_argument("--no-drag", action="store_true", help="leave all wires where they are")

    q = cmd("set", cmd_set, "Change a field: value, position, angle or visibility.",
            "FIELD is Reference, Value, Footprint, Datasheet or any custom field (created if\n"
            "missing, hidden). Changing Reference here renames the part in the drawing only -\n"
            "rename it in the design instead and `sync`.",
            ["kat set s.kicad_sch U1 Value LTC6229 --autoplace",
             "kat set s.kicad_sch U1 Reference --unit 3 --at 182.88,100.33",
             "kat set s.kicad_sch '#FLG1' Value --hide"], mutating=True)
    q.add_argument("ref", help="reference"); q.add_argument("field", help="field name")
    q.add_argument("value", nargs="?", help="new text (omit to only move/hide)")
    q.add_argument("--at", type=xy, metavar="X,Y", help="field position (needs --unit on multi-unit parts)")
    q.add_argument("--angle", type=float, help="text angle")
    q.add_argument("--hide", action="store_true", help="hide the field"); q.add_argument("--show", action="store_true", help="show it")
    q.add_argument("--unit", type=int, help="unit of a multi-unit part")
    q.add_argument("--autoplace", action="store_true", help="re-run automatic field placement")

    q = cmd("detach", cmd_detach, "Disconnect a pin from everything drawn at it.",
            "Removes the pin's own wire run up to the next branch point, the wire leading into\n"
            "the shared net, and labels / power symbols / flags on that run. Typical use:\n"
            "replace a label pair by a real wire - detach both pins, then `connect` them.\n"
            "Afterwards `check --expect` shows whether the net is complete again.",
            ["kat detach s.kicad_sch R1.2", "printf 'detach R2.1\\ndetach U1.1\\nconnect R2.1 U1.1\\n' | kat batch s.kicad_sch -"],
            mutating=True)
    q.add_argument("pin", help="REF.PIN")

    q = cmd("rm", cmd_rm, "Remove a part, an item by UUID, or the wires at a point.",
            "Removing parts that come from the design is undone by the next `sync`; remove them\n"
            "in the design instead.",
            ["kat rm s.kicad_sch --wires-at R1.1", "kat rm s.kicad_sch --wires-at 118.11,88.9",
             "kat rm s.kicad_sch --uuid 7f786971-1fb7-4862-b97f-c58ad95d01b4"], mutating=True)
    q.add_argument("ref", nargs="?", help="reference of the part to remove (all units unless --unit)")
    q.add_argument("--unit", type=int, help="only this unit")
    q.add_argument("--uuid", help="remove the item with this uuid (wire, label, junction, ...)")
    q.add_argument("--wires-at", metavar="POINT", help="REF.PIN or X,Y: wires ending at or passing through it")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mutating:
            s = Schematic(Path(args.sch))
            out = args.func(s, args)
            s.save()
        else:
            out = args.func(args)
        print(json.dumps(out, indent=1, default=list))
        failed = isinstance(out, dict) and (out.get("ok") is False or
                                            (out.get("check") or {}).get("ok") is False)
        return 1 if failed else 0
    except (env.KatError, ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
