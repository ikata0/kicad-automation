"""End to end with blocks: a library (RC, and CH = 3x RC + an output resistor), one design
drawn flat and the same design drawn with sheets; both boards get the CH layout stamped,
the rest routed, and must come out equally clean.

Set KAT_E2E_OUT=/some/dir to keep the projects for review.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import blocks, cli, netlist
from kat.pcb.board import Board

sys.path.insert(0, str(Path(__file__).parent))
from test_blocks import RC_PY  # noqa: E402
from test_pcb_stamp import check, env_board, layout_rc  # noqa: E402
from test_sync_hier import CH_PY, TOP_PY  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.skipif(
    shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
    reason="needs kicad-cli and skidl")]


def router_ready() -> bool:
    from kat.pcb import router
    return router.jar_path().exists() and shutil.which("java") is not None


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    out = Path(os.environ["KAT_E2E_OUT"]) if os.environ.get("KAT_E2E_OUT") else tmp_path_factory.mktemp("e2e")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    mp = pytest.MonkeyPatch()
    mp.setenv("KAT_REPORT", "0")
    mp.chdir(out)
    lib = out / "blocks"
    # RC: laid out once
    blocks.new("RC")
    (lib / "RC" / "RC.py").write_text(RC_PY)
    blocks.build("RC", force=True)
    rc_pcb = lib / "RC" / "RC.kicad_pcb"
    with kat.pcb.edit(rc_pcb) as b:
        env_board(b, 14, 10)
        b.footprint("J1").place(111, 101.46, 0)
    layout_rc(rc_pcb)
    with kat.pcb.edit(rc_pcb) as b:
        env_board(b, 14, 10)
    assert blocks.check("RC", require=())["drc_errors"] == 0
    # CH: three RC layouts stamped, the output resistor and the stage tracks
    blocks.new("CH")
    (lib / "CH" / "CH.py").write_text(CH_PY)
    blocks.build("CH", force=True)
    ch_pcb = lib / "CH" / "CH.kicad_pcb"
    with kat.pcb.edit(ch_pcb) as b:
        env_board(b, 36, 10)
        stages = [b.stamp("RC", f"/DUT/RC{i + 1}", at=(100.5 + 7 * i, 100.5))["pairs"] for i in range(3)]
        rout = next(f.ref for f in b.footprints() if f.field("SKiDL Tag") == "DUT/rout")
        b.footprint(rout).place(124, 102, 0)
        for i, (x, y, to) in enumerate(((107.5, 101.5, stages[1]["R1"]), (114.5, 101.5, stages[2]["R1"]),
                                        (121.5, 102, rout))):
            b.track([f"{stages[i]['C1']}.1", (x, y), f"{to}.1"], check=False)
        b.footprint("J1").place(132, 101.46, 0)
    kat.pcb.block_environment(ch_pcb, ["env GND In1"])
    ch = blocks.check("CH", require=())
    assert ch["drc_errors"] == 0 and ch["unconnected"] == 0, ch
    # the design script
    d = out / "design"
    d.mkdir()
    (d / "design.py").write_text(TOP_PY)
    subprocess.run([sys.executable, "design.py"], cwd=d, check=True, capture_output=True)
    yield {"out": out, "lib": lib, "net": d / "design.net"}
    mp.undo()


def board(sch: Path, net: Path) -> Path:
    pcb = sch.with_suffix(".kicad_pcb")
    kat.pcb.sync(pcb, force=True)
    comps, _ = netlist.read_design(net)
    j = next(c.ref for c in comps.values() if c.tag == "j")
    with kat.pcb.edit(pcb) as b:
        b.set_copper_layers(4)
        b.outline(rect=(100, 100, 50, 24))
        b.plane("GND", "In1.Cu")
        assert b.instances("CH") == ["/CH_A", "/CH_B"]
        res = b.stamp("CH", {"/CH_A": (112, 102), "/CH_B": (112, 113)})["instances"]
        b.footprint(j).place(103, 106.92, 0)
    assert all(r["footprints"] == 7 for r in res), res
    return pcb


def test_flat_and_sheets_give_the_same_clean_boards(work):
    net = work["net"]
    # A: flat, every RC drawn like the block, channels framed
    a = work["out"] / "flat" / "flat.kicad_sch"
    assert cli.main(["new", str(a)]) == 0
    kat.sync(a, net)
    comps, _ = netlist.read_design(net)
    with kat.edit(a) as s:                       # the connector out of the blocks' way
        s.move(next(c.ref for c in comps.values() if c.tag == "j"), to=(203.2, 101.6))
    for row, ch in enumerate(("CH_A", "CH_B")):      # one row per channel
        res = kat.stamp(a, "RC", [f"/{ch}/RC{i}" for i in (1, 2, 3)], at=(38.1, 38.1 + 50.8 * row),
                        step=(33.02, 0), design=net, frame=False, make_room=True)
        assert len(res["instances"]) == 3
    by_tag = {c.tag: c.ref for c in comps.values()}
    with kat.edit(a) as s:
        for row, ch in enumerate(("CH_A", "CH_B")):
            s.move(by_tag[f"{ch}/rout"], to=(142.24, 50.8 + 50.8 * row))
            s.frame(ch)
    assert kat.check(a, net)["netlist_vs_expected"]["match"]
    # B: sheets from the library
    b = work["out"] / "sheets" / "sheets.kicad_sch"
    assert cli.main(["new", str(b)]) == 0
    assert kat.sync(b, net, sheets=["ch", "rc"])["check"]["ok"]

    boards = {name: board(sch, net) for name, sch in (("flat", a), ("sheets", b))}
    counts = {}
    for name, pcb in boards.items():
        c = check(pcb)
        assert c["drc_errors"] == 0 and c["parity"] == 0 and c["unconnected"] == 4, (name, c)
        bb = Board(pcb)
        counts[name] = (len(bb.tracks()), len(bb.vias()), len(bb.zones()))
    assert counts["flat"] == counts["sheets"]
    if router_ready():
        from kat.pcb import router
        for name, pcb in boards.items():
            r = router.route(pcb, out=pcb.parent / "route")
            assert r["check"]["clean"] and r["check"]["unconnected"] == 0, (name, r["check"])
