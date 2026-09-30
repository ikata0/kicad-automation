"""Board sync (kat.pcb.sync): schematic -> board, proven by KiCad's DRC schematic parity."""

import json

import pytest

import kat
from kat import cli
from kat.pcb import Board
from test_gate import GOOD_FP, MPN
from test_sync import BASE_NETS, BASE_PARTS, project, write_netlist  # noqa: F401 (fixture)

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints")


def design(project, tmp_path, parts=BASE_PARTS, nets=BASE_NETS, footprints=GOOD_FP, fields=MPN,
           name="d.net"):
    net = write_netlist(tmp_path / name, parts, nets, footprints, fields)
    report = kat.sync(project, net)
    assert report["check"]["ok"], report["check"]
    return net


def board_of(project):
    return project.with_suffix(".kicad_pcb")


def pcb_ok(project, **kw) -> dict:
    r = kat.pcb.sync(board_of(project), **kw)
    assert r["ok"], r
    return r


def test_first_sync_creates_board_with_parity_and_is_idempotent(project, tmp_path):
    design(project, tmp_path)
    r = pcb_ok(project)
    assert {a["ref"] for a in r["added"]} == {"J1", "R1", "D1"}
    assert r["check"]["parity"] == 0 and r["check"]["unconnected"] == 3  # ratsnest, no copper
    b = Board(board_of(project))
    d1 = b.footprint("D1")
    assert d1.field("MPN") == "APT1608SEC" and d1.field("SKiDL Tag") == "tag-d1"
    assert {p.number: p.net for p in d1.pads()}["1"] == "GND"
    text = board_of(project).read_text()
    again = kat.pcb.sync(board_of(project))
    assert not again["changed"] and board_of(project).read_text() == text


def test_design_change_keeps_placement_and_reports_stale_copper(project, tmp_path):
    design(project, tmp_path)
    pcb_ok(project)
    pcb = board_of(project)
    with kat.pcb.edit(pcb) as b:
        b.footprint("R1").place(40, 30, rot=90)
        r2 = b.footprint("R1").pad("2")
        d2 = b.footprint("D1").pad("2")
        b.add_track((r2.x, r2.y), (d2.x, d2.y), 0.25, "F.Cu", r2.net)
    # v2: R1 renamed R5 (same tag), 0805 footprint; D1.2 moves to a new net LED_B
    parts = {**BASE_PARTS, "R5": ("Device", "R", "330", "tag-r1")}
    del parts["R1"]
    nets = {"VIN": [("J1", "1"), ("R5", "1")], "LED_A": [("R5", "2")],
            "LED_B": [("D1", "2"), ("J1", "2")], "GND": [("D1", "1")]}
    fps = {**GOOD_FP, "R5": "Resistor_SMD:R_0805_2012Metric"}
    mpn = {**MPN, "R5": MPN["R1"]}
    design(project, tmp_path, parts, nets, fps, mpn, name="v2.net")
    r = kat.pcb.sync(pcb)
    assert r["renamed"] == [{"from": "R1", "to": "R5"}]
    assert r["replaced"] == [{"ref": "R5", "from": GOOD_FP["R1"], "to": fps["R5"]}]
    assert (Board(pcb).footprint("R5").x, Board(pcb).footprint("R5").y) == (40, 30)
    assert len(r["stale_tracks"]) == 1 and "D1.2" in r["stale_tracks"][0]["why"]
    assert r["check"]["parity"] == 0
    r = kat.pcb.sync(pcb, rip_stale=True)
    assert r["stale_tracks"][0]["removed"] and not Board(pcb).tracks()
    assert not kat.pcb.sync(pcb)["changed"]


def test_net_rename_follows_the_copper_and_removed_part_goes(project, tmp_path):
    design(project, tmp_path)
    pcb_ok(project)
    pcb = board_of(project)
    with kat.pcb.edit(pcb) as b:
        p1, p2 = b.footprint("R1").pad("2"), b.footprint("D1").pad("2")
        b.add_track((p1.x, p1.y), (p2.x, p2.y), 0.25, "F.Cu", p1.net)
        old_net = p1.net
    nets = {**BASE_NETS, "LED_DRIVE": BASE_NETS["LED_A"]}
    del nets["LED_A"]
    design(project, tmp_path, nets=nets, name="v2.net")
    # the re-synced schematic has a text overlap an arrangement pass would fix; the gate
    # rightly says "not clean", so this board-only test forces the sync
    r = pcb_ok(project, force=True)
    assert r["renamed_nets"] == [{"from": old_net, "to": old_net.replace("LED_A", "LED_DRIVE"),
                                  "kind": "track"}]
    assert not r["stale_tracks"] and Board(pcb).tracks()[0]["net"].endswith("LED_DRIVE")
    # J1 leaves the design: footprint removed
    parts = {k: v for k, v in BASE_PARTS.items() if k != "J1"}
    nets2 = {"LED_DRIVE": BASE_NETS["LED_A"], "GND": [("D1", "1"), ("R1", "1")]}
    design(project, tmp_path, parts, nets2, name="v3.net")
    r = pcb_ok(project, force=True)
    assert r["removed"] == ["J1"] and {f.ref for f in Board(pcb).footprints()} == {"R1", "D1"}


def test_gate_blocks_until_ready(project, tmp_path, capsys):
    design(project, tmp_path, fields={})                       # no MPNs
    r = kat.pcb.sync(board_of(project))
    assert not r["ok"] and r["gate"]["by_problem"]["missing_field"] == 3
    assert not board_of(project).exists()
    assert cli.main(["pcb", "sync", str(board_of(project)), "--force"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and len(out["added"]) == 3


def test_gate_compares_with_the_remembered_design(project, tmp_path):
    """I7: the board sync's gate compares the schematic with the netlist it was synced
    from; a design changed since then blocks the board."""
    net = design(project, tmp_path)
    assert pcb_ok(project)["gate"]["ready"]
    changed = dict(BASE_NETS)
    changed["VSUP"] = changed.pop("VIN")
    write_netlist(net, BASE_PARTS, changed, GOOD_FP, MPN)       # same file, not synced
    r = kat.pcb.sync(board_of(project))
    assert not r["ok"] and r["gate"]["by_problem"].get("schematic_not_clean") == 1, r["gate"]
