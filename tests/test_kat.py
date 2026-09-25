import json
import shutil
from pathlib import Path

import pytest

from kat import cli, library, netlist
from kat.schematic import Schematic
from kat.sexpr import dumps, loads

needs_kicad = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")


def run(capsys, *argv):
    code = cli.main([str(a) for a in argv])
    out = json.loads(capsys.readouterr().out)
    assert "error" not in out, out
    return code, out


# --- sexpr ----------------------------------------------------------------------

def test_sexpr_roundtrip_keeps_unknown_tokens_and_escapes():
    text = '(kicad_sch (future_token yes (nested 1.2700)) (property "D" "say \\"hi\\"\\nok") (hide yes))'
    tree = loads(text)
    assert tree[2][2] == 'say "hi"\nok'
    assert loads(dumps(tree)) == tree
    assert "1.2700" in dumps(tree)  # numbers keep their exact text


def test_sexpr_rejects_unbalanced():
    with pytest.raises(ValueError):
        loads('(a (b "c")')


# --- library --------------------------------------------------------------------

@needs_kicad
def test_derived_symbol_is_flattened():
    sym = library.resolve("Amplifier_Operational:ADA4625-2ARDZ")
    names = [str(s[1]) for s in sym if isinstance(s, list) and str(s[0]) == "symbol"]
    assert names and all(n.startswith("ADA4625-2ARDZ_") for n in names)
    assert library.unit_count(sym) == 3
    assert {p.number for p in library.pins(sym)} == {str(i) for i in range(1, 10)}


@needs_kicad
def test_search_ranks_name_matches():
    hits = library.search("lm358")
    assert hits[0]["lib_id"].startswith("Amplifier_Operational:LM358")


# --- geometry: pin positions agree with KiCad's own connectivity ------------------

@needs_kicad
def test_pin_positions_all_orientations(tmp_path):
    run_new = cli.main(["new", str(tmp_path / "o")])
    assert run_new == 0
    s = Schematic(tmp_path / "o" / "o.kicad_sch")
    expect, i = {}, 0
    for lib in ("Amplifier_Operational:ADA4625-2ARDZ", "Transistor_BJT:Q_NPN_BCE"):
        for rot in (0, 90, 180, 270):
            for mir in ("", "x", "y"):
                sym = s.add_symbol(lib, 30 + (i % 8) * 30, 30 + (i // 8) * 30, rot, mir)
                for p in s.pins(sym):
                    name = f"{sym.ref}_{p['number']}"
                    s.add_label(name, p["x"], p["y"])
                    expect[name] = (sym.ref, p["number"])
                i += 1
    s.save()
    nets = netlist.from_schematic(s.path)
    wrong = [k for k, v in expect.items() if v not in nets.get("/" + k, ())]
    assert not wrong


# --- end to end through the CLI -----------------------------------------------------

LED = """
add Device:R 100 80 --ref R1 --value 330
add Device:LED 100 95 --rot 90 --ref D1
connect R1.2 D1.2
label VIN R1.1 --stub 0,-2.54
power GND D1.1 --stub 0,2.54
power PWR_FLAG 100,98.81 --stub -5.08,0
"""

EXPECTED = """(export (version "E") (nets
  (net (code "1") (name "VIN") (node (ref "R1") (pin "1")))
  (net (code "2") (name "N$1") (node (ref "R1") (pin "2")) (node (ref "D1") (pin "2")))
  (net (code "3") (name "GND") (node (ref "D1") (pin "1")))))"""


@needs_kicad
def test_batch_check_and_move(tmp_path, capsys):
    sch = tmp_path / "led" / "led.kicad_sch"
    run(capsys, "new", sch)
    script = tmp_path / "led.kat"
    script.write_text(LED)
    _, out = run(capsys, "batch", sch, script)
    assert out["applied"] == 6
    (tmp_path / "expect.net").write_text(EXPECTED)

    _, res = run(capsys, "check", sch, "--expect", tmp_path / "expect.net")
    assert res["netlist_vs_expected"]["match"], res
    before = {k: v for k, v in netlist.from_schematic(sch).items() if len(v) > 1}

    # moving D1 carries its GND stub and power symbol along; connectivity is unchanged
    run(capsys, "move", sch, "D1", "--by", "12.7,0")
    after = {k: v for k, v in netlist.from_schematic(sch).items() if len(v) > 1}
    assert sorted(before.values(), key=sorted) == sorted(after.values(), key=sorted)
    s = Schematic(sch)
    gnd = next(x for x in s.symbols() if x.lib_id == "power:GND")
    assert abs(gnd.place.x - 112.7) < 0.5


@needs_kicad
def test_batch_error_saves_nothing(tmp_path, capsys):
    sch = tmp_path / "e" / "e.kicad_sch"
    run(capsys, "new", sch)
    before = sch.read_text()
    script = tmp_path / "bad.kat"
    script.write_text("add Device:R 10 10\nadd Device:NoSuchPart 20 20\n")
    assert cli.main(["batch", str(sch), str(script)]) == 2
    assert "line 2" in json.loads(capsys.readouterr().out)["error"]
    assert sch.read_text() == before


# --- netlist comparison ---------------------------------------------------------------

def test_compare_ignores_generated_names():
    a = {"Net-(D1-A)": frozenset({("R1", "2"), ("D1", "2")}), "GND": frozenset({("D1", "1"), ("C1", "2")})}
    e = {"N$1": frozenset({("R1", "2"), ("D1", "2")}), "0V": frozenset({("D1", "1"), ("C1", "2")})}
    r = netlist.compare(a, e)
    assert r["match"]
    assert r["name_differences"] == [{"pins": [("C1", "2"), ("D1", "1")], "expected": "0V", "actual": "GND"}]
