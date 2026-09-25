"""kat — edit and verify KiCad 10 schematics from Python or the command line.

Library first: the ``kat`` command is a thin layer over these functions, and every
function returns the JSON-able result the matching command prints.

    import kat

    with kat.edit("board/board.kicad_sch") as s:     # saved once at the end,
        s.move("R1", to=(127, 86.36), rot=90)        # nothing saved on an exception
        s.detach("R1.1")
        s.connect("R1.1", "U1.2")
    kat.snapshot("board/board.kicad_sch", slug="pass2", expect="design.net")

Points are ``"REF.PIN"`` or ``(x, y)`` in mm (y grows downwards). See ``Sheet`` for the
editing verbs and SPEC.md for the workflow.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import checks, library, netlist, syncer
from .env import KatError, tidy
from .history import snapshot as _snapshot
from .project import new
from .sheet import Sheet

__all__ = ["KatError", "Sheet", "check", "edit", "lib_search", "lib_show", "load", "nets",
           "new", "snapshot", "sync"]


def load(path: Path | str) -> Sheet:
    """Open a sheet for inspection (or for editing with an explicit ``save()``)."""
    return Sheet(path)


@contextmanager
def edit(path: Path | str, dry_run: bool = False) -> Iterator[Sheet]:
    """Edit a sheet: all changes are saved once when the block ends without an exception.
    On an exception, or with ``dry_run=True``, the file is left untouched."""
    s = Sheet(path)
    yield s
    if not dry_run:
        s.save()


def sync(sch: Path | str, design: Path | str, dry_run: bool = False) -> dict:
    """Bring a design netlist (e.g. from SKiDL) into the schematic, keeping the arrangement."""
    return tidy(syncer.run(Path(sch), Path(design), dry_run))


def check(sch: Path | str, expect: Path | str | None = None) -> dict:
    """ERC, netlist comparison with ``expect`` and drawing findings (``ok`` / ``clean``)."""
    return tidy(checks.run(Path(sch), Path(expect) if expect else None))


def snapshot(sch: Path | str, slug: str = "", note: str | None = None,
             expect: Path | str | None = None, out: Path | str | None = None,
             zoom: float = 3.0) -> dict:
    """Render and check the sheet into ``history/<timestamp>[_slug]/`` (SVG, trimmed PNG,
    schematic copy, check.json, note.md). Returns the paths and the check summary."""
    return tidy(_snapshot(sch, slug, note, expect, out, zoom))


def nets(sch: Path | str) -> dict[str, list]:
    """Net name -> sorted [(ref, pin), ...], as KiCad reads the saved file."""
    return {name: sorted(pins) for name, pins in sorted(netlist.from_schematic(Path(sch)).items())}


def lib_search(query: str, limit: int = 30) -> list[dict]:
    """Find symbols: every word must match name, description or keywords."""
    return library.search(query, limit)


def lib_show(lib_id: str) -> dict:
    """Units, pins (library coordinates, y up), fields and body box of a library symbol."""
    from .sexpr import find_all
    sdef = library.resolve(lib_id)
    props = {str(p[1]): str(p[2]) for p in find_all(sdef, "property")}
    units = library.unit_count(sdef)
    return {"lib_id": lib_id, "units": units, "properties": props,
            "body": {u: library.outline(sdef, u) for u in range(1, units + 1)},
            "pins": [vars(p) for p in library.pins(sdef)]}
