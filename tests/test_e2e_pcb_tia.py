"""Full board flow for the TIA: SKiDL v3 -> schematic -> gate -> board -> placement ->
structure -> route -> finish -> fabrication -> release gate; then a design change during
layout (v4: R1 in 0805, extra capacitor C4) back through the same gates.

Driven like an agent: kat commands, passes through `kat batch-exec` (tests/e2e/tia_passes/),
comments - so tia/tia_REPORT.md is a complete design journal, checked at the end.

Needs kicad-cli, SKiDL, Java 21+ and Freerouting (`kat pcb route --install`).
Set KAT_E2E_OUT=/some/dir to keep the project, snapshots, report and fabrication files.
With -m kicad_gui the finished board and schematic are also opened in KiCad (--verify).
"""

import contextlib
import io
import json
import os
import re
import shutil
from pathlib import Path

import pytest

import kat
from kat import cli
from kat.pcb import Board
from test_e2e_skidl import HERE, design

pytestmark = [
    pytest.mark.requires("render"),
    pytest.mark.requires("kicad", "symbols", "footprints", "skidl"),
    pytest.mark.requires("routing"),
]


PASSES = HERE / "tia_passes"


def k(*args) -> dict:
    """A kat command as the agent types it; returns its JSON."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main([str(a) for a in args])
    res = json.loads(out.getvalue()[out.getvalue().rindex("\n{") + 1:] if "\n{" in out.getvalue()
                     else out.getvalue())
    assert code in (0, 1), res
    return res


def run_pass(name: str, *args) -> dict:
    res = k("batch-exec", PASSES / f"{name}.py", "--", *args)
    assert res["ok"], res["error"]
    return res


def clean(pcb: Path) -> dict:
    c = k("pcb", "check", pcb)
    assert c["clean"], {k: c[k] for k in ("by_type", "unconnected", "islands", "parity", "outside_outline")}
    return c


def test_tia_board_from_design_to_fabrication_and_back(tmp_path, request):
    root = Path(os.environ["KAT_E2E_OUT"]) / "pcb_tia" if os.environ.get("KAT_E2E_OUT") else tmp_path
    if root.exists() and root != tmp_path:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    sch, pcb = root / "tia" / "tia.kicad_sch", root / "tia" / "tia.kicad_pcb"

    # schematic: v2 imported and arranged, then v3 adds footprints and part numbers
    v2, v3 = design("v2", root), design("v3", root)
    k("new", sch)
    k("comment", "--kind", "plan", "--project", sch,
      "TIA: import the SKiDL design, arrange the sheet, then lay out a 32 x 20 mm board.")
    k("sync", sch, v2)
    assert k("batch-exec", HERE / "arrange_v2.py", "--", sch)["ok"]
    assert k("sync", sch, v3)["check"]["clean"]
    k("title", sch, "--title", "TIA", "--rev", "A", "--date", "2026-09-26")
    assert k("ready", sch, "--expect", v3)["ready"]
    k("comment", "--kind", "milestone", "--project", sch, "Schematic ready for layout.")

    # board
    r = k("pcb", "sync", pcb)
    assert r["ok"] and len(r["added"]) == 8
    run_pass("setup", pcb)
    run_pass("place1", pcb)
    run_pass("structure1", pcb)
    routed = k("pcb", "route", pcb, "--passes", 15, "--timeout", 600)
    assert routed["check"]["unconnected"] == 0 and routed["check"]["drc_errors"] == 0, routed
    run_pass("finish", pcb)
    k("pcb", "refill", pcb)
    clean(pcb)
    k("pcb", "snapshot", pcb, "--slug", "rev-A", "--note", "routed and finished", "--no-3d")
    k("pcb", "fab", pcb, "--no-step", "--no-3d")
    gate = k("pcb", "ready", pcb)
    assert gate["ready"], gate["errors"]
    k("comment", "--kind", "milestone", "--project", sch, "Rev A: routed, clean, fabrication set ready.")

    # design change during layout: SKiDL v4 -> schematic -> gate -> board
    k("comment", "--kind", "plan", "--project", sch,
      "Design change v4: R1 becomes 0805, C4 added on +2V5. Sync schematic, gate, board.")
    v4 = design("v4", root)
    before = {f.ref: (f.x, f.y) for f in Board(pcb).footprints()}
    s4 = k("sync", sch, v4)
    assert [a["ref"] for a in s4["added"]] == ["C4"] and s4["check"]["ok"]
    assert k("ready", sch, "--expect", v4)["by_problem"].get("missing_field") is None
    r = k("pcb", "sync", pcb, "--force")      # the new capacitor still sits in the import rows
    assert r["replaced"] == [{"ref": "R1", "from": "Resistor_SMD:R_0603_1608Metric",
                              "to": "Resistor_SMD:R_0805_2012Metric"}]
    assert [a["ref"] for a in r["added"]] == ["C4"] and r["check"]["parity"] == 0
    after = {f.ref: (f.x, f.y) for f in Board(pcb).footprints()}
    assert all(after[k] == v for k, v in before.items())          # nothing moved
    k("pcb", "sync", pcb, "--force", "--rip-stale")               # copper that no longer fits
    run_pass("v4", pcb)
    routed = k("pcb", "route", pcb, "--passes", 15, "--timeout", 600)
    assert routed["check"]["unconnected"] == 0 and routed["check"]["drc_errors"] == 0, routed
    with kat.pcb.edit(pcb) as b:
        b.place_refs(["C4", "R1"])
    k("pcb", "refill", pcb)
    clean(pcb)
    k("pcb", "fab", pcb, "--no-step", "--no-3d")
    assert k("pcb", "ready", pcb)["ready"]
    if "kicad_gui" in (request.config.option.markexpr or ""):
        for f in (pcb, sch):
            v = k("open", f, "--verify")
            assert v["verified"] is not False and v["ok"] is not False, v

    # the design journal
    report = sch.parent / "tia_REPORT.md"
    text = report.read_text()
    heads = re.findall(r"^## (\d+) · \S+ \S+ · (\w+)$", text, flags=re.M)
    assert [int(n) for n, _ in heads] == list(range(1, len(heads) + 1))
    kinds = [kind for _, kind in heads]
    assert kinds.count("batch") == 6 and kinds.count("milestone") == 2 and kinds.count("plan") == 2
    for name in ("setup", "place1", "structure1", "finish", "v4"):
        assert (PASSES / f"{name}.py").read_text().rstrip() in text          # the code as written
    for link in re.findall(r"!\[[^]]*\]\(([^)]+)\)", text):
        assert (sch.parent / link).is_file(), link
    assert "_structure/top.png" in text and "_rev-A/top.png" in text
    assert "`kat pcb sync tia.kicad_pcb --force`" in text and "R_0805_2012Metric" in text
