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
from kat.schematic import Schematic

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
