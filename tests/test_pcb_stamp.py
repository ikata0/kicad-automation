"""Stamping checked block layouts: hierarchical (nested RC -> BLOCK1 -> top) and flat (SKiDL
blocks), judged by KiCad's DRC with schematic parity."""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import cli, hierarchy
from kat.pcb import stamping as stamp_mod
from kat.pcb.board import Board
from kat.pcb.syncer import _set_child
from kat.sexpr import find, find_all

sys.path.insert(0, str(Path(__file__).parent))
from test_hierarchy import build  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
HERE = Path(__file__).parent / "e2e"


def env_board(b, w, h):
    """The environment a block is checked in: 4 layers, its outline, a GND plane on In1."""
    b.set_copper_layers(4)
    b.outline(rect=(100, 100, w, h))
    b.set_layer_type("In1.Cu", "power")
    b.zone("GND", "In1.Cu", name="env GND In1", clearance=0.3)


def layout_rc(pcb: Path, r="R1", c="C1") -> None:
    """R and C on top, the OUT node as a track plus a small pour, C's GND through a via to
    In1, and a keep-out on In1 under the OUT node (no GND pour there)."""
    with kat.pcb.edit(pcb) as b:
        env_board(b, 7, 5)
        b.footprint(r).place(102, 101.5, 0)
        b.footprint(c).place(104.5, 103, 270)
        b.track([f"{r}.2", (104.5, 101.5), f"{c}.1"])
        out = b.footprint(c).pad("1").net
        b.zone(out, "F.Cu", polygon=[(102.4, 101.0), (105.1, 101.0), (105.1, 102.9), (102.4, 102.9)],
               name="out node", priority=1, clearance=0.2)
        b.via_to_plane(f"{c}.2")
        b.keepout("In1.Cu", rect=(102.2, 100.8, 3.1, 2.3), tracks=True, vias=True, pads=True,
                  footprints=True, pour=False, name="no GND under OUT")
    stamp_mod.set_environment(pcb, ["env GND In1"])


def check(pcb: Path) -> dict:
    return kat.pcb.check(pcb)


def groups(pcb: Path) -> list[str]:
    return sorted(str(g[1]) for g in find_all(Board(pcb).tree, "group"))


def counts(pcb: Path) -> dict:
    b = Board(pcb)
    return {"tracks": len(b.tracks()), "vias": len(b.vias()), "zones": len(b.zones()),
            "groups": len(find_all(b.tree, "group")), "footprints": len(b.footprints())}


@pytest.fixture(scope="module")
def hier(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("KAT_REPORT", "0")
    root = tmp_path_factory.mktemp("stamp")
    f = build(root)
    hierarchy.annotate(f["rc"], per_page=0)
    hierarchy.annotate(f["block1"])
    hierarchy.annotate(f["top"])
    rc, b1, top = (f[k].with_suffix(".kicad_pcb") for k in ("rc", "block1", "top"))
    for pcb in (rc, b1, top):
        kat.pcb.sync(pcb, force=True)
    layout_rc(rc)
    with kat.pcb.edit(b1) as b:
        env_board(b, 29, 6)
        stages = [b.stamp(rc, f"/RC{i + 1}", at=(100.5 + 7 * i, 100.5))["pairs"] for i in range(3)]
        b.footprint("R101").place(124, 102, 0)
        for i, (x, y, to) in enumerate(((107.5, 101.5, stages[1]["R1"]), (114.5, 101.5, stages[2]["R1"]),
                                        (121.5, 102, "R101"))):
            b.track([f"{stages[i]['C1']}.1", (x, y), f"{to}.1"], check=False)
    stamp_mod.set_environment(b1, ["env GND In1"])
    with kat.pcb.edit(top) as b:
        b.set_copper_layers(4)
        b.outline(rect=(100, 100, 45, 20))
        b.plane("GND", "In1.Cu")
        b.footprint("J101").place(103, 104.92, 0)
        res = b.stamp(b1, {"/CH_A": (112, 102), "/CH_B": (112, 112)})
    yield {"rc": rc, "b1": b1, "top": top, "stamped": res, "stages": stages}
    mp.undo()


def copy_of(hier: dict, tmp_path: Path) -> dict:
    """The library and the design copied, so a test can change them freely."""
    root = hier["top"].parent.parent
    dst = tmp_path / "copy"
    shutil.copytree(root, dst)
    return {k: dst / hier[k].relative_to(root) for k in ("rc", "b1", "top")}


def test_block_boards_are_clean(hier):
    for pcb in (hier["rc"], hier["b1"]):
        c = check(pcb)
        assert c["drc_errors"] == 0 and c["parity"] == 0, c
    assert "/RC1 (RC)" in groups(hier["b1"])


def test_nested_stamp_gives_a_clean_design_with_only_the_top_links_open(hier):
    res = hier["stamped"]["instances"]
    a = res[0]
    assert a["mode"] == "hierarchical" and a["pairs"]["R101"] == "R201"
    assert a["nets"]["/IN"] == "/IN_A" and a["nets"]["/RC1/OUT"] == "/CH_A/RC1/OUT"
    assert a["skipped_environment"] == 1                       # BLOCK1's env plane
    c = check(hier["top"])
    assert c["drc_errors"] == 0 and c["parity"] == 0, c
    assert c["unconnected"] == 4, c                             # J1 to IN/OUT of both channels
    g = groups(hier["top"])
    assert "/CH_A (BLOCK1)" in g and "/CH_B/RC3 (RC)" in g     # nested groups kept
    names = {str(find(z, "name")[1]) for z in Board(hier["top"]).zones() if find(z, "name")}
    assert "/CH_B/RC2: out node" in names and "/CH_A/RC1: no GND under OUT" in names
    assert not any("env" in n for n in names)
    b = Board(hier["top"])
    assert all(t["locked"] for t in b.tracks()) and all(v["locked"] for v in b.vias())
    assert b.instances(hier["b1"]) == ["/CH_A", "/CH_B"]
    assert b.instances(hier["rc"]) == ["/CH_A/RC1", "/CH_A/RC2", "/CH_A/RC3",
                                       "/CH_B/RC1", "/CH_B/RC2", "/CH_B/RC3"]


def test_restamp_replaces_and_rot180_is_clean(hier, tmp_path):
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    before = counts(pcb)
    with kat.pcb.edit(pcb) as b:
        b.stamp(b1, "/CH_A", at=(112, 102))
    assert counts(pcb) == before
    with kat.pcb.edit(pcb) as b:
        r = b.stamp(b1, "CH_B", at=(141, 118), rot=180)
    assert r["rot"] == 180 and counts(pcb) == before
    c = check(pcb)
    assert c["drc_errors"] == 0 and c["parity"] == 0 and c["unconnected"] == 4, c
    assert Board(pcb).footprint("R601").rot == 180


def test_cli(hier, tmp_path, capsys):
    import json
    c = copy_of(hier, tmp_path)
    assert cli.main(["pcb", "instances", str(c["top"]), str(c["b1"])]) == 0
    assert json.loads(capsys.readouterr().out)["instances"] == ["/CH_A", "/CH_B"]
    before = counts(c["top"])
    # exit 1: the check is not ok yet (J1 is still to be routed), as with `pcb sync`
    assert cli.main(["pcb", "stamp", str(c["top"]), str(c["b1"]), "all", "--at", "112,102",
                     "--step", "0,10"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [i["at"] for i in out["instances"]] == [[112, 102], [112, 112]]
    assert out["check"]["drc_errors"] == 0 and counts(c["top"]) == before


def test_refusals(hier, tmp_path):
    pcb = copy_of(hier, tmp_path)["top"]
    b = Board(pcb)
    with pytest.raises(kat.KatError, match="no part"):
        b.stamp(hier["rc"], "/CH_A", at=(112, 102))                 # wrong block
    with pytest.raises(kat.KatError, match="multiples of 90"):
        b.stamp(hier["b1"], "/CH_A", at=(112, 102), rot=45)
    # miswired: one pad of CH_A's RC2 input on another net
    pad = next(p for p in find_all(b.footprint("C401").node, "pad") if str(p[1]) == "1")  # CH_A/RC2
    _set_child(pad, "net", "/OUT_A")
    with pytest.raises(kat.KatError, match="wired differently"):
        b.stamp(hier["b1"], "/CH_A", at=(112, 102))
    # a design without the inner layer the block uses
    two = Board(pcb)
    two.tree[:] = [n for n in two.tree if not (isinstance(n, list) and str(n[0]) in
                                               ("segment", "arc", "via", "zone", "group"))]
    two.set_copper_layers(2)
    with pytest.raises(kat.KatError, match="In1.Cu"):
        two.stamp(hier["b1"], "/CH_B", at=(112, 112))


@pytest.mark.skipif(not __import__("kat.pcb.router", fromlist=["x"]).jar_path().exists()
                    or shutil.which("java") is None, reason="needs Freerouting and Java")
def test_router_connects_the_rest_and_keeps_block_copper(hier, tmp_path):
    from kat.pcb import router
    pcb = copy_of(hier, tmp_path)["top"]
    before = {(t["start"], t["end"], t["layer"]) for t in Board(pcb).tracks()}
    res = router.route(pcb, out=tmp_path / "route")
    assert res["check"]["clean"] and res["check"]["unconnected"] == 0, res["check"]
    after = {(t["start"], t["end"], t["layer"]) for t in Board(pcb).tracks()}
    assert before <= after and res["added_tracks"] > 0


# --- flat: SKiDL blocks, parts paired by Block + tag --------------------------------------

@pytest.mark.skipif(importlib.util.find_spec("skidl") is None, reason="needs skidl")
def test_flat_stamp_by_block_and_tag(tmp_path, monkeypatch):
    monkeypatch.setenv("KAT_REPORT", "0")
    nets = {}
    for v in ("base", "rc"):
        nets[v] = tmp_path / f"{v}.net"
        subprocess.run([sys.executable, str(HERE / "blocks_design.py"), v, str(nets[v])],
                       check=True, capture_output=True, cwd=tmp_path)
    boards = {}
    for v in ("base", "rc"):
        sch = tmp_path / v / f"{v}.kicad_sch"
        assert cli.main(["new", str(sch)]) == 0
        kat.sync(sch, nets[v])
        boards[v] = sch.with_suffix(".kicad_pcb")
        kat.pcb.sync(boards[v], force=True)
    rc = boards["rc"]
    with kat.pcb.edit(rc) as b:
        env_board(b, 14, 10)
        b.footprint("J1").place(111, 101.46, 0)     # the harness: environment, not stamped
    layout_rc(rc)                                   # R1/C1 = DUT/r, DUT/c
    with kat.pcb.edit(rc) as b:                     # layout_rc sized outline and plane to 7x5
        env_board(b, 14, 10)
    c = check(rc)
    assert c["drc_errors"] == 0, c
    top = boards["base"]
    with kat.pcb.edit(top) as b:
        b.set_copper_layers(4)
        b.outline(rect=(100, 100, 60, 30))
        b.plane("GND", "In1.Cu")
        found = b.instances(rc)
        assert found == ["/CH_A/RC1", "/CH_A/RC2", "/CH_A/RC3", "/CH_B/RC1", "/CH_B/RC2", "/CH_B/RC3"]
        res = [b.stamp(rc, inst, at=(103 + 9 * (i % 3), 103 + 12 * (i // 3)))
               for i, inst in enumerate(found)]
        j = b.footprint("J1")
        j.place(160 - 5, 110, 0)
    assert all(r["mode"] == "flat" and r["footprints"] == 2 for r in res)
    assert res[0]["nets"]["/IN"] == "/IN_A"
    c = check(top)
    assert c["drc_errors"] == 0 and c["parity"] == 0, c
    assert "/CH_B/RC2 (rc)" in groups(top)
