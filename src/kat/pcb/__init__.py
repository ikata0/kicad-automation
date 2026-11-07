"""Board (PCB) side of kat: board model, geometry, footprint libraries, sync and checks.

    import kat
    kat.pcb.sync("board/board.kicad_pcb")          # parts and nets from board.kicad_sch
    with kat.pcb.edit("board/board.kicad_pcb") as b:
        b.footprint("R1").place(30, 20, rot=90)
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .. import report
from ..env import KatError, tidy
from . import checks, syncer, views
from .board import Board, Footprint, Pad

__all__ = ["Board", "Footprint", "Pad", "block_environment", "block_port", "remove", "check", "clearance", "coupling_2d", "coupling_rule",
           "drc", "edit", "escape", "fab", "import_mechanics", "instances", "layer_roles", "layer_rule", "load", "mate", "net_rules", "odb_pinout", "plane_rule",
           "pad_geometry", "port_access", "ready", "refill", "route", "snapshot", "stack", "stamp", "stamp_preview", "sync", "view"]


def load(path: Path | str) -> Board:
    """Open a board for inspection (or for editing with an explicit ``save()``)."""
    report.touch(path)
    return Board(path)


@contextmanager
def edit(path: Path | str, dry_run: bool = False) -> Iterator[Board]:
    """Edit a board; saved once when the block ends without an exception. On an exception,
    or with ``dry_run=True``, the file is left untouched."""
    report.touch(path)
    b = Board(path)
    yield b
    if not dry_run:
        b.save()


def sync(pcb: Path | str, sch: Path | str | None = None, dry_run: bool = False,
         force: bool = False, rip_stale: bool = False,
         require: tuple[str, ...] | list[str] = ("MPN",)) -> dict:
    """Bring the schematic's parts and nets onto the board (created if missing), keeping
    placement and copper. Refuses unless ``kat.ready`` passes (``force`` overrides).
    ``sch`` defaults to the board's name with .kicad_sch. Copper that no longer fits its
    pads is reported in ``stale_tracks`` and removed with ``rip_stale``."""
    report.touch(pcb)
    return tidy(syncer.run(Path(pcb), Path(sch) if sch else None, dry_run, force, rip_stale,
                           tuple(require)))


def drc(pcb: Path | str, parity: bool = True, refill: bool = False) -> dict:
    """KiCad's DRC: violations, unconnected items, schematic parity problems."""
    report.touch(pcb)
    return tidy(checks.drc(Path(pcb), parity, refill))


def check(pcb: Path | str, mapping: Path | str | None = None, complete: bool | list[str] = True,
          out: Path | str | None = None) -> dict:
    """DRC (zones refilled in memory, parity) plus kat's findings; ``ok`` / ``clean``.
    ``mapping``: a pin table the pads must also agree with, and ``complete`` what it must cover
    (``kat.mapping.check``), reported in ``mapping``. ``out``: also write the result as UTF-8
    JSON to that file (shell redirection may re-encode stdout), named in ``out``."""
    report.touch(pcb)
    res = tidy(checks.run(Path(pcb), Path(mapping) if mapping else None, complete))
    if out is not None:
        Path(out).write_text(json.dumps(res, indent=1, default=list) + "\n", encoding="utf-8")
        res["out"] = str(out)
    return res


def snapshot(pcb: Path | str, slug: str = "", note: str | None = None, out: Path | str | None = None,
             tiles: bool | None = None, finding_crops: int = 0, three_d: bool = True) -> dict:
    """History step ``history/<stamp>_pcb[_slug]/`` with a picture per layer set (top,
    bottom, placement, inner layers), tiles, ``finding_crops`` close-ups, the 3D view and
    the check. Journaled with its overview images."""
    res = views.snapshot(pcb, slug, note, out, tiles, finding_crops, three_d)
    report.snapshot_images(pcb, res, f"kat.pcb.snapshot({str(pcb)!r}, slug={slug!r})", slug or "pcb")
    return res


def view(pcb: Path | str, around: str | None = None, at: tuple[float, float] | None = None,
         region: tuple[float, float, float, float] | None = None, layers: str | list[str] = "top",
         margin: float | None = None, ppm: float | None = None, out: Path | str | None = None,
         texts: bool = True) -> dict:
    """Zoomed PNG of one board region into ``views/``: ``around`` a footprint, ``at`` a
    point (x, y) or a ``region`` (x, y, w, h) in mm; ``layers`` a set name (top, bottom,
    placement, copper, copper-top, copper-bottom, assembly, assembly-bottom, In1, ...) or
    KiCad layer names; ``ppm`` px/mm; ``texts=False`` hides the footprints' reference,
    value and user texts. Journaled with its image."""
    res = views.view(pcb, around, at, region, layers, margin, ppm, out, texts)
    what = around or (f"at {at[0]:g},{at[1]:g}" if at else f"region {region}")
    report.snapshot_images(pcb, res, f"kat.pcb.view({str(pcb)!r}, {what}, layers={layers!r})", "view")
    return res


def refill(pcb: Path | str) -> dict:
    """Fill all pours and save them into the board (KiCad's zone filler, via DRC). A fill
    that DRC finds over other nets' copper is retried once with a nudged minimum width
    (``fill_retry`` in the result; ``checks.refill``)."""
    report.touch(pcb)
    return tidy({"file": str(pcb), **checks.refill(Path(pcb))})


def route(pcb: Path | str, passes: int = 20, nets: list[str] | None = None, timeout: int = 1800,
          out: Path | str | None = None, install: bool = False, skip_classes: list[str] | None = None,
          optimize: bool = True, options: dict | None = None, classes: list[str] | None = None) -> dict:
    """Autoroute with Freerouting (headless) through kat's Specctra DSN/SES; locked copper
    stays, ``nets`` (names or globs, ``/SPI_*``) and ``classes`` (net classes) limit what is
    routed, ``skip_classes`` leaves net classes out. Route in chunks on a dense board:
    constrained nets first, ``lock`` each accepted chunk and check it. Ends with
    KiCad's DRC (``check``): trust that, not the router's own count. ``timeout`` (s): the router
    stops there and keeps what it has (``timed_out``). ``optimize=False`` skips its optimizer.
    ``options``: other Freerouting settings; ``settings`` reports the ones it refused or kat has not
    seen work. Returns also ``stages`` (seconds) and ``unrouted`` per pass. ``install`` downloads
    Freerouting first if missing."""
    report.touch(pcb)
    from . import router as _route
    if install:
        _route.install()
    return tidy(_route.route(Path(pcb), passes, nets, timeout, out, skip_classes, optimize, options, classes))


def lock(pcb: Path | str, net: str | None = None, locked: bool = True) -> dict:
    """Lock (or with ``locked=False`` unlock) the board's tracks and vias, all or one net's
    (``Board.lock``), and save: locked copper stays when the router runs again, so an accepted
    chunk of routing is kept. Returns the count."""
    report.touch(pcb)
    with edit(pcb) as b:
        res = b.lock(net, locked)
    return tidy({"board": str(pcb), **res})


def stamp(pcb: Path | str, block_pcb: Path | str, instances: str | list[str],
          at: tuple[float, float], step: tuple[float, float] | None = None, rot: float = 0.0,
          lock: bool = True, make_room: bool = False, side: str | None = None,
          layer_map: str | None = None, allow_asymmetric: bool = False, anchor: str | None = None,
          zone_priority: int | None = None, twin: dict[str, str] | None = None) -> dict:
    """Stamp a checked block layout onto instances of the block (``Board.stamp``) and save.
    ``instances``: paths such as ``/CH_A`` (a list, or ``"all"`` for every instance). The
    n-th one goes to ``at + n * step`` (mm; default step: the block's pitch, ``block_cell``,
    else 0,0), turned by ``rot`` degrees about that point. ``anchor="top-left"``: that point
    is the turned outline's top-left corner instead of the block origin. ``make_room``:
    push the design's parts out of the block parts' way instead of refusing. ``side``
    "top"/"bottom" (None: as recorded per instance, else top), ``layer_map`` "auto",
    "roles" or ``"F.Cu=B.Cu,..."``, ``allow_asymmetric``, ``zone_priority`` (added to the
    block's pour priorities), ``twin`` ({instance: the instance stamped mirrored on the other
    side at the same site}): see ``Board.stamp``. Ends with
    the board check, like ``sync``. All instances are judged on their final geometry, so they
    may swap places and the outcome does not depend on the order they are processed in
    (``order``, returned with their results); a refusal lists every collision and saves
    nothing. Each instance's result has ``origin`` and ``outline`` (the turned bounds x0, y0,
    x1, y1 mm)."""
    report.touch(pcb)
    step = _step_of(pcb, block_pcb) if step is None else step
    with edit(pcb) as b:
        names = b.instances(block_pcb) if instances == "all" else (
            [instances] if isinstance(instances, str) else list(instances))
        done = b.stamp(block_pcb, {name: ((at[0] + k * step[0], at[1] + k * step[1]), rot)
                                   for k, name in enumerate(names)}, lock=lock, make_room=make_room,
                       side=side, layer_map=layer_map, allow_asymmetric=allow_asymmetric, anchor=anchor,
                       zone_priority=zone_priority, twin=twin)
    c = checks.run(Path(pcb))
    return tidy({"board": str(pcb), "instances": done["instances"], "order": done["order"],
                 "check": {k: c[k] for k in ("ok", "drc_errors", "unconnected", "parity")}})


def stamp_sites(pcb: Path | str, block_pcb: Path | str, sites, lock: bool = True, make_room: bool = False,
                layer_map: str | None = None, allow_asymmetric: bool = False, anchor: str | None = None,
                zone_priority: int | None = None) -> dict:
    """Stamp a block at the sites of a floorplan list (``stamping.read_sites``: a CSV with
    ``instance, x, y[, rot, side, twin]`` columns, a JSON list or the list itself), all in one
    batch as ``stamp`` does: ``twin`` is the instance stamped mirrored on the other side of
    the same site. Saves, then checks the board like ``stamp``."""
    from .stamping import read_sites
    report.touch(pcb)
    rows = read_sites(sites)
    with edit(pcb) as b:
        done = b.stamp(block_pcb, {r["instance"]: (r["at"], r["rot"], r["side"]) for r in rows}, lock=lock,
                       make_room=make_room, layer_map=layer_map, allow_asymmetric=allow_asymmetric, anchor=anchor,
                       zone_priority=zone_priority, twin={r["instance"]: r["twin"] for r in rows if r["twin"]})
    c = checks.run(Path(pcb))
    return tidy({"board": str(pcb), "instances": done["instances"], "order": done["order"],
                 "check": {k: c[k] for k in ("ok", "drc_errors", "unconnected", "parity")}})


def stitch(pcb: Path | str, net: str, pitch: float = 5.0, margin: float = 2.0, lock: bool = True) -> dict:
    """Stitching vias of ``net`` on a grid (``Board.stitch``) and save; returns net, vias,
    points and the board check's ok, drc_errors and unconnected."""
    report.touch(pcb)
    with edit(pcb) as b:
        res = b.stitch(net, pitch, margin, lock)
    c = checks.run(Path(pcb))
    return tidy({"board": str(pcb), **res, "check": {k: c[k] for k in ("ok", "drc_errors", "unconnected")}})


def plane_stub(pcb: Path | str, refs: str | list[str], nets: list[str] | None = None,
               skip_nets: list[str] = (), lock: bool = True) -> dict:
    """A stub and via from each SMD pad of ``refs`` (only ``nets``, never ``skip_nets``) to
    its net's plane on another layer (``Board.fanout``: the nearest via of the net within
    reach first, else a new one) and save. Returns vias, failed, warnings (nets without a
    pour on another layer) and the board check's ok, drc_errors and unconnected."""
    report.touch(pcb)
    with edit(pcb) as b:
        res = b.fanout(refs, nets, list(skip_nets), lock)
    c = checks.run(Path(pcb))
    return tidy({"board": str(pcb), **res, "check": {k: c[k] for k in ("ok", "drc_errors", "unconnected")}})


def profile(pcb: Path | str, via_types: list[str] | None = None, min_clearance: float | None = None,
            clear: bool = False) -> dict:
    """The board's fabrication profile (``Board.fab_profile``): ``via_types`` the supplier
    makes (through, blind, buried, micro) and ``min_clearance`` (mm). Set, read (no
    arguments) or ``clear`` it; returns ``profile`` and the board's departures from it
    (``departures``, as ``kat pcb check`` reports them)."""
    from . import fabprofile as prof
    report.touch(pcb)
    if via_types is None and min_clearance is None and not clear:
        b = Board(pcb)
        return tidy({"board": str(pcb), "profile": b.fab_profile(), "departures": prof.findings(b)})
    with edit(pcb) as b:
        p = b.fab_profile(via_types, min_clearance, clear)
    return tidy({"board": str(pcb), "profile": p, "departures": prof.findings(Board(pcb))})


def stamps(pcb: Path | str, rebuild: bool | list[str] = False) -> dict:
    """The board's stamp records ({instance: {block, at, rot, side, origin, outline, layer_map,
    ports, ...}}), kept in the board file. ``rebuild``: recover lost ones from the stamped
    groups and footprints (``Board.rebuild_stamps``; a list names instances to redo) and save
    them; the result then also has rebuilt, kept and failed."""
    report.touch(pcb)
    if not rebuild:
        return tidy({"board": str(pcb), "stamps": Board(pcb).stamps()})
    with edit(pcb) as b:
        res = b.rebuild_stamps(None if rebuild is True else list(rebuild))
    return tidy({"board": str(pcb), **res, "stamps": Board(pcb).stamps()})


def _step_of(pcb: Path | str, block_pcb: Path | str) -> tuple[float, float]:
    from .. import blocks as lib
    from .stamping import block_info
    return tuple(block_info(lib.board_of(block_pcb, near=Path(pcb))[0]).get("pitch", (0.0, 0.0)))


def stamp_preview(pcb: Path | str, block_pcb: Path | str, instances: str | list[str] | None = None,
                  at: tuple[float, float] = (0.0, 0.0), step: tuple[float, float] | None = None,
                  rot: float = 0.0, side: str | None = None, layer_map: str | None = None,
                  allow_asymmetric: bool = False, anchor: str | None = None) -> dict:
    """What ``stamp`` with these arguments would place, without changing the board
    (``Board.stamp_preview``): per instance ``origin``, ``outline`` (bounds x0, y0, x1, y1
    mm), ``ports`` in the design frame and ``order`` (ports per outline edge). ``instances``
    None: one entry with the block's own net names, for pin assignment before the design
    has the instance. Returns board, instances."""
    report.touch(pcb)
    b = Board(pcb)
    step = _step_of(pcb, block_pcb) if step is None else step
    names = b.instances(block_pcb) if instances == "all" else (
        [instances] if isinstance(instances, str) else list(instances or [None]))
    return tidy({"board": str(pcb), "instances": [
        b.stamp_preview(block_pcb, (at[0] + k * step[0], at[1] + k * step[1]), rot, name, side, layer_map,
                        allow_asymmetric, anchor=anchor) for k, name in enumerate(names)]})


def clearance(pcb: Path | str, a: str, b: str, layer: str | None = None) -> dict:
    """The effective clearance between two nets or pads (``U1.8``), resolved as KiCad's DRC
    does (``Board.clearance``): class, board minimum or the last matching custom rule. Returns
    clearance (mm), source, unresolved rules, worst_case (mm) and the hole limits (mm)."""
    report.touch(pcb)
    return tidy({"board": str(pcb), **Board(pcb).clearance(a, b, layer)})


def pad_geometry(pcb: Path | str, refs: list[str] | None = None) -> dict:
    """The exact shape of pads, for clearance and routing code outside kat (``Pad.geometry``;
    mm, degrees): ``refs`` a list of footprints or single pads (``U1``, ``U1.8``), default
    all. Returns board, pads."""
    report.touch(pcb)
    b = Board(pcb)
    if not refs:
        pads = b.pads()
    else:
        pads = []
        for r in refs:
            ref, _, number = r.partition(".")
            got = [p for p in b.footprint(ref).pads() if not number or p.number == number]
            if not got:
                raise KatError(f"{ref} has no pad {number!r}")
            pads += got
    return tidy({"board": str(pcb), "pads": [p.geometry() for p in pads]})


def odb_pinout(job: Path | str, step: str | None = None, refs: list[str] | None = None,
               out: Path | str | None = None) -> dict:
    """Pin-to-net mapping of a legacy board, from an ODB++ job (a directory or archive, never
    unpacked; ``odb.pinout``): per component part, package, side, x, y (mm, y down) and pins, and
    the nets. ``step``: which one, if several have EDA data; ``refs``: only these components.
    ``out``: also write it as a ``ref,pin,net`` table (.csv), which ``kat.mapping.check`` and
    ``kat.skidl_blocks.pin_map`` read, or as JSON (.json); adds ``out`` to the result."""
    from . import odb
    from ..mapping import write
    res = odb.pinout(job, step, refs)
    if out is not None:
        out = Path(out)
        if out.suffix.lower() == ".json":
            import json
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
        else:
            write(out, {f"{r}.{pin}": net for r, c in res["components"].items() for pin, net in c["pins"].items()})
        res["out"] = str(out)
    return res


def import_mechanics(pcb: Path | str, odb: Path | str | None = None, gerber: Path | str | None = None,
                     drills: list[Path | str] | None = None, step: str | None = None,
                     at: tuple[float, float] | None = None, offset: tuple[float, float] | None = None,
                     vias: bool = False, min_drill: float = 0.0, annular: float = 0.25,
                     assume: str | None = None, tolerance: float = 0.01, dry_run: bool = False) -> dict:
    """Board edge, cut-outs and holes from fabrication data: an ODB++ job ``odb`` (``step`` if
    several), an outline ``gerber`` and Excellon ``drills`` (G85 and M15 slots). The outline
    replaces Edge.Cuts and the holes replace those of an earlier import, as board-only footprints
    MH1, ...; the board is created if missing. Coordinates stay as the files give them (y down);
    ``at`` puts the outline's top-left corner there, or ``offset`` shifts all (mm). Holes marked as
    vias or smaller than ``min_drill`` (mm) are left out unless ``vias``; a hole on an existing pad
    is left out. ``annular``: ring of a plated hole (mm). ``assume``: "plated" or "non_plated" for
    holes whose file does not say (default non_plated, with a warning). ``tolerance``: mm between
    stroke ends that still join. ``dry_run``: report, save nothing. Returns board, loops, cutouts,
    outline, offset, holes {plated, non_plated, slots, skipped}, open (chains that did not close),
    widths, warnings and the board check."""
    from . import mechanics
    report.touch(pcb)
    if not (odb or gerber or drills):
        raise KatError("name an ODB++ job, an outline Gerber or drill files")
    if odb and (gerber or drills):
        raise KatError("an ODB++ job has outline and holes: give it alone")
    loops, holes, opened, widths, warnings = [], [], [], [], []
    if odb:
        got = mechanics.read_odb(odb, step)
        loops, holes, warnings = got["loops"], got["holes"], got["warnings"]
    if gerber:
        got = mechanics.read_gerber(gerber, tolerance)
        loops, opened, widths = got["loops"], got["open"], got["widths"]
        warnings += got["warnings"]
        if not loops:
            raise KatError(f"{Path(gerber).name} has no closed outline: {len(opened)} open chains "
                           "(tolerance= joins ends further apart)")
    for d in drills or []:
        got = mechanics.read_excellon(d)
        holes += got["holes"]
        warnings += got["warnings"]
    if Path(pcb).exists():
        b = Board(pcb)
    elif dry_run:
        raise KatError(f"{pcb} does not exist: a dry run has no board to report on")
    else:
        Path(pcb).parent.mkdir(parents=True, exist_ok=True)
        b = Board.create(pcb)
    res = mechanics.apply(b, loops, holes, at, offset, vias, min_drill, annular, assume)
    res["warnings"] = warnings + res["warnings"]
    res["open"] = [{"from": list(c[0][1]), "to": list(c[-1][-1]), "segments": len(c)} for c in opened]
    if res["open"]:
        res["warnings"].append(f"{len(opened)} open chains left out")
    if widths:
        res["widths"] = widths
    if not dry_run:
        b.save()
    out = {"board": str(pcb), "dry_run": dry_run, **res}
    if not dry_run:
        c = checks.run(Path(pcb))
        out["check"] = {k: c[k] for k in ("ok", "drc_errors", "unconnected", "parity", "by_type")}
    return tidy(out)


def mate(a: Path | str, b: Path | str, pairs: list[tuple[str, str]], at: tuple[float, float] = (0.0, 0.0),
         rot: float = 0.0, flip: bool = False, nets: dict[str, str] | None = None, holes="auto",
         above: bool = True, tolerance: float = 0.05, hole_tolerance: float = 0.1) -> dict:
    """Check two boards as they mate, in board A's frame. Board B is mirrored about its y axis if
    ``flip`` (its top then faces A), turned by ``rot`` degrees and moved by ``at`` (mm). ``pairs``:
    mating connectors [(ref on A, ref on B), ...]; ``above``: B lies over A's top side, else under
    its bottom, which sets the side each connector must be on.

    Pads and guide holes of a pair mate by position within ``tolerance`` and ``hole_tolerance``
    (mm), not by pin number. Nets of mated pins must have the same name, or the name in ``nets``
    ({net on A: net on B}); generated names only count for ``shorts``. ``holes``: "auto" compares
    hole-only footprints, each of which needs a partner if it lies inside the other board's
    outline; a list of (ref on A, ref on B) names them; None skips them.

    Returns ok, pairs (pins with nets and offsets), holes, misaligned, wrong_side, net_mismatch,
    shorts (nets joined through the connectors), holes_unmatched, open (a pin connected on one board
    only, which does not fail ok) and counts."""
    from .mating import mate as _mate
    return _mate(a, b, pairs, at, rot, flip, nets, holes, above, tolerance, hole_tolerance)


def escape(pcb: Path | str, spec: dict | Path | str, apply: bool = False, out: Path | str | None = None) -> dict:
    """Route a group of permutable pins and choose their assignment with the tracks: a min-cost flow
    on a grid gives the most vertex-disjoint paths possible at the least total length. On one layer
    they cannot cross, so a planar escape needs no vias, which Freerouting cannot offer because it
    takes nets as given. ``spec``, a dictionary or JSON file: ``sources`` and ``targets`` (pads ``J1.3``
    or ``J1.*``), ``layers`` (default F.Cu), ``vias`` (default false, true needs exactly two layers),
    ``track``, ``clearance``, ``via``, ``via_drill`` (mm, default the board's Default class), ``grid``
    (mm, default the tightest legal), ``region`` [x0, y0, x1, y1] or ``margin`` (mm around the pins).
    ``pairs``, fixed pairs, are reported in ``not_handled``: they are for ``route``.

    Returns assignment {source: target's net}, routes, unrouted_sources, unrouted_targets, max_paths,
    total_length (mm) and via_count. It changes nothing unless ``apply``, which adds the tracks and
    checks the board. That needs the pads to have the nets already, because connectivity belongs to the
    design: run ``kat.mapping.back_annotate`` with ``assignment`` first. ``out``: also write the
    result as JSON. Tracks follow the grid at right angles; KiCad's DRC judges them."""
    from . import flowroute
    return flowroute.escape(pcb, spec, apply, out)


def port_access(spec: dict | Path | str) -> dict:
    """Can the ports of candidate cells reach a source, before detailed routing? A coarse maze on a
    grid per layer: fitting by area cannot see a port walled in by its neighbours. ``spec`` is a
    dictionary or a JSON file with: ``board`` (a .kicad_pcb, giving edge, cut-outs, keep-outs and
    parts; ``parts`` false leaves parts out) or ``outline`` ([x0, y0, x1, y1] mm, with ``board_layers``
    and ``edge_clearance``); ``layers`` routing may use; ``cells`` [{name, outline [x0, y0, x1, y1] or
    polygon, blocked_layers (default all), ports [{name, at [x, y] or x and y, layers}]}], or for a
    library block {block, at, rot, side, instance, anchor}, placed as ``stamp`` would with its port
    points; ``keepouts`` [{name, box or polygon, layers}]; ``source`` {box, layers}. Options: ``grid``
    (0.5 mm), ``track`` and ``clearance`` (0.2 mm), ``via`` (0.6 mm), ``vias`` (true), ``escape`` (how
    far a port may be from free space, mm), ``path`` (true: corner points of each route).

    Returns ok, ports [{cell, port, status reachable with length (mm) and vias, blocked with
    walled_by, or sealed with why}], counts, overlaps (cells sharing a layer), outside (cells past
    the edge) and warnings. Each port is judged alone: all of them may still not fit through one
    gap, so routing and DRC stay the judges."""
    from . import floorplan
    if not isinstance(spec, dict):
        spec = {**floorplan._read(spec), "_base": str(Path(spec).resolve().parent)}
    return floorplan.access(spec)


def instances(pcb: Path | str, block_pcb: Path | str) -> dict:
    """The instances of a block in a design (``Board.instances``)."""
    report.touch(pcb)
    return {"block": Path(block_pcb).stem, "instances": Board(pcb).instances(block_pcb)}


def stamp_collisions(pcb: Path | str, instances: list[str] | None = None) -> dict:
    """Collisions between the stamped block instances of a design, pairwise per layer
    (``Board.stamp_collisions``): a table of rows i, j, layer, kind, a, b, nets, gap,
    need (mm), x, y. ``instances``: only those of these instances."""
    report.touch(pcb)
    rows = Board(pcb).stamp_collisions(instances)
    return {"board": str(pcb), "collisions": rows, "ok": not rows}


def block_cell(block_pcb: Path | str, pitch: tuple[float, float] | None = None,
               clear: bool = False) -> dict:
    """A block's cell: its stamping ``pitch`` (dx, dy mm; set it with ``pitch``, remove it
    with ``clear``; ``stamp`` uses it as the default step) and its per-layer footprint,
    computed from the block board in block coordinates (mm, outline's top-left corner at
    0,0): per copper layer the box of the copper grown by half its clearance and of the
    keep-outs, per side the courtyards; ``outside``: how far each reaches past the
    outline (what interlocks with a neighbour)."""
    from .. import blocks as lib
    from . import cells
    from .stamping import block_info
    block_pcb = lib.board_of(block_pcb)[0]
    report.touch(block_pcb)
    if pitch is not None or clear:
        cells.set_pitch(block_pcb, None if clear else pitch)
    return {"block": block_pcb.stem, "pitch": block_info(block_pcb).get("pitch"),
            **cells.footprint(block_pcb)}


def min_pitch(block_pcb: Path | str, axis: str = "x", center: tuple[float, float] | None = None,
              side: str = "top", layer_map: str | None = None, allow_asymmetric: bool = False,
              shared: list[str] | None = None, save: bool = False) -> dict:
    """The smallest pitch at which copies of a block in a row do not collide, per layer
    (``cells.min_pitch``): ``axis`` "x"/"y" (mm) or "arc" about ``center`` (block
    coordinates, degrees); ``side`` top, bottom or alternate; ``shared``: globs of nets
    the copies share (default power nets). ``save``: store it as the block's pitch
    (x/y only). Returns pitch, outline, interlock (outline - pitch) and what limits it."""
    from .. import blocks as lib
    from . import cells
    block_pcb = lib.board_of(block_pcb)[0]
    report.touch(block_pcb)
    out = cells.min_pitch(block_pcb, axis, center, side, layer_map, allow_asymmetric, shared)
    if save:
        if axis == "arc":
            raise KatError("an arc pitch is an angle: save takes x or y pitches only")
        cells.set_pitch(block_pcb, (out["pitch"], 0.0) if axis == "x" else (0.0, out["pitch"]))
        out["saved"] = True
    return out


def block_environment(block_pcb: Path | str, zones: list[str]) -> dict:
    """Name the zones and keep-outs of a block board that are its test environment (the
    plane it was checked against; keep-outs that stand for the neighbouring blocks, so it
    is routed as it will sit); ``stamp`` does not copy them. Stored in ``NAME.block.json``."""
    from . import stamping
    return stamping.set_environment(block_pcb, zones)


def remove(pcb: Path | str, uuids: list[str] | None = None, at: tuple[float, float] | None = None,
           layer: str | None = None, kind: str | None = None, net: str | None = None) -> dict:
    """Remove single tracks, vias or zones (locked ones too): by ``uuids``, or what lies
    ``at`` a point (x, y mm; only ``layer`` / ``kind`` "track"|"via" / ``net`` if given).
    Saves, then checks the board."""
    report.touch(pcb)
    if not uuids and at is None:
        raise KatError("remove needs uuids or at")
    with edit(pcb) as b:
        res = b.remove(uuids) if uuids else b.remove_at(at, layer, kind, net)
    c = checks.run(Path(pcb))
    return tidy({"board": str(pcb), **res,
                 "check": {k: c[k] for k in ("ok", "drc_errors", "unconnected", "parity")}})


def block_port(block_pcb: Path | str, port: str, at: tuple[float, float], net: str | None = None) -> dict:
    """Mark where a port leaves a block board: the via or pad at ``at`` (mm) on the port's
    net, or the end of a track that leaves at the outline's edge (within the copper-edge
    clearance plus half its width), stored in ``NAME.block.json``. ``kat block check``
    checks the port points, ``stamp`` returns them on the design (``Board.ports``; a
    track port with its layer). Returns port, net, kind (via, pad, track), at."""
    from . import stamping
    report.touch(block_pcb)
    return stamping.set_port(block_pcb, port, at, net)


def fab(pcb: Path | str, out: Path | str | None = None, step: bool = True, three_d: bool = True) -> dict:
    """Fabrication set into fab/<name>-<rev>/: Gerbers + drill (zipped), position file,
    IPC-D-356, BOM with part numbers, STEP, 3D renders."""
    report.touch(pcb)
    from . import fabrication as _fab
    return tidy(_fab.fab(Path(pcb), out, step, three_d))


def ready(pcb: Path | str, fab_dir: Path | str | None = None, accept: dict[str, str] | None = None,
          overhang: list[str] | None = None) -> dict:
    """Release gate: board check clean, title and revision set, fabrication set complete
    and newer than the board. ``accept``: DRC warning kinds judged harmless, with the
    reason ({"via_dangling": "port vias joined on one layer"}); ``overhang``: parts
    meant to reach beyond the board edge (edge-mount connectors). The result lists what
    was accepted; errors are never accepted."""
    report.touch(pcb)
    from . import fabrication as _fab
    return tidy(_fab.ready(Path(pcb), fab_dir, accept, overhang))


def layer_roles(pcb: Path | str, roles: dict[str, str] | None = None) -> dict:
    """Read or set what each copper layer is for (``{"L1": "in_signal", ...}``; a design
    names side-specific roles ``in_signal_top`` / ``in_signal_bottom``). Used by
    ``stamp(layer_map="roles")`` and by layer rules. Stored in ``<board>.kat.json``;
    returns {layer: role}."""
    from . import layers
    report.touch(pcb)
    return layers.set_layer_roles(pcb, roles)


def stack(pcb: Path | str) -> dict:
    """The board's stack-up (copper and dielectric thicknesses in mm, εr, material; KiCad's
    default if none is stored), its layer roles and why it is not mirror-symmetric
    (``asymmetry``, empty if it is)."""
    from . import layers
    report.touch(pcb)
    b = Board(pcb)
    return {"board": str(pcb), **layers.stackup(b), "roles": layers.set_layer_roles(pcb),
            "asymmetry": layers.asymmetry(b)}


def coupling_rule(pcb: Path | str, a: str, b: str, distance: float = 0.3, harness: bool = False,
                  estimate: str = "plate") -> dict:
    """``kat pcb check`` flags copper of nets ``a`` (glob such as ``OUT*``, or
    ``class:NAME``) overlapping or within ``distance`` mm of copper of nets ``b`` of
    another index (the glob's wildcard part) on the same or an adjacent layer, with the
    overlap area and a parallel-plate capacitance (pF) from the stack-up. ``estimate="2d"``
    adds a 2D cross-section capacitance (pF, length mm) for side-by-side copper on one
    layer (needs numpy/scipy: kat[field]). On a block's board the harness parts' pads are
    exempt unless ``harness``. Returns the board's net rules."""
    from . import coupling
    report.touch(pcb)
    return {"board": str(pcb), "net_rules": coupling.coupling_rule(pcb, a, b, distance, harness, estimate)}


def coupling_2d(width_a: float, width_b: float, gap: float, below: float, above: float | None = None,
                epsilon_r: float = 4.5, copper: float = 0.035) -> dict:
    """Mutual capacitance per length of two tracks side by side on one layer, from a 2D
    cross-section (finite differences, needs numpy/scipy): widths and edge ``gap`` in mm,
    reference plane ``below`` mm under them, on an inner layer one ``above`` mm over them
    (None: outer layer, air above). Returns pf_per_mm (about 10 % accurate)."""
    from . import field2d
    return {"pf_per_mm": round(field2d.side_by_side(width_a, width_b, gap, below, above, epsilon_r, copper), 6)}


def layer_rule(pcb: Path | str, nets: str, layers: list[str], via_span: str = "strict",
               pads: bool = False) -> dict:
    """Nets ``nets`` may only lie on ``layers`` (KiCad names, ``L1``..., layer roles).
    ``via_span``: a via counts on every layer it spans (``strict``), only on the two it joins
    (``ends``: it may pass through others) or not at all (``ignore``). ``pads``: pads count too.
    Checked by ``kat pcb check`` and by ``stamp`` on what it copies. Returns the net rules."""
    from . import coupling
    report.touch(pcb)
    return {"board": str(pcb), "net_rules": coupling.layer_rule(pcb, nets, layers, via_span, pads)}


def plane_rule(pcb: Path | str, nets: str, net: str | None = None, except_zones: list[str] | None = None,
               except_areas: list[tuple[float, float, float, float]] | None = None,
               except_globs: list[str] | None = None) -> dict:
    """Tracks of ``nets`` need a pour of ``net`` (None: any other net) on an adjacent layer
    under their whole length; ``kat pcb check`` reports the uncovered length. Track inside
    a zone/rule area named in ``except_zones``, one whose name matches a glob of
    ``except_globs`` (``/*: cut*``, decided at check time) or a rectangle of ``except_areas``
    (x0, y0, x1, y1 mm) is exempt (an intended cut-out). Returns the net rules; ``warnings``
    names globs matching nothing yet."""
    from . import coupling
    report.touch(pcb)
    rules = coupling.plane_rule(pcb, nets, net, except_zones, except_areas, except_globs)
    none = coupling.unmatched_globs(pcb, except_globs or [])
    return {"board": str(pcb), "net_rules": rules,
            **({"warnings": [f"--except-zone-glob {g!r} matches no zone or rule area yet" for g in none]}
               if none else {})}


def net_rules(pcb: Path | str, clear: bool = False) -> dict:
    """The board's net rules (coupling, layers, plane); ``clear`` removes them."""
    from . import coupling
    report.touch(pcb)
    return {"board": str(pcb), "net_rules": coupling.net_rules(pcb, clear)}
