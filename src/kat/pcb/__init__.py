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
from ..env import tidy
from . import checks, syncer, views
from .board import Board, Footprint, Pad

__all__ = ["Board", "Footprint", "Pad", "block_environment", "check", "drc", "edit", "fab",
           "instances", "load", "ready", "refill", "route", "snapshot", "stamp", "sync", "view"]


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
          at: tuple[float, float], step: tuple[float, float] = (0.0, 0.0), rot: float = 0.0,
          lock: bool = True) -> dict:
    """Stamp a checked block layout onto instances of the block (``Board.stamp``) and save.
    ``instances``: paths such as ``/CH_A`` (a list, or ``"all"`` for every instance). The
    n-th one goes to ``at + n * step`` (mm), turned by ``rot`` degrees. Ends with the
    board check, like ``sync``."""
    report.touch(pcb)
    with edit(pcb) as b:
        names = b.instances(block_pcb) if instances == "all" else (
            [instances] if isinstance(instances, str) else list(instances))
        done = [b.stamp(block_pcb, name, (at[0] + k * step[0], at[1] + k * step[1]), rot, lock)
                for k, name in enumerate(names)]
    c = checks.run(Path(pcb))
    return tidy({"board": str(pcb), "instances": done,
                 "check": {k: c[k] for k in ("ok", "drc_errors", "unconnected", "parity")}})


def instances(pcb: Path | str, block_pcb: Path | str) -> dict:
    """The instances of a block in a design (``Board.instances``)."""
    report.touch(pcb)
    return {"block": Path(block_pcb).stem, "instances": Board(pcb).instances(block_pcb)}


def block_environment(block_pcb: Path | str, zones: list[str]) -> dict:
    """Name the zones of a block board that are its test environment (e.g. the plane it was
    checked against); ``stamp`` does not copy them. Stored in ``NAME.block.json``."""
    from . import stamping
    return stamping.set_environment(block_pcb, zones)


def fab(pcb: Path | str, out: Path | str | None = None, step: bool = True, three_d: bool = True) -> dict:
    """Fabrication set into fab/<name>-<rev>/: Gerbers + drill (zipped), position file,
    IPC-D-356, BOM with part numbers, STEP, 3D renders."""
    report.touch(pcb)
    from . import fabrication as _fab
    return tidy(_fab.fab(Path(pcb), out, step, three_d))


def ready(pcb: Path | str, fab_dir: Path | str | None = None) -> dict:
    """Release gate: board check clean, title and revision set, fabrication set complete
    and newer than the board."""
    report.touch(pcb)
    from . import fabrication as _fab
    return tidy(_fab.ready(Path(pcb), fab_dir))
