"""Floorplan access: the maze against hand-made floorplans, and against the real router."""

import json

import pytest

import kat
from kat import cli
from kat.pcb.board import Board
from kat.sexpr import Sym, find

SMD = "Resistor_SMD:R_0603_1608Metric"


def plan(**kw):
    """A 60 x 40 mm area with the source along its bottom edge and one cell, whose port faces up."""
    out = {"outline": [0, 0, 60, 40], "board_layers": ["F.Cu", "B.Cu"], "source": {"box": [0, 35, 60, 40]},
           "cells": [{"name": "A", "outline": [20, 10, 30, 20], "ports": [{"name": "in", "at": [25, 10]}]}], "grid": 0.5}
    return {**out, **kw}


def port(r, name="in"):
    return next(p for p in r["ports"] if p["port"] == name)


def test_an_open_floorplan_reaches_the_source():
    r = kat.pcb.port_access(plan())
    assert r["ok"] and r["counts"] == {"reachable": 1, "blocked": 0, "sealed": 0}
    p = port(r)
    assert p["vias"] == 0 and 23.5 <= p["exit"][0] <= 26.5 and p["exit"][1] <= 10, p
    assert p["length"] > 25, "out of the top edge and around the cell to the source below"


def test_a_neighbour_right_against_a_port_seals_it():
    cells = [{"name": "A", "outline": [20, 10, 30, 20], "ports": [{"name": "in", "at": [25, 10]}]},
             {"name": "B", "outline": [20, 5, 30, 10]}]
    r = kat.pcb.port_access(plan(cells=cells))
    assert not r["ok"] and port(r)["status"] == "sealed" and "free point" in port(r)["why"]


def test_cells_that_fit_by_area_can_still_wall_a_port_in():
    """A ring of cells with plenty of room in it: fitting says yes, the maze says no."""
    ring = [{"name": "N", "outline": [10, 5, 50, 10]}, {"name": "S", "outline": [10, 25, 50, 30]},
            {"name": "W", "outline": [10, 10, 15, 25]}, {"name": "E", "outline": [45, 10, 50, 25]},
            {"name": "A", "outline": [25, 14, 35, 21], "ports": [{"name": "in", "at": [30, 14]}]}]
    r = kat.pcb.port_access(plan(cells=ring))
    assert not r["ok"] and not r["overlaps"] and not r["outside"]
    p = port(r)
    assert p["status"] == "blocked" and set(p["walled_by"]) >= {"N", "S", "W", "E"}, p


def test_a_gap_in_the_ring_opens_it():
    ring = [{"name": "N", "outline": [10, 5, 50, 10]}, {"name": "S1", "outline": [10, 25, 28, 30]},
            {"name": "S2", "outline": [32, 25, 50, 30]},
            {"name": "W", "outline": [10, 10, 15, 25]}, {"name": "E", "outline": [45, 10, 50, 25]},
            {"name": "A", "outline": [25, 14, 35, 21], "ports": [{"name": "in", "at": [30, 14]}]}]
    r = kat.pcb.port_access(plan(cells=ring, path=True))
    assert r["ok"], r
    route = port(r)["path"]
    crossing = [x for a, b in zip(route, route[1:]) for x in (a[0], b[0]) if min(a[1], b[1]) <= 27.5 <= max(a[1], b[1])]
    assert crossing and all(28 < x < 32 for x in crossing), route


def test_a_gap_too_narrow_for_a_track_stays_shut():
    def ring(gap):
        return [{"name": "N", "outline": [10, 5, 50, 10]}, {"name": "S1", "outline": [10, 25, 30 - gap / 2, 30]},
                {"name": "S2", "outline": [30 + gap / 2, 25, 50, 30]},
                {"name": "W", "outline": [10, 10, 15, 25]}, {"name": "E", "outline": [45, 10, 50, 25]},
                {"name": "A", "outline": [25, 14, 35, 21], "ports": [{"name": "in", "at": [30, 14]}]}]
    assert port(kat.pcb.port_access(plan(cells=ring(0.4), grid=0.1)))["status"] == "blocked"
    assert port(kat.pcb.port_access(plan(cells=ring(1.0), grid=0.1)))["status"] == "reachable"


def test_a_cell_only_blocks_its_layers_and_a_via_gets_past():
    ring = [{"name": n, "outline": o, "blocked_layers": ["F.Cu"]} for n, o in (
        ("N", [10, 5, 50, 10]), ("S", [10, 25, 50, 30]), ("W", [10, 10, 15, 25]), ("E", [45, 10, 50, 25]))]
    ring.append({"name": "A", "outline": [25, 14, 35, 21], "blocked_layers": ["F.Cu"],
                 "ports": [{"name": "in", "at": [30, 14], "layers": ["F.Cu"]}]})
    r = kat.pcb.port_access(plan(cells=ring))
    assert port(r)["status"] == "reachable" and port(r)["vias"] >= 1, r
    assert port(kat.pcb.port_access(plan(cells=ring, vias=False)))["status"] == "blocked"
    assert port(kat.pcb.port_access(plan(cells=ring, layers=["F.Cu"])))["status"] == "blocked"
    # a through via needs room on every layer, also one routing may not use
    block = [dict(c, blocked_layers=["F.Cu", "B.Cu"]) for c in ring]
    assert port(kat.pcb.port_access(plan(cells=block)))["status"] == "blocked"


def test_a_via_is_only_used_when_it_helps():
    r = kat.pcb.port_access(plan(cells=[{"name": "A", "outline": [20, 10, 30, 20], "blocked_layers": ["F.Cu"],
                                         "ports": [{"name": "in", "at": [25, 20], "layers": ["F.Cu"]}]}]))
    assert port(r)["vias"] == 0


def test_keepouts_block_their_layers():
    wall = [{"name": "wall", "box": [0, 30, 60, 33]}]
    r = kat.pcb.port_access(plan(keepouts=wall))
    assert port(r)["status"] == "blocked" and "wall" in port(r)["walled_by"] and "board edge" in port(r)["walled_by"]
    r = kat.pcb.port_access(plan(keepouts=[{"name": "wall", "box": [0, 30, 60, 33], "layers": ["F.Cu"]}]))
    assert port(r)["status"] == "reachable" and port(r)["exit"]
    poly = [{"name": "tri", "polygon": [[0, 30], [60, 30], [60, 33], [0, 33]]}]
    assert port(kat.pcb.port_access(plan(keepouts=poly)))["status"] == "blocked"


def test_a_source_on_some_layers_needs_a_via_to_reach():
    r = kat.pcb.port_access(plan(source={"box": [0, 35, 60, 40], "layers": ["B.Cu"]},
                                 cells=[{"name": "A", "outline": [20, 10, 30, 20], "blocked_layers": ["F.Cu"],
                                         "ports": [{"name": "in", "at": [25, 10], "layers": ["F.Cu"]}]}]))
    assert port(r)["vias"] == 1


def test_overlaps_and_cells_past_the_edge_are_named():
    cells = [{"name": "A", "outline": [20, 10, 30, 20], "ports": [{"name": "in", "at": [25, 10]}]},
             {"name": "B", "outline": [25, 15, 35, 25]}, {"name": "C", "outline": [55, 5, 65, 10]}]
    r = kat.pcb.port_access(plan(cells=cells))
    assert r["overlaps"] == [{"a": "A", "b": "B", "layers": ["B.Cu", "F.Cu"]}] and r["outside"] == ["C"] and not r["ok"]
    apart = [dict(cells[0], blocked_layers=["F.Cu"]), dict(cells[1], blocked_layers=["B.Cu"])]
    assert not kat.pcb.port_access(plan(cells=apart))["overlaps"]


def test_no_ports_is_not_a_pass():
    r = kat.pcb.port_access(plan(cells=[{"name": "A", "outline": [20, 10, 30, 20]}]))
    assert not r["ok"] and "no ports" in r["warnings"][0]


@pytest.mark.parametrize("change, match", [
    ({"cells": []}, "no cells"),
    ({"source": None}, "source is"),
    ({"source": {"box": [1, 2]}}, "x0, y0, x1, y1"),
    ({"source": {"box": [5, 5, 1, 1]}}, "empty"),
    ({"source": {"box": [0, 35, 60, 40], "layers": ["In9.Cu"]}}, "no layer In9.Cu"),
    ({"layers": ["F.Cu"], "source": {"box": [0, 35, 60, 40], "layers": ["B.Cu"]}}, "no layer routing may use"),
    ({"source": {"box": [100, 100, 101, 101]}}, "no free grid point"),
    ({"grid": 0}, "above 0"),
    ({"grid": 0.001}, "coarser"),
    ({"outline": None}, "outline is"),
    ({"cells": [{"name": "A", "outline": [1, 1, 2, 2], "ports": [{"name": "x"}]}]}, "give at"),
    ({"cells": [{"name": "A", "block": "RC"}]}, "needs board"),
])
def test_bad_floorplans_are_errors(change, match):
    with pytest.raises(kat.KatError, match=match):
        kat.pcb.port_access(plan(**change))


def test_json_file_and_cli(tmp_path, capsys):
    f = tmp_path / "plan.json"
    f.write_text(json.dumps(plan()))
    assert cli.main(["pcb", "access", str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["reachable"] == 1
    wall = plan(keepouts=[{"box": [0, 30, 60, 33]}])
    f.write_text(json.dumps(wall))
    assert cli.main(["pcb", "access", str(f)]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["blocked"] == 1
    f.write_text("not json")
    assert cli.main(["pcb", "access", str(f)]) == 2
    assert "not valid JSON" in json.loads(capsys.readouterr().out)["error"]


# --- on real boards, with the real router as the judge ------------------------------------------

def net_on(pad, name):
    pad.node.insert(pad.node.index(find(pad.node, "layers")) + 1, [Sym("net"), name])


def walled_board(path, gap):
    """Two parts joined by one net, one of them inside a keep-out ring on both layers, with a gap
    of ``gap`` mm in it (0: closed). The router must agree with the maze."""
    path.parent.mkdir(parents=True, exist_ok=True)
    b = Board.create(path)
    b.outline(rect=(0, 0, 60, 40))
    inner = b.add_board_part(SMD, "R1", (20, 20))
    outer = b.add_board_part(SMD, "R2", (50, 20))
    for f in (inner, outer):
        net_on(f.pad("1"), "A")
        net_on(f.pad("2"), f"U{f.ref}")
    left, top, right, bottom = 10, 10, 30, 30
    rects = [(left, top, right - left, 2), (left, bottom - 2, right - left, 2), (left, top, 2, bottom - top)]
    if gap:
        mid = (top + bottom) / 2
        rects += [(right - 2, top, 2, mid - gap / 2 - top), (right - 2, mid + gap / 2, 2, bottom - mid - gap / 2)]
    else:
        rects.append((right - 2, top, 2, bottom - top))
    for k, r in enumerate(rects):
        b.keepout(["F.Cu", "B.Cu"], rect=r, tracks=False, vias=False, name=f"wall {k}")
    b.save()
    return path


def board_plan(pcb, **kw):
    return {"board": str(pcb), "source": {"box": [48, 15, 52, 25]},
            "cells": [{"name": "inner", "outline": [19, 19, 21, 21], "blocked_layers": [],
                       "ports": [{"name": "1", "at": [19.2, 20], "layers": ["F.Cu"]}]}], **kw}


@pytest.mark.requires("kicad", "footprints", "routing")
@pytest.mark.parametrize("gap, reachable", [(0, False), (6, True)])
def test_the_router_agrees_with_the_maze(tmp_path, gap, reachable):
    pcb = walled_board(tmp_path / "b" / "b.kicad_pcb", gap)
    r = kat.pcb.port_access(board_plan(pcb))
    assert (port(r, "1")["status"] == "reachable") is reachable, r
    routed = kat.pcb.route(pcb, passes=10, timeout=120, nets=["A"], optimize=False)
    assert (routed["check"]["drc_errors"] == 0), routed["check"]
    assert (routed["added_tracks"] > 0 and routed["router"]["unrouted"] == 0) is reachable, routed["router"]
    if not reachable:
        assert {f"wall {k}" for k in range(4)} <= set(port(r, "1")["walled_by"])


@pytest.mark.requires("kicad", "footprints")
def test_a_board_gives_edge_cutouts_and_parts(tmp_path):
    from kat.pcb.setup import _edge
    pcb = tmp_path / "b" / "b.kicad_pcb"
    pcb.parent.mkdir()
    b = Board.create(pcb)
    b.outline(rect=(0, 0, 60, 40))
    for a, c in (((0.0, 25), (55, 25)), ((55, 25), (55, 26)), ((55, 26), (0.0, 26)), ((0.0, 26), (0.0, 25))):
        b._insert(_edge("gr_line", start=a, end=c))
    b.add_board_part(SMD, "R1", (50, 10))
    b.save()
    # a cut-out across the board, nearly to the far edge: there is only the way around it
    p = {"board": str(pcb), "source": {"box": [0, 30, 60, 40]},
         "cells": [{"name": "A", "outline": [20, 10, 30, 20], "ports": [{"name": "in", "at": [25, 20]}]}], "path": True}
    r = kat.pcb.port_access(p)
    assert port(r)["status"] == "reachable" and port(r)["length"] > 35, r
    assert any(x > 55 for x, _, _ in port(r)["path"])
    # a part's pads are obstacles, on their layers; parts can be left out. Between R1's two pads,
    # 0.85 mm apart: a 0.2 mm track fits with 0.2 mm clearance, and not with 0.4 mm
    between = dict(p, cells=[{"name": "A", "outline": [49.9, 9.9, 50.1, 10.1], "blocked_layers": [],
                              "ports": [{"name": "in", "at": [50, 10], "layers": ["F.Cu"]}]}], escape=0.2, grid=0.1,
                   clearance=0.4)
    assert port(kat.pcb.port_access(dict(between, clearance=0.2)))["status"] == "reachable"
    assert port(kat.pcb.port_access(between))["status"] == "sealed"
    assert port(kat.pcb.port_access(dict(between, parts=False)))["status"] == "reachable"
    on_b = dict(between, cells=[dict(between["cells"][0], ports=[{"name": "in", "at": [50, 10], "layers": ["B.Cu"]}])])
    assert port(kat.pcb.port_access(on_b))["status"] == "reachable", "R1 is on the top side"
    # the board's layers are known, and checked
    with pytest.raises(kat.KatError, match="no layer In1.Cu"):
        kat.pcb.port_access(dict(p, layers=["In1.Cu"]))
