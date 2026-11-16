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

Fixed primitives are measured only where they can matter: k-d trees (scipy) over pieces
of them yield the pairs whose slack may be at most ``SKIN``; every other pair has more
slack and decides nothing. A sweep widens that by the move's largest displacement, so a
pair left out cannot come into contact on the way. ``Model(prune=False)`` measures every
pair (the reference).
"""

from __future__ import annotations

import math

import numpy as np

from ...env import KatError
from . import require
from .case import Case

TOL = 1e-9                  # mm: numerical tolerance, never a clearance allowance
MAX_SUBSTEPS = 64
SKIN = 1.0                  # mm: slack beyond which a fixed primitive is not measured
CHUNK = 0.5                 # mm: longest piece of a segment in the spatial index
BAND = 0.25                 # mm: primitives are indexed in bands of their required distance


def point_seg(p, s, e) -> np.ndarray:
    """Distance of points ``p`` to segments s-e (arrays of points, broadcast)."""
    sx, sy = s[..., 0], s[..., 1]                    # x and y apart: no reduction over 2
    vx, vy = e[..., 0] - sx, e[..., 1] - sy
    wx, wy = p[..., 0] - sx, p[..., 1] - sy
    L2 = vx * vx + vy * vy
    u = np.clip((wx * vx + wy * vy) / np.where(L2 > 0, L2, 1.0), 0.0, 1.0)
    qx, qy = u * vx - wx, u * vy - wy
    return np.sqrt(qx * qx + qy * qy)


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


def to_polyline(points: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Distance of each of ``points`` (N, 2) to the polyline ``pts``."""
    return point_seg(points[:, None], pts[None, :-1], pts[None, 1:]).min(1)


def ring_edges(ring) -> tuple[np.ndarray, np.ndarray]:
    r = np.asarray(ring, float)
    return r, np.roll(r, -1, axis=0)


def _pieces(a: np.ndarray, b: np.ndarray, h: float) -> tuple[np.ndarray, np.ndarray]:
    """Midpoints of the pieces, none longer than ``h``, of segments a-b (every point of a
    segment lies within h/2 of one) and the segment index of each."""
    v = b - a
    n = np.maximum(1, np.ceil(np.hypot(v[:, 0], v[:, 1]) / h).astype(int))
    seg = np.repeat(np.arange(len(a)), n)
    i = np.arange(len(seg)) - np.repeat(np.cumsum(n) - n, n)
    return a[seg] + ((i + 0.5) / n[seg])[:, None] * v[seg], seg


def joints(case: Case, t: str) -> set[str]:
    """The ids of same-net objects that trace ``t`` is joined to at its fixed ends in the input
    (version 2; empty for version 1), besides its anchors: fixed copper that its end cap (the
    disc of half its width at a fixed end) meets, other traces whose end cap meets one of its
    end caps, and the drills inside such copper. Every move keeps the ends, so these
    contacts cannot open: clearance to them is waived like to an anchor. A same-net contact
    with a trace's body is not a joint (a move could break it)."""
    if case.version < 2:
        return set()
    from .case import TOUCH, copper_distance
    me = case.traces[t]
    ends, half = (me.points[0], me.points[-1]), me.width / 2
    out = {c.id for c in case.copper.values() if c.net == me.net and c.id not in me.anchors
           and any(copper_distance(c, e) <= half + TOL for e in ends)}
    for u in case.traces.values():
        if u.id != t and u.net == me.net and any(
                math.dist(p, q) <= half + u.width / 2 + TOL for p in ends for q in (u.points[0], u.points[-1])):
            out.add(u.id)
    joined_cu = [case.copper[c] for c in out if c in case.copper]
    out |= {h.id for h in case.holes.values() if any(copper_distance(c, h.center) <= TOUCH for c in joined_cu)}
    return out


class Model:
    """The constraints of one run: ``movable`` traces (default the group ``group``) move,
    all other traces are fixed copper. ``orig`` holds the input geometry. ``prune`` false
    measures every (segment, fixed primitive) pair instead of the nearby ones (reference)."""

    def __init__(self, case: Case, group: str | None = None, movable: list[str] | None = None,
                 prune: bool = True):
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
        A = np.array([p[0] for p in prims], float).reshape(-1, 2)
        B = np.array([p[1] for p in prims], float).reshape(-1, 2)
        R = np.array([p[2] for p in prims], float)
        gap = np.maximum(np.maximum(lo - np.maximum(A, B), np.minimum(A, B) - hi), 0.0)
        keep = np.flatnonzero(np.hypot(gap[:, 0], gap[:, 1]) <= reach + R + 1e-6)
        self.fa, self.fb, self.fr = A[keep], B[keep], R[keep]
        self.fkind = [prims[j][3] for j in keep]
        self.fowner = [prims[j][4] for j in keep]
        # clearance floors per (trace, obstacle owner): 0 normally; a run that starts short of
        # clearance (``allow``) may stay as short as it is, never shorter (``tighten``)
        self.owners = sorted(set(self.fowner))
        pos = {o: i for i, o in enumerate(self.owners)}
        self.own = np.array([pos[o] for o in self.fowner], int)
        base = {"copper": case.copper_clearance, "hole": case.hole_clearance, "edge": case.edge_clearance, "region": 0.0}
        n, F = len(self.ids), len(keep)
        self.joined = {t: joints(case, t) for t in self.ids}
        cl = np.tile(np.array([case.keepouts[o].clearance if k == "keepout" else base[k]
                               for k, o in zip(self.fkind, self.fowner)], float), (n, 1))
        kind = np.array(self.fkind, dtype=object)
        paired = np.isin(kind, ("copper", "hole", "keepout"))          # physical edges: no pair entries
        own_ = np.isin(kind, ("copper", "hole"))
        row = {t: i for i, t in enumerate(self.ids)}
        for key, v in case.pairs.items():
            if len(key) != 2:
                continue
            for t, o in (tuple(key), tuple(key)[::-1]):
                if t in row and o in pos:
                    js = paired & (self.own == pos[o])
                    cl[row[t], js] = np.maximum(cl[row[t], js], v)
        self.req = self.half[:, None] + self.fr[None, :] + cl
        for i, t in enumerate(self.ids):
            anchors = [pos[o] for o in set(case.traces[t].anchors) | self.joined[t] if o in pos]
            self.req[i, own_ & np.isin(self.own, anchors)] = -np.inf        # own anchor or joint: no clearance
        # what a move must never do on the way: the centreline touches an obstacle (or own
        # anchors, waived). Feasible states keep half width + clearance > 0 of this slack,
        # so a trace exactly at its clearance can still move.
        self.treq = np.where(np.isinf(self.req), -np.inf, np.broadcast_to(self.fr, self.req.shape))
        self.mreq = np.array([[self.half[i] + self.half[j] + case.clearance(self.ids[i], self.ids[j], case.copper_clearance)
                               for j in range(n)] for i in range(n)]).reshape(n, n)
        for i, t in enumerate(self.ids):                 # same-net runs joined at their fixed ends
            for j, u in enumerate(self.ids):
                if u in self.joined[t]:
                    self.mreq[i, j] = self.mreq[j, i] = -np.inf
        self.floor_f = np.zeros((n, len(self.owners)))
        self.floor_m = np.zeros((n, n))
        self.polys = [(c.id, np.asarray(c.points, float)) for c in case.copper.values() if c.kind == "polygon"]
        self.keep_polys = [(k.id, np.asarray(k.points, float)) for k in case.keepouts.values()]
        rings = [r for _, r in self.polys + self.keep_polys]
        self.poly_box = np.array([[r.min(0), r.max(0)] for r in rings], float).reshape(-1, 2, 2)
        # the spatial index: pieces of the fixed primitives in k-d trees, one per band of the
        # largest distance any movable trace requires of them (``BAND``)
        need = np.where(np.isfinite(self.req), self.req, 0.0).max(0, initial=0.0) if n else np.zeros(F)
        self.prune, self.trees = prune and F > 0, []
        if self.prune:
            self.kdtree = require("scipy.spatial").cKDTree
            band = np.ceil(need / BAND)
            for level in np.unique(band):
                js = np.flatnonzero(band == level)
                pts, seg = _pieces(self.fa[js], self.fb[js], CHUNK)
                self.trees.append((float(need[js].max()), js[seg], self.kdtree(pts)))

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

    def candidates(self, a: np.ndarray, b: np.ndarray, slack: float = math.inf) -> tuple[np.ndarray, np.ndarray]:
        """Indices (k, j), sorted by k then j, of movable segments a-b ((M, 2) arrays) and
        fixed primitives whose clearance slack may be at most ``slack`` (mm): every pair
        whose slack is (for any movable trace), and some others. All M x F pairs with
        ``slack`` inf or without pruning."""
        M, F = len(a), len(self.fa)
        if not self.prune or not math.isfinite(slack) or M == 0:
            k, j = np.divmod(np.arange(M * F), F) if F else (np.zeros(0, int), np.zeros(0, int))
            return k, j
        pts, seg = _pieces(a, b, CHUNK)
        query = self.kdtree(pts)
        keys = []
        for need, jmap, tree in self.trees:
            hit = query.sparse_distance_matrix(tree, need + slack + CHUNK + 1e-9, output_type="ndarray")
            keys.append(seg[hit["i"]] * F + jmap[hit["j"]])
        return np.divmod(np.unique(np.concatenate(keys)), F)

    def _fixed(self, a, b, tr, near, topology=False):
        k, j = near
        return k, j, seg_dist(a[k], b[k], self.fa[j], self.fb[j]) - (self.treq if topology else self.req)[tr[k], j]

    def slacks(self, state, topology: bool = False, cutoff: float | None = None) -> tuple[tuple, np.ndarray, tuple]:
        """Slack (mm) of the movable segments against the fixed primitives, sparse as (k, j,
        value) arrays sorted by segment k then primitive j: every pair whose slack is at most
        ``cutoff`` (default ``SKIN``) is there, others may be missing (all with ``prune``
        off). Also the slack against every other movable segment (M, M; +inf where not
        constrained), and the segments' (trace index, segment index). With ``topology``
        the slack is to contact of the centreline instead of to the clearance."""
        a, b, tr, ix = self._segments(state)
        near = self.candidates(a, b, SKIN if cutoff is None else cutoff)
        fs = self._fixed(a, b, tr, near, topology)
        ms = seg_dist(a[:, None], b[:, None], a[None], b[None])
        ms = np.where(np.isinf(self.mreq[tr][:, tr]), np.inf, ms)       # joined runs: no constraint
        same = tr[:, None] == tr[None, :]
        if not topology:
            ms = ms - np.where(same, 0.0, self.mreq[tr][:, tr])         # own segments: no crossing
        adjacent = same & (np.abs(ix[:, None] - ix[None, :]) <= 1)
        ms[adjacent] = np.inf
        return fs, ms, (tr, ix)

    def pair_slack(self, state) -> tuple[np.ndarray, np.ndarray]:
        """The least clearance slack (mm) per (movable trace, obstacle owner) and per pair of
        movable traces (diagonal +inf). Exact up to ``SKIN``; a larger slack to an
        obstacle may read +inf."""
        (k, j, v), ms, (tr, ix) = self.slacks(state)
        n = len(self.ids)
        f = np.full((n, len(self.owners)), np.inf)
        np.minimum.at(f, (tr[k], self.own[j]), v)
        mm = np.full((len(tr), n), np.inf)
        same = tr[:, None] == tr[None, :]
        np.minimum.at(mm.T, tr, np.where(same, np.inf, ms).T)
        m = np.full((n, n), np.inf)
        np.minimum.at(m, tr, mm)
        return f, m

    def allow(self, state) -> None:
        """Let a run start from ``state`` although it is short of clearance: each (trace,
        obstacle) pair may stay as short as it is in ``state``, not shorter."""
        f, m = self.pair_slack(state)
        self.floor_f, self.floor_m = np.minimum(f, 0.0), np.minimum(m, 0.0)

    @property
    def recovering(self) -> bool:
        """Whether some floor still allows a shortfall."""
        return bool(self.floor_f.any() or self.floor_m.any())

    def tighten(self, state) -> None:
        """Raise the floors to what ``state`` reaches (a shortfall once reduced stays
        reduced)."""
        if self.recovering:
            f, m = self.pair_slack(state)
            self.floor_f = np.maximum(self.floor_f, np.minimum(f, 0.0))
            self.floor_m = np.maximum(self.floor_m, np.minimum(m, 0.0))

    def shortfall(self, state) -> float:
        """How far ``state`` is short of clearance in total (mm, summed over (trace,
        obstacle) pairs; 0 when every clearance holds)."""
        f, m = self.pair_slack(state)
        return float(np.maximum(-f, 0.0).sum() + np.triu(np.maximum(-m, 0.0), 1).sum())

    def short_pairs(self, state) -> list[tuple[str, int, str]]:
        """The segments short of clearance, worst first, as (trace, segment index, obstacle:
        an owner or a movable trace id); one entry per (segment, obstacle)."""
        (fk, fj, fv), ms, (tr, ix) = self.slacks(state)
        ms = np.where(tr[:, None] == tr[None, :], np.inf, ms)
        out = {}
        for k, j, v in zip(fk[fv < -TOL], fj[fv < -TOL], fv[fv < -TOL]):
            key = (self.ids[tr[k]], int(ix[k]), self.fowner[j])
            out[key] = min(out.get(key, 0.0), float(v))
        for k, j in zip(*np.nonzero(ms < -TOL)):
            key = (self.ids[tr[k]], int(ix[k]), self.ids[tr[j]])
            out[key] = min(out.get(key, 0.0), float(ms[k, j]))
        return sorted(out, key=out.get)

    def closest(self, state, t: str, k: int, obstacle: str) -> np.ndarray:
        """The point of segment ``k`` of trace ``t`` nearest to ``obstacle`` (an owner or a
        movable trace id), kept off the segment's ends."""
        a, b = state[t][k], state[t][k + 1]
        u = np.linspace(0.05, 0.95, 91)
        pts = a + u[:, None] * (b - a)
        if obstacle in self.ids:
            q = state[obstacle]
            d = point_seg(pts[:, None], q[None, :-1], q[None, 1:]).min(1)
        else:
            j = np.nonzero(self.own == self.owners.index(obstacle))[0]
            d = (point_seg(pts[:, None], self.fa[j][None], self.fb[j][None]) - self.fr[j][None]).min(1)
        return pts[int(np.argmin(d))]

    def blocking(self, state) -> list[str]:
        """What no move can repair in ``state``: every hard-constraint failure except a
        clearance shortfall whose centreline is still clear of the obstacle."""
        bad = [v for v in self.violations(state) if not v.startswith("clearance")]
        (fk, fj, fv), ms, (tr, ix) = self.slacks(state, topology=True)
        for k, j in zip(fk[fv <= TOL], fj[fv <= TOL]):
            bad.append(f"{self._ref(tr, ix, k)} touches {self.fowner[j]}")
        for k, j in zip(*np.nonzero(np.triu(ms <= TOL))):
            bad.append(f"{self._ref(tr, ix, k)} touches {self._ref(tr, ix, j)}")
        return bad

    def _ref(self, tr, ix, k) -> str:
        return f"{self.ids[tr[k]]}#{ix[k]}"

    def min_slack(self, state) -> dict:
        """The smallest slack in ``state``: {value (mm), a, b} with object references
        (``t01#3`` is segment 3 of trace t01)."""
        cutoff = SKIN
        while True:
            (fk, fj, fv), ms, (tr, ix) = self.slacks(state, cutoff=cutoff)
            # a pair left out has more slack than the cutoff: the least is known once
            # something is at or below it
            least = min(fv.min(initial=math.inf), ms.min(initial=math.inf))
            if least <= cutoff or not math.isfinite(cutoff) or not self.prune:
                break
            cutoff = cutoff * 4 if cutoff < 64 * SKIN else math.inf
        out = {"value": math.inf, "a": None, "b": None}
        if fv.size:
            i = int(np.argmin(fv))
            out = {"value": float(fv[i]), "a": self._ref(tr, ix, fk[i]), "b": self.fowner[fj[i]]}
        if ms.size and float(ms.min()) < out["value"]:
            k = np.unravel_index(np.argmin(ms), ms.shape)
            out = {"value": float(ms[k]), "a": self._ref(tr, ix, k[0]), "b": self._ref(tr, ix, k[1])}
        return out

    def violations(self, state) -> list[str]:
        """Every hard-constraint failure of ``state`` as text; empty when feasible.
        Independent of how the state was reached (the sweep checks the way). Clearance is
        measured against the floors (``allow``): 0 unless the run started short."""
        out = []
        for t in self.ids:
            p, o = state[t], self.orig[t]
            if not (np.array_equal(p[0], o[0]) and np.array_equal(p[-1], o[-1])):
                out.append(f"{t}: anchor moved")
            if np.any(np.linalg.norm(np.diff(p, axis=0), axis=1) <= TOL):
                out.append(f"{t}: zero-length segment")
        (fk, fj, fv), ms, (tr, ix) = self.slacks(state)
        short = fv < self.floor_f[tr[fk], self.own[fj]] - TOL
        for k, j, v in zip(fk[short], fj[short], fv[short]):
            out.append(f"clearance {self._ref(tr, ix, k)}-{self.fowner[j]} short by {-v:.4f} mm")
        floor = np.where(tr[:, None] == tr[None, :], 0.0, self.floor_m[tr][:, tr])
        for k, j in zip(*np.nonzero(np.triu(ms < floor - TOL))):
            out.append(f"clearance {self._ref(tr, ix, k)}-{self._ref(tr, ix, j)} short by {-ms[k, j]:.4f} mm")
        rings = self.polys + self.keep_polys
        for i, t in enumerate(self.ids):
            p = state[t]
            if not inside(p, np.asarray(self.case.outline, float)).all() or any(
                    inside(p, np.asarray(c, float)).any() for c in self.case.cutouts):
                out.append(f"{t}: off the board")
            if not inside(p, np.asarray(self.case.region, float)).all():
                out.append(f"{t}: outside the movement region")
            anchors = set(self.case.traces[t].anchors)
            box = self.poly_box                          # a ring can only hold a vertex inside its box
            near = np.flatnonzero(((p[None] >= box[:, None, 0]) & (p[None] <= box[:, None, 1])).all(2).any(1))
            for pid, ring in (rings[q] for q in near):
                if pid not in anchors and inside(p, ring).any():
                    out.append(f"{t}: inside {pid}")
        return out

    def sweep(self, cur: dict, new: dict, max_substeps: int = MAX_SUBSTEPS, exempt=(),
              final: dict | None = None) -> str | None:
        """Check the straight move from state ``cur`` to ``new`` (same vertex counts): None
        when no centreline touches an obstacle or another trace on the way (which keeps the
        topology) and ``new`` is feasible, else the reason (``sweep``, or the first
        violation of ``new``). Clearance is required of ``new`` only, so a trace at exactly
        its clearance, or short of it, can move away. ``exempt``: (trace, segment index,
        segment index) pairs of one trace that may meet (the legs beside a segment that
        collapses); ``final``: the state checked at the end instead of ``new`` (``new``
        with the collapsed segment removed)."""
        disp = []
        for t in self.ids:
            if cur[t].shape != new[t].shape:
                raise ValueError(f"{t}: sweep needs the same vertices")
            d = np.linalg.norm(new[t] - cur[t], axis=1)
            disp.append(np.maximum(d[:-1], d[1:]))
        rate = np.concatenate(disp)                                  # per segment, mm per unit of the move
        if float(rate.max(initial=0.0)) > 0:
            tau, skip = 0.0, None
            # a fixed primitive farther than this from a segment at the start stays more
            # than SKIN clear of its centreline all the way: never measured on the way
            a0, b0, tr, ix = self._segments(cur)
            near = self.candidates(a0, b0, SKIN + float(rate.max()))
            for _ in range(max_substeps):
                pos = {t: cur[t] + tau * (new[t] - cur[t]) for t in self.ids}
                a, b, tr, ix = self._segments(pos)
                fk, fj, fv = self._fixed(a, b, tr, near, topology=True)
                ms = seg_dist(a[:, None], b[:, None], a[None], b[None])
                ms[(tr[:, None] == tr[None, :]) & (np.abs(ix[:, None] - ix[None, :]) <= 1)] = np.inf
                if exempt:
                    if skip is None:
                        skip = np.zeros(ms.shape, bool)
                        for t, i, j in exempt:
                            mine = tr == self.ids.index(t)
                            skip |= np.outer(mine & (ix == i), mine & (ix == j))
                        skip |= skip.T
                    ms = np.where(skip, np.inf, ms)
                if (fv.size and fv.min() < -TOL) or (ms.size and ms.min() < -TOL):
                    return "sweep"
                with np.errstate(divide="ignore", invalid="ignore"):
                    dt_f = np.where(rate[fk] > 0, fv / rate[fk], np.inf)
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
        bad = self.violations(new if final is None else final)
        return bad[0] if bad else None


def preflight(case: Case) -> dict:
    """Check the input geometry of every trace against all hard constraints (every trace
    movable). Returns ``feasible``, ``violations`` (text) and ``min_slack`` {value (mm), a,
    b}."""
    m = Model(case, movable=list(case.traces))
    s = m.initial()
    bad = m.violations(s)
    return {"feasible": not bad, "violations": bad, "min_slack": m.min_slack(s)}


__all__ = ["Model", "inside", "joints", "point_seg", "preflight", "seg_dist", "to_polyline"]
