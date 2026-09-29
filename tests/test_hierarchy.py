"""Hierarchical sheets built with kat: sheet boxes, ports, parameters, instance tree and
annotation, checked with KiCad's own netlist and ERC.

The spike's design: top -> 2x BLOCK1 (CH_A, CH_B) -> 3x RC each (6 RC instances). The RC
block is its own project in blocks/RC/, used by BLOCK1 through a relative path.
"""

import json
import re
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
    """A block library and a design: blocks/RC/RC.kicad_sch and blocks/BLOCK1/BLOCK1.kicad_sch
    (each its own project) and top/top.kicad_sch."""
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
    b1 = new(root / "blocks" / "BLOCK1" / "BLOCK1.kicad_sch")
    set_vars(b1, RA="1k")
    with kat.edit(b1) as s:
        s.port("IN", (60.96, 60.96), angle=180, shape="input")
        prev = (60.96, 60.96)
        for k, val in enumerate(("${RA}", "2k2", "4k7")):
            name = f"RC{k + 1}"
            s.sheet(name, "../RC/RC.kicad_sch", (71.12 + 30.48 * k, 58.42), size=(15.24, 7.62),
                    left={"IN": "input"}, right={"OUT": "output"}, fields={"R_VAL": val})
            s.connect(prev, f"{name}.IN")
            prev = s.point(f"{name}.OUT")
        s.add("Device:R", 170.18, 60.96, rot=90, value="100", footprint=R_FP)
        s.connect(prev, "R1.1")
        s.port("OUT", (180.34, 60.96), angle=0, shape="output")
        s.connect("R1.2", (180.34, 60.96))
    top = new(root / "top" / "top.kicad_sch")
    set_vars(top, RA="1k")
    with kat.edit(top) as s:
        s.add("Connector_Generic:Conn_01x05", 50.8, 76.2, mirror="y", value="Conn",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical")
        for i, ch in enumerate("AB"):
            y = 60.96 + 30.48 * i
            s.sheet(f"CH_{ch}", "../blocks/BLOCK1/BLOCK1.kicad_sch", (91.44, y), size=(20.32, 10.16),
                    left={"IN": "input"}, right={"OUT": "output"},
                    fields={"RA": "1k" if ch == "A" else "3k3"})
            s.label(f"IN_{ch}", f"CH_{ch}.IN", stub=(-5.08, 0))
            s.label(f"OUT_{ch}", f"CH_{ch}.OUT", stub=(5.08, 0))
            s.label(f"IN_{ch}", f"J1.{2 * i + 1}", stub=(7.62, 0))
            s.label(f"OUT_{ch}", f"J1.{2 * i + 2}", stub=(7.62, 0))
        s.power("GND", "J1.5", stub=(5.08, 0), rot=90)
        s.power("PWR_FLAG", "J1.5", stub=(5.08, 0))
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
        s.sheet("CH_A", "../blocks/BLOCK1/BLOCK1.kicad_sch", (0, 0))


def test_moving_a_sheet_box_brings_its_wiring(design):
    before = read_design(design["top"])[1]
    uuid = next(sh["uuid"] for sh in kat.load(design["top"]).sheets() if sh["name"] == "CH_B")
    with kat.edit(design["top"]) as s:
        res = s.move("CH_B", by=(50.8, 0))
    assert res[0]["sheet"] == "CH_B" and res[0]["x"] == 142.24
    s = kat.load(design["top"])
    box = next(sh for sh in s.sheets() if sh["name"] == "CH_B")
    assert box["uuid"] == uuid                                 # board paths depend on it
    labels = {(l["text"], l["x"], l["y"]) for l in s.list()["labels"]}
    assert ("IN_B", 137.16, 93.98) in labels and ("OUT_B", 167.64, 93.98) in labels
    assert read_design(design["top"])[1] == before
    # a part wired straight to a box pin: the wire stays on the pin, it is dragged
    with kat.edit(design["block1"]) as s:
        s.move("R1", by=(0, 10.16))
    assert read_design(design["top"])[1] == before
    assert checks.erc(design["top"])["errors"] == 0


def test_a_name_both_part_and_box_must_be_told_apart(design):
    """B5: a part named like a sheet box is not moved silently in the box's place."""
    with kat.edit(design["top"]) as s:
        s.add("Device:R", 30.48, 30.48, ref="CH_B")
    with pytest.raises(kat.KatError, match="both a part and a sheet box"):
        with kat.edit(design["top"]) as s:
            s.move("CH_B", by=(0, 5.08))
    with kat.edit(design["top"]) as s:
        box = s.move("sheet:CH_B", by=(50.8, 0))
        part = s.move("part:CH_B", by=(0, 5.08))
    assert box[0]["sheet"] == "CH_B" and box[0]["x"] == 142.24
    assert part[0]["ref"] == "CH_B" and part[0]["y"] == 35.56


def test_snapshot_and_view_of_a_hierarchy(design, tmp_path):
    """I8: one PNG per sheet file (3 files, 9 pages); B14: tiles of each page in their
    own folder; B13: kat view shows the root page, not the first sub-sheet."""
    from kat import render, views
    top = design["top"]
    res = render.snapshot(top, tmp_path / "snap", zoom=1, tiles=True)
    assert len(res["svg"]) == 9 and len(res["images"]) == 3, res["images"]
    folders = {Path(t["png"]).parent.name for t in res["tiles"]}
    assert len(folders) == 3 and all(Path(t["png"]).exists() for t in res["tiles"])
    assert len(render.snapshot(top, tmp_path / "all", zoom=1, pages="instances")["images"]) == 9
    root = next(Path(p) for p in res["svg"] if Path(p).stem == top.stem)
    shown = views._svg(top)
    plain = render.export_svg(top, tmp_path / "plain", frame=False)
    def body(f):                          # the <title> line carries the export time
        return re.sub(r"<title>.*?</title>", "", f.read_text())
    assert body(shown) == body(next(p for p in plain if p.stem == top.stem))
    assert root.exists()


def test_sheet_box_refusals(design):
    with pytest.raises(kat.KatError, match="sheet pin CH_A.IN"):
        with kat.edit(design["top"]) as s:
            s.move("CH_B", to=(91.44, 60.96))
    with pytest.raises(kat.KatError, match="cannot rotate"):
        with kat.edit(design["top"]) as s:
            s.move("CH_B", by=(0, 0), rot=90)
    with pytest.raises(kat.KatError, match="too small"):
        with kat.edit(design["top"]) as s:
            s.sheet_box("CH_A", size=(20.32, 2.54))


def test_reshaping_a_box_moves_and_mirrors_its_pin_wiring(design):
    before = read_design(design["top"])[1]
    with kat.edit(design["top"]) as s:
        res = s.sheet_box("CH_A", size=(30.48, 12.7), left=["OUT"], right=["IN"])
    assert res["pins"] == {"OUT": {"x": 91.44, "y": 63.5}, "IN": {"x": 121.92, "y": 63.5}}
    s = kat.load(design["top"])
    box = next(sh for sh in s.sheets() if sh["name"] == "CH_A")
    assert box["size"] == [30.48, 12.7]
    labels = {l["text"]: l for l in s.list()["labels"] if l["y"] == 63.5}
    # both pins changed sides: stub and label mirrored (the fixture's labels have angle 0)
    assert (labels["IN_A"]["x"], labels["IN_A"]["angle"]) == (127.0, 180)
    assert (labels["OUT_A"]["x"], labels["OUT_A"]["angle"]) == (86.36, 180)
    assert read_design(design["top"])[1] == before
    assert checks.erc(design["top"])["errors"] == 0


def test_pin_sides_follow_the_child_drawing():
    ports = {"a": {"x": 10, "y": 5, "angle": 180, "shape": "passive"},     # drawn at the left edge
             "b": {"x": 90, "y": 9, "angle": 0, "shape": "input"},         # drawn at the right edge
             "out": {"x": 50, "y": 1, "angle": 90, "shape": "passive"},
             "clk": {"x": 50, "y": 2, "angle": 270, "shape": "passive"},
             "q": {"x": 50, "y": 3, "angle": 90, "shape": "output"}}
    left, right = hierarchy.pin_sides(ports)
    assert list(left) == ["clk", "a"] and list(right) == ["out", "q", "b"]


def test_pins_added_and_removed_in_place(design):
    uuid = next(sh["uuid"] for sh in kat.load(design["top"]).sheets() if sh["name"] == "CH_B")
    with kat.edit(design["top"]) as s:
        hierarchy.set_box(s.schematic, "CH_B", add={"EN": ("input", "left")}, remove=["OUT"])
    s = kat.load(design["top"])
    box = next(sh for sh in s.sheets() if sh["name"] == "CH_B")
    assert box["uuid"] == uuid and set(box["pins"]) == {"IN", "EN"}
    assert box["pins"]["EN"]["x"] == box["at"][0] and box["pins"]["EN"]["y"] == pytest.approx(box["pins"]["IN"]["y"] + 2.54)
    # the removed pin's stub and label went with it; the connector's OUT_B label stays
    assert [(l["x"], l["y"]) for l in s.list()["labels"] if l["text"] == "OUT_B"] == [(63.5, 78.74)]
