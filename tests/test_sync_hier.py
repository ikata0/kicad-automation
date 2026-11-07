"""Hierarchical sync from SKiDL: a library block drawn as a sheet, used by a design whose
instances of that type become sheet boxes; references and values follow the design."""

import json
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

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints", "skidl")


CH_PY = '''"""Block CH: three RC stages and an output resistor."""
import os
import sys

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


CHAIN_PY = '''from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool
import kat
set_default_tool(KICAD10)
rc = kat.blocks.use("RC")
gnd = Net("GND")
j = Part("Connector_Generic", "Conn_01x02", tag="j",
         footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
nets = [Net("+5V")] + [Net(f"N{i}") for i in range(1, %d)]   # a power net into a signal port
j[1] += nets[0]
j[2] += gnd
for i in range(len(nets) - 1):
    rc(inp=nets[i], out=nets[i + 1], gnd=gnd, tag=f"S{i:02d}")
generate_netlist(file_="design.net")
kat.skidl_blocks.write("design.net")
'''


def test_many_boxes_grow_the_paper(lib, tmp_path):
    """FR-032: sheet boxes that do not fit below each other grow the paper, as parts do.
    FR-030: a repeated sync adds nothing, power symbols included (with a power net on a
    signal port: its label at the sheet pin is not turned into another power symbol)."""
    from kat.syncer import paper_size
    blocks.build("RC", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(CHAIN_PY % 31)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    rep = kat.sync(sch, d / "design.net", sheets=["rc"])
    assert rep["check"]["ok"], rep["check"]
    assert rep["paper"] and rep["paper"][0]["from"] == "A4" and rep["paper"][0]["sheet"] == "top.kicad_sch", rep["paper"]
    s = Schematic(sch)
    w, h = paper_size(s)
    boxes = hierarchy.sheets(s)
    assert len(boxes) == 30
    assert all(b["at"][0] + b["size"][0] < w and b["at"][1] + b["size"][1] < h - 45 for b in boxes)
    j = next(b for sym in s.symbols() if sym.ref == "J1" and (b := s.body_box(sym)))
    assert all(b["at"][1] > j[3] for b in boxes)                     # below the parts
    again = kat.sync(sch, d / "design.net")
    changed = {k: v for k, v in again.items() if isinstance(v, list) and v and k not in ("files", "possible_renames")}
    assert not changed, changed


TM_PY = '''"""Block TM: an op-amp follower on a divider, with its supply (power input pins:
ERC wants them driven)."""
import sys
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool, subcircuit


@subcircuit
def tm(out, vs, gnd):
    u = Part("Amplifier_Operational", "MCP6001-OT", footprint="Package_TO_SOT_SMD:SOT-23-5", tag="u")
    r1, r2 = (Part("Device", "R", value="10k", footprint="Resistor_SMD:R_0603_1608Metric", tag=t)
              for t in ("r1", "r2"))
    u[5] += vs
    u[2] += gnd
    vs += r1[1]
    r1[2] += u[3]
    u[3] += r2[1]
    r2[2] += gnd
    u[1] += out
    u[4] += out


if __name__ == "__main__":
    import kat
    set_default_tool(KICAD10)
    out, vs, gnd = Net("OUT"), Net("VSUP"), Net("GND")      # not a power symbol: vs is a port
    j = Part("Connector_Generic", "Conn_01x03", tag="j",
             footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
    j[1] += vs
    j[2] += out
    j[3] += gnd
    tm(out=out, vs=vs, gnd=gnd, tag="DUT")
    net = sys.argv[1] if len(sys.argv) > 1 else "TM.net"
    generate_netlist(file_=net)
    kat.skidl_blocks.write(net)
'''

SUPPLIES_PY = '''from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool
import kat
set_default_tool(KICAD10)
tm = kat.blocks.use("TM")
gnd = Net("GND")
j = Part("Connector_Generic", "Conn_01x03", tag="j",
         footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
j[3] += gnd
for i, supply in enumerate(("+5V", "VBAT_SW")):     # a power-library name and another one
    o = Net(f"OUT_{i}")
    j[1 + i] += o
    tm(out=o, vs=Net(supply), gnd=gnd, tag=f"T{i}")
generate_netlist(file_="design.net")
kat.skidl_blocks.write("design.net")
'''


def test_supplies_reaching_only_blocks_get_one_flag_at_the_top(lib, tmp_path):
    """FR-031: a supply that reaches the blocks only through their ports has no part on the
    root sheet, so the root's own ERC never sees it undriven: the whole hierarchy's ERC
    does, and the flag goes on the root sheet, once, never into the block's sheet."""
    blocks.new("TM")
    (lib / "TM" / "TM.py").write_text(TM_PY)
    blocks.build("TM", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(SUPPLIES_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    rep = kat.sync(sch, d / "design.net", sheets=["tm"])
    assert sorted(rep["flags"]) == ["top.kicad_sch: +5V", "top.kicad_sch: GND", "top.kicad_sch: VBAT_SW"], rep
    c = kat.check(sch, d / "design.net")
    assert c["erc"]["errors"] == 0, [v for v in c["erc"]["violations"] if v["severity"] == "error"]
    assert c["netlist_vs_expected"]["match"]
    flags = lambda f: sum(x.field("Value") == "PWR_FLAG" for x in Schematic(f).symbols())
    assert flags(sch) == 3 and flags(lib / "TM" / "TM_block.kicad_sch") == 0
    again = kat.sync(sch, d / "design.net")
    assert not again["flags"] and flags(sch) == 3, again


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


RENAMED_PY = DESIGN_PY.replace('tag=f"CH_{ch}"', 'tag={"A": "LEFT", "B": "CH_B"}[ch]')


def renamed_project(lib, tmp_path):
    """A design with sheets CH_A and CH_B, CH_A moved by hand and resized."""
    blocks.build("RC", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    assert kat.sync(sch, d / "design.net", sheets=["rc"])["check"]["ok"]
    with kat.edit(sch) as s:
        s.move("CH_A", to=(152.4, 101.6))
        s.sheet_box("CH_A", size=(25.4, 12.7), left=["out"], right=["inp"])
    return d, sch


def rename_in_design(d):
    (d / "design.py").write_text(RENAMED_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)


def box(sch, name):
    return {sh["name"]: sh for sh in hierarchy.sheets(Schematic(sch))}[name]


def test_a_renamed_instance_keeps_its_box(lib, tmp_path):
    d, sch = renamed_project(lib, tmp_path)
    before = box(sch, "CH_A")
    other = box(sch, "CH_B")
    wires = len(Schematic(sch).wires())
    rename_in_design(d)
    plan = kat.sync(sch, d / "design.net", dry_run=True, rename_sheets={"CH_A": "LEFT"})
    assert plan["dry_run"] and plan["renamed_sheets"][0]["to"] == "LEFT"
    assert "CH_A" in {sh["name"] for sh in hierarchy.sheets(Schematic(sch))}, "a dry run saves nothing"
    rep = kat.sync(sch, d / "design.net", rename_sheets={"/CH_A": "/LEFT"})
    assert rep["check"]["ok"], rep["check"]
    assert not rep["added"] and not rep["removed"] and not rep["possible_renames"], rep
    assert rep["renamed_sheets"] == [{"from": "CH_A", "to": "LEFT", "uuid": before["uuid"]}]
    after = box(sch, "LEFT")
    assert {k: after[k] for k in ("uuid", "at", "size", "pins", "file", "fields")} == \
        {k: before[k] for k in ("uuid", "at", "size", "pins", "file", "fields")}
    assert box(sch, "CH_B")["uuid"] == other["uuid"]
    assert len(Schematic(sch).wires()) == wires, "the wiring stays"
    assert {i["path"] for i in hierarchy.tree(sch)} == {"/", "/LEFT", "/CH_B"}
    again = kat.sync(sch, d / "design.net")
    assert again["check"]["ok"] and not again["added"] and not again["removed"] and not again["renamed_sheets"]


def test_without_the_rename_it_is_replaced_and_named_as_a_possibility(lib, tmp_path):
    d, sch = renamed_project(lib, tmp_path)
    old = box(sch, "CH_A")
    rename_in_design(d)
    plan = kat.sync(sch, d / "design.net", dry_run=True)
    assert plan["possible_renames"] == [{"from": "CH_A", "to": "LEFT"}]
    rep = kat.sync(sch, d / "design.net")
    assert rep["check"]["ok"] and rep["removed"] == ["sheet CH_A"] and rep["added"] == ["sheet LEFT"]
    assert box(sch, "LEFT")["uuid"] != old["uuid"]


@pytest.mark.parametrize("names, match", [
    ({"NOPE": "LEFT"}, "no sheet 'NOPE'"),
    ({"CH_A": "NOPE"}, "no sheet instance 'NOPE'"),
    ({"CH_B": "LEFT"}, "still in the design"),
    ({"CH_A": "CH_B"}, "already a sheet"),
])
def test_bad_renames_change_nothing(lib, tmp_path, names, match):
    d, sch = renamed_project(lib, tmp_path)
    rename_in_design(d)
    text = sch.read_text()
    with pytest.raises(kat.KatError, match=match):
        kat.sync(sch, d / "design.net", rename_sheets=names)
    assert sch.read_text() == text


def test_a_rename_on_a_flat_sheet_is_refused(tmp_path):
    from test_sync import BASE_NETS, BASE_PARTS, write_netlist
    sch = tmp_path / "p" / "p.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    net = write_netlist(tmp_path / "d.net", BASE_PARTS, BASE_NETS)
    with pytest.raises(kat.KatError, match="flat"):
        kat.sync(sch, net, rename_sheets={"A": "B"})


def test_the_board_keeps_placement_copper_and_stamp_records(lib, tmp_path):
    from test_pcb_stamp import check, env_board, layout_rc
    from kat.pcb.board import Board
    from kat.pcb.stamping import stamps_file
    d, sch = renamed_project(lib, tmp_path)
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
        b.stamp("RC", {"/CH_A": (110, 103), "/CH_B": (110, 111)})
    base = Board(pcb)
    where = {f.ref: (f.x, f.y, f.rot, f.path) for f in base.footprints()}
    tracks = len(base.tracks())
    assert any(str(g[1]).startswith("/CH_A") for g in base._groups())
    rename_in_design(d)
    kat.sync(sch, d / "design.net", rename_sheets={"CH_A": "LEFT"})
    plan = kat.pcb.sync(pcb, force=True, dry_run=True)
    assert plan["renamed_instances"] == [{"from": "/CH_A", "to": "/LEFT"}]
    assert any(str(g[1]).startswith("/CH_A") for g in Board(pcb)._groups()), "a dry run saves nothing"
    r = kat.pcb.sync(pcb, force=True)
    assert r["check"]["parity"] == 0, r
    assert r["renamed_instances"] == [{"from": "/CH_A", "to": "/LEFT"}] and r["renamed_instances_items"]["groups"] >= 1
    after = Board(pcb)
    assert {f.ref: (f.x, f.y, f.rot, f.path) for f in after.footprints()} == where
    assert len(after.tracks()) == tracks
    names = [str(g[1]) for g in after._groups()]
    assert any(n.startswith("/LEFT") for n in names) and not any(n.startswith("/CH_A") for n in names), names
    saved = json.loads(stamps_file(pcb).read_text())
    assert "/LEFT" in saved and "/CH_A" not in saved
    assert kat.pcb.instances(pcb, "RC")["instances"] == ["/CH_B", "/LEFT"]
    # stamping it again replaces the copy instead of adding a second
    with kat.pcb.edit(pcb) as b:
        b.stamp("RC", {"/LEFT": (110, 103)})
    assert len(Board(pcb).tracks()) == tracks
    c = check(pcb)
    assert c["drc_errors"] == 0 and c["parity"] == 0, c
    again = kat.pcb.sync(pcb, force=True)
    assert "renamed_instances" not in again and not again["changed"]


def test_renaming_a_sheet_with_sheets_inside_it(lib, tmp_path):
    blocks.new("CH")
    (lib / "CH" / "CH.py").write_text(CH_PY)
    blocks.build("RC", force=True)
    blocks.build("CH", force=True)
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(TOP_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    assert kat.sync(sch, d / "design.net", sheets=["ch", "rc"])["check"]["ok"]
    before = box(sch, "CH_A")
    uuids = {i["path"].removeprefix("/CH_A"): i["uuids"] for i in hierarchy.tree(sch) if i["path"].startswith("/CH_A")}
    (d / "design.py").write_text(TOP_PY.replace('tag=f"CH_{name}"', 'tag={"A": "LEFT", "B": "CH_B"}[name]'))
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    with pytest.raises(kat.KatError, match="own drawing"):
        kat.sync(sch, d / "design.net", rename_sheets={"CH_A/RC1": "CH_A/RC9"})
    rep = kat.sync(sch, d / "design.net", rename_sheets={"CH_A": "LEFT"})
    assert rep["check"]["ok"] and not rep["added"] and not rep["removed"], rep
    assert box(sch, "LEFT")["uuid"] == before["uuid"]
    after = {i["path"].removeprefix("/LEFT"): i["uuids"] for i in hierarchy.tree(sch) if i["path"].startswith("/LEFT")}
    assert after == uuids and "/RC1" in after
    assert kat.check(sch, d / "design.net")["netlist_vs_expected"]["match"]


def test_cli_rename(lib, tmp_path, capsys):
    d, sch = renamed_project(lib, tmp_path)
    rename_in_design(d)
    capsys.readouterr()
    assert cli.main(["sync", str(sch), str(d / "design.net"), "--rename-sheet", "CH_A"]) == 2
    assert "OLD=NEW" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(["sync", str(sch), str(d / "design.net"), "--rename-sheet", "CH_A=LEFT"]) == 0
    assert json.loads(capsys.readouterr().out)["renamed_sheets"][0]["to"] == "LEFT"
