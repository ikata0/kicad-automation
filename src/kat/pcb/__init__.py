"""Board (PCB) side of kat: board model, geometry, footprint libraries, sync and checks.

    import kat
    kat.pcb.sync("board/board.kicad_pcb")          # parts and nets from board.kicad_sch
    with kat.pcb.edit("board/board.kicad_pcb") as b:
        b.footprint("R1").place(30, 20, rot=90)
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .. import report
from ..env import KatError, tidy
from . import checks, syncer, views
from .board import Board, Footprint, Pad

__all__ = ["Board", "Footprint", "Pad", "block_environment", "block_port", "remove", "check", "coupling_rule",
           "drc", "edit", "fab", "instances", "layer_roles", "layer_rule", "load", "net_rules", "plane_rule",
           "ready", "refill", "route", "snapshot", "stack", "stamp", "sync", "view"]


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


def check(pcb: Path | str) -> dict:
    """DRC (zones refilled in memory, parity) plus kat's findings; ``ok`` / ``clean``."""
    report.touch(pcb)
    return tidy(checks.run(Path(pcb)))


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
         margin: float | None = None, ppm: float | None = None, out: Path | str | None = None) -> dict:
    """Zoomed PNG of one board region into ``views/``: ``around`` a footprint, ``at`` a
    point (x, y) or a ``region`` (x, y, w, h) in mm; ``layers`` a set name (top, bottom,
    placement, copper, In1, ...) or KiCad layer names; ``ppm`` px/mm. Journaled with its
    image."""
    res = views.view(pcb, around, at, region, layers, margin, ppm, out)
    what = around or (f"at {at[0]:g},{at[1]:g}" if at else f"region {region}")
    report.snapshot_images(pcb, res, f"kat.pcb.view({str(pcb)!r}, {what}, layers={layers!r})", "view")
    return res


def refill(pcb: Path | str) -> dict:
    """Fill all pours and save them into the board (KiCad's zone filler, via DRC)."""
    report.touch(pcb)
    d = checks.drc(Path(pcb), parity=False, refill=True, save=True)
    return tidy({"file": str(pcb), "drc_errors": d["errors"], "unconnected": len(d["unconnected"])})


def route(pcb: Path | str, passes: int = 20, nets: list[str] | None = None, timeout: int = 1800,
          out: Path | str | None = None, install: bool = False) -> dict:
    """Autoroute with Freerouting (headless) through kat's Specctra DSN/SES; locked copper
    stays, ``nets`` limits what is routed. Ends with KiCad's DRC (``check``): trust that,
    not the router's own count. ``install`` downloads Freerouting first if missing."""
    report.touch(pcb)
    from . import router as _route
    if install:
        _route.install()
    return tidy(_route.route(Path(pcb), passes, nets, timeout, out))


def stamp(pcb: Path | str, block_pcb: Path | str, instances: str | list[str],
          at: tuple[float, float], step: tuple[float, float] | None = None, rot: float = 0.0,
          lock: bool = True, make_room: bool = False, side: str | None = None,
          layer_map: str | None = None, allow_asymmetric: bool = False) -> dict:
    """Stamp a checked block layout onto instances of the block (``Board.stamp``) and save.
    ``instances``: paths such as ``/CH_A`` (a list, or ``"all"`` for every instance). The
    n-th one goes to ``at + n * step`` (mm; default step: the block's pitch, ``block_cell``,
    else 0,0), turned by ``rot`` degrees. ``make_room``:
    push the design's parts out of the block parts' way instead of refusing. ``side``
    "top"/"bottom" (None: as recorded per instance, else top), ``layer_map`` "auto",
    "roles" or ``"F.Cu=B.Cu,..."``, ``allow_asymmetric``: see ``Board.stamp``. Ends with
    the board check, like ``sync``."""
    report.touch(pcb)
    if step is None:
        from .. import blocks as lib
        from .stamping import block_info
        step = tuple(block_info(lib.board_of(block_pcb, near=Path(pcb))[0]).get("pitch", (0.0, 0.0)))
    with edit(pcb) as b:
        names = b.instances(block_pcb) if instances == "all" else (
            [instances] if isinstance(instances, str) else list(instances))
        done = [b.stamp(block_pcb, name, (at[0] + k * step[0], at[1] + k * step[1]), rot, lock,
                        make_room=make_room, side=side, layer_map=layer_map,
                        allow_asymmetric=allow_asymmetric)
                for k, name in enumerate(names)]
    c = checks.run(Path(pcb))
    return tidy({"board": str(pcb), "instances": done,
                 "check": {k: c[k] for k in ("ok", "drc_errors", "unconnected", "parity")}})


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
    net, stored in ``NAME.block.json``. ``kat block check`` checks the port points,
    ``stamp`` returns them on the design (``Board.ports``)."""
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


def coupling_rule(pcb: Path | str, a: str, b: str, distance: float = 0.3) -> dict:
    """``kat pcb check`` flags copper of nets ``a`` (glob such as ``OUT*``, or
    ``class:NAME``) overlapping or within ``distance`` mm of copper of nets ``b`` of
    another index (the glob's wildcard part) on the same or an adjacent layer, with the
    overlap area and a parallel-plate capacitance (pF) from the stack-up. Returns the
    board's net rules."""
    from . import coupling
    report.touch(pcb)
    return {"board": str(pcb), "net_rules": coupling.coupling_rule(pcb, a, b, distance)}


def layer_rule(pcb: Path | str, nets: str, layers: list[str]) -> dict:
    """Nets ``nets`` may only lie on ``layers`` (KiCad names, ``L1``..., layer roles).
    Checked by ``kat pcb check`` and by ``stamp`` on what it copies. Returns the net rules."""
    from . import coupling
    report.touch(pcb)
    return {"board": str(pcb), "net_rules": coupling.layer_rule(pcb, nets, layers)}


def plane_rule(pcb: Path | str, nets: str, net: str | None = None) -> dict:
    """Tracks of ``nets`` need a pour of ``net`` (None: any other net) on an adjacent layer
    under their whole length; ``kat pcb check`` reports the uncovered length. Returns the
    net rules."""
    from . import coupling
    report.touch(pcb)
    return {"board": str(pcb), "net_rules": coupling.plane_rule(pcb, nets, net)}


def net_rules(pcb: Path | str, clear: bool = False) -> dict:
    """The board's net rules (coupling, layers, plane); ``clear`` removes them."""
    from . import coupling
    report.touch(pcb)
    return {"board": str(pcb), "net_rules": coupling.net_rules(pcb, clear)}
