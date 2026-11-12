"""Route polishing solver (needs torch): every accepted state independently feasible, exact
snaps, a snap refused by an obstacle, a bounded bend attempt, the run directory."""

import json
import math

import pytest

import kat
from kat import cli

torch = pytest.importorskip("torch")
np = pytest.importorskip("numpy")
from kat.pcb.polish import case as pc, constraints as cs, merit as mt, solver as sv  # noqa: E402
from test_polish_case import EXAMPLE, tiny  # noqa: E402


def run(c, **limits):
    s = sv.Solver(c, options={"settle_iterations": 5, "stagnation_window": 10}, limits=limits)
    return s, list(s.events())


def test_the_example_improves_and_every_accepted_state_is_feasible():
    s, evs = run(pc.load(EXAMPLE), max_iterations=60)
    check = cs.Model(pc.load(EXAMPLE))                      # a fresh model: independent of the run
    assert all(not check.violations(e.traces) for e in evs if e.accepted)
    assert evs[0].type == "start" and evs[-1].type == "stop" and s.termination == "max-iterations"
    res = s.result()
    assert res["score"]["total"] < 0.5 * res["initial_score"]["total"]
    snapped = [(t["id"], i) for t in res["traces"] for i in t["snapped"]]
    assert snapped
    for t in res["traces"]:                                   # snapped means exact
        p = np.array(t["points"])
        assert all(mt.deviation_deg(p[i], p[i + 1]) < 1e-6 for i in t["snapped"])
    again = sv.Solver(pc.load(EXAMPLE), options={"settle_iterations": 5, "stagnation_window": 10},
                      limits={"max_iterations": 20})
    first = [e.traces for e in again.events()]
    third = sv.Solver(pc.load(EXAMPLE), options={"settle_iterations": 5, "stagnation_window": 10},
                      limits={"max_iterations": 20})
    assert all(np.array_equal(a[k], b[k]) for a, b in zip(first, (e.traces for e in third.events())) for k in a)


def test_a_snap_blocked_by_a_hole_is_refused_and_a_bend_is_bounded():
    d = tiny(points=((2, 10), (10, 10.6), (18, 10)), holes=((10, 9.6, 0.3),))
    d["limits"].update(snap_tolerance_deg=5, max_added_vertices_per_trace=1)
    s, evs = run(pc.loads(json.dumps(d)), max_iterations=80)
    snaps = [e for e in evs if e.type == "snap" and e.target == "t01#0"]
    assert snaps and not snaps[0].accepted and snaps[0].reason
    assert any(e.type == "adjust" for e in evs) and any(e.type == "bend" for e in evs)
    assert all(len(e.traces["t01"]) <= 3 + 1 for e in evs)
    m = cs.Model(pc.loads(json.dumps(d)))
    assert all(not m.violations(e.traces) for e in evs if e.accepted)
    hole = np.array([10, 9.6])
    assert all(cs.to_polyline(hole[None], e.traces["t01"])[0] > 0.3 for e in evs)   # still above the hole


def test_projection_makes_snapped_directions_exact():
    p = np.array([[0, 0], [4, 0.3], [6, 2.4], [10, 3]], float)
    q = sv.project(p, {0: 0, 1: 1})
    assert q[0].tolist() == [0, 0] and q[-1].tolist() == [10, 3]
    assert abs(q[1, 1]) < 1e-12 and abs((q[2, 0] - q[1, 0]) - (q[2, 1] - q[1, 1])) < 1e-12
    assert sv.project(np.array([[0, 0], [1, 0.5]]), {0: 0}) is None      # fixed ends, cannot hold


def test_run_directory_and_replay(tmp_path, capsys):
    out = tmp_path / "run"
    res = kat.pcb.polish_run(EXAMPLE, out, limits={"max_iterations": 15})
    assert res["termination"] == "max-iterations" and res["result"]["total"] < res["initial"]["total"]
    rec = json.loads((out / "run.json").read_text())
    assert rec["input"]["sha256"] == pc.load(EXAMPLE).sha256 and rec["solver"]["merit"] == mt.VERSION
    lines = (out / "history.jsonl").read_text().splitlines()
    assert len(lines) == rec["events"] and json.loads(lines[rec["best_seq"]])["total"] == pytest.approx(res["result"]["total"], abs=1e-4)
    with pytest.raises(kat.KatError, match="already holds"):
        kat.pcb.polish_run(EXAMPLE, out, limits={"max_iterations": 1})
    assert cli.main(["--no-report", "pcb", "polish", "replay", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["iterations"] == 15
    assert cli.main(["--no-report", "pcb", "polish", "run", str(EXAMPLE), "--out", str(tmp_path / "r2"),
                     "--weight", "nope=1"]) == 2
    assert math.isfinite(rec["events"])
