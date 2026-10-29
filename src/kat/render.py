"""Schematic snapshots: SVG via kicad-cli, rasterised and trimmed to PNG."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import env


def export_svg(sch: Path, out_dir: Path, frame: bool = True) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    args = ["sch", "export", "svg", "-o", str(out_dir)]
    if not frame:
        args += ["--exclude-drawing-sheet", "--no-background-color"]
    env.run_cli(*args, str(sch))
    return sorted(out_dir.glob("*.svg"))


def rasterize(svg: Path, png: Path, zoom: float = 3.0) -> None:
    """SVG -> PNG with a white background. Uses rsvg-convert if present, else PyMuPDF."""
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-z", str(zoom), "-b", "white", "-o", str(png), str(svg)],
                       check=True, stdin=subprocess.DEVNULL)
        return
    try:
        import pymupdf
    except ImportError as e:
        raise env.KatError("no rasterizer: install rsvg-convert (librsvg) or `pip install pymupdf`") from e
    # PyMuPDF uses 72 points/inch; match rsvg-convert's 96 pixels/inch.
    scale = zoom * 96 / 72
    with pymupdf.open(str(svg)) as doc:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        pix.save(str(png))


PX_PER_MM = 96 / 25.4  # rsvg-convert / PyMuPDF at zoom 1


def region_svg(svg: Path, out: Path, box: tuple[float, float, float, float]) -> None:
    """Copy of a KiCad SVG (viewBox in sheet mm) showing only ``box`` = (x0, y0, x1, y1)."""
    text = svg.read_text(encoding="utf-8")
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    head, n = re.subn(r'width="[^"]*" height="[^"]*" viewBox="[^"]*"',
                      f'width="{w:.4f}mm" height="{h:.4f}mm" viewBox="{x0:.4f} {y0:.4f} {w:.4f} {h:.4f}"',
                      text, count=1)
    if not n:
        raise env.KatError(f"{svg}: unexpected SVG header, cannot crop")
    out.write_text(head, encoding="utf-8")


def render_region(svg: Path, png: Path, box: tuple[float, float, float, float], zoom: float) -> tuple[int, int]:
    """Rasterise one region (sheet mm) of a KiCad SVG; returns the PNG size."""
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        part = Path(tmp) / "region.svg"
        region_svg(svg, part, box)
        rasterize(part, png, zoom)
    with Image.open(png) as im:
        return im.size


def trim(png: Path, border: int = 40) -> tuple[int, int]:
    """Crop to the drawn content plus a white border; returns the new size."""
    return trim_box(png, border)[0]


def trim_box(png: Path, border: int = 40) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
    """As ``trim``; also returns the kept box in pixels of the original image."""
    from PIL import Image, ImageChops
    Image.MAX_IMAGE_PIXELS = None  # large sheets (A0 at zoom 3) are legitimate
    im = Image.open(png).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 8 else 0)
    box = diff.getbbox()
    kept = (0, 0, im.width, im.height)
    if box:
        x0, y0, x1, y1 = box
        kept = (max(0, x0 - border), max(0, y0 - border), min(im.width, x1 + border), min(im.height, y1 + border))
        im = im.crop(kept)
    im.save(png)
    return im.size, kept


TILE_PX = 1600  # readable in one look: agents' image viewers downscale above ~2000 px


def snapshot(sch: Path, out_dir: Path, zoom: float = 3.0, tiles: bool | None = None,
             pages: str = "files") -> dict:
    """Write ``<stem>.svg`` (full page, for people) and ``<stem>.png`` (trimmed, no frame,
    for the model) for every sheet into ``out_dir``. With ``tiles`` (default: when the PNG
    is larger than 2000 px), also ``tiles/r<i>c<j>.png`` (``tiles/<page>/...`` when there
    are several pages): the drawn area in overlapping pieces of about 1600 px, each with
    its sheet region in mm. ``pages``: for a hierarchy, ``"files"`` renders the PNG of one
    instance per sheet file (the others show the same drawing), ``"instances"`` all."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_svg = export_svg(sch, out_dir, frame=True)
    wanted = None
    if pages == "files" and len(pages_svg) > 1:
        from . import hierarchy
        seen, wanted = set(), set()
        for i in hierarchy.tree(sch):
            if i["file"] not in seen:
                seen.add(i["file"])
                wanted.add(sch.stem + i["path"].rstrip("/").replace("/", "-"))
    images, tile_list = [], []
    ppm = PX_PER_MM * zoom
    with tempfile.TemporaryDirectory() as tmp:
        plain = [s for s in export_svg(sch, Path(tmp), frame=False) if wanted is None or s.stem in wanted]
        for svg in plain:
            png = out_dir / (svg.stem + ".png")
            rasterize(svg, png, zoom)
            (w, h), kept = trim_box(png)
            area = [round(v / ppm, 2) for v in kept]
            images.append({"png": str(png), "width": w, "height": h, "region_mm": area})
            if tiles or (tiles is None and max(w, h) > 2000):
                where = out_dir / "tiles" / (svg.stem if len(plain) > 1 else "")
                tile_list += make_tiles(svg, where, area, zoom, png=png, kept=kept)
    result = {"svg": [str(p) for p in pages_svg], "images": images}
    if wanted is not None:
        result["pages"] = f"one per sheet file ({len(images)} of {len(pages_svg)}; pages='instances' for all)"
    if tile_list:
        result["tiles"] = tile_list
    return result


def make_tiles(svg: Path, out_dir: Path, area: list[float], zoom: float, png: Path | None = None,
               kept: tuple[int, int, int, int] | None = None) -> list[dict]:
    """Cover ``area`` (x0, y0, x1, y1 in mm) with overlapping tiles of about TILE_PX.
    With ``png`` (the page rasterised at ``zoom`` and trimmed to ``kept``, pixels of the
    full page) the tiles are cut from it instead of rendered one by one."""
    import math
    out_dir.mkdir(parents=True, exist_ok=True)
    size = TILE_PX / (PX_PER_MM * zoom)
    step = size * 0.9
    x0, y0, x1, y1 = area
    nx = max(1, math.ceil((x1 - x0 - size) / step) + 1)
    ny = max(1, math.ceil((y1 - y0 - size) / step) + 1)
    page = None
    if png is not None and kept is not None:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        page = Image.open(png)
    ppm = PX_PER_MM * zoom
    out = []
    for i in range(ny):
        for j in range(nx):
            box = (x0 + j * step, y0 + i * step, min(x0 + j * step + size, x1), min(y0 + i * step + size, y1))
            tile = out_dir / f"r{i + 1}c{j + 1}.png"
            if page is not None:
                crop = [round(box[0] * ppm) - kept[0], round(box[1] * ppm) - kept[1],
                        round(box[2] * ppm) - kept[0], round(box[3] * ppm) - kept[1]]
                im = page.crop(tuple(crop))
                im.save(tile)
                w, h = im.size
            else:
                w, h = render_region(svg, tile, box, zoom)
            out.append({"png": str(tile), "width": w, "height": h,
                        "region_mm": [round(v, 2) for v in box]})
    return out
