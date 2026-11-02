"""How zones join pads: judged by the copper KiCad's filler makes and by its DRC."""

import pytest

import kat
from kat.pcb import Board
from kat.pcb.coupling import _filled, _pts
from kat.sexpr import Sym, find, find_all

pytestmark = pytest.mark.requires("kicad", "footprints")
SMD, THT = "Resistor_SMD:R_0603_1608Metric", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"


def on_net(f, net):
    """Every pad of the footprint on ``net``."""
    for p in f.pads():
        node = p.node
        node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), net])


def design(tmp_path, pads="thermal", overrides=()) -> "Path":
    pcb = tmp_path / "z.kicad_pcb"
    for s in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        pcb.with_suffix(s).unlink(missing_ok=True)
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 30, 20))
    on_net(b.add_footprint(SMD, "R1", 8, 6), "GND")
    on_net(b.add_footprint(SMD, "R2", 8, 14), "GND")
    on_net(b.add_footprint(THT, "J1", 22, 9), "GND")
    b.zone("GND", "F.Cu", pads=pads)
    for pad, mode in overrides:
        b.pad_connection(pad, mode)
    b.save()
    return pcb


def copper_area(pcb) -> float:
    """Area of the filled pour, mm²."""
    total = 0.0
    for z in find_all(_filled(pcb).tree, "zone"):
        for fp in find_all(z, "filled_polygon"):
            pts = _pts(fp)
            total += abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))) / 2
    return total


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("KAT_REPORT", "0")


def test_zone_modes_make_the_copper_they_name(tmp_path):
    """More copper from thermal to through-hole-only to solid; none joins nothing."""
    area = {}
    for mode in ("thermal", "thru_hole_only", "solid", "none"):
        pcb = design(tmp_path, mode)
        area[mode] = copper_area(pcb)
        d = kat.pcb.drc(pcb, parity=False, refill=True)
        joined = not d["unconnected"]
        assert joined == (mode != "none"), (mode, d["unconnected"])
        kept = {str(t) for z in Board(pcb).zones() for t in find(z, "connect_pads")[1:] if not isinstance(t, list)}
        assert kept == ({"yes"} if mode == "solid" else {"no"} if mode == "none"
                        else {"thru_hole_only"} if mode == "thru_hole_only" else set()), kept
    assert area["none"] < area["thermal"] < area["thru_hole_only"] < area["solid"], area


def test_a_pad_overrides_its_zone(tmp_path):
    thermal = copper_area(design(tmp_path))
    solid = design(tmp_path, overrides=[("R1.1", "solid")])
    assert Board(solid).footprint("R1").pad("1").zone_connection == "solid"     # through KiCad's save
    assert Board(solid).footprint("R1").pad("1").geometry()["zone_connection"] == "solid"
    assert copper_area(solid) > thermal
    # cut one pad off a solid zone: DRC finds it unconnected
    cut = design(tmp_path, "solid", overrides=[("R1.1", "none"), ("R1.2", "none")])
    assert kat.pcb.drc(cut, parity=False, refill=True)["unconnected"]
    with kat.pcb.edit(cut) as b:
        assert b.pad_connection(["R1.1", "R1.2"], None) == {"pads": ["R1.1", "R1.2"], "mode": None}
    assert Board(cut).footprint("R1").pad("1").zone_connection is None
    assert not kat.pcb.drc(cut, parity=False, refill=True)["unconnected"]


def test_refusals_and_a_changed_mode_replaces_the_zone(tmp_path):
    pcb = design(tmp_path)
    with kat.pcb.edit(pcb) as b:
        assert b.zone("GND", "F.Cu")["status"] == "unchanged"
        assert b.zone("GND", "F.Cu", pads="solid")["status"] == "replaced"
        assert b.zone("GND", "F.Cu", pads="solid")["status"] == "unchanged"
        with pytest.raises(kat.KatError, match="pads 'x'"):
            b.zone("GND", "F.Cu", pads="x")
        with pytest.raises(kat.KatError, match="zone connection 'x'"):
            b.pad_connection("R1.1", "x")
        with pytest.raises(kat.KatError, match="no pad '9'"):
            b.pad_connection("R1.9", "solid")
