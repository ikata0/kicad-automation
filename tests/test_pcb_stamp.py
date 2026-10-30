"""Stamping checked block layouts: hierarchical (nested RC -> BLOCK1 -> top) and flat (SKiDL
blocks), judged by KiCad's DRC with schematic parity."""

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
from kat.sexpr import find, find_all, head

sys.path.insert(0, str(Path(__file__).parent))
from test_hierarchy import build  # noqa: E402

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints")
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


def test_parts_in_the_way_are_refused_or_moved(hier, tmp_path):
    """I10: a top-level part where the block's parts go: refused, or with make_room
    pushed aside; the result is DRC clean."""
    pcb = copy_of(hier, tmp_path)["top"]
    r = Board(pcb).footprint("R201")                             # CH_A's R101
    with kat.pcb.edit(pcb) as b:
        b.footprint("J101").place(r.x, r.y, 0)
    with pytest.raises(kat.KatError, match="overlap J101"):
        with kat.pcb.edit(pcb) as b:
            b.stamp(hier["b1"], "/CH_A", at=(112, 102))
    with kat.pcb.edit(pcb) as b:
        res = b.stamp(hier["b1"], "/CH_A", at=(112, 102), make_room=True)
    assert [m["ref"] for m in res["made_room"]] == ["J101"]
    assert not [c for c in Board(pcb).collisions() if "J101" in (c["a"], c["b"])]


def test_net_classes_stricter_than_the_blocks_are_refused(hier, tmp_path):
    """I4: the block was routed with 0.2 mm clearance; a design class of 0.3 on its port
    nets would make the stamped copper DRC errors. Another width is only a warning (DRC
    checks a class's clearance, not its width)."""
    pcb = copy_of(hier, tmp_path)["top"]
    with kat.pcb.edit(pcb) as b:
        b.netclass("WIDE", track=0.25, clearance=0.3, nets=["/IN_A"])
    with pytest.raises(kat.KatError, match=r"/IN_A \(WIDE: clearance 0.3 > block 0.2\)"):
        with kat.pcb.edit(pcb) as b:
            b.stamp(hier["b1"], "/CH_A", at=(112, 102))
    with kat.pcb.edit(pcb) as b:
        b.netclass("WIDE", track=0.3, clearance=0.2, nets=["/IN_A"])
        res = b.stamp(hier["b1"], "/CH_A", at=(112, 102))
    assert any("differ" in w and "/IN_A" in w for w in res["warnings"]), res
    assert check(pcb)["drc_errors"] == 0


@pytest.mark.requires("routing")
def test_router_connects_the_rest_and_keeps_block_copper(hier, tmp_path):
    from kat.pcb import router
    pcb = copy_of(hier, tmp_path)["top"]
    before = {(t["start"], t["end"], t["layer"]) for t in Board(pcb).tracks()}
    res = router.route(pcb, out=tmp_path / "route")
    assert res["check"]["clean"] and res["check"]["unconnected"] == 0, res["check"]
    after = {(t["start"], t["end"], t["layer"]) for t in Board(pcb).tracks()}
    assert before <= after and res["added_tracks"] > 0


# --- flat: SKiDL blocks, parts paired by Block + tag --------------------------------------

@pytest.mark.requires("skidl")
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


# --- the bottom side: flipped block, layer map ----------------------------------------------

def zone_layer(z) -> str:
    return str((find(z, "layer") or find(z, "layers"))[1])


def test_bottom_stamp_is_clean_mirrored_and_kept_on_restamp(hier, tmp_path):
    """CH_B flipped under the board: parts on B, copper F.Cu -> B.Cu, In1 keep-out -> In2
    (auto map, 4 layers, KiCad's default stack-up is symmetric), outline mirrored so
    ``at`` is the flipped block's top-left corner. DRC and parity judge it; stamping
    again without --side keeps it on the bottom."""
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    before = counts(pcb)
    src = Board(b1)
    box = src.outline_box()
    with kat.pcb.edit(pcb) as b:
        r = b.stamp(b1, "/CH_B", at=(112, 112), side="bottom")
    assert r["side"] == "bottom" and r["layer_map"] == {"F.Cu": "B.Cu", "In1.Cu": "In2.Cu",
                                                         "In2.Cu": "In1.Cu", "B.Cu": "F.Cu"}
    assert counts(pcb) == before
    d = Board(pcb)
    for sref, tref in r["pairs"].items():
        sf, tf = src.footprint(sref), d.footprint(tref)
        assert tf.side == "B"
        assert (tf.x, tf.y) == pytest.approx((112 + box[2] - sf.x, 112 + sf.y - box[1]), abs=1e-3)
        assert tf.rot % 360 == pytest.approx((180 - sf.rot) % 360)
    g = groups(pcb)
    assert "/CH_B (BLOCK1, bottom)" in g and "/CH_A (BLOCK1)" in g
    zones = {str(find(z, "name")[1]): z for z in d.zones() if find(z, "name")}
    assert zone_layer(zones["/CH_B/RC1: out node"]) == "B.Cu"
    assert zone_layer(zones["/CH_B/RC1: no GND under OUT"]) == "In2.Cu"
    ch_b = {t["layer"] for t in d.tracks() if "_B" in t["net"] or t["net"].startswith("/CH_B")}
    assert ch_b == {"B.Cu"}
    res = check(pcb)
    assert res["drc_errors"] == 0 and res["parity"] == 0 and res["unconnected"] == 4, res
    with kat.pcb.edit(pcb) as b:
        again = b.stamp(b1, "/CH_B", at=(112, 112))
    assert again["side"] == "bottom" and counts(pcb) == before
    assert check(pcb)["drc_errors"] == 0
    with kat.pcb.edit(pcb) as b:                    # and back to the top
        top = b.stamp(b1, "/CH_B", at=(112, 112), side="top")
    assert {Board(pcb).footprint(t).side for t in top["pairs"].values()} == {"F"}
    res = check(pcb)
    assert res["drc_errors"] == 0 and res["parity"] == 0 and res["unconnected"] == 4, res


def test_bottom_layer_maps_and_asymmetric_stackups(hier, tmp_path):
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    with kat.pcb.edit(pcb) as b:                    # In2 under the bottom side is thinner
        b.set_stackup([0.1, 1.2, 0.2])
    with pytest.raises(kat.KatError, match=r"not mirror-symmetric.*dielectric 1 vs 3: thickness 0.1 vs 0.2"):
        Board(pcb).stamp(b1, "/CH_B", at=(112, 112), side="bottom")
    with kat.pcb.edit(pcb) as b:
        r = b.stamp(b1, "/CH_B", at=(112, 112), side="bottom", allow_asymmetric=True)
    assert r["layer_map"]["In1.Cu"] == "In2.Cu"
    # explicit: the GND reference stays on In1 (unnamed layers follow auto, bijective)
    with kat.pcb.edit(pcb) as b:
        r = b.stamp(b1, "/CH_B", at=(112, 112), side="bottom", layer_map="L1=L4,In1.Cu=In1.Cu,In2.Cu=In2.Cu")
    assert r["layer_map"] == {"F.Cu": "B.Cu", "B.Cu": "F.Cu"}
    for bad, why in (("F.Cu=F.Cu", "outer layers go to B.Cu"), ("In1.Cu=B.Cu", "not one-to-one|inner layer"),
                     ("In1.Cu=In1.Cu", "not one-to-one")):
        with pytest.raises(kat.KatError, match=why):
            Board(pcb).stamp(b1, "/CH_B", at=(112, 112), side="bottom", layer_map=bad)
    # roles: the block's gnd_ref next to its component side; the design names each layer's
    # role for a block on either side, so the flip works on an asymmetric stack too
    kat.pcb.layer_roles(b1, {"L1": "sig", "L2": "gnd_ref", "L3": "inner", "L4": "back"})
    kat.pcb.layer_roles(pcb, {"L1": "sig_top|back_bottom", "L2": "gnd_ref_top|inner_bottom",
                              "L3": "inner_top|gnd_ref_bottom", "L4": "back_top|sig_bottom"})
    with kat.pcb.edit(pcb) as b:
        r = b.stamp(b1, "/CH_B", at=(112, 112), side="bottom", layer_map="roles")
    assert r["layer_map"] == {"F.Cu": "B.Cu", "In1.Cu": "In2.Cu", "In2.Cu": "In1.Cu", "B.Cu": "F.Cu"}
    kat.pcb.layer_roles(pcb, {"L1": "sig_top", "L2": "gnd_ref", "L3": "gnd_ref", "L4": "sig_bottom"})
    with pytest.raises(kat.KatError, match="role 'gnd_ref': the block has it on In1.Cu, the design on In2.Cu, In1.Cu"):
        Board(pcb).stamp(b1, "/CH_B", at=(112, 112), side="bottom", layer_map="roles")


def test_bottom_overlap_counts_through_hole_pads_on_both_sides(hier, tmp_path):
    """J101 (pin header, on top) where a flipped block part goes: its pins go through."""
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    with kat.pcb.edit(pcb) as b:
        r = b.stamp(b1, "/CH_B", at=(112, 112), side="bottom")
    part = Board(pcb).footprint(r["pairs"]["R101"])
    with kat.pcb.edit(pcb) as b:
        b.footprint("J101").place(part.x, part.y, 90)
    with pytest.raises(kat.KatError, match="overlap J101"):
        Board(pcb).stamp(b1, "/CH_B", at=(112, 112), side="bottom")


def test_layer_rules_refuse_a_flipped_block(hier, tmp_path):
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    kat.pcb.layer_roles(pcb, {"F.Cu": "in_signal_top", "B.Cu": "in_signal_bottom"})
    kat.pcb.layer_rule(pcb, "/CH_*", ["in_signal_top", "In1.Cu", "In2.Cu"])
    with pytest.raises(kat.KatError, match=r"layer rules: track of /CH_B/RC\d/OUT on B.Cu"):
        Board(pcb).stamp(b1, "/CH_B", at=(112, 112), side="bottom")
    kat.pcb.layer_rule(pcb, "/CH_*", ["in_signal_top", "in_signal_bottom", "In1.Cu", "In2.Cu"])
    with kat.pcb.edit(pcb) as b:
        b.stamp(b1, "/CH_B", at=(112, 112), side="bottom")
    assert not check(pcb)["net_rules"]["findings"]


def test_flipped_block_board_is_clean(hier, tmp_path):
    """What `kat block check --sides both` checks: the whole block board turned over."""
    rc = copy_of(hier, tmp_path)["rc"]
    res = stamp_mod.flip_board(rc)
    res["board"].save()
    c = check(rc)
    assert c["drc_errors"] == 0 and c["parity"] == 0, c
    b = Board(rc)
    assert {f.side for f in b.footprints()} == {"B"}
    assert {str(find(z, "name")[1]): zone_layer(z) for z in b.zones()} == {
        "env GND In1": "In2.Cu", "out node": "B.Cu", "no GND under OUT": "In2.Cu"}


def test_blocks_interlock_down_to_the_min_pitch(hier, tmp_path, monkeypatch, capsys):
    """Request 18: copies of RC in a row closer than its outline (7 mm). At the min pitch
    the stamps are accepted and KiCad's DRC agrees (no errors); also alternating sides.
    A bit closer is refused, and forced through, DRC finds the collision."""
    import json
    from kat.pcb import cells
    c = copy_of(hier, tmp_path)
    rc, b1 = c["rc"], c["b1"]
    cell = kat.pcb.block_cell(rc)
    assert cell["size"] == [7, 5] and cell["layers"]["In1.Cu"]["keepout"] == [2.2, 0.8, 5.3, 3.1]
    assert cli.main(["pcb", "cell", str(rc), "--min-pitch", "x", "--save"]) == 0
    px = json.loads(capsys.readouterr().out)
    assert 3 < px["pitch"] < 7 and px["interlock"] > 0 and px["limited_by"], px
    assert kat.pcb.block_cell(rc)["pitch"] == [px["pitch"], 0]
    with kat.pcb.edit(b1) as b:                  # BLOCK1 without its inter-stage wiring
        b.tree[:] = [n for n in b.tree if not (head(n) == "segment" and find(n, "locked") is None)]
    res = kat.pcb.stamp(b1, rc, ["/RC1", "/RC2", "/RC3"], at=(100.5, 100.5))     # step: the pitch
    assert [i["at"][0] for i in res["instances"]] == pytest.approx([100.5 + k * px["pitch"] for k in range(3)])
    assert res["check"]["drc_errors"] == 0, check(b1)
    assert kat.pcb.stamp_collisions(b1)["ok"]
    with pytest.raises(kat.KatError, match=r"/RC2 collides with stamped instances: .*courtyard"):
        Board(b1).stamp(rc, "/RC2", at=(100.5 + px["pitch"] - 0.1, 100.5))

    alt = kat.pcb.min_pitch(rc, "x", side="alternate")
    assert alt["pitch"] < px["pitch"]
    with kat.pcb.edit(b1) as b:
        b.stamp(rc, {"/RC1": (100.5, 100.5), "/RC2": ((100.5 + alt["pitch"], 100.5), 0, "bottom"),
                     "/RC3": (100.5 + 2 * alt["pitch"], 100.5)})
    assert check(b1)["drc_errors"] == 0
    assert kat.pcb.stamp_collisions(b1)["ok"]

    monkeypatch.setattr(cells, "against", lambda *a, **k: [])          # forced too close
    with kat.pcb.edit(b1) as b:
        b.stamp(rc, {"/RC1": (100.5, 100.5), "/RC2": ((100.5 + px["pitch"] - 0.1, 100.5), 0, "top"),
                     "/RC3": (100.5 + 2 * px["pitch"], 100.5)})
    rows = kat.pcb.stamp_collisions(b1)["collisions"]
    assert {(r["i"], r["j"], r["kind"]) for r in rows} >= {("/RC1", "/RC2", "courtyard")}, rows
    assert check(b1)["drc_errors"] > 0


def test_coupling_rule_estimates_the_crosstalk_capacitance(tmp_path, monkeypatch):
    """2 mm of OUT1 over IN2 through 0.1 mm (6 layers, 0.1/0.5/0.15/0.5/0.1): ≈ 0.2 pF;
    the own channel's IN1 under OUT1 does not count, nor does IN4 two layers away."""
    monkeypatch.setenv("KAT_REPORT", "0")
    pcb = tmp_path / "c.kicad_pcb"
    b = Board.create(pcb)
    b.set_copper_layers(6)
    b.outline(rect=(0, 0, 20, 10))
    b.set_stackup([0.1, 0.5, 0.15, 0.5, 0.1])
    b.add_track((2, 2), (8, 2), 0.25, "F.Cu", "OUT1")
    b.add_track((6, 2), (12, 2), 0.25, "In1.Cu", "IN2")
    b.add_track((2, 2), (5, 2), 0.25, "In1.Cu", "IN1")
    b.add_track((2, 2.45), (4, 2.45), 0.2, "F.Cu", "IN3")
    b.add_track((9, 2), (12, 2), 0.25, "In2.Cu", "IN4")
    b.save()
    assert kat.pcb.stack(pcb)["asymmetry"] == []
    kat.pcb.coupling_rule(pcb, "OUT*", "IN*", distance=0.3)
    kat.pcb.plane_rule(pcb, "IN*")
    res = check(pcb)
    f = {(x["kind"], x.get("b") or x.get("net")): x for x in res["net_rules"]["findings"]}
    assert set(f) == {("coupling", "IN2"), ("coupling", "IN3"), ("plane", "IN1"), ("plane", "IN2"),
                      ("plane", "IN3"), ("plane", "IN4")}
    over = f[("coupling", "IN2")]
    assert over["layers"] == ["F.Cu", "In1.Cu"] and over["dielectric"] == 0.1
    assert 0.5 <= over["area"] <= 0.62 and 2.0 <= over["length"] <= 2.3
    assert over["capacitance_pf"] == pytest.approx(8.854e-3 * 4.5 * over["area"] / 0.1, rel=1e-3)
    assert f[("coupling", "IN3")]["distance"] == pytest.approx(0.225, abs=1e-3)
    assert not res["clean"]
    assert cli.main(["pcb", "netrule", str(pcb), "clear"]) == 0
    assert "net_rules" not in check(pcb)
