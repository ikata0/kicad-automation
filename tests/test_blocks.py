"""The block library: new -> build -> layout -> check, used by a design (SKiDL function) and
stamped onto its board by name."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import blocks, cli

sys.path.insert(0, str(Path(__file__).parent))
from test_pcb_stamp import check, env_board, layout_rc  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
    reason="needs kicad-cli and skidl")

RC_PY = '''"""Block RC: RC low-pass. Ports: inp, out, gnd. Parameters: r_val."""
import os
import sys

os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool, subcircuit  # noqa: E402


@subcircuit
def rc(inp, out, gnd, r_val="10k"):
    r = Part("Device", "R", value=r_val, footprint="Resistor_SMD:R_0603_1608Metric", tag="r")
    c = Part("Device", "C", value="100n", footprint="Capacitor_SMD:C_0603_1608Metric", tag="c")
    inp += r[1]
    r[2] += out
    out += c[1]
    c[2] += gnd


if __name__ == "__main__":
    import kat
    set_default_tool(KICAD10)
    inp, out, gnd = Net("IN"), Net("OUT"), Net("GND")
    j = Part("Connector_Generic", "Conn_01x03", tag="j",
             footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
    j[1] += inp
    j[2] += out
    j[3] += gnd
    rc(inp=inp, out=out, gnd=gnd, tag="DUT")
    net = sys.argv[1] if len(sys.argv) > 1 else "RC.net"
    generate_netlist(file_=net)
    kat.skidl_blocks.write(net)
'''

DESIGN_PY = '''import os, sys
os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool
import kat
set_default_tool(KICAD10)
rc = kat.blocks.use("RC")
gnd = Net("GND")
j = Part("Connector_Generic", "Conn_01x05", tag="j",
         footprint="Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical")
for i, ch in enumerate("AB"):
    a, b = Net(f"IN_{ch}"), Net(f"OUT_{ch}")
    j[2 * i + 1] += a
    j[2 * i + 2] += b
    rc(inp=a, out=b, gnd=gnd, r_val="1k" if ch == "A" else "3k3", tag=f"CH_{ch}")
j[5] += gnd
generate_netlist(file_="design.net")
kat.skidl_blocks.write("design.net")
'''


def run(capsys, *argv) -> dict:
    code = cli.main([str(a) for a in argv])
    out = json.loads(capsys.readouterr().out)
    assert "error" not in out, out
    return {"code": code, **out}


@pytest.fixture
def lib(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAT_REPORT", "0")
    monkeypatch.chdir(tmp_path)
    made = run(capsys, "block", "new", "RC")
    assert Path(made["dir"]) == tmp_path / "blocks" / "RC"
    (tmp_path / "blocks" / "RC" / "RC.py").write_text(RC_PY)
    built = run(capsys, "block", "build", "RC", "--force")
    assert built["type"] == "rc" and built["ports"] == ["gnd", "inp", "out"]
    assert built["params"] == {"r_val": "10k"} and built["warnings"] == []
    with kat.edit(tmp_path / "blocks" / "RC" / "RC_block.kicad_sch") as s:   # arrangement pass
        s.move("R1", by=(7.62, 0))
    pcb = tmp_path / "blocks" / "RC" / "RC.kicad_pcb"
    with kat.pcb.edit(pcb) as b:
        env_board(b, 14, 10)
        b.footprint("J1").place(111, 101.46, 0)
    layout_rc(pcb)
    with kat.pcb.edit(pcb) as b:          # layout_rc sized outline and plane for the RC alone
        env_board(b, 14, 10)
    return tmp_path / "blocks"


def test_new_refuses_existing_and_bad_names(lib, capsys):
    assert cli.main(["block", "new", "RC"]) == 2
    assert "exists" in capsys.readouterr().out
    with pytest.raises(kat.KatError, match="identifier"):
        blocks.new("1RC")


def test_check_counts_only_the_block(lib, capsys):
    res = run(capsys, "block", "check", "RC", "--no-require")
    assert res["ok"] and res["code"] == 0, res
    assert res["drc_errors"] == 0 and res["parity"] == 0, res
    assert res["unconnected"] == 0 and res["unconnected_to_environment"] == 2, res
    assert res["environment_parts"] == ["J1"]
    listed = run(capsys, "block", "list")["blocks"]
    assert listed[0]["block"] == "RC" and listed[0]["params"] == {"r_val": "10k"}
    assert listed[0]["state"] == "clean"


def design(tmp_path: Path, capsys) -> Path:
    """The design using the RC block twice, synced flat; returns its schematic."""
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    capsys.readouterr()
    kat.sync(sch, d / "design.net")
    return sch


def test_flat_sheet_gets_the_block_drawing(lib, tmp_path, capsys):
    from kat.schematic import Schematic
    sch = design(tmp_path, capsys)
    net = tmp_path / "design" / "design.net"
    with pytest.raises(kat.KatError, match="no parts with Block = CH "):
        kat.stamp(sch, "RC", "/CH", at=(50.8, 50.8), design=net)
    res = kat.stamp(sch, "RC", "/CH_A", at=(50.8, 50.8), design=net)
    assert "J1" in res["instances"][0]["warnings"][0]               # sync put J1 there
    with kat.edit(sch) as s:
        s.move("J1", to=(160.02, 101.6))
    res = kat.stamp(sch, "RC", "all", at=(50.8, 50.8), step=(0, 40.64), design=net)
    # (CH_A may still cover CH_B's parts before those are stamped: only J1 matters here)
    assert not any("J1" in w for i in res["instances"] for w in i.get("warnings", []))
    assert [i["instance"] for i in res["instances"]] == ["/CH_A", "/CH_B"]
    assert res["check"]["erc_errors"] == 0
    c = kat.check(sch, net)
    assert c["netlist_vs_expected"]["match"], c["netlist_vs_expected"]
    # both instances drawn like the block: same relative placement as R1/C1 there
    src = {x.field("SKiDL Tag").split("/")[-1]: x.place for x in Schematic(lib / "RC" / "RC_block.kicad_sch").symbols()
           if (x.field("Block") or "") == "DUT"}
    got = Schematic(sch)
    for ch in ("CH_A", "CH_B"):
        tgt = {x.field("SKiDL Tag").split("/")[-1]: x.place for x in got.symbols()
               if (x.field("Block") or "") == ch}
        assert (tgt["r"].x - tgt["c"].x, tgt["r"].y - tgt["c"].y) == pytest.approx(
            (src["r"].x - src["c"].x, src["r"].y - src["c"].y))
        assert (tgt["r"].rot, tgt["c"].rot) == (src["r"].rot, src["c"].rot)
    texts = [str(t[1]) for t in got.tree if isinstance(t, list) and str(t[0]) == "text"]
    assert "CH_A (rc)" in texts and "CH_B (rc)" in texts
    assert cli.main(["frame", str(sch), "CH_A", "--title", "Channel A"]) == 0      # replaces
    capsys.readouterr()
    texts = [str(t[1]) for t in Schematic(sch).tree if isinstance(t, list) and str(t[0]) == "text"]
    assert "Channel A" in texts and "CH_A (rc)" not in texts
    # stamping again changes nothing that matters and adds no duplicates
    n_labels = len(got.labels())
    kat.stamp(sch, "RC", ["/CH_A", "/CH_B"], at=(50.8, 50.8), step=(0, 40.64), design=net)
    assert len(Schematic(sch).labels()) == n_labels


def test_design_uses_the_block_and_stamps_it_by_name(lib, tmp_path, capsys):
    assert blocks.check("RC", require=())["ok"]
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    rec = json.loads((d / "design.blocks.json").read_text())
    assert rec["instances"]["CH_B"]["type"] == "rc" and rec["instances"]["CH_B"]["params"] == {"r_val": "3k3"}
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    capsys.readouterr()
    kat.sync(sch, d / "design.net")
    pcb = sch.with_suffix(".kicad_pcb")
    kat.pcb.sync(pcb, force=True)
    with kat.pcb.edit(pcb) as b:
        b.set_copper_layers(4)
        b.outline(rect=(100, 100, 40, 20))
        b.plane("GND", "In1.Cu")
        assert b.instances("RC") == ["/CH_A", "/CH_B"]
        res = b.stamp("RC", {"/CH_A": (110, 103), "/CH_B": (110, 111)})["instances"]
        b.footprint("J1").place(103, 104.92, 0)
    assert [r["footprints"] for r in res] == [2, 2] and "warnings" not in res[0]
    c = check(pcb)
    assert c["drc_errors"] == 0 and c["parity"] == 0 and c["unconnected"] == 4, c
    # the block changes: stamping warns until it is checked again
    rc_pcb = lib / "RC" / "RC.kicad_pcb"
    with kat.pcb.edit(rc_pcb) as b:
        b.footprint("R1").place(102, 101.5, 0)
    with kat.pcb.edit(pcb) as b:
        again = b.stamp("RC", "/CH_A", at=(110, 103))
    assert "changed since its last clean check (pcb)" in again["warnings"][0]
