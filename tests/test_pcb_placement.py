"""Placement: courtyard-checked moves, alignment, parts next to a pin, blocks, replication."""


import pytest

import kat
from test_pcb_sync import board_of, design
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints")

# a switch transistor, a supply capacitor and two identical LED channels (blocks ch1, ch2)
PARTS = {
    "J1": ("Connector_Generic", "Conn_01x03", "IN", "j1"),
    "Q1": ("Transistor_BJT", "Q_NPN_BEC", "BC817", "q1"),
    "C1": ("Device", "C", "100n", "c1"),
    "R1": ("Device", "R", "330", "ch1_r"), "D1": ("Device", "LED", "red", "ch1_d"),
    "R2": ("Device", "R", "330", "ch2_r"), "D2": ("Device", "LED", "red", "ch2_d"),
}
NETS = {
    "VIN": [("J1", "1"), ("C1", "1"), ("R1", "1"), ("R2", "1")],
    "GND": [("J1", "2"), ("C1", "2"), ("Q1", "2")],
    "CTRL": [("J1", "3"), ("Q1", "1")],
    "LOAD": [("Q1", "3"), ("D1", "1"), ("D2", "1")],
    "A1": [("R1", "2"), ("D1", "2")], "A2": [("R2", "2"), ("D2", "2")],
}
FPS = {"J1": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
       "Q1": "Package_TO_SOT_SMD:SOT-23", "C1": "Capacitor_SMD:C_0603_1608Metric",
       "R1": "Resistor_SMD:R_0603_1608Metric", "R2": "Resistor_SMD:R_0603_1608Metric",
       "D1": "LED_SMD:LED_0603_1608Metric", "D2": "LED_SMD:LED_0603_1608Metric"}
FIELDS = {r: {"MPN": f"X-{r}", **({"Block": "ch1"} if r in ("R1", "D1") else
                                  {"Block": "ch2"} if r in ("R2", "D2") else {})} for r in PARTS}


@pytest.fixture
def pcb(project, tmp_path):
    design(project, tmp_path, PARTS, NETS, FPS, FIELDS)
    r = kat.pcb.sync(board_of(project), force=True)   # the drawing is not arranged: gate says not clean
    assert r["check"]["parity"] == 0, r
    return board_of(project)


def test_move_refuses_overlap_and_move_many_swaps(pcb):
    with kat.pcb.edit(pcb) as b:
        b.move("R1", to=(50, 50))
        b.move("R2", to=(60, 50))
        with pytest.raises(kat.KatError, match="overlap"):
            b.move("R2", to=(50.5, 50))
        assert (b.footprint("R2").x, b.footprint("R2").y) == (60, 50)   # untouched after refusal
        b.move_many({"R1": {"to": (60, 50)}, "R2": {"to": (50, 50), "rot": 90}})
        assert b.footprint("R1").x == 60 and b.footprint("R2").rot == 90
        b.move("D1", side="bottom")               # FR-045: the stamping words as well
        assert b.footprint("D1").side == "B"
        b.move("D1", to=(50, 50), side="B")        # other side: no courtyard conflict
        assert b.footprint("D1").side == "B"
        with pytest.raises(kat.KatError, match="side 'up'"):
            b.move("D1", side="up")                # was taken as "the other side"
        assert b.footprint("D1").side == "B"
    assert kat.pcb.drc(pcb)["by_type"].get("courtyards_overlap") is None


def test_align_distribute_and_near(pcb):
    with kat.pcb.edit(pcb) as b:
        b.distribute(["R1", "R2", "C1"], start=(40, 40), pitch=(4, 0))
        assert [b.footprint(r).x for r in ("R1", "R2", "C1")] == [40, 44, 48]
        b.move("D1", to=(40, 45))
        b.align(["R1", "D1"], axis="x")
        assert b.footprint("D1").x == 40
        hits = b.near("R1.1", radius=0.1)
        assert hits[0]["pad"] == "R1.1" and hits[0]["net"].endswith("VIN")


def test_place_near_puts_the_same_net_pad_next_to_the_pin(pcb):
    with kat.pcb.edit(pcb) as b:
        b.move("J1", to=(30, 30))
        b.place_near("C1", "J1.1")
        j = b.footprint("J1").pad("1")
        c = {p.number: p for p in b.footprint("C1").pads()}
        vin_pad = next(p for p in c.values() if p.net == j.net)
        other = next(p for p in c.values() if p is not vin_pad)
        # beside the connector, VIN pad nearer to J1.1 than the GND pad
        assert abs(vin_pad.x - j.x) + abs(vin_pad.y - j.y) < abs(other.x - j.x) + abs(other.y - j.y)
        assert not b.collisions(["C1"])
        # a second part next to the same pin goes beside the first one
        b.place_near("R1", "J1.1")
        assert not b.collisions(["R1", "C1"])


def test_place_free_finds_the_nearest_legal_spot(pcb):
    """FR-046: any part, near a part, a pad or a point, on a chosen side, out of keep-outs;
    the spot is nearest first and passes KiCad's courtyard check."""
    import math
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 80, 60))
        b.distribute(["J1", "Q1", "R1", "R2", "D1", "D2", "C1"], start=(10, 10), pitch=(8, 0))
        b.move("Q1", to=(40, 30))
        r = b.place_free("C1", "Q1", gap=0.25)
        assert not b.collisions(["C1"], gap=0.2) and r["distance"] < 4, r
        near = r["distance"]
        q = b.footprint("Q1").box()
        wall = (q[0] - 6, q[1] - 6, q[2] + 6, q[3] + 6)            # nothing may go close to Q1 now
        r = b.place_free("C1", "Q1", keepouts=[wall])
        c = b.footprint("C1").box()
        assert r["distance"] > near and (c[2] <= wall[0] or c[0] >= wall[2] or c[3] <= wall[1] or c[1] >= wall[3]), r
        r = b.place_free("D1", (40, 30), side="bottom")           # under Q1: the other side is free
        assert r["side"] == "B" and r["distance"] < 0.3, r
        with pytest.raises(kat.KatError, match="no legal spot"):
            b.place_free("R1", "Q1", keepouts=[(0, 0, 80, 60)], max_distance=2)
        assert (b.footprint("R1").x, b.footprint("R1").y) == (26, 10)            # left where it was
        assert math.dist(r["at"], (b.footprint("D1").x, b.footprint("D1").y)) < 1e-6
    assert kat.pcb.drc(pcb)["by_type"].get("courtyards_overlap") is None


def test_blocks_move_and_replicate(pcb):
    with kat.pcb.edit(pcb) as b:
        assert {k: sorted(v) for k, v in b.blocks().items()} == {"ch1": ["D1", "R1"], "ch2": ["D2", "R2"]}
        b.move_many({"R1": {"to": (40, 40), "rot": 90}, "D1": {"to": (40, 43), "rot": 270}})
        b.move_block("ch1", by=(5, 0))
        assert (b.footprint("R1").x, b.footprint("D1").x) == (45, 45)
        res = b.replicate("ch1", {"ch2": (60, 30)})
        assert res["pairs"]["ch2"] == {"R1": "R2", "D1": "D2"}   # by SKiDL tag ch1_r -> ch2_r
        r1, d1, r2, d2 = (b.footprint(r) for r in ("R1", "D1", "R2", "D2"))
        assert (r2.x - d2.x, r2.y - d2.y) == pytest.approx((r1.x - d1.x, r1.y - d1.y))
        assert (r2.rot, d2.rot) == (r1.rot, d1.rot)
        assert b.block_box("ch2")[:2] == pytest.approx((60, 30))
    assert kat.pcb.sync(pcb, force=True)["check"]["parity"] == 0


def test_locked_parts_refuse_to_move(pcb):
    """FR-012: the flag goes through KiCad's save, the checked moves respect it and leave
    everything where it was, staying put is allowed, and force overrides."""
    with kat.pcb.edit(pcb) as b:
        b.footprint("J1").place(300, 300, 0)
        b.footprint("R1").place(340, 300, 0)
        b.footprint("D1").place(345, 300, 0)
        assert b.footprint("J1").locked is False and b.footprint("J1").info()["locked"] is False
        b.footprint("J1").lock()
        b.footprint("J1").lock()                                  # twice: still one flag
    b = kat.pcb.Board(pcb)
    j = b.footprint("J1")
    assert j.locked and j.info()["locked"] and len([c for c in j.node if str(c[0]) == "locked"]) == 1
    for refuse in (lambda: b.move("J1", to=(330, 330)), lambda: b.move("J1", rot=90), lambda: b.flip("J1"),
                   lambda: b.move_many({"R1": {"to": (60, 60)}, "J1": {"to": (30, 30)}}),
                   lambda: b.distribute(["R1", "J1"], (70, 70), (5, 0)),
                   lambda: b.align(["R1", "J1"], value=70),
                   lambda: b.place_near("J1", "C1.1")):
        with pytest.raises(kat.KatError, match="J1 locked"):
            refuse()
        assert (j.x, j.y, j.rot, j.side) == (300, 300, 0, "F")
        assert (b.footprint("R1").x, b.footprint("R1").y) == (340, 300)     # all or nothing
    b.move("J1", to=(300, 300), rot=360)                            # staying put is no move
    b.align(["J1", "R1"])                                         # the locked part as the reference
    assert (j.x, j.y) == (300, 300) and b.footprint("R1").y == 300
    b.footprint("D1").lock()
    with pytest.raises(kat.KatError, match="D1 locked"):
        b.move_block("ch1", by=(1, 1))
    with pytest.raises(kat.KatError, match="D1 locked"):
        b.replicate("ch2", {"ch1": (70, 70)})
    b.footprint("D1").unlock()
    assert not b.footprint("D1").locked
    assert b.move("J1", to=(330, 330), force=True)["moved"][0]["locked"] is True
    assert (j.x, j.y) == (330, 330) and j.locked
    j.unlock()
    b.move("J1", to=(331, 331))
    assert (j.x, j.y) == (331, 331) and not j.locked

