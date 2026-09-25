"""Zoomed views of a sheet region, for reviewing details the full image cannot show.

Views are not history steps: they go to ``views/`` next to the schematic. The frameless
SVG they are cut from is cached per file modification time, so a series of views after
one edit exports the sheet only once.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from . import env, render
from .checks import text_items
from .schematic import Schematic


def _svg(sch: Path) -> Path:
    cache = sch.parent / "views" / ".cache"
    stamp = sch.stat().st_mtime_ns
    svg = cache / f"{sch.stem}.{stamp}.svg"
    if not svg.exists():
        for old in cache.glob(f"{sch.stem}.*.svg"):
            old.unlink()
        tmp = cache / "export"
        exported = render.export_svg(sch, tmp, frame=False)
        exported[0].replace(svg)
        for f in tmp.glob("*"):
            f.unlink()
        tmp.rmdir()
    return svg


def part_box(s: Schematic, ref: str) -> tuple[float, float, float, float]:
    """Body boxes of all placed units of ``ref``, its visible field texts, and the labels
    anchored within 7.62 mm of its body (the net names on its pins)."""
    bodies = [b for sym in s.find_symbols(ref) if (b := s.body_box(sym))]
    if not bodies:
        raise env.KatError(f"{ref} has no drawn body")
    near = lambda x, y: any(b[0] - 7.62 <= x <= b[2] + 7.62 and b[1] - 7.62 <= y <= b[3] + 7.62 for b in bodies)
    labels = {(l["x"], l["y"], l["text"]) for l in s.labels() if near(l["x"], l["y"])}
    boxes = list(bodies)
    for t in text_items(s):
        if t["owner"] == ref or (t["owner"] is None and any(
                t["text"] == txt and t["box"][0] - 1e-3 <= x <= t["box"][2] + 1e-3 and
                t["box"][1] - 1e-3 <= y <= t["box"][3] + 1e-3 for x, y, txt in labels)):
            boxes.append(t["box"])
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def view(sch: Path | str, around: str | None = None, at: tuple[float, float] | None = None,
         region: tuple[float, float, float, float] | None = None, margin: float | None = None,
         zoom: float | None = None, out: Path | str | None = None) -> dict:
    """Render one region of the sheet as PNG. Give exactly one of
    ``around`` (a reference: the part, its fields and ``margin`` mm, default 10),
    ``at`` ((x, y): a square of ±``margin`` mm, default 15) or
    ``region`` ((x, y, w, h) in sheet mm).

    ``zoom`` defaults to a scale that makes the longer side about 1600 px (at least 2, at
    most 12). The result gives the PNG, its size, the region and ``px_per_mm``, so a pixel
    position maps back to sheet mm as ``x = region[0] + px / px_per_mm``."""
    sch = Path(sch)
    if sum(v is not None for v in (around, at, region)) != 1:
        raise env.KatError("give exactly one of around, at, region")
    if around is not None:
        m = 10.0 if margin is None else margin
        b = part_box(Schematic(sch), around)
        box, what = (b[0] - m, b[1] - m, b[2] + m, b[3] + m), around
    elif at is not None:
        m = 15.0 if margin is None else margin
        box, what = (at[0] - m, at[1] - m, at[0] + m, at[1] + m), f"{at[0]:g}_{at[1]:g}"
    else:
        x, y, w, h = region
        if w <= 0 or h <= 0:
            raise env.KatError("region needs a positive width and height")
        box, what = (x, y, x + w, y + h), f"{x:g}_{y:g}_{w:g}x{h:g}"
    if zoom is None:
        longest = max(box[2] - box[0], box[3] - box[1])
        zoom = min(12.0, max(2.0, render.TILE_PX / (longest * render.PX_PER_MM)))
    if out is None:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out = sch.parent / "views" / f"{stamp}_{re.sub(r'[^A-Za-z0-9_.-]', '-', what)}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = render.render_region(_svg(sch), out, box, zoom)
    return {"png": str(out), "width": w, "height": h,
            "region": [round(box[0], 2), round(box[1], 2), round(box[2] - box[0], 2), round(box[3] - box[1], 2)],
            "zoom": round(zoom, 3), "px_per_mm": round(zoom * render.PX_PER_MM, 3)}


def finding_spots(check: dict) -> list[tuple[str, tuple[float, float]]]:
    """(label, position) of every check finding that has one: ERC, overlaps, bodies."""
    spots = []
    for v in check.get("erc", {}).get("violations", []):
        for it in v.get("items", [])[:1]:
            if "x" in it:
                spots.append((f"erc-{v['type']}", (it["x"], it["y"])))
    for o in check.get("overlaps", []):
        spots.append((o["kind"].replace("/", "-"), tuple(o["at"])))
    for o in check.get("body_overlaps", []):
        if "at" in o:
            spots.append(("body", tuple(o["at"])))
    return spots
