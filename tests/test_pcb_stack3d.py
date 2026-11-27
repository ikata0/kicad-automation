"""A stack of boards in 3D: placement from KiCad's own STEP exports, collisions, near misses and
connector engagement judged by cqx (cadquery-agentic)."""

import json

import pytest

import kat
from kat import cli, env
from kat.pcb.board import Board
from kat.pcb.mating import transform
from kat.pcb.stacking import SEAT, _cqx, frame_2d, load_stack, step_instances
from kat.sexpr import find

pytestmark = pytest.mark.requires("kicad", "footprints", "cqx")

HEADER = "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical"
SOCKET = "Connector_PinSocket_2.54mm:PinSocket_2x03_P2.54mm_Vertical"
R0402 = "Resistor_SMD:R_0402_1005Metric"
CAP_10 = "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm"          # 10 mm above its seat
CAP_12 = "Capacitor_THT:C_Radial_D10.0mm_H12.5mm_P5.00mm"   # 12.5 mm above its seat
MATED = 2.54 + 8.5   # the models: header plastic 2.54 mm, socket body 8.5 mm above the seat
SEAT_TOP = 1.51 + 0.035 + SEAT   # default 1.6 mm board: body 1.51 (dielectric), outer copper 0.035


def board(path, outline, parts):
    """A board with ``parts`` [(lib_id, ref, (x, y), rot, side)], fitted as parts from a schematic
    would be (``add_board_part`` marks them board-only and excluded from the BOM); ``outline``
    (x, y, w, h)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    b = Board.create(path)
    b.outline(rect=outline)
    for lib_id, ref, at, rot, side in parts:
        node = b.add_board_part(lib_id, ref, at, rot, side).node
        attr = find(node, "attr")
        attr[:] = [t for t in attr if str(t) not in ("board_only", "exclude_from_bom", "exclude_from_pos_files")]
    b.save()
    return path


def write_stack(tmp_path, gap=MATED, a_parts=(), b_parts=(), clearance=1.0, **io):
    """Board ``io`` lies face down on ``main``, its socket P1 on the header J1: B's point (x, y)
    lands on (10 - x, 10 + y) and B covers A's 30 x 20 mm."""
    board(tmp_path / "a" / "a.kicad_pcb", (0, 0, 30, 20), [(HEADER, "J1", (10, 10), 0, "F"), *a_parts])
    board(tmp_path / "b" / "b.kicad_pcb", (-20, -10, 30, 20), [(SOCKET, "P1", (0, 0), 0, "F"), *b_parts])
    stack = {"boards": {"main": {"pcb": "a/a.kicad_pcb"},
                        "io": {"pcb": "b/b.kicad_pcb", "on": "main", "at": [10, 10], "flip": True, "gap": gap,
                               "pairs": [["J1", "P1"]], "holes": "none", **io}},
             "clearance": clearance}
    path = tmp_path / "stack.json"
    path.write_text(json.dumps(stack))
    return path


def boxes(result):
    """Part bounding boxes of the generated cqx model, by path: ((xmin, ymin, zmin), (xmax, ...))."""
    from pathlib import Path
    model = Path(result["model"])
    rep = _cqx(["inspect", model.name, "--brief"], model.parent)
    return {p["path"]: (p["bbox"]["min"], p["bbox"]["max"]) for p in rep["parts"]}


def centre(box):
    (x0, y0, _), (x1, y1, _) = box
    return (x0 + x1) / 2, (y0 + y1) / 2


@pytest.mark.parametrize("rot, flip, below", [(0, True, False), (90, True, False), (37, False, False),
                                               (0, False, True), (180, True, True)])
def test_placement_agrees_with_kicads_export(tmp_path, rot, flip, below):
    """Symmetric 0402s, whose box centre is the footprint origin: KiCad's STEP of each board, placed by
    kat, puts B's parts where ``mating.transform`` puts the footprints; the facing seat lies ``gap``
    from A's."""
    a = board(tmp_path / "a" / "a.kicad_pcb", (0, 0, 40, 40), [(R0402, "R1", (20, 20), 0, "F")])
    side = ("F" if flip else "B") if not below else ("B" if flip else "F")   # the side facing A
    parts = [(R0402, "R1", (3, 4), 0, side), (R0402, "R2", (-6, 2), 90, side)]
    b = board(tmp_path / "b" / "b.kicad_pcb", (-10, -10, 20, 20), parts)
    gap, at = 5.0, (20.0, 18.0)
    stack = {"boards": {"main": {"pcb": str(a)},
                        "io": {"pcb": str(b), "on": "main", "at": list(at), "rot": rot, "flip": flip,
                               "below": below, "gap": gap, "holes": "none"}}}
    r = kat.pcb.mate_stack(stack, out=tmp_path / "out", render=False)
    assert r["ok"], r
    got = boxes(r)
    seat_bottom = -(0.035 + SEAT)
    for f in Board(b).footprints():
        x, y = transform((f.x, f.y), at, rot, flip)
        cx, cy = centre(got[f"io/{f.ref}"])
        assert (round(cx, 3), round(-cy, 3)) == (round(x, 3), round(y, 3)), f.ref
        (_, _, z0), (_, _, z1) = got[f"io/{f.ref}"]
        facing = SEAT_TOP + gap if not below else seat_bottom - gap
        assert round(z1 if not below else z0, 3) == round(facing, 3), f.ref    # a 0402 on the seat, facing A
    body = got["main/body"]
    assert (body[0][2], body[1][2]) == (0.0, 1.51)


def test_frame_2d_is_mating_transform_in_step_coordinates():
    for at, rot, flip in [((3, 4), 0, False), ((12.5, -7), 90, True), ((0, 0), 33, True)]:
        m, t = frame_2d(at, rot, flip)
        for x, y in [(0, 0), (1, 2), (-4, 7)]:
            u, v = transform((x, -y), at, rot, flip)
            assert m[0] * x + m[1] * y + t[0] == pytest.approx(u, abs=1e-5)
            assert m[2] * x + m[3] * y + t[1] == pytest.approx(-v, abs=1e-5)


def test_a_mated_stack_is_ok_and_the_connectors_engage(tmp_path):
    r = kat.pcb.mate_stack(write_stack(tmp_path), render=False)
    assert r["ok"], r
    assert r["links"]["main-io"]["ok"]
    (conn,) = r["connectors"]
    assert (conn["a"], conn["b"], conn["seated"], conn["touching"]) == ("main/J1", "io/P1", True, True)
    assert conn["contact_mm2"] > 20      # the socket's face on the header's plastic


def test_a_tall_part_under_the_other_board_collides(tmp_path):
    r = kat.pcb.mate_stack(write_stack(tmp_path, a_parts=[(CAP_12, "C1", (22, 10), 0, "F")]), render=False)
    assert not r["ok"]
    assert [(c["a"], c["b"]) for c in r["collisions"]] == [("main/C1", "io/body")]
    assert r["collisions"][0]["overlap_mm3"] > 0


def test_a_near_miss_is_reported_with_its_distance(tmp_path):
    """The 10 mm capacitor (body centre 2.5 mm right of pad 1, at A's (24.5, 10)) under B's 0402 at
    B's (-14.5, 0): their boxes are 11.04 - 10 - 0.35 = 0.69 mm apart, and the capacitor's top is
    recessed 0.1 mm inside its rim, so the parts are 0.79 mm apart."""
    path = write_stack(tmp_path, a_parts=[(CAP_10, "C1", (22, 10), 0, "F")],
                       b_parts=[(R0402, "R9", (-14.5, 0), 0, "F")])
    r = kat.pcb.mate_stack(path, render=False)
    assert [(n["a"], n["b"], n["distance"]) for n in r["near"]] == [("main/C1", "io/R9", 0.79)]
    assert not r["ok"] and not r["collisions"]
    assert kat.pcb.mate_stack(path, clearance=0.75, render=False)["ok"]


@pytest.mark.parametrize("error", [2.0, -0.5])
def test_a_wrong_gap_leaves_the_connectors_unseated(tmp_path, error):
    """2 mm too far apart the pins still reach into the socket; 0.5 mm too close the housings collide:
    neither is seated."""
    r = kat.pcb.mate_stack(write_stack(tmp_path, gap=MATED + error), render=False)
    assert not r["ok"]
    (row,) = r["not_seated"]
    assert (row["a"], row["b"], row["seated"], row["touching"]) == ("main/J1", "io/P1", False, True)


def test_touch_is_enough_when_asked(tmp_path):
    r = kat.pcb.mate_stack(write_stack(tmp_path, gap=MATED + 2, engage="touch"), render=False)
    assert r["ok"] and r["connectors"][0]["engage"] == "touch"


def test_allowed_pairs_do_not_fail(tmp_path):
    path = write_stack(tmp_path, a_parts=[(CAP_12, "C1", (22, 10), 0, "F")])
    stack = json.loads(path.read_text())
    stack["allow"] = [["main/C1", "io/body"]]
    path.write_text(json.dumps(stack))
    assert kat.pcb.mate_stack(path, render=False)["ok"]


def test_a_part_without_a_model_is_named(tmp_path):
    path = write_stack(tmp_path, a_parts=[(R0402, "R5", (4, 4), 0, "F")])
    a = Board(tmp_path / "a" / "a.kicad_pcb")
    node = a.footprint("R5").node
    node[:] = [x for x in node if not (isinstance(x, list) and x and str(x[0]) == "model")]
    a.save()
    r = kat.pcb.mate_stack(path, render=False)
    assert r["missing_models"] == ["main/R5"] and not r["ok"]
    assert "R5" not in step_instances(tmp_path / "stack.mate" / "main.step")


def test_without_solid_only_the_links_are_checked(tmp_path):
    r = kat.pcb.mate_stack(write_stack(tmp_path, gap=MATED + 2), solid=False)
    assert r["ok"] and "collisions" not in r and not (tmp_path / "stack.mate").exists()


def test_stack_file_errors_are_named(tmp_path):
    path = write_stack(tmp_path)
    good = json.loads(path.read_text())
    main, io = good["boards"]["main"], good["boards"]["io"]
    cases = [({"boards": {"main": main}}, "at least two"),
             ({"boards": {"main": main, "io": io, "x": {"pcb": "a/a.kicad_pcb", "on": "nowhere", "gap": 1}}},
              "unknown board"),
             ({"boards": {"main": main, "io": {**io, "on": "x"}, "x": {**io, "on": "io"}}}, "loop"),
             ({"boards": {"main": main, "io": {k: v for k, v in io.items() if k != "gap"}}}, "needs 'gap'"),
             ({"boards": {"main": main, "io": {**io, "spin": 1}}}, "unknown keys"),
             ({"boards": {"main": main, "io": {**io, "on": None}}}, "exactly one board"),
             ({"boards": {"a/b": main, "io": io}}, "letters, digits")]
    for stack, words in cases:
        path.write_text(json.dumps(stack))
        with pytest.raises(env.KatError, match=words):
            load_stack(path)


def test_cli(tmp_path, capsys):
    path = write_stack(tmp_path, a_parts=[(CAP_12, "C1", (22, 10), 0, "F")])
    assert cli.main(["pcb", "mate", "--stack", str(path), "--no-render"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["collisions"] == 1
    assert cli.main(["pcb", "mate", "--stack", str(path), "--no-3d"]) == 0
    capsys.readouterr()
    assert cli.main(["pcb", "mate", "--stack", str(path), str(tmp_path / "a" / "a.kicad_pcb")]) == 2
    assert "--stack" in json.loads(capsys.readouterr().out)["error"]


def test_pictures_of_the_stack_and_of_each_finding(tmp_path):
    from pathlib import Path
    r = kat.pcb.mate_stack(write_stack(tmp_path, a_parts=[(CAP_12, "C1", (22, 10), 0, "F")]))
    (hit,) = r["collisions"]
    assert [Path(p).name for p in hit["images"]] == ["front__finding1.png", "right__finding1.png"]
    assert len(r["3d"]) == 5 and all(Path(p).is_file() for p in r["3d"])
