"""Board pictures: snapshots (history steps) and zoomed views, cut from kicad-cli SVGs.

The SVG is exported in page coordinates (``--page-size-mode 1``), so SVG units are board
mm and the schematic's viewBox cropping (``render.region_svg``) works unchanged. Pours
are filled for the picture (``--check-zones``) without touching the file.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import shutil
from pathlib import Path

from .. import env, render
from ..env import tidy
from . import checks
from .board import Board
from .geometry import union

# layer sets: what one picture shows
SETS = {
    "top": ["F.Cu", "F.SilkS", "Edge.Cuts"],
    "bottom": ["B.Cu", "B.SilkS", "Edge.Cuts"],
    "placement": ["F.CrtYd", "F.Fab", "B.CrtYd", "B.Fab", "Edge.Cuts"],
    "copper": ["B.Cu", "F.Cu", "Edge.Cuts"],   # bottom-up: later layers are drawn on top
}
OVERVIEW_PX = 2000   # longest side of an overview picture
TILE_PPM = 30.0      # px/mm of tiles: a 0.2 mm track is 6 px wide


def layer_sets(b: Board) -> dict[str, list[str]]:
    sets = dict(SETS)
    for name in b.copper_layers():
        if name.startswith("In"):
            sets[name.split(".")[0]] = [name, "Edge.Cuts"]
    sets["copper"] = list(reversed(b.copper_layers())) + ["Edge.Cuts"]
    return sets


def _layers(b: Board, layers: str | list[str]) -> tuple[str, list[str]]:
    if isinstance(layers, list):
        return "-".join(l.split(".")[0] for l in layers), layers
    sets = layer_sets(b)
    if layers in sets:
        return layers, sets[layers]
    return layers.replace(",", "-"), layers.split(",")


def export_svg(pcb: Path, layers: list[str], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    env.run_cli("pcb", "export", "svg", "--mode-single", "--layers", ",".join(layers),
                "--page-size-mode", "1", "--exclude-drawing-sheet", "--check-zones",
                "-o", str(out), str(pcb))
    return out


def _svg(pcb: Path, name: str, layers: list[str]) -> Path:
    """Cached SVG of a layer set, per board modification time."""
    import hashlib
    cache = pcb.parent / "views" / ".cache"
    stamp = pcb.stat().st_mtime_ns
    key = hashlib.sha1(",".join(layers).encode()).hexdigest()[:8]  # same name, other layers
    svg = cache / f"{pcb.stem}.{name}.{stamp}.{key}.svg"
    if not svg.exists():
        for old in cache.glob(f"{pcb.stem}.{name}.*.svg"):
            old.unlink()
        export_svg(pcb, layers, svg)
    return svg


def board_area(b: Board, margin: float = 3.0) -> tuple[float, float, float, float]:
    """Outline plus everything placed (parts still in the import area included)."""
    box = union([b.outline_box()] + [f.box() for f in b.footprints()])
    if box is None:
        raise env.KatError("empty board: nothing to show")
    return box[0] - margin, box[1] - margin, box[2] + margin, box[3] + margin


def _zoom_for(ppm: float) -> float:
    return ppm / render.PX_PER_MM


def _render(svg: Path, png: Path, box, ppm: float) -> dict:
    png.parent.mkdir(parents=True, exist_ok=True)
    w, h = render.render_region(svg, png, box, _zoom_for(ppm))
    return {"png": str(png), "width": w, "height": h, "px_per_mm": round(ppm, 3),
            "region_mm": [round(v, 2) for v in box]}


def _tiles(svg: Path, out_dir: Path, area, ppm: float) -> list[dict]:
    size = render.TILE_PX / ppm
    step = size * 0.9
    x0, y0, x1, y1 = area
    nx = max(1, math.ceil((x1 - x0 - size) / step) + 1)
    ny = max(1, math.ceil((y1 - y0 - size) / step) + 1)
    out = []
    for i in range(ny):
        for j in range(nx):
            box = (x0 + j * step, y0 + i * step, min(x0 + j * step + size, x1), min(y0 + i * step + size, y1))
            out.append(_render(svg, out_dir / f"r{i + 1}c{j + 1}.png", box, ppm))
    return out


def render_3d(pcb: Path, png: Path, side: str = "top") -> str | None:
    """KiCad's 3D view; None if the renderer is unavailable (no OpenGL, e.g. headless CI)."""
    proc = env.run_cli("pcb", "render", "--side", side, "-w", "1600", "-h", "1200",
                       "--background", "opaque", "-o", str(png), str(pcb), check=False)
    return str(png) if proc.returncode == 0 and png.exists() else None


def snapshot(pcb: Path | str, slug: str = "", note: str | None = None, out: Path | str | None = None,
             tiles: bool | None = None, finding_crops: int = 0, three_d: bool = True) -> dict:
    """History step ``history/<stamp>_pcb[_slug]/``: board copy, ``check.json``, ``note.md``
    and one picture per layer set (top, bottom, placement, inner layers), the whole board
    at about 2000 px. Tiles (``tiles/<set>/r<i>c<j>.png`` at 30 px/mm) for the top and
    bottom copper when the overview is coarser than that; ``finding_crops`` close-ups of
    the first N findings; KiCad's 3D view from top and bottom."""
    pcb = Path(pcb)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = ("_" + re.sub(r"[^A-Za-z0-9_-]", "-", slug)) if slug else ""
    target = Path(out or pcb.parent / "history") / f"{stamp}_pcb{slug}"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy(pcb, target / pcb.name)
    b = Board(pcb)
    area = board_area(b)
    longest = max(area[2] - area[0], area[3] - area[1])
    ppm = min(40.0, OVERVIEW_PX / longest)
    images, tile_list = {}, {}
    for name, layers in layer_sets(b).items():
        if name == "copper":
            continue
        svg = export_svg(pcb, layers, target / "svg" / f"{name}.svg")
        images[name] = _render(svg, target / f"{name}.png", area, ppm)
        want = tiles if tiles is not None else ppm < TILE_PPM
        if want and name in ("top", "bottom"):
            tile_list[name] = _tiles(svg, target / "tiles" / name, area, TILE_PPM)
    res = tidy(checks.run(pcb))
    (target / "check.json").write_text(json.dumps(res, indent=1))
    if note:
        (target / "note.md").write_text(note + "\n")
    result = {"dir": str(target), "images": images}
    if tile_list:
        result["tiles"] = tile_list
    if finding_crops:
        result["finding_crops"] = []
        for i, (what, at) in enumerate(checks.finding_spots(res)[:finding_crops], 1):
            v = view(pcb, at=at, margin=4, layers="copper",
                     out=target / "findings" / f"{i:02d}_{what}.png")
            result["finding_crops"].append({"png": v["png"], "what": what, "at": list(at)})
    if three_d:
        result["3d"] = [p for side in ("top", "bottom")
                        if (p := render_3d(pcb, target / f"3d_{side}.png", side))]
    result.update({k: res[k] for k in ("ok", "clean", "drc_errors", "drc_warnings", "unconnected",
                                       "parity", "islands", "outside_outline", "by_type")})
    return tidy(result)


def view(pcb: Path | str, around: str | None = None, at: tuple[float, float] | None = None,
         region: tuple[float, float, float, float] | None = None, layers: str | list[str] = "top",
         margin: float | None = None, ppm: float | None = None, out: Path | str | None = None) -> dict:
    """Zoomed PNG of one board region into ``views/``: ``around`` a footprint (its box plus
    ``margin`` mm, default 3), ``at`` a point (±``margin``, default 5) or a ``region``
    (x, y, w, h). ``layers``: a set name (top, bottom, placement, copper, In1, ...) or a
    comma list of KiCad layer names. ``ppm`` (px/mm) defaults to a 1600 px longest side."""
    pcb = Path(pcb)
    if sum(v is not None for v in (around, at, region)) != 1:
        raise env.KatError("give exactly one of around, at, region")
    b = Board(pcb)
    name, lay = _layers(b, layers)
    if around is not None:
        m = 3.0 if margin is None else margin
        f = b.footprint(around)
        fb = f.box()
        box, what = (fb[0] - m, fb[1] - m, fb[2] + m, fb[3] + m), around
    elif at is not None:
        m = 5.0 if margin is None else margin
        box, what = (at[0] - m, at[1] - m, at[0] + m, at[1] + m), f"{at[0]:g}_{at[1]:g}"
    else:
        x, y, w, h = region
        if w <= 0 or h <= 0:
            raise env.KatError("region needs a positive width and height")
        box, what = (x, y, x + w, y + h), f"{x:g}_{y:g}_{w:g}x{h:g}"
    if ppm is None:
        ppm = min(120.0, max(10.0, render.TILE_PX / max(box[2] - box[0], box[3] - box[1])))
    if out is None:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out = pcb.parent / "views" / f"{stamp}_pcb_{name}_{re.sub(r'[^A-Za-z0-9_.-]', '-', what)}.png"
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    res = _render(_svg(pcb, name, lay), out, box, ppm)
    res["layers"] = lay
    res["region"] = [round(box[0], 2), round(box[1], 2), round(box[2] - box[0], 2), round(box[3] - box[1], 2)]
    return tidy(res)
