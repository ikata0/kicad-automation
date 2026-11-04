"""Board mechanics from fabrication data. KiCad writes the files and KiCad's DRC and exports judge
the result: a board imported from its own Gerber, Excellon or ODB++ exports again to the same."""

import json
import math
from pathlib import Path

import pytest

import kat
from kat import cli, env
from kat.pcb import mechanics, specctra
from kat.pcb.board import Board
from kat.pcb.geometry import arc_points, chain
from kat.pcb.setup import _edge
from test_pcb_odb import MATRIX, as_dir
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_sync import project  # noqa: F401 (fixture)

needs_kicad = pytest.mark.requires("kicad")
needs_parts = pytest.mark.requires("kicad", "symbols", "footprints")
needs_router = pytest.mark.requires("kicad", "symbols", "footprints", "routing")

# x, y, drill, slot length, angle, plated
HOLES = [(20, 20, 3.2, None, 0, False), (30, 20, 1.2, None, 0, True), (40, 20, 2.0, 5.0, 0, False),
         (50, 20, 2.0, 5.0, 90, False), (60, 20, 2.0, 5.0, 30, True), (30, 40, 1.2, None, 0, True)]


def hole(x, y, d, length, angle, plated):
    return {"x": x, "y": y, "d": d, "length": length, "angle": angle, "plating": plated, "kind": "hole"}


@pytest.fixture
def source(tmp_path):
    """A native board: rounded outline, a square and a round cut-out, holes and slots, both platings."""
    path = tmp_path / "src" / "src.kicad_pcb"
    path.parent.mkdir()
    b = Board.create(path)
    b.outline(rect=(10, 10, 60, 40), radius=3)
    for a, c in (((45, 32), (55, 32)), ((55, 32), (55, 38)), ((55, 38), (45, 38)), ((45, 38), (45, 32))):
        b._insert(_edge("gr_line", start=a, end=c))
    b._insert(mechanics._outline_nodes([("circle", (20, 38), 4)], (0, 0))[0])
    for k, h in enumerate(HOLES, 1):
        b._insert(mechanics.hole_node(f"H{k}", hole(*h), h[5]))
    b.save()
    return path


def key(h, nd=2):
    return (round(h["x"], nd), round(h["y"], nd), round(h["d"], nd),
            round(h["length"], nd) if h["length"] else None, round(h["angle"], 1) % 180 if h["length"] else 0, h["plating"])


def expected(nd=2):
    return sorted((key(hole(*h), nd) for h in HOLES), key=str)


def got(holes, nd=2):
    return sorted((key(h, nd) for h in holes), key=str)


def close_holes(holes, tol):
    want = sorted(HOLES, key=lambda h: (h[0], h[1]))
    have = sorted(holes, key=lambda h: (round(h["x"]), round(h["y"])))
    assert len(have) == len(want), have
    for h, w in zip(have, want):
        assert (h["x"], h["y"], h["d"]) == pytest.approx(w[:3], abs=tol), (h, w)
        assert h["plating"] is w[5], (h, w)
        if w[3]:
            assert h["length"] == pytest.approx(w[3], abs=tol) and h["angle"] % 180 == pytest.approx(w[4], abs=0.3), (h, w)
        else:
            assert h["length"] is None, (h, w)


def drill_files(pcb, out, *args):
    out.mkdir(parents=True, exist_ok=True)
    env.run_cli("pcb", "export", "drill", *args, "-o", str(out) + "/", str(pcb))
    return sorted(out.glob("*.drl"))


def gerber_of(pcb, out, *args):
    out.mkdir(parents=True, exist_ok=True)
    env.run_cli("pcb", "export", "gerbers", "--layers", "Edge.Cuts", *args, "-o", str(out) + "/", str(pcb))
    return next(out.glob("*Edge_Cuts*"))


def all_holes(files):
    return [h for f in files for h in mechanics.read_excellon(f)["holes"]]


@needs_kicad
@pytest.mark.parametrize("zeros", ["decimal", "suppressleading", "suppresstrailing", "keep"])
@pytest.mark.parametrize("oval", ["alternate", "route"])
def test_every_excellon_form_kicad_writes(source, tmp_path, zeros, oval):
    files = drill_files(source, tmp_path / "d", "--excellon-zeros-format", zeros, "--excellon-oval-format", oval,
                        "--excellon-separate-th")
    assert len(files) == 2
    close_holes(all_holes(files), 0.002)


@needs_kicad
@pytest.mark.parametrize("zeros", ["decimal", "suppressleading", "suppresstrailing", "keep"])
def test_inches_in_one_mixed_file(source, tmp_path, zeros):
    files = drill_files(source, tmp_path / "d", "-u", "in", "--excellon-zeros-format", zeros)
    assert len(files) == 1
    close_holes(all_holes(files), 0.01)


@needs_kicad
def test_a_minimal_header_says_no_plating_but_the_file_name_does(source, tmp_path):
    files = drill_files(source, tmp_path / "d", "--excellon-min-header", "--excellon-separate-th")
    close_holes(all_holes(files), 0.002)


@needs_kicad
@pytest.mark.parametrize("args", [(), ("--precision", "5"), ("--no-x2",), ("--disable-aperture-macros",)])
def test_outline_gerber_gives_edge_and_cutouts(source, tmp_path, args):
    r = mechanics.read_gerber(gerber_of(source, tmp_path / "g", *args))
    assert not r["open"] and r["widths"] == [0.05], r
    assert len(r["loops"]) == 3
    kinds = {tuple(sorted({s[0] for s in l})) for l in r["loops"]}
    assert ("arc", "line") in kinds and ("line",) in kinds, kinds
    assert kinds & {("arc",), ("circle",)}, kinds        # KiCad draws a circle in arcs or in one piece


@needs_kicad
def test_gerber_and_excellon_round_trip_through_kicad(source, tmp_path):
    ger = gerber_of(source, tmp_path / "g")
    drills = drill_files(source, tmp_path / "d", "--excellon-separate-th")
    new = tmp_path / "new" / "new.kicad_pcb"
    new.parent.mkdir()
    r = kat.pcb.import_mechanics(new, gerber=ger, drills=drills)
    assert r["loops"] == 3 and r["cutouts"] == 2 and r["outline"] == [10, 10, 70, 50]
    assert r["holes"] == {"plated": 3, "non_plated": 3, "slots": 3, "skipped": {}}
    assert r["check"]["drc_errors"] == 0 and r["check"]["ok"], r["check"]
    assert not r["warnings"]
    # KiCad exports the imported board to the same holes and the same outline
    close_holes(all_holes(drill_files(new, tmp_path / "d2", "--excellon-separate-th")), 0.002)
    assert _segments(ger) == _segments(gerber_of(new, tmp_path / "g2"))
    # and its own pads say the same
    b = Board(new)
    assert len(b.footprints()) == 6 and all(f.board_only and f.lib_id == mechanics.MECH_LIB for f in b.footprints())
    assert {p.kind for p in b.pads()} == {"thru_hole", "np_thru_hole"}
    assert kat.pcb.drc(new, parity=False)["by_type"] == {}


def _segments(ger):
    """Outline strokes as an unordered set: direction and order may differ."""
    out = set()
    for loop in mechanics.read_gerber(ger)["loops"]:
        for s in loop:
            pts = [tuple(round(v, 3) for v in p) for p in s[1:]] if s[0] != "circle" else [s[1], s[2]]
            out.add((s[0], *sorted(pts, key=str)))
    return out


@needs_kicad
def test_odb_round_trip_through_kicad(source, tmp_path):
    job = tmp_path / "job.zip"
    env.run_cli("pcb", "export", "odb", "--precision", "6", "-o", str(job), str(source))
    m = mechanics.read_odb(job)
    close_holes(m["holes"], 0.002)
    assert len(m["loops"]) == 3 and not m["warnings"]
    new = tmp_path / "new" / "new.kicad_pcb"
    new.parent.mkdir()
    r = kat.pcb.import_mechanics(new, odb=job)
    assert r["check"]["drc_errors"] == 0 and r["check"]["ok"], r["check"]
    assert r["outline"] == pytest.approx([10, 10, 70, 50], abs=0.001) and r["cutouts"] == 2
    close_holes(all_holes(drill_files(new, tmp_path / "d2", "--excellon-separate-th")), 0.002)
    loops = Board(new).outline_loops()
    ref = Board(source).outline_loops()
    assert len(loops) == len(ref) == 3
    for a, c in zip(loops, ref):
        assert _area(a) == pytest.approx(_area(c), rel=0.005)     # both only near a circle


def _area(p):
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(p, p[1:] + p[:1]))) / 2


@needs_kicad
def test_vias_in_kicad_exports_are_left_out_unless_asked(source, tmp_path):
    with kat.pcb.edit(source) as b:
        b.tree.append(_via(35, 25))
    drills = drill_files(source, tmp_path / "d", "--excellon-separate-th")
    job = tmp_path / "job.zip"
    env.run_cli("pcb", "export", "odb", "--precision", "6", "-o", str(job), str(source))
    for name, kw in (("g", {"drills": drills}), ("o", {"odb": job})):
        new = tmp_path / name / "new.kicad_pcb"
        new.parent.mkdir()
        r = kat.pcb.import_mechanics(new, **kw)
        assert r["holes"]["skipped"] == {"vias": 1} and r["holes"]["plated"] == 3, r
        r = kat.pcb.import_mechanics(new, vias=True, **kw)
        assert not r["holes"]["skipped"] and r["holes"]["plated"] == 4, r


def _via(x, y):
    from kat.sexpr import Sym, num
    from kat.schematic import new_uuid
    return [Sym("via"), [Sym("at"), num(x), num(y)], [Sym("size"), num(0.8)], [Sym("drill"), num(0.4)],
            [Sym("layers"), "F.Cu", "B.Cu"], [Sym("net"), ""], [Sym("uuid"), new_uuid()]]


@needs_kicad
def test_import_again_replaces_and_at_places_the_corner(source, tmp_path):
    ger = gerber_of(source, tmp_path / "g")
    drills = drill_files(source, tmp_path / "d", "--excellon-separate-th")
    new = tmp_path / "new" / "new.kicad_pcb"
    new.parent.mkdir()
    r = kat.pcb.import_mechanics(new, gerber=ger, drills=drills, at=(100, 200))
    assert r["offset"] == [90, 190] and r["outline"] == [100, 200, 160, 240]
    r2 = kat.pcb.import_mechanics(new, gerber=ger, drills=drills, offset=(1, 2))
    assert r2["outline"] == [11, 12, 71, 52]
    b = Board(new)
    assert sorted(f.ref for f in b.footprints()) == [f"MH{k}" for k in range(1, 7)]
    assert {(f.x, f.y) for f in b.footprints()} == {(h[0] + 1, h[1] + 2) for h in HOLES}
    with pytest.raises(kat.KatError, match="not both"):
        kat.pcb.import_mechanics(new, gerber=ger, at=(1, 1), offset=(1, 1))
    with pytest.raises(kat.KatError, match="no outline"):
        kat.pcb.import_mechanics(new, drills=drills, at=(1, 1))


@needs_kicad
def test_dry_run_saves_nothing(source, tmp_path):
    ger = gerber_of(source, tmp_path / "g")
    before = source.read_text()
    r = kat.pcb.import_mechanics(source, gerber=ger, offset=(5, 5), dry_run=True)
    assert r["dry_run"] and "check" not in r and source.read_text() == before
    with pytest.raises(kat.KatError, match="does not exist"):
        kat.pcb.import_mechanics(tmp_path / "none.kicad_pcb", gerber=ger, dry_run=True)
    assert not (tmp_path / "none.kicad_pcb").exists()


@needs_parts
def test_a_hole_on_an_existing_pad_is_left_out(pcb, tmp_path):
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 40, 25))
        b.move("J1", to=(4, 10))
        pad = b.footprint("J1").pads()[0]
        x, y = pad.center
    f = tmp_path / "x.drl"
    f.write_text(f"M48\nMETRIC\nT1C1.0\n%\nT1\nX{x}Y{-y}\nX30.0Y-12.0\nM30\n")
    r = kat.pcb.import_mechanics(pcb, drills=[f], assume="non_plated")
    assert r["holes"]["skipped"] == {"on_pads": 1} and r["holes"]["non_plated"] == 1 and not r["warnings"]
    assert len([q for q in Board(pcb).footprints() if q.board_only]) == 1


# --- the formats other CAD tools write ---------------------------------------------------------

def write(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


GERBER_INCH = """G04 another tool*
%FSTAX24Y24*%
%MOIN*%
%ADD11C,0.01*%
G70*
G54D11*
X01Y01D02*
G01X03Y01D01*
X03Y02D01*
X01Y02D01*
X01Y01D01*
M02*
"""


def test_gerber_inches_with_trailing_zeros_left_out(tmp_path):
    r = mechanics.read_gerber(write(tmp_path, "o.gbr", GERBER_INCH))
    assert r["widths"] == [0.254] and len(r["loops"]) == 1 and not r["open"]
    pts = {tuple(round(v, 3) for v in s[1]) for s in r["loops"][0]}
    assert pts == {(25.4, -25.4), (76.2, -25.4), (76.2, -50.8), (25.4, -50.8)}


def test_gerber_single_quadrant_arcs_and_regions(tmp_path):
    text = """%FSLAX33Y33*%
%MOMM*%
%ADD10C,0.1*%
D10*
G74*
X0Y10000D02*
G02X10000Y0I0J10000D01*
G01X0Y0D01*
X0Y10000D01*
G36*
X20000Y0D02*
G01X30000Y0D01*
X30000Y10000D01*
X20000Y0D01*
G37*
M02*
"""
    r = mechanics.read_gerber(write(tmp_path, "o.gbr", text))
    assert len(r["loops"]) == 2 and not r["open"]
    arc = next(s for l in r["loops"] for s in l if s[0] == "arc")
    # a quarter circle about the origin, clockwise from (0, 10) to (10, 0) with y up
    assert arc[2] == pytest.approx((10 * math.sin(math.pi / 4), -10 * math.cos(math.pi / 4)), abs=1e-3)


def test_gerber_arc_direction_is_not_mirrored(tmp_path):
    """G03 is counter-clockwise seen with y up: its middle lies on the right side of the chord."""
    text = "%FSLAX33Y33*%\n%MOMM*%\n%ADD10C,0.1*%\nD10*\nG75*\nX0Y0D02*\nG03X10000Y0I5000J0D01*\nG01X0Y0D01*\nM02*\n"
    arc = next(s for s in mechanics.read_gerber(write(tmp_path, "o.gbr", text))["loops"][0] if s[0] == "arc")
    assert arc[2] == pytest.approx((5, 5), abs=1e-3), "below the chord when y-up, so y=+5 when y-down"


def test_gerber_problems(tmp_path):
    with pytest.raises(kat.KatError, match="step and repeat"):
        mechanics.read_gerber(write(tmp_path, "a.gbr", "%FSLAX33Y33*%\n%MOMM*%\n%SRX2Y2I5.0J5.0*%\nM02*\n"))
    with pytest.raises(kat.KatError, match="incremental"):
        mechanics.read_gerber(write(tmp_path, "b.gbr", "%FSLIX33Y33*%\n%MOMM*%\nM02*\n"))
    with pytest.raises(kat.KatError, match="before the units"):
        mechanics.read_gerber(write(tmp_path, "c.gbr", "%FSLAX33Y33*%\nX1Y1D02*\nM02*\n"))
    with pytest.raises(kat.KatError, match="before the format"):
        mechanics.read_gerber(write(tmp_path, "d.gbr", "%MOMM*%\nX1Y1D02*\nM02*\n"))
    with pytest.raises(kat.KatError, match="not a Gerber"):
        mechanics.read_gerber(write(tmp_path, "e.gbr", "hello\n"))


def test_open_chains_and_flashes_are_reported(tmp_path):
    text = """%FSLAX33Y33*%
%MOMM*%
%ADD10C,0.1*%
D10*
X0Y0D02*
X10000Y0D01*
X10000Y10000D01*
X50000Y50000D02*
X60000Y50000D01*
X1000Y1000D03*
M02*
"""
    f = write(tmp_path, "o.gbr", text)
    r = mechanics.read_gerber(f)
    assert not r["loops"] and len(r["open"]) == 2 and "flashed" in r["warnings"][0]
    with pytest.raises(kat.KatError, match="no closed outline: 2 open chains"):
        kat.pcb.import_mechanics(tmp_path / "x.kicad_pcb", gerber=f)


def test_small_gaps_join_only_within_the_tolerance(tmp_path):
    text = """%FSLAX34Y34*%
%MOMM*%
%ADD10C,0.1*%
D10*
X0Y0D02*
X100000Y0D01*
X100000Y100000D01*
X0Y100000D01*
X0Y50D01*
M02*
"""
    f = write(tmp_path, "o.gbr", text)
    assert mechanics.read_gerber(f)["loops"], "5 um closes at the default 10 um"
    assert not mechanics.read_gerber(f, tolerance=0.001)["loops"]


@pytest.mark.parametrize("header, x, y", [
    ("METRIC,TZ\n", "1000", "-2000"),                  # kept trailing: 3 decimals by default
    ("METRIC,LZ\n", "001", "-002"),                    # kept leading: 3 integer digits by default
    ("METRIC,LZ,000.000\n", "001", "-002"),
    (";FILE_FORMAT=4:2\nMETRIC,TZ\n", "100", "-200"),
    (";FILE_FORMAT=2:5\nMETRIC,LZ\n", "01", "-02"),
    ("M71\n", "1.0", "-2.0"),
    ("INCH,TZ\n", "10000", "-20000"),                  # 0.0001 inch by default
    ("INCH,LZ,00.0000\n", "01", "-02"),
    ("M72\n", "1.0", "-2.0"),
])
def test_excellon_number_formats(tmp_path, header, x, y):
    inch = "INCH" in header or "M72" in header
    text = f"M48\n{header}T01C{1 / 25.4 if inch else 1.0}\n%\nT01\nX{x}Y{y}\nM30\n"
    h = mechanics.read_excellon(write(tmp_path, "a_NPTH.drl", text))["holes"]
    k = 25.4 if inch else 1.0
    assert len(h) == 1 and (h[0]["x"], h[0]["y"], h[0]["d"]) == pytest.approx((k, 2 * k, 1), abs=1e-5), h
    assert h[0]["plating"] is False


def test_excellon_without_zero_setting_warns(tmp_path):
    r = mechanics.read_excellon(write(tmp_path, "a.drl", "M48\nMETRIC\nT1C1\n%\nT1\nX1000Y-2000\nM30\n"))
    assert "does not say which zeros" in r["warnings"][0]
    assert not mechanics.read_excellon(write(tmp_path, "b.drl", "M48\nMETRIC\nT1C1\n%\nT1\nX1.0Y-2.0\nM30\n"))["warnings"]


def test_excellon_plating_words(tmp_path):
    body = "%\nT1\nX1.0Y1.0\nM30\n"
    for text, name, want in [("M48\n;TYPE=PLATED\nMETRIC\nT1C1\n" + body, "a.txt", True),
                             ("M48\n;TYPE=NON_PLATED\nMETRIC\nT1C1\n" + body, "a.txt", False),
                             ("M48\nMETRIC\nT1C1\n" + body, "board-PTH.drl", True),
                             ("M48\nMETRIC\nT1C1\n" + body, "board_NPTH.drl", False),
                             ("M48\nMETRIC\nT1C1\n" + body, "board.drl", None),
                             ("M48\n; #@! TF.FileFunction,NonPlated,1,2,NPTH\nMETRIC\nT1C1\n" + body, "a.drl", False),
                             ("M48\n; #@! TF.FileFunction,MixedPlating,1,2\nMETRIC\n"
                              "; #@! TA.AperFunction,Plated,PTH,ViaDrill\nT1C1\n" + body, "a.drl", True)]:
        h = mechanics.read_excellon(write(tmp_path, name, text))["holes"]
        assert h[0]["plating"] is want, (text, name)
    assert h[0]["kind"] == "via"


def test_excellon_routed_paths_and_repeats(tmp_path):
    text = """M48
METRIC,LZ
T01F00S00C1.000
T02C2.000
M95
G90
T01
X10.0Y-10.0
R2X5.0
T02
G00X0.0Y0.0
M15
G01X10.0Y0.0
G01X10.0Y-10.0
G02X0.0Y-10.0I-5.0J0.0
M16
G05
X50.0Y-50.0
G00X60.0Y-60.0
M30
X99.0Y-99.0
"""
    r = mechanics.read_excellon(write(tmp_path, "a.drl", text))
    plain = [h for h in r["holes"] if not h["length"]]
    slots = [h for h in r["holes"] if h["length"]]
    assert [(h["x"], h["y"], h["d"]) for h in plain] == [(10, 10, 1), (15, 10, 1), (20, 10, 1), (50, 50, 2)]
    assert [(h["x"], h["y"], h["length"], h["angle"]) for h in slots] == [(5, 0, 12, 0), (10, 5, 12, 90)]
    assert "1 routed arcs left out" in r["warnings"][0]


@pytest.mark.parametrize("text, match", [
    ("X1.0Y1.0\nM30\n", "no header"),
    ("M48\nMETRIC\nICI,ON\n%\n", "incremental"),
    ("M48\nMETRIC\nT1C1\n%\nX1.0Y1.0\nM30\n", "no tool"),
    ("M48\nMETRIC\nT1C1\n%\nX1.0Y1.0G85X2.0Y1.0\nM30\n", "no tool"),
    ("M48\nT1C1\n%\n", "before the units"),
    ("nothing here\n", "no units"),
])
def test_excellon_problems(tmp_path, text, match):
    with pytest.raises(kat.KatError, match=match):
        mechanics.read_excellon(write(tmp_path, "a.drl", text))


def test_an_unknown_excellon_code_is_reported(tmp_path):
    r = mechanics.read_excellon(write(tmp_path, "a.drl", "M48\nMETRIC\nT1C1\n%\nT1\nG81\nX1.0Y1.0\nM30\n"))
    assert "'G81' not understood" in r["warnings"][0] and len(r["holes"]) == 1


# a job another tool could write: inches, mils, real arcs, a slot as a line, an old orientation
PROFILE = """UNITS=INCH
F 2
S P 0
OB 0 0 I
OS 2 0
OC 3 1 2 1 N
OS 3 2
OS 0 2
OS 0 0
OE
OB 1 1 H
OC 1 1 1.25 1 Y
OE
SE
"""
DRILL = """UNITS=INCH
#
$0 r100
$1 r40
$2 oval200x80
$3 s50
@0 .drill
P 0.5 0.5 0 P 0 0 ;0=1
P 1.5 0.5 1 P 0 0 ;0=0
P 2.5 0.5 1 P 0 0 ;0=2
L 0.5 1 0.5 1.5 1 P 0 ;0=1
P 2 1.5 2 P 0 1 ;0=1
P 2.5 1.5 2 P 0 8 45.0 ;0=0
P 1 1.5 0 P 0 0
P 1.2 1.5 3 P 0 0 ;0=0
A 0 0 1 1 1 0 1 P 0 Y ;0=1
"""
LAYERS = MATRIX + """
LAYER {
    ROW=3
    CONTEXT=BOARD
    TYPE=DRILL
    NAME=NPTH_DRILLS
}
"""
JOB = {"matrix/matrix": LAYERS, "steps/pcb/profile": PROFILE, "steps/pcb/layers/npth_drills/features": DRILL}


def test_odb_written_to_the_specification(tmp_path):
    m = mechanics.read_odb(as_dir(tmp_path / "job", JOB))
    assert len(m["loops"]) == 2
    outer, inner = m["loops"]
    arc = next(s for s in outer if s[0] == "arc")
    # counter-clockwise about (2,1) inch from (2,0) to (3,1): through the lower right, y up
    assert arc[1] == (50.8, 0) and arc[3] == (76.2, -25.4)
    assert arc[2] == pytest.approx((50.8 + 25.4 * math.sin(math.pi / 4), -25.4 + 25.4 * math.cos(math.pi / 4)), abs=1e-3)
    assert inner == [("circle", (31.75, -25.4), 6.35)]
    h = {(round(q["x"], 2), round(q["y"], 2)): q for q in m["holes"]}
    assert h[(12.7, -12.7)]["d"] == 2.54 and h[(12.7, -12.7)]["plating"] is False
    assert h[(38.1, -12.7)]["d"] == pytest.approx(1.016) and h[(38.1, -12.7)]["plating"] is True
    assert h[(63.5, -12.7)]["kind"] == "via"
    slot = h[(12.7, -31.75)]
    assert (slot["length"], slot["angle"], slot["plating"]) == (pytest.approx(13.716), 90, False)
    assert h[(50.8, -38.1)]["angle"] == 90 and h[(50.8, -38.1)]["length"] == pytest.approx(5.08), "old orientation 1: 90"
    assert h[(63.5, -38.1)]["angle"] == 135 and h[(63.5, -38.1)]["d"] == pytest.approx(2.032), "45 clockwise"
    assert h[(25.4, -38.1)]["plating"] is False, "no attribute: the layer's name says"
    assert any("'s50'" in w for w in m["warnings"]) and any("arc" in w for w in m["warnings"])


def test_odb_problems(tmp_path):
    with pytest.raises(kat.KatError, match="has 0 steps with profile"):
        mechanics.read_odb(as_dir(tmp_path / "a", {"matrix/matrix": LAYERS, "steps/pcb/stephdr": "x"}))
    both = dict(JOB, **{"steps/panel/profile": PROFILE})
    with pytest.raises(kat.KatError, match="2 steps with profile"):
        mechanics.read_odb(as_dir(tmp_path / "b", both))
    assert mechanics.read_odb(as_dir(tmp_path / "c", both), "panel")["step"] == "panel"


def test_unknown_plating_needs_a_word(tmp_path):
    f = write(tmp_path, "a.drl", "M48\nMETRIC\nT1C1\n%\nT1\nX1.0Y-1.0\nM30\n")
    r = mechanics.apply(_empty_board(), [], mechanics.read_excellon(f)["holes"])
    assert r["holes"]["non_plated"] == 1 and "taken as non_plated (assume" in r["warnings"][0]
    r = mechanics.apply(_empty_board(), [], mechanics.read_excellon(f)["holes"], assume="plated")
    assert r["holes"]["plated"] == 1 and not r["warnings"]
    r = mechanics.apply(_empty_board(), [], mechanics.read_excellon(f)["holes"], assume="non_plated")
    assert r["holes"]["non_plated"] == 1 and not r["warnings"]
    with pytest.raises(kat.KatError, match="plated or non_plated"):
        mechanics.apply(_empty_board(), [], [], assume="maybe")


def _empty_board():
    b = Board.__new__(Board)
    from kat.sexpr import Sym
    b.tree = [Sym("kicad_pcb")]
    return b


def test_holes_listed_twice_become_one_and_small_ones_can_stay_out():
    b = _empty_board()
    twice = [hole(1, 1, 1.0, None, 0, False), hole(1, 1, 1.0, None, 0, False), hole(2, 2, 0.2, None, 0, False)]
    r = mechanics.apply(b, [], twice, min_drill=0.3)
    assert r["holes"]["non_plated"] == 1 and r["holes"]["skipped"] == {"small": 1, "duplicates": 1}


def test_a_loop_outside_the_largest_is_pointed_out():
    b = _empty_board()
    square = lambda x: [("line", (x, 0), (x + 5, 0)), ("line", (x + 5, 0), (x + 5, 5)),   # noqa: E731
                        ("line", (x + 5, 5), (x, 5)), ("line", (x, 5), (x, 0))]
    r = mechanics.apply(b, [square(0), square(100)], [])
    assert "1 loops lie outside" in r["warnings"][0]


def test_import_needs_something_and_takes_odb_alone(tmp_path):
    with pytest.raises(kat.KatError, match="name an ODB"):
        kat.pcb.import_mechanics(tmp_path / "x.kicad_pcb")
    with pytest.raises(kat.KatError, match="alone"):
        kat.pcb.import_mechanics(tmp_path / "x.kicad_pcb", odb="a", gerber="b")


def test_chain_snaps_ends_and_counts_a_stroke_drawn_twice():
    a = ("line", (0, 0), (10, 0))
    loops, opened = chain([a, a, ("line", (10.005, 0), (10, 10)), ("line", (0, 0), (10, 10))])
    assert len(loops) == 1 and not opened and len(loops[0]) == 3
    assert loops[0][0][-1] == loops[0][1][1]


def test_chain_reverses_an_arc_walked_backwards():
    loops, _ = chain([("line", (0, 0), (10, 0)), ("arc", (0, 0), (5, 5), (10, 0))])
    assert len(loops) == 1 and sorted(s[0] for s in loops[0]) == ["arc", "line"]
    s = next(q for q in loops[0] if q[0] == "arc")
    other = next(q for q in loops[0] if q[0] == "line")
    assert s[1] == other[2] and s[3] == other[1]


def test_arc_points_stays_on_the_arc_and_keeps_its_side():
    pts = arc_points((1, 0), (0, 1), (-1, 0), 0.001)
    assert pts[0] == (1, 0) and pts[-1] == (-1, 0) and all(y >= -1e-9 for _, y in pts)
    assert all(math.hypot(x, y) == pytest.approx(1) for x, y in pts)
    pts = arc_points((1, 0), (0, -1), (-1, 0), 0.001)
    assert all(y <= 1e-9 for _, y in pts)
    assert arc_points((0, 0), (1, 0), (2, 0)) == [(0, 0), (2, 0)]


# --- what the rest of kat does with such an outline -------------------------------------------

@needs_kicad
def test_outline_loops_and_the_dsn_know_the_real_shape(source, tmp_path):
    b = Board(source)
    loops = b.outline_loops()
    assert len(loops) == 3
    assert _area(loops[0]) == pytest.approx(60 * 40 - (4 - math.pi) * 9, rel=1e-3), "rounded corners"
    assert _area(loops[1]) == pytest.approx(60) and _area(loops[2]) == pytest.approx(math.pi * 16, rel=5e-3)
    assert specctra._outline_points(b) == loops[0]
    dsn = tmp_path / "x.dsn"
    specctra.write_dsn(b, dsn)
    text = dsn.read_text()
    assert "(boundary (path pcb 0 " in text
    assert "(boundary (path pcb 0 " + " ".join(specctra._xy(x, y) for x, y in loops[0][:3]) in text
    # a ring along each cut-out, its own width on a board without an edge clearance too
    (ax, ay), (cx, cy) = loops[1][:2]
    assert specctra._xy(ax, ay) not in text.split("(boundary")[0]
    assert text.count("wire_keepout") > 2 * 2 * len(loops[1])
    with kat.pcb.edit(source) as e:
        e.constraints(edge_clearance=0)
    specctra.write_dsn(Board(source), dsn)
    assert dsn.read_text().count("wire_keepout") >= 2 * (len(loops[1]) + len(loops[2]))


def test_outline_loops_of_nothing_open_and_a_plain_rectangle(tmp_path):
    b = _empty_board()
    assert b.outline_loops() == []
    b.tree.append(_edge("gr_line", start=(0, 0), end=(1, 0)))
    assert b.outline_loops() == []
    b = _empty_board()
    node = _edge("gr_rect", start=(5, 6), end=(1, 2))
    b.tree.append(node)
    assert b.outline_loops() == [[(1, 2), (5, 2), (5, 6), (1, 6)]]


@needs_parts
def test_a_part_past_a_rounded_corner_counts_as_outside(pcb):
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 40, 25), radius=10)
        b.move("R1", to=(1.0, 1.0))
    c = kat.pcb.check(pcb)
    assert "R1" in c["outside_outline"] + c["overhang"], c["outside_outline"]


def _parts_either_side_of_a_cutout(b):
    b.outline(rect=(0, 0, 60, 30))
    b.move_many({"J1": {"to": (6, 12)}, "Q1": {"to": (12, 22)}, "C1": {"to": (12, 6)},
                 "R1": {"to": (48, 15)}, "D1": {"to": (53, 8)}, "R2": {"to": (48, 22)}, "D2": {"to": (53, 22)}})


def _wall(b, x0, y0, x1, y1):
    for a, c in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        b._insert(_edge("gr_line", start=a, end=c))


@needs_router
def test_a_tall_cutout_routes_only_with_necks(pcb):
    """A net stays open here with Freerouting's necks off, kat's default, as on 2.4.1 with them on or off.
    If an upgrade routes it, the default costs nothing and this test can go."""
    with kat.pcb.edit(pcb) as b:
        _parts_either_side_of_a_cutout(b)
        _wall(b, 27, 7, 33, 23)
    assert kat.pcb.route(pcb, passes=20, timeout=300, optimize=False)["check"]["unconnected"] == 1
    with kat.pcb.edit(pcb) as b:
        b.remove_tracks()
    c = kat.pcb.route(pcb, passes=20, timeout=300, optimize=False, options={"automatic_neckdown": True})["check"]
    assert c["unconnected"] == 0 and c["drc_errors"] == 0, c


@needs_router
@pytest.mark.parametrize("shape", ["wall", "round"])
def test_the_router_goes_around_a_cutout(pcb, shape):
    """Parts on both sides of a cut-out. KiCad's DRC judges it, with its default edge clearance:
    the DSN has to keep that even when the board sets none."""
    with kat.pcb.edit(pcb) as b:
        _parts_either_side_of_a_cutout(b)
        if shape == "round":
            b._insert(mechanics._outline_nodes([("circle", (30, 15), 9)], (0, 0))[0])
        else:
            _wall(b, 28, 5, 32, 25)
    kat.pcb.route(pcb, passes=20, timeout=300)
    c = kat.pcb.check(pcb)
    assert c["by_type"] == {} and c["unconnected"] == 0, c["by_type"]
    assert c["ok"]


def test_cli_names_what_is_missing_and_leaves_no_trace(tmp_path, capsys):
    new = tmp_path / "n" / "n.kicad_pcb"
    assert cli.main(["pcb", "import-mech", str(new), "--dry-run"]) == 2
    assert "name an ODB" in json.loads(capsys.readouterr().out)["error"]
    assert not new.parent.exists()


@needs_kicad
def test_cli_writes_a_board_in_a_new_folder(tmp_path, capsys):
    job = as_dir(tmp_path / "job", JOB)
    new = tmp_path / "n" / "n.kicad_pcb"
    assert cli.main(["pcb", "import-mech", str(new), "--odb", str(job), "--at", "10,10", "--min-drill", "1.5",
                     "--annular", "0.3"]) in (0, 1)
    r = json.loads(capsys.readouterr().out)
    assert r["outline"][:2] == [10, 10] and r["cutouts"] == 1
    assert r["holes"]["skipped"]["vias"] == 1 and "small" in r["holes"]["skipped"]
    assert Path(new).exists()
