"""Copper structure: tracks (net from the pads), vias into a plane, fan-out, stitching,
locking and rip-up - each judged by KiCad's DRC."""

import shutil

import pytest

import kat
from kat.pcb import Board
from test_pcb_placement import pcb  # noqa: F401 (fixture: transistor + two LED channels)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")


@pytest.fixture
def placed(pcb):
    """Parts in a row inside a 40 x 25 mm board with a GND pour on B.Cu."""
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 40, 25))
        b.move_many({"J1": {"to": (4, 10)}, "Q1": {"to": (14, 12)}, "C1": {"to": (14, 6)},
                     "R1": {"to": (24, 8)}, "D1": {"to": (28, 8), "rot": 180},   # anode faces R
                     "R2": {"to": (24, 16)}, "D2": {"to": (28, 16), "rot": 180}})
        b.zone("GND", "B.Cu")
    return pcb


def drc(pcb):
    return kat.pcb.drc(pcb, refill=True)


def test_track_takes_the_net_of_its_pads(placed):
    before = len(drc(placed)["unconnected"])
    with kat.pcb.edit(placed) as b:
        t = b.track(["R1.2", "D1.2"])
        assert t["net"].endswith("A1") and t["width"] == 0.2
        with pytest.raises(kat.KatError, match="exactly one net"):
            b.track(["R1.2", "R2.2"])              # A1 and A2: would join two nets
        with pytest.raises(kat.KatError, match="too close"):
            b.track([(26, 16.0), (30, 16.0)], net="VIN")  # straight across D2's pads
    d = drc(placed)
    assert len(d["unconnected"]) == before - 1 and not d["errors"]


def test_via_to_plane_and_fanout_reach_the_gnd_pour(placed):
    before = len(drc(placed)["unconnected"])
    with kat.pcb.edit(placed) as b:
        v = b.via_to_plane("C1.2")
        assert v["net"] == "GND"
        f = b.fanout("Q1", nets=["GND"])
        assert len(f["vias"]) == 1 and not f["failed"]
    d = drc(placed)
    # C1.2 and Q1.2 now reach the pour, which already held J1.2 (through-hole)
    assert len(d["unconnected"]) == before - 2
    assert not d["errors"], d["by_type"]


def test_stitch_lock_and_rip_up(placed):
    with kat.pcb.edit(placed) as b:
        b.track(["R1.2", "D1.2"], lock=False)
        b.track(["R2.2", "D2.2"], lock=True)
        s = b.stitch("GND", pitch=6)        # last: stitching fills what structure left free
        assert s["vias"] > 5
        assert b.remove_tracks(vias=False)["removed"] == 1       # the unlocked one
        assert [t["net"][-2:] for t in b.tracks()] == ["A2"]
        b.lock(locked=False)
        assert b.remove_tracks()["removed"] == 1 + s["vias"]     # all copper, stitching included
    assert not drc(placed)["errors"]


def test_refill_writes_the_pour(placed):
    r = kat.pcb.refill(placed)
    assert r["drc_errors"] == 0
    assert "filled_polygon" in placed.read_text()
    assert Board(placed).zones()
