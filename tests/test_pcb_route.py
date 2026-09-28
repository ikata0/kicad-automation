"""Autorouting through kat's Specctra DSN/SES and Freerouting; judged by KiCad's DRC.

Needs Java 21+ and Freerouting (`kat pcb route --install`); skipped otherwise."""

import shutil
from pathlib import Path

import pytest

import kat
from kat.pcb import Board, router, specctra
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_pcb_structure import placed  # noqa: F401 (fixture: parts placed, GND pour on B.Cu)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = [
    pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed"),
    pytest.mark.skipif(shutil.which("java") is None or not router.jar_path().exists(),
                       reason="Freerouting not installed (kat pcb route --install)"),
]


def test_dsn_round_trip_of_existing_copper(placed, tmp_path):
    """Our DSN carries every pad of every net; the SES reader reads Specctra wires back."""
    with kat.pcb.edit(placed) as b:
        b.track(["R1.2", "D1.2"], lock=True)
    b = Board(placed)
    info = specctra.write_dsn(b, tmp_path / "t.dsn")
    text = (tmp_path / "t.dsn").read_text()
    assert info["footprints"] == 7 and "(plane GND" in text and "(type protect)" in text
    for p in b.pads():
        if p.net:
            assert f"{p.ref}-{p.number}" in text
    ses = tmp_path / "t.ses"
    ses.write_text('(session t (routes (resolution um 10) (network_out (net "/A1" '
                   '(wire (path F.Cu 2000 248250 -80000 272125 -80000)) '
                   '(via "Via[0-1]_600:300_um" 250000 -100000)))))')
    r = specctra.read_ses(ses)
    assert r["wires"][0]["start"] == pytest.approx((24.825, 8.0)) and r["wires"][0]["width"] == 0.2
    assert r["vias"][0]["at"] == pytest.approx((25.0, 10.0)) and r["vias"][0]["size"] == 0.6


def test_route_after_structure_is_complete_and_keeps_it(placed):
    with kat.pcb.edit(placed) as b:
        b.fanout(["Q1", "C1"], nets=["GND"])          # vias into the B.Cu plane, locked
        locked = {v["uuid"] for v in b.vias()}
    r = kat.pcb.route(placed, passes=10, timeout=300)
    c = r["check"]
    assert c["unconnected"] == 0 and c["drc_errors"] == 0 and c["parity"] == 0 and c["islands"] == 0, r
    assert r["added_tracks"] > 0
    assert locked <= {v["uuid"] for v in Board(placed).vias()}   # structure untouched
    assert Path(r["dir"], "freerouting.log").exists()


def test_route_takes_a_path_relative_to_another_folder(placed, monkeypatch):
    """Freerouting runs in the route folder; the board path given from a parent folder
    must still reach it (it once got the relative path and found no DSN)."""
    monkeypatch.chdir(placed.parent.parent)
    r = kat.pcb.route(placed.relative_to(placed.parent.parent), passes=5, timeout=300)
    assert r["router"]["unrouted"] == 0, r


def test_router_keeps_class_layers_class_clearance_and_hole_clearance(placed):
    """I3: a class restricted to F.Cu + In2.Cu is routed there; the class-to-class rule and
    the hole clearance reach the router (DSN) and DRC; the result is DRC clean."""
    with kat.pcb.edit(placed) as b:
        b.set_copper_layers(4)
        b.netclass("SIG", track=0.2, clearance=0.2, nets=["/A1"], layers=["F.Cu", "In2.Cu"])
        b.netclass("OTHER", track=0.2, clearance=0.2, nets=["/A2"])
        b.class_clearance("SIG", "OTHER", 1.0)
        b.constraints(hole_clearance=0.4, edge_clearance=0.5)
    b = Board(placed)
    assert b.route_settings() == {"layers": {"SIG": ["F.Cu", "In2.Cu"]}, "class_clearance": {"OTHER SIG": 1.0}}
    specctra.write_dsn(b, placed.parent / "t.dsn")
    dsn = (placed.parent / "t.dsn").read_text()
    assert "(use_layer F.Cu) (use_layer In2.Cu)" in dsn and "(class_class (classes OTHER SIG)" in dsn
    assert "(clearance 250 (type via_wire))" in dsn       # 0.4 - (0.6 - 0.3) / 2 = 0.25 mm
    assert dsn.count("wire_keepout") >= 4 * 4             # the edge band: 4 edges x 4 layers
    r = kat.pcb.route(placed, passes=10, timeout=300)
    c = r["check"]
    assert c["unconnected"] == 0 and c["drc_errors"] == 0, r
    assert {t["layer"] for t in Board(placed).tracks() if t["net"] == "/A1"} <= {"F.Cu", "In2.Cu"}
    assert "kat class OTHER SIG" in placed.with_suffix(".kicad_dru").read_text()
