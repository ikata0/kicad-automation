"""Assembly variants (FR-063): declared in the SKiDL script, written by kat sync, exported by
KiCad. One hierarchical design built once: two CH channels (three RC stages each) and a
top-level connector."""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import blocks, env, hierarchy, variants

sys.path.insert(0, str(Path(__file__).parent))
from test_blocks import RC_PY  # noqa: E402
from test_sync_hier import CH_PY, TOP_PY  # noqa: E402

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints", "skidl")

VARIANTS = '''
import kat.skidl_variants as kv
kv.variant("LITE", "channel B unpopulated")
kv.override(j, "LITE", MPN="J-LITE")                          # a Part; MPN is not on the base symbol
kv.override("c", "LITE", block="CH_B/RC*", dnp=True)          # every RC stage of CH_B
kv.override("CH_A/RC2/r", "LITE", Value="2k7", in_bom=False)  # one instance
kv.param("/CH_B", "LITE", "ra", "6k8")                        # CH_B/RC1/r follows
kv.variant("FULL")
kv.write("design.net")
'''


def bom(sch: Path, variant: str | None = None) -> dict[str, dict]:
    out = sch.parent / f"bom-{variant or 'base'}.csv"
    env.run_cli("sch", "export", "bom", "--fields", "Reference,Value,MPN,${DNP}", "--labels", "Ref,Value,MPN,DNP",
                "--group-by", "", *(["--variant", variant] if variant else []), "-o", str(out), str(sch))
    with open(out, newline="", encoding="utf-8") as f:
        return {r["Ref"]: r for r in csv.DictReader(f)}


@pytest.fixture(scope="module")
def design(tmp_path_factory):
    root = tmp_path_factory.mktemp("variants")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("KAT_REPORT", "0")
        mp.chdir(root)
        blocks.new("RC")
        (root / "blocks" / "RC" / "RC.py").write_text(RC_PY)
        blocks.build("RC", force=True)
        blocks.new("CH")
        (root / "blocks" / "CH" / "CH.py").write_text(CH_PY)
        blocks.build("CH", force=True)
        d = root / "design"
        d.mkdir()
        (d / "design.py").write_text(TOP_PY + VARIANTS)
        subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
        sch = d / "top" / "top.kicad_sch"
        kat.cli.main(["new", str(sch)])
        (d / "design.variants.json").rename(d / "later.json")       # first without variants
        kat.sync(sch, d / "design.net", sheets=["ch", "rc"])
        base_before = bom(sch)
        (d / "later.json").rename(d / "design.variants.json")
        rep = kat.sync(sch, d / "design.net", sheets=["ch", "rc"])
        comps, _ = kat.netlist.read_design(d / "design.net")
        yield {"sch": sch, "net": d / "design.net", "rep": rep, "base_before": base_before,
               "ref": {c.tag: c.ref for c in comps.values()}, "mp": mp}


def test_sync_writes_variants_that_kicad_exports(design):
    sch, ref, rep = design["sch"], design["ref"], design["rep"]
    assert rep["check"]["ok"], rep["check"]
    assert rep["variants"]["names"] == ["LITE", "FULL"]
    assert {"ref": ref["j"], "field": "MPN"} in rep["variants"]["fields_added"]
    pro = json.loads(sch.with_suffix(".kicad_pro").read_text())
    assert pro["schematic"]["variants"] == [{"name": "LITE", "description": "channel B unpopulated"},
                                            {"name": "FULL"}]
    assert bom(sch) == design["base_before"]                        # the base is unchanged
    lite = bom(sch, "LITE")
    assert lite[ref["j"]]["MPN"] == "J-LITE"
    for stage in ("RC1", "RC2", "RC3"):
        assert lite[ref[f"CH_B/{stage}/c"]]["DNP"] and not lite[ref[f"CH_A/{stage}/c"]]["DNP"]
    assert ref["CH_A/RC2/r"] not in lite and ref["CH_B/RC2/r"] in lite      # in_bom on one instance
    assert lite[ref["CH_B/RC1/r"]]["Value"] == "6k8"                       # the parameter, base 3k3
    assert lite[ref["CH_A/RC1/r"]]["Value"] == "1k"
    assert bom(sch, "FULL") == bom(sch)


def test_a_second_sync_changes_nothing_and_check_sees_edits(design):
    sch, net = design["sch"], design["net"]
    lite = bom(sch, "LITE")
    rep = kat.sync(sch, net, sheets=["ch", "rc"])
    assert not any(rep["variants"][k] for k in ("added", "changed", "removed", "fields_added")), rep["variants"]
    assert bom(sch, "LITE") == lite
    # an override lost outside kat: the check names it, the next sync puts it back
    rc = next(Path(i["file"]) for i in hierarchy.tree(sch) if i["path"] == "/CH_A/RC2")
    text = rc.read_text()
    rc.write_text(text.replace('(in_bom no)', '', 1))
    chk = kat.check(sch, net)
    assert not chk["ok"] and chk["variants"]["added"] == [
        {"ref": design["ref"]["CH_A/RC2/r"], "variant": "LITE", "key": "in_bom", "to": False}]
    kat.sync(sch, net, sheets=["ch", "rc"])
    assert kat.check(sch, net)["variants"]["ok"]


def test_removed_declarations_go_and_kicad_edits_stay(design):
    sch, net, ref = design["sch"], design["net"], design["ref"]
    # a variant and an override written in KiCad by hand
    pro = sch.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text())
    data["schematic"]["variants"].append({"name": "HAND"})
    pro.write_text(json.dumps(data))
    text = sch.read_text()
    at = text.index(f'(reference "{ref["j"]}")')
    sch.write_text(text[:at] + '(variant (name "HAND") (dnp yes)) ' + text[at:])
    # the declaration loses FULL and the CH_A/RC2/r override
    f = variants.variants_file(net)
    decl = json.loads(f.read_text())
    del decl["variants"]["FULL"]
    decl["variants"]["LITE"]["parts"] = [e for e in decl["variants"]["LITE"]["parts"] if e.get("tag") != "CH_A/RC2/r"]
    f.write_text(json.dumps(decl))
    rep = kat.sync(sch, net, sheets=["ch", "rc"])["variants"]
    assert rep["names"] == ["HAND", "LITE"] and rep["names_removed"] == ["FULL"]
    assert sorted((e["ref"], e["key"]) for e in rep["removed"]) == [(ref["CH_A/RC2/r"], "Value"),
                                                                     (ref["CH_A/RC2/r"], "in_bom")]
    assert ref["CH_A/RC2/r"] in bom(sch, "LITE") and bom(sch, "HAND")[ref["j"]]["DNP"]
    listed = variants.listing(sch)
    assert listed["parts"][ref["j"]] == {"LITE": {"MPN": "J-LITE"}, "HAND": {"dnp": True}}


def pos(pcb: Path, variant: str | None = None) -> set[str]:
    out = pcb.parent / f"pos-{variant or 'base'}.csv"
    env.run_cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--exclude-dnp",
                *(["--variant", variant] if variant else []), "-o", str(out), str(pcb))
    with open(out, newline="", encoding="utf-8") as f:
        return {r["Ref"] for r in csv.DictReader(f)}


def test_board_sync_carries_variants_through_kicad_saves(design):
    sch, ref = design["sch"], design["ref"]
    pcb = sch.with_suffix(".kicad_pcb")
    rep = kat.pcb.sync(pcb, force=True, require=())
    assert rep["check"]["parity"] == 0, rep["check"]
    names = [n["name"] for n in variants.listing(sch)["names"]]
    assert sorted(rep["variants"]) == sorted(names) and "LITE" in names     # KiCad's netlist sorts them
    board = variants.listing(pcb)
    assert sorted(n["name"] for n in board["names"]) == sorted(names)
    assert board["parts"][ref["CH_A/RC2/r"]]["LITE"] == {"exclude_from_bom": True, "Value": "2k7"}
    assert board["parts"][ref["j"]]["LITE"] == {"MPN": "J-LITE"}
    dnp = {ref[f"CH_B/{s}/c"] for s in ("RC1", "RC2", "RC3")}
    assert dnp <= pos(pcb) and not dnp & pos(pcb, "LITE")         # KiCad's variant position file
    a = kat.pcb.assembly(pcb, variant="LITE", require=())           # symbol and footprint agree; the
    assert [(e["ref"], e["problem"]) for e in a["errors"]] == [     # design forgot in_pos_files there
        (ref["CH_A/RC2/r"], "pos_not_in_bom")], a["errors"]
    kat.pcb.refill(pcb)                                           # a KiCad save of the board
    assert variants.listing(pcb)["parts"] == board["parts"]
    assert not kat.pcb.sync(pcb, force=True, require=())["changed"]


def test_declaration_errors_name_the_entry(tmp_path):
    net = tmp_path / "d.net"
    f = variants.variants_file(net)
    for bad, msg in (({"variants": {"A": {"parts": [{"ref": "R1"}]}}}, "changes nothing"),
                     ({"variants": {"A": {"parts": [{"ref": "R1", "Footprint": "x"}]}}}, "cannot differ"),
                     ({"variants": {"A": {"parts": [{"block": "CH_*", "dnp": True}]}}}, "select by"),
                     ({"variants": {"A": {"parts": [{"ref": "R1", "dnp": "yes"}]}}}, "true or false")):
        f.write_text(json.dumps(bad))
        with pytest.raises(env.KatError, match=msg):
            variants.load(net)
