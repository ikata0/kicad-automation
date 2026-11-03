"""A computed pin assignment through a real SKiDL script: the table changes, the script does not."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import cli
from kat.pcb.board import Board

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints", "skidl")

DESIGN = '''import kat
from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool
set_default_tool(KICAD10)
j = Part("Connector_Generic", "Conn_01x04", ref="J1", tag="j", value="HDR", fields={"MPN": "X"},
         footprint="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
kat.skidl_blocks.connect_pins(j, "j1.csv")
for name, ref in (("CH1", "R1"), ("CH2", "R2")):
    r = Part("Device", "R", value="1k", ref=ref, tag=ref, fields={"MPN": "Y"},
             footprint="Resistor_SMD:R_0603_1608Metric")
    Net.fetch(name) += r[1]
    Net.fetch("GND") += r[2]
generate_netlist(file_="design.net")
kat.skidl_blocks.write("design.net")
'''.replace('Net.fetch(name) += r[1]', 'r[1] += Net.fetch(name)').replace('Net.fetch("GND") += r[2]', 'r[2] += Net.fetch("GND")')
TABLE = """# from the mechanical drawing
ref,pin,net,note
J1,1,CH1,left
J1,2,CH2,right
J1,3,GND,
J1,4,,spare
"""


@pytest.fixture
def design(tmp_path):
    d = tmp_path / "top"
    sch = d / "top.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    (d / "design.py").write_text(DESIGN)
    (d / "j1.csv").write_text(TABLE)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True, text=True)
    rep = kat.sync(sch, d / "design.net")
    assert rep["check"]["ok"], rep["check"]
    pcb = sch.with_suffix(".kicad_pcb")
    kat.pcb.sync(pcb, force=True)
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 40, 25))
        b.move_many({"J1": {"to": (5, 8)}, "R1": {"to": (20, 8)}, "R2": {"to": (20, 15)}})
    return d, sch, pcb


def state(d, sch, pcb):
    return {f: f.read_bytes() for f in (d / "j1.csv", d / "design.net", sch, pcb)}


def pad_nets(pcb, ref="J1"):
    return {p.number: p.net for p in Board(pcb).footprint(ref).pads()}


def test_a_swap_reaches_design_sheet_and_board(design):
    d, sch, pcb = design
    script = (d / "design.py").read_text()
    where = {f.ref: (f.x, f.y) for f in Board(pcb).footprints()}
    with kat.pcb.edit(pcb) as b:
        b.track(["J1.1", (10, 5.46)], width=0.2) if False else None
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH2", "J1.2": "CH1"}, d / "design.py", sch, pcb=pcb, netlist_file=d / "design.net",
                                  force=True)
    assert r["ok"] and r["changed"] and r["moved"] == []
    assert {c["pin"]: (c["from"], c["to"]) for c in r["changes"]} == {"J1.1": ("CH1", "CH2"), "J1.2": ("CH2", "CH1")}
    assert r["check"] == {"netlist": True, "sheet": True, "board": True} and r["board"]["parity"] == 0
    # the script is as it was; the table keeps its comment, note column and order
    assert (d / "design.py").read_text() == script
    assert (d / "j1.csv").read_text().splitlines() == [
        "# from the mechanical drawing", "ref,pin,net,note", "J1,1,CH2,left", "J1,2,CH1,right", "J1,3,GND,", "J1,4,,spare"]
    # KiCad's own netlist and the board's pads have it
    nets = kat.nets(sch)
    assert ("J1", "1") in next(v for k, v in nets.items() if k.endswith("CH2"))
    assert ("J1", "2") in next(v for k, v in nets.items() if k.endswith("CH1"))
    pads = pad_nets(pcb)
    assert pads["1"].endswith("CH2") and pads["2"].endswith("CH1") and pads["3"].endswith("GND")
    assert {f.ref: (f.x, f.y) for f in Board(pcb).footprints()} == where, "placement stays"
    assert kat.pcb.drc(pcb)["parity"] == []
    assert kat.check(sch, d / "design.net")["netlist_vs_expected"]["match"]
    assert kat.mapping.check(sch, d / "j1.csv")["ok"] and kat.mapping.check(pcb, d / "j1.csv")["ok"]


def test_copper_that_no_longer_fits_is_reported_and_removed_on_request(design):
    d, sch, pcb = design
    with kat.pcb.edit(pcb) as b:
        b.track(["J1.1", "R1.1"])
    tracks = len(Board(pcb).tracks())
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH2", "J1.2": "CH1"}, d / "design.py", sch, d / "design.net", pcb, force=True)
    assert r["board"]["stale_tracks"], r["board"]
    assert len(Board(pcb).tracks()) == tracks
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH1", "J1.2": "CH2"}, d / "design.py", sch, d / "design.net", pcb, force=True)
    assert r["ok"]
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH2", "J1.2": "CH1"}, d / "design.py", sch, d / "design.net", pcb, force=True, rip_stale=True)
    assert all(t.get("removed") for t in r["board"]["stale_tracks"]) and len(Board(pcb).tracks()) < tracks


def test_nets_that_leave_a_part_are_named(design):
    d, sch, _ = design
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.4": "CH1", "J1.1": ""}, d / "design.py", sch, d / "design.net")
    assert r["ok"] and r["moved"] == []
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.4": "GND", "J1.2": ""}, d / "design.py", sch, d / "design.net")
    assert r["moved"] == [{"ref": "J1", "left": ["CH1", "CH2"], "came": []}] and r["ok"]


def test_a_new_pin_joins_the_table_and_an_unconnected_pin_gets_a_flag(design):
    d, sch, _ = design
    (d / "j1.csv").write_text(TABLE.replace("J1,4,,spare\n", ""))
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.4": "GND"}, d / "design.py", sch, d / "design.net")
    assert r["ok"] and r["changes"] == [{"pin": "J1.4", "from": None, "to": "GND"}]
    assert (d / "j1.csv").read_text().splitlines()[-1] == "J1,4,GND,"
    assert ("J1", "4") in kat.nets(sch)["GND"]
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.4": ""}, d / "design.py", sch, d / "design.net")
    assert r["ok"] and ("J1", "4") not in kat.nets(sch)["GND"]
    assert kat.check(sch, d / "design.net")["erc"]["errors"] == 0


def test_dry_run_and_no_change_touch_nothing(design):
    d, sch, pcb = design
    before = state(d, sch, pcb)
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH2"}, d / "design.py", sch, d / "design.net", pcb, dry_run=True)
    assert r["dry_run"] and not r["changed"] and r["changes"][0]["to"] == "CH2"
    assert r["moved"] == [{"ref": "J1", "left": ["CH1"], "came": []}]
    r = kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH1"}, d / "design.py", sch, d / "design.net", pcb)
    assert r["ok"] and not r["changed"] and r["changes"] == []
    assert state(d, sch, pcb) == before


CONNECT = 'kat.skidl_blocks.connect_pins(j, "j1.csv")'


@pytest.mark.parametrize("script, match", [
    ("raise SystemExit('broken design')", "put back: design.py failed: broken design"),
    # no netlist of its own: the old one stays, with the old pins
    ("pass", "does design.py read it"),
    (DESIGN.replace(CONNECT, 'j[1] += Net.fetch("CH1"); j[2] += Net.fetch("CH2"); j[3] += Net.fetch("GND")'), "does design.py read it"),
    (DESIGN.replace(CONNECT, 'kat.skidl_blocks.connect_pins(j, "j1.csv", ref="J9")'), "no row for J9"),
])
def test_a_failing_step_puts_everything_back(design, script, match):
    d, sch, pcb = design
    (d / "design.py").write_text(script)
    before = state(d, sch, pcb)
    with pytest.raises(kat.KatError, match=match):
        kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH2", "J1.2": "CH1"}, d / "design.py", sch, d / "design.net", pcb, force=True)
    assert state(d, sch, pcb) == before


def test_a_pin_the_part_lacks_puts_everything_back(design):
    d, sch, pcb = design
    before = state(d, sch, pcb)
    with pytest.raises(kat.KatError, match="put back"):
        kat.mapping.back_annotate(d / "j1.csv", {"J1.9": "CH1"}, d / "design.py", sch, d / "design.net", pcb, force=True)
    assert state(d, sch, pcb) == before


def test_a_board_that_is_not_ready_puts_the_sheet_back_too(design):
    d, sch, pcb = design
    before = state(d, sch, pcb)
    with pytest.raises(kat.KatError, match="put back: schematic not ready"):
        kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "CH2", "J1.2": "CH1"}, d / "design.py", sch, d / "design.net", pcb,
                                  require=("NoSuchField",))
    assert state(d, sch, pcb) == before


def test_bad_arguments(design):
    d, sch, _ = design
    with pytest.raises(kat.KatError, match="empty"):
        kat.mapping.back_annotate(d / "j1.csv", {}, d / "design.py", sch)
    with pytest.raises(kat.KatError, match="not a pin"):
        kat.mapping.back_annotate(d / "j1.csv", {"J1": "A"}, d / "design.py", sch)
    with pytest.raises(kat.KatError, match="does not exist"):
        kat.mapping.back_annotate(d / "j1.csv", {"J1.1": "A"}, d / "none.py", sch)
    chain = d / "c.csv"
    chain.write_text("source,channel,destination\nJ1.1,0,R1.1\n")
    with pytest.raises(kat.KatError, match="one pin and a net"):
        kat.mapping.back_annotate(chain, {"J1.1": "A"}, d / "design.py", sch)


def test_an_assignment_from_a_table_and_the_cli(design, capsys):
    d, sch, pcb = design
    new = d / "computed.csv"
    new.write_text("pin,net\nJ1.1,CH2\nJ1.2,CH1\n")
    base = ["back-annotate", str(d / "j1.csv"), "--run", str(d / "design.py"), "--netlist", str(d / "design.net"), "--sch", str(sch)]
    capsys.readouterr()
    assert cli.main(base + ["--from", str(new), "--dry-run"]) == 0
    assert len(json.loads(capsys.readouterr().out)["changes"]) == 2
    assert cli.main(base + ["--from", str(new), "--pcb", str(pcb), "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["check"]["board"] is True
    assert cli.main(base) == 2
    assert "give the assignment" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(base + ["--assign", "J1.1"]) == 2
    assert "PIN=NET" in json.loads(capsys.readouterr().out)["error"]
    assert cli.main(base + ["--assign", "J1.1=CH1", "J1.2=CH2"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is True


@pytest.mark.parametrize("name, text, want", [
    ("a.csv", "pin;net;x\nJ1.1;A;keep\n", "pin;net;x\nJ1.1;B;keep\n"),
    ("b.csv", 'ref,pin,net,note\nJ1,1,A,"a, b"\n', 'ref,pin,net,note\nJ1,1,B,"a, b"\n'),
])
def test_update_keeps_the_form_of_a_table(tmp_path, name, text, want):
    f = tmp_path / name
    f.write_text(text)
    assert kat.mapping.update(f, {"J1.1": "B"}) == [{"pin": "J1.1", "from": "A", "to": "B"}]
    assert f.read_text() == want
    assert kat.mapping.update(f, {"J1.1": "B"}) == []


def test_update_json_forms(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"J1.1": "A"}))
    kat.mapping.update(f, {"J1.1": "B", "J1.2": "C"})
    assert json.loads(f.read_text()) == {"J1.1": "B", "J1.2": "C"}
    g = tmp_path / "b.json"
    g.write_text(json.dumps([{"ref": "J1", "pin": "1", "net": "A", "x": 1}]))
    kat.mapping.update(g, {"J1.1": "B", "J1.2": "C"})
    assert json.loads(g.read_text()) == [{"ref": "J1", "pin": "1", "net": "B", "x": 1}, {"ref": "J1", "pin": "2", "net": "C"}]
    assert kat.mapping.pin_nets(g, "J1") == {"1": "B", "2": "C"}


def test_a_table_from_a_legacy_board_seeds_a_design(design, tmp_path):
    """Pins read from an ODB++ export of this board go straight into a script."""
    from kat import env
    d, sch, pcb = design
    job = tmp_path / "job.zip"
    env.run_cli("pcb", "export", "odb", "-o", str(job), str(pcb))
    out = tmp_path / "old.csv"
    kat.pcb.odb_pinout(job, refs=["J1"], out=out)
    pins = kat.skidl_blocks.pin_map(out, "J1")
    assert {k: v.rsplit("/", 1)[-1] for k, v in pins.items()} == {"1": "CH1", "2": "CH2", "3": "GND", "4": ""}
    assert Path(out).read_text().startswith("ref,pin,net")


def test_pin_map_reads_all_or_one_part(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text("pin,net\nJ1.1,A\nJ2.1,\n")
    assert kat.skidl_blocks.pin_map(f) == {"J1.1": "A", "J2.1": ""}
    assert kat.skidl_blocks.pin_map(f, "J1") == {"1": "A"} and kat.skidl_blocks.pin_map(f, "J9") == {}
