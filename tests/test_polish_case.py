"""Route polishing input (specs/ROUTE_POLISH_INPUT.md): the strict reader, preflight, and the
hard constraints' swept moves (conservative advancement keeps the topology)."""

import copy
import json
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


def tiny(points=((2, 10), (10, 10), (18, 10)), holes=((10, 12, 0.5),), max_disp=5.0) -> dict:
    """One trace across a 20 x 20 board, a hole beside it."""
    d = example()
    d["board"] = {"outline": [[0, 0], [20, 0], [20, 20], [0, 20]], "cutouts": []}
    d["movement_region"] = [[1, 1], [19, 1], [19, 19], [1, 19]]
    d["rules"]["pair_clearances"] = []
    d["traces"] = [{"id": "t01", "net": "n01", "width_mm": 0.2, "points": [list(p) for p in points]}]
    d["fixed_copper"], d["keepouts"] = [], []
    d["holes"] = [{"id": f"h{i:02d}", "center": [x, y], "radius_mm": r} for i, (x, y, r) in enumerate(holes, 1)]
    d["groups"] = [{"id": "g01", "trace_ids": ["t01"]}]
    d["limits"]["max_displacement_mm"] = max_disp
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


def test_v2_anchors_waive_only_their_own_copper():
    d = to_v2(example())
    d["fixed_copper"].append({"id": "p01", "net": "n01", "shape": {"type": "circle", "center": [3, 5], "radius_mm": 0.3}})
    c = pc.loads(json.dumps(d))
    assert not cs.preflight(c)["feasible"]                           # same net, not an anchor: clearance
    d["traces"][0]["anchors"] = ["p01"]
    assert cs.preflight(pc.loads(json.dumps(d)))["feasible"]
    d["traces"][1]["anchors"] = ["p01"]                              # n02 cannot anchor on n01 copper
    with pytest.raises(pc.CaseError, match="has net n01"):
        pc.loads(json.dumps(d))


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
    assert "displacement" in m.sweep(below, {"t01": np.array([[2, 10], [10, 4], [18, 10]], float)})
    assert kat.pcb.polish_check(EXAMPLE)["ok"]
