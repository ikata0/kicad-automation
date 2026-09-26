"""Board check, snapshots and views (kat.pcb.check / snapshot / view)."""

import json
import shutil
from pathlib import Path

import pytest

import kat
from kat import cli
from test_pcb_setup import board  # noqa: F401 (fixture: 3-part board with outline)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")


def test_check_reports_unconnected_and_parts_outside(board):
    c = kat.pcb.check(board)
    assert c["outline"] and c["parity"] == 0 and c["unconnected"] == 3 and not c["ok"]
    assert c["outside_outline"] == []
    with kat.pcb.edit(board) as b:
        o = b.outline_box()
        b.footprint("R1").place(o[2] + 10, o[1])
    assert kat.pcb.check(board)["outside_outline"] == ["R1"]


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
