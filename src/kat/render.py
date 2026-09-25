"""Schematic snapshots: SVG via kicad-cli, rasterised and trimmed to PNG."""

from __future__ import annotations

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
                       check=True)
        return
    try:
        import pymupdf
    except ImportError as e:
        raise env.KatError("no rasterizer: install rsvg-convert (librsvg) or `pip install pymupdf`") from e
    doc = pymupdf.open(str(svg))
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    pix.save(str(png))


def trim(png: Path, border: int = 40) -> tuple[int, int]:
    """Crop to the drawn content plus a white border; returns the new size."""
    from PIL import Image, ImageChops
    im = Image.open(png).convert("RGB")
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 8 else 0)
    box = diff.getbbox()
    if box:
        x0, y0, x1, y1 = box
        im = im.crop((max(0, x0 - border), max(0, y0 - border),
                      min(im.width, x1 + border), min(im.height, y1 + border)))
    im.save(png)
    return im.size


def snapshot(sch: Path, out_dir: Path, zoom: float = 3.0) -> dict:
    """Write ``<stem>.svg`` (full page, for people) and ``<stem>.png`` (trimmed, no frame,
    for the model) for every sheet into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = export_svg(sch, out_dir, frame=True)
    images = []
    with tempfile.TemporaryDirectory() as tmp:
        for svg in export_svg(sch, Path(tmp), frame=False):
            png = out_dir / (svg.stem + ".png")
            rasterize(svg, png, zoom)
            w, h = trim(png)
            images.append({"png": str(png), "width": w, "height": h})
    return {"svg": [str(p) for p in pages], "images": images}
