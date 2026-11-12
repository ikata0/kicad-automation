"""Hard constraints of a polishing problem, and moves that keep the topology.

Everything is a capsule: a segment (a, b) with a radius. Movable traces are their segments
with half the width; fixed copper polylines likewise; circles and drill holes are points
with their radius; polygon copper, keep-outs, the board edge and the movement region are
their edges with radius 0 (plus a containment test). The **slack** of a pair is the
centreline distance minus the required distance (both radii plus the applicable
clearance); every slack must stay >= 0.

``Model.sweep`` moves the traces from one state to another along the straight line
between their vertex coordinates. Along such a move a point of a segment moves at most as
far as the farther of its two vertices, so a sub-step shorter than every pair's slack
cannot let anything touch, let alone pass through, anything else (conservative
advancement). With fixed anchors that preserves the side of every obstacle, the order of
neighbouring routes and the absence of crossings. Pure float64 numpy; no autograd.
"""

from __future__ import annotations

import math

import numpy as np

from ...env import KatError
from .case import Case

TOL = 1e-9                  # mm: numerical tolerance, never a clearance allowance
SAMPLE = 0.05               # mm: sampling step of the displacement (Hausdorff) bound
MAX_SUBSTEPS = 64


def point_seg(p, s, e) -> np.ndarray:
    """Distance of points ``p`` to segments s-e (arrays of points, broadcast)."""
    v = e - s
    L2 = (v * v).sum(-1)
    u = np.clip(((p - s) * v).sum(-1) / np.where(L2 > 0, L2, 1.0), 0.0, 1.0)
    q = s + u[..., None] * v - p
    return np.sqrt((q * q).sum(-1))


def seg_dist(a, b, c, d) -> np.ndarray:
    """Distance between segments a-b and c-d (arrays of points, broadcast), 0 where they
    cross."""
    def cross(o, p, q):
        return (p[..., 0] - o[..., 0]) * (q[..., 1] - o[..., 1]) - (p[..., 1] - o[..., 1]) * (q[..., 0] - o[..., 0])
    d1, d2, d3, d4 = cross(c, d, a), cross(c, d, b), cross(a, b, c), cross(a, b, d)
    hit = ((d1 > 0) != (d2 > 0)) & ((d3 > 0) != (d4 > 0))
    dist = np.minimum(np.minimum(point_seg(a, c, d), point_seg(b, c, d)), np.minimum(point_seg(c, a, b), point_seg(d, a, b)))
    return np.where(hit, 0.0, dist)


def inside(points: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """Which of ``points`` (N, 2) lie inside the polygon ``ring`` (K, 2) (even-odd rule)."""
    x, y = points[:, 0:1], points[:, 1:2]
    x1, y1 = ring[None, :, 0], ring[None, :, 1]
    nxt = np.roll(ring, -1, axis=0)
    x2, y2 = nxt[None, :, 0], nxt[None, :, 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        xs = (x2 - x1) * (y - y1) / (y2 - y1) + x1
    hit = ((y1 > y) != (y2 > y)) & (x < xs)
    return (hit.sum(1) % 2) == 1


def samples(pts: np.ndarray, step: float = SAMPLE) -> np.ndarray:
    """Points along a polyline no farther apart than ``step``, vertices included."""
    n = np.maximum(1, np.ceil(np.linalg.norm(np.diff(pts, axis=0), axis=1) / step).astype(int))
    seg = np.repeat(np.arange(len(n)), n)
    u = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 1) / np.repeat(n, n)
    return np.concatenate([pts[:1], pts[seg] + u[:, None] * (pts[seg + 1] - pts[seg])])


def to_polyline(points: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Distance of each of ``points`` (N, 2) to the polyline ``pts``."""
    return point_seg(points[:, None], pts[None, :-1], pts[None, 1:]).min(1)


def hausdorff(p: np.ndarray, q: np.ndarray) -> float:
    """An upper bound of the symmetric Hausdorff distance of two polylines (mm): the largest
    distance of samples at most ``SAMPLE`` apart, plus ``SAMPLE`` / 2 (distance to a set
    changes at most as fast as the point moves), so the limit is never exceeded unseen."""
    return float(max(to_polyline(samples(p), q).max(), to_polyline(samples(q), p).max())) + SAMPLE / 2


def ring_edges(ring) -> tuple[np.ndarray, np.ndarray]:
    r = np.asarray(ring, float)
    return r, np.roll(r, -1, axis=0)


class Model:
    """The constraints of one run: ``movable`` traces (default the group ``group``) move,
    all other traces are fixed copper. ``orig`` holds the input geometry."""

    def __init__(self, case: Case, group: str | None = None, movable: list[str] | None = None):
        self.case = case
        if movable is None:
            if group is None:
                if len(case.groups) != 1:
                    raise KatError(f"several groups ({', '.join(case.groups)}): choose one")
                group = next(iter(case.groups))
            if group not in case.groups:
                raise KatError(f"no group {group!r} (have {', '.join(case.groups)})")
            movable = case.groups[group]
        self.group, self.ids = group, list(movable)
        self.half = np.array([case.traces[t].width / 2 for t in self.ids])
        self.orig = {t: np.asarray(case.traces[t].points, float) for t in self.ids}
        prims: list[tuple] = []                       # (a, b, r, kind, owner)
        for c in case.copper.values():
            if c.kind == "polyline":
                prims += [(a, b, c.width / 2, "copper", c.id) for a, b in zip(c.points, c.points[1:])]
            elif c.kind == "circle":
                prims.append((c.center, c.center, c.radius, "copper", c.id))
            else:
                prims += [(a, b, 0.0, "copper", c.id) for a, b in zip(*ring_edges(c.points))]
        for t in case.traces.values():
            if t.id not in self.ids:
                prims += [(a, b, t.width / 2, "copper", t.id) for a, b in zip(t.points, t.points[1:])]
        prims += [(h.center, h.center, h.radius, "hole", h.id) for h in case.holes.values()]
        for k in case.keepouts.values():
            prims += [(a, b, 0.0, "keepout", k.id) for a, b in zip(*ring_edges(k.points))]
        prims += [(a, b, 0.0, "edge", "edge") for a, b in zip(*ring_edges(case.outline))]
        for i, cut in enumerate(case.cutouts):
            prims += [(a, b, 0.0, "edge", f"cutout{i}") for a, b in zip(*ring_edges(cut))]
        prims += [(a, b, 0.0, "region", "region") for a, b in zip(*ring_edges(case.region))]
        # what cannot reach the movement region cannot constrain anything
        reg = np.asarray(case.region, float)
        lo, hi = reg.min(0), reg.max(0)
        reach = float(self.half.max()) + max([case.copper_clearance, case.edge_clearance, case.hole_clearance,
                                              *case.pairs.values(), *(k.clearance for k in case.keepouts.values())])
        keep = []
        for p in prims:
            a, b = np.asarray(p[0], float), np.asarray(p[1], float)
            gap = np.maximum(np.maximum(lo - np.maximum(a, b), np.minimum(a, b) - hi), 0.0)
            if float(np.hypot(*gap)) <= reach + p[2] + 1e-6:
                keep.append(p)
        self.fa = np.array([p[0] for p in keep], float).reshape(-1, 2)
        self.fb = np.array([p[1] for p in keep], float).reshape(-1, 2)
        self.fr = np.array([p[2] for p in keep], float)
        self.fkind = [p[3] for p in keep]
        self.fowner = [p[4] for p in keep]
        base = {"copper": case.copper_clearance, "hole": case.hole_clearance, "edge": case.edge_clearance, "region": 0.0}
        self.req = np.empty((len(self.ids), len(keep)))
        for i, t in enumerate(self.ids):
            anchors = set(case.traces[t].anchors)
            for j, (kind, owner) in enumerate(zip(self.fkind, self.fowner)):
                if kind in ("copper", "hole") and owner in anchors:
                    self.req[i, j] = -np.inf                     # own anchor: no clearance
                    continue
                if kind == "keepout":
                    cl = case.clearance(t, owner, case.keepouts[owner].clearance)
                elif kind in ("edge", "region"):
                    cl = base[kind]                              # physical edges: no pair entries
                else:
                    cl = case.clearance(t, owner, base[kind])
                self.req[i, j] = self.half[i] + self.fr[j] + cl
        n = len(self.ids)
        self.mreq = np.array([[self.half[i] + self.half[j] + case.clearance(self.ids[i], self.ids[j], case.copper_clearance)
                               for j in range(n)] for i in range(n)]).reshape(n, n)
        self.polys = [(c.id, np.asarray(c.points, float)) for c in case.copper.values() if c.kind == "polygon"]
        self.keep_polys = [(k.id, np.asarray(k.points, float)) for k in case.keepouts.values()]

    # --- state ------------------------------------------------------------------------

    def initial(self) -> dict[str, np.ndarray]:
        """The input geometry of the movable traces, as a state {id: (n, 2) array}."""
        return {t: p.copy() for t, p in self.orig.items()}

    def _segments(self, state):
        a, b, tr, ix = [], [], [], []
        for i, t in enumerate(self.ids):
            p = state[t]
            a.append(p[:-1])
            b.append(p[1:])
            tr += [i] * (len(p) - 1)
            ix += list(range(len(p) - 1))
        return np.concatenate(a), np.concatenate(b), np.array(tr), np.array(ix)

    def slacks(self, state) -> tuple[np.ndarray, np.ndarray, tuple]:
        """Slack (mm) of every movable segment against every fixed primitive (M, F) and
        against every other movable segment (M, M; +inf where not constrained), and the
        segments' (trace index, segment index)."""
        a, b, tr, ix = self._segments(state)
        fs = seg_dist(a[:, None], b[:, None], self.fa[None], self.fb[None]) - self.req[tr]
        ms = seg_dist(a[:, None], b[:, None], a[None], b[None])
        same = tr[:, None] == tr[None, :]
        need = np.where(same, 0.0, self.mreq[tr][:, tr])             # own segments: no crossing
        ms = ms - need
        adjacent = same & (np.abs(ix[:, None] - ix[None, :]) <= 1)
        ms[adjacent] = np.inf
        return fs, ms, (tr, ix)

    def _ref(self, tr, ix, k) -> str:
        return f"{self.ids[tr[k]]}#{ix[k]}"

    def min_slack(self, state) -> dict:
        """The smallest slack in ``state``: {value (mm), a, b} with object references
        (``t01#3`` is segment 3 of trace t01)."""
        fs, ms, (tr, ix) = self.slacks(state)
        out = {"value": math.inf, "a": None, "b": None}
        if fs.size:
            k = np.unravel_index(np.argmin(fs), fs.shape)
            out = {"value": float(fs[k]), "a": self._ref(tr, ix, k[0]), "b": self.fowner[k[1]]}
        if ms.size and float(ms.min()) < out["value"]:
            k = np.unravel_index(np.argmin(ms), ms.shape)
            out = {"value": float(ms[k]), "a": self._ref(tr, ix, k[0]), "b": self._ref(tr, ix, k[1])}
        return out

    def violations(self, state) -> list[str]:
        """Every hard-constraint failure of ``state`` as text; empty when feasible.
        Independent of how the state was reached (the sweep checks the way)."""
        out = []
        for t in self.ids:
            p, o = state[t], self.orig[t]
            if not (np.array_equal(p[0], o[0]) and np.array_equal(p[-1], o[-1])):
                out.append(f"{t}: anchor moved")
            if np.any(np.linalg.norm(np.diff(p, axis=0), axis=1) <= TOL):
                out.append(f"{t}: zero-length segment")
        fs, ms, (tr, ix) = self.slacks(state)
        for k, j in zip(*np.nonzero(fs < -TOL)):
            out.append(f"clearance {self._ref(tr, ix, k)}-{self.fowner[j]} short by {-fs[k, j]:.4f} mm")
        for k, j in zip(*np.nonzero(np.triu(ms < -TOL))):
            out.append(f"clearance {self._ref(tr, ix, k)}-{self._ref(tr, ix, j)} short by {-ms[k, j]:.4f} mm")
        for i, t in enumerate(self.ids):
            p = state[t]
            if not inside(p, np.asarray(self.case.outline, float)).all() or any(
                    inside(p, np.asarray(c, float)).any() for c in self.case.cutouts):
                out.append(f"{t}: off the board")
            if not inside(p, np.asarray(self.case.region, float)).all():
                out.append(f"{t}: outside the movement region")
            anchors = set(self.case.traces[t].anchors)
            for pid, ring in self.polys + self.keep_polys:
                if pid not in anchors and inside(p, ring).any():
                    out.append(f"{t}: inside {pid}")
            h = hausdorff(p, self.orig[t])
            if h > self.case.limits["max_displacement_mm"] + TOL:
                out.append(f"{t}: displacement {h:.4f} mm over the limit")
        return out

    def sweep(self, cur: dict, new: dict, max_substeps: int = MAX_SUBSTEPS) -> str | None:
        """Check the straight move from state ``cur`` to ``new`` (same vertex counts): None
        when every intermediate state keeps every slack and ``new`` is feasible, else the
        reason (``sweep``, or the first violation of ``new``)."""
        disp = []
        for t in self.ids:
            if cur[t].shape != new[t].shape:
                raise ValueError(f"{t}: sweep needs the same vertices")
            d = np.linalg.norm(new[t] - cur[t], axis=1)
            disp.append(np.maximum(d[:-1], d[1:]))
        rate = np.concatenate(disp)                                  # per segment, mm per unit of the move
        if float(rate.max(initial=0.0)) > 0:
            tau = 0.0
            for _ in range(max_substeps):
                pos = {t: cur[t] + tau * (new[t] - cur[t]) for t in self.ids}
                fs, ms, _ = self.slacks(pos)
                if (fs.size and fs.min() < -TOL) or (ms.size and ms.min() < -TOL):
                    return "sweep"
                with np.errstate(divide="ignore", invalid="ignore"):
                    dt_f = np.where(rate[:, None] > 0, fs / rate[:, None], np.inf)
                    pair = rate[:, None] + rate[None, :]
                    dt_m = np.where(pair > 0, ms / pair, np.inf)
                dt = 0.95 * float(min(dt_f.min(initial=np.inf), dt_m.min(initial=np.inf)))
                if dt >= 1.0 - tau:
                    break
                if dt <= 1e-9:
                    return "sweep"
                tau += dt
            else:
                return "sweep"
        bad = self.violations(new)
        return bad[0] if bad else None


def preflight(case: Case) -> dict:
    """Check the input geometry of every trace against all hard constraints (every trace
    movable). Returns ``feasible``, ``violations`` (text) and ``min_slack`` {value (mm), a,
    b}."""
    m = Model(case, movable=list(case.traces))
    s = m.initial()
    bad = m.violations(s)
    return {"feasible": not bad, "violations": bad, "min_slack": m.min_slack(s)}


__all__ = ["Model", "hausdorff", "inside", "point_seg", "preflight", "samples", "seg_dist", "to_polyline"]
