"""Route polishing inspection: the history controller without Qt, and one offscreen window
run (needs PySide6; skipped without it)."""

import json
import time

import pytest

pytest.importorskip("torch")
from kat.pcb.polish import case as pc, gui, solver as sv  # noqa: E402
from test_polish_case import EXAMPLE, tiny  # noqa: E402


def finished(n=12):
    s = sv.Solver(pc.load(EXAMPLE), limits={"max_iterations": n})
    return s, [e.to_json() for e in s.events()]


def test_the_controller_browses_without_branching_and_exports(tmp_path):
    s, evs = finished()
    ctl = gui.Controller(pc.load(EXAMPLE), None, solver=s)
    for e in evs[:5]:
        ctl.add(e)
    assert ctl.cursor == 4 and ctl.follow
    ctl.back()
    ctl.back()
    ctl.add(evs[5])                                      # the solver goes on; the view stays
    assert ctl.cursor == 2 and len(ctl.events) == 6
    ctl.reset()
    assert ctl.current()["type"] == "start"
    assert ctl.forward() and ctl.cursor == 1
    for e in evs[6:]:
        ctl.add(e)
    assert ctl.finished
    ctl.select(3)
    rec = ctl.export(tmp_path / "a", "current")
    res = json.loads((tmp_path / "a" / "result.json").read_text())
    assert res["seq"] == 3 and rec["events"] == len(evs)
    assert len((tmp_path / "a" / "history.jsonl").read_text().splitlines()) == len(evs)
    with pytest.raises(Exception, match="already holds"):
        ctl.export(tmp_path / "a")


def test_the_window_steps_runs_and_stops_cleanly(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    s = sv.Solver(pc.load(EXAMPLE), limits={"max_iterations": 15})
    ctl = gui.Controller(pc.load(EXAMPLE), None, solver=s)
    app, win = gui.open_window(ctl, s, tmp_path)

    def pump(until, limit=20.0):
        t = time.time()
        while not until() and time.time() - t < limit:
            app.processEvents()
            time.sleep(0.005)
    pump(lambda: len(ctl.events) >= 1)
    win.step()
    pump(lambda: len(ctl.events) >= 2)
    assert len(ctl.events) == 2                       # paused: one event per step
    win.toggle_run()
    pump(lambda: ctl.finished)
    assert ctl.finished and ctl.events[-1]["type"] == "stop"
    win.reset()
    win._render()
    assert ctl.cursor == 0 and win.dynamic
    assert not any((tmp_path / f).exists() for f in ("result.json", "run.json"))   # nothing written by itself
    win.close()
    assert not win.worker.isRunning()


def test_the_window_renders_snapped_directions_and_exports_their_indices(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    c = pc.loads(json.dumps(tiny(points=((2, 14), (6, 10), (12, 10)), holes=())))
    s = sv.Solver(c)
    ctl = gui.Controller(c, None, solver=s)
    ctl.add(next(s.events()).to_json())
    app, win = gui.open_window(ctl)
    try:
        win._render()
        assert len(win.dynamic) == 3
        ctl.export(tmp_path / "run", "current")
        res = json.loads((tmp_path / "run" / "result.json").read_text())
        assert res["traces"][0]["snapped"] == [0, 1]
    finally:
        win.close()


def test_added_vertex_markers_follow_history_and_survive_export(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    c = pc.loads(json.dumps(tiny(points=((2, 10), (18, 10)), holes=((10, 10.7, 0.5),))))
    s = sv.Solver(c, options={"settle_iterations": 5})
    events = [s._event("start", True).to_json()]
    before = s.state.copy()
    events.extend(e.to_json() for e in s._bend("t01", before.sid["t01"][0], before, s.total,
                                             s.short, type_="rescue"))
    assert events[-1]["accepted"] and events[-1]["added_vertices"] == {"t01": [1]}
    assert events[-1]["traces"]["t01"][1] != events[1]["traces"]["t01"][1]
    live = gui.Controller(c, None, events, solver=s)
    live.export(tmp_path / "run", "current")
    rec, recorded, res = gui._hist.load(tmp_path / "run")
    ctl = gui.Controller(c, rec["group"], recorded, result=res)
    app, win = gui.open_window(ctl)

    def markers():
        return [it for it in win.dynamic if it.toolTip().startswith("Automatically added vertex:")]

    try:
        for cursor in (1, len(recorded) - 1):
            ctl.select(cursor)
            win._render()
            marker, = markers()
            x, y = recorded[cursor]["traces"]["t01"][1]
            assert (marker.pos().x(), marker.pos().y()) == (x, -y)
            assert marker.brush().color().name() == win.COLORS["added"]
            size = marker.deviceTransform(win.view.viewportTransform()).mapRect(marker.rect()).size()
            win.view.scale(2, 2)
            assert marker.deviceTransform(win.view.viewportTransform()).mapRect(marker.rect()).size() == size
            assert "1 vertices (pink dots)" in win.details.toPlainText()
            assert win.view.hover_text(win.view.mapFromScene(x, -y)).startswith("t01 vertex 1 ")
        ctl.reset()
        win._render()
        assert not markers()
    finally:
        win.close()


def test_hovering_names_the_vertex_or_segment_under_the_mouse():
    c = pc.loads(json.dumps(tiny(points=((2, 10), (10, 10), (14, 14)), holes=())))
    ctl = gui.Controller(c, None, [sv.Solver(c)._event("start", True).to_json()])
    assert ctl.hover(10.05, 9.97, 0.1, 0.05) == "t01 vertex 1  x 10.000  y 10.000 mm"
    assert ctl.hover(2.0, 10.0, 0.1, 0.05).endswith("(anchored end)")
    assert ctl.hover(6, 10.08, 0.1, 0.05) == "t01 segment 0  angle 0.0 deg, snapped 0 deg"
    assert ctl.hover(12, 12.02, 0.1, 0.05) == "t01 segment 1  angle 45.0 deg, snapped 45 deg"
    assert ctl.hover(6, 11, 0.1, 0.05) is None
