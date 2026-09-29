"""Board finishing: references on the silkscreen, board text, title block, fiducials."""

import shutil

import pytest

import kat
from kat.pcb import Board
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_pcb_structure import placed  # noqa: F401 (fixture: parts placed, GND pour on B.Cu)
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")

SILK = ("silk_overlap", "silk_over_copper", "silk_edge_clearance")


def silk(pcb) -> dict:
    return {k: v for k, v in kat.pcb.drc(pcb)["by_type"].items() if k in SILK}


def test_place_refs_clears_the_silkscreen_findings(placed):
    before = silk(placed)
    with kat.pcb.edit(placed) as b:
        r = b.place_refs()
        assert r["placed"] == 7 and not r["failed"]
        d1 = b.footprint("D1")
        prop = next(p for p in d1.node if isinstance(p, list) and str(p[0]) == "property"
                    and str(p[1]) == "Reference")
        assert str(next(c for c in prop if isinstance(c, list) and str(c[0]) == "layer")[1]) == "F.SilkS"
    after = silk(placed)
    assert sum(after.values()) <= sum(before.values()) and not after.get("silk_over_copper"), (before, after)


def test_place_refs_keeps_off_other_parts_silkscreen(placed):
    """B17: D2's silkscreen outline right where R2's reference would go first (above):
    the reference goes elsewhere."""
    from kat.pcb.geometry import overlap
    b = Board(placed)
    cy = b.footprint("R2").box()
    with kat.pcb.edit(placed) as b:
        d2 = b.footprint("D2")
        d2.node[:] = [c for c in d2.node if not (isinstance(c, list) and str(c[0]) == "pad")]   # silk only
        d2.set_field("Reference", d2.ref, hide=True)                   # its own text is elsewhere
        sb = d2.graphics_box("SilkS")
        # its lower silk line through the middle of the spot above R2
        d2.place(d2.x + (cy[0] + cy[2]) / 2 - (sb[0] + sb[2]) / 2, d2.y + cy[1] - 0.73 - sb[3])
        b.place_refs(["R2"])
    b = Board(placed)
    ref = b._ref_box(b.footprint("R2"))
    assert ref and not any(overlap(ref, s) for s in b.footprint("D2").graphics_boxes("SilkS")), ref


def test_set_field_hides_and_shows_an_existing_field(placed):
    """B16: hide=True / False acts on an existing field; None leaves it."""
    with kat.pcb.edit(placed) as b:
        b.footprint("R1").set_field("Value", "10k", hide=True)
    def hidden():
        p = Board(placed).footprint("R1")._prop("Value")
        return any(isinstance(c, list) and str(c[0]) == "hide" and str(c[1]) == "yes" for c in p)
    assert hidden()
    with kat.pcb.edit(placed) as b:
        b.footprint("R1").set_field("Value", "10k")
    assert hidden()
    with kat.pcb.edit(placed) as b:
        b.footprint("R1").set_field("Value", "10k", hide=False)
    assert not hidden()


def test_board_text_title_and_fiducials(placed, project):
    with kat.edit(project) as s:
        s.title(title="LED driver", rev="A", date="2026-09-26")
    with kat.pcb.edit(placed) as b:
        assert b.title_from_schematic()["title"] == "LED driver"
        b.board_text("LED driver rev A", (20, 23), size=1.0)
        f = b.fiducials(count=3)
        assert len(f["fiducials"]) == 3
        assert not b.collisions([x["ref"] for x in f["fiducials"]])
    text = placed.read_text()
    assert '(title "LED driver")' in text and '"LED driver rev A"' in text
    r = kat.pcb.sync(placed, force=True)        # fiducials are board-only: kept, parity 0
    assert not r["removed"] and r["check"]["parity"] == 0
    assert {x.ref for x in Board(placed).footprints() if x.board_only} == {"FID1", "FID2", "FID3"}
