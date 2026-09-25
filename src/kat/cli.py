"""``kat`` command line: a thin layer over the library (``import kat``).

Every command prints one JSON document to stdout. Mutating commands run inside
``kat.edit``: load, apply, save once through ``kicad-cli sch upgrade`` (validation and
KiCad's own formatting). Arrangement passes are Python scripts using the same API.
"""

from __future__ import annotations

import argparse
import json
import sys

import kat

from . import env
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
    return kat.load(a.sch).list()


def cmd_pins(a):
    return kat.load(a.sch).pins(a.ref, a.unit)


def cmd_lib(a):
    if a.lib_cmd == "search":
        return kat.lib_search(" ".join(a.query), a.limit)
    return kat.lib_show(a.lib_id)


def cmd_netlist(a):
    return kat.nets(a.sch)


def cmd_check(a):
    return kat.check(a.sch, a.expect)


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
    return kat.sync(a.sch, a.netlist, a.dry_run)


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
  5. review the PNG, then arrange:       python passN.py  (uses `import kat`)
  6. repeat 4-5; after a design change: regenerate design.net and go to 3

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
            "drawing findings: text overlaps (estimated boxes), overlapping symbol bodies,\n"
            "items outside the page frame and items drawn twice on one spot.\n"
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
            "Running it twice changes nothing. Review the reported spots afterwards.",
            ["kat sync tia.kicad_sch tia_design.net", "kat sync tia.kicad_sch tia_design.net --dry-run"])
    q.add_argument("netlist", metavar="NETLIST", help="KiCad s-expression netlist, e.g. SKiDL generate_netlist()")
    q.add_argument("--dry-run", action="store_true", help="report what would change, save nothing")

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

    q = cmd("detach", cmd_detach, "Disconnect a pin from everything drawn at it.",
            "Removes the pin's own wire run up to the next branch point, the wire leading into\n"
            "the shared net, and labels / power symbols / flags on that run. Typical use:\n"
            "replace a label pair by a real wire - detach both pins, then `connect` them.\n"
            "Afterwards `check --expect` shows whether the net is complete again.",
            ["kat detach s.kicad_sch R1.2",
             "in a pass: s.detach('R2.1'); s.detach('U1.1'); s.connect('R2.1', 'U1.1')"],
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
            with kat.edit(args.sch) as s:
                out = args.func(s, args)
        else:
            out = args.func(args)
        print(json.dumps(env.tidy(out), indent=1, default=list))
        failed = isinstance(out, dict) and (out.get("ok") is False or
                                            (out.get("check") or {}).get("ok") is False)
        return 1 if failed else 0
    except (env.KatError, ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
