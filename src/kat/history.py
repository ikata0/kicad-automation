"""History steps: rendered, checked copies of the schematic under ``history/``."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from . import checks, render


def snapshot(sch: Path | str, slug: str = "", note: str | None = None,
             expect: Path | str | None = None, out: Path | str | None = None,
             zoom: float = 3.0) -> dict:
    """Write ``history/<timestamp>[_slug]/`` (or under ``out``): SVG, trimmed PNG, a copy of
    the schematic, ``check.json`` and ``note.md``."""
    sch = Path(sch)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = ("_" + "".join(c if c.isalnum() or c in "-_" else "-" for c in slug)) if slug else ""
    target = Path(out or sch.parent / "history") / f"{stamp}{slug}"
    result = render.snapshot(sch, target, zoom)
    shutil.copy(sch, target / sch.name)
    res = checks.run(sch, Path(expect) if expect else None)
    (target / "check.json").write_text(json.dumps(res, indent=1))
    if note:
        (target / "note.md").write_text(note + "\n")
    result.update({"dir": str(target), "ok": res["ok"], "erc_errors": res["erc"]["errors"],
                   "erc_warnings": res["erc"]["warnings"], "overlaps": len(res["overlaps"])})
    return result
