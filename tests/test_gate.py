"""Ready-for-layout gate (kat.ready / kat ready) on synthetic projects."""

import json
import shutil

import pytest

import kat
from kat import cli, env
from kat.schematic import Schematic
from kat.sexpr import Sym, find
from test_sync import BASE_NETS, BASE_PARTS, project, write_netlist  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")

GOOD_FP = {"J1": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
           "R1": "Resistor_SMD:R_0603_1608Metric",
           "D1": "LED_SMD:LED_0603_1608Metric"}
MPN = {"J1": {"MPN": "61300211121"}, "R1": {"MPN": "RC0603FR-07330RL"},
       "D1": {"MPN": "APT1608SEC"}}


def prepare(project, tmp_path, footprints, fields=MPN, parts=BASE_PARTS):
    net = write_netlist(tmp_path / "design.net", parts, BASE_NETS, footprints, fields)
    report = kat.sync(project, net)
    assert report["check"]["ok"], report["check"]
    return net


def problems(result, kind="errors"):
    return {(e["ref"], e["problem"]) for e in result[kind]}


def test_complete_design_is_ready_with_project_footprint_library(project, tmp_path):
    """Footprints from the stock libraries and from a project fp-lib-table (${KIPRJMOD})."""
    d = project.parent
    (d / "my.pretty").mkdir()
    shutil.copy(env.footprint_libraries()["LED_SMD"] / "LED_0603_1608Metric.kicad_mod",
                d / "my.pretty" / "MyLED.kicad_mod")
    (d / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "my") (type "KiCad") '
        '(uri "${KIPRJMOD}/my.pretty") (options "") (descr "")))\n')
    net = prepare(project, tmp_path, {**GOOD_FP, "D1": "my:MyLED"})
    r = kat.ready(project, expect=net)
    assert r["ready"], r["errors"]
    assert r["parts"] == 3 and r["check"]["netlist_match"] is True
    assert not problems(r, "warnings") - {("D1", "footprint_filter")}  # "LED*" does not match MyLED


def test_each_blocking_problem_is_reported(project, tmp_path):
    fps = {"J1": "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical",  # pin 2 has no pad
           "R1": "",                                                          # none chosen
           "D1": "NoSuchLib:LED_0603_1608Metric"}
    net = prepare(project, tmp_path, fps, fields={"J1": MPN["J1"], "D1": MPN["D1"]})
    r = kat.ready(project, expect=net)
    assert not r["ready"]
    assert problems(r) == {("J1", "pin_without_pad"), ("R1", "no_footprint"),
                           ("R1", "missing_field"), ("D1", "footprint_library_unknown")}
    pin = next(e for e in r["errors"] if e["problem"] == "pin_without_pad")
    assert pin["detail"].endswith("pins 2")
    assert r["by_problem"]["no_footprint"] == 1


def test_warnings_do_not_block(project, tmp_path):
    fps = {**GOOD_FP, "J1": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
           "R1": "Capacitor_SMD:C_0603_1608Metric"}    # pads fit, but not an R footprint
    prepare(project, tmp_path, fps)
    r = kat.ready(project)                             # no expect -> warning only
    assert r["ready"], r["errors"]
    assert problems(r, "warnings") >= {(None, "no_expect"), ("J1", "pad_without_pin"),
                                       ("R1", "footprint_filter")}


def test_board_and_bom_flags(project, tmp_path):
    """Not on board: not a board part at all. Excluded from BOM: no MPN needed."""
    net = prepare(project, tmp_path, GOOD_FP, fields={"J1": MPN["J1"]})
    s = Schematic(project)
    for sym in s.symbols():
        flag = {"R1": "in_bom", "D1": "on_board"}.get(sym.ref)
        if flag:
            find(sym.node, flag)[1] = Sym("no")
    s.save()
    r = kat.ready(project)
    assert r["parts"] == 2                            # D1 is not on the board
    assert r["ready"], r["errors"]                    # R1 needs no MPN
    # the design netlist still has D1: a part left off the board is a netlist difference
    assert (None, "schematic_not_ok") in problems(kat.ready(project, expect=net))


def test_unclean_schematic_blocks(project, tmp_path):
    net = prepare(project, tmp_path, GOOD_FP)
    other = write_netlist(tmp_path / "other.net", BASE_PARTS,
                          {**BASE_NETS, "VIN": [("J1", "1"), ("D1", "2")]}, GOOD_FP, MPN)
    assert kat.ready(project, expect=net)["ready"]
    r = kat.ready(project, expect=other)
    assert not r["ready"] and (None, "schematic_not_ok") in problems(r)


def test_cli_exit_code(project, tmp_path, capsys):
    net = prepare(project, tmp_path, GOOD_FP)
    assert cli.main(["ready", str(project), "--expect", str(net)]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] is True
    assert cli.main(["ready", str(project), "--expect", str(net), "--require-field", "Manufacturer"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["by_problem"] == {"missing_field": 3}
    assert cli.main(["ready", str(project), "--expect", str(net), "--no-require"]) == 0


def test_reference_needs_a_number(project, tmp_path):
    """KiCad treats "ROUT" as unannotated (its BOM prints "ROUT?")."""
    parts = {**BASE_PARTS, "ROUT": BASE_PARTS["R1"]}
    del parts["R1"]
    nets = {k: [("ROUT" if r == "R1" else r, p) for r, p in v] for k, v in BASE_NETS.items()}
    fps = {**GOOD_FP, "ROUT": GOOD_FP["R1"]}
    net = write_netlist(tmp_path / "d.net", parts, nets, fps, {**MPN, "ROUT": MPN["R1"]})
    assert kat.sync(project, net)["check"]["ok"]
    r = kat.ready(project, expect=net)
    assert ("ROUT", "reference_without_number") in problems(r)
