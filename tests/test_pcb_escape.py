"""Escape routing with free pin assignment, judged by KiCad's DRC and netlist."""

import json

import pytest

import kat
from kat import cli
from kat.pcb.board import Board
from kat.pcb.geometry import seg_seg
from kat.sexpr import Sym, find
from test_back_annotate import design  # noqa: F401 (fixture)

pytestmark = pytest.mark.requires("kicad", "footprints")

HEADER = "Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical"


def set_net(pad, name):
    old = find(pad.node, "net")
    if old is not None:
        pad.node.remove(old)
    if name:
        pad.node.insert(pad.node.index(find(pad.node, "layers")) + 1, [Sym("net"), name])


@pytest.fixture
def rows(tmp_path):
    """Two 8-pin headers facing each other. The targets' nets are scrambled along the row, so with
    the sources' pin order as given every track would cross."""
    pcb = tmp_path / "b" / "b.kicad_pcb"
    pcb.parent.mkdir()
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 60, 40))
    b.add_board_part(HEADER, "J1", (10, 10))
    t = b.add_board_part(HEADER, "J2", (45, 10))
    for k, n in enumerate((5, 2, 8, 1, 7, 3, 6, 4), 1):
        set_net(t.pad(str(k)), f"N{n}")
    b.save()
    return pcb


SPEC = {"sources": ["J1.*"], "targets": ["J2.*"], "layers": ["F.Cu"]}


def give_nets(pcb, assignment):
    """What back-annotation does in a real design."""
    b = Board(pcb)
    for pin, net in assignment.items():
        ref, number = pin.split(".")
        set_net(b.footprint(ref).pad(number), net)
    b.save()


def test_a_planar_escape_needs_no_vias_and_keeps_the_rows_in_order(rows):
    r = kat.pcb.escape(rows, SPEC)
    assert r["max_paths"] == 8 and not r["unrouted_sources"] and not r["unrouted_targets"], r
    assert r["via_count"] == 0 and not r["applied"]
    pairs = {x["source"]: x["target"] for x in r["routes"]}
    assert [pairs[f"J1.{k}"] for k in range(1, 9)] == [f"J2.{k}" for k in range(1, 9)], "in order: no crossing"
    assert r["assignment"]["J1.1"] == "N5" and r["assignment"]["J1.4"] == "N1"
    # planar on the numbers too: no two tracks meet
    tracks = [(x["source"], t) for x in r["routes"] for t in x["tracks"]]
    for i, (a, s) in enumerate(tracks):
        for b, t in tracks[i + 1:]:
            if a != b:
                assert seg_seg(s["start"], s["end"], t["start"], t["end"]) >= r["track"] + r["clearance"] - 1e-6, (a, b)
    assert Board(rows).tracks() == [], "nothing changes without apply"


def test_tracks_go_in_after_the_nets_and_drc_agrees(rows):
    plan = kat.pcb.escape(rows, SPEC)
    with pytest.raises(kat.KatError, match="back-annotate TABLE --assign J1.1=N5"):
        kat.pcb.escape(rows, SPEC, apply=True)
    assert Board(rows).tracks() == []
    give_nets(rows, plan["assignment"])
    r = kat.pcb.escape(rows, SPEC, apply=True)
    assert r["applied"] and r["check"]["drc_errors"] == 0, r["check"]["by_type"]
    assert r["check"]["unconnected"] == 0, "every net joined"
    b = Board(rows)
    assert {t["layer"] for t in b.tracks()} == {"F.Cu"} and not b.vias()
    assert len({t["net"] for t in b.tracks()}) == 8


def test_a_gap_limits_how_many_paths_can_exist(rows):
    with kat.pcb.edit(rows) as b:                      # a wall with a gap for about two tracks
        b.keepout(["F.Cu"], rect=(25, 0.5, 2, 15.3), name="upper")
        b.keepout(["F.Cu"], rect=(25, 16.8, 2, 22.7), name="lower")
    r = kat.pcb.escape(rows, SPEC)
    assert 1 <= r["max_paths"] <= 3 and len(r["unrouted_sources"]) == 8 - r["max_paths"], r["max_paths"]
    give_nets(rows, r["assignment"])
    done = kat.pcb.escape(rows, SPEC, apply=True)
    assert done["check"]["drc_errors"] == 0 and done["check"]["by_type"].get("items_not_allowed") is None, done["check"]


def test_a_wall_on_one_layer_needs_vias_when_they_are_allowed(tmp_path):
    """Surface pads, only on the top: the way past a wall on that layer goes through a via."""
    pcb = tmp_path / "b" / "b.kicad_pcb"
    pcb.parent.mkdir()
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 60, 40))
    smd = "Resistor_SMD:R_0603_1608Metric"
    for k in range(3):
        b.add_board_part(smd, f"S{k + 1}", (10, 10 + 5 * k))
        t = b.add_board_part(smd, f"T{k + 1}", (50, 10 + 5 * k))
        set_net(t.pad("1"), f"N{k + 1}")
    b.keepout(["F.Cu"], rect=(25, 0.5, 2, 39), name="wall")
    b.save()
    spec = {"sources": ["S1.1", "S2.1", "S3.1"], "targets": ["T1.1", "T2.1", "T3.1"]}
    assert kat.pcb.escape(pcb, spec)["max_paths"] == 0, "no way on the top alone"
    both = {**spec, "layers": ["F.Cu", "B.Cu"], "vias": True}
    r = kat.pcb.escape(pcb, both)
    assert r["max_paths"] == 3 and r["via_count"] == 6, r["via_count"]
    give_nets(pcb, r["assignment"])
    done = kat.pcb.escape(pcb, both, apply=True)
    assert done["check"]["drc_errors"] == 0, done["check"]["by_type"]
    pads = {p.net for f in Board(pcb).footprints() for p in f.pads() if p.net}
    assert pads >= {"N1", "N2", "N3"} and len(Board(pcb).vias()) == 6
    joined = {t["net"] for t in Board(pcb).tracks() if t["layer"] == "B.Cu"}
    assert joined == {"N1", "N2", "N3"}


def test_two_layers_without_vias_stay_planar_on_each(rows):
    with kat.pcb.edit(rows) as b:
        b.keepout(["F.Cu"], rect=(25, 0.5, 2, 39), name="wall")
    r = kat.pcb.escape(rows, {**SPEC, "layers": ["F.Cu", "B.Cu"]})
    assert r["via_count"] == 0 and r["max_paths"] == 8, "through-hole pins start on either layer"
    assert all(len({t["layer"] for t in x["tracks"]}) == 1 for x in r["routes"])


def test_more_sources_than_targets_leaves_the_rest_named(rows):
    r = kat.pcb.escape(rows, {**SPEC, "targets": ["J2.1", "J2.2", "J2.3"]})
    assert r["max_paths"] == 3 and len(r["unrouted_sources"]) == 5 and not r["unrouted_targets"]


def test_existing_copper_and_pads_are_obstacles(rows):
    with kat.pcb.edit(rows) as b:
        b.add_track((27, 0.6), (27, 39.4), 0.25, "F.Cu", "OTHER")
    assert kat.pcb.escape(rows, SPEC)["max_paths"] == 0
    r = kat.pcb.escape(rows, {**SPEC, "layers": ["B.Cu"]})
    assert r["max_paths"] == 8, "on the other layer it does not count"


def test_a_narrow_region_limits_the_search(rows):
    r = kat.pcb.escape(rows, {**SPEC, "region": [5, 9, 50, 13]})
    assert r["max_paths"] == 2, r["max_paths"]


def test_fixed_pairs_are_reported_as_not_handled(rows):
    r = kat.pcb.escape(rows, {"pairs": [["J1.1", "J2.1"]]})
    assert r["not_handled"]["pairs"] == [["J1.1", "J2.1"]] and "route" in r["not_handled"]["why"] and r["routes"] == []
    r = kat.pcb.escape(rows, {**SPEC, "pairs": [["J1.1", "J2.1"]]})
    assert r["not_handled"] and r["max_paths"] == 8


@pytest.mark.parametrize("change, match", [
    ({"sources": []}, "needs sources"),
    ({"targets": None}, "needs sources"),
    ({"sources": ["J1"]}, "not a pad"),
    ({"sources": ["J1.99"]}, "no pad '99'"),
    ({"sources": ["Z9.1"]}, "no footprint"),
    ({"targets": ["J1.*"]}, "both sides"),
    ({"layers": ["In9.Cu"]}, "no layer In9.Cu"),
    ({"vias": True}, "exactly two layers"),
    ({"grid": 0.1}, "below"),
])
def test_bad_specs_are_errors(rows, change, match):
    with pytest.raises(kat.KatError, match=match):
        kat.pcb.escape(rows, {**SPEC, **change})


def test_a_pad_off_the_layers_is_named(tmp_path):
    pcb = tmp_path / "b" / "b.kicad_pcb"
    pcb.parent.mkdir()
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 40, 30))
    b.add_board_part("Resistor_SMD:R_0603_1608Metric", "R1", (10, 10))
    b.add_board_part("Resistor_SMD:R_0603_1608Metric", "R2", (25, 10), side="B")
    b.save()
    with pytest.raises(kat.KatError, match="not on F.Cu: R2.1"):
        kat.pcb.escape(pcb, {"sources": ["R1.1"], "targets": ["R2.1"]})


def test_json_file_output_and_cli(rows, tmp_path, capsys):
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(SPEC))
    out = tmp_path / "plan.json"
    assert cli.main(["pcb", "escape", str(rows), str(f), "--out", str(out)]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["max_paths"] == 8 and json.loads(out.read_text())["assignment"] == r["assignment"]
    assert cli.main(["pcb", "escape", str(rows), str(f), "--apply"]) == 2
    assert "back-annotate" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_the_whole_way_through_a_real_design(design):
    """The parts lie so that the table's pins would cross. Escape chooses, back-annotation carries it
    through the SKiDL script, and only then do the tracks go in."""
    d, sch, pcb = design
    with kat.pcb.edit(pcb) as b:
        b.move_many({"R1": {"to": (20, 15)}, "R2": {"to": (20, 8)}})      # R2, on CH2, now on top
    spec = {"sources": ["J1.1", "J1.2"], "targets": ["R1.1", "R2.1"], "layers": ["F.Cu"]}
    plan = kat.pcb.escape(pcb, spec)
    assert plan["max_paths"] == 2 and plan["via_count"] == 0
    assert plan["assignment"]["J1.1"].endswith("CH2") and plan["assignment"]["J1.2"].endswith("CH1"), plan["assignment"]
    wanted = {pin: net.rsplit("/", 1)[-1] for pin, net in plan["assignment"].items()}
    r = kat.mapping.back_annotate(d / "j1.csv", wanted, d / "design.py", sch, d / "design.net", pcb, force=True)
    assert r["ok"] and r["moved"] == [] and r["board"]["parity"] == 0
    assert ("J1", "1") in next(v for k, v in kat.nets(sch).items() if k.endswith("CH2"))
    done = kat.pcb.escape(pcb, spec, apply=True)
    assert done["applied"] and done["check"]["drc_errors"] == 0 and done["check"]["parity"] == 0, done["check"]
    b = Board(pcb)
    ch1 = next(p.net for p in b.footprint("J1").pads() if p.number == "2")
    assert ch1.endswith("CH1") and any(t["net"] == ch1 for t in b.tracks())
