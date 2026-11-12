"""The polishing solver: continuous moves on the merit's gradient, discrete snaps and bends,
every change of state a checked straight move (``Model.sweep``).

One **iteration** is one optimiser step: ``torch.optim.Adam`` proposes new interior
vertices from the merit plus the barrier; the proposal is projected so that snapped
segments keep their exact direction, then shortened (1, 1/2, 1/4, ...) until the move is
clear all the way and the new state passes the hard check. Discrete edits are **trials**:
``snap`` (fix a segment at its nearest multiple of 45 degrees), ``adjust`` (weight that
segment's angle heavily for a while so that its neighbours make room, then snap) and
``bend`` (split it, as an octilinear dog-leg or freely), each settled by some iterations and
then committed or reverted. A collinear vertex is ``merge``d away. Snapped segments stay
snapped. ``Solver.events`` yields every event; the best feasible state is kept apart.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from ..trackclean import _replacements
from . import require
from .case import Case
from .constraints import Model
from .merit import BEND, TERMS, VERSION as MERIT_VERSION, Merit, deviation_deg, turns_deg

NAME, VERSION = "kat-polish", "0.1"
DEFAULTS = {"learning_rate": 0.02, "settle_iterations": 20, "stagnation_window": 20, "stagnation_tolerance": 1e-4,
            "snap_slack": 0.0, "bend_gain": 0.005, "max_attempts_per_segment": 1, "max_substeps": 64,
            "barrier_margin_mm": 0.05, "adjust_boost": 100.0}
BACKTRACK = (1.0, 0.5, 0.25, 0.125, 0.0625)
EXACT = 1e-9                # degrees: a segment this close to 45-degree multiples counts as exact


@dataclass
class Event:
    """One step of the history (``specs/ROUTE_POLISH_OUTPUT.md`` §2): ``traces`` is the
    accepted state *after* the event, ``candidate`` a rejected proposal."""
    seq: int
    iter: int
    type: str
    accepted: bool
    reason: str | None
    target: str | None
    traces: dict
    snapped: dict
    terms: dict
    total: float
    min_slack: dict
    candidate: dict | None = None

    def to_json(self) -> dict:
        """The event as a JSON object (points as lists)."""
        d = {"seq": self.seq, "iter": self.iter, "type": self.type, "accepted": self.accepted, "reason": self.reason,
             "target": self.target, "traces": _lists(self.traces), "snapped": self.snapped, "terms": self.terms,
             "total": self.total, "min_slack": self.min_slack}
        if self.candidate is not None:
            d["candidate"] = _lists(self.candidate)
        return d


def _lists(pts: dict) -> dict:
    return {k: [[float(x), float(y)] for x, y in v] for k, v in pts.items()}


@dataclass
class State:
    """Points per trace, a stable id per segment, and the snapped segments {id: direction
    0-3, in steps of 45 degrees, undirected}."""
    pts: dict
    sid: dict
    snapped: dict = field(default_factory=dict)

    def copy(self) -> "State":
        """A deep copy."""
        return State({k: v.copy() for k, v in self.pts.items()}, {k: list(v) for k, v in self.sid.items()},
                     {k: dict(v) for k, v in self.snapped.items()})


def direction(a, b) -> int:
    """The nearest multiple of 45 degrees of segment a-b, as 0-3 (undirected)."""
    return round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) / 45.0) % 4


def project(pts: np.ndarray, snapped_idx: dict) -> np.ndarray | None:
    """``pts`` moved as little as possible (least squares, interior vertices only) so that
    each segment index in ``snapped_idx`` {index: direction 0-3} lies exactly on its
    direction; None when the directions cannot all hold between the fixed ends."""
    if not snapped_idx:
        return pts
    n = len(pts)
    rows, rhs = [], []
    for k, q in snapped_idx.items():
        ang = math.radians(45.0 * q)
        nx, ny = -math.sin(ang), math.cos(ang)
        row = np.zeros(2 * n)
        row[2 * (k + 1)], row[2 * (k + 1) + 1] = nx, ny
        row[2 * k], row[2 * k + 1] = -nx, -ny
        rows.append(row)
    A = np.array(rows)
    free = np.zeros(2 * n, bool)
    free[2:-2] = True
    x = pts.ravel().copy()
    Af, b = A[:, free], -A[:, ~free] @ x[~free]
    r = Af @ x[free] - b
    if Af.size:
        lam, *_ = np.linalg.lstsq(Af @ Af.T, r, rcond=None)
        x[free] = x[free] - Af.T @ lam
    if np.abs(A @ x).max() > 1e-10:
        return None
    return x.reshape(n, 2)


class Solver:
    """A polishing run on ``case``'s group ``group``: ``weights``, ``options`` (solver knobs,
    ``DEFAULTS``) and ``limits`` (the input's names) override the case's values."""

    def __init__(self, case: Case, group: str | None = None, weights: dict | None = None,
                 options: dict | None = None, limits: dict | None = None):
        self.torch = require("torch")
        self.case = case
        self.limits = {**case.limits, **(limits or {})}
        case.limits = self.limits                     # the model's displacement limit reads it
        self.objectives = {**case.objectives, "weights": {**case.objectives["weights"], **(weights or {})}}
        bad = [k for k in (weights or {}) if k not in TERMS] + [k for k in (options or {}) if k not in DEFAULTS]
        if bad:
            from ...env import KatError
            raise KatError(f"unknown option(s) {', '.join(bad)}: weights {', '.join(TERMS)}; options {', '.join(DEFAULTS)}")
        self.opt = {**DEFAULTS, **(options or {})}
        self.model = Model(case, group)
        self.group = self.model.group
        self.merit = Merit(self.model, self.objectives, self.opt["barrier_margin_mm"])
        self.torch.manual_seed(self.limits["seed"])
        ids = self.model.ids
        self._next = 0
        sid = {}
        for t in ids:
            sid[t] = [self._new_id() for _ in range(len(self.model.orig[t]) - 1)]
        self.state = State(self.model.initial(), sid, {t: {} for t in ids})
        for t in ids:                                  # already exact: snapped from the start
            p = self.state.pts[t]
            for i, s in enumerate(sid[t]):
                if deviation_deg(p[i], p[i + 1]) < EXACT:
                    self.state.snapped[t][s] = direction(p[i], p[i + 1])
        self.iter = self.seq = 0
        self.inserted = defaultdict(int)
        self.attempts = defaultdict(int)
        self.no_merge: set = set()
        self.in_trial = False
        self.stop_requested = False
        self.termination: str | None = None
        self.lr = self.opt["learning_rate"]
        self.total, self.terms = self.merit.evaluate(self.state.pts)
        self.initial = (self.total, dict(self.terms))
        self.best = (self.total, self.state.copy(), 0, dict(self.terms))
        self._reset_stagnation()
        self._rebuild()

    # --- plumbing -----------------------------------------------------------------------

    def _new_id(self) -> int:
        self._next += 1
        return self._next

    def _reset_stagnation(self) -> None:
        self.phase_best, self.since = self.total, 0

    def _rebuild(self) -> None:
        torch = self.torch
        ids = self.model.ids
        self.X = torch.tensor(np.concatenate([self.state.pts[t][1:-1].ravel() for t in ids]),
                              dtype=torch.float64, requires_grad=True)
        self.adam = torch.optim.Adam([self.X], lr=self.lr)

    def _tensors(self, X) -> dict:
        torch = self.torch
        out, i = {}, 0
        for t in self.model.ids:
            p = self.state.pts[t]
            n = 2 * (len(p) - 2)
            ends = torch.as_tensor(p[[0, -1]], dtype=torch.float64)
            out[t] = torch.cat([ends[:1], X[i:i + n].view(-1, 2), ends[1:]])
            i += n
        return out

    def _from(self, x: np.ndarray) -> dict:
        out, i = {}, 0
        for t in self.model.ids:
            p = self.state.pts[t].copy()
            n = 2 * (len(p) - 2)
            p[1:-1] = x[i:i + n].reshape(-1, 2)
            out[t] = p
            i += n
        return out

    def _snapped_idx(self, st: State | None = None) -> dict:
        st = st or self.state
        return {t: {st.sid[t].index(s): q for s, q in st.snapped[t].items()} for t in self.model.ids}

    def _project_all(self, pts: dict, st: State | None = None) -> dict | None:
        idx = self._snapped_idx(st)
        out = {}
        for t in self.model.ids:
            p = project(pts[t], idx[t])
            if p is None:
                return None
            out[t] = p
        return out

    def _event(self, type_: str, accepted: bool, reason=None, target=None, candidate=None, known=None) -> Event:
        self.total, self.terms = known or self.merit.evaluate(self.state.pts)
        if accepted and (not self.in_trial or type_ == "commit") and self.total < self.best[0] - 1e-12:
            self.best = (self.total, self.state.copy(), self.seq, dict(self.terms))
        ev = Event(self.seq, self.iter, type_, accepted, reason, target,
                   {k: v.copy() for k, v in self.state.pts.items()},
                   {t: sorted(v.values()) for t, v in self._snapped_idx().items()},
                   dict(self.terms), self.total, self.model.min_slack(self.state.pts), candidate)
        self.seq += 1
        return ev

    def _ref(self, t: str, s: int) -> str:
        return f"{t}#{self.state.sid[t].index(s)}"

    def all_snapped(self) -> bool:
        """Whether every segment of the group is snapped."""
        return all(len(self.state.snapped[t]) == len(self.state.sid[t]) for t in self.model.ids)

    # --- continuous ---------------------------------------------------------------------

    def _step(self, boost: dict | None = None):
        """One iteration; yields its event, returns whether it moved."""
        self.iter += 1
        cur = self.state.pts
        self.adam.zero_grad()
        P = self._tensors(self.X)
        loss = self.merit.total(self.merit.terms(P, boost)) + self.merit.barrier(P)
        loss.backward()
        level = float(loss.detach())
        self.adam.step()
        prop = self._project_all(self._from(self.X.detach().numpy()))
        reason = "projection"
        if prop is not None:
            for alpha in BACKTRACK:
                cand = {t: cur[t] + alpha * (prop[t] - cur[t]) for t in cur}
                value, known = self._loss(cand, boost)
                if value > level + 1e-12:
                    reason = "no descent"
                    continue
                reason = self.model.sweep(cur, cand, self.opt["max_substeps"])
                if reason is None:
                    self.state.pts = cand
                    with self.torch.no_grad():
                        self.X.copy_(self.torch.as_tensor(np.concatenate([cand[t][1:-1].ravel() for t in cand])))
                    self.lr = min(self.opt["learning_rate"], self.lr * 1.1)
                    self.adam.param_groups[0]["lr"] = self.lr
                    ev = self._event("step", True, known=known)
                    if ev.total < self.phase_best - self.opt["stagnation_tolerance"]:
                        self.phase_best, self.since = ev.total, 0
                    else:
                        self.since += 1
                    yield ev
                    return True
        with self.torch.no_grad():
            self.X.copy_(self.torch.as_tensor(np.concatenate([cur[t][1:-1].ravel() for t in cur])))
        self.lr *= 0.5
        self.adam.param_groups[0]["lr"] = self.lr
        self.since += 1
        yield self._event("step", False, reason, candidate=prop)
        return False

    def _loss(self, pts: dict, boost: dict | None = None) -> tuple[float, tuple | None]:
        """What the optimiser minimises (merit plus barrier) at numpy state ``pts``, and
        (total, terms) of the merit when there is no ``boost``."""
        with self.torch.no_grad():
            P = {k: self.torch.as_tensor(v, dtype=self.torch.float64) for k, v in pts.items()}
            terms = self.merit.terms(P, boost)
            total = self.merit.total(terms)
            known = None if boost else (float(total), {k: float(v) for k, v in terms.items()})
            return float(total + self.merit.barrier(P)), known

    def _settle(self, boost: dict | None = None):
        for _ in range(self.opt["settle_iterations"]):
            if self.iter >= self.limits["max_iterations"] or self.stop_requested:
                break
            yield from self._step(boost)

    # --- discrete -----------------------------------------------------------------------

    def _restore(self, st: State) -> None:
        self.state = st.copy()
        self._rebuild()

    def _apply(self, pts: dict, st: State) -> None:
        self.state = State(pts, st.sid, st.snapped)
        self._rebuild()

    def _commit(self, before: State, total_before: float, need_gain: float, target: str):
        """Ends a trial: kept when the merit is at most ``total_before - need_gain``."""
        if self.stop_requested:
            self._restore(before)
            self.in_trial = False
            yield self._event("commit", False, "user stop", target)
            return False
        total, _ = self.merit.evaluate(self.state.pts)
        ok = total <= total_before - need_gain + 1e-12
        if not ok:
            self._restore(before)
        self.in_trial = False
        yield self._event("commit", ok, None if ok else ("merit worse" if need_gain <= 0 else "no gain"), target)
        self._reset_stagnation()
        return ok

    def _snap_move(self, t: str, s: int, type_: str):
        """Snap segment ``s`` of trace ``t`` where it is now; yields the event, returns
        whether the state changed (a trial is then open)."""
        st = self.state.copy()
        i = st.sid[t].index(s)
        p = st.pts[t]
        st.snapped[t][s] = direction(p[i], p[i + 1])
        target = self._ref(t, s)
        prop = self._project_all(st.pts, st)
        if prop is None:
            yield self._event(type_, False, "inconsistent directions", target)
            return False
        reason = self.model.sweep(self.state.pts, prop, self.opt["max_substeps"])
        if reason:
            yield self._event(type_, False, reason, target, candidate=prop)
            return False
        self._apply(prop, st)
        self.in_trial = True
        yield self._event(type_, True, None, target)
        return True

    def _trial(self, t: str, s: int):
        before, total_before = self.state.copy(), self.total
        target = self._ref(t, s)
        # 1: snap where it is
        if (yield from self._snap_move(t, s, "snap")):
            yield from self._settle()
            if (yield from self._commit(before, total_before, -self.opt["snap_slack"], target)):
                return
        # 2: let the group make room, then snap
        if not self.stop_requested and self.iter < self.limits["max_iterations"]:
            self.in_trial = True
            yield self._event("adjust", True, None, target)
            i = self.state.sid[t].index(s)
            boost = {t: self.torch.ones(len(self.state.sid[t]), dtype=self.torch.float64)}
            boost[t][i] = self.opt["adjust_boost"]
            yield from self._settle(boost)
            if (yield from self._snap_move(t, s, "snap")):
                yield from self._settle()
                if (yield from self._commit(before, total_before, -self.opt["snap_slack"], target)):
                    return
            elif self.in_trial:
                self._restore(before)
                self.in_trial = False
                yield self._event("commit", False, "adjust did not make room", target)
        # 3: a bend
        if not self.stop_requested and self.iter < self.limits["max_iterations"]:
            if (yield from self._bend(t, s, before, total_before)):
                return
        self.attempts[(t, s)] += 1
        self._reset_stagnation()

    def _bend(self, t: str, s: int, before: State, total_before: float):
        target = f"{t}#{before.sid[t].index(s)}"
        if self.inserted[t] >= self.limits["max_added_vertices_per_trace"]:
            yield self._event("bend", False, "vertex limit", target)
            return False
        i = before.sid[t].index(s)
        a, b = before.pts[t][i], before.pts[t][i + 1]
        corners = [c[1] for c in _replacements(tuple(a), tuple(b))[1:]]
        for corner in [*corners, None]:                 # dog-legs, then a free split
            st = before.copy()
            mid = (a + b) / 2
            st.pts[t] = np.insert(st.pts[t], i + 1, mid, axis=0)
            s1, s2 = self._new_id(), self._new_id()
            st.sid[t][i:i + 1] = [s1, s2]
            self._apply(st.pts, st)                     # geometrically the same path
            if corner is not None:
                st = self.state.copy()
                st.snapped[t][s1] = direction(a, corner)
                st.snapped[t][s2] = direction(corner, b)
                moved = {k: v.copy() for k, v in st.pts.items()}
                moved[t][i + 1] = corner
                prop = self._project_all(moved, st)
                reason = "inconsistent directions" if prop is None else self.model.sweep(self.state.pts, prop, self.opt["max_substeps"])
                if reason:
                    self._restore(before)
                    yield self._event("bend", False, reason, target, candidate=prop)
                    continue
                self._apply(prop, st)
            self.in_trial = True
            yield self._event("bend", True, None, target)
            yield from self._settle()
            if turns_deg(self.state.pts[t])[i] <= BEND:      # settled straight: no bend, no credit
                self._restore(before)
                self.in_trial = False
                yield self._event("commit", False, "no bend formed", target)
                continue
            if (yield from self._commit(before, total_before, self.opt["bend_gain"], target)):
                self.inserted[t] += 1
                self.attempts[(t, s1)] = self.attempts[(t, s2)] = self.attempts[(t, s)]
                return True
            if self.stop_requested or self.iter >= self.limits["max_iterations"]:
                break
        return False

    def _merge(self):
        """Removes collinear vertices (outside trials), each as a checked move."""
        for t in self.model.ids:
            p = self.state.pts[t]
            if len(p) < 3:
                continue
            tr = turns_deg(p)
            for k in np.nonzero(tr < BEND)[0] + 1:
                sa, sb = self.state.sid[t][k - 1], self.state.sid[t][k]
                qa, qb = self.state.snapped[t].get(sa), self.state.snapped[t].get(sb)
                if (sa, sb) in self.no_merge or (qa is None) != (qb is None) or qa != qb:
                    continue
                a, c = p[k - 1], p[k + 1]
                v = c - a
                foot = a + np.clip(np.dot(p[k] - a, v) / np.dot(v, v), 0, 1) * v
                moved = {x: y.copy() for x, y in self.state.pts.items()}
                moved[t][k] = foot
                reason = self.model.sweep(self.state.pts, moved, self.opt["max_substeps"])
                target = f"{t}#{k}"
                if reason:
                    self.no_merge.add((sa, sb))
                    yield self._event("merge", False, reason, target, candidate=moved)
                    return
                st = self.state.copy()
                st.pts = moved
                st.pts[t] = np.delete(moved[t], k, axis=0)
                new = self._new_id()
                self.attempts[(t, new)] = max(self.attempts[(t, sa)], self.attempts[(t, sb)])
                st.sid[t][k - 1:k + 1] = [new]
                st.snapped[t].pop(sa, None)
                st.snapped[t].pop(sb, None)
                if qa is not None:
                    st.snapped[t][new] = qa
                self._apply(st.pts, st)
                yield self._event("merge", True, None, target)
                return

    def _candidate(self, stagnated: bool):
        best = None
        for t in self.model.ids:
            p = self.state.pts[t]
            for i, s in enumerate(self.state.sid[t]):
                if s in self.state.snapped[t] or self.attempts[(t, s)] >= self.opt["max_attempts_per_segment"]:
                    continue
                dev = deviation_deg(p[i], p[i + 1])
                if (dev <= self.limits["snap_tolerance_deg"] or stagnated) and (best is None or dev < best[0]):
                    best = (dev, t, s)
        return best[1:] if best else None

    # --- the run ------------------------------------------------------------------------

    def stop(self) -> None:
        """Ask the run to end after the current event; the state reverts to the last
        committed one."""
        self.stop_requested = True

    def events(self):
        """The run as a generator of ``Event``: ``start``, then steps and trials, ``stop``."""
        yield self._event("start", True)
        limit = self.limits["max_iterations"]
        while True:
            if self.stop_requested:
                self.termination = "user-stop"
                break
            if self.iter >= limit:
                self.termination = "max-iterations"
                break
            yield from self._step()
            yield from self._merge()
            stagnated = self.since >= self.opt["stagnation_window"]
            cand = self._candidate(stagnated)
            if cand:
                yield from self._trial(*cand)
            elif stagnated:
                self.termination = "all-snapped" if self.all_snapped() else "stagnated"
                break
        yield self._event("stop", True, self.termination)

    def info(self) -> dict:
        """Solver identity and the effective options, for the run record."""
        return {"solver": {"name": NAME, "version": VERSION, "merit": MERIT_VERSION,
                           "torch": self.torch.__version__},
                "options": {"objectives": self.objectives, "limits": self.limits, "solver": self.opt}}

    def result(self, which: Event | None = None) -> dict:
        """The candidate file (``route-polish-result``): the best feasible state, or the
        state after event ``which``."""
        if which is None:
            total, st, seq, terms = self.best
            traces = st.pts
            snapped = {t: sorted(st.sid[t].index(s) for s in st.snapped[t]) for t in self.model.ids}
        else:
            total, terms, seq, traces, snapped = which.total, which.terms, which.seq, which.traces, which.snapped
        return result_doc(self.case, self.group, seq, traces, snapped, total, terms, self.initial,
                          self.model.min_slack(traces), self.termination, self.info()["solver"])


def result_doc(case, group, seq, traces, snapped, total, terms, initial, min_slack, termination, solver) -> dict:
    """The ``route-polish-result`` document (``specs/ROUTE_POLISH_OUTPUT.md`` §3)."""
    return {"format": "route-polish-result", "version": 1, "input_sha256": case.sha256, "group": group, "seq": seq,
            "traces": [{"id": t, "points": [[float(x), float(y)] for x, y in traces[t]], "snapped": list(snapped[t])}
                       for t in traces],
            "score": {"total": total, "terms": terms},
            "initial_score": {"total": initial[0], "terms": initial[1]},
            "constraints": {"feasible": True, "min_slack": min_slack},
            "termination": termination, "solver": {k: solver[k] for k in ("name", "version", "merit")}}


__all__ = ["DEFAULTS", "Event", "Solver", "State", "direction", "project", "result_doc"]
