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


def test_widening_a_router_neck_sees_the_routers_new_vias(placed):
    """B9: a necked 0.1 mm track and a new via of another net 0.55 mm beside it: widened to
    the class's 0.2 mm it would come 0.15 mm close; it stays narrow and is reported."""
    from kat.pcb.router import apply_session
    b = Board(placed)
    res = apply_session(b, {"wires": [{"start": (5.0, 20.0), "end": (10.0, 20.0), "width": 0.1,
                                       "layer": "F.Cu", "net": "/A1"}],
                            "vias": [{"at": (7.5, 20.55), "size": 0.6, "drill": 0.3, "net": "/A2"}]})
    assert res["added_tracks"] == 1 and res["added_vias"] == 1
    assert res["widened"] == 0 and len(res["narrow"]) == 1, res


def test_fine_pitch_reports_classes_too_wide_to_leave_a_pin(placed):
    """I11: a class whose width plus clearance does not fit beside the neighbouring pad."""
    from kat.pcb.router import fine_pitch
    assert fine_pitch(Board(placed)) == []
    with kat.pcb.edit(placed) as b:
        b.netclass("BIG", track=0.6, clearance=0.9, nets=["/A1"])
    got = fine_pitch(Board(placed))
    assert got and {e["part"] for e in got} <= {"R1", "D1"} and all(e["net"] == "/A1" and e["fits"] < 0.6 for e in got), got


def test_fanout_gives_each_of_several_same_number_pads_its_own_via(placed):
    """B10: an edge SMA has four GND pads numbered "2": one via per pad, not four on one."""
    from kat.pcb.syncer import _set_child
    from kat.sexpr import find_all
    with kat.pcb.edit(placed) as b:
        j = b.add_footprint("Connector_Coaxial:SMA_Amphenol_132289_EdgeMount", "J9", 20, 20, 90)
        for pad in find_all(j.node, "pad"):
            if str(pad[1]) == "2":
                _set_child(pad, "net", "GND")
        res = b.fanout(["J9"], nets=["GND"])
    pads = [d["pad"] for d in res["vias"]] + [f["pad"] for f in res["failed"]]
    assert sorted(pads) == ["J9.2@1", "J9.2@2", "J9.2@3", "J9.2@4"], res
    spots = [tuple(d["via"]) for d in res["vias"] if not d.get("shared")]   # F and B pads share
    assert len(spots) >= 2 and len(set(spots)) == len(spots), res
    vias = [v["at"] for v in Board(placed).vias()]
    assert len(vias) == len(set(vias)), vias                                  # nothing stacked


def test_single_tracks_and_vias_are_removed_even_when_locked(placed):
    """I5: a locked stacked via and a locked track go, one by one; nothing else does."""
    with kat.pcb.edit(placed) as b:
        b.track(["R1.2", "D1.2"], lock=True)
        b.via((35, 22), net="GND", lock=True)
        b._via((35, 22), "GND", 0.6, 0.3, True)                  # stacked twice
    b = Board(placed)
    before = (len(b.tracks()), len(b.vias()))
    with kat.pcb.edit(placed) as b:
        assert len(b.at_point((35, 22), kind="via")) == 2
        assert b.remove_at((35, 22), kind="via")["removed"] == 2
        t = next(t for t in b.tracks() if t["net"] == "/A1")
        b.remove(t["uuid"])
        with pytest.raises(kat.KatError, match="no track or via"):
            b.remove_at((1, 1))
    b = Board(placed)
    assert (len(b.tracks()), len(b.vias())) == (before[0] - 1, before[1] - 2)
