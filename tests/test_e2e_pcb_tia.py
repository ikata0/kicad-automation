"""Full board flow for the TIA: SKiDL v3 -> schematic -> gate -> board -> placement ->
structure -> route -> finish -> fabrication -> release gate; then a design change during
layout (v4: R1 in 0805, extra capacitor C4) back through the same gates.

Needs kicad-cli, SKiDL, Java 21+ and Freerouting (`kat pcb route --install`).
Set KAT_E2E_OUT=/some/dir to keep the project, snapshots and fabrication files.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat.pcb import Board, router
from test_e2e_skidl import HERE, design

pytestmark = [
    pytest.mark.skipif(shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
                       reason="needs kicad-cli and skidl"),
    pytest.mark.skipif(shutil.which("java") is None or not router.jar_path().exists(),
                       reason="Freerouting not installed (kat pcb route --install)"),
]


def board_pass(pcb: Path, name: str) -> None:
    subprocess.run([sys.executable, str(HERE / "tia_board.py"), str(pcb), name], check=True)


def clean(pcb: Path) -> dict:
    c = kat.pcb.check(pcb)
    assert c["clean"], {k: c[k] for k in ("by_type", "unconnected", "islands", "parity", "outside_outline")}
    return c


def test_tia_board_from_design_to_fabrication_and_back(tmp_path):
    root = Path(os.environ["KAT_E2E_OUT"]) / "pcb_tia" if os.environ.get("KAT_E2E_OUT") else tmp_path
    if root.exists() and root != tmp_path:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    sch, pcb = root / "tia" / "tia.kicad_sch", root / "tia" / "tia.kicad_pcb"

    # schematic: v2 imported and arranged, then v3 adds footprints and part numbers
    v2, v3 = design("v2", root), design("v3", root)
    kat.new(sch)
    kat.sync(sch, v2)
    subprocess.run([sys.executable, str(HERE / "arrange_v2.py"), str(sch)], check=True)
    assert kat.sync(sch, v3)["check"]["clean"]
    with kat.edit(sch) as s:
        s.title(title="TIA", rev="A", date="2026-09-26")
    assert kat.ready(sch, expect=v3)["ready"]

    # board
    r = kat.pcb.sync(pcb)
    assert r["ok"] and len(r["added"]) == 8
    board_pass(pcb, "place")
    board_pass(pcb, "structure")
    kat.pcb.snapshot(pcb, slug="structure", note="placed; GND vias; decoupling", three_d=False)
    routed = kat.pcb.route(pcb, passes=15, timeout=600)
    assert routed["check"]["unconnected"] == 0 and routed["check"]["drc_errors"] == 0, routed
    board_pass(pcb, "finish")
    kat.pcb.refill(pcb)
    clean(pcb)
    kat.pcb.snapshot(pcb, slug="rev-A", note="routed and finished", three_d=False)
    kat.pcb.fab(pcb, step=False, three_d=False)
    gate = kat.pcb.ready(pcb)
    assert gate["ready"], gate["errors"]

    # design change during layout: SKiDL v4 -> schematic -> gate -> board
    v4 = design("v4", root)
    before = {f.ref: (f.x, f.y) for f in Board(pcb).footprints()}
    s4 = kat.sync(sch, v4)
    assert [a["ref"] for a in s4["added"]] == ["C4"] and s4["check"]["ok"]
    assert kat.ready(sch, expect=v4, require=["MPN"])["by_problem"].get("missing_field") is None
    r = kat.pcb.sync(pcb, force=True)          # the new capacitor still sits in the import rows
    assert r["replaced"] == [{"ref": "R1", "from": "Resistor_SMD:R_0603_1608Metric",
                              "to": "Resistor_SMD:R_0805_2012Metric"}]
    assert [a["ref"] for a in r["added"]] == ["C4"] and r["check"]["parity"] == 0
    after = {f.ref: (f.x, f.y) for f in Board(pcb).footprints()}
    assert all(after[k] == v for k, v in before.items())          # nothing moved
    kat.pcb.sync(pcb, force=True, rip_stale=True)                 # copper that no longer fits
    board_pass(pcb, "v4")
    routed = kat.pcb.route(pcb, passes=15, timeout=600)
    assert routed["check"]["unconnected"] == 0 and routed["check"]["drc_errors"] == 0, routed
    with kat.pcb.edit(pcb) as b:
        b.place_refs(["C4", "R1"])
    kat.pcb.refill(pcb)
    clean(pcb)
    kat.pcb.fab(pcb, step=False, three_d=False)
    assert kat.pcb.ready(pcb)["ready"]
