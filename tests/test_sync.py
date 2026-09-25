"""kat sync against synthetic netlists (KiCad s-expression format, as SKiDL writes it)."""

import shutil
from pathlib import Path

import pytest

from kat import cli, netlist, sync
from kat.schematic import Schematic

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")

# ref: (library, part, value, tag)
BASE_PARTS = {
    "J1": ("Connector_Generic", "Conn_01x02", "SUPPLY", "tag-j1"),
    "R1": ("Device", "R", "330", "tag-r1"),
    "D1": ("Device", "LED", "red", "tag-d1"),
}
BASE_NETS = {
    "VIN": [("J1", "1"), ("R1", "1")],
    "LED_A": [("R1", "2"), ("D1", "2")],
    "GND": [("D1", "1"), ("J1", "2")],
}


def write_netlist(path: Path, parts: dict, nets: dict, footprints: dict | None = None) -> Path:
    comps = []
    for ref, (lib, part, value, tag) in parts.items():
        fp = (footprints or {}).get(ref, "")
        comps.append(f'(comp (ref "{ref}") (value "{value}") (footprint "{fp}") '
                      f'(fields (field (name "SKiDL Tag") "{tag}")) '
                      f'(libsource (lib "{lib}") (part "{part}")))')
    net_txt = []
    for i, (name, pins) in enumerate(nets.items(), 1):
        nodes = " ".join(f'(node (ref "{r}") (pin "{p}"))' for r, p in pins)
        net_txt.append(f'(net (code "{i}") (name "{name}") {nodes})')
    path.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(net_txt)}))\n')
    return path


@pytest.fixture
def project(tmp_path):
    sch = tmp_path / "p" / "p.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    return sch


def positions(sch: Path) -> dict:
    return {(s.ref, s.unit): (s.place.x, s.place.y) for s in Schematic(sch).symbols()
            if not s.ref.startswith("#")}


def sync_ok(sch, net) -> dict:
    report = sync.run(sch, net)
    assert report["check"]["ok"], report["check"]
    assert not report["unresolved"]
    return report


def test_first_import_is_correct_clean_and_idempotent(project, tmp_path):
    net = write_netlist(tmp_path / "d.net", BASE_PARTS, BASE_NETS)
    report = sync_ok(project, net)
    assert {a["ref"] for a in report["added"]} == {"J1", "R1", "D1"}
    assert report["check"]["clean"], report["check"]
    assert report["check"]["erc"]["warnings"] == 0
    s = Schematic(project)
    assert {x.field("SKiDL Tag") for x in s.symbols() if not x.ref.startswith("#")} == \
        {"tag-j1", "tag-r1", "tag-d1"}
    again = sync.run(project, net)
    assert not again["changed"], again


def test_power_flag_added_once(project, tmp_path):
    net = write_netlist(tmp_path / "d.net", BASE_PARTS, BASE_NETS)
    report = sync_ok(project, net)
    assert report["flags"] == ["GND"]
    sync_ok(project, net)
    flags = [x for x in Schematic(project).symbols() if x.lib_id == "power:PWR_FLAG"]
    assert len(flags) == 1


def test_rename_by_tag_keeps_position(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    before = positions(project)
    parts = dict(BASE_PARTS)
    parts["RLED"] = parts.pop("R1")
    nets = {k: [("RLED" if r == "R1" else r, p) for r, p in v] for k, v in BASE_NETS.items()}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert report["renamed"] == [{"from": "R1", "to": "RLED"}]
    assert not report["added"] and not report["removed"] and not report["attached"]
    after = positions(project)
    assert after[("RLED", 1)] == before[("R1", 1)]


def test_swapped_references_do_not_collide(project, tmp_path):
    parts = dict(BASE_PARTS, R2=("Device", "R", "1k", "tag-r2"))
    nets = dict(BASE_NETS, VIN=[("J1", "1"), ("R1", "1"), ("R2", "1")],
                LED_A=[("R1", "2"), ("R2", "2"), ("D1", "2")])
    sync_ok(project, write_netlist(tmp_path / "a.net", parts, nets))
    before = positions(project)
    swapped = dict(parts, R1=("Device", "R", "1k", "tag-r2"), R2=("Device", "R", "330", "tag-r1"))
    swap = {"R1": "R2", "R2": "R1"}
    nets2 = {k: [(swap.get(r, r), p) for r, p in v] for k, v in nets.items()}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", swapped, nets2))
    assert len(report["renamed"]) == 2
    assert positions(project)[("R1", 1)] == before[("R2", 1)]


def test_value_and_footprint_update(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    parts = dict(BASE_PARTS, R1=("Device", "R", "470", "tag-r1"))
    fps = {"R1": "Resistor_SMD:R_0603_1608Metric"}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, BASE_NETS, fps))
    fields = {(u["ref"], u["field"]): u["to"] for u in report["updated"]}
    assert fields == {("R1", "Value"): "470", ("R1", "Footprint"): "Resistor_SMD:R_0603_1608Metric"}
    r1 = Schematic(project).find_symbols("R1")[0]
    assert r1.field("Value") == "470"


def test_removed_part_takes_its_dangling_items_along(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    parts = {k: v for k, v in BASE_PARTS.items() if k != "D1"}
    nets = {"VIN": BASE_NETS["VIN"], "GND": [("J1", "2")]}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert report["removed"] == ["D1"]
    s = Schematic(project)
    assert "D1" not in {x.ref for x in s.symbols()}
    # every remaining wire end touches a pin, label, or another wire
    assert s.prune_dangling() == 0
    assert report["check"]["erc"]["errors"] == 0


def test_pin_moved_to_another_net_is_rewired(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    parts = dict(BASE_PARTS, R2=("Device", "R", "10", "tag-r2"))
    nets = dict(BASE_NETS, GND=[("J1", "2"), ("R2", "2")], LED_K=[("D1", "1"), ("R2", "1")])
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert "D1.1" in report["detached"]
    assert {"pin": "D1.1", "net": "LED_K"} in report["attached"]


def test_net_rename_renames_labels(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    nets = dict(BASE_NETS)
    nets["VSUP"] = nets.pop("VIN")
    report = sync_ok(project, write_netlist(tmp_path / "b.net", BASE_PARTS, nets))
    assert report["named"] == [{"from": "VIN", "to": "VSUP"}]
    assert not report["detached"] and not report["attached"]
    texts = {l["text"] for l in Schematic(project).labels()}
    assert "VSUP" in texts and "VIN" not in texts
    assert not report["check"]["netlist_vs_expected"]["name_differences"]


def test_library_change_replaces_part_in_place(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    before = positions(project)
    parts = dict(BASE_PARTS, R1=("Device", "R_Small", "330", "tag-r1"))
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, BASE_NETS))
    assert report["replaced"] == [{"ref": "R1", "lib_id": "Device:R_Small"}]
    r1 = Schematic(project).find_symbols("R1")[0]
    assert r1.lib_id == "Device:R_Small" and (r1.place.x, r1.place.y) == before[("R1", 1)]


def test_new_parts_go_below_existing_drawing(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    lowest = max(y for (_, _), (_, y) in positions(project).items())
    parts = dict(BASE_PARTS, C1=("Device", "C", "100n", "tag-c1"))
    nets = dict(BASE_NETS, VIN=BASE_NETS["VIN"] + [("C1", "1")], GND=BASE_NETS["GND"] + [("C1", "2")])
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert report["added"][0]["ref"] == "C1" and report["added"][0]["y"] > lowest


def test_dry_run_saves_nothing(project, tmp_path):
    before = project.read_text()
    report = sync.run(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS), dry_run=True)
    assert report["changed"] and report["check"] is None
    assert project.read_text() == before


def test_detach_then_connect_replaces_labels_by_wire(project, tmp_path, capsys):
    net = write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS)
    sync_ok(project, net)
    script = tmp_path / "wire.kat"
    script.write_text("detach R1.2\ndetach D1.2\nconnect R1.2 D1.2\n")
    assert cli.main(["batch", str(project), str(script)]) == 0
    capsys.readouterr()
    assert "LED_A" not in {l["text"] for l in Schematic(project).labels()}
    cmp = netlist.compare(netlist.from_schematic(project), netlist.read(net))
    assert cmp["match"], cmp
    # connected correctly, but the design's net name is no longer visible -> not clean
    assert cmp["unnamed"] == ["LED_A"]
    assert cli.main(["check", str(project), "--expect", str(net)]) == 0
    assert '"clean": false' in capsys.readouterr().out
    # a label on the new wire restores it; sync then has nothing to do
    assert cli.main(["label", str(project), "LED_A", "R1.2", "--stub", "0,0"]) == 0
    capsys.readouterr()
    assert not sync.run(project, net)["changed"]
