"""``kat`` command line: a thin layer over the library (``import kat``).

Every command prints one JSON document to stdout. Mutating commands run inside
``kat.edit``: load, apply, save once through ``kicad-cli sch upgrade`` (validation and
KiCad's own formatting). Arrangement passes are Python scripts using the same API.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import kat

from . import env, report
from .sheet import xy as _xy


def xy(text: str) -> tuple[float, float]:
    try:
        return _xy(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


# --- handlers: one library call each ----------------------------------------------
# Mutating handlers take (sheet, args); the others take (args).

def cmd_add(s, a):
    return s.add(a.lib_id, a.x, a.y, a.rot, a.mirror or "", a.unit, a.ref, a.value, a.footprint)


def cmd_wire(s, a):
    return s.wire(*a.points)


def cmd_connect(s, a):
    return s.connect(a.a, a.b, [xy(v) for v in a.via or []], a.first)


def cmd_label(s, a):
    return s.label(a.name, a.at, a.stub, a.angle, a.glob, a.shape)


def cmd_power(s, a):
    return s.power(a.name, a.at, a.stub, a.rot)


def cmd_nc(s, a):
    return s.nc(a.at)


def cmd_move(s, a):
    return s.move(a.ref, a.to, a.by, a.rot, a.mirror, a.unit, drag=not a.no_drag,
                  allow_contact=a.allow_contact)


def cmd_move_many(s, a):
    text = sys.stdin.read() if a.moves == "-" else a.moves
    if a.moves.startswith("@"):
        text = open(a.moves[1:], encoding="utf-8").read()
    try:
        moves = json.loads(text)
    except ValueError as e:
        raise env.KatError(f"MOVES is not valid JSON: {e}")
    return s.move_many(moves, drag=not a.no_drag, allow_contact=a.allow_contact)


def cmd_set(s, a):
    hide = True if a.hide else False if a.show else None
    return s.set(a.ref, a.field, a.value, a.at, a.angle, hide, a.unit, a.autoplace)


def cmd_detach(s, a):
    return s.detach(a.pin)


def cmd_rm(s, a):
    return s.rm(a.ref, a.unit, a.uuid, a.wires_at)


def cmd_new(a):
    return kat.new(a.path, a.paper, a.force)


def cmd_list(a):
    if a.near:
        return kat.load(a.sch).near(a.near, a.radius)
    return kat.load(a.sch).list()


def cmd_page(s, a):
    return s.page(a.paper, a.portrait)


def cmd_title(s, a):
    return s.title(a.title, a.date, a.rev, a.company,
                   {int(n): v for n, v in (c.split("=", 1) for c in a.comment or [])})


def cmd_text(s, a):
    return s.text(a.text, a.at, a.size, a.angle, a.bold)


def cmd_frame(s, a):
    return s.frame(a.block, a.title, a.margin)


def cmd_stamp(a):
    names = "all" if a.instance == ["all"] else a.instance
    return kat.stamp(a.sch, a.block, names, xy(a.at), a.expect, xy(a.step) if a.step else (0.0, 0.0),
                     not a.no_frame)


def cmd_restore(a):
    return kat.restore(a.sch, a.step, a.expect)


def cmd_pins(a):
    return kat.load(a.sch).pins(a.ref, a.unit)


def cmd_lib(a):
    if a.lib_cmd == "search":
        return kat.lib_search(" ".join(a.query), a.limit)
    return kat.lib_show(a.lib_id)


def cmd_pcb(a):
    if a.pcb_cmd == "sync":
        return kat.pcb.sync(a.pcb, a.sch, a.dry_run, a.force, a.rip_stale,
                            () if a.no_require else (a.require_field or ["MPN"]))
    if a.pcb_cmd == "check":
        return kat.pcb.check(a.pcb)
    if a.pcb_cmd == "refill":
        return kat.pcb.refill(a.pcb)
    if a.pcb_cmd == "fab":
        return kat.pcb.fab(a.pcb, a.out, not a.no_step, not a.no_3d)
    if a.pcb_cmd == "ready":
        return kat.pcb.ready(a.pcb, a.fab)
    if a.pcb_cmd == "route":
        if a.install and not a.pcb:
            from kat.pcb import router as _route
            return _route.install()
        if not a.pcb:
            raise env.KatError("give the board (or only --install)")
        return kat.pcb.route(a.pcb, a.passes, a.net or None, a.timeout, install=a.install)
    if a.pcb_cmd == "stamp":
        names = "all" if a.instance == ["all"] else a.instance
        return kat.pcb.stamp(a.pcb, a.block, names, xy(a.at), xy(a.step) if a.step else (0.0, 0.0),
                             a.rot, not a.no_lock)
    if a.pcb_cmd == "instances":
        return kat.pcb.instances(a.pcb, a.block)
    if a.pcb_cmd == "environment":
        return kat.pcb.block_environment(a.block, a.zone)
    if a.pcb_cmd == "snapshot":
        return kat.pcb.snapshot(a.pcb, a.slug, a.note, a.out, a.tiles, a.finding_crops, not a.no_3d)
    if a.pcb_cmd == "view":
        region = None
        if a.region:
            try:
                region = tuple(float(v) for v in a.region.split(","))
                assert len(region) == 4
            except (ValueError, AssertionError):
                raise env.KatError("--region needs X,Y,W,H")
        return kat.pcb.view(a.pcb, a.around, xy(a.at) if a.at else None, region, a.layers,
                            a.margin, a.ppm)
    raise env.KatError(f"unknown pcb action {a.pcb_cmd!r}")


def cmd_block(a):
    from . import blocks
    if a.block_cmd == "new":
        return blocks.new(a.name, a.lib)
    if a.block_cmd == "build":
        return blocks.build(a.name, a.lib, a.force)
    if a.block_cmd == "check":
        return blocks.check(a.name, a.lib, () if a.no_require else tuple(a.require_field or ["MPN"]))
    if a.block_cmd == "list":
        return blocks.list_blocks(a.lib)
    raise env.KatError(f"unknown block action {a.block_cmd!r}")


def cmd_api(a):
    from . import api
    return api.lookup(a.name)


def cmd_comment(a):
    text = sys.stdin.read() if a.text == ["-"] else " ".join(a.text)
    return kat.comment(text, a.kind, a.project)


def cmd_batch_exec(a):
    args = a.args[1:] if a.args[:1] == ["--"] else a.args
    return kat.batch_exec(a.file, a.project, args)


def cmd_open(a):
    return kat.open_kicad(a.file, a.verify, a.timeout)


def cmd_netlist(a):
    return kat.nets(a.sch)


def cmd_check(a):
    return kat.check(a.sch, a.expect)


def cmd_ready(a):
    return kat.ready(a.sch, a.expect, () if a.no_require else (a.require_field or ["MPN"]))


def cmd_snapshot(a):
    return kat.snapshot(a.sch, a.slug, a.note, a.expect, a.out, a.zoom, a.tiles, a.finding_crops)


def cmd_view(a):
    region = None
    if a.region:
        try:
            region = tuple(float(v) for v in a.region.split(","))
            assert len(region) == 4
        except (ValueError, AssertionError):
            raise env.KatError(f"--region expects X,Y,W,H in mm, got {a.region!r}")
    return kat.view(a.sch, a.around, a.at, region, a.margin, a.zoom, a.out)


def cmd_sync(a):
    return kat.sync(a.sch, a.netlist, a.dry_run, a.sheets.split(",") if a.sheets else None)


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
  4. kat snapshot SCH --expect design.net  image + checks into history/ (+ tiles if large)
     kat view SCH --around U1             zoom into details the overview cannot show
  5. review the PNG, then arrange:       kat batch-exec passN.py  (uses `import kat`)
  6. repeat 4-5; after a design change: regenerate design.net and go to 3
  7. kat ready SCH --expect design.net     gate before the PCB layout starts
  8. board: kat pcb sync / batch-exec passes / route / fab / ready (kat pcb --help)

Design report: every command on a project, every batch-exec pass (with its code) and every
snapshot (with its pictures) is recorded in NAME_REPORT.md next to NAME.kicad_pro;
add your own plans, problems and milestones with `kat comment --kind ... TEXT`.
Look up the Python API with `kat api [NAME]`.

An arrangement pass is a Python script; it is saved once, or not at all on an error:
    import kat
    with kat.edit("board/board.kicad_sch") as s:
        s.move("R1", to=(127, 86.36), rot=90)
        s.detach("R1.1"); s.connect("R1.1", "U1.2")
The editing commands below are the methods of kat.Sheet, with the same arguments.

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
    p.add_argument("--no-report", action="store_true",
                   help="do not journal this command in NAME_REPORT.md (also: KAT_REPORT=0)")
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

    q = cmd("api", cmd_api, "Look up kat's Python API: signatures and docstrings from the code.",
            "Without NAME: the functions of kat and kat.pcb and their classes. NAME may be\n"
            "full (kat.pcb.Board.keepout) or short (pcb.Board.keepout, Board.keepout, keepout);\n"
            "a function shows its signature, docstring and source line, a class or module its\n"
            "members with one line each. Plain text; --json for the data.",
            ["kat api", "kat api Board", "kat api Board.keepout", "kat api kat.pcb.route"], sch=False)
    q.add_argument("name", nargs="?", help="what to look up")
    q.add_argument("--json", action="store_true", help="print JSON instead of text")

    # --- journal ---
    q = cmd("batch-exec", cmd_batch_exec, "Run a Python pass and journal it with its code.",
            "Runs FILE.py as __main__ (like `python FILE.py`) and appends a batch entry to the\n"
            "project's NAME_REPORT.md: the script's docstring, its original source, its output,\n"
            "the error if it failed, and the images of snapshots/views it made. kat calls\n"
            "inside the script add no entries of their own. The project is --project, else\n"
            "KAT_PROJECT, else found from the files the script edits, else the working directory.\n"
            "Exit code 1 when the script failed (its failing edit block saved nothing).",
            ["kat batch-exec place1.py", "kat batch-exec route_pass.py --project board -- --fast"],
            sch=False)
    q.add_argument("file", metavar="FILE.py")
    q.add_argument("--project", metavar="DIR", help="project directory or file")
    q.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the script (after --)")

    q = cmd("comment", cmd_comment, "Add a markdown comment to the design report.",
            "For the agent's own entries: plan (what comes next and why), milestone (what was\n"
            "reached), problem (a failed check or a design error that needs another iteration),\n"
            "note. TEXT is markdown; `-` reads it from stdin (for several lines).",
            ["kat comment --kind plan 'Place the connectors first, then the op-amp.'",
             "kat comment --kind problem - < findings.md"], sch=False)
    q.add_argument("text", nargs="+", metavar="TEXT", help="markdown, or - for stdin")
    q.add_argument("--kind", default="note", choices=["plan", "milestone", "problem", "note"])
    q.add_argument("--project", metavar="DIR", help="project directory or file (default: found "
                                                     "from KAT_PROJECT or the working directory)")

    q = cmd("open", cmd_open, "Open a file in KiCad's editor; --verify checks what it loaded.",
            "Without --verify the editor stays open for you (close it before kat edits the file:\n"
            "kat refuses to save a file KiCad has open). --verify starts the editor with a\n"
            "private copy of the settings (IPC API on), compares what it loaded with kat's own\n"
            "reading and closes it without saving: board (needs kicad-python, kat[gui]) -\n"
            "footprints, nets, tracks, vias, zones, outline; schematic - opened and running\n"
            "(KiCad 10.0.6 has no schematic API). Exit code 1 on a mismatch.",
            ["kat open board/board.kicad_pcb", "kat open board/board.kicad_pcb --verify",
             "kat open board/board.kicad_sch --verify"], sch=False)
    q.add_argument("file", metavar="FILE", help=".kicad_sch, .kicad_pcb or .kicad_pro")
    q.add_argument("--verify", action="store_true", help="compare and close again")
    q.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for the editor")

    q = cmd("list", cmd_list, "List placed symbols, labels, wires and junctions.",
            "Symbols include position, rotation, mirror, value, footprint and body box.\n"
            "With --near: only what lies within --radius of a point, nearest first, with uuids.",
            ["kat list tia.kicad_sch", "kat list tia.kicad_sch --near 127,101.6 --radius 3"])
    q.add_argument("--near", metavar="POINT", help="REF.PIN or X,Y")
    q.add_argument("--radius", type=float, default=5.0, help="mm around --near (default 5)")
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
            "drawing findings: text overlaps (estimated boxes), overlapping symbol bodies,\n"
            "items outside the page frame and items drawn twice on one spot.\n"
            "  ok    = ERC has no errors and the netlist matches\n"
            "  clean = ok and no drawing findings\n"
            "The overlap estimate ignores pin names/numbers: always look at the snapshot too.",
            ["kat check tia.kicad_sch", "kat check tia.kicad_sch --expect tia_design.net"])
    q.add_argument("--expect", metavar="NETLIST",
                   help="KiCad s-expression netlist (e.g. from SKiDL) the drawing must match")

    q = cmd("ready", cmd_ready, "Gate before the PCB layout: is the schematic finished?",
            "Checks the parts KiCad will put on the board (symbols marked 'not on board' are\n"
            "skipped):\n"
            "  errors   schematic not ok/clean (as kat check), unannotated reference, empty\n"
            "           value, no footprint, footprint library or file not found, symbol pin\n"
            "           without a pad, missing required field (default MPN; parts excluded\n"
            "           from the BOM are exempt)\n"
            "  warnings no --expect, numbered pad without a symbol pin (often fine: mounting\n"
            "           or shield pads), footprint outside the symbol's filters, no 3D model\n"
            "ready = no errors. Footprints come from fp-lib-table (global + project).",
            ["kat ready tia.kicad_sch --expect tia_design.net",
             "kat ready tia.kicad_sch --expect tia_design.net --require-field MPN "
             "--require-field Manufacturer"])
    q.add_argument("--expect", metavar="NETLIST", help="design netlist, as for kat check")
    q.add_argument("--require-field", action="append", metavar="NAME",
                   help="field every BOM part must fill (repeatable; default MPN)")
    q.add_argument("--no-require", action="store_true", help="require no fields")

    q = cmd("snapshot", cmd_snapshot, "Render the schematic and record a checked history step.",
            "Writes history/<timestamp>[_SLUG]/ next to the schematic (or under --out) with:\n"
            "  <name>.svg   full page with frame (for people)\n"
            "  <name>.png   trimmed, frameless image (for review by the agent)\n"
            "  <name>.kicad_sch copy, check.json, note.md (if --note)\n"
            "  tiles/r<i>c<j>.png  readable pieces of large sheets, with their regions in mm\n"
            "  findings/NN_*.png   close-ups of findings (--finding-crops N)",
            ["kat snapshot tia.kicad_sch --slug pass2 --expect tia_design.net --note \"moved R1\""])
    q.add_argument("--out", help="history root directory (default: history/ next to SCH)")
    q.add_argument("--slug", default="", help="short name appended to the timestamp")
    q.add_argument("--note", help="text saved as note.md: what changed and why")
    q.add_argument("--zoom", type=float, default=3.0, help="PNG scale factor (default 3)")
    q.add_argument("--expect", metavar="NETLIST", help="as for `check`")
    q.add_argument("--tiles", action=argparse.BooleanOptionalAction, default=None,
                   help="also write tiles/r<i>c<j>.png of ~1600 px (default: when the PNG exceeds 2000 px)")
    q.add_argument("--finding-crops", type=int, default=0, metavar="N",
                   help="write close-ups of the first N findings with a position to findings/")

    q = cmd("view", cmd_view, "Render a zoomed view of one region of the sheet.",
            "For details the full image cannot show (large sheets are downscaled when viewed).\n"
            "Give one of --around REF, --at X,Y or --region X,Y,W,H (sheet mm). Writes\n"
            "views/<timestamp>_<what>.png next to SCH (not a history step) and returns its size,\n"
            "the region and px_per_mm: sheet x = region x + pixel x / px_per_mm.",
            ["kat view tia.kicad_sch --around U1", "kat view tia.kicad_sch --at 127,101.6 --margin 8",
             "kat view big.kicad_sch --region 100,50,120,80 --zoom 6"])
    q.add_argument("--around", metavar="REF", help="the part, its fields and --margin (default 10 mm)")
    q.add_argument("--at", type=xy, metavar="X,Y", help="square of +-margin (default 15 mm) around a point")
    q.add_argument("--region", metavar="X,Y,W,H", help="rectangle in sheet mm")
    q.add_argument("--margin", type=float, help="mm around the part or point")
    q.add_argument("--zoom", type=float, help="scale (default: longer side about 1600 px)")
    q.add_argument("--out", help="PNG path (default views/<timestamp>_<what>.png)")

    q = cmd("restore", cmd_restore, "Put the schematic back to a history step.",
            "Copies the schematic saved in STEP (a history/<timestamp>_<slug>/ directory) back\n"
            "and checks it: the undo for a pass that broke something.",
            ["kat restore tia.kicad_sch history/2026-09-25_141218_pass1 --expect tia_design.net"])
    q.add_argument("step", metavar="STEP", help="history directory")
    q.add_argument("--expect", metavar="NETLIST", help="as for `check`")

    q = cmd("sync", cmd_sync, "Bring a design netlist (e.g. from SKiDL) into the schematic.",
            "The netlist is the source of truth for parts and connections; the arrangement is kept.\n"
            "  - parts are matched by their SKiDL tag, then by reference\n"
            "  - renamed parts keep their place; value/footprint changes are applied in place\n"
            "  - new parts go into rows below the drawing, wired with stubs + labels; parts on\n"
            "    an unnamed two-pin net sit side by side with a short wire; the paper grows to\n"
            "    the next ISO size if the rows do not fit\n"
            "    (power nets such as GND or +3V3 get power symbols)\n"
            "  - removed parts are deleted together with wires/labels left dangling\n"
            "  - pins on the wrong net are cut loose and re-attached to the right one\n"
            "  - PWR_FLAGs are added where ERC reports undriven power nets\n"
            "Running it twice changes nothing. Review the reported spots afterwards.\n"
            "--sheets TYPE,...: instances of these SKiDL block types become hierarchical sheets\n"
            "that use the block library's own drawing (kat block build); references follow the\n"
            "design. The choice is remembered in NAME.kat.json.",
            ["kat sync tia.kicad_sch tia_design.net", "kat sync tia.kicad_sch tia_design.net --dry-run",
             "kat sync top.kicad_sch design.net --sheets ch,rc"])
    q.add_argument("netlist", metavar="NETLIST", help="KiCad s-expression netlist, e.g. SKiDL generate_netlist()")
    q.add_argument("--dry-run", action="store_true", help="report what would change, save nothing")
    q.add_argument("--sheets", metavar="TYPE,...", help="block types drawn as hierarchical sheets")

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
            "`connect` it again. Multi-unit parts need --unit. A pin may not land on another\n"
            "pin, a wire or a label (that joins nets): use --allow-contact, or `move-many`.",
            ["kat move s.kicad_sch R1 --by 0,-2.54", "kat move s.kicad_sch U1 --unit 3 --to 175.26,101.6",
             "kat move s.kicad_sch Q1 --rot 90 --mirror y"], mutating=True)
    q.add_argument("ref", help="reference")
    q.add_argument("--to", type=xy, metavar="X,Y", help="new absolute position")
    q.add_argument("--by", type=xy, metavar="DX,DY", help="relative displacement")
    q.add_argument("--rot", type=float, choices=[0, 90, 180, 270], help="new rotation")
    q.add_argument("--mirror", choices=["", "x", "y"], help="new mirror state ('' = none)")
    q.add_argument("--unit", type=int, help="unit of a multi-unit part")
    q.add_argument("--no-drag", action="store_true", help="leave all wires where they are")
    q.add_argument("--allow-contact", action="store_true",
                   help="allow a pin to land on another pin, a wire or a label (joins nets)")

    q = cmd("move-many", cmd_move_many, "Move several parts at once (block move).",
            "Every decision is taken on the drawing before anything moves, so parts may take\n"
            "each other's places without a parking step. MOVES is JSON: a list of\n"
            '{"ref", "to": [x, y] | "by": [dx, dy], "rot", "mirror", "unit"} objects, or an object\n'
            "keyed by reference; pass it inline, as @file.json, or - for stdin.",
            ["kat move-many s.kicad_sch '{\"R1\": {\"to\": [101.6, 76.2]}, \"R2\": {\"to\": [101.6, 88.9]}}'",
             "kat move-many s.kicad_sch @moves.json"], mutating=True)
    q.add_argument("moves", metavar="MOVES", help="JSON text, @file or -")
    q.add_argument("--no-drag", action="store_true", help="leave all wires where they are")
    q.add_argument("--allow-contact", action="store_true", help="as for `move`")

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

    q = cmd("detach", cmd_detach, "Disconnect a pin (or every pin of a part) from everything drawn at it.",
            "Removes the pin's own wire run up to the next branch point, the wire leading into\n"
            "the shared net, and labels / power symbols / flags on that run. Typical use:\n"
            "replace a label pair by a real wire - detach both pins, then `connect` them.\n"
            "Afterwards `check --expect` shows whether the net is complete again.",
            ["kat detach s.kicad_sch R1.2", "kat detach s.kicad_sch R1   (all pins)",
             "in a pass: s.detach('R2.1'); s.detach('U1.1'); s.connect('R2.1', 'U1.1')"],
            mutating=True)
    q.add_argument("pin", help="REF.PIN, or REF for all pins")

    q = cmd("stamp", cmd_stamp, "Draw block instances like the block's own checked schematic.",
            "For flat sheets with SKiDL blocks: the instance's parts (Block field) are placed as in\n"
            "the block's schematic (library name or .kicad_sch), its wires, labels (renamed to the\n"
            "instance's nets) and power symbols are copied, and what the design still misses is\n"
            "attached as sync would. The block's harness must connect to it by labels. Each\n"
            "instance gets a dashed frame. The sheet must then match --expect, else it is put back.",
            ["kat stamp top.kicad_sch RC all --at 50.8,50.8 --step 0,40.64 --expect design.net",
             "kat stamp top.kicad_sch RC /CH_A --at 50.8,50.8 --expect design.net"])
    q.add_argument("block", metavar="BLOCK", help="block name (library) or its .kicad_sch")
    q.add_argument("instance", nargs="+", metavar="INSTANCE", help="/CH_A ... or all")
    q.add_argument("--at", required=True, metavar="X,Y", help="top-left of the first block drawing (mm)")
    q.add_argument("--step", metavar="DX,DY", help="offset from one instance to the next (mm)")
    q.add_argument("--expect", required=True, metavar="NETLIST", help="the design netlist")
    q.add_argument("--no-frame", action="store_true", help="no frame and heading")

    q = cmd("frame", cmd_frame, "Dashed frame and heading around a block instance's parts.",
            "Around the parts with that Block (sub-blocks included), their fields, labels and\n"
            "power symbols. Drawing it again replaces it; only a drawing, no connectivity.",
            ["kat frame top.kicad_sch CH_A", "kat frame top.kicad_sch CH_A --title 'Channel A'"],
            mutating=True)
    q.add_argument("block", metavar="BLOCK", help="instance path, e.g. CH_A")
    q.add_argument("--title", help='heading (default: "CH_A (block type)")')
    q.add_argument("--margin", type=float, default=3.81, help="mm around the content (default 3.81)")

    q = cmd("page", cmd_page, "Change the paper size.",
            "Everything keeps its position; `check` reports items outside the new frame.",
            ["kat page s.kicad_sch A2", "kat page s.kicad_sch A4 --portrait"], mutating=True)
    q.add_argument("paper", help="A4, A3, A2, A1, A0, A..E, USLetter")
    q.add_argument("--portrait", action="store_true", help="portrait orientation")

    q = cmd("title", cmd_title, "Fill the title block.",
            "Only the given fields change; an empty string clears a field.",
            ["kat title s.kicad_sch --title 'LED matrix 10x10' --rev A --date 2026-09-25",
             "kat title s.kicad_sch --comment 1='Drive 3.3-5 V, 2-4.5 mA per LED'"], mutating=True)
    q.add_argument("--title"); q.add_argument("--date"); q.add_argument("--rev")
    q.add_argument("--company")
    q.add_argument("--comment", action="append", metavar="N=TEXT", help="comment line 1-9 (repeatable)")

    q = cmd("text", cmd_text, "Place a free text (note, heading).",
            "Not connected to anything. Remove it with `rm --uuid`.",
            ["kat text s.kicad_sch 'J1: rows 1-5' 30,40 --size 2.54 --bold"], mutating=True)
    q.add_argument("text"); q.add_argument("at", help="REF.PIN or X,Y")
    q.add_argument("--size", type=float, default=1.27, help="text height in mm (default 1.27)")
    q.add_argument("--angle", type=float, default=0, choices=[0, 90, 180, 270], help="text angle")
    q.add_argument("--bold", action="store_true")

    q = cmd("rm", cmd_rm, "Remove a part, an item by UUID, or the wires at a point.",
            "Removing parts that come from the design is undone by the next `sync`; remove them\n"
            "in the design instead.",
            ["kat rm s.kicad_sch --wires-at R1.1", "kat rm s.kicad_sch --wires-at 118.11,88.9",
             "kat rm s.kicad_sch --uuid 7f786971-1fb7-4862-b97f-c58ad95d01b4"], mutating=True)
    q.add_argument("ref", nargs="?", help="reference of the part to remove (all units unless --unit)")
    q.add_argument("--unit", type=int, help="only this unit")
    q.add_argument("--uuid", help="remove the item with this uuid (wire, label, junction, ...)")
    q.add_argument("--wires-at", metavar="POINT", help="REF.PIN or X,Y: wires ending at or passing through it")

    # --- board ---
    q = cmd("pcb", cmd_pcb, "Board (PCB layout) commands.",
            "The board follows the schematic: `pcb sync` puts its parts and nets on the board\n"
            "(KiCad's own netlist of the schematic, so DRC schematic parity holds). Nothing is\n"
            "connected on the board by hand; design changes go SKiDL -> kat sync -> kat pcb sync.",
            ["kat pcb sync board/board.kicad_pcb", "kat pcb check board/board.kicad_pcb",
             "kat pcb snapshot board/board.kicad_pcb --slug place1 --note 'connectors placed'",
             "kat pcb view board/board.kicad_pcb --around U1 --layers copper"], sch=False)
    psp = q.add_subparsers(dest="pcb_cmd", required=True, metavar="ACTION")
    s1 = psp.add_parser("sync", formatter_class=_Fmt,
                        help="parts and nets from the schematic; keeps placement and copper",
                        description="Creates the board if missing. Footprints match by symbol link, then\n"
                                    "SKiDL tag, then reference. New parts go in rows right of the outline.\n"
                                    "A changed footprint is replaced in place. Copper that no longer fits\n"
                                    "its pads is listed in stale_tracks (--rip-stale removes it).\n"
                                    "Refuses unless `kat ready` passes (--force overrides).")
    s1.add_argument("pcb", metavar="PCB", help="the .kicad_pcb file (created if missing)")
    s1.add_argument("--sch", help="schematic (default: PCB with .kicad_sch)")
    s1.add_argument("--dry-run", action="store_true", help="report only, save nothing")
    s1.add_argument("--force", action="store_true", help="sync even if `kat ready` fails")
    s1.add_argument("--rip-stale", action="store_true", help="remove stale tracks and vias")
    s1.add_argument("--require-field", action="append", metavar="NAME",
                    help="field the gate requires (repeatable; default MPN)")
    s1.add_argument("--no-require", action="store_true", help="gate requires no fields")
    s2 = psp.add_parser("check", formatter_class=_Fmt, help="DRC + parity + kat findings",
                        description="KiCad's DRC with zones refilled (in memory) and schematic parity,\n"
                                    "plus: outline missing, parts outside the outline.\n"
                                    "  ok    = no DRC errors, parity 0, nothing unconnected, outline\n"
                                    "  clean = ok, no DRC warnings, no kat findings\n"
                                    "islands counts copper cut off from its net (starved thermals,\n"
                                    "isolated copper): the router's own \"all routed\" does not see them.")
    s2.add_argument("pcb", metavar="PCB")
    s8 = psp.add_parser("refill", formatter_class=_Fmt, help="fill all pours and save them",
                        description="KiCad's zone filler (through DRC) fills every pour and the result\n"
                                    "is saved into the board. Run it after the finish pass.")
    s8.add_argument("pcb", metavar="PCB")
    s3 = psp.add_parser("snapshot", formatter_class=_Fmt, help="pictures + check into history/",
                        description="history/<stamp>_pcb_<slug>/: top.png, bottom.png, placement.png,\n"
                                    "In<n>.png (whole board, ~2000 px), tiles/top|bottom/ at 30 px/mm when\n"
                                    "the overview is coarser, findings/ close-ups, 3d_top/bottom.png,\n"
                                    "check.json, note.md and a copy of the board.")
    s3.add_argument("pcb", metavar="PCB")
    s3.add_argument("--slug", default=""); s3.add_argument("--note")
    s3.add_argument("--out", help="history directory (default: history/ next to the board)")
    s3.add_argument("--tiles", action="store_true", default=None, help="always write tiles")
    s3.add_argument("--no-tiles", action="store_false", dest="tiles", help="never write tiles")
    s3.add_argument("--finding-crops", type=int, default=0, metavar="N", help="close-ups of N findings")
    s3.add_argument("--no-3d", action="store_true", help="skip the 3D renders")
    s5 = psp.add_parser("route", formatter_class=_Fmt, help="autoroute with Freerouting (headless)",
                        description="Writes a Specctra DSN (kat's own writer), runs Freerouting without GUI,\n"
                                    "imports the new tracks and vias, saves and runs the board check.\n"
                                    "Locked copper stays as it is (lock the structure first). DSN, SES and\n"
                                    "log go to history/<stamp>_route/. Judge the result by check.unconnected\n"
                                    "and check.islands, not by router.unrouted.\n"
                                    "Freerouting (Java 21+, GPL-3) is downloaded once by --install into kat's\n"
                                    "cache; KAT_FREEROUTING=/path/to.jar uses another copy.")
    s5.add_argument("pcb", metavar="PCB", nargs="?")
    s5.add_argument("--passes", type=int, default=20, help="router passes (default 20)")
    s5.add_argument("--net", action="append", metavar="NAME", help="route only this net (repeatable)")
    s5.add_argument("--timeout", type=int, default=1800, help="seconds (default 1800)")
    s5.add_argument("--install", action="store_true", help="download Freerouting first if missing")
    s6 = psp.add_parser("fab", formatter_class=_Fmt, help="fabrication files",
                        description="fab/<name>-<rev>/: gerbers/ (copper, paste, silk, mask, outline, drill\n"
                                    "files and drill map) and a zip of them, assembly/ (position file, BOM\n"
                                    "with MPN from the schematic), IPC-D-356 netlist, STEP, 3D renders.\n"
                                    "Pours are refilled for the Gerbers.")
    s6.add_argument("pcb", metavar="PCB")
    s6.add_argument("--out", help="output directory")
    s6.add_argument("--no-step", action="store_true"); s6.add_argument("--no-3d", action="store_true")
    s7 = psp.add_parser("ready", formatter_class=_Fmt, help="release gate before ordering",
                        description="ready = board check clean (no DRC errors or warnings, parity 0,\n"
                                    "nothing unconnected, nothing outside the outline), title and revision\n"
                                    "set, fabrication set complete and newer than the board.")
    s7.add_argument("pcb", metavar="PCB")
    s7.add_argument("--fab", help="fabrication directory (default fab/<name>-<rev>/)")
    s9 = psp.add_parser("stamp", formatter_class=_Fmt, help="copy a checked block layout onto instances",
                        description="Places the instance's parts as on the block board and copies its tracks,\n"
                                    "vias, zones, keep-outs, graphics and board-only parts with the instance's\n"
                                    "nets, into a KiCad group \"/CH_A (NAME)\". Instances are sheet paths\n"
                                    "(hierarchical) or SKiDL block paths (flat, Block field). Stamping again\n"
                                    "replaces the copy. The block board's origin is the top-left corner of\n"
                                    "its outline. Refused if the instance is wired differently.")
    s9.add_argument("pcb", metavar="PCB", help="the design's board")
    s9.add_argument("block", metavar="BLOCK_PCB", help="the checked block board")
    s9.add_argument("instance", nargs="+", metavar="INSTANCE", help="/CH_A ... or all")
    s9.add_argument("--at", required=True, metavar="X,Y", help="where the block origin goes (mm)")
    s9.add_argument("--step", metavar="DX,DY", help="offset from one instance to the next (mm)")
    s9.add_argument("--rot", type=float, default=0.0, help="0, 90, 180 or 270")
    s9.add_argument("--no-lock", action="store_true", help="leave the copied copper unlocked")
    s10 = psp.add_parser("instances", formatter_class=_Fmt, help="instances of a block in a design")
    s10.add_argument("pcb", metavar="PCB"); s10.add_argument("block", metavar="BLOCK_PCB")
    s11 = psp.add_parser("environment", formatter_class=_Fmt, help="name a block board's test-environment zones",
                         description="Zones listed here (e.g. the plane the block was checked against) are\n"
                                     "not copied by stamp. Stored in NAME.block.json next to the block board.")
    s11.add_argument("block", metavar="BLOCK_PCB"); s11.add_argument("zone", nargs="*", metavar="ZONE")

    # --- block library ---
    q = cmd("block", cmd_block, "The block library: blocks designed, laid out and checked once.",
            "Each block is a project in blocks/NAME/ (KAT_BLOCKS, else the nearest blocks/ folder):\n"
            "NAME.py (SKiDL function + test harness), schematic, board, NAME.block.json. The\n"
            "harness parts are the block's environment: not stamped, and their open connections\n"
            "do not count in `check`. Designs use the function (kat.blocks.use) and stamp the\n"
            "board (kat pcb stamp PCB NAME ...).",
            ["kat block new RC", "kat block build RC", "kat block check RC", "kat block list"], sch=False)
    bsp = q.add_subparsers(dest="block_cmd", required=True, metavar="ACTION")
    for action, summary in (("new", "create a block: project, SKiDL template with harness"),
                            ("build", "run the harness, sync schematic and board, record ports/params"),
                            ("check", "schematic, gate and board check; stored for stamp"),
                            ("list", "the library's blocks and their check state")):
        b = bsp.add_parser(action, formatter_class=_Fmt, help=summary, description=summary)
        if action != "list":
            b.add_argument("name", metavar="NAME")
        b.add_argument("--lib", help="library folder (default: KAT_BLOCKS or the nearest blocks/)")
        if action == "build":
            b.add_argument("--force", action="store_true", help="pass the board sync gate")
        if action == "check":
            b.add_argument("--require-field", action="append", metavar="NAME",
                           help="field the gate requires (repeatable; default MPN)")
            b.add_argument("--no-require", action="store_true", help="gate requires no fields")
    s4 = psp.add_parser("view", formatter_class=_Fmt, help="zoomed PNG of one board region",
                        description="Into views/ next to the board; not a history step. Layer sets:\n"
                                    "top, bottom, placement, copper, In1, ... or KiCad layer names.\n"
                                    "A pixel maps back to board mm as x = region[0] + px / px_per_mm.")
    s4.add_argument("pcb", metavar="PCB")
    s4.add_argument("--around", metavar="REF"); s4.add_argument("--at", metavar="X,Y")
    s4.add_argument("--region", metavar="X,Y,W,H")
    s4.add_argument("--layers", default="top", help="set name or F.Cu,B.Cu,... (default top)")
    s4.add_argument("--margin", type=float); s4.add_argument("--ppm", type=float, help="px per mm")
    return p


# commands that write no journal entry: lookups, and those that write their own
NOT_JOURNALED = ("api", "lib", "comment", "batch-exec")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_parser().parse_args(argv)
    report._disabled = args.no_report
    if args.cmd in NOT_JOURNALED:
        return _run(args)
    hint = (getattr(args, "sch", None) or getattr(args, "pcb", None) or getattr(args, "path", None)
            or getattr(args, "file", None))
    if args.cmd == "block" and getattr(args, "name", None):
        from . import blocks
        try:                                   # the block's own journal
            hint = (blocks.library() if not args.lib else Path(args.lib)) / args.name / f"{args.name}.kicad_pro"
        except env.KatError:
            hint = None
    with report.command([a for a in argv if a != "--no-report"], hint) as entry:
        code = _run(args, entry)
        entry.exit = code
    return code


def _run(args, entry=None) -> int:
    try:
        if args.mutating:
            with kat.edit(args.sch) as s:
                out = args.func(s, args)
        else:
            out = args.func(args)
        if entry is not None:
            entry.result = out
        if args.cmd == "api" and not args.json:
            from . import api
            print(api.text(out))
            return 0
        print(json.dumps(env.tidy(out), indent=1, default=list))
        failed = isinstance(out, dict) and (out.get("ok") is False or out.get("ready") is False or
                                            (out.get("check") or {}).get("ok") is False)
        return 1 if failed else 0
    except (env.KatError, ValueError, OSError) as e:
        if entry is not None:
            entry.result = {"error": str(e)}
        print(json.dumps({"error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
