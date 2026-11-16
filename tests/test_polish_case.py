"""Route polishing input (specs/ROUTE_POLISH_INPUT.md): the strict reader, preflight, and the
hard constraints' swept moves (conservative advancement keeps the topology)."""

import copy
import json
import math
import os
from pathlib import Path

import pytest

import kat
from kat import cli
from kat.pcb.polish import case as pc

np = pytest.importorskip("numpy")
from kat.pcb.polish import constraints as cs  # noqa: E402

EXAMPLE = Path(__file__).parent.parent / "specs" / "route_polish_example.json"


def example() -> dict:
    return json.loads(EXAMPLE.read_text())


def tiny(points=((2, 10), (10, 10), (18, 10)), holes=((10, 12, 0.5),)) -> dict:
    """One trace across a 20 x 20 board, a hole beside it."""
    d = example()
    d["board"] = {"outline": [[0, 0], [20, 0], [20, 20], [0, 20]], "cutouts": []}
    d["movement_region"] = [[1, 1], [19, 1], [19, 19], [1, 19]]
    d["rules"]["pair_clearances"] = []
    d["traces"] = [{"id": "t01", "net": "n01", "width_mm": 0.2, "points": [list(p) for p in points]}]
    d["fixed_copper"], d["keepouts"] = [], []
    d["holes"] = [{"id": f"h{i:02d}", "center": [x, y], "radius_mm": r} for i, (x, y, r) in enumerate(holes, 1)]
    d["groups"] = [{"id": "g01", "trace_ids": ["t01"]}]
    return d


def to_v2(d: dict) -> dict:
    d = copy.deepcopy(d)
    d["version"], d["scope"]["profile"] = 2, "single-layer-anchored-paths"
    for t in d["traces"]:
        t["anchors"] = []
    return d


def test_the_example_passes_preflight(capsys):
    c = pc.load(EXAMPLE)
    assert c.version == 1 and list(c.groups["g01"]) == ["t01", "t02", "t03", "t04", "t05"]
    assert cli.main(["--no-report", "pcb", "polish", "check", str(EXAMPLE)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and not out["violations"] and len(out["neighbours"]) == 4
    assert min(n["gap"] for n in out["neighbours"]) > c.copper_clearance


def _set(path, value):
    def edit(d):
        *head, last = path
        for k in head:
            d = d[k]
        d[last] = value
    return edit


@pytest.mark.parametrize("edit, says", [
    (_set(["extra"], 1), "unsupported extra"),
    (_set(["version"], 3), "version 3"),
    (_set(["scope", "context_complete"], False), "context_complete"),
    (_set(["keepouts", 0, "points"], [[15, 30], [17.5, 33], [17.5, 30], [15, 33]]), "self-intersecting"),
    (_set(["rules", "pair_clearances", 0, "b"], "zz"), "distinct existing objects"),
    (_set(["traces", 1, "net"], "n01"), "distinct net"),
    (_set(["traces", 0, "points", 1], [3, 5]), "zero length"),
    (_set(["groups"], []), "in no group"),
    (_set(["limits", "snap_tolerance_deg"], 30), "snap_tolerance_deg"),
    (_set(["limits", "max_displacement_mm"], 1.5), "max_displacement_mm: no longer supported"),
])
def test_malformed_inputs_are_rejected(edit, says):
    d = example()
    edit(d)
    with pytest.raises(pc.CaseError, match=says):
        pc.loads(json.dumps(d))


def test_duplicate_keys_and_non_finite_numbers_are_rejected():
    text = EXAMPLE.read_text()
    with pytest.raises(pc.CaseError, match="duplicate key 'units'"):
        pc.loads(text.replace('"units": "mm",', '"units": "mm", "units": "mm",', 1))
    with pytest.raises(pc.CaseError, match="non-finite"):
        pc.loads(text.replace('"width_mm": 0.2', '"width_mm": NaN', 1))


def test_an_infeasible_input_fails_preflight():
    d = example()
    d["traces"][4]["points"][3] = [16.0, 18.5]                     # into hole h01
    res = cs.preflight(pc.loads(json.dumps(d)))
    assert not res["feasible"] and any("h01" in v for v in res["violations"])


@pytest.mark.parametrize("y, recoverable", [(10.7, True), (10.3, False)])
def test_check_says_whether_a_run_can_recover(tmp_path, y, recoverable):
    # 0.15 mm short of the hole clearance: the run may work it off; the centreline in the
    # hole's copper: no move can repair that
    f = tmp_path / "case.json"
    f.write_text(json.dumps(tiny(points=((2, 10), (18, 10)), holes=((10, y, 0.5),))))
    res = kat.pcb.polish_check(f)
    assert not res["ok"] and res["recoverable"] is recoverable
    assert bool(res["blocking"]) is not recoverable


def test_v2_anchors_waive_only_their_own_copper():
    d = to_v2(example())
    d["fixed_copper"].append({"id": "p01", "net": "n01", "shape": {"type": "circle", "center": [3, 5], "radius_mm": 0.3}})
    assert cs.preflight(pc.loads(json.dumps(d)))["feasible"]          # same net at its end: a joint
    d["fixed_copper"][-1]["net"] = "n09"
    assert not cs.preflight(pc.loads(json.dumps(d)))["feasible"]      # foreign net: clearance
    d["fixed_copper"][-1]["net"] = "n01"
    d["traces"][0]["anchors"] = ["p01"]
    assert cs.preflight(pc.loads(json.dumps(d)))["feasible"]
    d["traces"][1]["anchors"] = ["p01"]                              # n02 cannot anchor on n01 copper
    with pytest.raises(pc.CaseError, match="has net n01"):
        pc.loads(json.dumps(d))


def test_same_net_runs_joined_at_their_ends_need_no_clearance():
    d = tiny(points=((2, 10), (10, 10)), holes=())
    d["traces"].append({"id": "t02", "net": "n01", "width_mm": 0.2, "points": [[10.1, 10], [18, 10]]})
    d["groups"][0]["trace_ids"] = ["t01", "t02"]
    d = to_v2(d)
    assert cs.preflight(pc.loads(json.dumps(d)))["feasible"]          # end caps overlap 0.1 mm apart
    assert cs.joints(pc.loads(json.dumps(d)), "t01") == {"t02"}
    d["traces"][1]["net"] = "n02"                                     # foreign net: still constrained
    assert not cs.preflight(pc.loads(json.dumps(d)))["feasible"]
    d["traces"][1]["net"] = "n01"
    d["fixed_copper"] = [{"id": "c01", "net": "n01",                  # a stub on t01's body: no joint
                          "shape": {"type": "polyline", "points": [[6, 10.05], [6, 13]], "width_mm": 0.2}}]
    res = cs.preflight(pc.loads(json.dumps(d)))
    assert not res["feasible"] and all("c01" in v for v in res["violations"])


def test_pair_clearances_take_the_maximum():
    c = pc.load(EXAMPLE)
    m = cs.Model(c)
    j = m.fowner.index("h01")
    assert m.req[m.ids.index("t05"), j] == pytest.approx(0.1 + 0.7 + 0.4)
    assert m.req[m.ids.index("t04"), j] == pytest.approx(0.1 + 0.7 + 0.25)


def test_a_move_through_a_hole_is_refused_although_both_ends_are_clear():
    m = cs.Model(pc.loads(json.dumps(tiny())))
    below = m.initial()
    above = {"t01": np.array([[2, 10], [10, 14], [18, 10]], float)}
    assert not m.violations(above)                                  # clear on the other side
    assert m.sweep(below, above) == "sweep"
    nudge = {"t01": np.array([[2, 10], [10, 9.5], [18, 10]], float)}
    assert m.sweep(below, nudge) is None
    assert m.sweep(below, {"t01": np.array([[2, 10], [10, 4], [18, 10]], float)}) is None   # no distance limit
    d = tiny()                                                       # exactly at clearance: may move away
    d["holes"][0]["center"][1] = 10 + 0.1 + 0.5 + d["rules"]["hole_clearance_mm"]
    m = cs.Model(pc.loads(json.dumps(d)))
    assert abs(m.min_slack(m.initial())["value"]) < 1e-12
    away, closer = ({"t01": np.array([[2, 10], [10, 10 + dy], [18, 10]], float)} for dy in (-0.001, 0.001))
    assert m.sweep(m.initial(), away) is None
    assert m.sweep(m.initial(), closer).startswith("clearance")
    assert kat.pcb.polish_check(EXAMPLE)["ok"]


def test_pruned_checks_match_measuring_every_pair():
    """The spatial index leaves out only what cannot matter: a board dense with pads, moves
    short and far (past ``SKIN``)."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("scipy")
    from kat.pcb.polish.merit import Merit
    d = example()
    (x0, y0), (x1, y1) = np.min(d["movement_region"], 0), np.max(d["movement_region"], 0)
    board = np.asarray(d["board"]["outline"], float)
    corners = 0.5 * np.array([[1, 1], [1, -1], [-1, 1], [-1, -1]])
    on_board = lambda p: cs.inside(p + corners, board).all() and not any(
        cs.inside(p + corners, np.asarray(cut, float)).any() for cut in d["board"]["cutouts"])
    grid = [np.array((x, y)) for x in np.arange(x0 + 1, x1 - 1, 1.3) for y in np.arange(y0 + 1, y1 - 1, 1.1)]
    for i, (x, y) in enumerate(p for p in grid if on_board(p)):
        if i % 4 == 0:                                   # large round pads: a wide range of required distances
            shape = {"type": "circle", "center": [x, y], "radius_mm": 0.2 + 0.1 * (i % 5)}
        else:
            r = 0.15 + 0.1 * (i % 3)
            shape = {"type": "polygon", "points": [[x + r * math.cos(a), y + r * math.sin(a)]
                                                   for a in np.linspace(0, 2 * math.pi, 9)[:-1]]}
        d["fixed_copper"].append({"id": f"p{i:03d}", "net": "nx", "shape": shape})
    c = pc.loads(json.dumps(d))
    fast, ref = cs.Model(c), cs.Model(c, prune=False)
    assert fast.prune and len(fast.fa) > 500
    start = fast.initial()
    rng = np.random.default_rng(7)
    for scale in (0.03, 0.3, 3.0):
        new = {t: p.copy() for t, p in start.items()}
        for t in new:
            new[t][1:-1] += rng.normal(0, scale, new[t][1:-1].shape)
        a, b, tr, _ = fast._segments(new)
        slack = cs.seg_dist(a[:, None], b[:, None], fast.fa[None], fast.fb[None]) - fast.req[tr]
        for most in (0.05, 0.4, 1.5):
            k, j = fast.candidates(a, b, most)
            assert len(k) < slack.size and set(zip(*np.nonzero(slack <= most))) <= set(zip(k, j))
        for name in ("violations", "min_slack", "shortfall", "short_pairs", "blocking"):
            assert getattr(fast, name)(new) == getattr(ref, name)(new), (scale, name)
        (f, m), (rf, rm) = fast.pair_slack(new), ref.pair_slack(new)
        assert np.array_equal(m, rm)
        assert np.array_equal(np.where(rf <= cs.SKIN, rf, np.inf), np.where(f <= cs.SKIN, f, np.inf))
        assert fast.sweep(start, new, 10_000) == ref.sweep(start, new, 10_000), scale
        grads = []
        for model in (fast, ref):
            P = {t: torch.tensor(p, requires_grad=True) for t, p in new.items()}
            b = Merit(model, c.objectives).barrier(P)
            b.backward()
            grads.append((float(b.detach()), torch.cat([P[t].grad.ravel() for t in model.ids])))
        assert grads[0][0] == pytest.approx(grads[1][0], rel=1e-12, abs=1e-12)
        assert torch.allclose(grads[0][1], grads[1][1], rtol=1e-12, atol=1e-12)


@pytest.mark.skipif(not os.environ.get("KAT_POLISH_BENCH"), reason="set KAT_POLISH_BENCH=<case.json> (a large case)")
def test_bench_pruned_checks_on_a_large_case():
    """Opt-in timing of the hard checks and the barrier, pruned against every pair, on a
    real case: ``KAT_POLISH_BENCH=<case.json> pytest -s -k bench``."""
    import time
    torch = pytest.importorskip("torch")
    from kat.pcb.polish.merit import Merit
    c = pc.load(os.environ["KAT_POLISH_BENCH"])
    group = os.environ.get("KAT_POLISH_BENCH_GROUP")
    t0 = time.perf_counter()
    fast = cs.Model(c, group)
    setup = time.perf_counter() - t0
    ref = cs.Model(c, group, prune=False)
    start = fast.initial()
    new = {t: p.copy() for t, p in start.items()}
    for t in new:
        new[t][1:-1] += np.random.default_rng(1).normal(0, 0.02, new[t][1:-1].shape)

    def timed(f, *args):
        t = time.perf_counter()
        return f(*args), time.perf_counter() - t

    def barrier(model):
        P = {t: torch.tensor(p, requires_grad=True) for t, p in new.items()}
        b = Merit(model, c.objectives).barrier(P)
        b.backward()
        return float(b.detach())

    print(f"\n{len(fast.ids)} traces, {len(fast.fa)} fixed primitives; setup {setup:.2f} s")
    for name, call in [("violations", lambda m: m.violations(new)), ("min_slack", lambda m: m.min_slack(new)),
                       ("sweep", lambda m: m.sweep(start, new)), ("barrier", barrier)]:
        (a, ta), (b, tb) = timed(call, fast), timed(call, ref)
        print(f"{name:10} pruned {ta * 1e3:8.1f} ms   every pair {tb * 1e3:8.1f} ms   x{tb / ta:.0f}")
        assert a == pytest.approx(b, rel=1e-12) if name == "barrier" else a == b
