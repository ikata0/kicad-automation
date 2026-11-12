"""Route polishing on a board: extract a bundle, polish it, apply the result to a copy that
KiCad's DRC judges; the input board is never written."""

import json
from pathlib import Path

import pytest

import kat
from kat import cli
from kat.pcb import Board
from kat.pcb.polish import case as pc
from test_pcb_tidy import SMD, on

pytest.importorskip("torch")
pytestmark = pytest.mark.requires("kicad", "footprints")


def bundle(pcb: Path) -> Path:
    """Four 0.25 mm tracks from a column of resistors to another, with diagonals a little
    off 45 degrees and uneven corners."""
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 60, 40))
    for k in range(4):
        on(b.add_footprint(SMD, f"R{k + 1}", 10, 10 + 2 * k), {"1": f"L{k}", "2": f"B{k}"})
        on(b.add_footprint(SMD, f"R{k + 11}", 50, 20 + 2 * k), {"1": f"B{k}", "2": f"M{k}"})
    for k in range(4):
        a, z = b.footprint(f"R{k + 1}").pad("2"), b.footprint(f"R{k + 11}").pad("1")
        jog = (0.0, 0.35, -0.2, 0.15)[k]
        path = [(a.x, a.y), (20 + jog, a.y), (31 + jog, z.y), (z.x, z.y)]
        for p, q in zip(path, path[1:]):
            b.add_track(p, q, 0.25, "F.Cu", f"B{k}")
    b.save()
    return pcb


def test_extract_polish_and_apply_to_a_copy(tmp_path, capsys):
    pcb = bundle(tmp_path / "bundle.kicad_pcb")
    original = pc.sha256_of(pcb)
    case = tmp_path / "case.json"
    assert cli.main(["--no-report", "pcb", "polish", "extract", str(pcb), "--net", "B*", "--out", str(case)]) == 0
    ex = json.loads(capsys.readouterr().out)
    assert ex["ok"] and ex["order"] == ["B3", "B2", "B1", "B0"] and ex["layer"] == "F.Cu"
    assert all(t["anchors"] == 2 for t in ex["traces"]) and ex["rules"]["copper"] == 0.2
    c = pc.load(case)
    assert c.version == 2 and c.provenance["kind"] == "private-board-extract"
    assert "B0" not in case.read_text()                              # net names only in the map
    run = tmp_path / "run"
    res = kat.pcb.polish_run(case, run, weights={"displacement": 0.05}, limits={"max_iterations": 60})
    assert res["result"]["total"] < res["initial"]["total"]
    out = tmp_path / "out" / "polished.kicad_pcb"
    ap = kat.pcb.polish_apply(pcb, run / "result.json", tmp_path / "case.map.json", out)
    assert ap["ok"] and ap["round_trip"]["ok"] and not ap["drc"]["worse_nets"], ap
    assert ap["drc"]["after"] == {"errors": 0, "unconnected": 0}
    assert pc.sha256_of(pcb) == original                             # never written
    moved = {(t["start"], t["end"]) for t in Board(out).tracks()} - {(t["start"], t["end"]) for t in Board(pcb).tracks()}
    assert moved
    with pytest.raises(kat.KatError, match="exists"):
        kat.pcb.polish_apply(pcb, run / "result.json", tmp_path / "case.map.json", out)
    with kat.pcb.edit(pcb) as b:                                     # the board moves on: the map is stale
        b.add_track((5, 35), (8, 35), 0.25, "F.Cu", "L0")
    with pytest.raises(kat.KatError, match="changed since the extraction"):
        kat.pcb.polish_apply(pcb, run / "result.json", tmp_path / "case.map.json", tmp_path / "o2.kicad_pcb")


def test_rules_it_cannot_model_stop_the_extraction(tmp_path):
    pcb = bundle(tmp_path / "bundle.kicad_pcb")
    pcb.with_suffix(".kicad_dru").write_text('(version 1)\n(rule "len" (condition "A.NetName == \'B1\'")\n'
                                             '  (constraint length (min 40mm)))\n')
    with pytest.raises(kat.KatError, match="cannot model.*length"):
        kat.pcb.polish_extract(pcb, ["B*"], tmp_path / "case.json")
    res = kat.pcb.polish_extract(pcb, ["B*"], tmp_path / "case.json", accept_unmodelled=True)
    assert res["ok"] and any("length" in u for u in res["unmodelled"])
    assert any("length" in u for u in json.loads((tmp_path / "case.map.json").read_text())["unmodelled"])


def test_an_empty_selection_says_why(tmp_path):
    pcb = bundle(tmp_path / "bundle.kicad_pcb")
    for nets, kw, says in ((["B7"], {}, "no net on the board matches B7"),
                           (["/B1"], {}, r"matches /B1 \(similar: B1\)"),
                           (["M*"], {}, "exist but have no tracks"),
                           (["B*"], {"layer": "B.Cu"}, "no tracks on B.Cu .they run on F.Cu"),
                           (["B*"], {"region": (0, 0, 5, 5)}, "inside the region")):
        with pytest.raises(kat.KatError, match=says):
            kat.pcb.polish_extract(pcb, nets, tmp_path / "case.json", **kw)
