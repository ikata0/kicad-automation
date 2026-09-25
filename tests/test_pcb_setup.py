"""Board setup (outline, layers, holes, pours, keep-outs, net classes, rules), judged by DRC."""

import shutil

import pytest

import kat
from kat.pcb import Board
from kat.pcb.geometry import union
from test_pcb_sync import board_of, design
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")


@pytest.fixture
def board(project, tmp_path):
    """The 3-part design on a board with an outline around the parts."""
    design(project, tmp_path)
    kat.pcb.sync(board_of(project))
    with kat.pcb.edit(board_of(project)) as b:
        box = union(f.box() for f in b.footprints())
        b.outline(rect=(box[0] - 5, box[1] - 5, box[2] - box[0] + 10, box[3] - box[1] + 10), radius=2)
    return board_of(project)


def kinds(pcb, refill=False) -> dict:
    return kat.pcb.drc(pcb, refill=refill)["by_type"]


def test_outline_rect_rounded_and_polygon(board):
    assert "invalid_outline" not in kinds(board)
    b = Board(board)
    box = b.outline_box()
    with kat.pcb.edit(board) as b:
        b.outline(polygon=[(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0] + 3, box[3])])
    assert "invalid_outline" not in kinds(board)
    assert Board(board).outline_box() == pytest.approx(box)


def test_copper_layers_and_mounting_holes_survive_sync(board):
    with kat.pcb.edit(board) as b:
        assert b.set_copper_layers(4)["copper_layers"] == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        box = b.outline_box()
        b.mounting_hole((box[0] + 2.5, box[1] + 2.5), "M2")
        b.add_track((box[0] + 1, box[3] - 1), (box[0] + 6, box[3] - 1), 0.2, "In1.Cu", "GND")
    b = Board(board)
    assert b.copper_layers() == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    with pytest.raises(kat.KatError, match="in use"):
        b.set_copper_layers(2)
    r = kat.pcb.sync(board)
    assert r["ok"] and not r["removed"] and r["check"]["parity"] == 0
    assert Board(board).footprint("H1").board_only


def test_pour_connects_gnd(board):
    before = len(kat.pcb.drc(board)["unconnected"])
    with kat.pcb.edit(board) as b:
        b.zone("GND", "F.Cu")
    after = kat.pcb.drc(board, refill=True)
    assert len(after["unconnected"]) == before - 1   # D1.1 and J1.2 joined by the pour


def test_netclass_and_rules_reach_drc(board):
    b = Board(board)
    gnd, vin = b.footprint("D1").pad("1"), b.footprint("J1").pad("1")
    box = b.outline_box()
    y = box[3] - 1.5
    with kat.pcb.edit(board) as b:
        b.add_track((box[0] + 1, y), (box[0] + 8, y), 0.25, "F.Cu", gnd.net)
        b.add_track((box[0] + 1, y - 0.6), (box[0] + 8, y - 0.6), 0.25, "F.Cu", vin.net)  # gap 0.35
    assert "clearance" not in kinds(board)
    with kat.pcb.edit(board) as b:
        b.netclass("POWER", track=0.25, clearance=0.5, nets=[gnd.net])
    assert "clearance" in kinds(board)
    with kat.pcb.edit(board) as b:
        b.netclass("POWER", track=0.25, clearance=0.2, nets=[gnd.net])
        b.rules('(rule "wide" (constraint track_width (min 0.3mm)))')
    k = kinds(board)
    assert "clearance" not in k and "track_width" in k


def test_keepout_forbids_tracks(board):
    b = Board(board)
    box = b.outline_box()
    y = box[3] - 1.5
    with kat.pcb.edit(board) as b:
        b.add_track((box[0] + 1, y), (box[0] + 8, y), 0.25, "F.Cu", "GND")
        b.keepout("F.Cu", rect=(box[0] + 2, y - 1, 3, 2), name="no tracks")
    assert "items_not_allowed" in kinds(board)
