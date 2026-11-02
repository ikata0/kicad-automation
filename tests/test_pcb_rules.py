"""Effective clearance against KiCad's DRC: for each set of rules, two tracks just inside the
resolved value are a violation and just outside it are not."""

import json

import pytest

import kat
from kat import cli
from kat.pcb import Board
from kat.pcb import rules as dru

LOW = """(rule lo (condition "A.NetClass == 'HV' && B.NetClass == 'LV'") (constraint clearance (min 0.1mm)))"""
CASES = {
    "default class": (dict(), 0.2, "class"),
    "the larger class": (dict(classes=[("HV", 0.5), ("LV", 0.2)]), 0.5, "class"),
    "board minimum over class": (dict(minimum=0.4), 0.4, "board minimum"),
    "class over board minimum": (dict(classes=[("HV", 0.5)], minimum=0.4), 0.5, "class"),
    "rule lowers the class": (dict(classes=[("HV", 0.5), ("LV", 0.2)], rules=LOW), 0.1, "rule lo"),
    "rule the other way round": (dict(classes=[("HV", 0.5), ("LV", 0.2)], rules=LOW.replace(
        "A.NetClass == 'HV' && B.NetClass == 'LV'", "A.NetClass == 'LV' && B.NetClass == 'HV'")), 0.1, "rule lo"),
    "rule below board minimum": (dict(minimum=0.4, rules="""(rule lo (condition "A.NetName == 'HV1'")
        (constraint clearance (min 0.1mm)))"""), 0.1, "rule lo"),
    "the last rule, lower": (dict(rules="""(rule hi (condition "A.NetName == 'HV1'") (constraint clearance (min 1mm)))
        (rule lo (condition "A.NetName == 'HV1'") (constraint clearance (min 0.3mm)))"""), 0.3, "rule lo"),
    "the last rule, higher": (dict(rules="""(rule lo (condition "A.NetName == 'HV1'") (constraint clearance (min 0.3mm)))
        (rule hi (condition "A.NetName == 'HV1'") (constraint clearance (min 1mm)))"""), 1.0, "rule hi"),
    "no condition, mil": (dict(rules="(rule hi (constraint clearance (min 40mil)))"), 1.016, "rule hi"),
    "a wildcard": (dict(rules="""(rule w (condition "A.NetName == 'HV*'") (constraint clearance (min 0.7mm)))"""), 0.7, "rule w"),
    "not, or": (dict(rules="""(rule w (condition "!(A.NetName == 'X') || B.NetName == 'Y'") (constraint clearance (min 0.7mm)))"""),
                0.7, "rule w"),
    "no match": (dict(rules="""(rule w (condition "A.NetName == 'X*'") (constraint clearance (min 0.7mm)))"""), 0.2, "class"),
    "this layer": (dict(rules="""(rule w (layer "F.Cu") (constraint clearance (min 0.7mm)))"""), 0.7, "rule w"),
    "another layer": (dict(rules="""(rule w (layer "B.Cu") (constraint clearance (min 0.7mm)))"""), 0.2, "class"),
    "outer layers": (dict(rules="""(rule w (layer outer) (constraint clearance (min 0.7mm)))"""), 0.7, "rule w"),
    "hasNetclass": (dict(classes=[("HV", 0.3)], rules="""(rule w (condition "A.hasNetclass('HV')")
        (constraint clearance (min 0.6mm)))"""), 0.6, "rule w"),
}


def tracks(tmp_path, name, gap, classes=(), minimum=None, rules=None, class_pair=None):
    pcb = tmp_path / f"{name}.kicad_pcb"
    for s in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        pcb.with_suffix(s).unlink(missing_ok=True)
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 60, 40))
    for c, cl in classes:
        b.netclass(c, 0.2, cl, nets=[f"{c}*"])
    if minimum is not None:
        b.constraints(min_clearance=minimum)
    if rules:
        b.rules(rules)
    if class_pair:
        b.class_clearance(*class_pair)
    b.add_track((5, 5), (30, 5), 0.2, "F.Cu", "HV1")
    b.add_track((5, 5.2 + gap), (30, 5.2 + gap), 0.2, "F.Cu", "LV1")
    b.save()
    return pcb


def drc_flags(pcb) -> bool:
    return any(v["type"] == "clearance" for v in kat.pcb.drc(pcb, parity=False)["violations"])


@pytest.mark.requires("kicad", "footprints")
@pytest.mark.parametrize("case", list(CASES))
def test_resolved_clearance_is_what_drc_applies(tmp_path, monkeypatch, case):
    monkeypatch.setenv("KAT_REPORT", "0")
    kw, want, source = CASES[case]
    pcb = tracks(tmp_path, "c", want + 0.02, **kw)
    got = Board(pcb).clearance("HV1", "LV1", "F.Cu")
    assert got["clearance"] == pytest.approx(want) and got["source"] == source, got
    assert not drc_flags(pcb), f"{case}: DRC objects just outside {want}"
    assert drc_flags(tracks(tmp_path, "c", want - 0.02, **kw)), f"{case}: DRC accepts just inside {want}"


@pytest.mark.requires("kicad", "footprints")
def test_generated_class_rule_and_the_local_checks_agree_with_drc(tmp_path, monkeypatch, capsys):
    """class_clearance writes a rule; local checks used only the larger class value and passed
    what DRC then refused."""
    monkeypatch.setenv("KAT_REPORT", "0")
    kw = dict(classes=[("HV", 0.2), ("LV", 0.2)], class_pair=("HV", "LV", 0.9))
    pcb = tracks(tmp_path, "p", 0.92, **kw)
    assert not drc_flags(pcb)
    assert drc_flags(tracks(tmp_path, "p", 0.88, **kw))
    b = Board(pcb)
    res = b.clearance("HV1", "LV1")
    assert res["clearance"] == pytest.approx(0.9) and res["source"] == "rule kat class HV LV"
    assert res["classes"] == ["HV", "LV"] and res["class_clearance"] == pytest.approx(0.2)
    with pytest.raises(kat.KatError, match="too close"):
        b.track([(5, 5.6), (30, 5.6)], net="LV1", width=0.2, layer="F.Cu")
    assert cli.main(["pcb", "clearance", str(pcb), "HV1", "LV1", "--layer", "F.Cu"]) == 0
    assert json.loads(capsys.readouterr().out)["clearance"] == pytest.approx(0.9)


@pytest.mark.requires("kicad", "footprints")
def test_pending_changes_count_and_pads_name_their_net(tmp_path, monkeypatch):
    monkeypatch.setenv("KAT_REPORT", "0")
    pcb = tmp_path / "r.kicad_pcb"
    b = Board.create(pcb)
    b.netclass("HV", 0.2, 0.5, nets=["HV*"])
    assert b.clearance("HV1", "X")["clearance"] == 0.5          # not saved yet
    b.rules("""(rule r (condition "A.NetName == 'HV1'") (constraint clearance (min 0.7mm)))""")
    assert b.clearance("HV1", "X")["clearance"] == 0.7
    assert "below the board minimum" not in str(b.rules("(rule r (constraint clearance (min 0.7mm)))"))
    b.constraints(min_clearance=0.9)
    warn = b.rules("(rule r (constraint clearance (min 0.7mm)))")["warnings"][0]
    assert "below the board minimum 0.9" in warn and "DRC follows the rule" in warn
    f = b.add_footprint("Resistor_SMD:R_0603_1608Metric", "R1", 10, 10)
    assert b.clearance("HV1", "HV1")["same_net"] and not b.clearance("HV1", "X")["same_net"]
    assert b.clearance("R1.1", "X")["nets"] == ["", "X"]
    assert {"hole_clearance", "hole_to_hole"} <= set(b.clearance("A", "B"))


def rule(condition="", layer=None, value=1.0):
    return dru.Rule("r", layer, condition, value)


@pytest.mark.parametrize("condition,expected", [
    ("A.NetName == 'a'", True), ("B.NetName == 'a'", True), ("A.NetName == 'z'", False),
    ("A.NetClass == 'CA' && B.NetClass == 'CB'", True), ("A.NetClass == 'CB' && B.NetClass == 'CA'", True),
    ("A.NetClass == 'CA' && B.NetClass == 'CA'", False), ("A.NetName != 'z'", True),
    ("!(A.NetName == 'a' || A.NetName == 'b')", False), ("A.NetName == 'a*'", True),
    ("A.Type == 'pad'", None), ("A.Type == 'pad' && A.NetName == 'z'", False),
    ("A.Type == 'pad' || A.NetName == 'a'", True), ("A.intersectsCourtyard('U1')", None),
    ("A.NetName == 'a' &&", None), ("what is this", None), ("", True),
])
def test_conditions_in_three_values(condition, expected):
    assert dru.matches(rule(condition), "a", "b", "CA", "CB", "F.Cu") is expected


def test_rules_that_depend_on_the_items_are_reported_not_guessed():
    rules = dru.parse("""(rule known (condition "A.NetName == 'a'") (constraint clearance (min 0.3mm)))
        (rule items (condition "A.Type == 'pad'") (constraint clearance (min 2mm)))
        (rule width (constraint track_width (min 1mm)))""")
    assert [r.name for r in rules] == ["known", "items"]
    res = dru.resolve(rules, "a", "b", "D", "D", 0.2, 0.0, "F.Cu")
    assert res == {"clearance": 0.3, "source": "rule known", "unresolved": ["items"], "worst_case": 2.0}
    later = dru.parse("""(rule items (condition "A.Type == 'pad'") (constraint clearance (min 2mm)))
        (rule known (condition "A.NetName == 'a'") (constraint clearance (min 0.3mm)))""")
    assert dru.resolve(later, "a", "b", "D", "D", 0.2, 0.0)["unresolved"] == []      # overruled anyway
    limited = dru.parse("""(rule w (layer "F.Cu") (constraint clearance (min 2mm)))""")
    assert dru.resolve(limited, "a", "b", "D", "D", 0.2, 0.0)["unresolved"] == ["w"]     # no layer named
