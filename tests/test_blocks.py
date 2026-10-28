"""The block library: new -> build -> layout -> check, used by a design (SKiDL function) and
stamped onto its board by name."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import blocks, cli

sys.path.insert(0, str(Path(__file__).parent))
from test_pcb_stamp import check, env_board, layout_rc  # noqa: E402


RC_PY = '''"""Block RC: RC low-pass. Ports: inp, out, gnd. Parameters: r_val."""
import os
import sys

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
    assert "flat" not in made["files"] and "py" in made["files"]         # B18: only files that exist
    (tmp_path / "blocks" / "RC" / "RC.py").write_text(RC_PY)
    built = run(capsys, "block", "build", "RC", "--force")
    assert built["type"] == "rc" and built["ports"] == ["inp", "out"] and built["power"] == ["gnd"]
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


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_new_refuses_existing_and_bad_names(lib, capsys):
    assert cli.main(["block", "new", "RC"]) == 2
    assert "exists" in capsys.readouterr().out
    with pytest.raises(kat.KatError, match="identifier"):
        blocks.new("1RC")


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
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


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_flat_sheet_gets_the_block_drawing(lib, tmp_path, capsys):
    from kat import sch_room
    from kat.schematic import Schematic
    sch = design(tmp_path, capsys)
    net = tmp_path / "design" / "design.net"
    with pytest.raises(kat.KatError, match="no parts with Block = CH "):
        kat.stamp(sch, "RC", "/CH", at=(50.8, 50.8), design=net)
    before = sch.read_text()
    with pytest.raises(kat.KatError, match="holds J1: pass make_room"):     # sync put J1 there
        kat.stamp(sch, "RC", "/CH_A", at=(50.8, 50.8), design=net)
    assert sch.read_text() == before
    plan = kat.stamp(sch, "RC", "all", at=(50.8, 50.8), step=(0, 40.64), design=net, plan=True)
    assert not plan["free"] and plan["plan"][0]["in_the_way"] == ["J1"]    # CH_B's parts are no obstacle
    assert sch.read_text() == before
    res = kat.stamp(sch, "RC", "all", at=(50.8, 50.8), step=(0, 40.64), design=net, make_room=True)
    assert [m["what"] for m in res["instances"][0]["made_room"]] == ["J1"]
    assert [i["instance"] for i in res["instances"]] == ["/CH_A", "/CH_B"]
    j1 = next(x for x in Schematic(sch).symbols() if x.ref == "J1")
    assert all(not sch_room.hits(Schematic(sch).body_box(j1), tuple(i["area"])) for i in res["instances"])
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


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
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
    with kat.pcb.edit(rc_pcb) as b:          # saved unchanged: still clean (I9, content not time)
        b.footprint("R1").place(102, 101.5, 0)
    with kat.pcb.edit(pcb) as b:
        assert "warnings" not in b.stamp("RC", "/CH_A", at=(110, 103))
    with kat.pcb.edit(rc_pcb) as b:
        b.footprint("R1").place(102, 101.0, 0)
    with kat.pcb.edit(pcb) as b:
        again = b.stamp("RC", "/CH_A", at=(110, 103))
    assert "changed since its last clean check (pcb)" in again["warnings"][0]
    # B4: the block routed together with its harness connector: none of the harness's
    # route is stamped, not even the part that ends at the block's R1
    tracks = again["tracks"]
    with kat.pcb.edit(rc_pcb) as b:
        j = b.footprint("J1").pad("1")
        r = b.footprint("R1").pad("1")
        b.track(["J1.1", (j.x, 99.6), (r.x, 99.6), "R1.1"], layer="F.Cu")
    with kat.pcb.edit(pcb) as b:
        routed = b.stamp("RC", "/CH_A", at=(110, 103))
    assert routed["tracks"] == tracks and routed["skipped_environment"] >= 3, routed


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_port_points_are_checked_and_stamped(lib, tmp_path, capsys):
    """I1: a via at the block's edge marks where port inp leaves it; block check checks
    it, the stamp reports it on the design; D4: the harness needs no MPN."""
    rc_pcb = lib / "RC" / "RC.kicad_pcb"
    with kat.pcb.edit(rc_pcb) as b:
        net = b.footprint("R1").pad("1").net
        b.via((100.8, 101.5), net=net)
        b.track(["R1.1", (100.8, 101.5)])
    assert kat.pcb.block_port(rc_pcb, "inp", (100.8, 101.5))["net"] == net
    with pytest.raises(kat.KatError, match="no via or pad"):
        kat.pcb.block_port(rc_pcb, "out", (107, 107))
    res = blocks.check("RC", require=("MPN",))
    assert "port_problems" not in res and not any(e["ref"] == "J1" for e in res["gate_errors"]), res
    with kat.pcb.edit(rc_pcb) as b:
        b.via((106.0, 105.0), net=net)
    kat.pcb.block_port(rc_pcb, "inp", (106.0, 105.0))
    assert "inside the outline's edge" in blocks.check("RC", require=())["port_problems"][0]
    kat.pcb.block_port(rc_pcb, "inp", (100.8, 101.5))

    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    sch = d / "top" / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    capsys.readouterr()
    kat.sync(sch, d / "design.net")
    pcb = sch.with_suffix(".kicad_pcb")
    kat.pcb.sync(pcb, force=True)
    with kat.pcb.edit(pcb) as b:
        b.set_copper_layers(4)
        b.outline(rect=(100, 100, 40, 20))
        got = b.stamp("RC", "/CH_B", at=(110, 111), rot=180)
    assert got["ports"]["inp"]["net"] == "IN_B" or got["ports"]["inp"]["net"].endswith("IN_B"), got["ports"]
    # 180 deg about the block origin (100, 100) -> (110, 111): (100.8, 101.5) -> (109.2, 109.5)
    assert (got["ports"]["inp"]["x"], got["ports"]["inp"]["y"]) == (109.2, 109.5)
    assert kat.pcb.Board(pcb).ports("/CH_B") == got["ports"]


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_a_block_board_is_routed_without_its_harness(lib, tmp_path):
    """The harness J1 joins no net in the router's design (its routes would not be
    stamped); it stays an obstacle."""
    from kat.pcb import Board, specctra
    from kat.blocks import _env_refs
    rc = lib / "RC" / "RC.kicad_pcb"
    assert _env_refs(rc.parent) == {"J1"}
    specctra.write_dsn(Board(rc), tmp_path / "rc.dsn", skip_pins=_env_refs(rc.parent))
    dsn = (tmp_path / "rc.dsn").read_text()
    network = dsn[dsn.index("(network"):]
    assert "J1-" not in network and "img_J1" in dsn


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_stamp_finds_free_areas_itself(lib, tmp_path, capsys):
    from kat import sch_room
    from kat.schematic import Schematic
    sch = design(tmp_path, capsys)
    net = tmp_path / "design" / "design.net"
    assert cli.main(["stamp", str(sch), "RC", "all", "--expect", str(net), "--plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["free"] and len(plan["plan"]) == 2
    res = kat.stamp(sch, "RC", "all", design=net)
    a, b = (tuple(i["area"]) for i in res["instances"])
    assert not sch_room.hits(a, b) and not any(i.get("made_room") for i in res["instances"])
    s = Schematic(sch)
    room = sch_room.Room(s, {x.ref for x in s.symbols() if x.field("Block")}, {"CH_A", "CH_B"})
    assert not room.occupants(a) and not room.occupants(b)     # J1 was left alone, and is outside
    assert res["check"]["clean"] and res["check"]["erc_errors"] == 0


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_stamp_without_a_free_area_makes_room_in_place(lib, tmp_path, capsys, monkeypatch):
    """B8: no free area anywhere: with make_room the instance is stamped where it stands;
    without, the error's hint fits the page (no "kat page A3" on an A0 page)."""
    from kat import sch_room
    from kat.schematic import Schematic
    sch = design(tmp_path, capsys)
    net = tmp_path / "design" / "design.net"

    def full(*a, **k):
        raise kat.KatError("no free area of 1 x 1 mm on x: " + sch_room.bigger_page(Schematic(sch)))
    monkeypatch.setattr(sch_room, "free_spot", full)
    with pytest.raises(kat.KatError, match="kat page A3"):
        kat.stamp(sch, "RC", "/CH_A", design=net)
    res = kat.stamp(sch, "RC", "/CH_A", design=net, make_room=True)
    assert res["check"]["erc_errors"] == 0
    with kat.edit(sch) as e:
        e.page("A0")
    with pytest.raises(kat.KatError, match="largest"):
        kat.stamp(sch, "RC", "/CH_B", design=net)


@pytest.mark.requires("kicad", "symbols")
def test_contacts_with_ignored_parts_are_allowed(tmp_path):
    """B7: a batch stamp moves parts of instances that are detached and about to move
    themselves: their pins are no contacts."""
    sch = tmp_path / "c" / "c.kicad_sch"
    kat.new(sch)
    with kat.edit(sch) as e:
        e.add("Device:R", 100, 80, ref="R1")
        e.add("Device:R", 120, 80, ref="R2")
        s = e.schematic
        p1, p2 = ({p["number"]: (p["x"], p["y"]) for p in s.pins(s.find_symbols(r)[0])} for r in ("R1", "R2"))
        d = (p2["1"][0] - p1["1"][0], p2["1"][1] - p1["1"][1])
        with pytest.raises(kat.KatError, match="R2.1"):
            s.move_symbols([dict(ref="R1", dx=d[0], dy=d[1])])
        s.move_symbols([dict(ref="R1", dx=d[0], dy=d[1])], ignore={"R2"})


@pytest.mark.requires("kicad", "symbols", "footprints", "skidl")
def test_bottom_side_needs_its_own_check_and_mirrors_the_ports(lib, tmp_path, capsys):
    """`block check --sides both` checks the block turned over (DRC, port points);
    `stamp --side bottom` warns until then; port points are mirrored with the block."""
    rc_pcb = lib / "RC" / "RC.kicad_pcb"
    with kat.pcb.edit(rc_pcb) as b:
        net = b.footprint("R1").pad("1").net
        b.via((100.8, 101.5), net=net)
        b.track(["R1.1", (100.8, 101.5)])
    kat.pcb.block_port(rc_pcb, "inp", (100.8, 101.5))
    assert "bottom" not in blocks.check("RC", require=())
    d = tmp_path / "design"
    d.mkdir()
    (d / "design.py").write_text(DESIGN_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
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
        got = b.stamp("RC", "/CH_B", at=(110, 109), side="bottom")
    assert "not checked on the bottom side" in got["warnings"][0]
    # mirrored about the block's vertical axis (outline 100..114): x -> 110 + (114 - 100.8)
    assert (got["ports"]["inp"]["x"], got["ports"]["inp"]["y"]) == (123.2, 110.5)
    res = run(capsys, "block", "check", "RC", "--no-require", "--sides", "both")
    assert res["ok"] and res["bottom"]["ok"], res["bottom"]
    assert res["bottom"]["layer_map"] == {"F.Cu": "B.Cu", "In1.Cu": "In2.Cu", "In2.Cu": "In1.Cu", "B.Cu": "F.Cu"}
    with kat.pcb.edit(pcb) as b:
        again = b.stamp("RC", "/CH_B", at=(110, 109))
    assert again["side"] == "bottom" and "warnings" not in again, again
    c = check(pcb)
    assert c["drc_errors"] == 0 and c["parity"] == 0, c
