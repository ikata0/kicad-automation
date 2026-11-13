"""The polishing solver: continuous moves on the merit's gradient, discrete snaps and bends,
every change of state a checked straight move (``Model.sweep``).

One **iteration** is one optimiser step: ``torch.optim.Adam`` proposes new interior
vertices from the merit plus the barrier; the proposal is projected so that snapped
segments keep their exact direction, then shortened (1, 1/2, 1/4, ...) until the move is
clear all the way and the new state passes the hard check; traces that the check names are
held where they are and the rest retried. Discrete edits are **trials**:
``snap`` (fix a segment at its nearest multiple of 45 degrees), ``adjust`` (weight that
segment's angle heavily for a while so that its neighbours make room, then snap) and
``bend`` (replace the segment by an already-bent shape whose legs lie on 45-degree
multiples: a corner either way, then a Z either way, each moved into place as a checked move;
then a free collinear midpoint), each settled by some iterations and then committed or
reverted. The legs are not snapped, and a shape is only kept for its merit gain. A collinear
vertex alone barely moves: turning one half towards 45 degrees turns the other away. A collinear vertex is ``merge``d away outside trials, and the tip of a doubled-back
detour (a turn of at least ``reversal_turn_deg``) is moved onto its nearer neighbour and
dropped (``simplify``); a best state never keeps a tip that a later one removed. Snapped segments stay snapped
except when explicitly split for rescue. ``Solver.events`` yields every event; the best
feasible state is kept apart.

**Recovery.** A case that starts short of clearance (but with every centreline clear of
every obstacle) is run all the same: no (trace, obstacle) pair may get shorter than it is,
and once reduced a shortfall stays reduced (``Model.allow``/``tighten``). The barrier pushes
out; a smaller shortfall outranks a better merit. When the steps stall while still short, the
worst segment gets a ``rescue`` bend: split where it comes closest to the obstacle and
settled. A segment that steps keep running into an obstacle gets the same bend once the
snaps are tried.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from . import OPTIONS, require
from .case import Case
from .constraints import Model
from .merit import BEND, TERMS, VERSION as MERIT_VERSION, Merit, deviation_deg, turns_deg

NAME, VERSION = "kat-polish", "0.1"
DEFAULTS = {k: v for k, (v, _) in OPTIONS.items()}
BACKTRACK = (1.0, 0.5, 0.25, 0.125, 0.0625)
EXACT = 1e-9                # degrees: a segment this close to 45-degree multiples counts as exact
GAIN = 1e-6                 # mm: a shortfall reduced by this much counts as progress
STALL = 1e-4                # mm: recovery has stalled when a window of steps gains less
CLEARANCE = re.compile(r"clearance (\S+?)#(\d+)-(\S+?)(?:#(\d+))? short")
MIN_LEG = 0.01             # mm: a bent shape needs legs at least this long
PIN_ROUNDS = 8              # a failing step pins vertices this many times before giving up


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
    shortfall: float = 0.0
    added_vertices: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        """The event as a JSON object (points as lists)."""
        d = {"seq": self.seq, "iter": self.iter, "type": self.type, "accepted": self.accepted, "reason": self.reason,
             "target": self.target, "traces": _lists(self.traces), "snapped": self.snapped, "terms": self.terms,
             "total": self.total, "min_slack": self.min_slack, "shortfall": self.shortfall,
             "added_vertices": self.added_vertices}
        if self.candidate is not None:
            d["candidate"] = _lists(self.candidate)
        return d


def _lists(pts: dict) -> dict:
    return {k: [[float(x), float(y)] for x, y in v] for k, v in pts.items()}


@dataclass
class State:
    """Points per trace, a stable id per segment, and the snapped segments {id: direction
    0-3, in steps of 45 degrees, undirected}. ``added`` holds inserted vertex indices."""
    pts: dict
    sid: dict
    snapped: dict = field(default_factory=dict)
    added: dict = field(default_factory=dict)

    def copy(self) -> "State":
        """A deep copy."""
        return State({k: v.copy() for k, v in self.pts.items()}, {k: list(v) for k, v in self.sid.items()},
                     {k: dict(v) for k, v in self.snapped.items()}, {k: set(v) for k, v in self.added.items()})


def direction(a, b) -> int:
    """The nearest multiple of 45 degrees of segment a-b, as 0-3 (undirected)."""
    return round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) / 45.0) % 4


def project(pts: np.ndarray, snapped_idx: dict, fixed=()) -> np.ndarray | None:
    """``pts`` moved as little as possible (least squares, interior vertices only, not the
    vertex indices in ``fixed``) so that each segment index in ``snapped_idx`` {index:
    direction 0-3} lies exactly on its direction; None when the directions cannot all hold
    between the fixed vertices."""
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
    for v in fixed:
        free[2 * v:2 * v + 2] = False
    x = pts.ravel().copy()
    Af, b = A[:, free], -A[:, ~free] @ x[~free]
    r = Af @ x[free] - b
    if Af.size:
        lam, *_ = np.linalg.lstsq(Af @ Af.T, r, rcond=None)
        x[free] = x[free] - Af.T @ lam
    if np.abs(A @ x).max() > 1e-10:
        return None
    return x.reshape(n, 2)


def solver_options(weights: dict | None, options: dict | None) -> dict:
    """``DEFAULTS`` with ``options`` applied, each value of its default's type (a count must
    be a whole number); ``KatError`` for an unknown weight or option, naming the valid ones."""
    from ...env import KatError
    bad = [k for k in (weights or {}) if k not in TERMS] + [k for k in (options or {}) if k not in DEFAULTS]
    if bad:
        raise KatError(f"unknown option(s) {', '.join(bad)}: weights {', '.join(TERMS)}; options "
                       f"{', '.join(DEFAULTS)} (meanings and defaults: kat pcb polish run --help)")
    out = dict(DEFAULTS)
    for k, v in (options or {}).items():
        if isinstance(DEFAULTS[k], int):
            if not float(v).is_integer():
                raise KatError(f"option {k}: a whole number, not {v}")
            v = int(v)
        out[k] = type(DEFAULTS[k])(v)
    return out


class Solver:
    """A polishing run on ``case``'s group ``group``: ``weights``, ``options`` (solver knobs,
    ``DEFAULTS``) and ``limits`` (the input's names) override the case's values."""

    def __init__(self, case: Case, group: str | None = None, weights: dict | None = None,
                 options: dict | None = None, limits: dict | None = None):
        self.torch = require("torch")
        self.case = case
        self.limits = {**case.limits, **(limits or {})}
        self.objectives = {**case.objectives, "weights": {**case.objectives["weights"], **(weights or {})}}
        self.opt = solver_options(weights, options)
        self.model = Model(case, group)
        self.group = self.model.group
        start = self.model.initial()
        self.initial_violations = self.model.violations(start)
        if self.initial_violations:
            blocking = self.model.blocking(start)
            if blocking:
                from ...env import KatError
                raise KatError(f"input infeasible beyond recovery, nothing optimised: {'; '.join(blocking[:5])}")
            self.model.allow(start)
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
        self.rescues = defaultdict(int)               # rescue bends tried per (trace, segment id)
        self.blocked = defaultdict(int)               # steps held per (trace, segment id, obstacle)
        self.no_merge: set = set()
        self.no_simplify: set = set()
        self.in_trial = False
        self.stop_requested = False
        self.termination: str | None = None
        self.lr = self.opt["learning_rate"]
        self.total, self.terms = self.merit.evaluate(self.state.pts)
        self.short = self.model.shortfall(self.state.pts)
        self.phase_short, self.since_short = self.short, 0
        self.initial = (self.total, dict(self.terms))
        self.initial_short = self.short
        self.best = (self.total, self.state.copy(), 0, dict(self.terms), self.short)
        self._reset_stagnation()
        self._rebuild()

    # --- plumbing -----------------------------------------------------------------------

    def _new_id(self) -> int:
        self._next += 1
        return self._next

    def _reset_stagnation(self) -> None:
        self.phase_best, self.since = self.total, 0     # the shortfall's own window runs on

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
        if self.initial_short:
            self.short = self.model.shortfall(self.state.pts) if self.model.recovering else 0.0
        if accepted and (not self.in_trial or type_ == "commit"):
            self.model.tighten(self.state.pts)
            if self._better(self.short, self._rank(self.total, self.state), self.best[4],
                            self._rank(self.best[0], self.best[1]), self._tips(self.state), self._tips(self.best[1])):
                self.best = (self.total, self.state.copy(), self.seq, dict(self.terms), self.short)
        ev = Event(self.seq, self.iter, type_, accepted, reason, target,
                   {k: v.copy() for k, v in self.state.pts.items()},
                   {t: sorted(v) for t, v in self._snapped_idx().items()},
                   dict(self.terms), self.total, self.model.min_slack(self.state.pts), candidate, self.short,
                   {t: sorted(int(v) for v in self.state.added.get(t, ())) for t in self.model.ids})
        self.seq += 1
        return ev

    def _rank(self, total: float, st: State) -> float:
        """What "best" means: the merit, each snapped segment credited the snap allowance
        (``snap_slack``), consistent with accepting a snap that costs up to that much."""
        return total - self.opt["snap_slack"] * sum(len(v) for v in st.snapped.values())

    def _tips(self, st: State) -> int:
        """Doubled-back tips (turns of at least ``reversal_turn_deg``) in ``st``."""
        return sum(int((turns_deg(p) >= self.opt["reversal_turn_deg"]).sum()) for p in st.pts.values() if len(p) > 2)

    @staticmethod
    def _better(short: float, rank: float, short0: float, rank0: float, tips: int = 0, tips0: int = 0) -> bool:
        """Whether (shortfall, tips, rank) beats (short0, tips0, rank0): less short first,
        then fewer doubled-back tips, then the rank."""
        if short < short0 - GAIN:
            return True
        if short > short0 + GAIN:
            return False
        return tips < tips0 or (tips == tips0 and rank < rank0 - 1e-12)

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
            snapped = self._snapped_idx()
            for alpha in BACKTRACK:
                cand = {t: cur[t] + alpha * (prop[t] - cur[t]) for t in cur}
                value, known = self._loss(cand, boost)
                if value > level + 1e-12:
                    reason = "no descent"
                    continue
                reason = self.model.sweep(cur, cand, self.opt["max_substeps"])
                # what fails a hard check stays put: the vertices it names, else its trace
                held: set = set()
                pins: dict = defaultdict(set)
                if reason:
                    self._note_blocked(reason)
                for _ in range(PIN_ROUNDS):
                    named = self._named(reason) - held if reason else set()
                    if not named:
                        break
                    for t in named:
                        more = self._pins(t, reason, cand[t]) - pins[t]
                        q = None
                        if more:
                            pins[t] |= more
                            q = cand[t].copy()
                            q[sorted(pins[t])] = cur[t][sorted(pins[t])]
                            q = project(q, snapped[t], pins[t])
                        if q is None:
                            held.add(t)
                            q = cur[t]
                        cand[t] = q
                    if len(held) == len(cur):
                        break
                    value, known = self._loss(cand, boost)
                    if value > level + 1e-12:
                        reason = "no descent"
                        break
                    reason = self.model.sweep(cur, cand, self.opt["max_substeps"])
                if reason is None:
                    self.state.pts = cand
                    with self.torch.no_grad():
                        self.X.copy_(self.torch.as_tensor(np.concatenate([cand[t][1:-1].ravel() for t in cand])))
                    self.lr = min(self.opt["learning_rate"], self.lr * 1.1)
                    self.adam.param_groups[0]["lr"] = self.lr
                    ev = self._event("step", True, known=known)
                    if ev.shortfall < self.phase_short - STALL:
                        self.phase_short, self.since, self.since_short = ev.shortfall, 0, 0
                    else:
                        self.since_short += 1
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
        self.since_short += 1
        yield self._event("step", False, reason, candidate=prop)
        return False

    def _named(self, reason: str) -> set:
        """The movable traces a hard-check failure names (``t13: off the board``,
        ``clearance t08#1-t10#2 ...``)."""
        return {t for t in self.model.ids if re.search(rf"(?<![\w]){re.escape(t)}(?=[:#])", reason)}

    def _pins(self, t: str, reason: str, p: np.ndarray) -> set:
        """Interior vertices of trace ``t`` (at ``p``) to keep where they are after the
        failure ``reason``: a short segment's ends; empty when the failure names none."""
        out = set()
        m = CLEARANCE.match(reason)
        if m:
            if m.group(1) == t:
                k = int(m.group(2))
                out |= {k, k + 1}
            if m.group(3) == t and m.group(4) is not None:
                k = int(m.group(4))
                out |= {k, k + 1}
        return {v for v in out if 0 < v < len(p) - 1}

    def _note_blocked(self, reason: str) -> None:
        """Counts a step held back by a clearance (``clearance t08#1-c0546 short ...``) on
        that segment, for a bend there later."""
        m = CLEARANCE.match(reason)
        if m and m.group(1) in self.state.sid:
            t, k, obstacle = m.group(1), int(m.group(2)), m.group(3)
            if k < len(self.state.sid[t]):
                self.blocked[(t, self.state.sid[t][k], obstacle)] += 1

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
        self.state = State(pts, st.sid, st.snapped, st.added)
        self._rebuild()

    def _commit(self, before: State, total_before: float, need_gain: float, target: str, short_before: float = 0.0):
        """Ends a trial: kept when the shortfall dropped below ``short_before`` (mm), or did
        not grow and the merit is at most ``total_before - need_gain``."""
        if self.stop_requested:
            self._restore(before)
            self.in_trial = False
            yield self._event("commit", False, "user stop", target)
            return False
        total, _ = self.merit.evaluate(self.state.pts)
        short = self.model.shortfall(self.state.pts) if self.model.recovering else 0.0
        ok = short < short_before - GAIN or (short <= short_before + GAIN and total <= total_before - need_gain + 1e-12)
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
        before, total_before, short_before = self.state.copy(), self.total, self.short
        target = self._ref(t, s)
        # 1: snap where it is
        if (yield from self._snap_move(t, s, "snap")):
            yield from self._settle()
            if (yield from self._commit(before, total_before, -self.opt["snap_slack"], target, short_before)):
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
                if (yield from self._commit(before, total_before, -self.opt["snap_slack"], target, short_before)):
                    return
            elif self.in_trial:
                self._restore(before)
                self.in_trial = False
                yield self._event("commit", False, "adjust did not make room", target)
        # 3: a bend
        if not self.stop_requested and self.iter < self.limits["max_iterations"]:
            if (yield from self._bend(t, s, before, total_before, short_before)):
                return
        self.attempts[(t, s)] += 1
        self._reset_stagnation()

    @staticmethod
    def _shapes(a: np.ndarray, b: np.ndarray) -> list:
        """Already-bent replacements of segment a-b (mm), every leg on a multiple of 45
        degrees: the corner (one vertex) both ways, then the Z (two vertices: one direction
        in halves around the other) both ways, as [(name, (n, 2) vertices)]; [] when a leg
        would be shorter than ``MIN_LEG`` (the segment is on 45 degrees already)."""
        d = b - a
        k = math.floor(math.degrees(math.atan2(d[1], d[0])) / 45.0)
        u = [np.array([math.cos(math.radians(45.0 * j)), math.sin(math.radians(45.0 * j))]) for j in (k, k + 1)]
        deg = [45 * j % 180 for j in (k, k + 1)]
        lam = np.linalg.solve(np.column_stack(u), d)
        if lam.min() < MIN_LEG:
            return []
        corners, zs = [], []
        for p, q in ((0, 1), (1, 0)):
            first = a + lam[p] * u[p]
            corners.append((f"corner {deg[p]}/{deg[q]}", first[None]))
            half = a + lam[p] / 2 * u[p]
            zs.append((f"Z {deg[p]}/{deg[q]}/{deg[p]}", np.array([half, half + lam[q] * u[q]])))
        return corners + zs

    def _bend(self, t: str, s: int, before: State, total_before: float, short_before: float = 0.0,
              at: np.ndarray | None = None, type_: str = "bend"):
        """Bend trials on segment ``s`` of trace ``t`` until one is kept: the shapes of
        ``_shapes``, then a free collinear midpoint; for rescue only one free collinear
        vertex ``at`` (mm). A shape's vertices are inserted on the segment and moved to
        their place as a checked move (a blocked shape is a rejected event); its legs are
        free, not snapped. Each trial settles before it is judged. True when one was kept."""
        i = before.sid[t].index(s)
        target = f"{t}#{i}"
        a, b = before.pts[t][i], before.pts[t][i + 1]
        if at is not None:
            tries = [("collinear", np.asarray(at, float)[None])]
        else:
            tries = self._shapes(a, b) + [("midpoint", ((a + b) / 2)[None])]
        room = self.limits["max_added_vertices_per_trace"] - self.inserted[t]
        if not any(len(v) <= room for _, v in tries):
            yield self._event(type_, False, "vertex limit", target)
            return False
        v = b - a
        for name, new in tries:
            if self.stop_requested or self.iter >= self.limits["max_iterations"]:
                break
            n = len(new)
            if n > room:
                continue
            feet = a + np.clip((new - a) @ v / (v @ v), 0, 1)[:, None] * v
            st = before.copy()
            st.pts[t] = np.insert(st.pts[t], i + 1, feet, axis=0)
            st.added[t] = {x + n * (x > i) for x in st.added.get(t, ())} | set(range(i + 1, i + 1 + n))
            ids = [self._new_id() for _ in range(n + 1)]
            st.sid[t][i:i + 1] = ids
            st.snapped[t].pop(s, None)                  # a rescue may split a snapped segment
            if not np.array_equal(new, feet):
                moved = {x: y.copy() for x, y in st.pts.items()}
                moved[t][i + 1:i + 1 + n] = new
                reason = self.model.sweep(st.pts, moved, self.opt["max_substeps"])
                if reason:
                    yield self._event(type_, False, f"{name}: {reason}", target, candidate=moved)
                    continue
                st.pts = moved
            self._apply(st.pts, st)
            self.in_trial = True
            yield self._event(type_, True, name, target)
            yield from self._settle()
            if (yield from self._commit(before, total_before, self.opt["bend_gain"], target, short_before)):
                self.inserted[t] += n
                for x in ids:
                    self.attempts[(t, x)] = self.attempts[(t, s)]
                for key in [k for k in self.blocked if k[:2] == (t, s)]:
                    del self.blocked[key]
                return True
        return False

    def _merge(self):
        """Removes collinear vertices (outside trials), each as a checked move."""
        for t in self.model.ids:
            p = self.state.pts[t]
            if len(p) < 3:
                continue
            tr = turns_deg(p)
            for k in map(int, np.nonzero(tr < BEND)[0] + 1):
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
                st.added[t] = {v - (v > k) for v in st.added.get(t, ()) if v != k}
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

    def _simplify(self):
        """Removes doubled-back detours (outside trials): an interior vertex whose turn is at
        least ``reversal_turn_deg`` is moved onto its nearer neighbour, as a checked move,
        and dropped; the longer leg survives (with its snap when it can stay exact)."""
        for t in self.model.ids:
            p = self.state.pts[t]
            if len(p) < 3:
                continue
            tr = turns_deg(p)
            for k in map(int, np.nonzero(tr >= self.opt["reversal_turn_deg"])[0] + 1):
                sa, sb = self.state.sid[t][k - 1], self.state.sid[t][k]
                if (sa, sb) in self.no_simplify:
                    continue
                # j: the nearer neighbour; the leg towards it collapses
                j = k - 1 if np.linalg.norm(p[k] - p[k - 1]) < np.linalg.norm(p[k + 1] - p[k]) else k + 1
                keep, gone = (sb, sa) if j == k - 1 else (sa, sb)
                st = self.state.copy()
                reduced = np.delete(p, k, axis=0)
                sid = list(st.sid[t])
                sid.remove(gone)
                snaps = {s: q for s, q in st.snapped[t].items() if s != gone}
                q = project(reduced, {sid.index(s): d for s, d in snaps.items()})
                if q is None:
                    snaps.pop(keep, None)
                    q = project(reduced, {sid.index(s): d for s, d in snaps.items()})
                target = f"{t}#{k}"
                if q is None:
                    self.no_simplify.add((sa, sb))
                    yield self._event("simplify", False, "inconsistent directions", target)
                    return
                moved = {x: y.copy() for x, y in self.state.pts.items()}
                moved[t] = np.insert(q, k, q[j if j < k else k], axis=0)   # the tip on its neighbour
                final = {**moved, t: q}
                lo, hi = min(j, k) - 1, max(j, k)          # the legs that meet where one collapses
                exempt = [(t, lo, hi)] if lo >= 0 and hi < len(p) - 1 else []
                reason = self.model.sweep(self.state.pts, moved, self.opt["max_substeps"], exempt, final)
                if reason:
                    self.no_simplify.add((sa, sb))
                    yield self._event("simplify", False, reason, target, candidate=moved)
                    return
                st.pts = moved
                st.pts[t] = q
                st.added[t] = {v - (v > k) for v in st.added.get(t, ()) if v != k}
                st.sid[t] = sid
                st.snapped[t] = snaps
                self._apply(st.pts, st)
                yield self._event("simplify", True, None, target)
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

    def _rescue_target(self, stagnated: bool):
        """(trace, segment id, obstacle) for a rescue bend: while short of clearance and
        the shortfall stalls (``stagnation_window`` steps), the worst short pair not tried
        yet; else, once ``stagnated``, a segment that steps kept running into an obstacle.
        None when there is none with a try and a vertex left."""
        ok = lambda t, s: self.rescues[(t, s)] < 1 and \
            self.inserted[t] < self.limits["max_added_vertices_per_trace"]   # noqa: E731
        if self.short > GAIN and self.since_short >= self.opt["stagnation_window"]:
            for t, k, obstacle in self.model.short_pairs(self.state.pts):
                if ok(t, self.state.sid[t][k]):
                    return t, self.state.sid[t][k], obstacle
        if not stagnated:
            return None
        for (t, s, obstacle), n in sorted(self.blocked.items(), key=lambda kv: -kv[1]):
            if n >= self.opt["blocked_bend_after"] and s in self.state.sid[t] and ok(t, s):
                return t, s, obstacle
        return None

    def _rescue(self, t: str, s: int, obstacle: str):
        """A ``rescue`` bend on segment ``s`` of trace ``t`` where it comes closest to
        ``obstacle``."""
        self.rescues[(t, s)] += 1
        i = self.state.sid[t].index(s)
        at = self.model.closest(self.state.pts, t, i, obstacle)
        yield from self._bend(t, s, self.state.copy(), self.total, self.short, at=at, type_="rescue")
        self._reset_stagnation()

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
            yield from self._simplify()
            stagnated = self.since >= self.opt["stagnation_window"]
            if self.short > GAIN and (r := self._rescue_target(False)):
                yield from self._rescue(*r)
                continue
            cand = self._candidate(stagnated)
            if cand:
                yield from self._trial(*cand)
            elif (r := self._rescue_target(stagnated)):
                yield from self._rescue(*r)
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
            total, st, seq, terms, _ = self.best
            traces = st.pts
            snapped = {t: sorted(st.sid[t].index(s) for s in st.snapped[t]) for t in self.model.ids}
        else:
            total, terms, seq, traces, snapped = which.total, which.terms, which.seq, which.traces, which.snapped
        return result_doc(self.case, self.group, seq, traces, snapped, total, terms, self.initial,
                          self.model.min_slack(traces), self.termination, self.info()["solver"],
                          Model(self.case, movable=self.model.ids).violations(traces), self.initial_violations)


def result_doc(case, group, seq, traces, snapped, total, terms, initial, min_slack, termination, solver,
               violations=(), initial_violations=()) -> dict:
    """The ``route-polish-result`` document (``specs/ROUTE_POLISH_OUTPUT.md`` §3);
    ``violations`` are the state's hard-check failures (empty: feasible), and a run that
    started short of clearance lists the start's as ``initial_violations``."""
    return {"format": "route-polish-result", "version": 1, "input_sha256": case.sha256, "group": group, "seq": seq,
            "traces": [{"id": t, "points": [[float(x), float(y)] for x, y in traces[t]], "snapped": list(snapped[t])}
                       for t in traces],
            "score": {"total": total, "terms": terms},
            "initial_score": {"total": initial[0], "terms": initial[1]},
            "constraints": {"feasible": not violations, "min_slack": min_slack,
                            **({"violations": list(violations)} if violations else {}),
                            **({"initial_violations": list(initial_violations)} if initial_violations else {})},
            "termination": termination, "solver": {k: solver[k] for k in ("name", "version", "merit")}}


__all__ = ["DEFAULTS", "Event", "Solver", "State", "direction", "project", "result_doc", "solver_options"]
