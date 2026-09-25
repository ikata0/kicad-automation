import json
import shutil
from pathlib import Path

import pytest

import kat
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

def draw_led(s):
    s.add("Device:R", 100, 80, ref="R1", value="330")
    s.add("Device:LED", 100, 95, rot=90, ref="D1")
    s.connect("R1.2", "D1.2")
    s.label("VIN", "R1.1", stub=(0, -2.54))
    s.power("GND", "D1.1", stub=(0, 2.54))
    s.power("PWR_FLAG", (100, 98.81), stub=(-5.08, 0))

EXPECTED = """(export (version "E") (nets
  (net (code "1") (name "VIN") (node (ref "R1") (pin "1")))
  (net (code "2") (name "N$1") (node (ref "R1") (pin "2")) (node (ref "D1") (pin "2")))
  (net (code "3") (name "GND") (node (ref "D1") (pin "1")))))"""


@needs_kicad
def test_edit_check_and_move(tmp_path, capsys):
    sch = tmp_path / "led" / "led.kicad_sch"
    run(capsys, "new", sch)
    with kat.edit(sch) as s:
        draw_led(s)
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
def test_edit_error_and_dry_run_save_nothing(tmp_path, capsys):
    sch = tmp_path / "e" / "e.kicad_sch"
    run(capsys, "new", sch)
    before = sch.read_text()
    with pytest.raises(kat.KatError):
        with kat.edit(sch) as s:
            s.add("Device:R", 10, 10)
            s.add("Device:NoSuchPart", 20, 20)
    assert sch.read_text() == before
    with kat.edit(sch, dry_run=True) as s:
        s.add("Device:R", 10, 10)
    assert sch.read_text() == before


@needs_kicad
def test_cli_and_library_give_the_same_file(tmp_path, capsys):
    """The CLI is a thin layer: the same edits through both paths give the same drawing.
    UUIDs are new for every item, and the CLI saves (and KiCad reorders) after every step,
    so the top-level items are compared as sorted lists."""
    import re
    a, b = tmp_path / "a" / "x.kicad_sch", tmp_path / "b" / "x.kicad_sch"
    run(capsys, "new", a)
    run(capsys, "new", b)
    with kat.edit(a) as s:
        draw_led(s)
        s.move("D1", by=(12.7, 0))
        s.set("R1", "Value", "470", at=(104.14, 80))
        s.detach("R1.1")
        s.nc("R1.1")
    for argv in (["add", "Device:R", "100", "80", "--ref", "R1", "--value", "330"],
                 ["add", "Device:LED", "100", "95", "--rot", "90", "--ref", "D1"],
                 ["connect", "R1.2", "D1.2"],
                 ["label", "VIN", "R1.1", "--stub", "0,-2.54"],
                 ["power", "GND", "D1.1", "--stub", "0,2.54"],
                 ["power", "PWR_FLAG", "100,98.81", "--stub", "-5.08,0"],
                 ["move", "D1", "--by", "12.7,0"],
                 ["set", "R1", "Value", "470", "--at", "104.14,80"],
                 ["detach", "R1.1"],
                 ["nc", "R1.1"]):
        run(capsys, argv[0], b, *argv[1:])
    def items(p):
        tree = Schematic(p).tree
        return sorted(re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27}', 'U', dumps(c)) for c in tree[1:])
    assert items(a) == items(b)


# --- netlist comparison ---------------------------------------------------------------

def test_compare_ignores_generated_names():
    a = {"Net-(D1-A)": frozenset({("R1", "2"), ("D1", "2")}), "GND": frozenset({("D1", "1"), ("C1", "2")})}
    e = {"N$1": frozenset({("R1", "2"), ("D1", "2")}), "0V": frozenset({("D1", "1"), ("C1", "2")})}
    r = netlist.compare(a, e)
    assert r["match"]
    assert r["name_differences"] == [{"pins": [("C1", "2"), ("D1", "1")], "expected": "0V", "actual": "GND"}]


@needs_kicad
def test_detach_removes_stacked_duplicates(tmp_path):
    """B3: a pin with the same stub + label drawn three times is detached completely."""
    sch = tmp_path / "d" / "d.kicad_sch"
    kat.new(sch)
    with kat.edit(sch) as s:
        s.add("Device:R", 100, 80, ref="R1")
        for _ in range(3):
            s.label("SIG", "R1.1", stub=(0, -2.54))
    assert kat.check(sch)["duplicates"] == ["3x label SIG 100.33,73.66", "3x wire 100.33,73.66 100.33,76.2"]
    with kat.edit(sch) as s:
        out = s.detach("R1.1")
        assert out["wires"] == 3 and out["labels"] == 3, out
        assert not s.list()["labels"] and not s.list()["wires"]


@needs_kicad
def test_results_are_rounded_and_flag_removal_warns(tmp_path):
    """B7: no float noise such as 506.72999999999996 in results. B6: detaching a pin that
    carries a PWR_FLAG says so."""
    sch = tmp_path / "f" / "f.kicad_sch"
    kat.new(sch)
    with kat.edit(sch) as s:
        s.add("Device:R", 501.65, 80, ref="R1")
        out = s.power("GND", "R1.1", stub=(5.08, 0))
        assert out["x"] == round(out["x"], 4) and len(repr(out["x"])) < 10, out
        s.power("PWR_FLAG", "R1.2", stub=(0, 2.54))
        res = s.detach("R1.2")
    assert res["power_symbols"] == ["#FLG1"] and "PWR_FLAG" in res["warning"]


@needs_kicad
def test_view_tiles_and_finding_crops(tmp_path):
    sch = tmp_path / "v" / "v.kicad_sch"
    kat.new(sch)
    with kat.edit(sch) as s:
        draw_led(s)
        s.add("Device:R", 150, 80, ref="R2")
        s.label("CLASH", (150, 80))  # text over the body of R2: an overlap finding
    v = kat.view(sch, around="R1")
    x, y, w, h = v["region"]
    assert x < 100 < x + w and y < 80 < y + h
    assert abs(v["width"] - w * v["px_per_mm"]) <= 2
    assert Path(v["png"]).parent.name == "views"
    v2 = kat.view(sch, region=(90, 70, 40, 20), zoom=4)
    assert v2["region"] == [90, 70, 40, 20] and v2["width"] == round(40 * v2["px_per_mm"])
    with pytest.raises(kat.KatError):
        kat.view(sch)
    snap = kat.snapshot(sch, tiles=True, finding_crops=5)
    assert snap["tiles"] and all(Path(t["png"]).exists() for t in snap["tiles"])
    assert snap["finding_crops"] and Path(snap["finding_crops"][0]["png"]).exists()
    assert not kat.snapshot(sch).get("tiles")  # small sheet: no tiles by default
