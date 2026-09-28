"""Block identity from SKiDL subcircuits: the block record (ports, parameters), tags
qualified by the instance path, and sync/replicate on nested blocks."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import cli, netlist
from kat.schematic import Schematic

HERE = Path(__file__).parent / "e2e"

pytestmark = pytest.mark.skipif(
    shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
    reason="needs kicad-cli and skidl")


@pytest.fixture(scope="module")
def designs(tmp_path_factory):
    out = tmp_path_factory.mktemp("blocks")
    nets = {}
    for variant in ("base", "value", "untagged"):
        net = out / f"{variant}.net"
        subprocess.run([sys.executable, str(HERE / "blocks_design.py"), variant, str(net)],
                       check=True, capture_output=True, cwd=out)
        nets[variant] = net
    return nets


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("KAT_REPORT", "0")
    sch = tmp_path / "p" / "p.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    return sch


def test_block_record_has_ports_params_and_types(designs):
    rec = json.loads(netlist.blocks_file(designs["base"]).read_text())
    inst = rec["instances"]
    assert sorted(inst) == ["CH_A", "CH_A/RC1", "CH_A/RC2", "CH_A/RC3",
                            "CH_B", "CH_B/RC1", "CH_B/RC2", "CH_B/RC3"]
    assert inst["CH_A"]["type"] == "ch"
    assert inst["CH_A"]["ports"] == {"io.inp": "IN_A", "io.out": "OUT_A", "io.gnd": "GND"}
    assert inst["CH_A"]["params"] == {"ra": "1k", "rb": "2k2"}      # rb: the unset default
    assert inst["CH_B/RC1"]["type"] == "rc"
    assert inst["CH_B/RC1"]["params"] == {"r_val": "3k3"}
    assert inst["CH_B/RC1"]["ports"]["inp"] == "IN_B"
    assert len(inst["CH_B/RC1"]["parts"]) == 2
    assert rec["warnings"] == []


def test_untagged_positional_calls_are_warned_and_defaults_left_out(designs):
    rec = json.loads(netlist.blocks_file(designs["untagged"]).read_text())
    assert any("no tag=" in w for w in rec["warnings"])
    assert any("passed by position" in w and "inp, out, gnd" in w for w in rec["warnings"])
    rc = next(i for i in rec["instances"].values() if i["type"] == "rc")
    assert rc["params"] == {}          # r_val came by position: its default is not the value


def test_design_parts_carry_block_and_qualified_tag(designs):
    comps, _ = netlist.read_design(designs["base"])
    by_tag = {c.tag: c for c in comps.values()}
    assert len(by_tag) == len(comps)                      # tags unique once qualified
    r = by_tag["CH_B/RC1/r"]
    assert (r.block, r.value) == ("CH_B/RC1", "3k3")
    assert r.fields["Block"] == "CH_B/RC1" and r.fields["Block Type"] == "rc"
    j = by_tag["j"]
    assert j.block == "" and "Block" not in j.fields


def state(sch: Path) -> dict:
    return {s.ref: (s.field("SKiDL Tag"), s.field("Block"), s.field("Value"), s.place.x, s.place.y)
            for s in Schematic(sch).symbols() if not s.ref.startswith("#")}


def test_sync_keeps_instances_apart_across_a_design_change(designs, project):
    rep = kat.sync(project, designs["base"])
    assert rep["check"]["ok"] and rep["check"]["netlist_vs_expected"]["match"]
    first = state(project)
    assert first["R5"][:3] == ("CH_B/RC1/r", "CH_B/RC1", "3k3")
    assert not kat.sync(project, designs["base"])["changed"]
    rep = kat.sync(project, designs["value"])
    assert rep["check"]["netlist_vs_expected"]["match"]
    assert rep["updated"] == [{"ref": "R5", "field": "Value", "from": "3k3", "to": "6k8"}]
    assert not rep["renamed"] and not rep["added"] and not rep["removed"]
    after = state(project)
    assert {k: v[3:] for k, v in after.items()} == {k: v[3:] for k, v in first.items()}


def test_board_blocks_are_nested_and_replicate_pairs_by_instance(designs, project):
    kat.sync(project, designs["base"])
    pcb = project.with_suffix(".kicad_pcb")
    assert kat.pcb.sync(pcb, force=True)["check"]["parity"] == 0
    with kat.pcb.edit(pcb) as b:
        blocks = b.blocks()
        assert len(blocks["CH_A"]) == 7 and len(blocks["CH_A/RC1"]) == 2
        res = b.replicate("CH_A", {"CH_B": (150, 150)})
        tag = {f.ref: f.field("SKiDL Tag") for f in b.footprints()}
        for a, bb in res["pairs"]["CH_B"].items():
            assert tag[a].replace("CH_A/", "CH_B/", 1) == tag[bb]
        assert b.block_box("CH_B")[:2] == pytest.approx((150, 150))
