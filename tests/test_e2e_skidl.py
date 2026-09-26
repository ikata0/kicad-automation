"""Full iterative workflow: SKiDL design -> sync -> validate -> design change -> sync ->
validate -> manual arrangement -> validate -> sync again (it must survive).

Set KAT_E2E_OUT=/some/dir to keep the project and its snapshot history for review.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import cli
from kat.schematic import Schematic

HERE = Path(__file__).parent / "e2e"

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
    reason="needs kicad-cli and skidl")


def design(variant: str, out: Path) -> Path:
    net = out / f"tia_{variant}.net"
    subprocess.run([sys.executable, str(HERE / "tia_design.py"), variant, str(net)],
                   check=True, capture_output=True, cwd=out)
    return net


def run_kat(capsys, *argv) -> dict:
    code = cli.main([str(a) for a in argv])
    out = json.loads(capsys.readouterr().out)
    assert code in (0, 1) and "error" not in out, out
    return out


def assert_clean(check: dict) -> None:
    assert check["erc"]["errors"] == 0, check["erc"]
    assert check["erc"]["warnings"] == 0, check["erc"]
    assert check["netlist_vs_expected"]["match"], check["netlist_vs_expected"]
    assert not check["overlaps"], check["overlaps"]
    assert not check["body_overlaps"], check["body_overlaps"]
    assert not check["off_page"], check["off_page"]
    assert check["clean"]


def snapshot(capsys, sch: Path, slug: str, net: Path, note: str) -> dict:
    snap = run_kat(capsys, "snapshot", sch, "--slug", slug, "--expect", net, "--note", note)
    assert Path(snap["images"][0]["png"]).stat().st_size > 1000
    check = json.loads((Path(snap["dir"]) / "check.json").read_text())
    assert_clean(check)
    return snap


def placements(sch: Path) -> dict:
    """tag -> (ref, unit, x, y) of every design part."""
    return {(s.field("SKiDL Tag"), s.unit): (s.ref, s.place.x, s.place.y)
            for s in Schematic(sch).symbols() if not s.ref.startswith("#")}


def test_iterative_design_flow(tmp_path, capsys):
    root = Path(os.environ["KAT_E2E_OUT"]) if os.environ.get("KAT_E2E_OUT") else tmp_path
    if root.exists() and root != tmp_path:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    sch = root / "tia" / "tia.kicad_sch"

    # 1. initial SKiDL design
    v1 = design("v1", root)

    # 2. translate to KiCad
    run_kat(capsys, "new", sch)
    report = run_kat(capsys, "sync", sch, v1)
    assert {a["ref"] for a in report["added"]} == {"U1", "R1", "C1", "J1", "J2", "C2", "C3"}
    assert not report["unresolved"]

    # 3. validate: ERC, connectivity, readability findings, rendered image
    assert_clean(report["check"])
    snapshot(capsys, sch, "v1-import", v1, "Initial import of design v1.")
    before = placements(sch)

    # 4. change the design: add R2, rename C1 -> CF1
    v2 = design("v2", root)

    # 5. sync and validate
    report = run_kat(capsys, "sync", sch, v2)
    assert report["renamed"] == [{"from": "C1", "to": "CF1"}]
    assert [a["ref"] for a in report["added"]] == ["R2"]
    assert report["detached"] == ["J2.1"]  # output connector moved from TIA_OUT to OUT
    assert not report["removed"] and not report["unresolved"]
    assert_clean(report["check"])
    after = placements(sch)
    for key, (ref, x, y) in before.items():
        new_ref, nx, ny = after[key]
        assert (nx, ny) == (x, y), f"{ref} moved during sync"
    assert after[("c_fb", 1)][0] == "CF1"
    lowest = max(y for _, _, y in before.values())
    assert after[("r_out", 1)][2] > lowest
    snapshot(capsys, sch, "v2-sync", v2, "Design v2: R2 added, C1 renamed CF1.")

    # 6. arrange the drawing for readability and validate
    subprocess.run([sys.executable, str(HERE / "arrange_v2.py"), str(sch)], check=True)
    snapshot(capsys, sch, "v2-arrange", v2, "Manual arrangement: signal flow left to right.")

    # 7. the arrangement survives a further sync of the unchanged design
    text = sch.read_text()
    placements_before_v3 = placements(sch)
    again = kat.sync(sch, v2)
    assert not again["changed"], {k: v for k, v in again.items() if v and k != "check"}
    assert sch.read_text() == text

    # 8. ready for the PCB: the gate names what is missing; v3 adds footprints and MPNs
    gate = kat.ready(sch, expect=v2)
    assert not gate["ready"]
    # U1 has a footprint from its library symbol already
    assert gate["by_problem"]["no_footprint"] == 7 and gate["by_problem"]["missing_field"] == 8
    v3 = design("v3", root)
    report = kat.sync(sch, v3)
    assert not report["added"] and not report["removed"] and not report["detached"]
    assert_clean(report["check"])
    gate = kat.ready(sch, expect=v3)
    assert gate["ready"], gate["errors"]
    assert sch.read_text() != text and placements(sch) == placements_before_v3

    # 9. the board: parts and nets from the schematic, proven by DRC schematic parity
    board = kat.pcb.sync(sch.with_suffix(".kicad_pcb"))
    assert board["ok"] and board["check"]["parity"] == 0 and len(board["added"]) == 8
    assert not kat.pcb.sync(sch.with_suffix(".kicad_pcb"))["changed"]
