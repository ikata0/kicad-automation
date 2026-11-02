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
    with pytest.raises(kat.KatError, match="multiples of 45"):
        b.stamp(hier["b1"], "/CH_A", at=(112, 102), rot=30)
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


def test_stamp_bounds_and_anchor(hier, tmp_path):
    """FR-010: the turned outline as worked out by hand for the 29 x 6 mm block, the same from
    the preview and the stamp; --anchor top-left puts that corner at `at`, and stays recorded."""
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    b = Board(pcb)
    expect = {0: (112, 102, 141, 108), 90: (112, 73, 118, 102), 180: (83, 96, 112, 102),
              270: (106, 102, 112, 131)}
    for rot, outline in expect.items():
        got = b.stamp_preview(b1, (112, 102), rot)
        assert got["outline"] == pytest.approx(outline) and got["origin"] == [112, 102], rot
        assert got["anchor"] == "origin" and got["instance"] is None
        turned = b.stamp_preview(b1, (112, 102), rot, anchor="top-left")
        assert turned["outline"][:2] == pytest.approx([112, 102]), rot
        assert turned["outline"][2] - turned["outline"][0] == pytest.approx(outline[2] - outline[0])
    flipped = b.stamp_preview(b1, (112, 102), 90, side="bottom")
    assert flipped["outline"] == pytest.approx((112, 73, 118, 102)) and flipped["side"] == "bottom"
    with pytest.raises(kat.KatError, match="anchor 'x'"):
        b.stamp_preview(b1, (112, 102), anchor="x")

    with kat.pcb.edit(pcb) as e:
        r = e.stamp(b1, "/CH_B", at=(112, 112), rot=270, anchor="top-left")
    assert r["outline"] == pytest.approx((112, 112, 118, 141)) and r["origin"] == pytest.approx((118, 112))
    placed = [Board(pcb).footprint(x) for x in r["pairs"].values()]
    assert all(r["outline"][0] <= f.x <= r["outline"][2] and r["outline"][1] <= f.y <= r["outline"][3]
               for f in placed)
    with kat.pcb.edit(pcb) as e:                                # recorded, like side
        again = e.stamp(b1, "/CH_B", at=(112, 112), rot=270)
    assert again["anchor"] == "top-left" and again["outline"] == r["outline"]
    with kat.pcb.edit(pcb) as e:
        back = e.stamp(b1, "/CH_B", at=(118, 112), rot=270, anchor="origin")
    assert back["origin"] == [118, 112] and back["outline"] == pytest.approx(r["outline"])


def test_preview_tells_ports_without_changing_the_board(hier, tmp_path, capsys):
    """FR-014: ports where the stamp will put them, mirrored on the bottom side, in order per
    edge, with the file untouched; with an instance their nets are the instance's."""
    import json
    c = copy_of(hier, tmp_path)
    pcb, rc, b1 = c["top"], c["rc"], c["b1"]
    via = next(v for v in Board(rc).vias())
    pad = Board(rc).footprint("R1").pad("1")
    stamp_mod.set_port(rc, "gnd", via["at"])
    stamp_mod.set_port(rc, "inp", (pad.x, pad.y))
    box = Board(rc).outline_box()
    before = pcb.read_bytes(), stamp_mod.stamps_file(pcb).read_bytes()
    assert cli.main(["pcb", "stamp", str(pcb), str(rc), "/CH_A/RC1", "--at", "120,110", "--preview"]) == 0
    top = json.loads(capsys.readouterr().out)["instances"][0]
    assert (pcb.read_bytes(), stamp_mod.stamps_file(pcb).read_bytes()) == before
    assert top["ports"]["inp"]["x"] == pytest.approx(120 + pad.x - box[0])
    assert top["ports"]["inp"]["net"] == "/IN_A"                # the instance's net
    assert top["ports"]["gnd"]["net"] == "GND"
    assert cli.main(["pcb", "stamp", str(pcb), str(rc), "--at", "120,110", "--side", "bottom", "--preview"]) == 0
    bottom = json.loads(capsys.readouterr().out)["instances"][0]
    assert bottom["instance"] is None and bottom["ports"]["inp"]["net"] == "/IN"     # the block's net
    assert bottom["ports"]["inp"]["x"] == pytest.approx(120 + box[2] - pad.x)        # mirrored
    assert bottom["ports"]["inp"]["y"] == pytest.approx(top["ports"]["inp"]["y"])
    assert cli.main(["pcb", "stamp", str(pcb), str(rc), "--at", "120,110"]) == 2     # stamping needs a name
    assert "error" in json.loads(capsys.readouterr().out)
    # what the stamp then does is what the preview said
    with kat.pcb.edit(pcb) as e:
        e.tree[:] = [n for n in e.tree if not (head(n) == "group" and str(n[1]).startswith("/CH_A"))]
    b = Board(pcb)
    want = b.stamp_preview(rc, (120, 110), 90, "/CH_A/RC1")
    done = b.stamp(rc, "/CH_A/RC1", at=(120, 110), rot=90)
    for k in ("origin", "outline", "ports"):
        assert want[k] == done[k], k


def test_port_order_per_edge():
    """Names follow the way an edge faces once placed; lists run left to right, sides top to
    bottom; a flip reverses a row, a turn moves it to another edge."""
    box = (0, 0, 10, 4)
    src = {"a": (2, 0), "b": (8, 0), "c": (10, 2), "d": (5, 4)}

    def order(rot, bottom=False):
        f = stamp_mod._Place(box, (100, 100), rot, bottom)
        ports = {n: dict(zip("xy", f(*p))) for n, p in src.items()}
        return stamp_mod.port_order(box, f, ports)

    assert order(0) == {"top": ["a", "b"], "right": ["c"], "bottom": ["d"]}
    assert order(0, True) == {"top": ["b", "a"], "left": ["c"], "bottom": ["d"]}
    assert order(90) == {"left": ["b", "a"], "top": ["c"], "right": ["d"]}
    assert order(180) == {"bottom": ["b", "a"], "left": ["c"], "top": ["d"]}
    assert order(270) == {"right": ["a", "b"], "bottom": ["c"], "left": ["d"]}
    assert order(45) == {"top-left": ["a", "b"], "top-right": ["c"], "bottom-right": ["d"]}


def layout_of(pcb: Path) -> dict:
    """What a stamp decided, without the fresh uuids."""
    b = Board(pcb)
    return {"parts": sorted((f.ref, f.x, f.y, f.rot, f.side) for f in b.footprints()),
            "tracks": sorted((t["start"], t["end"], t["layer"], t["net"]) for t in b.tracks()),
            "vias": sorted((v["at"], v["net"]) for v in b.vias()),
            "groups": groups(pcb)}


def test_batch_swap_does_not_depend_on_the_order_named(hier, tmp_path):
    """FR-016: two stamped instances swap places. Judged by their final geometry, in either
    named order, with the same board as the outcome, and DRC agrees."""
    results = []
    for k, names in enumerate((("/CH_A", "/CH_B"), ("/CH_B", "/CH_A"))):
        pcb, b1 = (c := copy_of(hier, tmp_path / str(k)))["top"], c["b1"]
        spots = {"/CH_A": (112, 112), "/CH_B": (112, 102)}
        with kat.pcb.edit(pcb) as b:
            res = b.stamp(b1, {n: spots[n] for n in names})
        assert res["order"] == ["/CH_A", "/CH_B"], res["order"]
        assert [i["instance"] for i in res["instances"]] == res["order"]
        results.append(layout_of(pcb))
        ch = check(pcb)
        assert ch["drc_errors"] == 0 and ch["parity"] == 0, ch["by_type"]
        assert kat.pcb.stamp_collisions(pcb)["ok"]
    assert results[0] == results[1]


def test_batch_refusal_lists_every_collision_and_changes_nothing(hier, tmp_path):
    """FR-016: three copies on one spot: all three pairs named, whichever way round they were
    named, and the board in memory and on disk as it was."""
    from kat.sexpr import dumps
    c = copy_of(hier, tmp_path)
    b1, rc = c["b1"], c["rc"]
    with kat.pcb.edit(b1) as b:                 # BLOCK1 without its inter-stage wiring
        b.tree[:] = [n for n in b.tree if not (head(n) == "segment" and find(n, "locked") is None)]
    on_disk = b1.read_bytes()
    texts = set()
    for names in (("/RC1", "/RC2", "/RC3"), ("/RC3", "/RC1", "/RC2")):
        b = Board(b1)
        before = dumps(b.tree)
        with pytest.raises(kat.KatError) as e:
            b.stamp(rc, {n: (100.5, 100.5) for n in names})
        texts.add(str(e.value))
        assert dumps(b.tree) == before and not b._stamps
    assert len(texts) == 1, texts
    text = texts.pop()
    assert "/RC2, /RC3 collide with stamped instances" in text
    for pair in ("/RC2 / /RC1", "/RC3 / /RC1", "/RC3 / /RC2"):
        assert pair in text, text
    assert "stamped in the order /RC1, /RC2, /RC3" in text
    assert b1.read_bytes() == on_disk
    with pytest.raises(kat.KatError, match="named twice"):
        Board(b1).stamp(rc, {"/RC1": (100.5, 100.5), "RC1": (110, 100.5)})
    with pytest.raises(kat.KatError, match="/RC2 collides with stamped instances"):     # one alone
        Board(b1).stamp(rc, "/RC2", at=Board(b1)._stamp_record("/RC1")["at"])


def test_batch_make_room_does_not_depend_on_the_order_named(hier, tmp_path):
    """FR-016: the parts pushed aside are the same however the instances are named."""
    moved = []
    for k, names in enumerate((("/CH_A", "/CH_B"), ("/CH_B", "/CH_A"))):
        pcb, b1 = (c := copy_of(hier, tmp_path / str(k)))["top"], c["b1"]
        r = Board(pcb).footprint("R201")
        with kat.pcb.edit(pcb) as b:
            b.footprint("J101").place(r.x, r.y, 0)
            res = b.stamp(b1, {n: {"/CH_A": (112, 102), "/CH_B": (112, 112)}[n] for n in names}, make_room=True)
        moved.append([i.get("made_room") for i in res["instances"]])
    assert moved[0] == moved[1] and moved[0][0]


def turned_design(hier, tmp_path):
    """A copy of the design with room for a block at 45 degrees."""
    c = copy_of(hier, tmp_path)
    with kat.pcb.edit(c["top"]) as e:
        e.outline(rect=(60, 60, 140, 100))
        e.plane("GND", "In1.Cu")
    return c


@pytest.mark.parametrize("side,rot", [("top", 45), ("bottom", 45), ("top", 135), ("bottom", 315)])
def test_45_degree_stamp_is_where_kicad_says(hier, tmp_path, side, rot):
    """FR-015: pads against KiCad's IPC-D-356, worked out from the block board by hand; DRC
    finds no error and the footprints still equal their library copies; ports turn too."""
    import math
    from kat.pcb.geometry import rotate
    from test_pcb_geometry import ipc356
    c = turned_design(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    src = Board(b1)
    x0, y0, x1, _ = src.outline_box()
    at = (165, 125)
    with kat.pcb.edit(pcb) as e:
        r = e.stamp(b1, "/CH_B", at=at, rot=rot, side=side)
    assert r["outline"] == pytest.approx(Board(pcb).stamp_preview(b1, at, rot, "/CH_B", side)["outline"])
    kicad = ipc356(pcb)
    checked = 0
    for sref, tref in r["pairs"].items():
        for p in src.footprint(sref).pads():
            lx = x1 - p.x if side == "bottom" else p.x - x0
            dx, dy = rotate(lx, p.y - y0, rot)
            want = (at[0] + dx, at[1] + dy)
            got = kicad[(tref, p.number)]
            assert math.dist(want, got) < 0.005, (sref, p.number, want, got)
            checked += 1
    assert checked == 14
    res = check(pcb)
    assert res["drc_errors"] == 0 and res["parity"] == 0, res["by_type"]
    assert not {"lib_footprint_mismatch", "lib_footprint_issues"} & set(res["by_type"]), res["by_type"]
    assert res["unconnected"] == 4 and res["outside_outline"] == [], res["unconnected"]      # as at 0 degrees
    assert all(f.rot % 90 == pytest.approx(45) for f in map(Board(pcb).footprint, r["pairs"].values())
               if f.ref.startswith("R")), r["pairs"]


@pytest.mark.requires("routing")
def test_a_45_degree_stamp_can_be_routed(hier, tmp_path):
    """FR-015: turned pads reach the router and its tracks connect them; judged by DRC. The
    picture of the result is left in the design's folder for review (KAT_E2E_OUT)."""
    import os
    c = turned_design(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    with kat.pcb.edit(pcb) as e:
        e.stamp(b1, "/CH_B", at=(165, 125), rot=45)
    res = kat.pcb.route(pcb, passes=6, timeout=300)
    assert res["check"]["drc_errors"] == 0 and res["check"]["unconnected"] == 0, res["check"]
    if out := os.environ.get("KAT_E2E_OUT"):
        kat.pcb.view(pcb, region=(100, 95, 90, 60), layers="top", out=Path(out) / "stamp_45.png")


def test_a_board_rectangle_and_text_box_turn_as_polygons(hier, tmp_path):
    """FR-015: gr_rect cannot be turned by 45 degrees; KiCad keeps the corners we write."""
    import math
    from kat.sexpr import loads
    from kat.pcb.geometry import rotate
    c = turned_design(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    with kat.pcb.edit(b1) as e:
        x0, y0, _, _ = e.outline_box()
        e._insert(loads(f'''(gr_rect (start {x0 + 1} {y0 + 1}) (end {x0 + 5} {y0 + 3}) (stroke (width 0.1) (type solid))
            (fill no) (layer "F.Fab") (uuid "aaaaaaaa-1111-4111-8111-111111111111"))'''))
        e._insert(loads(f'''(gr_text_box "tb" (start {x0 + 6} {y0 + 1}) (end {x0 + 9} {y0 + 3}) (margins 0.1 0.1 0.1 0.1)
            (layer "F.Fab") (uuid "bbbbbbbb-1111-4111-8111-111111111111")
            (effects (font (size 0.5 0.5) (thickness 0.1)) (justify left top)) (border yes)
            (stroke (width 0.1) (type solid)))'''))
    with kat.pcb.edit(pcb) as e:
        e.stamp(b1, "/CH_B", at=(130, 120), rot=45)
    b = Board(pcb)
    polys = [n for n in b.tree if head(n) == "gr_poly" and str(find(n, "layer")[1]) == "F.Fab"]
    assert len(polys) == 1                                       # /CH_B's copy; /CH_A was not stamped again
    corners = [(1, 1), (5, 1), (5, 3), (1, 3)]
    want = [(130 + d[0], 120 + d[1]) for d in (rotate(x, y, 45) for x, y in corners)]
    got = [[(float(q[1]), float(q[2])) for q in find_all(find(n, "pts"), "xy")] for n in polys]
    assert any(all(math.dist(w, g) < 1e-3 for w, g in zip(want, poly)) for poly in got), got
    boxes = [n for n in b.tree if head(n) == "gr_text_box" and find(n, "pts") is not None]
    assert boxes and float(find(boxes[0], "angle")[1]) == 45
    assert check(pcb)["drc_errors"] == 0


def test_pours_of_different_nets_must_not_overlap(hier, tmp_path):
    """Found with FR-015: two stamped pours on one layer with equal priority and different nets
    are a DRC error (zones_intersect), so they collide, at any angle; the same net or a
    different priority makes it fine."""
    from kat.pcb import cells
    from kat.sexpr import loads

    def pour(net, priority=1, x=0.0):
        z = loads(f'''(zone (net "{net}") (layer "F.Cu") (uuid "11111111-1111-4111-8111-111111111111")
            (name "p") (priority {priority}) (hatch edge 0.5) (connect_pads (clearance 0.2))
            (min_thickness 0.25) (fill yes) (polygon (pts (xy {x} 0) (xy {x + 2} 0) (xy {x + 2} 2) (xy {x} 2))))''')
        return cells.items([z], ["F.Cu", "B.Cu"])

    cl = lambda a, b: 0.2
    assert cells.clashes(pour("A"), pour("B", x=1), cl)[0]["kind"] == "pour"
    assert "pours overlap" in cells.text(cells.clashes(pour("A"), pour("B", x=1), cl)[0])
    assert not cells.clashes(pour("A"), pour("B", x=3), cl)      # apart
    assert not cells.clashes(pour("A"), pour("A", x=1), cl)      # one net
    assert not cells.clashes(pour("A", 1), pour("B", 2, x=1), cl)    # priorities decide
    assert cells.clashes(pour("A"), pour("A", x=1), cl, same_ok=lambda n: False)     # copies of a block's net

    rc = copy_of(hier, tmp_path)["rc"]
    assert "pour" in {i.kind for i in cells.block_items(rc)["items"]}
    assert "F.Cu" in kat.pcb.block_cell(rc)["layers"]            # a block's cell still reports


def test_turned_blocks_interlock_by_their_real_shapes(hier, tmp_path, monkeypatch):
    """FR-015: two RC blocks at 45 degrees stepped across their long axis. The stamp is accepted
    where the courtyards' bounding boxes overlap, and DRC is clean a little further out: the
    check does not fill pours, so a neighbour can still starve a thermal spoke there. Forced
    closer than the check allows, both object."""
    from kat.pcb import cells
    from kat.pcb.geometry import overlap
    c = copy_of(hier, tmp_path)
    b1, rc = c["b1"], c["rc"]
    with kat.pcb.edit(b1) as e:
        e.outline(rect=(60, 60, 120, 100))
        e.tree[:] = [n for n in e.tree if not (head(n) == "segment" and find(n, "locked") is None)]

    def place(d: float) -> dict:
        return {"/RC1": ((120, 120), 45), "/RC2": ((120 + d, 120 + d), 45), "/RC3": ((90, 90), 0)}

    def boxes_overlap(res) -> bool:
        b = Board(b1)
        a = [b.footprint(r).courtyard() for r in res["instances"][0]["pairs"].values()]
        n = [b.footprint(r).courtyard() for r in res["instances"][1]["pairs"].values()]
        return any(overlap(x, y) for x in a for y in n)

    free = None
    for k in range(1, 60):
        d = k * 0.1
        try:
            Board(b1).stamp(rc, place(d))
        except kat.KatError as e:
            assert "collide" in str(e)
            continue
        free = d
        break
    assert free is not None
    clean = None
    for k in range(12):
        d = free + k * 0.1
        with kat.pcb.edit(b1) as e:
            res = e.stamp(rc, place(d))
        if check(b1)["drc_errors"] == 0:
            clean = d
            break
    assert clean is not None and boxes_overlap(res), (free, clean)
    assert kat.pcb.stamp_collisions(b1)["ok"]
    monkeypatch.setattr(cells, "against", lambda *a, **k: [])    # forced closer: both object
    with kat.pcb.edit(b1) as e:
        e.stamp(rc, place(free - 0.3))
    assert check(b1)["drc_errors"] > 0
    assert kat.pcb.stamp_collisions(b1)["collisions"]


def guide_holes(b, ref, at, spacing, drill=1.0):
    """A board-only part of two distant holes and no courtyard (a connector's guide pegs)."""
    from copy import deepcopy
    from kat.sexpr import num
    f = b.add_board_part("MountingHole:MountingHole_3.2mm_M3", ref, at)
    f.node[:] = [c for c in f.node if head(c) != "fp_circle"]
    pad = find(f.node, "pad")
    for key, vals in (("size", (drill, drill)), ("drill", (drill,))):
        find(pad, key)[1:] = [num(v) for v in vals]
    second = deepcopy(pad)
    find(second, "at")[1:] = [num(0), num(spacing)]
    f.node.insert(f.node.index(pad) + 1, second)
    return f


def test_opposite_side_holes_are_checked_one_by_one(hier, tmp_path):
    """FR-011: a flipped block part between two distant guide holes on top is free (and DRC
    agrees), while a hole right under the part is still refused."""
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    with kat.pcb.edit(pcb) as b:
        r = b.stamp(b1, "/CH_B", at=(112, 112), side="bottom")
    part = Board(pcb).footprint(r["pairs"]["R101"])
    assert part.side == "B"
    with kat.pcb.edit(pcb) as b:
        guide_holes(b, "H9", (part.x, part.y - 4), 8)
    board = Board(pcb)
    assert len([t for t in stamp_mod._taken(board.footprint("H9")) if t[0] == "B"]) == 2
    with kat.pcb.edit(pcb) as b:
        b.stamp(b1, "/CH_B", at=(112, 112), side="bottom")      # between the holes: free
    res = check(pcb)
    assert res["drc_errors"] == 0 and not {"pth_inside_courtyard", "npth_inside_courtyard"} & set(res["by_type"]), res["by_type"]
    with kat.pcb.edit(pcb) as b:
        b.footprint("H9").place(part.x, part.y)                 # now one hole is under the part
    with pytest.raises(kat.KatError, match="overlap H9"):
        Board(pcb).stamp(b1, "/CH_B", at=(112, 112), side="bottom")
    assert {"npth_inside_courtyard"} & set(check(pcb)["by_type"])   # and DRC calls that an error


def test_hole_box_covers_a_turned_slot(tmp_path, monkeypatch):
    """A slot reaches past a small pad: its length counts, turned with the part."""
    from kat.sexpr import Sym, num
    monkeypatch.setenv("KAT_REPORT", "0")
    b = Board.create(tmp_path / "s.kicad_pcb")
    f = guide_holes(b, "H1", (50, 50), 10)
    d = find(f.pads()[0].node, "drill")
    d[1:] = [Sym("oval"), num(1), num(3)]
    assert f.pads()[0].hole_box == pytest.approx((49.5, 48.5, 50.5, 51.5))
    assert f.pads()[1].hole_box == pytest.approx((49.5, 59.5, 50.5, 60.5))
    f.place(rot=90)
    assert f.pads()[0].hole_box == pytest.approx((48.5, 49.5, 51.5, 50.5))
    taken = stamp_mod._taken(f)
    assert [s for s, _ in taken] == ["F", "F", "B", "B"]
    assert stamp_mod.box_of(taken[2][1]) == pytest.approx((48.5, 49.5, 51.5, 50.5))     # the slot, not the pad


def test_stamp_refuses_to_move_a_locked_part(hier, tmp_path):
    """FR-012: a locked part of the instance stops a stamp that would move it, with nothing
    changed; in place it stamps again, and unlocked it moves."""
    c = copy_of(hier, tmp_path)
    pcb, b1 = c["top"], c["b1"]
    ref = hier["stamped"]["instances"][1]["pairs"]["R101"]
    with kat.pcb.edit(pcb) as b:
        b.footprint(ref).lock()
    before = Board(pcb).footprint(ref)
    with pytest.raises(kat.KatError, match=rf"/CH_B: {ref} locked"):
        Board(pcb).stamp(b1, "/CH_B", at=(112, 113))
    with kat.pcb.edit(pcb) as b:
        b.stamp(b1, "/CH_B", at=(112, 112))                     # where it is: fine
        assert b.footprint(ref).locked and b.footprint(ref).x == before.x
    with kat.pcb.edit(pcb) as b:
        b.footprint(ref).unlock()
        b.stamp(b1, "/CH_B", at=(112, 113))
    assert Board(pcb).footprint(ref).y == pytest.approx(before.y + 1)


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
    # #13: an intended cut-out (a named rule area, or a rectangle) needs no plane
    with kat.pcb.edit(pcb) as e:
        e.keepout("In3.Cu", rect=(8.5, 1, 4, 2), name="CUT")
    kat.pcb.plane_rule(pcb, "IN*", except_zones=["CUT"], except_areas=[(0, 0, 5.5, 4)])
    planes = {x["net"] for x in check(pcb)["net_rules"]["findings"] if x["kind"] == "plane"}
    assert planes == {"IN2"}, planes
    with pytest.raises(kat.KatError, match="no zone named NOPE"):
        kat.pcb.plane_rule(pcb, "IN*", except_zones=["NOPE"])
    assert cli.main(["pcb", "netrule", str(pcb), "clear"]) == 0
    assert "net_rules" not in check(pcb)


def test_plane_rule_exempts_a_family_of_named_cutouts(tmp_path, monkeypatch, capsys):
    """FR-008: a glob over names exempts each matching area at check time, later ones too;
    exact names stay exact, and names that only look alike do not count."""
    import json
    monkeypatch.setenv("KAT_REPORT", "0")
    pcb = tmp_path / "g.kicad_pcb"
    b = Board.create(pcb)
    b.set_copper_layers(4)
    b.outline(rect=(0, 0, 30, 10))
    b.add_track((2, 2), (6, 2), 0.25, "F.Cu", "IN1")
    b.add_track((12, 2), (16, 2), 0.25, "F.Cu", "IN2")
    b.add_track((22, 2), (26, 2), 0.25, "F.Cu", "IN3")
    b.keepout("In1.Cu", rect=(1, 1, 6, 2), name="/CH_A: cut")
    b.keepout("In1.Cu", rect=(21, 1, 6, 2), name="/CH_A: keep")
    b.save()

    def uncovered() -> set[str]:
        return {x["net"] for x in check(pcb)["net_rules"]["findings"] if x["kind"] == "plane"}

    assert cli.main(["pcb", "netrule", str(pcb), "plane", "IN*", "--except-zone-glob", "/*: cut*"]) == 0
    assert "warnings" not in json.loads(capsys.readouterr().out)
    assert uncovered() == {"IN2", "IN3"}
    with kat.pcb.edit(pcb) as e:
        e.keepout("In1.Cu", rect=(11, 1, 6, 2), name="/CH_B: cut 2")
    assert uncovered() == {"IN3"}                                       # added later: counts
    assert kat.pcb.plane_rule(pcb, "IN*", except_globs=["/CH_C:*"])["warnings"]   # nothing yet
    assert uncovered() == {"IN1", "IN2", "IN3"}
    with pytest.raises(kat.KatError, match="no zone named /CH_A: cu"):  # exact names stay exact
        kat.pcb.plane_rule(pcb, "IN*", except_zones=["/CH_A: cu"])


def test_coupling_2d_estimate_side_by_side(tmp_path, monkeypatch):
    """#16: OUT1 and IN2 side by side on F.Cu for 4 mm (0.2 mm gap, plane 0.1 mm below):
    no overlap, so the plate estimate has nothing; the 2D cross-section gives pF/mm x length."""
    pytest.importorskip("scipy")
    monkeypatch.setenv("KAT_REPORT", "0")
    pcb = tmp_path / "s.kicad_pcb"
    b = Board.create(pcb)
    b.set_copper_layers(4)
    b.outline(rect=(0, 0, 20, 10))
    b.set_stackup([0.1, 1.0, 0.1])
    b.add_track((2, 2), (8, 2), 0.25, "F.Cu", "OUT1")
    b.add_track((4, 2.45), (8, 2.45), 0.25, "F.Cu", "IN2")
    b.save()
    kat.pcb.coupling_rule(pcb, "OUT*", "IN*", distance=0.3, estimate="2d")
    f = next(x for x in check(pcb)["net_rules"]["findings"] if x["kind"] == "coupling")
    # 4 mm side by side, plus the run along the round end of IN2 within the rule distance
    assert f["estimate"] == "2d" and f["length"] == pytest.approx(4.2, abs=0.25), f
    per_mm = kat.pcb.coupling_2d(0.25, 0.25, 0.2, 0.1)["pf_per_mm"]
    assert 0.003 < per_mm < 0.012 and f["pf_per_mm"] == pytest.approx(per_mm, rel=0.02)
    assert f["capacitance_pf"] == pytest.approx(per_mm * f["length"], rel=0.02)
    # farther apart couples less; an inner layer between two planes is computed too
    assert kat.pcb.coupling_2d(0.25, 0.25, 0.4, 0.1)["pf_per_mm"] < per_mm
    assert kat.pcb.coupling_2d(0.25, 0.25, 0.2, 0.1, above=0.1)["pf_per_mm"] > 0
