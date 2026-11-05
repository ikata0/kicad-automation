"""Two boards as they mate: positions come from KiCad's own export of each board."""

import json

import pytest

import kat
from kat import cli
from kat.pcb.board import Board, num
from kat.pcb.geometry import rotate
from kat.pcb.mating import transform
from kat.pcb.mechanics import hole_node
from kat.sexpr import Sym, find, find_all
from test_pcb_geometry import ipc356

pytestmark = pytest.mark.requires("kicad", "footprints")

HEADER = "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical"
HOLE = "MountingHole:MountingHole_3.2mm_M3"


def set_nets(f, nets):
    for p in f.pads():
        if p.number in nets:
            layers = find(p.node, "layers")
            p.node.insert(p.node.index(layers) + 1, [Sym("net"), nets[p.number]])


def board(path, conn=None, holes=(), outline=(0, 0, 40, 30), flip=False):
    """A board with a connector and mounting holes. ``conn``: (ref, x, y, rot, {pin: net})."""
    path.parent.mkdir(parents=True, exist_ok=True)
    b = Board.create(path)
    b.outline(rect=outline)
    if conn:
        ref, x, y, rot, nets = conn
        set_nets(b.add_board_part(HEADER, ref, (x, y), rot, "B" if flip else "F"), nets)
    for k, (x, y) in enumerate(holes, 1):
        b.mounting_hole((x, y), "M3", f"H{k}")
    b.save()
    return path


NETS = {"1": "VCC", "2": "GND", "3": "SDA", "4": "SCL", "5": "/sub/IRQ", "6": "GND"}


def stack(tmp_path, nets_a=NETS, nets_b=None, holes_a=(), holes_b=()):
    """B lies face down: its connector on the top side, mirrored by the flip, so pins meet pin for
    pin across the columns: 1-2, 3-4, 5-6."""
    a = board(tmp_path / "a" / "a.kicad_pcb", ("J1", 10, 10, 0, nets_a), holes_a)
    b = board(tmp_path / "b" / "b.kicad_pcb", ("P1", 0, 0, 0, nets_b or {k: nets_a[w] for k, w in zip("123456", "214365")}),
              holes_b, outline=(-10, -10, 30, 30))
    return a, b


def test_a_flipped_board_mates_pin_to_pin(tmp_path):
    a, b = stack(tmp_path)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True)
    assert r["ok"], r
    pins = {p["a"]: p["b"] for p in r["pairs"][0]["pins"]}
    assert pins == {"J1.1": "P1.2", "J1.2": "P1.1", "J1.3": "P1.4", "J1.4": "P1.3", "J1.5": "P1.6", "J1.6": "P1.5"}
    assert r["pairs"][0]["mated"] == 6 and all(p["offset"] == 0 for p in r["pairs"][0]["pins"])
    assert r["transform"] == {"at": [12.54, 10], "rot": 0, "flip": True, "above": True}


def test_positions_agree_with_kicads_export(tmp_path):
    """The transform of B's pads as KiCad exports them is what mate compares."""
    a, b = stack(tmp_path)
    pa, pb = ipc356(a), ipc356(b)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True)
    for pin in r["pairs"][0]["pins"]:
        ra, na = pin["a"].split(".")
        rb, nb = pin["b"].split(".")
        q = transform(pb[(rb, nb)], (12.54, 10), 0, True)
        assert q == pytest.approx(pa[(ra, na)], abs=0.01), pin


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_a_turned_board_lands_on_the_same_pins(tmp_path, rot):
    """The connector turned one way on B, and B turned back by mate."""
    a = board(tmp_path / "a" / "a.kicad_pcb", ("J1", 10, 10, 0, NETS))
    nets = dict(zip("123456", (NETS[k] for k in "214365")))
    b = board(tmp_path / "b" / "b.kicad_pcb", ("P1", 3, 4, rot, nets), outline=(-30, -30, 60, 60))
    # the connector is turned by rot on B; mirrored by the flip that turns the other way, so
    # turning B by rot undoes it. Then only its position is left to move onto J1
    dx, dy = rotate(-3, 4, rot)
    at = (12.54 - dx, 10 - dy)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=at, rot=rot, flip=True)
    assert r["ok"], (rot, r["misaligned"], r["net_mismatch"])
    assert r["pairs"][0]["mated"] == 6


def test_misaligned_pins_are_named_with_how_far_off(tmp_path):
    a, b = stack(tmp_path)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.6, 10), flip=True)
    assert not r["ok"] and r["counts"]["misaligned"] == 12, r["misaligned"]
    first = r["misaligned"][0]
    assert first["nearest"] == pytest.approx(0.06, abs=1e-3) and first["limit"] == 0.05
    assert kat.pcb.mate(a, b, [("J1", "P1")], at=(12.6, 10), flip=True, tolerance=0.1)["ok"]


def test_a_wrong_net_is_named(tmp_path):
    nets = dict(zip("123456", ("GND", "VCC", "SCL", "SDA", "GND", "other_irq")))
    a, b = stack(tmp_path, nets_b=nets)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True)
    assert not r["ok"] and r["shorts"] == []
    assert [(m["a"], m["b"], m["net_a"], m["net_b"]) for m in r["net_mismatch"]] == [("J1.5", "P1.6", "/sub/IRQ", "other_irq")]


def test_names_may_differ_by_intent(tmp_path):
    nets = dict(zip("123456", ("GND", "VCC", "SCL", "SDA", "GND", "other_irq")))
    a, b = stack(tmp_path, nets_b=nets)
    for key in ("/sub/IRQ", "IRQ"):
        r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True, nets={key: "other_irq"})
        assert r["ok"], r


def test_nets_joined_through_a_connector_are_a_short(tmp_path):
    joined = dict(zip("123456", ("VCC", "VCC", "SCL", "SDA", "GND", "/sub/IRQ")))
    a, b = stack(tmp_path, nets_b=joined)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True)
    # P1.1 is VCC and meets J1.2, which is GND; J1.6, also GND, meets P1.5, which is GND
    assert r["shorts"] == [{"a": ["GND", "VCC"], "b": ["GND", "VCC"]}], r["shorts"]
    assert not r["ok"]


def test_a_pin_connected_on_one_board_is_listed_and_does_not_fail(tmp_path):
    nets_b = dict(zip("123456", ("GND", "VCC", "SCL", "SDA", "", "/sub/IRQ")))
    a, b = stack(tmp_path, nets_a=dict(NETS, **{"6": "OTHER"}), nets_b=nets_b)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True)
    assert [o["a"] for o in r["open"]] == ["J1.6"] and r["counts"]["open"] == 1 and r["ok"], r


def test_generated_net_names_only_count_as_shorts(tmp_path):
    nets = dict(NETS, **{"3": "Net-(U1-Pad3)"})
    other = dict(zip("123456", ("GND", "VCC", "SCL", "Net-(R9-Pad1)", "GND", "/sub/IRQ")))
    a, b = stack(tmp_path, nets_a=nets, nets_b=other)
    assert kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True)["ok"]


def test_sides_must_face_each_other(tmp_path):
    a, b = stack(tmp_path)
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True, above=False)
    assert {(w["board"], w["facing"]) for w in r["wrong_side"]} == {("a", "B"), ("b", "B")}, r["wrong_side"]
    assert not r["ok"]
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=False)
    assert ("b", "B") in {(w["board"], w["facing"]) for w in r["wrong_side"]}


def test_mounting_holes_auto_and_named(tmp_path):
    # B's hole at (9.54, -7) lands on A's at (3, 3). A's at (25, 3) lies outside B: it needs no partner
    a, b = stack(tmp_path, holes_a=[(3, 3), (25, 3)], holes_b=[(9.54, -7)])
    r = kat.pcb.mate(a, b, [("J1", "P1")], at=(12.54, 10), flip=True)
    assert r["holes"] == [{"a": "H1", "b": "H1", "offset": 0, "drill_a": 3.2, "drill_b": 3.2}]
    assert not r["holes_unmatched"] and r["ok"], r["holes_unmatched"]
    # B's second hole lands on A at (11.54, 11), with nothing there
    a2, b2 = stack(tmp_path / "2", holes_a=[(3, 3)], holes_b=[(9.54, -7), (1, 1)])
    r = kat.pcb.mate(a2, b2, [("J1", "P1")], at=(12.54, 10), flip=True)
    assert r["holes_unmatched"] == [{"board": "b", "ref": "H2", "at": [11.54, 11]}]
    assert not r["ok"]
    r = kat.pcb.mate(a2, b2, [("J1", "P1")], at=(12.54, 10), flip=True, holes=None)
    assert r["ok"] and r["holes"] == []
    r = kat.pcb.mate(a2, b2, [("J1", "P1")], at=(12.54, 10), flip=True, holes=[("H1", "H1")])
    assert r["ok"] and r["holes"][0]["offset"] == 0
    r = kat.pcb.mate(a2, b2, [("J1", "P1")], at=(12.54, 10), flip=True, holes=[("H1", "H2")])
    assert r["holes_unmatched"][0]["ref"] == "H1=H2" and r["holes_unmatched"][0]["gap"] > 1


def with_peg(path, x):
    """A two-pin header with a guide hole in its footprint at (x, 0)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    b = Board.create(path)
    b.outline(rect=(-20, -20, 40, 40))
    f = b.add_board_part("Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "J1", (0, 0))
    node = hole_node("x", {"x": x, "y": 0, "d": 1.0, "length": None, "angle": 0, "plating": False, "kind": "hole"}, False)
    pad = next(c for c in node if isinstance(c, list) and str(c[0]) == "pad")
    at = find(pad, "at")
    at[1], at[2] = num(x), num(0)
    f.node.insert(f.node.index(find_all(f.node, "pad")[-1]) + 1, pad)
    b.save()
    return path


def test_guide_holes_of_a_pair_must_meet(tmp_path):
    a, c = with_peg(tmp_path / "a" / "a.kicad_pcb", 5), with_peg(tmp_path / "c" / "c.kicad_pcb", -5)
    # B lies flipped: its guide hole at x = -5 lands on A's at +5, and its pins on A's
    r = kat.pcb.mate(a, c, [("J1", "J1")], flip=True, holes=None)
    assert not r["misaligned"] and r["pairs"][0]["mated"] == 2, r["misaligned"]
    # moved by 0.2 the pins still pass a loose tolerance, and the guide hole does not
    r = kat.pcb.mate(a, c, [("J1", "J1")], flip=True, holes=None, at=(0, 0.2), tolerance=0.3)
    assert [(m["a"], m["b"]) for m in r["misaligned"]] == [("J1 hole", "J1 hole")] * 2
    assert r["misaligned"][0]["limit"] == 0.1 and not r["ok"], r["misaligned"]
    # a guide hole with no partner at all
    d = with_peg(tmp_path / "d" / "d.kicad_pcb", 9)
    r = kat.pcb.mate(a, d, [("J1", "J1")], flip=True, holes=None)
    assert {m["a"] for m in r["misaligned"]} >= {"J1 hole"} and not r["ok"]


def test_problems_name_what_is_wrong(tmp_path):
    a, b = stack(tmp_path)
    with pytest.raises(kat.KatError, match="no footprint 'Z9'"):
        kat.pcb.mate(a, b, [("Z9", "P1")])
    with pytest.raises(kat.KatError, match="nothing to check"):
        kat.pcb.mate(a, b, [], holes=None)


def test_cli(tmp_path, capsys):
    a, b = stack(tmp_path, holes_a=[(3, 3)], holes_b=[(9.54, -7)])
    base = ["pcb", "mate", str(a), str(b), "--pair", "J1=P1", "--at", "12.54,10", "--flip"]
    assert cli.main(base) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["ok"] and r["counts"]["misaligned"] == 0 and len(r["holes"]) == 1
    assert cli.main(base + ["--rot", "90"]) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert cli.main(base + ["--holes", "none"]) == 0
    assert json.loads(capsys.readouterr().out)["holes"] == []
    assert cli.main(base + ["--holes", "H1=H1", "--net", "VCC=VCC"]) == 0
    capsys.readouterr()
    assert cli.main(["pcb", "mate", str(a), str(b), "--pair", "J1"]) == 2
    assert "give A=B" in json.loads(capsys.readouterr().out)["error"]
