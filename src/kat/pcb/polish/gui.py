"""The polisher's inspection window (``kat pcb polish run --gui`` / ``replay --gui``), Qt
(PySide6, extra ``kat[polish-gui]``).

The solver runs in a worker thread and only ever moves on when the window lets it (Step
releases one event, Run releases all, Pause none), so the window never freezes and a pause
keeps the last accepted state. The window only *browses* the recorded history: Back and
Forward move a cursor through it, Reset shows the original geometry; none of them creates a
new solver branch. Nothing is written except on Export (the best feasible state, or the
state shown), into a directory chosen then.

``Controller`` holds the history, the cursor and the export logic without Qt, for tests.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from ...env import KatError
from . import history as _hist, require
from .case import Case
from .constraints import Model
from .solver import Solver, result_doc


class Controller:
    """History and cursor of an inspection: ``events`` are event JSON objects (as in
    ``history.jsonl``); ``follow`` keeps the cursor on the newest event."""

    def __init__(self, case: Case, group: str | None, events: list[dict] | None = None, solver: Solver | None = None,
                 result: dict | None = None):
        self.case, self.solver = case, solver
        self.model = solver.model if solver else Model(case, group)
        self.group = self.model.group
        self.judge = Model(case, self.group) if solver else self.model   # no recovery floors: plain hard check
        self.events: list[dict] = list(events or [])
        self.result = result
        self.cursor = len(self.events) - 1 if self.events else -1
        self.follow = True
        self.finished = solver is None

    def add(self, ev: dict) -> None:
        """A new event from the solver."""
        self.events.append(ev)
        if self.follow:
            self.cursor = len(self.events) - 1
        if ev["type"] == "stop":
            self.finished = True

    def current(self) -> dict | None:
        """The event under the cursor."""
        return self.events[self.cursor] if 0 <= self.cursor < len(self.events) else None

    def back(self) -> None:
        """One event back (stops following the solver)."""
        self.follow = False
        self.cursor = max(0, self.cursor - 1)

    def forward(self) -> bool:
        """One event forward in the recorded history; False at its end."""
        if self.cursor < len(self.events) - 1:
            self.cursor += 1
            self.follow = self.cursor == len(self.events) - 1
            return True
        self.follow = True
        return False

    def reset(self) -> None:
        """Show the original geometry (the start event); the history stays."""
        self.follow = False
        self.cursor = 0

    def select(self, i: int) -> None:
        """Show event ``i``."""
        self.cursor = max(0, min(i, len(self.events) - 1))
        self.follow = self.cursor == len(self.events) - 1

    def best_seq(self) -> int:
        """The event holding the best feasible state."""
        if self.solver is not None:
            return self.solver.best[2]
        if self.result is not None:
            return self.result["seq"]
        return min((e for e in self.events if e["accepted"]), key=lambda e: e["total"])["seq"]

    def hit(self, x: float, y: float, vertex_tol: float, segment_tol: float) -> tuple[str, str, int] | None:
        """What lies under the point (x, y) (mm, the input's coordinates, y up) in the state
        shown: ``("vertex", trace, index)`` for the nearest vertex within ``vertex_tol`` mm,
        else ``("segment", trace, index)`` for the nearest segment within ``segment_tol`` mm
        or its half width; None when nothing is that close."""
        ev = self.current()
        if ev is None:
            return None
        from ..geometry import point_seg
        best = None
        for t, pts in ev["traces"].items():
            for i, (px, py) in enumerate(pts):
                d = math.hypot(px - x, py - y)
                if d <= vertex_tol and (best is None or d < best[0]):
                    best = (d, t, i)
        if best:
            return ("vertex",) + best[1:]
        for t, pts in ev["traces"].items():
            tol = max(segment_tol, self.case.traces[t].width / 2)
            for i, (a, b) in enumerate(zip(pts, pts[1:])):
                d = point_seg((x, y), a, b)
                if d <= tol and (best is None or d < best[0]):
                    best = (d, t, i)
        return ("segment",) + best[1:] if best else None

    def describe(self, hit: tuple[str, str, int] | None) -> str | None:
        """The hover text for a :meth:`hit`: trace, vertex index and position in mm, or
        trace, segment index and undirected angle in degrees; None for None."""
        if hit is None:
            return None
        kind, t, i = hit
        ev = self.current()
        pts = ev["traces"][t]
        if kind == "vertex":
            px, py = pts[i]
            note = (" (anchored end)" if i in (0, len(pts) - 1) else
                    " (added automatically)" if i in ev.get("added_vertices", {}).get(t, ()) else "")
            return f"{t} vertex {i}  x {px:.3f}  y {py:.3f} mm{note}"
        (ax, ay), (bx, by) = pts[i:i + 2]
        angle = math.degrees(math.atan2(by - ay, bx - ax)) % 180.0
        angle = 0.0 if angle > 180.0 - 5e-4 else angle
        snap = f", snapped {round(angle / 45.0) % 4 * 45} deg" if i in ev["snapped"].get(t, ()) else ""
        return f"{t} segment {i}  angle {angle:.1f} deg{snap}"

    def hover(self, x: float, y: float, vertex_tol: float, segment_tol: float) -> str | None:
        """The hover text at (x, y) (mm, y up): :meth:`describe` of :meth:`hit`."""
        return self.describe(self.hit(x, y, vertex_tol, segment_tol))

    def export(self, out: Path | str, which: str = "best", force: bool = False,
               result_file: Path | None = None) -> dict:
        """Write run.json, history.jsonl (the events so far) and result.json (``which``:
        "best" feasible state or the "current" one) into ``out``; refused over an existing
        run unless ``force``. Also copies the candidate to ``result_file`` when supplied.
        Returns the run record."""
        if not self.events:
            raise KatError("nothing to export yet")
        d = _hist.prepare(out, force, result_file)
        ev = self.events[self.best_seq() if which == "best" else self.cursor]
        first = self.events[0]
        solver_id = (self.solver.info()["solver"] if self.solver else {"name": "kat-polish", "version": "?", "merit": "?"})
        termination = (self.solver.termination if self.solver else None) or ("paused" if not self.finished else
                                                                              self.events[-1]["reason"])
        pts = {k: _np().asarray(v, float) for k, v in ev["traces"].items()}
        start = {k: _np().asarray(v, float) for k, v in first["traces"].items()}
        res = result_doc(self.case, self.group, ev["seq"], {k: v for k, v in ev["traces"].items()}, ev["snapped"],
                         ev["total"], ev["terms"], (first["total"], first["terms"]), self.model.min_slack(pts),
                         termination, solver_id, self.judge.violations(pts), self.judge.violations(start))
        with open(d / "history.jsonl", "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e) + "\n")
        rec = {"format": "route-polish-run", "version": 1,
               "input": {"sha256": self.case.sha256, "format_version": self.case.version}, "group": self.group,
               **(self.solver.info() if self.solver else {"solver": solver_id, "options": None}),
               "termination": termination, "events": len(self.events), "best_seq": ev["seq"]}
        (d / "result.json").write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
        if result_file is not None:
            result_file.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
        (d / "run.json").write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
        return rec


def _np():
    return require("numpy")


# --- Qt ------------------------------------------------------------------------------------

def _qt():
    require("PySide6.QtWidgets")
    from PySide6 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _classes():
    QtCore, QtGui, QtWidgets = _qt()

    class Worker(QtCore.QThread):
        """Runs ``solver.events()``, waiting for a permit between events."""
        produced = QtCore.Signal(dict)

        def __init__(self, solver: Solver):
            super().__init__()
            self.solver = solver
            self.cond = threading.Condition()
            self.permits, self.free, self.quitting = 0, False, False

        def run(self):
            for ev in self.solver.events():
                self.produced.emit(ev.to_json())
                with self.cond:
                    while not (self.free or self.permits > 0 or self.quitting):
                        self.cond.wait()
                    if self.permits > 0 and not self.free:
                        self.permits -= 1

        def allow(self, n: int = 1, free: bool | None = None):
            with self.cond:
                self.permits += n
                if free is not None:
                    self.free = free
                self.cond.notify_all()

        def quit_run(self):
            self.solver.stop()
            with self.cond:
                self.quitting = True
                self.cond.notify_all()

    class View(QtWidgets.QGraphicsView):
        """Wheel zoom, drag pan, and a tooltip naming the vertex (8 px) or segment (4 px)
        under the mouse, which is highlighted."""
        VERTEX_PX, SEGMENT_PX = 8, 4
        HIGHLIGHT = "#00b4d8"

        def __init__(self, scene, ctl: Controller):
            super().__init__(scene)
            self.ctl = ctl
            self.hovered = None                            # the Controller.hit under the mouse
            self.highlight = []                            # its scene items
            self.mouse = None                              # last viewport position
            self.setRenderHint(QtGui.QPainter.Antialiasing)
            self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
            self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
            self.viewport().setMouseTracking(True)

        def hit_at(self, pos):
            """The Controller.hit at viewport position ``pos``."""
            p = self.mapToScene(pos)
            mm = 1.0 / max(abs(self.transform().m11()), 1e-9)
            return self.ctl.hit(p.x(), -p.y(), self.VERTEX_PX * mm, self.SEGMENT_PX * mm)

        def hover_text(self, pos) -> str | None:
            """The hover text at viewport position ``pos``."""
            return self.ctl.describe(self.hit_at(pos))

        def set_hovered(self, hit, force=False):
            """Highlight ``hit`` (a Controller.hit or None) in the state shown."""
            if hit == self.hovered and not force:
                return
            for it in self.highlight:
                self.scene().removeItem(it)
            self.highlight, self.hovered = [], hit
            ev = self.ctl.current()
            if hit is None or ev is None or hit[1] not in ev["traces"]:
                return
            kind, t, i = hit
            pts = ev["traces"][t]
            if i + (kind == "segment") >= len(pts):
                self.hovered = None
                return
            color = QtGui.QColor(self.HIGHLIGHT)
            if kind == "vertex":
                x, y = pts[i]
                it = self.scene().addEllipse(-7, -7, 14, 14, QtGui.QPen(color, 2.5), QtGui.QBrush(QtCore.Qt.NoBrush))
                it.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations)
                it.setPos(x, -y)
            else:
                (ax, ay), (bx, by) = pts[i:i + 2]
                color.setAlpha(170)
                for width, cosmetic in ((self.ctl.case.traces[t].width * 1.6, False), (5, True)):
                    pen = QtGui.QPen(color, width)                 # the wider of trace scale and 5 px
                    pen.setCapStyle(QtCore.Qt.RoundCap)
                    pen.setCosmetic(cosmetic)
                    self.highlight.append(self.scene().addLine(ax, -ay, bx, -by, pen))
            if kind == "vertex":
                self.highlight.append(it)
            for it in self.highlight:
                it.setZValue(2)
                it.setAcceptedMouseButtons(QtCore.Qt.NoButton)

        def refresh_hover(self):
            """Redo the hover for the state shown, after it changed under the mouse."""
            self.highlight = [it for it in self.highlight if it.scene() is not None]
            self.set_hovered(self.hit_at(self.mouse) if self.mouse is not None else None, force=True)

        def mouseMoveEvent(self, e):
            super().mouseMoveEvent(e)
            pos = self.mouse = e.position().toPoint()
            hit = self.hit_at(pos)
            self.set_hovered(hit)
            text = self.ctl.describe(hit)
            if text:
                QtWidgets.QToolTip.showText(self.viewport().mapToGlobal(pos), text, self.viewport())
            else:
                QtWidgets.QToolTip.hideText()

        def leaveEvent(self, e):
            self.mouse = None
            self.set_hovered(None)
            super().leaveEvent(e)

        def viewportEvent(self, e):
            if e.type() == QtCore.QEvent.ToolTip:          # the hover text replaces item tooltips
                return True
            return super().viewportEvent(e)

        def wheelEvent(self, e):
            f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
            self.scale(f, f)

    class Window(QtWidgets.QMainWindow):
        """Board view, controls, event list and the details of the event shown."""

        COLORS = {"board": "#f4f1e8", "edge": "#6b6b6b", "region": "#3a7bd5", "keepout": "#d9534f",
                  "fixed": "#9a8f80", "hole": "#222222", "orig": "#c9a46a", "cur": "#c46a00",
                  "snapped": "#2e8b57", "cand": "#d0021b", "env": "#f0ad4e", "anchor": "#1f4e79",
                  "added": "#e91e63"}

        def __init__(self, ctl: Controller, worker: Worker | None, out: Path | None, force: bool,
                     result_file: Path | None = None):
            super().__init__()
            self.ctl, self.worker, self.out, self.force = ctl, worker, out, force
            self.result_file = result_file
            self.exported: list[str] = []
            self.running = False
            self.dirty = True
            self.setWindowTitle(f"kat pcb polish - group {ctl.group}")
            self.scene = QtWidgets.QGraphicsScene()
            self.view = View(self.scene, ctl)
            self.view.scale(30, 30)
            self.dynamic: list = []
            self.context: list = []
            self._draw_static()

            bar = QtWidgets.QHBoxLayout()
            self.buttons = {}
            for name, fn in (("Step", self.step), ("Run", self.toggle_run), ("Back", self.back),
                             ("Forward", self.forward), ("Reset", self.reset), ("Fit all", self.fit_all),
                             ("Export...", self.export_dialog)):
                b = QtWidgets.QPushButton(name)
                b.clicked.connect(fn)
                bar.addWidget(b)
                self.buttons[name] = b
            self.checks = {}
            for name, on in (("original", True), ("candidate", True), ("clearance envelope", False), ("context", True)):
                c = QtWidgets.QCheckBox(name)
                c.setChecked(on)
                c.toggled.connect(self.redraw)
                bar.addWidget(c)
                self.checks[name] = c
            self.buttons["Fit all"].setToolTip("show the whole geometry (F)")
            QtGui.QShortcut(QtGui.QKeySequence("F"), self, self.fit_all)
            bar.addStretch()
            self.which = QtWidgets.QComboBox()
            self.which.addItems(["export best feasible", "export shown state"])
            bar.addWidget(self.which)

            self.list = QtWidgets.QListWidget()
            self.list.currentRowChanged.connect(self._pick)
            self.details = QtWidgets.QPlainTextEdit()
            self.details.setReadOnly(True)
            self.details.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
            side = QtWidgets.QSplitter(QtCore.Qt.Vertical)
            side.addWidget(self.details)
            side.addWidget(self.list)
            split = QtWidgets.QSplitter()
            split.addWidget(self.view)
            split.addWidget(side)
            split.setSizes([900, 380])
            box = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(box)
            lay.addLayout(bar)
            lay.addWidget(split)
            self.setCentralWidget(box)
            self.resize(1300, 850)
            for e in ctl.events:
                self._list_add(e)
            if worker is not None:
                worker.produced.connect(self.on_event)
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(40)
            self.fitted = False

        def showEvent(self, e):
            super().showEvent(e)
            if not self.fitted:
                self.fitted = True
                QtCore.QTimer.singleShot(0, self.fit)

        def fit(self):
            """Zoom to the movement region and a little around it."""
            xs = [p[0] for p in self.ctl.case.region]
            ys = [-p[1] for p in self.ctl.case.region]
            rect = QtCore.QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)).adjusted(-1, -1, 1, 1)
            self.view.fitInView(rect, QtCore.Qt.KeepAspectRatio)

        def fit_all(self):
            """Zoom so that everything drawn fits the view, aspect ratio kept (button Fit all, key F)."""
            rect = self.scene.itemsBoundingRect()
            m = 0.02 * max(rect.width(), rect.height())
            rect = rect.adjusted(-m, -m, m, m)
            w, h = rect.width(), rect.height()               # room to centre it: no clamping at the edge
            self.view.setSceneRect(rect.adjusted(-w, -h, w, h))
            self.view.fitInView(rect, QtCore.Qt.KeepAspectRatio)

        # drawing; scene units are mm with y flipped (the input's y points up)
        def _pen(self, color, width, style=None, alpha=255):
            c = QtGui.QColor(color)
            c.setAlpha(alpha)
            p = QtGui.QPen(c, width)
            p.setCapStyle(QtCore.Qt.RoundCap)
            p.setJoinStyle(QtCore.Qt.RoundJoin)
            if style is not None:
                p.setStyle(style)
            return p

        def _poly(self, pts):
            return QtGui.QPolygonF([QtCore.QPointF(x, -y) for x, y in pts])

        def _path(self, pts):
            path = QtGui.QPainterPath(QtCore.QPointF(pts[0][0], -pts[0][1]))
            for x, y in pts[1:]:
                path.lineTo(x, -y)
            return path

        def _draw_static(self):
            c, col, s = self.ctl.case, self.COLORS, self.scene
            nobrush = QtGui.QBrush(QtCore.Qt.NoBrush)
            s.addPolygon(self._poly(c.outline), self._pen(col["edge"], 0.05), QtGui.QBrush(QtGui.QColor(col["board"])))
            for cut in c.cutouts:
                s.addPolygon(self._poly(cut), self._pen(col["edge"], 0.05), QtGui.QBrush(QtGui.QColor("white")))
            s.addPolygon(self._poly(c.region), self._pen(col["region"], 0.04, QtCore.Qt.DashLine), nobrush)
            for k in c.keepouts.values():
                s.addPolygon(self._poly(k.points), self._pen(col["keepout"], 0.04),
                             QtGui.QBrush(QtGui.QColor(col["keepout"]), QtCore.Qt.BDiagPattern))
            fixed = QtGui.QColor(col["fixed"])
            for o in c.copper.values():
                if o.kind == "polyline":
                    it = s.addPath(self._path(o.points), self._pen(col["fixed"], o.width), nobrush)
                elif o.kind == "circle":
                    r = o.radius
                    it = s.addEllipse(o.center[0] - r, -o.center[1] - r, 2 * r, 2 * r, QtGui.QPen(QtCore.Qt.NoPen),
                                      QtGui.QBrush(fixed))
                else:
                    it = s.addPolygon(self._poly(o.points), QtGui.QPen(QtCore.Qt.NoPen), QtGui.QBrush(fixed))
                self.context.append(it)
            for t in c.traces.values():
                if t.id not in self.ctl.model.ids:
                    self.context.append(s.addPath(self._path(t.points), self._pen(col["fixed"], t.width), nobrush))
            for h in c.holes.values():
                r = h.radius
                s.addEllipse(h.center[0] - r, -h.center[1] - r, 2 * r, 2 * r, QtGui.QPen(QtCore.Qt.NoPen),
                             QtGui.QBrush(QtGui.QColor(col["hole"])))
            self.orig_items = []
            for t in self.ctl.model.ids:
                tr = c.traces[t]
                self.orig_items.append(s.addPath(self._path(tr.points), self._pen(col["orig"], tr.width, alpha=110), nobrush))
                for p in (tr.points[0], tr.points[-1]):
                    s.addEllipse(p[0] - 0.08, -p[1] - 0.08, 0.16, 0.16, QtGui.QPen(QtCore.Qt.NoPen),
                                 QtGui.QBrush(QtGui.QColor(col["anchor"])))

        def redraw(self, *_):
            self.dirty = True

        def _tick(self):
            if self.running and self.worker is None:          # replay: play the recorded history
                if not self.ctl.forward():
                    self.toggle_run()
                self.dirty = True
            if self.dirty:
                self.dirty = False
                self._render()

        def _render(self):
            for it in self.dynamic:
                self.scene.removeItem(it)
            self.dynamic = []
            for it in self.context:
                it.setVisible(self.checks["context"].isChecked())
            for it in self.orig_items:
                it.setVisible(self.checks["original"].isChecked())
            ev = self.ctl.current()
            if ev is None:
                return
            col, nobrush, case = self.COLORS, QtGui.QBrush(QtCore.Qt.NoBrush), self.ctl.case
            for t, pts in ev["traces"].items():
                w = case.traces[t].width
                if self.checks["clearance envelope"].isChecked():
                    self.dynamic.append(self.scene.addPath(self._path(pts), self._pen(col["env"], w + 2 * case.copper_clearance,
                                                                                     alpha=70), nobrush))
                self.dynamic.append(self.scene.addPath(self._path(pts), self._pen(col["cur"], w), nobrush))
                for i in ev["snapped"].get(t, []):
                    self.dynamic.append(self.scene.addPath(self._path(pts[i:i + 2]), self._pen(col["snapped"], w * 0.45), nobrush))
            if ev.get("candidate") and self.checks["candidate"].isChecked():
                for t, pts in ev["candidate"].items():
                    self.dynamic.append(self.scene.addPath(self._path(pts), self._pen(col["cand"], case.traces[t].width * 0.6,
                                                                                     QtCore.Qt.DashLine), nobrush))
            if ev.get("target"):
                t, i = ev["target"].split("#")
                pts = (ev.get("candidate") or ev["traces"]).get(t) or ev["traces"][t]
                i = int(i)
                if i + 1 < len(pts):
                    self.dynamic.append(self.scene.addPath(self._path(pts[i:i + 2]),
                                                           self._pen("#7b2cbf", case.traces[t].width * 2.2, alpha=90), nobrush))
            for t, indices in ev.get("added_vertices", {}).items():
                for i in indices:
                    x, y = ev["traces"][t][i]
                    marker = self.scene.addEllipse(-5, -5, 10, 10, self._pen("white", 1.5),
                                                   QtGui.QBrush(QtGui.QColor(col["added"])))
                    marker.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations)
                    marker.setPos(x, -y)
                    marker.setZValue(1)
                    marker.setToolTip(f"Automatically added vertex: {t}#{i}")
                    self.dynamic.append(marker)
            self.view.refresh_hover()
            self._details(ev)
            if self.list.currentRow() != self.ctl.cursor:
                self.list.blockSignals(True)
                self.list.setCurrentRow(self.ctl.cursor)
                self.list.blockSignals(False)

        def _details(self, ev):
            ms = ev["min_slack"]
            lines = [f"event {ev['seq']}   iteration {ev['iter']}   {ev['type']}   "
                     f"{'accepted' if ev['accepted'] else 'REJECTED'}",
                     f"reason   {ev['reason'] or '-'}", f"target   {ev['target'] or '-'}",
                     f"added    {sum(len(v) for v in ev.get('added_vertices', {}).values())} vertices (pink dots)", "",
                     f"total    {ev['total']:.5f}"]
            w = self.ctl.solver.objectives["weights"] if self.ctl.solver else self.ctl.case.objectives["weights"]
            first = self.ctl.events[0]["terms"]
            for k, v in ev["terms"].items():
                lines.append(f"  {k:<13}{v:9.5f}  x{w[k]:<5g} (start {first[k]:.5f})")
            lines += ["", f"min slack {ms['value']:.4f} mm   {ms['a']} - {ms['b']}",
                      ("constraints  all hard constraints hold (state after the event)" if not ev.get("shortfall") else
                       f"constraints  {ev['shortfall']:.4f} mm short of clearance in total (recovering)"), "",
                      f"history  {len(self.ctl.events)} events"
                      + ("" if self.ctl.finished else (", running" if self.running else ", paused")),
                      f"best     event {self.ctl.best_seq()}"]
            self.details.setPlainText("\n".join(lines))

        def _list_add(self, e):
            mark = "+" if e["accepted"] else "x"
            text = f"{e['seq']:>5} {e['iter']:>5} {mark} {e['type']:<7} {e['target'] or ''} {e['reason'] or ''}"
            item = QtWidgets.QListWidgetItem(text)
            if not e["accepted"]:
                item.setForeground(QtGui.QColor(self.COLORS["cand"]))
            self.list.addItem(item)

        def _pick(self, row):
            if row >= 0:
                self.ctl.select(row)
                self.redraw()

        # controls
        def on_event(self, ev):
            self.ctl.add(ev)
            self._list_add(ev)
            if self.ctl.finished:
                self.running = False
                self.buttons["Run"].setText("Run")
            self.redraw()

        def step(self):
            if not self.ctl.forward() and self.worker is not None and not self.ctl.finished:
                self.worker.allow(1)
            self.redraw()

        def toggle_run(self):
            if self.running:
                self.running = False
                if self.worker is not None:
                    self.worker.allow(0, free=False)
                self.buttons["Run"].setText("Run")
            else:
                self.running = True
                self.ctl.follow = True
                if self.worker is not None and not self.ctl.finished:
                    self.worker.allow(0, free=True)
                self.buttons["Run"].setText("Pause")
            self.redraw()

        def back(self):
            self.ctl.back()
            self.redraw()

        def forward(self):
            self.ctl.forward()
            self.redraw()

        def reset(self):
            if self.running:
                self.toggle_run()
            self.ctl.reset()
            self.redraw()

        def export_dialog(self):
            if self.running:
                self.toggle_run()
            start = str(self.out) if self.out else str(Path.cwd())
            d = QtWidgets.QFileDialog.getExistingDirectory(self, "Export the candidate into", start)
            if not d:
                return
            force = self.force
            if (any((Path(d) / f).exists() for f in _hist.FILES)
                    or (self.result_file is not None and self.result_file.exists())) and not force:
                ok = QtWidgets.QMessageBox.question(self, "Overwrite?", "Output already exists. Overwrite it?")
                if ok != QtWidgets.QMessageBox.Yes:
                    return
                force = True
            which = "best" if self.which.currentIndex() == 0 else "current"
            try:
                self.ctl.export(d, which, force, self.result_file)
                self.exported.append(d)
                self.statusBar().showMessage(f"exported the {which} state to {d}", 8000)
            except KatError as e:
                QtWidgets.QMessageBox.warning(self, "Export failed", str(e))

        def closeEvent(self, e):
            if self.worker is not None:
                self.worker.quit_run()
                self.worker.wait(10000)
            super().closeEvent(e)

    return Worker, Window


def _app():
    _, _, QtWidgets = _qt()
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(["kat"])


def open_window(ctl: Controller, solver: Solver | None = None, out: Path | None = None, force: bool = False,
                result_file: Path | None = None):
    """The window on ``ctl`` (live when ``solver`` is given; its worker thread is started),
    without entering the event loop: (app, window)."""
    Worker, Window = _classes()
    app = _app()
    worker = Worker(solver) if solver is not None else None
    win = Window(ctl, worker, out, force, result_file)
    if worker is not None:
        worker.start()
    return app, win


def run_live(case: Case, out: Path, group=None, weights=None, options=None, limits=None, force=False,
             result_file: Path | None = None) -> dict:
    """Open the window on a new run of ``case`` (paused after the start event) and block
    until it closes. Returns the export directories and the history length."""
    solver = Solver(case, group, weights, options, limits)
    ctl = Controller(case, group, solver=solver)
    app, win = open_window(ctl, solver, out, force, result_file)
    win.show()
    app.exec()
    return {"exported": win.exported, "events": len(ctl.events), "termination": solver.termination or "user-stop"}


def run_replay(run_dir: Path, case: Case) -> dict:
    """Open the window on a finished run's history (``case``: its input) and block until it
    closes."""
    rec, events, res = _hist.load(run_dir)
    if rec["input"]["sha256"] != case.sha256:
        raise KatError("the case file is not the input of this run (sha256 differs)")
    ctl = Controller(case, rec["group"], events, result=res)
    ctl.select(0)
    app, win = open_window(ctl)
    win.show()
    app.exec()
    return {"exported": win.exported, "events": len(events)}


__all__ = ["Controller", "open_window", "run_live", "run_replay"]
