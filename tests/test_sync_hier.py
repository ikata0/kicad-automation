"""Hierarchical sync from SKiDL: a library block drawn as a sheet, used by a design whose
instances of that type become sheet boxes; references and values follow the design."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import blocks, cli, hierarchy
from kat.pcb.syncer import read_design
from kat.schematic import Schematic, snap
from kat.sexpr import find, find_all

sys.path.insert(0, str(Path(__file__).parent))
from test_blocks import DESIGN_PY, RC_PY  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
    reason="needs kicad-cli and skidl")


CH_PY = '''"""Block CH: three RC stages and an output resistor."""
import os
import sys

os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool, subcircuit  # noqa: E402
import kat  # noqa: E402

rc = kat.blocks.use("RC")


@subcircuit
def ch(inp, out, gnd, ra="1k", rb="2k2"):
    n1, n2, n3 = Net(), Net(), Net()
    rc(inp=inp, out=n1, gnd=gnd, r_val=ra, tag="RC1")
    rc(inp=n1, out=n2, gnd=gnd, r_val=rb, tag="RC2")
    rc(inp=n2, out=n3, gnd=gnd, r_val="4k7", tag="RC3")
    r = Part("Device", "R", value="100", footprint="Resistor_SMD:R_0603_1608Metric", tag="rout")
    n3 += r[1]
    r[2] += out


if __name__ == "__main__":
    set_default_tool(KICAD10)
    inp, out, gnd = Net("IN"), Net("OUT"), Net("GND")
    j = Part("Connector_Generic", "Conn_01x03", tag="j",
             footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
    j[1] += inp
    j[2] += out
    j[3] += gnd
    ch(inp=inp, out=out, gnd=gnd, tag="DUT")
    net = sys.argv[1] if len(sys.argv) > 1 else "CH.net"
    generate_netlist(file_=net)
    kat.skidl_blocks.write(net)
'''

TOP_PY = '''import os
os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool
import kat
set_default_tool(KICAD10)
ch = kat.blocks.use("CH")
gnd = Net("GND")
j = Part("Connector_Generic", "Conn_01x05", tag="j",
         footprint="Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical")
for i, name in enumerate("AB"):
    a, b = Net(f"IN_{name}"), Net(f"OUT_{name}")
    j[2 * i + 1] += a
    j[2 * i + 2] += b
    ch(inp=a, out=b, gnd=gnd, ra="1k" if name == "A" else "3k3", tag=f"CH_{name}")
j[5] += gnd
generate_netlist(file_="design.net")
kat.skidl_blocks.write("design.net")
'''


CELL_PY = '''"""Block CELL: R then LED, the inner net named (as in the LED matrix)."""
import os
import sys

os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool, subcircuit  # noqa: E402
import kat  # noqa: E402


@subcircuit
def cell(inp, gnd):
    a = Net("LED_A")
    r = Part("Device", "R", value="150", footprint="Resistor_SMD:R_0402_1005Metric", tag="r")
    d = Part("Device", "LED", value="green", footprint="LED_SMD:LED_0603_1608Metric", tag="d")
    inp += r[1]
    r[2] += a
    a += d[2]
    d[1] += gnd


if __name__ == "__main__":
    set_default_tool(KICAD10)
    inp, gnd = Net("IN"), Net("GND")
    j = Part("Connector_Generic", "Conn_01x02", tag="j",
             footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
    j[1] += inp
    j[2] += gnd
    cell(inp=inp, gnd=gnd, tag="DUT")
    net = sys.argv[1] if len(sys.argv) > 1 else "CELL.net"
    generate_netlist(file_=net)
    kat.skidl_blocks.write(net)
'''

PAIR_PY = CELL_PY.replace("CELL", "PAIR").replace('''@subcircuit
def cell(inp, gnd):
    a = Net("LED_A")
    r = Part("Device", "R", value="150", footprint="Resistor_SMD:R_0402_1005Metric", tag="r")
    d = Part("Device", "LED", value="green", footprint="LED_SMD:LED_0603_1608Metric", tag="d")
    inp += r[1]
    r[2] += a
    a += d[2]
    d[1] += gnd''', '''cell = kat.blocks.use("CELL")


@subcircuit
def pair(inp, gnd):
    for i in range(2):
        cell(inp=inp, gnd=gnd, tag=f"CELL{i}")''').replace("    cell(inp=inp, gnd=gnd, tag=\"DUT\")",
                                                     "    pair(inp=inp, gnd=gnd, tag=\"DUT\")")


@pytest.fixture
def lib(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAT_REPORT", "0")
    monkeypatch.chdir(tmp_path)
    blocks.new("RC")
    (tmp_path / "blocks" / "RC" / "RC.py").write_text(RC_PY)
    return tmp_path / "blocks"


def test_block_build_writes_the_block_as_a_sheet_with_ports_and_parameters(lib):
    res = blocks.build("RC", force=True)
    d = lib / "RC"
    root, sheet = d / "RC.kicad_sch", d / "RC_block.kicad_sch"
    assert sheet.exists()
    assert [sh["name"] for sh in hierarchy.sheets(Schematic(root))] == ["DUT"]
    ports = hierarchy.ports(Schematic(sheet))
    assert set(ports) == {"inp", "out"}                      # gnd is a power net: global
    values = {x.field("SKiDL Tag"): x.field("Value") for x in Schematic(sheet).symbols()
              if not x.ref.startswith("#")}
    assert values == {"DUT/r": "${r_val}", "DUT/c": "100n"}
    pro = json.loads((d / "RC.kicad_pro").read_text())
    assert pro["text_variables"] == {"r_val": "10k"}
    parts, _ = read_design(root)                           # KiCad resolves it: the harness value
    assert parts["R1"].value == "10k" and sorted(parts) == ["C1", "J1", "R1"]
    assert res["schematic"]["ok"], res
    c = kat.check(root, d / "RC.net")
    assert c["netlist_vs_expected"]["match"] and c["erc"]["errors"] == 0, c
    # B18: gnd is a power port, not a port; the ${r_val} value is not reported as a change
    assert res["ports"] == ["inp", "out"] and res["power"] == ["gnd"], res
    again = blocks.build("RC", force=True)
    assert not again["schematic"]["updated"], again["schematic"]


def test_design_draws_block_instances_as_sheets(lib, tmp_path):
    blocks.build("RC", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    rep = kat.sync(sch, d / "design.net", sheets=["rc"])
    assert rep["check"]["ok"], rep["check"]
    boxes = hierarchy.sheets(Schematic(sch))
    assert [b["name"] for b in boxes] == ["CH_A", "CH_B"]
    assert boxes[1]["fields"] == {"r_val": "3k3"}
    assert Path(boxes[0]["path"]) == (lib / "RC" / "RC_block.kicad_sch").resolve()
    parts, pin_net = read_design(sch)
    comps, _ = kat.netlist.read_design(d / "design.net")
    assert sorted(parts) == sorted(comps)                  # the design's references
    r_b = next(c.ref for c in comps.values() if c.tag == "CH_B/r")
    assert parts[r_b].value == "3k3"
    assert kat.check(sch, d / "design.net")["netlist_vs_expected"]["match"]
    # again: nothing changes; the choice of sheets is remembered
    again = kat.sync(sch, d / "design.net")
    assert again["check"]["ok"] and not again["added"] and not again["removed"]
    # the block itself keeps its own references
    assert sorted(read_design(lib / "RC" / "RC.kicad_sch")[0]) == ["C1", "J1", "R1"]

    # the board: the block's layout stamped by footprint path onto both sheet instances
    from test_pcb_stamp import check, env_board, layout_rc
    rc_pcb = lib / "RC" / "RC.kicad_pcb"
    with kat.pcb.edit(rc_pcb) as b:
        env_board(b, 14, 10)
        b.footprint("J1").place(111, 101.46, 0)
    layout_rc(rc_pcb)
    with kat.pcb.edit(rc_pcb) as b:
        env_board(b, 14, 10)
    pcb = sch.with_suffix(".kicad_pcb")
    kat.pcb.sync(pcb, force=True)
    with kat.pcb.edit(pcb) as b:
        b.set_copper_layers(4)
        b.outline(rect=(100, 100, 40, 20))
        b.plane("GND", "In1.Cu")
        assert b.instances("RC") == ["/CH_A", "/CH_B"]
        res = b.stamp("RC", {"/CH_A": (110, 103), "/CH_B": (110, 111)})["instances"]
        b.footprint(next(c.ref for c in comps.values() if c.tag == "j")).place(103, 104.92, 0)
    assert [(r["mode"], r["footprints"]) for r in res] == [("hierarchical", 2)] * 2
    c = check(pcb)
    assert c["drc_errors"] == 0 and c["parity"] == 0 and c["unconnected"] == 4, c


def test_nested_blocks_pass_parameters_down(lib, tmp_path):
    blocks.build("RC", force=True)
    blocks.new("CH")
    (lib / "CH" / "CH.py").write_text(CH_PY)
    res = blocks.build("CH", force=True)
    assert res["schematic"]["ok"], res
    ch_sheet = lib / "CH" / "CH_block.kicad_sch"
    boxes = {b["name"]: b for b in hierarchy.sheets(Schematic(ch_sheet))}
    assert sorted(boxes) == ["RC1", "RC2", "RC3"]
    assert boxes["RC1"]["fields"] == {"r_val": "${ra}"} and boxes["RC2"]["fields"] == {"r_val": "${rb}"}
    assert boxes["RC3"]["fields"] == {"r_val": "4k7"}
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(TOP_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    rep = kat.sync(sch, d / "design.net", sheets=["ch", "rc"])
    assert rep["check"]["ok"], rep["check"]
    paths = [i["path"] for i in hierarchy.tree(sch)]
    assert "/CH_B/RC1" in paths and len(paths) == 9
    parts, _ = read_design(sch)
    comps, _ = kat.netlist.read_design(d / "design.net")
    by_tag = {c.tag: c.ref for c in comps.values()}
    assert parts[by_tag["CH_B/RC1/r"]].value == "3k3" and parts[by_tag["CH_A/RC1/r"]].value == "1k"
    assert parts[by_tag["CH_A/RC2/r"]].value == "2k2"


def test_a_block_changed_after_its_build_is_caught(lib, tmp_path):
    blocks.build("RC", force=True)
    (lib / "RC" / "RC.py").write_text(RC_PY.replace('value="100n"', 'value="220n"'))
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    rep = kat.sync(sch, d / "design.net", sheets=["rc"])
    assert not rep["check"]["ok"]
    assert {p["problem"] for p in rep["check"]["parts"]} == {"value"}
    assert all(p["drawn"] == "100n" and p["design"] == "220n" for p in rep["check"]["parts"])


def test_a_design_using_a_block_does_not_make_it_stale(lib, tmp_path):
    """I9: syncing a design writes its instance data into the library's block sheet; the
    block's check still holds, and its flat drawing is not re-stamped for that."""
    blocks.build("RC", force=True)
    blocks.new("CH")
    (lib / "CH" / "CH.py").write_text(CH_PY)
    blocks.build("CH", force=True)
    blocks.check("RC", require=())        # no board yet: the checks fail, but are recorded
    before_stale = blocks.stale(lib / "RC")
    sheet = lib / "RC" / "RC_block.kicad_sch"
    before = sheet.read_text()
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(TOP_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    assert kat.sync(sch, d / "design.net", sheets=["ch", "rc"])["check"]["ok"]
    assert sheet.read_text() != before                      # KiCad's instance data was added
    assert blocks.stale(lib / "RC") == before_stale and "changed" not in before_stale
    assert not blocks.check("CH", require=()).get("warnings")
    assert blocks.build("CH", force=True)["flat"]["restamped"] == []


def test_boxes_are_arranged_and_keep_hand_changes(lib, tmp_path):
    blocks.build("RC", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    assert kat.sync(sch, d / "design.net", sheets=["rc"])["check"]["ok"]
    a, b = hierarchy.sheets(Schematic(sch))
    assert a["at"][1] == b["at"][1] and a["at"][0] < b["at"][0]      # one row, not a column
    for box in (a, b):                                               # inp left, out right
        assert box["pins"]["inp"]["x"] == box["at"][0]
        assert box["pins"]["out"]["x"] == box["at"][0] + box["size"][0]
    # by hand: CH_B elsewhere, bigger, its pins swapped; a re-sync keeps all of it
    with kat.edit(sch) as s:
        s.move("CH_B", to=(152.4, 101.6))
        s.sheet_box("CH_B", size=(25.4, 12.7), left=["out"], right=["inp"])
    before = {sh["name"]: sh for sh in hierarchy.sheets(Schematic(sch))}["CH_B"]
    again = kat.sync(sch, d / "design.net")
    assert again["check"]["ok"] and not again["added"] and not again["updated"], again
    after = {sh["name"]: sh for sh in hierarchy.sheets(Schematic(sch))}["CH_B"]
    assert {k: after[k] for k in ("uuid", "at", "size", "pins")} == {k: before[k] for k in ("uuid", "at", "size", "pins")}
    assert kat.check(sch, d / "design.net")["netlist_vs_expected"]["match"]



def test_renamed_nets_and_removed_boxes_leave_nothing_behind(lib, tmp_path):
    """B3: a renamed net that ends on a root part (J1) and on sheet pins is renamed at the
    connector too, and the sync says so. B6: a box that goes away takes its pin stubs and
    labels with it."""
    blocks.build("RC", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    assert kat.sync(sch, d / "design.net", sheets=["rc"])["check"]["ok"]

    (d / "design.py").write_text(DESIGN_PY.replace('Net(f"IN_{ch}")', 'Net(f"SIG_{ch}")'))
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    rep = kat.sync(sch, d / "design.net")
    assert rep["check"]["ok"], rep["check"]
    assert {(r["from"], r["to"]) for r in rep["renamed"]} >= {("IN_A", "SIG_A"), ("IN_B", "SIG_B")}
    texts = {l["text"] for l in Schematic(sch).labels()}
    assert not texts & {"IN_A", "IN_B"}, texts
    assert kat.check(sch, d / "design.net")["netlist_vs_expected"]["match"]

    (d / "design.py").write_text(DESIGN_PY.replace('Net(f"IN_{ch}")', 'Net(f"SIG_{ch}")')
                                 .replace('enumerate("AB")', 'enumerate("A")'))
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    before = [l["text"] for l in Schematic(sch).labels()]
    rep = kat.sync(sch, d / "design.net")
    assert rep["removed"] == ["sheet CH_B"], rep["removed"]
    texts = [l["text"] for l in Schematic(sch).labels()]
    # the box's labels are gone; J1's now unconnected pins keep theirs (as in a flat sync)
    assert before.count("SIG_B") == 2 and texts.count("SIG_B") == 1, texts
    assert before.count("OUT_B") == 2 and texts.count("OUT_B") == 1, texts
    assert kat.check(sch, d / "design.net")["netlist_vs_expected"]["match"]


def test_skidl_nc_pins_get_no_connect_flags(lib, tmp_path):
    """B11: SKiDL leaves NC pins out of the netlist; the block record carries them and
    sync draws a flag on each, flat and hierarchical."""
    blocks.build("RC", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY.replace("Conn_01x05", "Conn_01x06")
                                 .replace("j[5] += gnd", "j[5] += gnd\nj[6] += NC"))
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    assert json.loads((d / "design.blocks.json").read_text())["no_connect"] == ["J1.6"]
    for name, sheets in (("flat", None), ("top", ["rc"])):
        sch = d / name / f"{name}.kicad_sch"
        assert cli.main(["new", str(sch)]) == 0
        rep = kat.sync(sch, d / "design.net", sheets=sheets)
        assert rep["check"]["ok"], rep["check"]
        s = Schematic(sch)
        pin = next(p for p in s.pins(s.find_symbols("J1")[0]) if p["number"] == "6")
        flags = [(float(find(n, "at")[1]), float(find(n, "at")[2])) for n in find_all(s.tree, "no_connect")]
        assert flags == [(pytest.approx(pin["x"]), pytest.approx(pin["y"]))], (name, flags)
        again = kat.sync(sch, d / "design.net")
        assert len(list(find_all(Schematic(sch).tree, "no_connect"))) == 1

def test_a_nested_block_gets_a_flat_drawing_for_flat_designs(lib, tmp_path):
    blocks.build("RC", force=True)
    blocks.new("CH")
    (lib / "CH" / "CH.py").write_text(CH_PY)
    res = blocks.build("CH", force=True)
    flat = lib / "CH" / "CH_flat.kicad_sch"
    assert res["flat"]["created"] and res["flat"]["ok"] and flat.exists()
    assert res["flat"]["off_page"] == 0 and not res["flat"].get("warnings"), res["flat"]   # B15
    s = Schematic(flat)
    j = s.body_box(next(x for x in s.symbols() if x.field("SKiDL Tag") == "j"))
    others = [s.body_box(x) for x in s.symbols() if x.field("Block") and s.body_box(x)]
    assert all(j[2] < b[0] or b[2] < j[0] or j[3] < b[1] or b[3] < j[1] for b in others)
    chk = blocks.check("CH", require=())
    assert chk["flat"]["ok"] and not chk.get("warnings")
    s = Schematic(flat)
    tags = {x.field("SKiDL Tag"): x for x in s.symbols() if not x.ref.startswith("#")}
    assert {t.rsplit("/", 1)[0] for t in tags if t != "j"} == {"DUT", "DUT/RC1", "DUT/RC2", "DUT/RC3"}

    # a flat design: one stamp per channel, RC stages framed inside
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(TOP_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "flat" / "flat.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    kat.sync(sch, d / "design.net")
    out = kat.stamp(sch, "CH", "all", design=d / "design.net", make_room=True)
    assert [i["instance"] for i in out["instances"]] == ["/CH_A", "/CH_B"]
    assert out["check"]["erc_errors"] == 0 and out["check"]["clean"]
    assert kat.check(sch, d / "design.net")["netlist_vs_expected"]["match"]
    texts = {str(t[1]) for t in Schematic(sch).tree if isinstance(t, list) and str(t[0]) == "text"}
    assert {"CH_A (ch)", "CH_B (ch)", "CH_A/RC1 (rc)", "CH_B/RC3 (rc)"} <= texts

    # RC's drawing changes, rout was moved by hand: a rebuild re-stamps only the RC stages
    rout = next(x.ref for x in s.symbols() if x.field("SKiDL Tag") == "DUT/rout")
    with kat.edit(flat) as e:
        e.move(rout, by=(0, -5.08))
    moved = next(x.place for x in Schematic(flat).symbols() if x.ref == rout)
    rc_sheet = lib / "RC" / "RC_block.kicad_sch"
    c1 = next(x.ref for x in Schematic(rc_sheet).symbols() if (x.field("SKiDL Tag") or "").endswith("/c"))
    with kat.edit(rc_sheet) as e:
        e.move(c1, by=(-5.08, 0))
    assert "RC" in blocks.check("CH", require=())["warnings"][0]
    res = blocks.build("CH", force=True)
    assert not res["flat"]["created"] and res["flat"]["restamped"] == ["RC"] and res["flat"]["ok"]
    s = Schematic(flat)
    assert next(x.place for x in s.symbols() if x.ref == rout) == moved
    by_tag = {x.field("SKiDL Tag"): x.place for x in s.symbols() if x.field("SKiDL Tag")}
    src = {x.field("SKiDL Tag").rsplit("/", 1)[1]: x.place for x in Schematic(rc_sheet).symbols()
           if x.field("SKiDL Tag")}
    for i in (1, 2, 3):
        r, c = by_tag[f"DUT/RC{i}/r"], by_tag[f"DUT/RC{i}/c"]
        assert (r.x - c.x, r.y - c.y) == pytest.approx((src["r"].x - src["c"].x, src["r"].y - src["c"].y))


def test_a_changed_cell_is_restamped_in_its_own_place(lib, tmp_path):
    """B1: the instance's own wiring (R -> LED with its LED_A label) is no obstacle when
    the block is re-stamped where it stands."""
    blocks.new("CELL")
    (lib / "CELL" / "CELL.py").write_text(CELL_PY)
    blocks.build("CELL", force=True)
    blocks.new("PAIR")
    (lib / "PAIR" / "PAIR.py").write_text(PAIR_PY)
    res = blocks.build("PAIR", force=True)
    assert res["flat"]["created"] and res["flat"]["ok"], res["flat"]
    flat = lib / "PAIR" / "PAIR_flat.kicad_sch"
    with kat.edit(flat) as e:             # R -> LED by one wire, LED_A mid-wire (as in the matrix)
        s = e.schematic
        for i in range(2):
            r, d = (next(x for x in s.symbols() if x.field("SKiDL Tag") == f"DUT/CELL{i}/{k}") for k in "rd")
            net = next(str(n[1]) for n in find_all(s.tree, "label") if str(n[1]).startswith("LED_A")
                       and any(abs(float(find(n, "at")[1]) - p["x"]) + abs(float(find(n, "at")[2]) - p["y"]) < 3
                               for p in s.pins(r) if p["number"] == "2"))
            s.detach(r.ref, "2")
            s.detach(d.ref, "2")
            rp = next(p for p in s.pins(r) if p["number"] == "2")
            dp = next(p for p in s.pins(d) if p["number"] == "2")
            corner = (rp["x"], dp["y"])
            s.add_wire([(rp["x"], rp["y"]), corner, (dp["x"], dp["y"])])
            s.add_label(net, snap((corner[0] + dp["x"]) / 2), dp["y"])
    assert kat.check(flat, lib / "PAIR" / "PAIR.net")["netlist_vs_expected"]["match"]
    cell_sheet = lib / "CELL" / "CELL_block.kicad_sch"
    d = next(x.ref for x in Schematic(cell_sheet).symbols() if (x.field("SKiDL Tag") or "").endswith("/d"))
    with kat.edit(cell_sheet) as e:
        e.move(d, by=(0, 5.08))
    res = blocks.build("PAIR", force=True)
    assert res["flat"]["restamped"] == ["CELL"] and res["flat"]["ok"], res["flat"]
    plan = kat.stamp(flat, "CELL", "all", design=lib / "PAIR" / "PAIR.net", plan=True)
    assert plan["free"], plan
