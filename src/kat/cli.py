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


def cmd_sheet_box(s, a):
    names = lambda v: [n for n in v.split(",") if n] if v is not None else None  # noqa: E731
    return s.sheet_box(a.name, a.size, names(a.left), names(a.right))


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


def cmd_prefix(a):
    pairs = [x.split("=", 1) for x in a.map]
    if any(len(p) != 2 for p in pairs):
        raise env.KatError("give prefixes as FROM=TO (e.g. J=CON; J= removes it)")
    return {"prefixes": kat.prefixes(a.path, dict(pairs) if pairs else None, a.clear)}


def cmd_init(a):
    return kat.init(a.path, a.force)


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
    return kat.stamp(a.sch, a.block, names, xy(a.at) if a.at else None, a.expect,
                     xy(a.step) if a.step else (0.0, 0.0), not a.no_frame, a.make_room, a.plan)


def cmd_restore(a):
    return kat.restore(a.sch, a.step, a.expect)


def cmd_pins(a):
    return kat.load(a.sch).pins(a.ref, a.unit)


def cmd_lib(a):
    if a.lib_cmd == "search":
        return kat.lib_search(" ".join(a.query), a.limit)
    if a.lib_cmd == "make":
        pair = lambda v: _xy(v) if v and "," in v else ((float(v), float(v)) if v else None)  # noqa: E731
        return kat.libmake.make(a.pins, a.name, a.lib, a.out, not a.no_symbol, not a.no_footprint, a.view, a.y_up,
                                a.origin, a.drill, pair(a.pad), a.shape, pair(a.pitch),
                                None if a.marker == "none" else a.marker, a.reference, a.force, a.courtyard)
    return kat.lib_show(a.lib_id)


def _polish_options_help() -> str:
    from .pcb.polish import OPTIONS
    rows = "\n".join(f"  {k:<25}{d!s:>6}  {m}" for k, (d, m) in OPTIONS.items())
    return ("Solver options (--option KEY=V, repeatable; KEY, default, meaning; run.json records\n"
            "the effective values under options.solver):\n" + rows +
            "\n\nExamples:\n  kat pcb polish run CASE.json --out run1 --max-iterations 1000 \\\n"
            "      --option settle_iterations=10 --option bend_gain=0.01 --weight angle=2")


def _key_values(items, flag: str, conv) -> dict:
    out = {}
    for x in items or []:
        k, sep, v = x.partition("=")
        if not sep:
            raise env.KatError(f"{flag} {x!r}: give KEY=VALUE")
        out[k] = conv(v)
    return out


def _cmd_polish(a):
    if a.polish_cmd == "extract":
        region = None
        if a.region:
            x, y, w, h = (float(v) for v in a.region.split(","))
            region = (x, y, x + w, y + h)
        objectives = {k: v for k, v in (("preferred_spacing_mm", a.spacing), ("preferred_edge_margin_mm", a.edge_margin))
                      if v is not None}
        limits = {k: v for k, v in (("snap_tolerance_deg", a.snap_tolerance), ("max_added_vertices_per_trace", a.max_bends),
                                    ("max_iterations", a.max_iterations)) if v is not None}
        return kat.pcb.polish_extract(a.pcb, a.net, a.out, a.layer, region, a.order.split(",") if a.order else None,
                                      objectives or None, _key_values(a.weight, "--weight", float) or None,
                                      limits or None, a.accept_unmodelled, a.force, a.move_margin)
    if a.polish_cmd == "apply":
        return kat.pcb.polish_apply(a.pcb, a.result, a.map, a.out, a.force)
    if a.polish_cmd == "check":
        return kat.pcb.polish_check(a.case, a.group)
    if a.polish_cmd == "run":
        weights = _key_values(a.weight, "--weight", float)
        options = _key_values(a.option, "--option", float)
        limits = {k: v for k, v in (("max_iterations", a.max_iterations), ("snap_tolerance_deg", a.snap_tolerance),
                                    ("max_added_vertices_per_trace", a.max_bends)) if v is not None}
        return kat.pcb.polish_run(a.case, a.out, a.group, weights or None, options or None, limits or None,
                                  a.force, a.gui)
    if a.polish_cmd == "replay":
        return kat.pcb.polish_replay(a.run, a.case, a.gui)
    raise env.KatError(f"polish {a.polish_cmd}: not implemented")


def cmd_pcb(a):
    if a.pcb_cmd == "sync":
        return kat.pcb.sync(a.pcb, a.sch, a.dry_run, a.force, a.rip_stale,
                            () if a.no_require else (a.require_field or ["MPN"]))
    if a.pcb_cmd == "check":
        return kat.pcb.check(a.pcb, a.map, _complete(a), a.out)
    if a.pcb_cmd == "refill":
        return kat.pcb.refill(a.pcb)
    if a.pcb_cmd == "fab":
        return kat.pcb.fab(a.pcb, a.out, not a.no_step, not a.no_3d)
    if a.pcb_cmd == "ready":
        accept = dict(x.split("=", 1) if "=" in x else (x, "") for x in (a.accept or []))
        if any(not r.strip() for r in accept.values()):
            raise env.KatError("--accept KIND=REASON: give the reason the warnings are harmless")
        return kat.pcb.ready(a.pcb, a.fab, accept, a.overhang or [])
    if a.pcb_cmd == "route":
        if a.install and not a.pcb:
            from kat.pcb import router as _route
            return _route.install()
        if not a.pcb:
            raise env.KatError("give the board (or only --install)")
        bad = [x for x in a.router_option or [] if "=" not in x]
        if bad:
            raise env.KatError(f"--router-option {bad[0]!r}: give KEY=VALUE")
        return kat.pcb.route(a.pcb, a.passes, a.net or None, a.timeout, install=a.install, skip_classes=a.skip_class,
                             optimize=not a.no_optimize,
                             options=dict(x.split("=", 1) for x in a.router_option or []) or None,
                             classes=a.netclass or None)
    if a.pcb_cmd == "tidy":
        region = None
        if a.region:
            x, y, w, h = (float(v) for v in a.region.split(","))
            region = (x, y, x + w, y + h)
        return kat.pcb.tidy_tracks(a.pcb, a.nets or None, a.layers.split(",") if a.layers else None, region,
                                   a.steps.split(",") if a.steps else None, a.corners, a.spread, a.max_growth,
                                   a.freeze_length or [], a.include_locked, a.dry_run)
    if a.pcb_cmd == "lock":
        return kat.pcb.lock(a.pcb, a.net, not a.unlock)
    if a.pcb_cmd == "polish":
        return _cmd_polish(a)
    if a.pcb_cmd == "stamp":
        names = "all" if a.instance == ["all"] else a.instance
        if a.preview:
            if not a.at:
                raise env.KatError("--preview needs --at X,Y")
            return kat.pcb.stamp_preview(a.pcb, a.block, names or None, xy(a.at), xy(a.step) if a.step else None,
                                         a.rot, a.side, a.layer_map, a.allow_asymmetric, a.anchor)
        twins = [x.split("=", 1) for x in a.twin or []]
        if any(len(t) != 2 for t in twins):
            raise env.KatError("--twin TOP=BOTTOM (the instance stamped mirrored on the other side)")
        if a.sites:
            if names or a.at:
                raise env.KatError("--sites gives the instances and places: no INSTANCE or --at with it")
            return kat.pcb.stamp_sites(a.pcb, a.block, a.sites, not a.no_lock, a.make_room, a.layer_map,
                                       a.allow_asymmetric, a.anchor, a.zone_priority)
        if not names:
            raise env.KatError("name the instances (/CH_A ... or all) or give --sites; only --preview works without")
        if not a.at:
            raise env.KatError("--at X,Y is needed (or --sites)")
        return kat.pcb.stamp(a.pcb, a.block, names, xy(a.at), xy(a.step) if a.step else None,
                             a.rot, not a.no_lock, a.make_room, a.side, a.layer_map, a.allow_asymmetric, a.anchor,
                             a.zone_priority, dict(twins) or None)
    if a.pcb_cmd == "stitch":
        return kat.pcb.stitch(a.pcb, a.net, a.pitch, a.move_margin, not a.no_lock)
    if a.pcb_cmd == "plane-stub":
        return kat.pcb.plane_stub(a.pcb, a.ref, a.net or None, a.skip_net or [], not a.no_lock)
    if a.pcb_cmd == "profile":
        return kat.pcb.profile(a.pcb, a.via_types.split(",") if a.via_types else None, a.min_clearance, a.clear)
    if a.pcb_cmd == "stamps":
        return kat.pcb.stamps(a.pcb, (a.instance or True) if a.rebuild else False)
    if a.pcb_cmd == "collisions":
        return kat.pcb.stamp_collisions(a.pcb, a.instance or None)
    if a.pcb_cmd == "cell":
        if a.min_pitch:
            if a.pitch or a.clear:
                raise env.KatError("--min-pitch searches the pitch; set it with --save, not --pitch/--clear")
            return kat.pcb.min_pitch(a.block, a.min_pitch, xy(a.center) if a.center else None, a.side,
                                     a.layer_map, a.allow_asymmetric, a.shared, a.save)
        if a.save or a.center or a.shared or a.side not in ("top", "F"):
            raise env.KatError("--save, --center, --shared and --side go with --min-pitch")
        return kat.pcb.block_cell(a.block, xy(a.pitch) if a.pitch else None, a.clear)
    if a.pcb_cmd == "roles":
        roles = dict(x.split("=", 1) for x in a.role if "=" in x)
        if len(roles) != len(a.role):
            raise env.KatError("give roles as LAYER=ROLE (e.g. L1=in_signal F.Cu=in_signal_top)")
        return {"board": a.pcb, "roles": kat.pcb.layer_roles(a.pcb, roles or None)}
    if a.pcb_cmd == "stack":
        return kat.pcb.stack(a.pcb)
    if a.pcb_cmd == "netrule":
        if a.kind == "coupling":
            if len(a.args) != 2:
                raise env.KatError("coupling needs two net selectors: A B (e.g. 'OUT*' 'IN*')")
            return kat.pcb.coupling_rule(a.pcb, a.args[0], a.args[1], a.distance, a.harness, a.estimate)
        if a.kind == "layers":
            if len(a.args) < 2:
                raise env.KatError("layers needs NETS LAYER... (e.g. 'IN*' F.Cu B.Cu)")
            return kat.pcb.layer_rule(a.pcb, a.args[0], a.args[1:], a.via_span, a.pads)
        if a.kind == "plane":
            if len(a.args) != 1:
                raise env.KatError("plane needs one net selector (and --net for the plane's net)")
            return kat.pcb.plane_rule(a.pcb, a.args[0], a.net, a.except_zone,
                                      [tuple(float(v) for v in x.split(",")) for x in a.except_area or []],
                                      a.except_zone_glob)
        if a.args:
            raise env.KatError(f"{a.kind} takes no further arguments")
        return kat.pcb.net_rules(a.pcb, a.kind == "clear")
    if a.pcb_cmd == "escape":
        return kat.pcb.escape(a.pcb, a.spec, a.apply, a.out)
    if a.pcb_cmd == "access":
        return kat.pcb.port_access(a.spec)
    if a.pcb_cmd == "mate":
        def pairs(items, what):
            bad = [x for x in items if "=" not in x]
            if bad:
                raise env.KatError(f"{what} {bad[0]!r}: give A=B")
            return [tuple(x.split("=", 1)) for x in items]
        holes = "auto" if a.holes == ["auto"] else None if a.holes == ["none"] else pairs(a.holes, "--holes")
        return kat.pcb.mate(a.pcb, a.other, pairs(a.pair or [], "--pair"), xy(a.at), a.rot, a.flip,
                            dict(pairs(a.net or [], "--net")), holes, not a.below, a.tolerance, a.hole_tolerance)
    if a.pcb_cmd == "import-mech":
        assume = "plated" if a.assume_plated else "non_plated" if a.assume_non_plated else None
        return kat.pcb.import_mechanics(a.pcb, a.odb, a.gerber, a.drill or None, a.step, xy(a.at) if a.at else None,
                                        xy(a.offset) if a.offset else None, a.vias, a.min_drill, a.annular,
                                        assume, a.tolerance, a.dry_run)
    if a.pcb_cmd == "odb-pinout":
        return kat.pcb.odb_pinout(a.job, a.step, a.ref or None, a.out)
    if a.pcb_cmd == "clearance":
        return kat.pcb.clearance(a.pcb, a.a, a.b, a.layer)
    if a.pcb_cmd == "pads":
        return kat.pcb.pad_geometry(a.pcb, a.ref or None)
    if a.pcb_cmd == "instances":
        return kat.pcb.instances(a.pcb, a.block)
    if a.pcb_cmd == "environment":
        return kat.pcb.block_environment(a.block, a.zone)
    if a.pcb_cmd == "remove":
        return kat.pcb.remove(a.pcb, a.uuid or None, xy(a.at) if a.at else None, a.layer, a.kind, a.net)
    if a.pcb_cmd == "port":
        return kat.pcb.block_port(a.block, a.port, xy(a.at), a.net)
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
                            a.margin, a.ppm, texts=not a.no_texts)
    raise env.KatError(f"unknown pcb action {a.pcb_cmd!r}")


def cmd_block(a):
    from . import blocks
    if a.block_cmd == "new":
        return blocks.new(a.name, a.lib)
    if a.block_cmd == "build":
        return blocks.build(a.name, a.lib, a.force, a.overwrite)
    if a.block_cmd == "check":
        return blocks.check(a.name, a.lib, () if a.no_require else tuple(a.require_field or ["MPN"]),
                            a.sides, a.layer_map, a.allow_asymmetric)
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


def _complete(a):
    """What a mapping table must cover, from --partial and --complete."""
    if a.partial and a.complete:
        raise env.KatError("--partial and --complete exclude each other")
    return False if a.partial else (a.complete or True)


def cmd_check(a):
    return kat.check(a.sch, a.expect, a.full, a.map, _complete(a))


def cmd_back_annotate(a):
    if bool(a.assign) == bool(a.from_table):
        raise env.KatError("give the assignment: --assign PIN=NET... or --from TABLE")
    assignment = a.from_table
    if a.assign:
        bad = [x for x in a.assign if "=" not in x]
        if bad:
            raise env.KatError(f"--assign {bad[0]!r}: give PIN=NET (an empty NET: unconnected)")
        assignment = dict(x.split("=", 1) for x in a.assign)
    return kat.mapping.back_annotate(a.mapping, assignment, a.run, a.sch, a.netlist, a.pcb, None, a.dry_run, a.force,
                                     a.rip_stale, () if a.no_require else (a.require_field or ["MPN"]))


def cmd_map(a):
    return kat.mapping.check(a.file, a.mapping, _complete(a), a.pins.split(",") if a.pins else None)


def cmd_ready(a):
    return kat.ready(a.sch, a.expect, () if a.no_require else (a.require_field or ["MPN"]))


def cmd_snapshot(a):
    return kat.snapshot(a.sch, a.slug, a.note, a.expect, a.out, a.zoom, a.tiles, a.finding_crops, a.pages)


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
    bad = [x for x in a.rename_sheet or [] if "=" not in x]
    if bad:
        raise env.KatError(f"--rename-sheet {bad[0]!r}: give OLD=NEW")
    return kat.sync(a.sch, a.netlist, a.dry_run, a.sheets.split(",") if a.sheets else None,
                    dict(x.split("=", 1) for x in a.rename_sheet or []) or None)


# --- parser -------------------------------------------------------------------

POINTS = """\
Points: REF.PIN (pin number, or pin name if unique on the part, e.g. U1.3, R1.2, Q1.B)
        or X,Y in millimetres. Y grows downwards. Positions snap to the 1.27 mm grid;
        place parts on multiples of 2.54 mm so pins line up."""

WORKFLOW = f"""\
Workflow (kat init deploys self-contained agent skills):
   0. kat init [DIR]                       agent skills + managed AGENTS.md instructions
  1. describe the circuit in SKiDL (set tag= on parts), generate design.net
  2. kat new DIR/NAME.kicad_sch            empty project, once
  3. kat sync SCH design.net               parts + connectivity from the design
  4. kat snapshot SCH --expect design.net  image + checks into history/ (+ tiles if large)
     kat view SCH --around U1             zoom into details the overview cannot show
  5. review the PNG, then arrange:       kat batch-exec passN.py  (uses `import kat`)
  6. repeat 4-5; after a design change: regenerate design.net and go to 3
  7. kat ready SCH --expect design.net     gate before the PCB layout starts
  8. board: kat pcb sync / batch-exec passes / route / fab / ready (kat pcb --help)
  9. before handing over: KAT_FEEDBACK.md next to NAME.kicad_pro, kat's bugs,
     improvements, feature requests and documentation gaps met on the way

Design report: project commands (except bootstrap init), every batch-exec pass (with its code) and every
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
2 = error ({{"error": ...}}). Schematic-editing commands re-save the file through
`kicad-cli sch upgrade` (validation + KiCad formatting)."""


class _Fmt(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=100)


def _map_options(q) -> None:
    """--partial and --complete, for every command that reads a mapping table."""
    q.add_argument("--complete", action="append", metavar="REF",
                   help="connected pins of this part must be in the table (repeatable; default: every part in it)")
    q.add_argument("--partial", action="store_true", help="the table need not list all connected pins")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kat", formatter_class=_Fmt,
        description=("kat - edit and verify KiCad 10 schematics from the command line.\n\n"
                     "KiCad discovery: KICAD_CLI, then PATH, then Windows installer registration/"
                     "standard folders or the macOS app bundle. The detected path is cached in "
                     "this process's KICAD_CLI; the calling shell is unchanged."),
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
    q = cmd("init", cmd_init, "Deploy kat's agent skills into a project directory.",
            "Copies the three skills and references to .agents/skills and creates or updates\n"
            "a marked kat section in AGENTS.md, preserving other instructions. Identical\n"
            "content is unchanged; conflicts fail before writes unless --force is supplied.\n"
            "Records version and hashes in .agents/kat-init.json, not the design journal.\n"
            "No KiCad, dependency installation, Git repository or design files are required.",
            ["kat init", "kat init boards/tia", "kat init . --force"], sch=False)
    q.add_argument("path", nargs="?", default=".", metavar="DIR",
                   help="project directory (created if missing; default: current directory)")
    q.add_argument("--force", action="store_true",
                   help="replace differing kat-managed content; preserve other instructions")

    q = cmd("new", cmd_new, "Create a KiCad project with an empty schematic.",
            "Copies KiCad's default project template next to the new sheet.",
            ["kat new boards/tia/tia.kicad_sch", "kat new boards/tia --paper A3"], sch=False)
    q.add_argument("path", help="DIR (project named after it) or DIR/NAME.kicad_sch")
    q.add_argument("--paper", default="A4", help="page size: A4, A3, A2, ... (default A4)")
    q.add_argument("--force", action="store_true", help="overwrite an existing schematic")

    q = cmd("prefix", cmd_prefix, "The project's reference prefixes (e.g. J=CON).",
            "Stored in <project>.kat.json. kat lib make (without --reference) and symbols kat adds\n"
            "itself use them. Sync never renames: a SKiDL design's references come from its netlist,\n"
            "so give the part ref_prefix= there; kat check warns about references that do not follow.",
            ["kat prefix boards/tia", "kat prefix boards/tia J=CON", "kat prefix boards/tia J="], sch=False)
    q.add_argument("path", help="the project folder or a file in it")
    q.add_argument("map", nargs="*", metavar="FROM=TO", help="library prefix = project prefix (TO empty removes it)")
    q.add_argument("--clear", action="store_true", help="remove all")

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
    s3 = lsp.add_parser("make", formatter_class=_Fmt, help="generate a symbol and a footprint from a pin table",
                        description="One row per pad, CSV or JSON. Columns (case does not matter):\n"
                                    "  pin, name             pad number, name on the symbol\n"
                                    "  x, y  or  row, col    position in mm, or a grid position times --pitch\n"
                                    "  drill                 mm or WxH (slot); empty: a surface pad unless --drill\n"
                                    "  pad  or  pad_w, pad_h mm or WxH; default drill + 0.5 mm\n"
                                    "  shape                 circle, rect, oval, roundrect\n"
                                    "  type                  electrical type of the symbol pin (default passive)\n"
                                    "  side                  left, right, top, bottom on the symbol\n"
                                    "  kind                  tht, smd, npth; a row with a drill but no pin is a hole\n"
                                    "--view pin: positions are seen from the pin side, which mirrors x. The marker\n"
                                    "pin (pin 1 by default) gets a triangle on silk and fab and a rectangular pad.\n"
                                    "Writes OUT/LIB.kicad_sym and OUT/LIB.pretty/NAME.kicad_mod, read back through\n"
                                    "KiCad, and adds them to the library tables if OUT holds a .kicad_pro.\n"
                                    "Check a new part with kat pcb view and against its datasheet.")
    s3.add_argument("--pins", required=True, metavar="TABLE", help="CSV or JSON pin table")
    s3.add_argument("--name", required=True, help="part name")
    s3.add_argument("--lib", help="library name (default: the part's)")
    s3.add_argument("--out", metavar="DIR", help="where the libraries go (default: here)")
    s3.add_argument("--no-symbol", action="store_true", help="only the footprint")
    s3.add_argument("--no-footprint", action="store_true", help="only the symbol")
    s3.add_argument("--view", choices=("component", "pin"), default="component",
                    help="the side the table's positions are seen from (default component)")
    s3.add_argument("--y-up", action="store_true", help="y grows upwards in the table (default downwards)")
    s3.add_argument("--origin", choices=("table", "pin1", "center"), default="table",
                    help="footprint origin: the table's (default), the marker pin or the centre")
    s3.add_argument("--drill", type=float, metavar="MM", help="drill of rows that give none")
    s3.add_argument("--pad", metavar="W[,H]", help="pad size of rows that give none (mm)")
    s3.add_argument("--shape", choices=("circle", "rect", "oval", "roundrect"), help="shape of rows that give none")
    s3.add_argument("--pitch", metavar="DX[,DY]", help="mm per col and row")
    s3.add_argument("--marker", default="auto", metavar="PIN", help="marked pin: auto (pin 1, else the first), a number, none")
    s3.add_argument("--reference", help="reference prefix (default: the project's for J, see kat prefix; else J)")
    s3.add_argument("--courtyard", default="rect", metavar="SHAPE",
                    help="rect (default), stadium (round ends, for slot-shaped parts) or a JSON file [[x, y], ...]")
    s3.add_argument("--force", action="store_true", help="replace a part of that name")

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
            "The overlap estimate ignores pin names/numbers: always look at the snapshot too.\n"
            "Netlist differences are listed up to 10 per kind (totals in counts); --full lists all.\n"
            "--map also holds the sheet against an external pin table (see kat map).",
            ["kat check tia.kicad_sch", "kat check tia.kicad_sch --expect tia_design.net",
             "kat check tia.kicad_sch --map j1_pinout.csv --complete J1"])
    q.add_argument("--full", action="store_true", help="list every netlist difference")
    q.add_argument("--expect", metavar="NETLIST",
                   help="KiCad s-expression netlist (e.g. from SKiDL) the drawing must match")
    q.add_argument("--map", metavar="TABLE", help="pin table the sheet must agree with (CSV or JSON)")
    _map_options(q)

    q = cmd("map", cmd_map, "Check a schematic, board or netlist against an external pin table.",
            "The table is specified independently of the design: a datasheet, a mating board, a\n"
            "legacy product (kat pcb odb-pinout writes one). CSV (`,` `;` or tab, # starts a comment line) or JSON. Its columns:\n"
            "  ref,pin,net or pin,net        J1,3,CH1 / J1.3,CH1: pin on that net; an empty net or\n"
            "                                NC: unconnected. CH1 matches /CH_A/CH1; /CH_A/CH1 in full\n"
            "  channel                       a logical name: its pins share a net, channels never do\n"
            "  source, destination, dest,    several pins in a row share a net, and rows do not\n"
            "  from, to                      unless one channel or net says so\n"
            "Reports missing (pins or parts the design lacks), mismatched (expected, actual), split\n"
            "(should share a net), merged (should not), extra (connected pins of the table's parts\n"
            "that it lacks; --complete, --partial). Other columns are listed in ignored.\n"
            "Exit code 1 when it does not agree.",
            ["kat map tia.kicad_sch j1_pinout.csv",
             "kat map board.kicad_pcb channels.csv --complete J1 --complete J2",
             "kat map design.net pins.json --partial"], sch=False)
    q.add_argument("file", metavar="SOURCE", help=".kicad_sch, .kicad_pcb, a netlist (.net) or an ODB++ job")
    q.add_argument("mapping", metavar="TABLE", help="CSV or JSON pin table")
    q.add_argument("--pins", metavar="COL,...", help="the columns that hold pins (REF.NUMBER)")
    _map_options(q)

    q = cmd("ready", cmd_ready, "Gate before the PCB layout: is the schematic finished?",
            "Checks the parts KiCad will put on the board (symbols marked 'not on board' are\n"
            "skipped):\n"
            "  errors   schematic not ok/clean (as kat check), unannotated reference, empty\n"
            "           value, no footprint, footprint library or file not found, symbol pin\n"
            "           without a pad, missing required field (default MPN; parts excluded\n"
            "           from the BOM are exempt)\n"
            "  warnings no design netlist, numbered pad without a symbol pin (often fine: mounting\n"
            "           or shield pads), footprint outside the symbol's filters, no 3D model\n"
            "ready = no errors; explain names them. Without --expect the design netlist the sheet\n"
            "was last synced from is used, as kat pcb sync's gate does.\n"
            "Footprints come from fp-lib-table (global + project).",
            ["kat ready tia.kicad_sch --expect tia_design.net",
             "kat ready tia.kicad_sch --expect tia_design.net --require-field MPN "
             "--require-field Manufacturer"])
    q.add_argument("--expect", metavar="NETLIST", help="design netlist (default: the one last synced from)")
    q.add_argument("--require-field", action="append", metavar="NAME",
                   help="field every BOM part must fill (repeatable; default MPN)")
    q.add_argument("--no-require", action="store_true", help="require no fields")

    q = cmd("snapshot", cmd_snapshot, "Render the schematic and record a checked history step.",
            "Writes history/<timestamp>[_SLUG]/ next to the schematic (or under --out) with:\n"
            "  <name>.svg   full page with frame (for people)\n"
            "  <name>.png   trimmed, frameless image (for review by the agent)\n"
            "  <name>.kicad_sch copy, check.json, note.md (if --note)\n"
            "  tiles/r<i>c<j>.png  readable pieces of large sheets, with their regions in mm\n"
            "                      (tiles/<page>/... when there are several pages)\n"
            "  findings/NN_*.png   close-ups of findings (--finding-crops N)\n"
            "A hierarchy gets one PNG per sheet file (--pages instances: one per instance).",
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
    q.add_argument("--pages", choices=("files", "instances"), default="files",
                   help="hierarchy: a PNG per sheet file (default) or per sheet instance")

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
            "design. The choice is remembered in NAME.kat.json.\n"
            "--rename-sheet OLD=NEW: after renaming an instance in the design, its box keeps its\n"
            "uuid, place and wiring instead of being removed and added anew; possible_renames in\n"
            "the result names boxes that look like one. Check with --dry-run first.",
            ["kat sync tia.kicad_sch tia_design.net", "kat sync tia.kicad_sch tia_design.net --dry-run",
             "kat sync top.kicad_sch design.net --sheets ch,rc"])
    q.add_argument("netlist", metavar="NETLIST", help="KiCad s-expression netlist, e.g. SKiDL generate_netlist()")
    q.add_argument("--dry-run", action="store_true", help="report what would change, save nothing")
    q.add_argument("--sheets", metavar="TYPE,...", help="block types drawn as hierarchical sheets")
    q.add_argument("--rename-sheet", action="append", metavar="OLD=NEW",
                   help="an instance renamed in the design (repeatable)")

    q = cmd("back-annotate", cmd_back_annotate, "Apply a computed pin assignment through the design.",
            "Connectivity belongs to the design, and the SKiDL script stays as it is: it reads the\n"
            "pin table (kat.skidl_blocks.connect_pins). This sets the assignment in TABLE, keeping its\n"
            "comments and other columns, runs the script, syncs the sheet and --pcb, keeping\n"
            "placement, and verifies netlist, sheet and board against the table and each other. If a\n"
            "step fails, table, netlist, sheet and board are put back. moved names nets that left a\n"
            "part (a pure swap has none); copper that no longer fits is in board.stale_tracks\n"
            "(--rip-stale removes it): route again after it.",
            ["kat back-annotate j1.csv --assign J1.1=CH2 J1.2=CH1 --run design.py --sch top.kicad_sch --pcb top.kicad_pcb",
             "kat back-annotate j1.csv --from computed.csv --run design.py --sch top.kicad_sch --dry-run"], sch=False)
    q.add_argument("mapping", metavar="TABLE", help="the pin table the SKiDL script reads (CSV or JSON)")
    q.add_argument("--assign", nargs="+", metavar="PIN=NET", help="new assignments, e.g. J1.3=CH1")
    q.add_argument("--from", dest="from_table", metavar="FILE", help="new assignments as a table")
    q.add_argument("--run", required=True, metavar="SCRIPT", help="the SKiDL script")
    q.add_argument("--netlist", metavar="NETLIST", help="what it writes (default: its name with .net)")
    q.add_argument("--sch", required=True, metavar="SCH", help="the schematic")
    q.add_argument("--pcb", metavar="PCB", help="the board, if there is one yet")
    q.add_argument("--dry-run", action="store_true", help="only report the changes")
    q.add_argument("--force", action="store_true", help="sync the board even if `kat ready` fails")
    q.add_argument("--rip-stale", action="store_true", help="remove copper that no longer fits")
    q.add_argument("--require-field", action="append", metavar="NAME", help="as for pcb sync")
    q.add_argument("--no-require", action="store_true", help="the gate requires no fields")

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

    q = cmd("move", cmd_move, "Move, rotate or mirror a placed part, or move a sheet box.",
            "Everything that hangs only off the part moves with it: wire stubs, labels, power\n"
            "symbols, PWR_FLAGs. Wires into shared nets are dragged: their end follows with a\n"
            "bend if needed. If that leaves a step next to the part, `detach` the pin and\n"
            "`connect` it again. Multi-unit parts need --unit. A pin may not land on another\n"
            "pin, a wire or a label (that joins nets): use --allow-contact, or `move-many`.\n"
            "REF may name a sheet box (its Sheetname): the box moves with its pins, fields and\n"
            "pin wiring and keeps its uuid; --to is its top-left corner; no --rot/--mirror.",
            ["kat move s.kicad_sch R1 --by 0,-2.54", "kat move s.kicad_sch U1 --unit 3 --to 175.26,101.6",
             "kat move s.kicad_sch Q1 --rot 90 --mirror y", "kat move top.kicad_sch CH_B --to 127,82.55"],
            mutating=True)
    q.add_argument("ref", help="reference, or a sheet box name")
    q.add_argument("--to", type=xy, metavar="X,Y", help="new absolute position")
    q.add_argument("--by", type=xy, metavar="DX,DY", help="relative displacement")
    q.add_argument("--rot", type=float, choices=[0, 90, 180, 270], help="new rotation")
    q.add_argument("--mirror", choices=["", "x", "y"], help="new mirror state ('' = none)")
    q.add_argument("--unit", type=int, help="unit of a multi-unit part")
    q.add_argument("--no-drag", action="store_true", help="leave all wires where they are")
    q.add_argument("--allow-contact", action="store_true",
                   help="allow a pin to land on another pin, a wire or a label (joins nets)")

    q = cmd("sheet-box", cmd_sheet_box, "Resize a sheet box or put its pins on other edges.",
            "The box keeps its uuid (board footprint paths depend on it). Pins named in --left /\n"
            "--right go to that edge in that order; unlisted pins keep their side, below them.\n"
            "A pin's own wiring (stub, label) comes along, mirrored when it changes sides; wires\n"
            "into shared nets are dragged. Move the box with `move NAME`.",
            ["kat sheet-box top.kicad_sch CH_A --right out", "kat sheet-box top.kicad_sch CH_A --size 25.4,12.7"],
            mutating=True)
    q.add_argument("name", help="sheet box name (Sheetname)")
    q.add_argument("--size", type=xy, metavar="W,H", help="new size in mm")
    q.add_argument("--left", metavar="PIN,..", help="pins on the left edge, top to bottom")
    q.add_argument("--right", metavar="PIN,..", help="pins on the right edge, top to bottom")

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
            "instance gets a dashed frame. The sheet must then match --expect, else it is put back.\n"
            "Each instance's area must be free of other drawing (a pin on a foreign wire joins\n"
            "nets): else refused, or --make-room moves what is in the way out (parts with their\n"
            "own wiring, framed instances, sheet boxes; never wiring between parts). Without --at\n"
            "each instance goes to its current place if free, else to the first free area.\n"
            "--plan reports the areas and what is in them and changes nothing.",
            ["kat stamp top.kicad_sch RC all --at 50.8,50.8 --step 0,40.64 --expect design.net",
             "kat stamp top.kicad_sch RC /CH_A --at 50.8,50.8 --expect design.net --make-room",
             "kat stamp top.kicad_sch CH all --expect design.net --plan"])
    q.add_argument("block", metavar="BLOCK", help="block name (library) or its .kicad_sch")
    q.add_argument("instance", nargs="+", metavar="INSTANCE", help="/CH_A ... or all")
    q.add_argument("--at", metavar="X,Y", help="top-left of the first block drawing (mm); default: free areas")
    q.add_argument("--make-room", action="store_true", help="move what is in the way out of the areas")
    q.add_argument("--plan", action="store_true", help="only report the areas and what is in them")
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
                                    "plus: outline missing, parts outside the outline, and the board's net\n"
                                    "rules (kat pcb netrule: coupling, layers, plane) in net_rules.\n"
                                    "  ok    = no DRC errors, parity 0, nothing unconnected, outline\n"
                                    "  clean = ok, no DRC warnings, no kat findings\n"
                                    "islands counts copper cut off from its net (starved thermals,\n"
                                    "isolated copper): the router's own \"all routed\" does not see them.\n"
                                    "--map also holds the pads against an external pin table (see kat map).")
    s2.add_argument("pcb", metavar="PCB")
    s2.add_argument("--map", metavar="TABLE", help="pin table the pads must agree with (CSV or JSON)")
    _map_options(s2)
    s2.add_argument("--out", metavar="FILE", help="also write the result as JSON (UTF-8)")
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
                                    "and check.islands, not by router.unrouted (the router's \"all routed\" is no\n"
                                    "verdict).\n"
                                    "Time grows steeply with nets, layers and pad density (a 4-layer SOIC-16 grid:\n"
                                    "287 nets ~1.5 min, 510 nets ~3-4 min, 798 nets ~8 min; 2 more layers help\n"
                                    "more than more passes). On a dense board route in chunks: constrained nets\n"
                                    "first (--net, globs, --netclass), then groups by region or connector; after\n"
                                    "each, kat pcb check and kat pcb lock, so the next run keeps it. --timeout\n"
                                    "keeps a partial result; unrouted per pass shows a stall.\n"
                                    "Freerouting (Java 25+, GPL-3) is downloaded once by --install into kat's\n"
                                    "cache; KAT_FREEROUTING=/path/to.jar uses another copy.")
    s5.add_argument("pcb", metavar="PCB", nargs="?")
    s5.add_argument("--passes", type=int, default=20, help="router passes (default 20)")
    s5.add_argument("--net", action="append", metavar="NAME", help="route only this net, or a glob such as '/SPI_*' (repeatable)")
    s5.add_argument("--netclass", action="append", metavar="NAME", help="route only this net class's nets (repeatable)")
    s5.add_argument("--skip-class", action="append", metavar="NAME",
                    help="leave this net class out; its pads stay obstacles (repeatable)")
    s5.add_argument("--no-optimize", action="store_true", help="skip the router's optimizer stage")
    s5.add_argument("--router-option", action="append", metavar="KEY=VALUE",
                    help="another Freerouting setting (repeatable); settings reports what it refused\n"
                         "and what kat has not seen work")
    s5.add_argument("--timeout", type=int, default=1800,
                    help="seconds (default 1800): the router stops there and what it has is kept (timed_out)")
    s5.add_argument("--install", action="store_true", help="download Freerouting first if missing")
    s35 = psp.add_parser("tidy", formatter_class=_Fmt, help="clean up routed tracks",
                         description="Steps (default all): normalize (zero-length, collinear, spikes, dangling\n"
                                     "stubs), staircase (runs of short segments -> one straight or 45-degree path),\n"
                                     "tight (pull tight, never over a pad, via or track end), spread (with\n"
                                     "--spread GAP: segments closer than GAP to other copper moved away where there\n"
                                     "is room), corners (90-degree and acute corners -> two 45s or an arc).\n"
                                     "Chain ends (pads, vias, junctions, locked copper) never move; every new\n"
                                     "segment keeps its clearances. Saved, then judged by DRC: a net it made worse\n"
                                     "is put back (rolled_back). before/after: corners, short segments, reversals\n"
                                     "and gaps per layer. Running it again changes nothing.")
    s35.add_argument("pcb", metavar="PCB")
    s35.add_argument("--nets", action="append", metavar="GLOB", help="only these nets (repeatable)")
    s35.add_argument("--layers", metavar="L,..", help="only these copper layers")
    s35.add_argument("--region", metavar="X,Y,W,H", help="only tracks inside this box (mm)")
    s35.add_argument("--steps", metavar="S,..", help="normalize,staircase,tight,spread,corners (default all)")
    s35.add_argument("--corners", choices=("45", "arc", "keep"), default="45")
    s35.add_argument("--spread", type=float, metavar="GAP", help="target gap in mm for the spread step")
    s35.add_argument("--max-growth", type=float, default=10.0, metavar="PCT", help="spread may lengthen a net this much")
    s35.add_argument("--freeze-length", action="append", metavar="NET", help="never lengthen this net (repeatable)")
    s35.add_argument("--include-locked", action="store_true", help="also locked tracks")
    s35.add_argument("--dry-run", action="store_true", help="report only, save nothing")
    s36 = psp.add_parser("polish", formatter_class=_Fmt, help="topology-preserving route polishing (prototype)",
                         description="Polish a group of 5-10 routed traces without rerouting them: 45-degree\n"
                                     "directions, even spacing, edge margin, judged by hard constraints first.\n"
                                     "Works on a portable case file (specs/ROUTE_POLISH_INPUT.md); never writes\n"
                                     "to an input.")
    plsp = s36.add_subparsers(dest="polish_cmd", required=True, metavar="ACTION")
    pl0 = plsp.add_parser("extract", formatter_class=_Fmt, help="write a case of a bundle of routed traces",
                          description="The straight track runs of the nets (globs) on one layer become the movable\n"
                                      "traces of one group, in neighbour order across the bundle (or --order);\n"
                                      "their end pads and vias are anchors. Recorded stamped blocks (including nested\n"
                                      "groups) stay fixed; external runs end at their boundaries. Ordinary and unrecorded\n"
                                      "groups remain eligible. They may move anywhere in the movement\n"
                                      "region (their bounding box grown by --move-margin) that the hard constraints allow.\n"
                                      "Context within 3 mm of that region (pads, vias, holes, other tracks,\n"
                                      "keep-outs, outline) is fixed. Writes\n"
                                      "CASE.json and the private CASE.map.json; reads the board only. ok is false\n"
                                      "(exit 1) when the input already breaks a hard constraint.")
    pl0.add_argument("pcb", metavar="PCB")
    pl0.add_argument("--net", action="append", required=True, metavar="GLOB", help="nets to polish (repeatable)")
    pl0.add_argument("--out", required=True, metavar="CASE.json")
    pl0.add_argument("--layer", metavar="L", help="copper layer (needed when the nets run on several)")
    pl0.add_argument("--region", metavar="X,Y,W,H", help="only track runs inside this box (mm)")
    pl0.add_argument("--order", metavar="NET,..", help="neighbour order instead of the geometric one")
    pl0.add_argument("--spacing", type=float, metavar="MM", help="preferred gap (default 2 x clearance)")
    pl0.add_argument("--edge-margin", type=float, metavar="MM", help="preferred edge margin (default 2.0)")
    pl0.add_argument("--move-margin", type=float, default=2.0, metavar="MM",
                     help="how far the traces may move beyond their bounding box (default 2.0)")
    pl0.add_argument("--snap-tolerance", type=float, metavar="DEG", help="default 1")
    pl0.add_argument("--max-bends", type=int, metavar="N", help="added vertices per trace (default 4)")
    pl0.add_argument("--max-iterations", type=int, metavar="N", help="default 5000; a run usually ends on stagnation first")
    pl0.add_argument("--weight", action="append", metavar="TERM=W", help="merit weight (repeatable)")
    pl0.add_argument("--accept-unmodelled", action="store_true",
                     help="go on despite custom or kat net rules the polisher cannot model")
    pl0.add_argument("--force", action="store_true", help="overwrite the case and map")
    pl4 = plsp.add_parser("apply", formatter_class=_Fmt, help="write a result into a copy of the board",
                          description="Copies PCB (and its project files) to OUT, replaces the polished traces there,\n"
                                      "reads it back against the hard constraints and compares KiCad's DRC with the\n"
                                      "original per net. Never writes PCB. Then run kat pcb check on OUT.")
    pl4.add_argument("pcb", metavar="PCB")
    pl4.add_argument("result", metavar="RESULT.json")
    pl4.add_argument("--map", required=True, metavar="CASE.map.json")
    pl4.add_argument("--out", required=True, metavar="NEW.kicad_pcb")
    pl4.add_argument("--force", action="store_true", help="overwrite OUT")
    pl1 = plsp.add_parser("check", formatter_class=_Fmt, help="preflight a case file",
                          description="The format strictly (every problem listed), then the input geometry of\n"
                                      "every trace against the hard constraints; neighbour gaps in group order.\n"
                                      "ok is false (exit 1) when a trace is already infeasible; recoverable then\n"
                                      "says whether run can start from it (only clearance shortfalls, centrelines\n"
                                      "clear), blocking lists what it cannot repair.")
    pl1.add_argument("case", metavar="CASE.json")
    pl1.add_argument("--group", metavar="G", help="only this group's neighbour pairs")
    pl2 = plsp.add_parser("run", formatter_class=_Fmt, help="optimise one group of a case",
                          description="Continuous moves on the merit's gradient (PyTorch), snaps to 45-degree\n"
                                      "multiples, bounded bends: already-bent shapes with legs on 45-degree multiples\n"
                                      "(corner, then Z, each both ways), then a free collinear midpoint.\n"
                                      "Each settles before its gain is judged; a detour that doubles back is cut\n"
                                      "at its tip. Every move is checked against the hard constraints all the way\n"
                                      "(nothing jumps over an obstacle). Writes\n"
                                      "run.json, history.jsonl and result.json (best feasible state) into OUT.\n"
                                      "A case short of clearance is recovered: no shortfall may grow, rescue\n"
                                      "bends where it stalls; 'recovery' and the result's feasible tell.\n"
                                      "The case is never changed. Needs kat[polish] (torch); --gui kat[polish-gui].",
                          epilog=_polish_options_help())
    pl2.add_argument("case", metavar="CASE.json")
    pl2.add_argument("--out", required=True, metavar="DIR", help="run directory (refused if it holds a run)")
    pl2.add_argument("--group", metavar="G", help="the group to optimise (needed when there are several)")
    pl2.add_argument("--weight", action="append", metavar="TERM=W",
                     help="override a merit weight: angle, spacing, edge, displacement, length, complexity")
    pl2.add_argument("--option", action="append", metavar="KEY=V",
                     help="override a solver option, repeatable; every KEY with its default and meaning "
                          "is listed under 'Solver options' below")
    pl2.add_argument("--max-iterations", type=int, metavar="N")
    pl2.add_argument("--snap-tolerance", type=float, metavar="DEG")
    pl2.add_argument("--max-bends", type=int, metavar="N", help="added vertices per trace")
    pl2.add_argument("--gui", action="store_true",
                     help="inspection window; pink dots mark added vertices; writes only on Export")
    pl2.add_argument("--force", action="store_true", help="overwrite a run in OUT")
    pl3 = plsp.add_parser("replay", formatter_class=_Fmt, help="summary of a finished run, or browse it",
                          description="Counts per event type, rejection reasons, initial and result score.\n"
                                      "--gui browses the recorded history; nothing is written.")
    pl3.add_argument("run", metavar="RUN_DIR")
    pl3.add_argument("--case", metavar="CASE.json", help="the run's input (needed with --gui)")
    pl3.add_argument("--gui", action="store_true")
    s34 = psp.add_parser("lock", formatter_class=_Fmt, help="lock tracks and vias (all, or one net's)",
                         description="Locked copper stays when the router runs again: lock each accepted chunk\n"
                                     "of routing, then route the next.")
    s34.add_argument("pcb", metavar="PCB")
    s34.add_argument("--net", help="only this net")
    s34.add_argument("--unlock", action="store_true", help="unlock instead")
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
                                    "set, fabrication set complete and newer than the board. Warnings judged\n"
                                    "harmless pass with --accept KIND=REASON, edge-mount parts with --overhang.")
    s7.add_argument("pcb", metavar="PCB")
    s7.add_argument("--fab", help="fabrication directory (default fab/<name>-<rev>/)")
    s7.add_argument("--accept", action="append", metavar="KIND=REASON",
                    help="accept DRC warnings of KIND (e.g. via_dangling) for REASON (repeatable)")
    s7.add_argument("--overhang", action="append", metavar="REF",
                    help="a part meant to reach beyond the edge, pads inside (repeatable)")
    s9 = psp.add_parser("stamp", formatter_class=_Fmt, help="copy a checked block layout onto instances",
                        description="Places the instance's parts as on the block board and copies its tracks,\n"
                                    "vias, zones, keep-outs, graphics and board-only parts with the instance's\n"
                                    "nets, into a KiCad group \"/CH_A (NAME)\". Instances are sheet paths\n"
                                    "(hierarchical) or SKiDL block paths (flat, Block field). Stamping again\n"
                                    "replaces the copy. The block board's origin is the top-left corner of\n"
                                    "its outline: --at places it, and --rot turns the block about that point,\n"
                                    "counter-clockwise, so at 90 the block lies above --at. --anchor top-left\n"
                                    "makes --at the top-left corner of the turned outline's bounding box. The\n"
                                    "result gives origin and the turned bounds as outline; --preview tells that\n"
                                    "and the ports (positions, order per edge) without changing the board, and\n"
                                    "works without an instance, for assigning pins first. Refused if the instance is wired differently, if the design's\n"
                                    "net classes on its nets are stricter than the block's, if design parts\n"
                                    "overlap the block's parts (courtyards on the same side, through-hole pads on\n"
                                    "both; --make-room pushes them aside), or if it collides with stamped\n"
                                    "instances per layer (copper within clearance unless the same net, courtyards\n"
                                    "per side, keep-outs): blocks may interlock (kat pcb cell, kat pcb collisions).\n"
                                    "Several instances count by their final geometry, so they may swap places: all\n"
                                    "come off first, then go on in natural order (order in the result); a refusal\n"
                                    "lists every collision and leaves the board as it was.\n"
                                    "--side bottom flips the block: mirrored about its vertical axis, then turned\n"
                                    "by --rot (as seen from the top); --at is the top-left corner of the flipped\n"
                                    "outline, so both sides share one grid. Layers go through --layer-map: auto\n"
                                    "(L(k) <-> L(N+1-k), refused on a stack-up that is not mirror-symmetric unless\n"
                                    "--allow-asymmetric), roles (kat pcb roles on both boards) or explicit\n"
                                    "\"F.Cu=B.Cu,In1.Cu=In2.Cu\". Without --side/--layer-map an instance keeps what\n"
                                    "it was stamped with, so `stamp all` does not flip blocks back.")
    s9.add_argument("pcb", metavar="PCB", help="the design's board")
    s9.add_argument("block", metavar="BLOCK_PCB", help="the checked block board")
    s9.add_argument("instance", nargs="*", metavar="INSTANCE", help="/CH_A ... or all")
    s9.add_argument("--at", metavar="X,Y",
                    help="where the block origin goes (mm), or the corner named by --anchor")
    s9.add_argument("--anchor", choices=("origin", "top-left"),
                    help="what --at places: the block origin, which --rot turns about, or the turned\n"
                         "outline's top-left corner (default: as recorded, else origin)")
    s9.add_argument("--preview", action="store_true",
                    help="only report origin, outline and ports; the board stays as it is")
    s9.add_argument("--step", metavar="DX,DY",
                    help="offset from one instance to the next (mm; default the block's pitch, kat pcb cell)")
    s9.add_argument("--rot", type=float, default=0.0, help="degrees, a multiple of 45")
    s9.add_argument("--no-lock", action="store_true", help="leave the copied copper unlocked")
    s9.add_argument("--make-room", action="store_true", help="move design parts out of the block's way")
    s9.add_argument("--sites", metavar="FILE",
                    help="CSV (instance,x,y[,rot,side,twin]) or JSON list: every instance at its own site, one batch")
    s9.add_argument("--twin", action="append", metavar="TOP=BOTTOM",
                    help="also stamp BOTTOM mirrored on the other side at TOP's site (repeatable)")
    s9.add_argument("--zone-priority", type=int, metavar="N",
                    help="add N to the block's pour priorities, above a board-wide pour (default: as recorded, else 0)")
    s9.add_argument("--side", choices=("top", "bottom", "F", "B"), help="side of the block's parts, top/F or bottom/B (default: as recorded, else top)")
    s9.add_argument("--layer-map", metavar="MAP", help='auto (default), roles, or "F.Cu=B.Cu,In1.Cu=In2.Cu,..."')
    s9.add_argument("--allow-asymmetric", action="store_true",
                    help="flip with the auto map although the stack-up is not mirror-symmetric")
    s32 = psp.add_parser("stitch", formatter_class=_Fmt, help="stitching vias of a net on a grid",
                         description="Vias of NET on a grid inside the outline where they clear other copper,\n"
                                     "holes and courtyards: they tie the net's pours on all layers together.")
    s32.add_argument("pcb", metavar="PCB")
    s32.add_argument("net", metavar="NET")
    s32.add_argument("--pitch", type=float, default=5.0, metavar="MM", help="grid pitch (default 5)")
    s32.add_argument("--move-margin", type=float, default=2.0, metavar="MM", help="from the outline's box (default 2)")
    s32.add_argument("--no-lock", action="store_true", help="leave the vias unlocked")
    s33 = psp.add_parser("plane-stub", formatter_class=_Fmt, help="SMD pads to their plane by stub and via",
                         description="Each SMD pad of the parts gets a short stub to a via of its net: the nearest\n"
                                     "existing via within 3 mm a clear stub reaches, else a new one outward from the\n"
                                     "part. warnings: nets with no pour on another copper layer.")
    s33.add_argument("pcb", metavar="PCB")
    s33.add_argument("ref", nargs="+", metavar="REF")
    s33.add_argument("--net", action="append", metavar="NET", help="only these nets (repeatable)")
    s33.add_argument("--skip-net", action="append", metavar="NET", help="never these nets (repeatable)")
    s33.add_argument("--no-lock", action="store_true", help="leave stubs and vias unlocked")
    s31 = psp.add_parser("profile", formatter_class=_Fmt, help="the board's fabrication profile",
                         description="What the supplier can make, held for the whole flow: via types and the\n"
                                     "minimum clearance (also the board minimum DRC uses). A generated DRC rule\n"
                                     "disallows the other via kinds; b.via and route_vias refuse them, netclass\n"
                                     "refuses a clearance below the minimum (below_profile=True records an\n"
                                     "exception), stamp warns about a block's departures, pcb check lists them\n"
                                     "(profile, not clean), pcb ready makes them errors, pcb fab refuses forbidden\n"
                                     "vias. Without options: show it and the board's departures.")
    s31.add_argument("pcb", metavar="PCB")
    s31.add_argument("--via-types", metavar="KINDS", help="through,blind,buried,micro (comma-separated)")
    s31.add_argument("--min-clearance", type=float, metavar="MM", help="e.g. 0.125")
    s31.add_argument("--clear", action="store_true", help="remove the profile")
    s30 = psp.add_parser("stamps", formatter_class=_Fmt, help="the board's stamp records; --rebuild lost ones",
                         description="Where each block instance was stamped (block, at, rot, side, origin,\n"
                                     "outline, layer map, ports), kept in the board file. --rebuild recovers\n"
                                     "records from the stamped groups and footprints (e.g. an older board whose\n"
                                     "<board>.stamps.json is lost); the instances named are redone, else only\n"
                                     "those without a record. Stamping still needs the schematic beside the board\n"
                                     "to pair a hierarchical design's footprints.")
    s30.add_argument("pcb", metavar="PCB")
    s30.add_argument("instance", nargs="*", metavar="INSTANCE", help="with --rebuild: redo these")
    s30.add_argument("--rebuild", action="store_true", help="recover records from the board and save them")
    s17 = psp.add_parser("collisions", formatter_class=_Fmt, help="collisions between stamped block instances",
                         description="Stamped instances pairwise, per layer: copper closer than its clearance\n"
                                     "(the same net, e.g. a shared GND, is allowed), courtyards overlapping on a\n"
                                     "side, copper or parts in a keep-out. Through-hole pads and vias count on\n"
                                     "every layer they span; pours are no obstacle. A table of rows i, j\n"
                                     "(instances), layer, kind, a, b (items), nets, gap, need (mm), x, y.")
    s17.add_argument("pcb", metavar="PCB")
    s17.add_argument("instance", nargs="*", metavar="INSTANCE", help="only these instances (default all)")
    s18 = psp.add_parser("cell", formatter_class=_Fmt, help="a block's pitch, per-layer footprint, min pitch",
                         description="The block's pitch (its stamping grid, NAME.block.json; stamp's default\n"
                                     "--step) is separate from what it takes per layer: copper with clearance per\n"
                                     "copper layer, courtyards per side, keep-outs per layer (block coordinates,\n"
                                     "outline's top-left at 0,0; outside: how far past the outline). Copies may\n"
                                     "interlock where these do not collide.\n"
                                     "--min-pitch x|y|arc: the smallest collision-free pitch of copies in a row\n"
                                     "(mm; arc: degrees about --center); --side alternate puts every second copy on\n"
                                     "the bottom (through --layer-map). Nets the copies share (same net may touch):\n"
                                     "--shared globs, default power nets and nets outside any sheet.")
    s18.add_argument("block", metavar="BLOCK_PCB", help="block board or library block name")
    s18.add_argument("--pitch", metavar="DX,DY", help="set the block's pitch (mm)")
    s18.add_argument("--clear", action="store_true", help="remove the block's pitch")
    s18.add_argument("--min-pitch", choices=("x", "y", "arc"), help="search the smallest collision-free pitch")
    s18.add_argument("--center", metavar="CX,CY", help="arc centre in block coordinates (mm)")
    s18.add_argument("--side", choices=("top", "bottom", "F", "B", "alternate"), default="top",
                     help="side of the copies (default top)")
    s18.add_argument("--layer-map", metavar="MAP", help='bottom copies: auto (default) or "F.Cu=B.Cu,..."')
    s18.add_argument("--allow-asymmetric", action="store_true",
                     help="flip with the auto map on a stack-up that is not mirror-symmetric")
    s18.add_argument("--shared", action="append", metavar="GLOB", help="net the copies share (repeatable)")
    s18.add_argument("--save", action="store_true", help="store the found pitch as the block's pitch")
    s14 = psp.add_parser("roles", formatter_class=_Fmt, help="read or set what each copper layer is for",
                         description="Layer roles, e.g. a block: L1=in_signal L2=gnd_ref L3=out L4=out\n"
                                     "L5=gnd_ref L6=back_signal (as seen from its component side); a design:\n"
                                     "L1=in_signal_top ... L6=in_signal_bottom (side-specific roles with _top /\n"
                                     "_bottom, the rest plain). stamp --layer-map roles pairs layers by role, from\n"
                                     "the component side inwards; net rules may name roles as layers. Stored in\n"
                                     "<board>.kat.json. Without roles: prints them.")
    s14.add_argument("pcb", metavar="PCB"); s14.add_argument("role", nargs="*", metavar="LAYER=ROLE")
    s15 = psp.add_parser("stack", formatter_class=_Fmt, help="stack-up, layer roles, symmetry",
                         description="Copper and dielectric thicknesses (mm), εr and material top to bottom\n"
                                     "(KiCad's default build-up when the board stores none), the layer roles,\n"
                                     "and why the stack-up is not mirror-symmetric (asymmetry).")
    s15.add_argument("pcb", metavar="PCB")
    s16 = psp.add_parser("netrule", formatter_class=_Fmt, help="kat's net rules checked by pcb check",
                         description="Rules DRC does not know, checked by kat pcb check (net_rules; any finding\n"
                                     "makes the board not clean). Nets are a glob over net names (OUT*, /*/IN)\n"
                                     "or class:NAME. A net's index is what the glob's first wildcard matched (OUT3\n"
                                     "-> 3); nets of the same index are one channel and do not couple.\n"
                                     "  coupling A B [--distance D]  copper of A within D mm (default 0.3) of B of\n"
                                     "                               another index, same or adjacent layer; overlap\n"
                                     "                               area, length and parallel-plate pF (stack-up);\n"
                                     "                               a block board's harness pads are left out\n"
                                     "                               (--harness: include them); --estimate 2d:\n"
                                     "                               side-by-side copper gets pF from a 2D\n"
                                     "                               cross-section solve (pip install kat[field])\n"
                                     "  layers NETS LAYER...         NETS only on these layers (names, L1, roles);\n"
                                     "                               stamp refuses blocks that break it; --via-span ends:\n"
                                     "                               a via may pass through other layers, ignore: vias do\n"
                                     "                               not count; --pads: pads count too\n"
                                     "  plane NETS [--net GND]       a pour on an adjacent layer under every track,\n"
                                     "                               except in --except-zone NAME / --except-zone-glob\n"
                                     "                               GLOB / --except-area\n"
                                     "  list | clear                 show or remove the rules\n"
                                     "Stored in <board>.kat.json.")
    s16.add_argument("pcb", metavar="PCB")
    s16.add_argument("kind", choices=("coupling", "layers", "plane", "list", "clear"), metavar="KIND")
    s16.add_argument("args", nargs="*", metavar="ARG")
    s16.add_argument("--distance", type=float, default=0.3, help="coupling: distance in mm (default 0.3)")
    s16.add_argument("--harness", action="store_true", help="coupling: include a block board's harness parts")
    s16.add_argument("--estimate", choices=("plate", "2d"), default="plate",
                     help="coupling: plate (overlap only, default) or 2d (also side by side; numpy/scipy)")
    s16.add_argument("--via-span", choices=("strict", "ends", "ignore"), default="strict",
                     help="layers: a via counts on all layers it spans (default), only the two it joins, or not")
    s16.add_argument("--pads", action="store_true", help="layers: pads count too")
    s16.add_argument("--net", help="plane: the plane's net (default: any other net)")
    s16.add_argument("--except-zone", action="append", metavar="NAME",
                     help="plane: no pour needed inside this named zone / rule area (repeatable)")
    s16.add_argument("--except-zone-glob", action="append", metavar="GLOB",
                     help="plane: the same for every zone / rule area whose name matches (repeatable)")
    s16.add_argument("--except-area", action="append", metavar="X0,Y0,X1,Y1",
                     help="plane: no pour needed inside this rectangle, mm (repeatable)")
    s25 = psp.add_parser("escape", formatter_class=_Fmt, help="route permutable pins, choosing the assignment",
                         description="Freerouting takes each net as given. Where pins may be swapped, as on a\n"
                                     "connector, a min-cost flow on a grid chooses assignment and tracks together, as\n"
                                     "many paths as can exist, of the least total length. On one layer they cannot\n"
                                     "cross, so no vias are needed. SPEC is a JSON file:\n"
                                     '  {"sources": ["J1.*"], "targets": ["U1.1", "U1.2"], "layers": ["F.Cu"]}\n'
                                     'Also: "vias": true (exactly two layers), "track", "clearance", "via", "via_drill",\n'
                                     '"grid", "region" [x0, y0, x1, y1], "margin". "pairs" are reported as not_handled.\n'
                                     "Connectivity belongs to the design, so: plan with this, give the assignment to\n"
                                     "kat back-annotate, then run this again with --apply. Tracks go at right angles.")
    s25.add_argument("pcb", metavar="PCB")
    s25.add_argument("spec", metavar="SPEC", help="JSON file")
    s25.add_argument("--apply", action="store_true", help="add the tracks, once the pads have the nets; then check")
    s25.add_argument("--out", metavar="FILE", help="also write the result as JSON")
    s24 = psp.add_parser("access", formatter_class=_Fmt, help="can candidate cells' ports reach a source?",
                         description="Before detailed routing: a coarse maze on a grid per layer, from the source\n"
                                     "outwards. Fitting by area cannot see a port walled in by its neighbours. SPEC is\n"
                                     "a JSON file:\n"
                                     '  {"board": "top.kicad_pcb", "layers": ["F.Cu", "In2.Cu"],\n'
                                     '   "cells": [{"name": "A", "outline": [10, 10, 20, 20], "blocked_layers": ["F.Cu"],\n'
                                     '              "ports": [{"name": "in", "at": [10, 15], "layers": ["F.Cu"]}]},\n'
                                     '             {"block": "RC", "at": [30, 10], "instance": "/CH_A"}],\n'
                                     '   "keepouts": [{"box": [0, 0, 5, 5], "layers": ["F.Cu"]}],\n'
                                     '   "source": {"box": [0, 40, 60, 50]}, "grid": 0.5}\n'
                                     'Without "board": "outline": [x0, y0, x1, y1], "board_layers": [...]. A port is\n'
                                     "reachable (with length and vias), blocked (with walled_by) or sealed (no free\n"
                                     "space near it). overlaps and outside name cells that do not fit. Each port is\n"
                                     "judged alone: all may still not fit through one gap. Exit code 1 if any fails.")
    s24.add_argument("spec", metavar="SPEC", help="JSON file")
    s23 = psp.add_parser("mate", formatter_class=_Fmt, help="check two boards as they mate",
                         description="Board B over board A, in A's frame: mirrored about its y axis with --flip (its\n"
                                     "top then faces A), turned by --rot, moved by --at. For each --pair, pads and\n"
                                     "guide holes must coincide (by position, not by pin number), the connectors must\n"
                                     "be on the sides that face each other, and mated pins must carry the same net\n"
                                     "name, or the one given by --net. Nets joined through the connectors are listed\n"
                                     "in shorts; a pin connected on one board only in open, which does not fail.\n"
                                     "Hole-only footprints need a partner if inside the other board's outline\n"
                                     "(--holes auto), or name them with --holes A=B, or --holes none.\n"
                                     "Exit code 1 when it does not fit.")
    s23.add_argument("pcb", metavar="BOARD_A"); s23.add_argument("other", metavar="BOARD_B")
    s23.add_argument("--pair", action="append", metavar="REF_A=REF_B", help="mating connectors (repeatable)")
    s23.add_argument("--at", default="0,0", metavar="X,Y", help="B's origin in A's frame (mm, default 0,0)")
    s23.add_argument("--rot", type=float, default=0.0, help="degrees, counter-clockwise")
    s23.add_argument("--flip", action="store_true", help="B lies with its top towards A")
    s23.add_argument("--below", action="store_true", help="B lies under A's bottom side")
    s23.add_argument("--net", action="append", metavar="NET_A=NET_B", help="nets that differ by intent (repeatable)")
    s23.add_argument("--holes", nargs="+", default=["auto"], metavar="HOLES", help="auto (default), none or REF_A=REF_B...")
    s23.add_argument("--tolerance", type=float, default=0.05, metavar="MM", help="pins (default 0.05)")
    s23.add_argument("--hole-tolerance", type=float, default=0.1, metavar="MM", help="guide and mounting holes (default 0.1)")
    s22 = psp.add_parser("import-mech", formatter_class=_Fmt, help="board edge and holes from fabrication data",
                         description="From an ODB++ job (profile and drill layers), or an outline Gerber and\n"
                                     "Excellon files (G85 and M15 slots), so that a new board fits an old one's\n"
                                     "mechanics. The outline replaces Edge.Cuts; holes become board-only footprints\n"
                                     "MH1, ... replacing those of an earlier import. Vias, holes smaller than\n"
                                     "--min-drill and holes on existing pads are left out. Plating comes from the\n"
                                     "files; holes that do not say are non-plated, with a warning, unless\n"
                                     "--assume-plated. Coordinates stay as given unless --at or --offset. Stroke\n"
                                     "ends within --tolerance join, a stroke's centre line is the edge, and chains\n"
                                     "that do not close are reported in open. Ends with the board check.")
    s22.add_argument("pcb", metavar="PCB", help="the .kicad_pcb file (created if missing)")
    s22.add_argument("--odb", metavar="JOB", help="ODB++ directory or archive")
    s22.add_argument("--step", help="ODB++ step, if several have a profile")
    s22.add_argument("--gerber", metavar="FILE", help="outline Gerber")
    s22.add_argument("--drill", action="append", metavar="FILE", help="Excellon file (repeatable)")
    s22.add_argument("--at", metavar="X,Y", help="where the outline's top-left corner goes (mm)")
    s22.add_argument("--offset", metavar="DX,DY", help="shift everything by this (mm)")
    s22.add_argument("--vias", action="store_true", help="also import holes marked as vias")
    s22.add_argument("--min-drill", type=float, default=0.0, metavar="MM", help="leave out smaller holes")
    s22.add_argument("--annular", type=float, default=0.25, metavar="MM", help="ring of plated holes (default 0.25)")
    s22.add_argument("--assume-plated", action="store_true", help="holes that do not say are plated")
    s22.add_argument("--assume-non-plated", action="store_true", help="holes that do not say are non-plated, no warning needed")
    s22.add_argument("--tolerance", type=float, default=0.01, metavar="MM", help="stroke ends that still join (default 0.01)")
    s22.add_argument("--dry-run", action="store_true", help="report only, save nothing")
    s21 = psp.add_parser("odb-pinout", formatter_class=_Fmt, help="pin-to-net mapping of a legacy board's ODB++ job",
                         description="From the component layers and EDA data of an ODB++ job: a directory, .zip,\n"
                                     ".tgz, .tar.gz or .tar (read in memory, never unpacked; files may be plain,\n"
                                     ".z or .gz). Per component part, package, side, position (mm, y down) and\n"
                                     "pins, and the nets. --out writes a ref,pin,net table that kat map checks a\n"
                                     "new design against and a SKiDL script can read, or the whole as .json.")
    s21.add_argument("job", metavar="JOB", help="ODB++ directory or archive")
    s21.add_argument("--step", help="which step, if several have EDA data")
    s21.add_argument("--ref", action="append", metavar="REF", help="only this component (repeatable)")
    s21.add_argument("--out", metavar="FILE", help="also write .csv (ref,pin,net) or .json")
    s20 = psp.add_parser("clearance", formatter_class=_Fmt, help="effective clearance between two nets or pads",
                         description="Resolved as KiCad's DRC does: the larger of the two classes and the board\n"
                                     "minimum, or the last matching custom rule, which may also lower them. Rules\n"
                                     "depending on more than the nets are listed as unresolved, and counted by\n"
                                     "worst_case. Also gives the hole limits.")
    s20.add_argument("pcb", metavar="PCB")
    s20.add_argument("a", metavar="A", help="a net or pad (U1.8)")
    s20.add_argument("b", metavar="B", help="a net or pad")
    s20.add_argument("--layer", help="for rules limited to a layer")
    s19 = psp.add_parser("pads", formatter_class=_Fmt, help="exact pad shapes, for code outside kat",
                         description="Position, size, corner radius, chamfers, trapezoid delta, drill and offset,\n"
                                     "custom primitives and per-layer shapes of pads, mm and degrees.")
    s19.add_argument("pcb", metavar="PCB")
    s19.add_argument("ref", nargs="*", metavar="REF", help="footprints or pads (U1, U1.8); default all")
    s10 = psp.add_parser("instances", formatter_class=_Fmt, help="instances of a block in a design")
    s10.add_argument("pcb", metavar="PCB"); s10.add_argument("block", metavar="BLOCK_PCB")
    s11 = psp.add_parser("environment", formatter_class=_Fmt, help="name a block board's test-environment zones",
                         description="Zones and keep-outs listed here (the plane the block was checked against,\n"
                                     "keep-outs standing for the neighbouring blocks) are not copied by stamp.\n"
                                     "Stored in NAME.block.json next to the block board.")
    s11.add_argument("block", metavar="BLOCK_PCB"); s11.add_argument("zone", nargs="*", metavar="ZONE")
    s13 = psp.add_parser("remove", formatter_class=_Fmt, help="remove single tracks / vias (locked too)",
                         description="By uuid (from kat pcb check / the board file) or what lies at X,Y; locked\n"
                                     "items go too, because they are named. The board is checked afterwards.")
    s13.add_argument("pcb", metavar="PCB")
    s13.add_argument("--uuid", action="append", help="uuid of a track, via or zone (repeatable)")
    s13.add_argument("--at", metavar="X,Y", help="what lies at this point (mm)")
    s13.add_argument("--layer", help="only on this copper layer")
    s13.add_argument("--kind", choices=("track", "via"), help="only tracks or only vias")
    s13.add_argument("--net", help="only this net")
    s12 = psp.add_parser("port", formatter_class=_Fmt, help="mark where a port leaves a block board",
                         description="The via or pad at X,Y on the port's net is the port's point, or the end\n"
                                     "of a track that leaves at the outline (within the copper-edge clearance\n"
                                     "plus half its width; no via needed): kat block check checks it (there, on\n"
                                     "its net, near the outline's edge), stamp returns it on the design (ports;\n"
                                     "a track port with its layer), so the top level is routed to known points.")
    s12.add_argument("block", metavar="BLOCK_PCB"); s12.add_argument("port", metavar="PORT")
    s12.add_argument("--at", required=True, metavar="X,Y", help="the via, pad or track end (mm)")
    s12.add_argument("--net", help="refuse unless the copper there is on this net")

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
                            ("build", "run the harness, sync schematic and board, record ports/params;\n"
                                      "refused before anything is written while a block file is open in KiCad,\n"
                                      "and when its schematic was changed by hand against the last build's\n"
                                      "design (parts, values, wiring; arrangement is fine): --overwrite"),
                            ("check", "schematic, gate (harness parts exempt), board and port points; stored for stamp"),
                            ("list", "the library's blocks and their check state")):
        b = bsp.add_parser(action, formatter_class=_Fmt, help=summary, description=summary)
        if action != "list":
            b.add_argument("name", metavar="NAME")
        b.add_argument("--lib", help="library folder (default: KAT_BLOCKS or the nearest blocks/)")
        if action == "build":
            b.add_argument("--force", action="store_true", help="pass the board sync gate")
            b.add_argument("--overwrite", action="store_true",
                           help="let the design undo hand changes to the schematic (they are listed otherwise)")
        if action == "check":
            b.add_argument("--sides", choices=("top", "both"), default="top",
                           help="both: also check the block flipped to the bottom side (stamp --side bottom)")
            b.add_argument("--layer-map", metavar="MAP", help='flip map: auto (default) or "F.Cu=B.Cu,..."')
            b.add_argument("--allow-asymmetric", action="store_true",
                           help="flip with the auto map on a stack-up that is not mirror-symmetric")
            b.add_argument("--require-field", action="append", metavar="NAME",
                           help="field the gate requires of the block's parts (repeatable; default MPN)")
            b.add_argument("--no-require", action="store_true", help="gate requires no fields")
    s4 = psp.add_parser("view", formatter_class=_Fmt, help="zoomed PNG of one board region",
                        description="Into views/ next to the board; not a history step. Layer sets:\n"
                                    "top, bottom, placement, copper, copper-top, copper-bottom, assembly,\n"
                                    "assembly-bottom (fab + courtyard, pads sketched), In1, ... or KiCad layer\n"
                                    "names. --no-texts leaves the reference/value texts out (block scale).\n"
                                    "A pixel maps back to board mm as x = region[0] + px / px_per_mm.")
    s4.add_argument("pcb", metavar="PCB")
    s4.add_argument("--around", metavar="REF"); s4.add_argument("--at", metavar="X,Y")
    s4.add_argument("--region", metavar="X,Y,W,H")
    s4.add_argument("--layers", default="top", help="set name or F.Cu,B.Cu,... (default top)")
    s4.add_argument("--no-texts", action="store_true", help="hide the footprints' reference, value and user texts")
    s4.add_argument("--margin", type=float); s4.add_argument("--ppm", type=float, help="px per mm")
    return p


# Lookups, repository bootstrap, and commands that write their own journal entry.
NOT_JOURNALED = ("api", "lib", "init", "comment", "batch-exec")


def _robust_streams() -> None:
    """Text that the console's encoding lacks (εr, µm on a cp1252 console) is escaped
    instead of raising UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _robust_streams()
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
