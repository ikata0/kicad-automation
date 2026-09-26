"""Fabrication output and the release gate."""

import shutil
from pathlib import Path

import pytest

import kat
from kat.pcb import router
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_pcb_structure import placed  # noqa: F401 (fixture: parts placed, GND pour on B.Cu)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")
needs_router = pytest.mark.skipif(shutil.which("java") is None or not router.jar_path().exists(),
                                  reason="Freerouting not installed (kat pcb route --install)")


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
