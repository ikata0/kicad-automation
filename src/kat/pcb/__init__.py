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

from ..env import tidy
from . import checks, syncer, views
from .board import Board, Footprint, Pad

__all__ = ["Board", "Footprint", "Pad", "check", "drc", "edit", "fab", "load", "ready", "refill",
           "route", "snapshot", "sync", "view"]


def load(path: Path | str) -> Board:
    return Board(path)


@contextmanager
def edit(path: Path | str, dry_run: bool = False) -> Iterator[Board]:
    """Edit a board; saved once when the block ends without an exception."""
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
    return tidy(syncer.run(Path(pcb), Path(sch) if sch else None, dry_run, force, rip_stale,
                           tuple(require)))


def drc(pcb: Path | str, parity: bool = True, refill: bool = False) -> dict:
    """KiCad's DRC: violations, unconnected items, schematic parity problems."""
    return tidy(checks.drc(Path(pcb), parity, refill))


def check(pcb: Path | str) -> dict:
    """DRC (zones refilled in memory, parity) plus kat's findings; ``ok`` / ``clean``."""
    return tidy(checks.run(Path(pcb)))


def snapshot(pcb: Path | str, slug: str = "", note: str | None = None, out: Path | str | None = None,
             tiles: bool | None = None, finding_crops: int = 0, three_d: bool = True) -> dict:
    """History step with pictures per layer set, tiles, finding close-ups, 3D view, check."""
    return views.snapshot(pcb, slug, note, out, tiles, finding_crops, three_d)


def view(pcb: Path | str, around: str | None = None, at: tuple[float, float] | None = None,
         region: tuple[float, float, float, float] | None = None, layers: str | list[str] = "top",
         margin: float | None = None, ppm: float | None = None, out: Path | str | None = None) -> dict:
    """Zoomed PNG of one board region (see ``kat.pcb.views.view``)."""
    return views.view(pcb, around, at, region, layers, margin, ppm, out)


def refill(pcb: Path | str) -> dict:
    """Fill all pours and save them into the board (KiCad's zone filler, via DRC)."""
    d = checks.drc(Path(pcb), parity=False, refill=True, save=True)
    return tidy({"file": str(pcb), "drc_errors": d["errors"], "unconnected": len(d["unconnected"])})


def route(pcb: Path | str, passes: int = 20, nets: list[str] | None = None, timeout: int = 1800,
          out: Path | str | None = None, install: bool = False) -> dict:
    """Autoroute with Freerouting (headless) through kat's Specctra DSN/SES; locked copper
    stays, ``nets`` limits what is routed. Ends with KiCad's DRC (``check``): trust that,
    not the router's own count. ``install`` downloads Freerouting first if missing."""
    from . import router as _route
    if install:
        _route.install()
    return tidy(_route.route(Path(pcb), passes, nets, timeout, out))


def fab(pcb: Path | str, out: Path | str | None = None, step: bool = True, three_d: bool = True) -> dict:
    """Fabrication set into fab/<name>-<rev>/: Gerbers + drill (zipped), position file,
    IPC-D-356, BOM with part numbers, STEP, 3D renders."""
    from . import fabrication as _fab
    return tidy(_fab.fab(Path(pcb), out, step, three_d))


def ready(pcb: Path | str, fab_dir: Path | str | None = None) -> dict:
    """Release gate: board check clean, title and revision set, fabrication set complete
    and newer than the board."""
    from . import fabrication as _fab
    return tidy(_fab.ready(Path(pcb), fab_dir))
