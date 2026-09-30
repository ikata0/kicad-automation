"""Board geometry against KiCad's own answers.

Oracles: kicad-cli's IPC-D-356 export (absolute pad positions) and DRC's library check
(a placed footprint must still equal its library copy after rotation and flipping).
"""

import json
import re
from pathlib import Path

import pytest

from kat import env
from kat.pcb import Board
from kat.sexpr import Sym


FOOTPRINTS = ["Resistor_SMD:R_0603_1608Metric",
              "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
              "Package_SO:MSOP-8-1EP_3x3mm_P0.65mm_EP1.68x1.88mm",
              "Package_TO_SOT_SMD:SOT-23",
              "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
              "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm"]
ROTATIONS = [0, 90, 180, 270, 30]


def ipc356(pcb: Path) -> dict[tuple[str, str], tuple[float, float]]:
    out = pcb.with_suffix(".356")
    env.run_cli("pcb", "export", "ipcd356", "-o", str(out), str(pcb))
    pos = {}
    for line in out.read_text().splitlines():
        if not line.startswith(("317", "327")) or " VIA " in line:
            continue
        ref, pad = line[20:26].strip(), line[27:31].strip()
        m = re.search(r"X([+-]\d{6})Y([+-]\d{6})", line[31:])
        # units: 0.0001 inch, y upwards
        pos[(ref, pad)] = (int(m.group(1)) * 0.00254, -int(m.group(2)) * 0.00254)
    return pos


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    path = tmp_path_factory.mktemp("pcb") / "g.kicad_pcb"
    b = Board.create(path)
    n = 0
    for i, lib_id in enumerate(FOOTPRINTS):
        for j, rot in enumerate(ROTATIONS):
            for k, side in enumerate("FB"):
                n += 1
                f = b.add_footprint(lib_id, f"U{n}", 20 + 15 * j + 7 * k, 20 + 12 * i, rot, side)
                for p in f.pads():
                    if p.number:  # a net per pad, so IPC-D-356 lists every pad
                        layers = next(c for c in p.node if isinstance(c, list) and str(c[0]) == "layers")
                        p.node.insert(p.node.index(layers) + 1, [Sym("net"), f"N{n}_{p.number}"])
    b.save()
    return Board(path)


@pytest.mark.requires("kicad", "symbols", "footprints")
def test_pad_positions_match_kicad(board):
    kicad = ipc356(board.path)
    ours = {(p.ref, p.number): (p.x, p.y) for p in board.pads() if p.number}
    assert len(kicad) == len(ours) > 200
    bad = {k: (ours[k], kicad[k]) for k in ours
           if abs(ours[k][0] - kicad[k][0]) > 0.004 or abs(ours[k][1] - kicad[k][1]) > 0.004}
    assert not bad, list(bad.items())[:5]


@pytest.mark.requires("kicad", "symbols", "footprints")
def test_placed_footprints_equal_their_library_copy(board, tmp_path):
    """DRC compares each footprint, un-rotated and un-flipped, with the library."""
    out = tmp_path / "drc.json"
    env.run_cli("pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(board.path),
                check=False)
    kinds = {v["type"] for v in json.loads(out.read_text())["violations"]}
    assert "lib_footprint_mismatch" not in kinds and "lib_footprint_issues" not in kinds


@pytest.mark.requires("kicad", "symbols", "footprints")
def test_courtyard_contains_the_pads_and_sides(board):
    for f in board.footprints():
        cy = f.courtyard()
        assert cy, f.ref
        for p in f.pads():
            assert cy[0] - 0.01 <= p.x <= cy[2] + 0.01 and cy[1] - 0.01 <= p.y <= cy[3] + 0.01, (f.ref, p.number)
        assert all(l.startswith(f.side) or l.startswith("*") for p in f.pads() for l in p.layers
                   if l.endswith(".Cu") and p.kind == "smd")


@pytest.mark.requires("kicad", "symbols", "footprints")
def test_flip_and_rotate_in_place(board, tmp_path):
    b = Board(board.path)
    f = b.footprint("U3")  # R_0603 at 90 degrees, top side
    before = {p.number: (p.x, p.y) for p in f.pads()}
    f.flip()
    f.flip()
    assert {p.number: (round(p.x, 4), round(p.y, 4)) for p in f.pads()} == \
        {k: (round(x, 4), round(y, 4)) for k, (x, y) in before.items()}
    f.place(rot=f.rot + 90)
    f.flip()
    b.path = tmp_path / "t.kicad_pcb"
    b.save()
    kicad = ipc356(b.path)
    for p in Board(b.path).footprint("U3").pads():
        assert abs(kicad[("U3", p.number)][0] - p.x) < 0.004 and abs(kicad[("U3", p.number)][1] - p.y) < 0.004


def test_index_and_outlines():
    from kat.pcb.geometry import Index, box_of, pad_outline
    idx = Index(cell=2.0)
    idx.add((0, 0, 1, 1), "a")
    idx.add((10, 10, 11, 11), "b")
    idx.add((0.5, 0.5, 12, 1.0), "long")
    assert sorted(idx.query((0.9, 0.9, 1.2, 1.2))) == ["a", "long"]
    assert idx.query((7, 7, 8, 8)) == [] and idx.query((7, 7, 8, 8), margin=2.5) == ["b"]
    tall = box_of(pad_outline("oval", 0, 0, 1.0, 3.0, 0))    # long axis vertical
    assert abs(tall[2] - tall[0] - 1.0) < 0.05 and abs(tall[3] - tall[1] - 3.0) < 0.05
    turned = box_of(pad_outline("rect", 0, 0, 1.0, 3.0, 90))
    assert abs(turned[2] - turned[0] - 3.0) < 1e-9
