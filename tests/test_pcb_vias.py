"""Blind, buried and micro vias: built, checked on their own span, and routed with."""

import json

import pytest

import kat
from kat.pcb import Board, specctra
from kat.pcb.structure import via_kind
from kat.sexpr import Sym, find

pytestmark = pytest.mark.requires("kicad", "footprints")
R0603 = "Resistor_SMD:R_0603_1608Metric"
SIX = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("KAT_REPORT", "0")


@pytest.mark.parametrize("a,b,kind,expected", [
    ("F.Cu", "B.Cu", None, "through"), ("B.Cu", "F.Cu", None, "through"),
    ("F.Cu", "In2.Cu", None, "blind"), ("In3.Cu", "B.Cu", None, "blind"),
    ("In1.Cu", "In4.Cu", None, "buried"), ("F.Cu", "In1.Cu", "micro", "micro"),
    ("In4.Cu", "B.Cu", "micro", "micro"), ("F.Cu", "In1.Cu", "blind", "blind"),
])
def test_via_kinds_inferred_and_checked(a, b, kind, expected):
    got, span = via_kind(a, b, SIX, kind)
    assert got == expected and SIX.index(span[0]) < SIX.index(span[1])


@pytest.mark.parametrize("a,b,kind,why", [
    ("F.Cu", "F.Cu", None, "two layers"), ("F.Cu", "In9.Cu", None, "no copper layer In9.Cu"),
    ("In1.Cu", "In2.Cu", "micro", "µvia"), ("F.Cu", "In2.Cu", "micro", "µvia"),
    ("In1.Cu", "In2.Cu", "blind", "one outer"), ("F.Cu", "B.Cu", "blind", "one outer"),
    ("F.Cu", "In1.Cu", "buried", "stay inside"), ("F.Cu", "In1.Cu", "through", "both outer"),
    ("F.Cu", "B.Cu", "wide", "via kind 'wide'"),
])
def test_illegal_vias_are_refused(a, b, kind, why):
    with pytest.raises(kat.KatError, match=why):
        via_kind(a, b, SIX, kind)


def board(tmp_path, n=4):
    pcb = tmp_path / "v.kicad_pcb"
    b = Board.create(pcb)
    b.set_copper_layers(n)
    b.outline(rect=(0, 0, 40, 30))
    return pcb, b


def test_vias_go_through_kicad_with_their_kind_and_span(tmp_path):
    pcb, b = board(tmp_path, 6)
    made = [b.via((5, 5), "A"), b.via((10, 5), "A", layers=("F.Cu", "In2.Cu")),
            b.via((15, 5), "A", layers=("In1.Cu", "In3.Cu")), b.via((20, 5), "A", layers=("F.Cu", "In1.Cu"), kind="micro")]
    assert [m["kind"] for m in made] == ["through", "blind", "buried", "micro"]
    b.save()
    v = {x["at"][0]: x for x in Board(pcb).vias()}
    assert [v[x]["kind"] for x in (5, 10, 15, 20)] == ["through", "blind", "buried", "micro"]
    assert v[5]["span"] == tuple(SIX) and v[10]["span"] == ("F.Cu", "In1.Cu", "In2.Cu")
    assert v[15]["span"] == ("In1.Cu", "In2.Cu", "In3.Cu") and v[15]["layers"] == ("In1.Cu", "In3.Cu")
    assert (v[20]["size"], v[20]["drill"]) == (0.3, 0.1)      # the class's µvia
    d = kat.pcb.drc(pcb, parity=False)
    assert not d["errors"] or set(d["by_type"]) <= {"via_dangling"}, d["by_type"]


def test_clearance_is_only_checked_on_the_vias_span(tmp_path):
    """A blind via over an In2 track is fine (DRC agrees); a through via there is not. Existing
    vias count on their own span too."""
    pcb, b = board(tmp_path)
    b.add_track((2, 10), (38, 10), 0.2, "In2.Cu", "X")
    with pytest.raises(kat.KatError, match="too close"):
        b.via((20, 10), "A")
    b.via((20, 10), "A", layers=("F.Cu", "In1.Cu"))
    b.track([(19, 10), (21, 10)], layer="In2.Cu", net="X", check=True)    # the same net as the track: no conflict
    with pytest.raises(kat.KatError, match="too close"):
        b.track([(20, 8), (20, 12)], layer="F.Cu", net="B")               # the via is on F.Cu
    b.track([(20, 8), (20, 12)], layer="B.Cu", net="B")                    # it stops above B.Cu
    assert [x["kind"] for x in b.at_point((20, 10), layer="F.Cu", kind="via")] == ["via"]
    assert b.at_point((20, 10), layer="B.Cu", kind="via") == []
    b.save()
    assert "clearance" not in kat.pcb.drc(pcb, parity=False)["by_type"]


def test_dsn_gives_each_via_its_own_span(tmp_path):
    pcb, b = board(tmp_path)
    f = b.add_footprint(R0603, "R1", 20, 20)
    node = f.pad("1").node
    node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), "A"])
    b.via((5, 5), "A", layers=("F.Cu", "In1.Cu"))
    b.route_vias([("In1.Cu", "In2.Cu"), ("F.Cu", "In1.Cu", "micro", 0.25, 0.1)])
    dsn = tmp_path / "v.dsn"
    specctra.write_dsn(b, dsn)
    text = dsn.read_text()
    stack = {line.split('"')[1]: line for line in text.splitlines() if "(padstack \"Via[" in line}
    assert set(stack) >= {"Via[0-3]_600:300_um", "Via[0-1]_600:300_um", "Via[1-2]_600:300_um", "Via[0-1]_250:100_um"}
    assert all(l in stack["Via[0-1]_600:300_um"] for l in ("F.Cu", "In1.Cu"))
    assert not any(l in stack["Via[0-1]_600:300_um"] for l in ("In2.Cu", "B.Cu"))
    assert all(l in stack["Via[0-3]_600:300_um"] for l in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    assert '(via "Via[0-1]_250:100_um" "Via[0-3]_600:300_um" "Via[1-2]_600:300_um")' in text
    assert '(use_via "Via[1-2]_600:300_um" "Via[0-1]_250:100_um" "Via[0-3]_600:300_um")' in text    # allowed first
    assert "use_layer" not in text
    assert '(via "Via[0-1]_600:300_um" 5000 -5000' in text                # the existing one, on its span
    b.route_vias([("F.Cu", "In1.Cu")], through=False)
    specctra.write_dsn(b, dsn)
    text = dsn.read_text()
    assert "(use_layer F.Cu) (use_layer In1.Cu)" in text and "use_layer B.Cu" not in text
    b.route_vias([], through=False)
    with pytest.raises(kat.KatError, match="no via to lay"):
        specctra.write_dsn(b, dsn)


def wall_board(tmp_path, spans, walls=("F.Cu",)):
    """Net A must cross a locked wall of net W across F.Cu; only the given vias are allowed."""
    pcb, b = board(tmp_path)
    b.constraints(edge_clearance=0.5)

    def part(ref, x, y, net, rot=0, numbers=("1",)):
        f = b.add_footprint(R0603, ref, x, y, rot)
        for number in numbers:
            node = f.pad(number).node
            node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), net])
        return f

    part("R1", 5, 15, "A")
    part("R2", 35, 15, "A")
    part("R3", 20, 3, "W", 90, ("1", "2"))                     # the wall's pads lie on it
    part("R4", 20, 27, "W", 90, ("1", "2"))
    for layer in walls:
        b._segment((20, 0.7), (20, 29.3), 0.3, layer, "W", True)
    b.route_vias(spans, through=False)
    b.save()
    return pcb


@pytest.mark.requires("routing")
@pytest.mark.parametrize("spans,kind,size", [([("F.Cu", "In1.Cu")], "blind", 0.6),
                                             ([("F.Cu", "In1.Cu", "micro")], "micro", 0.3)])
def test_the_router_lays_only_the_allowed_vias(tmp_path, spans, kind, size):
    """Judged by KiCad: connected, no errors, and every via it laid has the allowed span."""
    pcb = wall_board(tmp_path, spans)
    res = kat.pcb.route(pcb, passes=6, timeout=300, out=tmp_path / "r")
    assert res["check"]["unconnected"] == 0 and res["check"]["drc_errors"] == 0, res["check"]
    vias = Board(pcb).vias()
    assert vias and all(v["kind"] == kind and v["layers"] == ("F.Cu", "In1.Cu") and v["size"] == size
                        for v in vias), vias


@pytest.mark.requires("routing")
def test_no_through_via_when_none_is_allowed(tmp_path):
    """F.Cu and In1 walled: a blind via to In1 cannot cross, and tracks may not leave those two
    layers. The net stays open, which KiCad reports, rather than getting a through via."""
    pcb = wall_board(tmp_path, [("F.Cu", "In1.Cu")], walls=("F.Cu", "In1.Cu"))
    res = kat.pcb.route(pcb, passes=4, timeout=300, out=tmp_path / "r")
    assert res["check"]["unconnected"] >= 1
    assert all(v["kind"] != "through" for v in Board(pcb).vias())


def test_a_net_using_a_refused_via_gets_none_of_its_new_copper(tmp_path):
    """The session check itself: the router's through via costs the net all of its new copper,
    and other nets keep theirs."""
    from kat.pcb.router import apply_session
    pcb, b = board(tmp_path)
    b.save()
    b.route_vias([("F.Cu", "In1.Cu")], through=False)
    session = {"wires": [{"net": "A", "layer": "F.Cu", "width": 0.2, "start": (2, 2), "end": (5, 2)},
                         {"net": "A", "layer": "B.Cu", "width": 0.2, "start": (5, 2), "end": (9, 2)},
                         {"net": "C", "layer": "F.Cu", "width": 0.2, "start": (2, 8), "end": (5, 8)},
                         {"net": "C", "layer": "In1.Cu", "width": 0.2, "start": (5, 8), "end": (9, 8)}],
               "vias": [{"net": "A", "at": (5, 2), "size": 0.6, "drill": 0.3, "span": (0, 3)},
                        {"net": "C", "at": (5, 8), "size": 0.6, "drill": 0.3, "span": (0, 1)}]}
    res = apply_session(b, session)
    assert res["disallowed_vias"] == [{"net": "A", "at": [5, 2], "layers": ["F.Cu", "B.Cu"]}]
    assert (res["added_tracks"], res["added_vias"]) == (2, 1)
    assert {t["net"] for t in b.tracks()} == {"C"}
    assert [(v["net"], v["kind"], v["layers"]) for v in b.vias()] == [("C", "blind", ("F.Cu", "In1.Cu"))]
    b2 = Board(pcb)                        # with through vias allowed, the default, nothing is refused
    assert "disallowed_vias" not in apply_session(b2, session)
    assert {v["kind"] for v in b2.vias()} == {"through", "blind"}


def holes(tmp_path, minimum=0.5):
    pcb = tmp_path / "h.kicad_pcb"
    for s in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        pcb.with_suffix(s).unlink(missing_ok=True)
    b = Board.create(pcb)
    b.set_copper_layers(4)
    b.outline(rect=(0, 0, 40, 30))
    b.constraints(hole_to_hole=minimum, hole_clearance=0.0, min_clearance=0.0)
    b.netclass("Default", 0.1, 0.05)
    return pcb, b


def drc_types(pcb) -> set:
    return set(kat.pcb.drc(pcb, parity=False)["by_type"])


@pytest.mark.parametrize("first,second,kinds,flagged", [
    (("F.Cu", "B.Cu"), ("F.Cu", "B.Cu"), (None, None), True),                  # one net: still a hole
    (("F.Cu", "In1.Cu"), ("F.Cu", "In1.Cu"), (None, None), True),
    (("F.Cu", "In1.Cu"), ("In2.Cu", "B.Cu"), (None, None), False),             # spans do not meet
    (("F.Cu", "In1.Cu"), ("F.Cu", "In1.Cu"), ("micro", "micro"), False),       # two µvias
    (("F.Cu", "B.Cu"), ("F.Cu", "In1.Cu"), (None, "micro"), True),
])
def test_hole_conflicts_count_what_drc_counts(tmp_path, first, second, kinds, flagged):
    """FR-023: for each pair, both ways of asking and KiCad's DRC give the same answer."""
    pcb, b = holes(tmp_path)
    b.via((10, 10), "X", layers=first, kind=kinds[0], check=False, size=0.5, drill=0.3)
    x = 10.6                                                                  # 0.3 mm of edge gap at drill 0.3
    drill = 0.1 if kinds[1] == "micro" else 0.3
    if kinds[1] == "micro":
        x = 10.5
    found = b.hole_conflicts((x, 10), drill, second, kinds[1])
    assert bool(found) == flagged, found
    if found:
        assert found[0]["need"] == 0.5 and found[0]["net"] == "X"
        with pytest.raises(kat.KatError, match="hole"):
            b.via((x, 10), "X", layers=second, kind=kinds[1], size=0.5, drill=drill)
    b.via((x, 10), "X", layers=second, kind=kinds[1], size=0.5, drill=drill, check=False)
    b.save()
    assert ("hole_to_hole" in drc_types(pcb)) == flagged


def test_a_via_keeps_the_minimum_from_a_pads_hole_at_the_limit(tmp_path):
    """FR-023: just outside the minimum is free and DRC is quiet; just inside is neither."""
    for gap, free in ((0.51, True), (0.49, False)):
        pcb, b = holes(tmp_path)
        f = b.add_footprint("Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical", "J1", 10, 10)
        pad = f.pad("1")
        at = (10 + pad.drill / 2 + 0.15 + gap, 10)
        found = b.hole_conflicts(at, 0.3)
        assert (not found) == free, found
        if not free:
            assert found[0]["what"] == "J1.1"
        b.via(at, "X", size=0.5, drill=0.3, check=False)
        b.save()
        assert ("hole_to_hole" not in drc_types(pcb)) == free


def test_stitching_and_fanout_keep_the_holes_apart(tmp_path):
    """FR-023: a minimum wider than the stitching pitch; DRC finds nothing, where it would."""
    pcb, b = holes(tmp_path, minimum=6.0)
    b.stitch("GND", pitch=5.0, margin=2.0)
    assert 1 < len(b.vias()) < 25
    b.save()
    assert "hole_to_hole" not in drc_types(pcb)
    pcb, b = holes(tmp_path, minimum=1.2)
    for ref, x in (("R1", 10), ("R2", 12.5)):
        f = b.add_footprint(R0603, ref, x, 10, 90)
        node = f.pad("1").node
        node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), "GND"])
    res = b.fanout(["R1", "R2"], nets=["GND"])
    assert len(res["vias"]) + len(res["failed"]) == 2
    b.save()
    assert "hole_to_hole" not in drc_types(pcb)


def test_dsn_carries_the_minimum_and_route_reports_what_breaks_it(tmp_path):
    """FR-023: as via clearance when the holes ask more than the copper, and a session's vias of
    one net, which the router does not keep apart, are reported; DRC agrees."""
    from kat.pcb.router import apply_session
    pcb, b = holes(tmp_path, minimum=0.8)
    f = b.add_footprint(R0603, "R1", 20, 20)
    node = f.pad("1").node
    node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), "A"])
    dsn = tmp_path / "h.dsn"
    specctra.write_dsn(b, dsn)
    want = 0.8 + 0.3 - 0.6                                              # the Default class's via
    assert f"(clearance {want * 1000:g} (type via_via))" in dsn.read_text()
    res = apply_session(b, {"wires": [], "vias": [{"net": "A", "at": (5, 5), "size": 0.6, "drill": 0.3, "span": (0, 3)},
                                                  {"net": "A", "at": (5.8, 5), "size": 0.6, "drill": 0.3, "span": (0, 3)},
                                                  {"net": "A", "at": (15, 5), "size": 0.6, "drill": 0.3, "span": (0, 3)}]})
    assert [(r["at"], r["near_net"], r["gap"], r["need"]) for r in res["hole_to_hole"]] == [([5.8, 5], "A", 0.5, 0.8)]
    b.save()
    assert "hole_to_hole" in drc_types(pcb)
    default = holes(tmp_path, minimum=0.25)[1]
    specctra.write_dsn(default, dsn)
    assert "via_via" not in dsn.read_text()                             # nothing asked beyond the copper



def test_a_fabrication_profile_holds_the_whole_flow(tmp_path, capsys):
    """FR-037: through vias only, 0.125 mm minimum: DRC disallows the rest, kat refuses to make
    them or a class below the minimum, check lists departures, fab refuses forbidden vias."""
    from kat import cli
    pcb, b = board(tmp_path)
    b.save()
    assert cli.main(["pcb", "profile", str(pcb), "--via-types", "through", "--min-clearance", "0.125"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["profile"] == {"via_types": ["through"], "min_clearance": 0.125} and out["departures"] == {}
    assert "disallow blind_via buried_via micro_via" in pcb.with_suffix(".kicad_dru").read_text()
    b = Board(pcb)
    assert b.board_minimums()["min_clearance"] == 0.125
    with pytest.raises(kat.KatError, match="allows through vias only"):
        b.via((5, 5), "A", layers=("F.Cu", "In1.Cu"))
    with pytest.raises(kat.KatError, match="allows through vias only"):
        b.route_vias([("F.Cu", "In1.Cu", "micro")])
    with pytest.raises(kat.KatError, match="below the fabrication profile"):
        b.netclass("Fine", 0.1, 0.1)
    b.netclass("Fine", 0.1, 0.1, below_profile=True)            # decided, and recorded
    b.via((20, 15), "A")
    b._via((5, 5), "A", 0.6, 0.3, False, ("F.Cu", "In1.Cu"), "blind")   # as an import would bring it
    b.save()
    assert kat.pcb.profile(pcb)["profile"]["exceptions"] == {"Fine": 0.1}
    c = kat.pcb.check(pcb)
    assert c["profile"] == {"forbidden_vias": [{"kind": "blind", "at": [5.0, 5.0], "net": "A"}]} and not c["clean"]
    assert c["by_type"].get("items_not_allowed") == 1                    # KiCad's DRC agrees
    with pytest.raises(kat.KatError, match="profile forbids"):
        kat.pcb.fab(pcb, step=False, three_d=False)
    assert any(e["problem"] == "fab_profile" for e in kat.pcb.ready(pcb)["errors"])
    # FR-051's via spacing: holes are kept apart in every direction, not only along the axes
    with pytest.raises(kat.KatError, match="hole"):
        b = Board(pcb)
        b.via((20.3, 15.3), "A")                                 # 0.42 mm centre to centre, diagonal
    assert kat.pcb.profile(pcb, clear=True)["profile"] == {}
    assert "kat profile vias" not in pcb.with_suffix(".kicad_dru").read_text()


def test_plane_stubs_reuse_a_near_via_and_say_when_no_plane_is_there(tmp_path, capsys):
    """FR-048: kat pcb plane-stub and kat pcb stitch; an existing via of the net within reach
    is used, and a net without a pour on another layer is named."""
    from kat import cli
    pcb, b = board(tmp_path)
    for ref, x in (("R1", 10), ("R2", 20)):
        f = b.add_footprint(R0603, ref, x, 10)
        for num, net in (("1", "GND"), ("2", f"S{ref}")):
            node = f.pad(num).node
            node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), net])
    gnd = b.footprint("R2").pad("1")
    b.via((gnd.x - 1.5, gnd.y), "GND")                      # already there, 1.5 mm from R2.1
    b.save()
    assert cli.main(["pcb", "plane-stub", str(pcb), "R1", "--net", "GND"]) in (0, 1)
    res = json.loads(capsys.readouterr().out)
    assert res["warnings"] == ["no pour of GND on another copper layer: the via reaches no plane yet"]
    with kat.pcb.edit(pcb) as e:
        e.plane("GND", "In1.Cu")
    res = kat.pcb.plane_stub(pcb, "R2", nets=["GND"])
    assert res["vias"][0]["reused"] and res["vias"][0]["via"] == [gnd.x - 1.5, gnd.y] and "warnings" not in res
    assert len(Board(pcb).vias()) == 2                        # R1's new one and the reused one
    assert cli.main(["pcb", "stitch", str(pcb), "GND", "--pitch", "8"]) in (0, 1)
    st = json.loads(capsys.readouterr().out)
    assert st["vias"] == len(st["points"]) > 4 and st["check"]["drc_errors"] == 0, st
