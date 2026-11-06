"""Checking a design against an independently specified pin table."""

import json

import pytest

import kat
from kat import cli
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_pcb_sync import board_of
from test_sync import project  # noqa: F401 (fixture)

needs_kicad = pytest.mark.requires("kicad", "symbols", "footprints")


def netlist(path, nets):
    """A KiCad s-expression netlist with only nets: {name: [(ref, pin), ...]}."""
    body = "".join(f'(net (code "{k}") (name "{name}") '
                   + "".join(f'(node (ref "{r}") (pin "{p}"))' for r, p in pins) + ")"
                   for k, (name, pins) in enumerate(nets.items(), 1))
    path.write_text(f'(export (version "E") (nets {body}))', encoding="utf-8")
    return path


@pytest.fixture
def net(tmp_path):
    return netlist(tmp_path / "d.net", {
        "/CH_A/SIG": [("J1", "1"), ("U1", "4")], "/CH_B/SIG": [("J1", "2"), ("U1", "5")],
        "GND": [("J1", "3"), ("U1", "8"), ("U2", "8")],
        "unconnected-(J1-Pad4)": [("J1", "4")], "Net-(U1-Pad1)": [("U1", "1"), ("R1", "1")]})


def table(tmp_path, text, name="map.csv"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def test_pin_net_table_agrees(net, tmp_path):
    r = kat.mapping.check(net, table(tmp_path, "pin,net,comment\n# from the datasheet\nJ1.1,/CH_A/SIG,x\n"
                                               "J1.2,SIG,\nJ1.3,GND,\nJ1.4,,free\n"))
    assert r["ok"], r
    assert r["rows"] == 4 and r["ignored"] == ["comment"]


def test_ref_pin_columns_semicolons_and_json_give_the_same(net, tmp_path):
    a = kat.mapping.check(net, table(tmp_path, "ref;pin;net\nJ1;1;SIG\nJ1;2;SIG\nJ1;3;GND\n"))
    b = kat.mapping.check(net, table(tmp_path, json.dumps({"J1.1": "SIG", "J1.2": "SIG", "J1.3": "GND"}), "m.json"))
    c = kat.mapping.check(net, table(tmp_path, json.dumps({"rows": [
        {"ref": "J1", "pin": "1", "net": "SIG"}, {"ref": "J1", "pin": "2", "net": "SIG"},
        {"ref": "J1", "pin": "3", "net": "GND"}]}), "n.json"))
    assert a["ok"] and b["ok"] and c["ok"], (a, b, c)


def test_every_kind_of_difference_is_reported(net, tmp_path):
    r = kat.mapping.check(net, table(tmp_path, "pin,net\nJ1.1,/CH_B/SIG\nJ1.3,\nJ1.4,GND\nJ1.9,GND\nJ9.1,GND\n"))
    assert not r["ok"]
    assert {(m["pin"], m["expected"], m["actual"]) for m in r["mismatched"]} == {
        ("J1.1", "/CH_B/SIG", "/CH_A/SIG"), ("J1.3", "", "GND"), ("J1.4", "GND", "")}
    assert {(m["pin"], m["problem"], m["row"]) for m in r["missing"]} == {("J1.9", "pin", 4), ("J9.1", "part", 5)}
    assert r["extra"] == [{"pin": "J1.2", "net": "/CH_B/SIG"}]         # not J1.4: it is free
    assert r["counts"] == {"missing": 2, "mismatched": 3, "split": 0, "merged": 0, "extra": 1}


def test_a_full_name_does_not_match_another_sheet(net, tmp_path):
    r = kat.mapping.check(net, table(tmp_path, "pin,net\nJ1.1,/CH_A/SIG\nJ1.2,/CH_A/SIG\n"), complete=False)
    assert [m["pin"] for m in r["mismatched"]] == ["J1.2"]


def test_an_unlisted_pin_on_no_net_is_not_extra_and_an_absent_one_can_be_free(tmp_path):
    n = netlist(tmp_path / "s.net", {"A": [("J1", "1"), ("U1", "1")]})       # SKiDL lists only connected pins
    assert kat.mapping.check(n, table(tmp_path, "pin,net\nJ1.1,A\nJ1.2,\n"))["ok"]
    r = kat.mapping.check(n, table(tmp_path, "pin,net\nJ1.1,A\nJ1.2,B\n"))
    assert r["missing"] == [{"row": 2, "pin": "J1.2", "problem": "pin"}]


def test_source_channel_destination(net, tmp_path):
    ok = "source,channel,destination\nJ1.1,0,U1.4\nJ1.2,1,U1.5\n"
    r = kat.mapping.check(net, table(tmp_path, ok), complete=["J1"])
    assert r["extra"] == [{"pin": "J1.3", "net": "GND"}], r
    assert not r["split"] and not r["merged"] and not r["mismatched"]
    # swapped destinations: both channels are split
    r = kat.mapping.check(net, table(tmp_path, "source,channel,destination\nJ1.1,0,U1.5\nJ1.2,1,U1.4\n"), complete=False)
    assert [s["what"] for s in r["split"]] == ["channel 0", "channel 1"], r
    assert r["split"][0]["nets"] == {"/CH_A/SIG": ["J1.1"], "/CH_B/SIG": ["U1.5"]}
    # two channels on one net are merged
    r = kat.mapping.check(net, table(tmp_path, "source,channel,destination\nJ1.3,0,U1.8\nU2.8,1,\n"), complete=False)
    assert r["merged"] == [{"net": "GND", "what": ["channel 0", "channel 1"]}] and not r["split"]


def test_rows_without_a_channel_are_separate_unless_a_net_joins_them(net, tmp_path):
    r = kat.mapping.check(net, table(tmp_path, "from,to\nJ1.3,U1.8\nU2.8,\nJ1.1,U1.4\n"), complete=False)
    assert r["merged"] == [] and not r["split"], "a lone pin in a pin-group table asserts nothing"
    r = kat.mapping.check(net, table(tmp_path, "from,to\nJ1.3,U1.8\nU2.8,U1.8x\n"), complete=False)
    assert r["missing"] == [{"row": 2, "pin": "U1.8x", "problem": "pin"}]
    r = kat.mapping.check(net, table(tmp_path, "from,to\nJ1.3,U1.8\nU2.8,R1.1\n"), complete=False)
    assert [s["what"] for s in r["split"]] == ["row 2"]
    r = kat.mapping.check(netlist(tmp_path / "t.net", {"X": [("A", "1"), ("B", "1"), ("A", "2"), ("B", "2")]}),
                          table(tmp_path, "from,to\nA.1,B.1\nA.2,B.2\n"))
    assert r["merged"] == [{"net": "X", "what": ["row 1", "row 2"]}]
    r = kat.mapping.check(netlist(tmp_path / "t.net", {"X": [("A", "1"), ("B", "1"), ("A", "2"), ("B", "2")]}),
                          table(tmp_path, "from,to,net\nA.1,B.1,X\nA.2,B.2,X\n"))
    assert r["ok"], r


def test_an_unconnected_pin_of_a_group_splits_it(net, tmp_path):
    r = kat.mapping.check(net, table(tmp_path, "from,to\nJ1.1,J1.4\n"), complete=False)
    assert r["split"] and r["split"][0]["nets"][""] == ["J1.4"]


def test_complete_decides_what_extra_covers(net, tmp_path):
    t = table(tmp_path, "pin,net\nJ1.1,SIG\n")
    assert {e["pin"] for e in kat.mapping.check(net, t)["extra"]} == {"J1.2", "J1.3"}
    assert kat.mapping.check(net, t, complete=False)["ok"]
    assert {e["pin"] for e in kat.mapping.check(net, t, complete=["U2"])["extra"]} == {"U2.8"}
    assert kat.mapping.check(net, t, complete=["Z9"])["missing"] == [{"row": None, "pin": "Z9", "problem": "part"}]


@pytest.mark.parametrize("text, match", [
    ("net\nGND\n", "no pin column"),
    ("pin,net\nJ1,GND\n", "not a pin"),
    ("ref,net\nJ1,GND\n", "needs a column pin"),
    ("ref,pin,net\nJ1,,GND\n", "both needed"),
    ("pin,net\n,GND\n", "no pin"),
    ("pin,net\nJ1.1,GND\nJ1.1,VCC\n", "row 1 and row 2"),
    ("from,to\nJ1.1,U1.1\nJ1.1,U1.2\n", "row 1 and row 2"),
])
def test_bad_tables_are_errors(net, tmp_path, text, match):
    with pytest.raises(kat.KatError, match=match):
        kat.mapping.check(net, table(tmp_path, text))


def test_a_pin_may_repeat_with_the_same_assignment(net, tmp_path):
    assert kat.mapping.check(net, table(tmp_path, "pin,net\nJ1.1,SIG\nJ1.1,SIG\n"), complete=False)["ok"]
    assert kat.mapping.check(net, table(tmp_path, "from,channel,to\nJ1.3,G,U1.8\nJ1.3,G,U2.8\n"), complete=False)["ok"]


def test_named_pin_columns_and_write_read_round_trip(net, tmp_path):
    t = table(tmp_path, "a,b\nJ1.1,U1.4\n")
    r = kat.mapping.check(net, t, complete=False, pins=["a", "b"])
    assert r["ok"] and r["rows"] == 1
    f = tmp_path / "out" / "pins.csv"
    assert kat.mapping.write(f, {"J1.10": "B", "J1.2": "A", "J1.3": ""})["pins"] == 3
    assert f.read_text().splitlines() == ["ref,pin,net", "J1,2,A", "J1,3,", "J1,10,B"]
    assert [(r["pins"], r["net"]) for r in kat.mapping.read(f)["rows"]] == [
        ([("J1", "2")], "A"), ([("J1", "3")], ""), ([("J1", "10")], "B")]


def test_an_unknown_source_is_an_error(net, tmp_path):
    with pytest.raises(kat.KatError, match="give a .kicad_sch"):
        kat.mapping.check(tmp_path / "x.txt", table(tmp_path, "pin,net\nJ1.1,A\n"))


def test_cli_exit_codes(net, tmp_path, capsys):
    t = table(tmp_path, "pin,net\nJ1.1,SIG\n")
    assert cli.main(["map", str(net), str(t), "--partial"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert cli.main(["map", str(net), str(t)]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["extra"] == 2
    assert cli.main(["map", str(net), str(t), "--partial", "--complete", "J1"]) == 2
    assert "exclude" in json.loads(capsys.readouterr().out)["error"]


@needs_kicad
def test_schematic_and_board_agree_and_checks_report_it(pcb, tmp_path):
    good = table(tmp_path, "ref,pin,net\nJ1,1,VIN\nJ1,2,GND\nJ1,3,CTRL\n")
    bad = table(tmp_path, "ref,pin,net\nJ1,1,VIN\nJ1,2,CTRL\nJ1,3,GND\n", "bad.csv")
    sch = pcb.with_suffix(".kicad_sch")
    for source in (sch, pcb):
        assert kat.mapping.check(source, good)["ok"], source
        r = kat.mapping.check(source, bad)
        assert {m["pin"] for m in r["mismatched"]} == {"J1.2", "J1.3"}, source
    assert kat.check(sch, mapping=good)["mapping"]["ok"]
    c = kat.check(sch, mapping=bad)
    assert not c["ok"] and not c["clean"] and c["mapping"]["counts"]["mismatched"] == 2
    assert "mapping" not in kat.check(sch)
    c = kat.pcb.check(pcb, mapping=bad)
    assert not c["ok"] and c["mapping"]["counts"]["mismatched"] == 2
    assert kat.pcb.check(pcb, mapping=good)["mapping"]["ok"]
    assert "mapping" not in kat.pcb.check(pcb)


@needs_kicad
def test_a_chain_table_on_a_real_board_and_the_board_cli(pcb, tmp_path, capsys):
    good = table(tmp_path, "source,channel,destination,meaning\nJ1.1,V,R1.1,supply\nJ1.3,C,Q1.1,drive\n")
    r = kat.mapping.check(pcb, good, complete=["J1"])
    assert r["extra"] == [{"pin": "J1.2", "net": next(e["net"] for e in r["extra"])}] and not r["split"], r
    assert r["ignored"] == ["meaning"]
    assert kat.mapping.check(board_of(pcb.with_suffix(".kicad_sch")), good, complete=False)["ok"]
    out = tmp_path / "check.json"
    assert cli.main(["pcb", "check", str(pcb), "--map", str(good), "--partial", "--out", str(out)]) in (0, 1)
    printed = json.loads(capsys.readouterr().out)
    assert printed["mapping"]["ok"] and printed["out"] == str(out)
    written = json.loads(out.read_bytes().decode("utf-8"))           # UTF-8, no BOM
    assert written["mapping"] == printed["mapping"] and "out" not in written
