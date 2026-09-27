"""Large design: 10x10 LED matrix (200 parts, two 64-pin connectors) on an A2 sheet.

Import, two arrangement passes, title block and notes, checks, review tiles, a design
change and back; then the board: four layers with a GND plane, placement by loop,
cathode vias, autorouting (Freerouting), finishing, fabrication files, release gate; the board passes run through
`kat.batch_exec`, so the design report holds their code. Guards the speed of the library (the same passes took 193 s through
`kat batch` before the geometry indexing), the arrangement result and sync on a large,
arranged sheet (no duplicate stubs or labels).
Set KAT_E2E_OUT=/some/dir to keep the project and its snapshots for review.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import kat

HERE = Path(__file__).parent / "e2e"
sys.path.insert(0, str(HERE))
import led_matrix_arrange as arrange  # noqa: E402
import led_matrix_board as board_passes  # noqa: E402
from kat.pcb import router  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.skipif(
    shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
    reason="needs kicad-cli and skidl")]


def design(out: Path, *variant: str) -> Path:
    net = out / ("led_matrix.net" if not variant else f"led_matrix_{variant[0]}.net")
    subprocess.run([sys.executable, str(HERE / "led_matrix.py"), str(net), *variant],
                   check=True, capture_output=True, cwd=out)
    return net


def assert_clean(check: dict) -> None:
    assert check["erc"]["errors"] == 0 and check["erc"]["warnings"] == 0, check["erc"]
    assert check["netlist_vs_expected"]["match"], check["netlist_vs_expected"]
    for k in ("overlaps", "body_overlaps", "off_page"):
        assert not check[k], (k, check[k][:5])
    assert check["clean"]


@pytest.fixture
def root(tmp_path) -> Path:
    root = Path(os.environ["KAT_E2E_OUT"]) / "led_matrix" if os.environ.get("KAT_E2E_OUT") else tmp_path
    if root.exists() and root != tmp_path:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_led_matrix(root):
    sch = root / "led_matrix" / "led_matrix.kicad_sch"
    net = design(root)
    kat.new(sch)
    report = kat.sync(sch, net)
    assert report["check"]["clean"], "the import alone is already clean"
    assert report["paper"] and not report["check"]["off_page"]
    assert len(report["added"]) == 202 and report["check"]["ok"]
    kat.snapshot(sch, slug="import", expect=net)

    t0 = time.time()
    with kat.edit(sch) as s:
        arrange.pass1(s)
    with kat.edit(sch) as s:
        arrange.pass2(s)
    elapsed = time.time() - t0
    assert elapsed < 60, f"arrangement took {elapsed:.0f} s"
    with kat.edit(sch) as s:
        s.title(title="LED matrix 10x10", rev="A", date="2026-09-25",
                comments={1: "Drive 3.3-5 V per LED_r_c, about 2-4.5 mA"})
        s.text("J1: rows 1-5", (arrange.J_X - 10 * arrange.G, arrange.J_Y[1] - 20 * arrange.G), size=2.54)
        s.text("J2: rows 6-10", (arrange.J_X - 10 * arrange.G, arrange.J_Y[2] - 20 * arrange.G), size=2.54)
    snap = kat.snapshot(sch, slug="arranged", expect=net, note="pass 1 + 2, title, notes")
    assert snap["clean"] and snap["netlist_match"], snap["findings"]
    assert '(paper "A2")' in sch.read_text()
    assert snap["tiles"], "an A2 sheet gets review tiles"
    assert_clean(kat.check(sch, net))
    assert kat.view(sch, around="J1")["height"] > 1000

    # a design change and back (J1 pins 2/3 swapped, R1 = 1k): the arrangement survives and
    # no pin collects duplicate stubs or labels
    probe = design(root, "probe")
    report = kat.sync(sch, probe)
    assert sorted(a["pin"] for a in report["attached"]) == ["J1.2", "J1.3"], report["attached"]
    assert report["check"]["ok"] and not report["check"]["duplicates"]
    report = kat.sync(sch, net)
    assert sorted(a["pin"] for a in report["attached"]) == ["J1.2", "J1.3"], report["attached"]
    check = kat.check(sch, net)
    assert check["ok"] and not check["duplicates"], check["duplicates"]
    kat.snapshot(sch, slug="probe-round-trip", expect=net)

    # the arrangement survives a further sync of the unchanged design
    text = sch.read_text()
    assert not kat.sync(sch, net)["changed"]
    assert sch.read_text() == text


    # the board (needs Freerouting; the schematic part above runs without it)
    if shutil.which("java") is None or not router.jar_path().exists():
        pytest.skip("Freerouting not installed (kat pcb route --install)")
    gate = kat.ready(sch, expect=net)
    assert gate["ready"], gate["errors"][:5]
    pcb = sch.with_suffix(".kicad_pcb")
    r = kat.pcb.sync(pcb)
    assert r["ok"] and len(r["added"]) == 202
    for name in ("place", "structure"):          # journaled with their code
        res = kat.batch_exec(HERE / "led_matrix_board.py", argv=[str(pcb), name])
        assert res["ok"], res["error"]
    kat.pcb.snapshot(pcb, slug="structure", note="4 layers, In1 GND plane, cathode vias", three_d=False)
    t0 = time.time()
    routed = kat.pcb.route(pcb, passes=25, timeout=1500)
    c = routed["check"]
    assert c["unconnected"] == 0 and c["drc_errors"] == 0 and c["islands"] == 0, routed
    with kat.pcb.edit(pcb) as b:
        board_passes.finish(b)
    kat.pcb.refill(pcb)
    final = kat.pcb.check(pcb)
    assert final["clean"], final["by_type"]
    kat.pcb.snapshot(pcb, slug="rev-A", note=f"routed in {time.time() - t0:.0f} s, finished",
                     three_d=False)
    kat.pcb.fab(pcb, step=False, three_d=False)
    assert kat.pcb.ready(pcb)["ready"]
    text = (sch.parent / f"{sch.stem}_REPORT.md").read_text()
    assert text.count(" · batch\n") == 2 and "def structure(b: kat.pcb.Board)" in text
    assert "_structure/top.png" in text and "_rev-A/In1.png" in text
