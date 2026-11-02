"""Pad shapes against KiCad's own answer: the distances DRC reports between a pad and small
tracks all around it."""

import json
import math
import re

import pytest

import kat
from kat import cli
from kat.pcb import Board
from kat.pcb.geometry import poly_seg
from kat.sexpr import find, find_all, loads

pytestmark = pytest.mark.requires("kicad", "footprints")

PADS = {
    "rect": '(pad "1" smd rect (at 0 0) (size 2 1) (layers "F.Cu") (net "A"))',
    "circle": '(pad "1" smd circle (at 0 0) (size 2 2) (layers "F.Cu") (net "A"))',
    "oval": '(pad "1" smd oval (at 0 0) (size 3 1) (layers "F.Cu") (net "A"))',
    "oval_tall": '(pad "1" smd oval (at 0 0) (size 1 3) (layers "F.Cu") (net "A"))',
    "roundrect": '(pad "1" smd roundrect (at 0 0) (size 3 2) (layers "F.Cu") (roundrect_rratio 0.25) (net "A"))',
    "chamfer": '(pad "1" smd roundrect (at 0 0) (size 3 2) (layers "F.Cu") (roundrect_rratio 0) '
               '(chamfer_ratio 0.3) (chamfer top_left bottom_right) (net "A"))',
    "chamfer_round": '(pad "1" smd roundrect (at 0 0) (size 3 2) (layers "F.Cu") (roundrect_rratio 0.2) '
                     '(chamfer_ratio 0.3) (chamfer top_right) (net "A"))',
    "trapezoid": '(pad "1" smd trapezoid (at 0 0) (size 3 2) (rect_delta 0.6 0) (layers "F.Cu") (net "A"))',
    "trapezoid_y": '(pad "1" smd trapezoid (at 0 0) (size 3 2) (rect_delta 0 0.8) (layers "F.Cu") (net "A"))',
    "offset": '(pad "1" thru_hole rect (at 0 0) (size 2 1.5) (drill 0.6 (offset 0.5 0.2)) '
              '(layers "*.Cu") (net "A"))',
    "custom": '(pad "1" smd custom (at 0 0) (size 0.8 0.8) (layers "F.Cu") (options (clearance outline) (anchor rect)) '
              '(primitives (gr_poly (pts (xy 0 0) (xy 2 0) (xy 2 1) (xy 0 1)) (width 0) (fill yes)) '
              '(gr_line (start 0 0) (end 0 -1.5) (width 0.4)) '
              '(gr_circle (center -1 0.5) (end -0.6 0.5) (width 0) (fill yes))) (net "A"))',
}


def distances_from_drc(pcb):
    """DRC's clearance rows: (x, y of the track item) -> the actual distance, mm."""
    d = kat.pcb.drc(pcb, parity=False)
    out = {}
    for v in d["violations"]:
        if v["type"] != "clearance":
            continue
        m = re.search(r"([\d.]+) mm\)\s*$", v["description"])
        it = next(i for i in v["items"] if "pad" not in i["description"].lower())
        out[(it["x"], it["y"])] = float(m.group(1))
    return out


@pytest.mark.parametrize("rot,side", [(0, "F"), (30, "F"), (90, "F"), (0, "B"), (30, "B")])
@pytest.mark.parametrize("name", list(PADS))
def test_pad_polygons_match_the_distances_drc_reports(tmp_path, monkeypatch, name, rot, side):
    monkeypatch.setenv("KAT_REPORT", "0")
    pcb = tmp_path / "p.kicad_pcb"
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 60, 60))
    b.netclass("Default", 0.1, 5.0)                    # everything near is a violation with its distance
    f = b.add_footprint("Resistor_SMD:R_0603_1608Metric", "R1", 30, 30)
    for old in find_all(f.node, "pad")[1:]:
        f.node.remove(old)
    pad = find(f.node, "pad")
    f.node[f.node.index(pad)] = loads(PADS[name])
    f.node[:] = [c for c in f.node if not (isinstance(c, list) and str(c[0]) in ("fp_line", "fp_rect", "fp_poly", "fp_text"))]
    if side == "B":
        f.flip()
    f.place(rot=rot)
    layer = f"{side}.Cu"
    probes = {}
    for k in range(24):                                # short tracks all round, one net
        a = 2 * math.pi * k / 24
        x, y = round(30 + 3.2 * math.cos(a), 3), round(30 + 3.2 * math.sin(a), 3)
        probes[(x, y)] = ((x, y), (round(x + 0.01, 3), y))
        b.add_track(*probes[(x, y)], 0.1, layer, "B")
    b.save()
    kicad = distances_from_drc(pcb)
    assert len(kicad) >= 20, kicad
    p = Board(pcb).footprint("R1").pads()[0]
    polys = p.polygons()
    for at, actual in kicad.items():
        q = min(probes, key=lambda c: math.dist(c, at))
        s, e = probes[q]
        ours = min(poly_seg(poly, s, e) for poly in polys) - 0.05
        assert ours == pytest.approx(actual, abs=0.012), (name, rot, side, at)
        # the simple outline is never closer in than the exact shape allows
        assert poly_seg(p.outline, s, e) - 0.05 <= actual + 0.012, (name, rot, side, at)


def test_geometry_describes_each_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KAT_REPORT", "0")
    pcb = tmp_path / "g.kicad_pcb"
    b = Board.create(pcb)
    f = b.add_footprint("Resistor_SMD:R_0603_1608Metric", "R1", 10, 10)
    pads = find_all(f.node, "pad")
    f.node[f.node.index(pads[0])] = loads(PADS["chamfer_round"])
    f.node[f.node.index(pads[1])] = loads(PADS["custom"].replace('"1"', '"2"'))
    f.place(rot=90)
    g = b.add_footprint("Resistor_SMD:R_0603_1608Metric", "R2", 20, 10)
    pads = find_all(g.node, "pad")
    g.node[g.node.index(pads[0])] = loads(
        '(pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill oval 0.6 1.2 (offset 0.1 0)) (layers "*.Cu") '
        '(padstack (mode front_inner_back) (layer "Inner" (shape rect) (size 1 1)) '
        '(layer "B.Cu" (shape roundrect) (size 2 2) (roundrect_rratio 0.2))) (net "E"))')
    g.node[g.node.index(pads[1])] = loads(PADS["trapezoid"].replace('"1"', '"2"'))
    b.save()
    assert cli.main(["pcb", "pads", str(pcb), "R1", "R2.1", "R2.2"]) == 0
    out = json.loads(capsys.readouterr().out)["pads"]
    chamfer, custom, stack, trap = out
    assert chamfer["corner_radius"] == pytest.approx(0.4) and chamfer["chamfer_size"] == pytest.approx(0.6)
    assert chamfer["chamfer"] == ["top_right"] and chamfer["angle"] == 90
    assert custom["anchor"] == "rect" and [p["kind"] for p in custom["primitives"]] == ["poly", "line", "circle"]
    assert custom["primitives"][1]["width"] == 0.4 and custom["primitives"][2]["radius"] == pytest.approx(0.4)
    assert stack["drill"] == {"shape": "oval", "w": 0.6, "h": 1.2} and stack["offset"] == [0.1, 0]
    assert stack["padstack"]["mode"] == "front_inner_back"
    assert stack["padstack"]["layers"]["B.Cu"] == {"shape": "roundrect", "w": 2, "h": 2, "roundrect_rratio": 0.2}
    assert trap["rect_delta"] == [0.6, 0]
    assert cli.main(["pcb", "pads", str(pcb), "R1.9"]) == 2
    assert "no pad" in json.loads(capsys.readouterr().out)["error"]
