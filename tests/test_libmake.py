"""Symbols and footprints from a pin table, judged by KiCad: its netlist for the symbol, IPC-D-356
and DRC's library check for the footprint."""

import json
from pathlib import Path

import pytest

import kat
from kat import cli, library
from kat.sexpr import find_all, loads
from test_pcb_geometry import ipc356
from test_pcb_sync import board_of
from test_sync import project, write_netlist  # noqa: F401 (fixture)

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints")

TABLE = """pin,name,x,y,type
1,VCC,0,0,power_in
2,SIG,2.54,0,bidirectional
3,GND,5.08,0,power_in
4,CLK,0,2.54,input
5,OUT,2.54,2.54,output
6,NC_6,5.08,2.54,passive
"""


def table(tmp_path, text=TABLE, name="pins.csv"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def part(tmp_path, text=TABLE, **kw):
    out = tmp_path / "lib"
    return kat.libmake.make(table(tmp_path, text), "CONN", out=out, drill=1.0, **kw), out


def footprint_tree(res):
    return loads(open(res["footprint"], encoding="utf-8").read())


def pads_of(res):
    out = {}
    for p in find_all(footprint_tree(res), "pad"):
        at = next(c for c in p if isinstance(c, list) and str(c[0]) == "at")
        out[str(p[1])] = (float(at[1]), float(at[2]))
    return out


def test_library_files_read_back_through_kicad(tmp_path):
    r, out = part(tmp_path)
    assert (r["lib_id"], r["pins"], r["pads"], r["holes"]) == ("CONN:CONN", 6, 6, 0)
    assert (out / "CONN.kicad_sym").exists() and (out / "CONN.pretty" / "CONN.kicad_mod").exists()
    assert r["registered"] == []
    tree = loads((out / "CONN.kicad_sym").read_text())
    sym = next(s for s in find_all(tree, "symbol") if str(s[1]) == "CONN")
    pins = {p.number: p for p in library.pins(sym)}
    assert {k: (v.name, v.etype) for k, v in pins.items()} == {
        "1": ("VCC", "power_in"), "2": ("SIG", "bidirectional"), "3": ("GND", "power_in"),
        "4": ("CLK", "input"), "5": ("OUT", "output"), "6": ("NC_6", "passive")}
    assert all(abs(p.x / 1.27 - round(p.x / 1.27)) < 1e-9 and abs(p.y / 1.27 - round(p.y / 1.27)) < 1e-9
               for p in pins.values()), "pins on the sheet grid"
    assert {p.x for p in pins.values()} == {min(p.x for p in pins.values()), max(p.x for p in pins.values())}
    assert pads_of(r) == {"1": (0, 0), "2": (2.54, 0), "3": (5.08, 0), "4": (0, 2.54), "5": (2.54, 2.54), "6": (5.08, 2.54)}


def flow(project, tmp_path, r_kw=None, fields=None):
    out = project.parent
    r = kat.libmake.make(table(tmp_path), "CONN", lib="gen", out=out, drill=1.0, **(r_kw or {}))
    assert sorted(r["registered"]) == ["fp-lib-table", "sym-lib-table"]
    parts = {"J1": ("gen", "CONN", "CONN", "j1"), "R1": ("Device", "R", "1k", "r1")}
    nets = {"VCC": [("J1", "1"), ("R1", "1")], "SIG": [("J1", "2"), ("R1", "2")], "GND": [("J1", "3"), ("J1", "4")],
            "OUT": [("J1", "5"), ("J1", "6")]}
    net = write_netlist(tmp_path / "d.net", parts, nets, {"J1": "gen:CONN", "R1": "Resistor_SMD:R_0603_1608Metric"},
                        {"J1": {"MPN": "X"}, "R1": {"MPN": "Y"}})
    rep = kat.sync(project, net)
    assert rep["check"]["ok"], rep["check"]
    assert kat.check(project, net)["netlist_vs_expected"]["match"]
    return r


def test_the_whole_flow_with_kicads_netlist_and_ipc(project, tmp_path):
    flow(project, tmp_path)
    gate = kat.ready(project, tmp_path / "d.net")
    assert not [e for e in gate["errors"] if "pad" in e.get("problem", "") or "footprint" in e.get("problem", "")], gate
    pcb = board_of(project)
    r = kat.pcb.sync(pcb, force=True)
    assert r["check"]["parity"] == 0, r
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 40, 30))
        b.move("J1", to=(10, 10))
        b.move("R1", to=(25, 20))
    d = kat.pcb.drc(pcb)
    assert not {"lib_footprint_issues", "lib_footprint_mismatch", "silk_over_copper", "silk_overlap",
                "courtyards_overlap", "pth_inside_courtyard"} & set(d["by_type"]), d["by_type"]
    assert d["parity"] == []
    assert d["errors"] == 0, d["violations"]
    pos = ipc356(pcb)
    for pin, (x, y) in {"1": (10, 10), "2": (12.54, 10), "3": (15.08, 10), "4": (10, 12.54)}.items():
        assert pos[("J1", pin)] == pytest.approx((x, y), abs=0.01), pin
    assert kat.nets(project)["GND"] == [("J1", "3"), ("J1", "4")]


def test_pin_side_view_mirrors_and_stays_true_on_a_board(project, tmp_path):
    flow(project, tmp_path, {"view": "pin"})
    pcb = board_of(project)
    kat.pcb.sync(pcb, force=True)
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 40, 30))
        b.move("J1", to=(20, 10))
        b.move("R1", to=(30, 25))
    pos = ipc356(pcb)
    assert pos[("J1", "1")] == pytest.approx((20, 10), abs=0.01)
    assert pos[("J1", "2")] == pytest.approx((17.46, 10), abs=0.01), "seen from the pins, x runs the other way"
    assert pos[("J1", "6")] == pytest.approx((14.92, 12.54), abs=0.01)
    assert kat.pcb.drc(pcb)["errors"] == 0


def test_y_up_and_origins(tmp_path):
    r, _ = part(tmp_path, y_up=True)
    assert pads_of(r)["4"] == (0, -2.54)
    r, _ = part(tmp_path, origin="center", force=True)
    assert pads_of(r)["1"] == (-2.54, -1.27) and pads_of(r)["5"] == (0, 1.27)
    r, _ = part(tmp_path, origin="pin1", marker="5", force=True)
    assert pads_of(r)["5"] == (0, 0) and pads_of(r)["1"] == (-2.54, -2.54)


def test_grid_rows_slots_holes_and_defaults(tmp_path):
    t = "pin,name,row,col,drill,pad,shape,kind\n1,A,A,1,,,,\n2,B,B,1,,,,\n3,,A,3,1.0x2.0,1.5x2.5,,\n,,C,2,3.2,,,\n"
    r = kat.libmake.make(table(tmp_path, t), "G", out=tmp_path / "o", drill=0.8, pitch=(2, 2.5))
    tree = footprint_tree(r)
    pads = {str(p[1]): p for p in find_all(tree, "pad")}
    assert (r["pins"], r["pads"], r["holes"]) == (3, 3, 1)
    assert pads_of(r)["1"] == (0, 0) and pads_of(r)["2"] == (0, 2.5) and pads_of(r)["3"] == (4, 0)
    assert pads_of(r)[""] == (2, 5)
    field = lambda p, k: next(c for c in p if isinstance(c, list) and str(c[0]) == k)[1:]   # noqa: E731
    assert [str(v) for v in field(pads["3"], "drill")] == ["oval", "1", "2"]
    assert [float(v) for v in field(pads["3"], "size")] == [1.5, 2.5] and str(pads["3"][3]) == "oval"
    assert str(pads[""][2]) == "np_thru_hole" and float(field(pads[""], "drill")[0]) == 3.2
    assert str(pads["1"][3]) == "rect", "the marker pin"
    assert str(pads["2"][3]) == "circle" and [float(v) for v in field(pads["2"], "size")] == [1.3, 1.3]


def test_surface_pads_need_a_size(tmp_path):
    t = "pin,x,y,pad\n1,0,0,1.5x0.6\n2,0,1.27,1.5x0.6\n"
    r = kat.libmake.make(table(tmp_path, t), "S", out=tmp_path / "o")
    text = open(r["footprint"]).read()
    assert '(attr smd' in text and '"F.Paste"' in text and "roundrect" in text
    with pytest.raises(kat.KatError, match="needs a pad size"):
        kat.libmake.make(table(tmp_path, "pin,x,y\n1,0,0\n"), "T", out=tmp_path / "o")


def test_several_pads_of_one_pin_are_one_symbol_pin(tmp_path):
    t = "pin,name,x,y\n1,S,0,0\n1,S,5,0\n2,A,2.5,0\n"
    r = kat.libmake.make(table(tmp_path, t), "M", out=tmp_path / "o", drill=1.0)
    assert (r["pins"], r["pads"]) == (2, 3)
    with pytest.raises(kat.KatError, match="named 'S' and 'T'"):
        kat.libmake.make(table(tmp_path, "pin,name,x,y\n1,S,0,0\n1,T,5,0\n"), "N", out=tmp_path / "o", drill=1.0)


def test_symbol_sides(tmp_path):
    t = "pin,name,side\n1,A,top\n2,B,bottom\n3,C,left\n4,D,right\n5,E,\n"
    r = kat.libmake.make(table(tmp_path, t), "Q", out=tmp_path / "o", footprint=False)
    assert r["footprint"] is None and r["pins"] == 5
    sym = next(s for s in find_all(loads(open(r["symbol"]).read()), "symbol") if str(s[1]) == "Q")
    p = {x.name: x for x in library.pins(sym)}
    assert p["A"].angle == 270 and p["B"].angle == 90 and p["C"].angle == 0 and p["D"].angle == 180
    assert p["E"].angle == 0, "one left over goes left"


def test_a_symbol_only_needs_no_positions_and_a_footprint_only_no_names(tmp_path):
    kat.libmake.make(table(tmp_path, "pin,name\n1,A\n2,B\n"), "A1", out=tmp_path / "o", footprint=False)
    r = kat.libmake.make(table(tmp_path, "pin,x,y\n1,0,0\n2,2,0\n"), "A2", out=tmp_path / "o", symbol=False, drill=1.0)
    assert r["symbol"] is None and r["footprint"]
    with pytest.raises(kat.KatError, match="needs x and y"):
        kat.libmake.make(table(tmp_path, "pin,name\n1,A\n"), "A3", out=tmp_path / "o")


def test_a_second_part_joins_the_library_and_replacing_needs_force(tmp_path):
    out = tmp_path / "o"
    kat.libmake.make(table(tmp_path), "ONE", lib="L", out=out, drill=1.0)
    kat.libmake.make(table(tmp_path), "TWO", lib="L", out=out, drill=1.0)
    names = {str(s[1]) for s in find_all(loads((out / "L.kicad_sym").read_text()), "symbol")}
    assert names == {"ONE", "TWO"} and (out / "L.pretty" / "TWO.kicad_mod").exists()
    with pytest.raises(kat.KatError, match="exists"):
        kat.libmake.make(table(tmp_path), "ONE", lib="L", out=out, drill=1.0)
    with pytest.raises(kat.KatError, match="already has"):
        kat.libmake.make(table(tmp_path), "ONE", lib="L", out=out, drill=1.0, footprint=False)
    kat.libmake.make(table(tmp_path), "ONE", lib="L", out=out, drill=1.0, force=True)


def test_registering_leaves_what_is_in_the_table(project, tmp_path):
    t = project.parent / "sym-lib-table"
    t.write_text('(sym_lib_table (version 7) (lib (name "mine")(type "KiCad")(uri "x.kicad_sym")(options "")(descr "")))\n')
    r = kat.libmake.make(table(tmp_path), "ONE", lib="gen", out=project.parent, drill=1.0)
    assert "sym-lib-table" in r["registered"]
    assert '"mine"' in t.read_text() and '"gen"' in t.read_text()
    r = kat.libmake.make(table(tmp_path), "TWO", lib="gen", out=project.parent, drill=1.0)
    assert r["registered"] == []


def test_project_prefixes_for_generated_and_added_parts(project, tmp_path, capsys):
    """FR-033: a project prefix map (J=CON) for parts kat makes itself; kat check warns about
    references that do not follow it, sync never renames them."""
    from kat.schematic import Schematic
    assert kat.prefixes(project) == {}
    assert cli.main(["prefix", str(project.parent), "J=CON", "U=IC"]) == 0
    assert json.loads(capsys.readouterr().out)["prefixes"] == {"J": "CON", "U": "IC"}
    assert kat.prefixes(project, {"U": ""}) == {"J": "CON"}                     # empty removes
    with pytest.raises(kat.KatError, match="letters"):
        kat.prefixes(project, {"J": "CON1"})
    r = kat.libmake.make(table(tmp_path), "ONE", lib="gen", out=project.parent, drill=1.0)
    assert '(property "Reference" "CON"' in Path(r["symbol"]).read_text()
    r = kat.libmake.make(table(tmp_path), "TWO", lib="gen", out=project.parent, drill=1.0, reference="X")
    assert '(property "Reference" "X"' in Path(r["symbol"]).read_text()
    s = Schematic(project)
    assert s.add_symbol("Connector_Generic:Conn_01x02", 50.8, 50.8).ref == "CON1"
    assert s.add_symbol("Device:R", 76.2, 50.8).ref == "R1"
    s.add_symbol("Connector_Generic:Conn_01x02", 101.6, 50.8, ref="J7")       # as a SKiDL netlist names it
    s.save()
    c = kat.check(project)
    assert c["prefix_warnings"] == [{"ref": "J7", "prefix": "J", "project_prefix": "CON"}]
    assert kat.prefixes(project, clear=True) == {}
    assert "prefix_warnings" not in kat.check(project)


def test_a_marker_sits_outside_the_outline_at_its_pin(tmp_path):
    r, _ = part(tmp_path, marker="3")
    tree = footprint_tree(r)
    box = r["outline"]
    polys = [p for p in find_all(tree, "fp_poly")]
    assert len(polys) == 2
    pts = [(float(x[1]), float(x[2])) for x in find_all(next(c for c in polys[0] if str(c[0]) == "pts"), "xy")]
    assert all(px >= box[2] for px, _ in pts) or all(py <= box[1] for _, py in pts), (pts, box)
    r2 = kat.libmake.make(table(tmp_path), "NOM", out=tmp_path / "o2", drill=1.0, marker=None)
    assert not find_all(footprint_tree(r2), "fp_poly")


@pytest.mark.parametrize("text, kw, match", [
    ("pin,x,y\n1,0\n", {"drill": 1.0}, "both needed"),
    ("pin,x,y\n1,a,0\n", {"drill": 1.0}, "not a number"),
    ("pin,row,col\n1,A,1\n", {"drill": 1.0}, "need a pitch"),
    ("pin,row,col\n1,,1\n", {"drill": 1.0, "pitch": (1, 1)}, "both needed"),
    ("pin,x,y,shape\n1,0,0,star\n", {"drill": 1.0}, "shape 'star'"),
    ("pin,x,y,type\n1,0,0,magic\n", {"drill": 1.0}, "type 'magic'"),
    ("pin,x,y,kind\n1,0,0,wired\n", {"drill": 1.0}, "kind 'wired'"),
    ("pin,x,y,kind\n1,0,0,npth\n", {"drill": 1.0}, "no pin number"),
    ("pin,x,y,kind\n,0,0,tht\n", {"drill": 1.0}, "no pin number"),
    ("pin,x,y,kind\n1,0,0,tht\n", {}, "needs a drill"),
    ("pin,x,y,drill\n1,0,0,big\n", {}, "drill 'big'"),
    ("pin,x,y,drill,pad\n1,0,0,1.0,0.5\n", {}, "smaller than its drill"),
    ("pin,x,y\n1,0,0\n2,0,0\n", {"drill": 1.0}, "one position"),
    ("pin,x,y\n", {"drill": 1.0}, "no rows"),
    ("x,y,drill\n0,0,1.0\n", {"marker": "9"}, "no pin of the table"),
    ("x,y,drill\n0,0,1.0\n", {"origin": "pin1"}, "needs a marker pin"),
])
def test_bad_tables_name_their_row(tmp_path, text, kw, match):
    with pytest.raises(kat.KatError, match=match):
        kat.libmake.make(table(tmp_path, text), "B", out=tmp_path / "o", **kw)
    assert not (tmp_path / "o" / "B.kicad_sym").exists()


@pytest.mark.parametrize("kw, match", [({"view": "x"}, "component or pin"), ({"origin": "x"}, "table, pin1"),
                                       ({"symbol": False, "footprint": False}, "nothing to make")])
def test_bad_options(tmp_path, kw, match):
    with pytest.raises(kat.KatError, match=match):
        kat.libmake.make(table(tmp_path), "B", out=tmp_path / "o", **kw)


def test_json_table_and_the_cli(tmp_path, capsys):
    t = table(tmp_path, json.dumps([{"pin": "1", "name": "A", "x": 0, "y": 0}, {"pin": "2", "name": "B", "x": 2.54, "y": 0}]), "p.json")
    code = cli.main(["lib", "make", "--pins", str(t), "--name", "P", "--lib", "L", "--out", str(tmp_path / "o"),
                     "--drill", "1", "--pad", "1.7,1.7", "--view", "pin", "--marker", "2"])
    r = json.loads(capsys.readouterr().out)
    assert code == 0 and r["lib_id"] == "L:P" and pads_of(r)["2"] == (-2.54, 0)
    assert cli.main(["lib", "make", "--pins", str(t), "--name", "P", "--lib", "L", "--out", str(tmp_path / "o"),
                     "--drill", "1"]) == 2
    assert "exists" in json.loads(capsys.readouterr().out)["error"]


def test_mapping_check_and_the_table_share_a_format(project, tmp_path):
    flow(project, tmp_path)
    m = table(tmp_path, "pin,name,net\nJ1.1,VCC,VCC\nJ1.2,SIG,SIG\nJ1.3,GND,GND\nJ1.4,CLK,GND\nJ1.5,OUT,OUT\nJ1.6,NC_6,OUT\n", "m.csv")
    r = kat.mapping.check(project, m)
    assert r["ok"] and r["ignored"] == ["name"], r
