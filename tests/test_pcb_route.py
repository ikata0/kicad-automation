"""Autorouting through kat's Specctra DSN/SES and Freerouting; judged by KiCad's DRC.

Needs Java 25+ and Freerouting (`kat pcb route --install`); skipped otherwise."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import cli
from kat.pcb import Board, router, specctra
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_pcb_structure import placed  # noqa: F401 (fixture: parts placed, GND pour on B.Cu)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = [
    pytest.mark.requires("kicad", "symbols", "footprints"),
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


@pytest.mark.requires("routing")
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


@pytest.mark.requires("routing")
def test_route_takes_a_path_relative_to_another_folder(placed, monkeypatch):
    """Freerouting runs in the route folder; the board path given from a parent folder
    must still reach it (it once got the relative path and found no DSN)."""
    monkeypatch.chdir(placed.parent.parent)
    r = kat.pcb.route(placed.relative_to(placed.parent.parent), passes=5, timeout=300)
    assert r["router"]["unrouted"] == 0, r


@pytest.mark.requires("routing")
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
    with kat.pcb.edit(placed) as b:
        b.mounting_hole((36, 21), "M2")                  # no copper: a keep-out for the router
    before = dsn.count("wire_keepout")
    specctra.write_dsn(Board(placed), placed.parent / "t.dsn")
    assert (placed.parent / "t.dsn").read_text().count("wire_keepout") == before + 4
    part = specctra.write_dsn(Board(placed), placed.parent / "p.dsn", nets=["/A1"])   # SIG only
    assert part["class_clearance_not_applied"][0].startswith("OTHER SIG")
    r = kat.pcb.route(placed, passes=10, timeout=300)
    c = r["check"]
    assert c["unconnected"] == 0 and c["drc_errors"] == 0, r
    assert {t["layer"] for t in Board(placed).tracks() if t["net"] == "/A1"} <= {"F.Cu", "In2.Cu"}
    assert "kat class OTHER SIG" in placed.with_suffix(".kicad_dru").read_text()


@pytest.mark.requires("routing")
def test_java_minimum_matches_the_jar():
    """The pinned jar's own classes say which Java runs them: class file version - 44."""
    import struct
    import zipfile
    with zipfile.ZipFile(router.jar_path()) as z:
        manifest = z.read("META-INF/MANIFEST.MF").decode()
        main = next(l for l in manifest.splitlines() if l.startswith("Main-Class")).split()[1]
        head = z.read(main.replace(".", "/") + ".class")[:8]
    assert struct.unpack(">H", head[6:8])[0] - 44 == router.JAVA_MIN


# --- what Freerouting does with its settings, and what kat says about it ------------------------

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
import router_benchmark  # noqa: E402

needs_routing = pytest.mark.requires("routing", "footprints")


@pytest.fixture
def bench(tmp_path):
    pcb = tmp_path / "b" / "bench.kicad_pcb"
    router_benchmark.build(pcb, 2, 2, 4, classes=True)
    return pcb


LOG = """2026-11-03 15:09:46.818 INFO   [A\\B] Fanout stage completed: started with 400 total SMD pins, completed in 3.71 seconds, escaped
2026-11-03 15:10:22.096 INFO   [A\\B] Auto-routing pass #1 on board 'x' was completed in 34.60 seconds with score 862.80 (45 unrouted and 0 violations).
2026-11-03 15:10:41.310 INFO   [A\\B] Auto-routing pass #2 on board 'y' was completed in 18.44 seconds with score 945.11 (18 unrouted and 0 violations).
2026-11-03 15:11:36.294 INFO   [A\\B] Auto-routing stage completed: started with 100 unrouted nets, completed in 107.40 seconds, final score: 999.98
2026-11-03 15:17:33.624 INFO   [A\\B] Optimization stage completed: started with score 984.74 (5 unrouted and 0 violations), completed in 356.51 seconds
2026-11-03 15:09:46.818 WARN   Failed to apply CLI router setting: router.bogus: No field found with name or SerializedName: bogus
"""


def test_reading_a_router_log():
    r = router.read_log(LOG)
    assert r["stages"] == {"fanout": 3.71, "routing": 107.4, "optimizer": 356.51}
    assert r["unrouted"] == [45, 18] and not r["timed_out"]
    assert r["failed"] == [{"option": "bogus", "reason": "No field found with name or SerializedName: bogus"}]
    r = router.read_log("Auto-routing stage completed with timeout: started with 72 unrouted nets, completed in 5.07 seconds")
    assert r["timed_out"] and r["stages"] == {"routing": 5.07}


@pytest.mark.parametrize("key, match", [("ignore_net_classes", "skip_classes"), ("router.ignore_net_classes", "skip_classes"),
                                        ("automatic_neckdown", "narrow"), ("max_passes", "passes"), ("job_timeout", "timeout"),
                                        ("enabled", "batch"), ("no spaces", "not a setting")])
def test_settings_that_cannot_work_are_refused_before_anything_runs(tmp_path, key, match):
    with pytest.raises(kat.KatError, match=match):
        router.route(tmp_path / "none.kicad_pcb", options={key: "x"})


def test_option_words():
    args, unverified = router._option_args({"optimizer.enabled": False, "--router.max_threads": 2})
    assert args == ["--router.optimizer.enabled=false", "--router.max_threads=2"]
    assert unverified == ["max_threads"]


@needs_routing
def test_freerouting_still_ignores_its_ignore_net_classes(bench, tmp_path):
    """Why kat refuses that setting: if an upgrade makes this fail, it can be offered instead."""
    b = Board(bench)
    dsn, ses = tmp_path / "x.dsn", tmp_path / "x.ses"
    specctra.write_dsn(b, dsn)
    slow = set(re.search(r"\(class SLOW ([^()]*)", dsn.read_text())[1].split())
    subprocess.run([router.java(), "-Djava.awt.headless=true", "-jar", str(router.jar_path()), "--gui.enabled=false",
                    "-de", str(dsn), "-do", str(ses), "-mp", "10", "--router.ignore_net_classes=SLOW"],
                   capture_output=True, stdin=subprocess.DEVNULL, timeout=120, check=True)
    assert slow and slow <= {w["net"] for w in specctra.read_ses(ses)["wires"]}


@needs_routing
def test_skipped_classes_get_no_copper(bench):
    r = kat.pcb.route(bench, passes=10, timeout=120, skip_classes=["SLOW"])
    slow = set(r["skipped"]["SLOW"])
    routed = {t["net"] for t in Board(bench).tracks()}
    assert slow and not slow & routed and routed
    assert r["check"]["unconnected"] == len(slow), "DRC counts them as open, as they are"
    assert r["check"]["drc_errors"] == 0, r["check"]["by_type"]
    # the rest can be routed afterwards, around what is there
    r = kat.pcb.route(bench, passes=10, timeout=120, nets=sorted(slow))
    assert r["check"]["unconnected"] == 0 and r["check"]["drc_errors"] == 0, r["check"]


@needs_routing
def test_skipping_combines_with_nets_and_names_an_unknown_class(bench):
    b = Board(bench)
    names = sorted({p.net for p in b.pads() if p.net})
    with pytest.raises(kat.KatError, match="no net is in class NOPE"):
        kat.pcb.route(bench, skip_classes=["NOPE"])
    r = kat.pcb.route(bench, passes=5, timeout=120, nets=names[:6], skip_classes=["SLOW"])
    routed = {t["net"] for t in Board(bench).tracks()}
    assert routed <= set(names[:6]) and not routed & set(r["skipped"]["SLOW"])


@needs_routing
def test_stages_and_a_setting_the_router_refuses_are_reported(bench):
    r = kat.pcb.route(bench, passes=10, timeout=120, options={"no_such_thing": 1, "max_threads": 2})
    assert {"routing", "optimizer"} <= set(r["stages"]) and r["unrouted"][-1] == 0
    assert [f["option"] for f in r["settings"]["refused_by_router"]] == ["no_such_thing"]
    assert r["settings"]["unverified"] == ["max_threads"]
    assert "timed_out" not in r


@needs_routing
def test_the_optimizer_can_be_left_out(bench):
    r = kat.pcb.route(bench, passes=10, timeout=120, optimize=False)
    assert "optimizer" not in r["stages"] and "routing" in r["stages"] and "settings" not in r
    assert r["check"]["unconnected"] == 0 and r["check"]["drc_errors"] == 0, r["check"]


@needs_routing
def test_a_timeout_keeps_what_the_router_has(tmp_path):
    pcb = tmp_path / "b" / "bench.kicad_pcb"
    router_benchmark.build(pcb, 3, 3, 4)
    r = kat.pcb.route(pcb, passes=25, timeout=10)
    assert r["timed_out"] and r["seconds"] < 40 and r["added_tracks"] > 0
    assert r["check"]["unconnected"] > 0 and r["check"]["drc_errors"] == 0, r["check"]
    assert len(Board(pcb).tracks()) == r["added_tracks"], "the board keeps it"


def _neck_beside_a_pad(b, offset):
    """A 0.075 mm router track of another net along a pad's edge, its centre ``offset`` mm from it."""
    from kat.pcb.router import apply_session
    pad = next(p for p in b.footprint("U1").pads() if p.net and int(p.number) <= 8)
    top = min(y for _, y in pad.outline)
    other = next(n for n in sorted({p.net for p in b.pads() if p.net}) if n != pad.net)
    return apply_session(b, {"vias": [], "wires": [{"start": (pad.x - 0.5, top - offset), "end": (pad.x + 0.5, top - offset),
                                                    "width": 0.075, "layer": "F.Cu", "net": other}]})


@pytest.mark.requires("kicad", "footprints")
def test_a_neck_too_tight_for_its_class_still_reaches_the_board_minimum(bench):
    """The class's 0.15 mm does not fit, 0.075 mm breaks the board's 0.1 mm minimum, and 0.1 mm passes DRC."""
    b = Board(bench)
    res = _neck_beside_a_pad(b, 0.205)
    assert res["widened"] == 0 and [n["width"] for n in res["narrow"]] == [0.1], res
    b.save()
    c = kat.pcb.check(bench)
    assert c["drc_errors"] == 0, c["by_type"]


@pytest.mark.requires("kicad", "footprints")
def test_a_neck_is_not_widened_into_other_copper(bench):
    """The minimum would overlap the pad: a short is worse than a narrow track, so it stays as it is."""
    res = _neck_beside_a_pad(Board(bench), 0.04)
    assert res["widened"] == 0 and [n["width"] for n in res["narrow"]] == [0.075], res


@needs_routing
def test_the_cli_passes_them_on(bench, capsys):
    assert cli.main(["pcb", "route", str(bench), "--passes", "5", "--timeout", "120", "--skip-class", "SLOW",
                     "--no-optimize", "--router-option", "max_threads=2"]) in (0, 1)
    import json
    r = json.loads(capsys.readouterr().out)
    assert r["skipped"]["SLOW"] and "optimizer" not in r["stages"] and r["settings"]["unverified"] == ["max_threads"]
    assert cli.main(["pcb", "route", str(bench), "--router-option", "x"]) == 2
    assert "KEY=VALUE" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.requires("kicad", "footprints")
def test_the_benchmark_board_is_reproducible(tmp_path):
    a, b = tmp_path / "a" / "x.kicad_pcb", tmp_path / "b" / "x.kicad_pcb"
    assert router_benchmark.build(a, 2, 2, 2, seed=3) == router_benchmark.build(b, 2, 2, 2, seed=3)
    nets = lambda f: {(q.net, p.ref, q.number) for p in Board(f).footprints() for q in p.pads()}   # noqa: E731
    assert nets(a) == nets(b)
    c = tmp_path / "c" / "x.kicad_pcb"
    router_benchmark.build(c, 2, 2, 2, seed=4)
    assert nets(a) != nets(c)

