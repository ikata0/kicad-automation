"""Route polishing inspection: the history controller without Qt, and one offscreen window
run (needs PySide6; skipped without it)."""

import json
import time

import pytest

pytest.importorskip("torch")
from kat.pcb.polish import case as pc, gui, solver as sv  # noqa: E402
from test_polish_case import EXAMPLE  # noqa: E402


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
