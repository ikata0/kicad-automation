"""Hierarchical sheets built with kat: sheet boxes, ports, parameters, instance tree and
annotation, checked with KiCad's own netlist and ERC.

The spike's design: top -> 2x BLOCK1 (CH_A, CH_B) -> 3x RC each (6 RC instances). The RC
block is its own project in blocks/RC/, used by BLOCK1 through a relative path.
"""

import json
import shutil
from pathlib import Path

import pytest

import kat
from kat import checks, cli, hierarchy
from kat.pcb.syncer import read_design

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")

R_FP, C_FP = "Resistor_SMD:R_0603_1608Metric", "Capacitor_SMD:C_0603_1608Metric"


def new(path: Path) -> Path:
    assert cli.main(["new", str(path)]) == 0
    return path


def set_vars(sch: Path, **values) -> None:
    pro = sch.with_suffix(".kicad_pro")
    d = json.loads(pro.read_text())
    d["text_variables"] = values
    pro.write_text(json.dumps(d, indent=2))


def build(root: Path) -> dict:
    """top/top.kicad_sch, top/BLOCK1.kicad_sch, blocks/RC/RC.kicad_sch (own project)."""
    rc = new(root / "blocks" / "RC" / "RC.kicad_sch")
    set_vars(rc, R_VAL="10k")
    with kat.edit(rc) as s:
        s.add("Device:R", 101.6, 50.8, rot=90, value="${R_VAL}", footprint=R_FP)
        s.add("Device:C", 111.76, 55.88, value="100n", footprint=C_FP)
        s.port("IN", (91.44, 50.8), angle=180, shape="input")
        s.wire((91.44, 50.8), "R1.1")
        s.connect("R1.2", "C1.1")
        s.port("OUT", (121.92, 50.8), angle=0, shape="output")
        s.wire(s.point("C1.1"), (111.76, 50.8), (121.92, 50.8))
        s.power("GND", "C1.2", stub=(0, 2.54))
    top = new(root / "top" / "top.kicad_sch")
    set_vars(top, RA="1k")
    b1 = root / "top" / "BLOCK1.kicad_sch"
    with kat.edit(top) as s:
        s.add("Connector_Generic:Conn_01x05", 50.8, 76.2, mirror="y", value="Conn",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical")
        for i, ch in enumerate("AB"):
            y = 60.96 + 30.48 * i
            s.sheet(f"CH_{ch}", "BLOCK1.kicad_sch", (91.44, y), size=(20.32, 10.16),
                    left={"IN": "input"}, right={"OUT": "output"},
                    fields={"RA": "1k" if ch == "A" else "3k3"})
            s.label(f"IN_{ch}", f"CH_{ch}.IN", stub=(-5.08, 0))
            s.label(f"OUT_{ch}", f"CH_{ch}.OUT", stub=(5.08, 0))
            s.label(f"IN_{ch}", f"J1.{2 * i + 1}", stub=(7.62, 0))
            s.label(f"OUT_{ch}", f"J1.{2 * i + 2}", stub=(7.62, 0))
        s.power("GND", "J1.5", stub=(5.08, 0), rot=90)
        s.power("PWR_FLAG", "J1.5", stub=(5.08, 0))
    with kat.edit(b1) as s:
        rc_rel = "../blocks/RC/RC.kicad_sch"
        s.port("IN", (60.96, 60.96), angle=180, shape="input")
        prev = (60.96, 60.96)
        for k, val in enumerate(("${RA}", "2k2", "4k7")):
            name = f"RC{k + 1}"
            s.sheet(name, rc_rel, (71.12 + 30.48 * k, 58.42), size=(15.24, 7.62),
                    left={"IN": "input"}, right={"OUT": "output"}, fields={"R_VAL": val})
            s.connect(prev, f"{name}.IN")
            prev = s.point(f"{name}.OUT")
        s.add("Device:R", 170.18, 60.96, rot=90, value="100", footprint=R_FP)
        s.connect(prev, "R1.1")
        s.port("OUT", (180.34, 60.96), angle=0, shape="output")
        s.connect("R1.2", (180.34, 60.96))
    return {"top": top, "block1": b1, "rc": rc}


@pytest.fixture
def design(tmp_path, monkeypatch):
    monkeypatch.setenv("KAT_REPORT", "0")
    files = build(tmp_path)
    files["annotated"] = hierarchy.annotate(files["top"])
    return files


def test_tree_lists_every_instance_with_inherited_parameters(design):
    t = hierarchy.tree(design["top"])
    assert [i["path"] for i in t] == ["/", "/CH_A", "/CH_A/RC1", "/CH_A/RC2", "/CH_A/RC3",
                                      "/CH_B", "/CH_B/RC1", "/CH_B/RC2", "/CH_B/RC3"]
    assert [i["page"] for i in t] == list(range(1, 10))
    ch_b_rc1 = t[6]
    assert ch_b_rc1["block"] == "RC" and ch_b_rc1["params"] == {"RA": "3k3", "R_VAL": "${RA}"}
    assert ch_b_rc1["uuids"].count("/") == 2
    assert hierarchy.instance(design["top"], "CH_B/RC1") == ch_b_rc1


def test_kicad_netlist_has_per_instance_references_and_values(design):
    parts, pin_net = read_design(design["top"])
    assert len(parts) == 15                    # J1 + 2 x (R_out + 3 x (R + C))
    assert parts["J101"].value == "Conn"
    assert parts["R201"].value == "100"        # BLOCK1's own R on page 2 (CH_A)
    assert parts["R301"].value == "1k"         # CH_A/RC1: ${R_VAL} = ${RA} = 1k
    assert parts["R701"].value == "3k3"        # CH_B/RC1: RA = 3k3 two levels up
    assert parts["R401"].value == "2k2" and parts["R501"].value == "4k7"
    # nets inside an instance carry its path; the top labels join the instances
    assert pin_net[("R301", "2")] == pin_net[("C301", "1")] == pin_net[("R401", "1")]
    assert pin_net[("R301", "2")] != pin_net[("R701", "2")]
    assert pin_net[("R301", "1")] == pin_net[("J101", "1")] == "/IN_A"
    assert pin_net[("R201", "2")] == "/OUT_A" and pin_net[("R601", "2")] == "/OUT_B"
    # the board footprint path: uuid path of the instance + symbol uuid
    inst = hierarchy.instance(design["top"], "/CH_B/RC1")
    assert parts["R701"].path.startswith(inst["uuids"] + "/")
    assert design["annotated"]["references"]["/CH_B/RC1"][parts["R701"].path.rsplit("/", 1)[1]] == "R701"


def test_erc_clean_and_the_block_keeps_its_own_numbering(design):
    erc = checks.erc(design["top"])
    assert erc["errors"] == 0, erc["violations"]
    # the block opened as its own project: its own references and its default value
    hierarchy.annotate(design["rc"], per_page=0)
    parts, _ = read_design(design["rc"])
    assert sorted(parts) == ["C1", "R1"] and parts["R1"].value == "10k"
    # and the design still sees its own references after that
    parts, _ = read_design(design["top"])
    assert parts["R701"].value == "3k3"


def test_sheet_pins_and_refusals(design):
    s = kat.load(design["top"])
    names = [sh["name"] for sh in s.sheets()]
    assert names == ["CH_A", "CH_B"]
    assert s.point("CH_A.IN") == (91.44, 63.5)
    with pytest.raises(kat.KatError, match="no pin"):
        s.point("CH_A.NOPE")
    with pytest.raises(kat.KatError, match="exists"):
        s.sheet("CH_A", "BLOCK1.kicad_sch", (0, 0))
