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
from . import checks, syncer
from .board import Board, Footprint, Pad

__all__ = ["Board", "Footprint", "Pad", "drc", "edit", "load", "sync"]


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
