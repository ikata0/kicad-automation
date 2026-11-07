"""How zones join pads: judged by the copper KiCad's filler makes and by its DRC."""

import pytest

import kat
import kat.pcb.geometry
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
    # FR-047: the footprint's default; its pads follow it, their own override wins
    whole = design(tmp_path, overrides=[("R1", "solid"), ("R1.2", "thermal")])
    f = Board(whole).footprint("R1")                                            # through KiCad's save
    assert f.zone_connection == "solid" and f.pad("1").zone_connection is None
    assert copper_area(whole) == pytest.approx(copper_area(solid), rel=1e-3)
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


def guarded(tmp_path):
    """IN on F.Cu above a GND plane on In1 of a 4-layer board: a straight run in two segments,
    a corner, and a via down to In2."""
    pcb = tmp_path / "g.kicad_pcb"
    for s in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        pcb.with_suffix(s).unlink(missing_ok=True)
    b = Board.create(pcb)
    b.set_copper_layers(4)
    b.outline(rect=(0, 0, 40, 30))
    b.zone("GND", "In1.Cu", name="plane")
    b.add_track((5, 10), (15, 10), 0.2, "F.Cu", "IN")
    b.add_track((15, 10), (25, 10), 0.2, "F.Cu", "IN")
    b.add_track((25, 10), (25, 20), 0.2, "F.Cu", "IN")
    return pcb, b


def poured(pcb, x, y, layer="In1.Cu") -> bool:
    from kat.pcb.geometry import inside
    for z in find_all(_filled(pcb).tree, "zone"):
        for fp in find_all(z, "filled_polygon"):
            if str(find(fp, "layer")[1]) == layer and inside(_pts(fp), (x, y)):
                return True
    return False


def test_keepout_around_clears_the_plane_next_to_a_track(tmp_path):
    """FR-025: under the track and its margin no copper is poured, beside that it is; DRC has no
    objection, since only pours are forbidden."""
    pcb, b = guarded(tmp_path)
    b.save()
    assert poured(pcb, 15, 10)
    with kat.pcb.edit(pcb) as e:
        res = e.keepout_around(nets=["IN"], margin=0.5, scope="adjacent", name="IN guard")
    assert res["layers"] == ["In1.Cu"] and "warnings" not in res
    assert res["keepouts"] == ["IN guard:1", "IN guard:2"]            # the straight run counts as one
    assert not poured(pcb, 15, 10) and not poured(pcb, 25, 15)
    assert poured(pcb, 15, 12) and poured(pcb, 30, 25)
    d = kat.pcb.drc(pcb, parity=False, refill=True)
    assert not d["errors"] and "items_not_allowed" not in d["by_type"], d["by_type"]


def test_keepout_around_scopes_selection_and_running_it_again(tmp_path):
    pcb, b = guarded(tmp_path)
    via = b.via((5, 10), "IN", layers=("F.Cu", "In2.Cu"))
    on = lambda res: res["layers"]                                       # noqa: E731
    assert on(b.keepout_around(nets=["IN"], scope="same", name="a")) == ["F.Cu", "In1.Cu", "In2.Cu"]   # the via too
    assert on(b.keepout_around(uuids=[via["uuid"]], scope="adjacent", name="b")) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert on(b.keepout_around(uuids=[via["uuid"]], scope=["In2.Cu", "B.Cu"], name="c")) == ["In2.Cu", "B.Cu"]
    assert on(b.keepout_around(uuids=[b.tracks()[0]["uuid"]], scope="span", name="d")) == ["F.Cu"]
    assert on(b.keepout_around(uuids=[via["uuid"]], scope="span", name="e")) == ["F.Cu", "In1.Cu", "In2.Cu"]
    f = b.add_footprint(SMD, "R1", 30, 5)
    pad = b.keepout_around(refs=["R1.1"], margin=0.1, scope=["In1.Cu"], name="f")
    assert len(pad["keepouts"]) == 1
    box = kat.pcb.geometry.box_of(_pts(find(next(z for z in b.zones() if str(find(z, "name")[1]) == "f:1"), "polygon")))
    p = f.pad("1").box
    assert box[0] == pytest.approx(p[0] - 0.1, abs=0.03) and box[3] == pytest.approx(p[3] + 0.1, abs=0.03)
    # again with fewer: the old ones of that name go, others stay
    count = len(b.zones())
    again = b.keepout_around(uuids=[b.tracks()[2]["uuid"]], scope="same", name="a")
    assert again["removed"] > 1 and again["keepouts"] == ["a:1"] and len(b.zones()) < count
    assert any(str(find(z, "name")[1]) == "b:1" for z in b.zones())
    for bad, why in ((dict(nets=["NOPE"]), "nothing selected"), (dict(nets=["IN"], scope="wide"), "scope 'wide'"),
                     (dict(nets=["IN"], scope=["In9.Cu"]), "no copper layer In9.Cu"),
                     (dict(nets=["IN"], forbid=("copper",)), "forbid copper"),
                     (dict(nets=["IN"], margin=-1), "margin"), (dict(refs=["R1.9"]), "no pad '9'")):
        with pytest.raises(kat.KatError, match=why):
            b.keepout_around(**bad)


def test_keepout_around_warns_when_its_copper_would_break_it(tmp_path):
    """FR-025: KiCad reports copper inside its own keep-out, on the layer that holds it; the
    warning names that case, and on another layer the router simply stays out."""
    from kat.pcb import specctra
    pcb, b = guarded(tmp_path)
    res = b.keepout_around(nets=["IN"], scope="same", forbid=("tracks",), name="own")
    assert "items_not_allowed" in res["warnings"][0] and "F.Cu" in res["warnings"][0]
    b.save()
    assert "items_not_allowed" in kat.pcb.drc(pcb, parity=False)["by_type"]
    pcb, b = guarded(tmp_path)
    res = b.keepout_around(nets=["IN"], scope="adjacent", forbid=("tracks", "vias"), name="below")
    assert "warnings" not in res
    f = b.add_footprint(SMD, "R1", 30, 5)
    node = f.pad("1").node
    node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), "A"])
    b.constraints(edge_clearance=0)               # only the area's own keep-outs in the DSN
    dsn = tmp_path / "k.dsn"
    specctra.write_dsn(b, dsn)
    text = dsn.read_text()
    assert text.count('(wire_keepout "" (polygon In1.Cu') == 2 == text.count('(via_keepout "" (polygon In1.Cu')
    assert '(wire_keepout "" (polygon F.Cu' not in text and '(wire_keepout "" (polygon In2.Cu' not in text
    b.save()
    assert "items_not_allowed" not in kat.pcb.drc(pcb, parity=False)["by_type"]



def test_a_pad_on_a_cut_off_fragment_is_named(tmp_path):
    """FR-038: a ring of another net cuts R1's pad 1 off the GND pour; DRC only says two parts
    of the zone are not connected, at the zone's corner. The check names the pad."""
    pcb = tmp_path / "z.kicad_pcb"
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 40, 20))
    on_net(b.add_footprint(SMD, "R1", 10, 10), "GND")
    on_net(b.add_footprint(SMD, "R2", 30, 10), "GND")
    ring = [(7, 8), (9.8, 8), (9.8, 12), (7, 12), (7, 8)]
    for a, c in zip(ring, ring[1:]):
        b.add_track(a, c, 0.25, "F.Cu", "X")
    b.zone("GND", "F.Cu")
    b.save()
    c = kat.pcb.check(pcb)
    assert c["unconnected"] == 1 and "R1" not in str(c["unconnected_items"])       # what DRC says
    (pad,) = c["isolated_pads"]
    assert pad["pad"] == "R1.1" and pad["zone"] == "GND F.Cu" and pad["layer"] == "F.Cu"
    assert 0 < pad["fragment_area"] < 12 < pad["zone_area"]


def test_a_fill_over_other_nets_is_retried_with_a_nudged_width(tmp_path, monkeypatch):
    """FR-039 (not reproduced on KiCad 10.0.6 here): if DRC finds a fill over a foreign via,
    refill nudges that zone's minimum width once and fills again, and says so."""
    from kat.pcb import checks
    pcb = design(tmp_path)
    real, calls = checks.drc, []

    def drc(path, parity=True, refill=False, save=False):
        d = real(path, parity, refill, save)
        calls.append(1)
        if len(calls) == 1:                               # the reported bug, once
            d["violations"].append({"severity": "error", "type": "clearance", "description": "Clearance violation",
                                    "items": [{"description": "Zone 'GND F.Cu' [GND] on F.Cu, priority 0", "x": 1, "y": 1},
                                              {"description": "Via [X] on F.Cu - B.Cu", "x": 1, "y": 1}]})
            d["errors"] += 1
        return d
    monkeypatch.setattr(checks, "drc", drc)
    r = kat.pcb.refill(pcb)
    assert r["fill_retry"] == {"zones": {"GND F.Cu": 0.251}, "before": 1, "after": 0} and r["drc_errors"] == 0, r
    assert float(find(Board(pcb).zones()[0], "min_thickness")[1]) == 0.251
    monkeypatch.setattr(checks, "drc", real)
    assert "fill_retry" not in kat.pcb.refill(pcb)
