"""History steps: rendered, checked copies of the schematic under ``history/``."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

from . import checks, env, render
from .env import tidy


def snapshot(sch: Path | str, slug: str = "", note: str | None = None,
             expect: Path | str | None = None, out: Path | str | None = None,
             zoom: float = 3.0, tiles: bool | None = None, finding_crops: int = 0) -> dict:
    """Write ``history/<timestamp>[_slug]/`` (or under ``out``): SVG, trimmed PNG, a copy of
    the schematic, ``check.json`` and ``note.md``; ``tiles/`` for large sheets (see
    ``render.snapshot``); with ``finding_crops`` = N, ``findings/NN_kind.png`` close-ups of
    the first N findings that have a position."""
    sch = Path(sch)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = ("_" + "".join(c if c.isalnum() or c in "-_" else "-" for c in slug)) if slug else ""
    target = Path(out or sch.parent / "history") / f"{stamp}{slug}"
    result = render.snapshot(sch, target, zoom, tiles)
    shutil.copy(sch, target / sch.name)
    res = tidy(checks.run(sch, Path(expect) if expect else None))
    (target / "check.json").write_text(json.dumps(res, indent=1))
    if note:
        (target / "note.md").write_text(note + "\n")
    if finding_crops:
        from .views import finding_spots, view
        result["finding_crops"] = []
        for i, (what, at) in enumerate(finding_spots(res)[:finding_crops], 1):
            crop = view(sch, at=at, margin=12, out=target / "findings" / f"{i:02d}_{what}.png")
            result["finding_crops"].append({"png": crop["png"], "what": what, "at": list(at)})
    nl = res.get("netlist_vs_expected")
    result.update({"dir": str(target), "ok": res["ok"], "clean": res["clean"],
                   "netlist_match": nl["match"] if nl else None,
                   "erc_errors": res["erc"]["errors"], "erc_warnings": res["erc"]["warnings"],
                   "findings": {k: len(res[k]) for k in ("overlaps", "body_overlaps", "off_page",
                                                          "duplicates")}})
    return result


def restore(sch: Path | str, step: Path | str, expect: Path | str | None = None) -> dict:
    """Put the schematic back to a history step (``history/<timestamp>_<slug>/``) and check
    it. Undo for a pass that broke something."""
    sch, step = Path(sch), Path(step)
    saved = step / sch.name
    if not saved.is_file():
        raise env.KatError(f"{step} holds no {sch.name}")
    shutil.copy(saved, sch)
    res = tidy(checks.run(sch, Path(expect) if expect else None))
    return {"restored": str(saved), "ok": res["ok"], "clean": res["clean"]}
