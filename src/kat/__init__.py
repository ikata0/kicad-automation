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

from . import checks, gate, library, netlist, pcb, report, skidl_blocks, syncer
from .env import KatError, tidy
from .history import restore as _restore
from .history import snapshot as _snapshot
from .project import new
from .report import batch_exec, comment
from .sheet import Sheet
from .views import view as _view

__all__ = ["KatError", "Sheet", "batch_exec", "check", "comment", "edit", "lib_search", "lib_show",
           "load", "nets", "new", "open_kicad", "pcb", "ready", "restore", "skidl_blocks", "snapshot", "sync",
           "view"]


def load(path: Path | str) -> Sheet:
    """Open a sheet for inspection (or for editing with an explicit ``save()``)."""
    report.touch(path)
    return Sheet(path)


@contextmanager
def edit(path: Path | str, dry_run: bool = False) -> Iterator[Sheet]:
    """Edit a sheet: all changes are saved once when the block ends without an exception.
    On an exception, or with ``dry_run=True``, the file is left untouched."""
    report.touch(path)
    s = Sheet(path)
    yield s
    if not dry_run:
        s.save()


def sync(sch: Path | str, design: Path | str, dry_run: bool = False) -> dict:
    """Bring a design netlist (e.g. from SKiDL) into the schematic, keeping the arrangement."""
    report.touch(sch)
    return tidy(syncer.run(Path(sch), Path(design), dry_run))


def check(sch: Path | str, expect: Path | str | None = None) -> dict:
    """ERC, netlist comparison with ``expect`` and drawing findings (``ok`` / ``clean``)."""
    report.touch(sch)
    return tidy(checks.run(Path(sch), Path(expect) if expect else None))


def ready(sch: Path | str, expect: Path | str | None = None,
          require: tuple[str, ...] | list[str] = ("MPN",)) -> dict:
    """Ready-for-layout gate: ``ready`` is true when the schematic is clean and every board
    part has a value, a footprint that exists and has a pad for every symbol pin, and the
    ``require`` fields (parts excluded from the BOM are exempt). ``errors`` block the
    layout; ``warnings`` (extra pads, footprint filters, 3D models) want a human look."""
    report.touch(sch)
    return tidy(gate.run(Path(sch), Path(expect) if expect else None, tuple(require)))


def snapshot(sch: Path | str, slug: str = "", note: str | None = None,
             expect: Path | str | None = None, out: Path | str | None = None,
             zoom: float = 3.0, tiles: bool | None = None, finding_crops: int = 0) -> dict:
    """Render and check the sheet into ``history/<timestamp>[_slug]/`` (SVG, trimmed PNG,
    schematic copy, check.json, note.md). Large sheets (PNG > 2000 px) also get readable
    ``tiles/`` unless ``tiles=False``; ``finding_crops=N`` adds close-ups of the first N
    findings. Returns the paths and the check summary."""
    res = tidy(_snapshot(sch, slug, note, expect, out, zoom, tiles, finding_crops))
    report.snapshot_images(sch, res, f"kat.snapshot({str(sch)!r}, slug={slug!r})", slug or "sch")
    return res


def restore(sch: Path | str, step: Path | str, expect: Path | str | None = None) -> dict:
    """Put the schematic back to a history step (its saved copy) and check it."""
    report.touch(sch)
    return _restore(sch, step, expect)


def view(sch: Path | str, around: str | None = None, at: tuple[float, float] | None = None,
         region: tuple[float, float, float, float] | None = None, margin: float | None = None,
         zoom: float | None = None, out: Path | str | None = None) -> dict:
    """Zoomed PNG of one region: ``around`` a part, ``at`` a point (x, y) or a ``region``
    (x, y, w, h) in sheet mm. Written to ``views/``; not a history step."""
    res = tidy(_view(sch, around, at, region, margin, zoom, out))
    what = around or (f"at {at[0]:g},{at[1]:g}" if at else f"region {region}")
    report.snapshot_images(sch, res, f"kat.view({str(sch)!r}, {what})", "view")
    return res


def open_kicad(path: Path | str, verify: bool = False, timeout: float = 90.0) -> dict:
    """Open a .kicad_sch, .kicad_pcb or .kicad_pro in KiCad's editor. Without ``verify`` it
    stays open for a person (close it before kat edits the file again). With ``verify`` kat
    compares what the editor loaded with its own reading (board: footprints, nets,
    tracks, vias, zones, outline; schematic: opened and running) and closes it without
    saving; returns ``verified`` (None without a display or kicad-python), ``ok``,
    ``mismatches``, ``kicad``."""
    from . import gui
    report.touch(path)
    return gui.verify(path, timeout) if verify else gui.open_file(path)


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
