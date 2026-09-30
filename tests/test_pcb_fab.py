"""Fabrication output and the release gate."""

from pathlib import Path

import pytest

import kat
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_pcb_structure import placed  # noqa: F401 (fixture: parts placed, GND pour on B.Cu)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.requires("kicad", "symbols", "footprints")
needs_router = pytest.mark.requires("routing")


def test_fab_set_and_gate_refuses_an_unrouted_board(placed):
    f = kat.pcb.fab(placed, step=False, three_d=False)
    d = Path(f["dir"])
    assert d.name == "p-draft" and (d / f["files"]["zip"]).exists()
    names = f["files"]["gerbers"]
    assert any("F_Cu" in n for n in names) and any("Edge_Cuts" in n for n in names)
    assert any(n.endswith(".drl") for n in names)
    bom = (d / "assembly" / f["files"]["bom"]).read_text()
    assert "MPN" in bom.splitlines()[0] and "X-R1" in bom
    r = kat.pcb.ready(placed)
    assert not r["ready"]
    assert {e["problem"] for e in r["errors"]} >= {"check_not_clean", "title_block"}


@needs_router
def test_routed_finished_board_is_ready(placed, project):
    with kat.edit(project) as s:
        s.title(title="LED driver", rev="A")
    with kat.pcb.edit(placed) as b:
        b.fanout(["Q1", "C1"], nets=["GND"])
        b.title_from_schematic()
    kat.pcb.route(placed, passes=10, timeout=300)
    with kat.pcb.edit(placed) as b:
        b.place_refs()
    kat.pcb.refill(placed)
    c = kat.pcb.check(placed)
    assert c["clean"], {k: c[k] for k in ("by_type", "unconnected", "outside_outline")}
    kat.pcb.fab(placed, step=False, three_d=False)
    r = kat.pcb.ready(placed)
    assert r["ready"], r["errors"]
    assert r["fab_dir"].endswith("p-A")


@needs_router
def test_gate_accepts_named_warnings_and_edge_mount_parts(placed, project):
    """I6: a lone via (via_dangling) and an edge SMA hanging over the edge block the gate
    until accepted with a reason / named as overhang; errors stay errors."""
    with kat.edit(project) as s:
        s.title(title="LED driver", rev="A")
    with kat.pcb.edit(placed) as b:
        b.fanout(["Q1", "C1"], nets=["GND"])
        b.title_from_schematic()
    kat.pcb.route(placed, passes=10, timeout=300)
    with kat.pcb.edit(placed) as b:
        b.place_refs()
        b.via((36, 3), net="GND", lock=True)                      # joined on no layer
        for rot in (0, 90, 180, 270):                             # body out over the top edge
            j = b.add_footprint("Connector_Coaxial:SMA_Amphenol_132289_EdgeMount", "J9", 20, 5, rot)
            top = min(q[1] for pd in j.pads() for q in pd.outline)
            j.place(20, 5 - top + 0.3)
            if j.box()[1] < -0.5:
                break
            b.remove_footprint("J9")
    kat.pcb.refill(placed)
    c = kat.pcb.check(placed)
    assert c["overhang"] == ["J9"] and "via_dangling" in c["by_type"], c["by_type"]
    kat.pcb.fab(placed, step=False, three_d=False)
    r = kat.pcb.ready(placed)
    assert not r["ready"] and r["errors"][0]["detail"]["overhang"] == ["J9"]
    warn = {v["type"] for v in c["violations"] if v["severity"] == "warning"}
    r = kat.pcb.ready(placed, accept={k: "test" for k in warn}, overhang=["J9"])
    detail = r["errors"][0]["detail"]                            # J9's pads: DRC errors stay
    assert detail["drc_errors"] > 0 and not detail["warnings"] and not detail["overhang"], detail
    assert r["accepted"]["warnings"]["via_dangling"]["reason"] == "test" and r["accepted"]["overhang"] == ["J9"]
    with kat.pcb.edit(placed) as b:                              # without J9: only the warning
        b.remove_footprint("J9")
    kat.pcb.fab(placed, step=False, three_d=False)
    c = kat.pcb.check(placed)
    warn = {v["type"] for v in c["violations"] if v["severity"] == "warning"}
    assert not kat.pcb.ready(placed)["ready"]
    r = kat.pcb.ready(placed, accept={k: "harmless here" for k in warn})
    assert r["ready"], r["errors"]
