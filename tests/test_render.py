import pytest
from PIL import Image

from kat import render


@pytest.mark.parametrize("zoom", [1.0, 2.0])
@pytest.mark.requires("pymupdf")
def test_pymupdf_uses_snapshot_pixel_scale(tmp_path, monkeypatch, zoom):
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    svg, png = tmp_path / "scale.svg", tmp_path / "scale.png"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="25.4mm" height="12.7mm" '
        'viewBox="0 0 25.4 12.7"><rect width="25.4" height="12.7" fill="red"/></svg>',
        encoding="utf-8",
    )
    render.rasterize(svg, png, zoom)
    with Image.open(png) as image:
        assert abs(image.width - 25.4 * render.PX_PER_MM * zoom) <= 1
        assert abs(image.height - 12.7 * render.PX_PER_MM * zoom) <= 1
