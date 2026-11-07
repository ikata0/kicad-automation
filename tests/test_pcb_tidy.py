"""Post-route clean-up (FR-049): each step on plain points, then on a board judged by DRC."""

import json
import math
from pathlib import Path

import pytest

import kat
from kat.pcb import Board
from kat.pcb import trackclean as tc
from kat.sexpr import Sym, find

SMD = "Resistor_SMD:R_0603_1608Metric"
ALWAYS = lambda old, new: True  # noqa: E731


def turns(pts):
    return [round(tc._turn(a, b, c)) for a, b, c in zip(pts, pts[1:], pts[2:])]


def test_normalize_drops_zero_length_collinear_points_and_spikes():
    assert tc.normalize([(0, 0), (0, 0), (1, 0), (2, 0), (2, 1)]) == [(0, 0), (2, 0), (2, 1)]
    assert tc.normalize([(0, 0), (3, 0), (2, 0), (2, 2)]) == [(0, 0), (2, 0), (2, 2)]   # the spike to x 3


def test_a_staircase_becomes_a_diagonal():
    stair = [(0, 0)] + [(x, y) for k in range(1, 5) for x, y in (((k - 1) * 0.3 + 0.3, (k - 1) * 0.3), (k * 0.3, k * 0.3))]
    assert len(tc.destaircase(stair, 0.2, ALWAYS)) == 2                     # one 45-degree segment
    bent = [(0, 0), (0.3, 0), (0.3, 0.2), (0.6, 0.2), (0.6, 0.4), (2, 0.4)]
    out = tc.destaircase(bent, 0.2, lambda old, new: len(new) == 3)        # the straight one refused
    assert all(t in (0, 45) for t in turns(out)) and tc._length(out) <= tc._length(bent)


def test_pull_tight_keeps_to_its_side_of_an_obstacle():
    detour = [(0, 0), (0, 2), (4, 2), (4, 0)]
    assert tc.pull_tight(detour, ALWAYS, lambda path: True) == [(0, 0), (4, 0)]
    bend = [(0, 0), (0, 3), (5, 3)]                               # to (5, 3): 45 degrees, then straight
    out = tc.pull_tight(bend, ALWAYS, lambda path: True)
    assert all(t in (0, 45) for t in turns(out)) and tc._length(out) < tc._length(bend)
    pin = (2, 1.2)                                                   # a pad between path and shortcut
    free = lambda path: not tc.inside(list(path), pin)              # noqa: E731
    out = tc.pull_tight(detour, ALWAYS, free)
    assert out[0] == (0, 0) and out[-1] == (4, 0) and len(out) > 2 and tc.inside(out, pin)   # still round it
    assert tc._length(out) < tc._length(detour)


def test_corners_become_two_45_degree_bends_or_an_arc():
    out = tc.chamfer([(0, 0), (5, 0), (5, 5)], 0.25, ALWAYS)
    assert len(out) == 4 and turns(out) == [45, 45]
    acute = tc.chamfer([(0, 0), (5, 0), (1, 2)], 0.25, ALWAYS)
    assert all(t < 90 for t in turns(acute))
    arcs = []
    tc.chamfer([(0, 0), (5, 0), (5, 5)], 0.25, ALWAYS, "arc", arcs)
    (a, mid, b), = arcs
    assert math.dist(mid, (5, 0)) < math.dist(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), (5, 0))   # bowed to the corner


def test_spread_moves_a_crowded_segment_away():
    pts = [(0, 0), (1, 0), (1, 1), (5, 1), (5, 0), (6, 0)]
    other = (3, 1.25)                                             # 0.25 mm below the run at y 1 ... above

    def nearest(a, b):
        return (abs(other[1] - a[1]), (0.0, -1.0 if other[1] > a[1] else 1.0)) if a[1] == b[1] == 1 else (None, (0, 0))
    out = tc.spread(pts, 0.5, nearest, ALWAYS)
    assert out[2][1] == out[3][1] == pytest.approx(0.75) and out[0] == pts[0] and out[-1] == pts[-1]


# --- on a board ---------------------------------------------------------------------------

pytestmark_board = pytest.mark.requires("kicad", "footprints")


def on(f, nets):
    for num, net in nets.items():
        node = f.pad(num).node
        node.insert(node.index(find(node, "layers")) + 1, [Sym("net"), net])


def messy(tmp_path) -> Path:
    """Net A: right-angle corners, a staircase, a spike that doubles back and a dangling stub;
    net B: an L; C: a locked L that must stay."""
    pcb = tmp_path / "m.kicad_pcb"
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 60, 40))
    on(b.add_footprint(SMD, "R1", 10, 10), {"1": "A0", "2": "A"})
    on(b.add_footprint(SMD, "R2", 40, 30), {"1": "A", "2": "A1"})
    on(b.add_footprint(SMD, "R3", 45, 5), {"1": "B0", "2": "B"})
    on(b.add_footprint(SMD, "R4", 55, 15), {"1": "B", "2": "B1"})
    on(b.add_footprint(SMD, "R5", 5, 30), {"1": "C0", "2": "C"})
    on(b.add_footprint(SMD, "R6", 15, 36), {"1": "C", "2": "C1"})
    a, z = b.footprint("R1").pad("2"), b.footprint("R2").pad("1")
    path = [(a.x, a.y), (20, a.y), (20, 12), (20.3, 12), (20.3, 12.3), (20.6, 12.3), (20.6, 12.6),
            (20.9, 12.6), (20.9, 12.9), (23, 12.9), (23, 30), (30, 30), (28, 30), (z.x, z.y)]
    for p, q in zip(path, path[1:]):
        b.add_track(p, q, 0.25, "F.Cu", "A")
    b.add_track((30, 30), (30, 34), 0.25, "F.Cu", "A")              # goes nowhere
    c, d = b.footprint("R3").pad("2"), b.footprint("R4").pad("1")
    for p, q in zip([(c.x, c.y), (50, c.y), (50, d.y)], [(50, c.y), (50, d.y), (d.x, d.y)]):
        b.add_track(p, q, 0.25, "F.Cu", "B")
    e, f = b.footprint("R5").pad("2"), b.footprint("R6").pad("1")
    for p, q in zip([(e.x, e.y), (f.x, e.y)], [(f.x, e.y), (f.x, f.y)]):
        b.add_track(p, q, 0.25, "F.Cu", "C")
    b.save()
    with kat.pcb.edit(pcb) as bb:
        bb.lock("C")
    return pcb


@pytestmark_board
def test_tidy_cleans_a_board_drc_stays_clean_and_again_changes_nothing(tmp_path, capsys):
    from kat import cli
    pcb = messy(tmp_path)
    assert set(kat.pcb.drc(pcb, parity=False)["by_type"]) == {"track_dangling"}
    locked_before = sorted((t["start"], t["end"]) for t in Board(pcb).tracks() if t["net"] == "C")
    text = pcb.read_text()
    assert cli.main(["pcb", "tidy", str(pcb), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] and dry["nets"] == ["A", "B"] and pcb.read_text() == text        # nothing saved
    assert dry["before"]["F.Cu"]["reversals"] == 1
    assert cli.main(["pcb", "tidy", str(pcb)]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["nets"] == ["A", "B"] and not res["rolled_back"], res
    for net, (before, after) in res["length"].items():
        assert after < before, net                                # steps 1-4 never lengthen
    assert res["after"]["F.Cu"]["reversals"] == 0 and res["check"]["drc_errors"] == 0, res
    b = Board(pcb)
    assert not [t for t in b.tracks() if t["net"] == "A" and max(t["start"][1], t["end"][1]) > 33]   # stub gone
    assert sorted((t["start"], t["end"]) for t in b.tracks() if t["net"] == "C") == locked_before
    again = kat.pcb.tidy_tracks(pcb)
    assert again["nets"] == [] and sum(again["steps"].values()) == 0


@pytestmark_board
def test_corners_only_and_a_net_drc_dislikes_is_put_back(tmp_path, monkeypatch):
    pcb = messy(tmp_path)
    r = kat.pcb.tidy_tracks(pcb, nets=["B"], steps=["corners"])
    assert r["steps"]["corners"] == 1 and r["nets"] == ["B"]
    assert Board(pcb).tracks() and kat.pcb.check(pcb)["by_type"].get("tracks_crossing") is None
    shape = kat.pcb.trackclean.metrics(Board(pcb))["F.Cu"]["corners"]
    assert shape["45"] >= 2
    # DRC finding something on net A after the change: A goes back as it was
    from kat.pcb import checks
    real = checks.drc
    before = sorted((t["start"], t["end"]) for t in Board(pcb).tracks() if t["net"] == "A")

    def drc(path, parity=True, refill=False, save=False):
        d = real(path, parity, refill, save)
        if sorted((t["start"], t["end"]) for t in Board(path).tracks() if t["net"] == "A") != before:
            d["violations"] += [{"severity": "error", "type": "clearance", "description": "x",       # more than
                                 "items": [{"description": "Track [A] on F.Cu", "x": 0, "y": k}]}    # the stub's
                                for k in range(2)]
        return d
    monkeypatch.setattr(checks, "drc", drc)
    r = kat.pcb.tidy_tracks(pcb, nets=["A"])
    assert r["rolled_back"] == ["A"] and r["nets"] == [], r
    assert sorted((t["start"], t["end"]) for t in Board(pcb).tracks() if t["net"] == "A") == before


@pytestmark_board
def test_the_check_reports_the_shape_of_the_routing(tmp_path):
    pcb = messy(tmp_path)
    shape = kat.pcb.check(pcb)["routing_shape"]["F.Cu"]
    assert shape["reversals"] == 1 and shape["corners"]["90"] >= 4 and shape["short"] >= 4

