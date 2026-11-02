"""Board check, snapshots and views (kat.pcb.check / snapshot / view)."""

import json
from pathlib import Path

import pytest

import kat
from kat import cli
from test_pcb_setup import board  # noqa: F401 (fixture: 3-part board with outline)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints")


def test_check_reports_unconnected_and_parts_outside(board):
    c = kat.pcb.check(board)
    assert c["outline"] and c["parity"] == 0 and c["unconnected"] == 3 and not c["ok"]
    assert c["outside_outline"] == []
    with kat.pcb.edit(board) as b:
        o = b.outline_box()
        b.footprint("R1").place(o[2] + 10, o[1])
    assert kat.pcb.check(board)["outside_outline"] == ["R1"]


@pytest.mark.requires("render")
def test_snapshot_pictures_tiles_crops(board):
    s = kat.pcb.snapshot(board, slug="first", note="three parts", tiles=True, finding_crops=2,
                         three_d=False)
    d = Path(s["dir"])
    assert d.name.endswith("_pcb_first") and (d / board.name).exists()
    assert {"top", "bottom", "placement"} <= set(s["images"])
    assert all(Path(i["png"]).exists() and max(i["width"], i["height"]) <= 2001
               for i in s["images"].values())
    assert s["tiles"]["top"] and Path(s["tiles"]["top"][0]["png"]).exists()
    assert len(s["finding_crops"]) == 2 and all(f["what"] == "unconnected" for f in s["finding_crops"])
    saved = json.loads((d / "check.json").read_text())
    assert saved["unconnected"] == s["unconnected"] == 3
    assert (d / "note.md").read_text().strip() == "three parts"


@pytest.mark.requires("render")
def test_view_around_part_and_cache(board, capsys):
    v = kat.pcb.view(board, around="R1", layers="copper")
    assert Path(v["png"]).exists() and v["layers"][:2] == ["B.Cu", "F.Cu"]
    # small parts: the scale is capped at 120 px/mm, so the picture stays below 1600 px
    assert v["px_per_mm"] <= 120 and max(v["width"], v["height"]) <= 1601
    kat.pcb.view(board, at=(v["region"][0] + 1, v["region"][1] + 1), layers="copper")
    assert len(list((board.parent / "views" / ".cache").glob("*.copper.*.svg"))) == 1
    assert cli.main(["pcb", "view", str(board), "--region", "0,0,10,5", "--layers", "F.Cu,Edge.Cuts"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["region"] == [0, 0, 10, 5] and out["layers"] == ["F.Cu", "Edge.Cuts"]
    with pytest.raises(kat.KatError):
        kat.pcb.view(board, around="R1", at=(1, 1))


def test_view_presets_and_no_texts(board):
    """#17: block-scale presets (copper-top, assembly) and the reference texts left out,
    from a scratch copy: the board file stays as it is."""
    from kat.pcb import views
    b = kat.pcb.Board(board)
    assert views._layers(b, "assembly")[1] == ["Edge.Cuts", "F.CrtYd", "F.Fab"]
    assert views._layers(b, "copper-top")[1] == ["F.Cu", "Edge.Cuts"]
    before = board.read_bytes()
    lay = views.SETS["assembly"]
    full = views._svg(board, "assembly", lay).stat().st_size
    bare = views._svg(board, "assembly", lay, texts=False).stat().st_size
    assert bare < full and board.read_bytes() == before


def test_inner_layer_views_show_only_that_layer(tmp_path, monkeypatch):
    """FR-028: a downstream report of an inner-layer view with other layers in it, gone on
    the next render. Every copper layer has its own track (and two a pour): each SVG holds
    one layer's colour, in any order and from the cache."""
    import re
    from kat.pcb import views
    monkeypatch.setenv("KAT_REPORT", "0")
    pcb = tmp_path / "six.kicad_pcb"
    b = kat.pcb.Board.create(pcb)
    b.set_copper_layers(6)
    b.outline(rect=(0, 0, 50, 40))
    copper = b.copper_layers()
    for i, layer in enumerate(copper):
        b.add_track((5, 4 + i * 5), (45, 4 + i * 5), 0.5, layer, f"N{i}")
    b.zone("P2", "In2.Cu", polygon=[(10, 33), (40, 33), (40, 38), (10, 38)], name="p2")
    b.zone("PF", "F.Cu", polygon=[(2, 33), (9, 33), (9, 38), (2, 38)], name="pf")
    b.save()

    def colours(layers: list[str]) -> set[str]:
        svg = views._svg(pcb, "-".join(l.split(".")[0] for l in layers), layers)
        return set(re.findall(r"(?:stroke|fill):#([0-9A-Fa-f]{6})", svg.read_text())) - {"000000", "FFFFFF"}

    seen = {layer: colours([layer]) for layer in copper}
    assert all(len(c) == 1 for c in seen.values()), seen
    assert len(set().union(*seen.values())) == len(copper)             # all different
    for layer in ("In1.Cu", "In2.Cu", "F.Cu", "In2.Cu", "In1.Cu", "In4.Cu"):
        assert colours([layer]) == seen[layer], layer                  # later, and cached


def test_view_of_a_layer_the_board_does_not_have(board):
    """kicad-cli says OK and writes nothing for a layer name the board lacks."""
    for bad in ("In9.Cu", "F.Cu,In9.Cu", ["In9.Cu"], "In1"):
        with pytest.raises(kat.KatError, match="has no layer"):
            kat.pcb.view(board, around="R1", layers=bad)
    assert kat.pcb.view(board, around="R1", layers="F.Silkscreen, Edge.Cuts")["layers"] == ["F.Silkscreen", "Edge.Cuts"]
