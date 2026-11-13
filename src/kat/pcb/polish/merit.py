"""The polisher's merit function in PyTorch (float64, autograd), definition version 1.

Six dimensionless terms, each independent of the vertex count (``specs/ROUTE_POLISH_INPUT.md``
§8); continuous ones are arc-length averages over samples placed by arc-length *fraction*,
so splitting a segment changes nothing:

- ``angle``: sin^2(4 theta) per segment (0 at multiples of 45 degrees, 1 half-way), averaged
  by segment length;
- ``spacing``: ((gap - preferred) / preferred)^2 between neighbours in group order, only
  where they ran locally parallel and close in the *original* geometry (the pairing is
  frozen, so changing it cannot hide a poor result);
- ``edge``: (shortfall below the preferred edge margin / margin)^2 against board edges and
  cut-outs, no reward beyond the margin;
- ``displacement``: (distance to the original polyline / scale)^2, both ways;
- ``length``: extra group length over the original length (no reward for shorter);
- ``complexity``: bends added (turns over 0.5 degrees) per original segment, plus
  (1 - length / short_segment)^2 for each short segment, per original segment.

``barrier`` (relu(m - slack) / m)^2 near the hard constraints steers the optimiser only;
it is never part of the reported score.
"""

from __future__ import annotations

import math

import numpy as np

from . import require
from .constraints import Model

VERSION = 1
TERMS = ("angle", "spacing", "edge", "displacement", "length", "complexity")
STEP = 0.1                  # mm between merit samples
BEND = 0.5                  # degrees: a turn this small is no bend


def _torch():
    return require("torch")


def turns_deg(p: np.ndarray) -> np.ndarray:
    """Direction change at each interior vertex, degrees."""
    d = np.diff(p, axis=0)
    a = np.arctan2(d[:, 1], d[:, 0])
    t = np.abs((np.diff(a) + math.pi) % (2 * math.pi) - math.pi)
    return np.degrees(t)


def deviation_deg(a: np.ndarray, b: np.ndarray) -> float:
    """How far segment a-b is from the nearest multiple of 45 degrees, degrees."""
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45.0
    return min(ang, 45.0 - ang)


def _fractions(length: float) -> np.ndarray:
    n = max(4, math.ceil(length / STEP))
    return (np.arange(n) + 0.5) / n


class Merit:
    """The merit of states of ``model``'s movable traces under ``objectives`` (the input's
    field names; ``weights`` may be overridden). Precomputes the frozen spacing pairing."""

    def __init__(self, model: Model, objectives: dict, barrier_margin: float = 0.05, barrier_weight: float = 1.0):
        torch = _torch()
        self.t = torch
        self.m, self.o = model, objectives
        self.w = dict(objectives["weights"])
        self.margin, self.bweight = barrier_margin, barrier_weight
        f64 = torch.float64
        self.T = lambda a: torch.as_tensor(np.asarray(a, float), dtype=f64)
        ids = model.ids
        self.L0 = {t: float(np.linalg.norm(np.diff(model.orig[t], axis=0), axis=1).sum()) for t in ids}
        self.S0 = sum(len(model.orig[t]) - 1 for t in ids)
        self.bends0 = sum(int((turns_deg(model.orig[t]) > BEND).sum()) for t in ids)
        self.u = {t: self.T(_fractions(self.L0[t])) for t in ids}
        c = model.case
        edges = [(a, b) for ring in [c.outline, *c.cutouts] for a, b in zip(ring, ring[1:] + ring[:1])]
        ea, eb = self.T([e[0] for e in edges]), self.T([e[1] for e in edges])
        self.edges = (ea[:, 0], ea[:, 1], eb[:, 0], eb[:, 1])
        self.fa, self.fb = self.T(model.fa), self.T(model.fb)
        self.req = self.T(np.where(np.isfinite(model.req), model.req, -1e9))
        self.gates = []                                   # (a, b, gate on a's samples) frozen
        pref, tol = objectives["preferred_spacing_mm"], objectives["parallel_tolerance_deg"]
        for a, b in zip(ids, ids[1:]):
            for x, y in ((a, b), (b, a)):
                gate = self._gate(model.orig[x], model.orig[y], _fractions(self.L0[x]),
                                  model.half[ids.index(x)] + model.half[ids.index(y)], pref, tol)
                if gate.any():
                    self.gates.append((x, y, gate))
        # everything batched: the traces' vertices, segments and samples stacked in id order
        self._layouts = {}
        Li = lambda a: torch.as_tensor(np.asarray(a), dtype=torch.long)
        self.tr_u = np.concatenate([[i] * len(self.u[k]) for i, k in enumerate(ids)])
        self.u_all = torch.cat([self.u[k] for k in ids])
        start = np.concatenate([[0], np.cumsum([len(self.u[k]) for k in ids])])
        rows, to, half = [], [], []
        for x, y, g in self.gates:
            i, j = ids.index(x), ids.index(y)
            rows.append(start[i] + np.flatnonzero(g))
            to += [j] * int(g.sum())
            half += [model.half[i] + model.half[j]] * int(g.sum())
        self.pair_rows = Li(np.concatenate(rows) if rows else np.zeros(0, int))
        self.pair_to = np.array(to, int)
        self.pair_half = self.T(half)
        self.half_u = self.T(model.half[self.tr_u])
        self.L0_total = sum(self.L0.values())
        V0 = self.T(np.concatenate([model.orig[k] for k in ids]))
        lay0 = self._layout(tuple(len(model.orig[k]) for k in ids), own=True)
        ox, oy = V0[:, 0], V0[:, 1]
        sa, sb, g = lay0["seg_a"], lay0["seg_b"], lay0["rows"]
        self.orig_seg = (ox[sa][g], oy[sa][g], ox[sb][g], oy[sb][g])       # each sample's own original
        self.qx, self.qy = self._sample(ox, oy, lay0)

    @staticmethod
    def _gate(px, py, u, halves, pref, tol) -> np.ndarray:
        """Which samples of ``px`` (at fractions ``u``) face ``py`` locally parallel within
        ``tol`` degrees and closer than twice the preferred gap, in the original geometry."""
        from .constraints import point_seg
        L = np.linalg.norm(np.diff(px, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(L)])
        s = u * cum[-1]
        i = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(L) - 1)
        p = px[i] + ((s - cum[i]) / L[i])[:, None] * (px[i + 1] - px[i])
        dx = (px[i + 1] - px[i]) / L[i][:, None]
        d = point_seg(p[:, None], py[None, :-1], py[None, 1:])
        j = d.argmin(1)
        dy = py[j + 1] - py[j]
        dy = dy / np.linalg.norm(dy, axis=1)[:, None]
        cos = np.abs((dx * dy).sum(1)).clip(0, 1)
        parallel = np.degrees(np.arccos(cos)) <= tol
        return parallel & (d.min(1) - halves <= 2 * pref)

    # --- torch helpers (x and y apart: reducing a trailing axis of 2 is slow) ---------

    @staticmethod
    def _pseg(px, py, ax, ay, bx, by):
        vx, vy = bx - ax, by - ay
        wx, wy = px - ax, py - ay
        u = ((wx * vx + wy * vy) / (vx * vx + vy * vy).clamp_min(1e-18)).clamp(0, 1)
        qx, qy = u * vx - wx, u * vy - wy
        return (qx * qx + qy * qy + 1e-18).sqrt()

    def _segdist(self, a, b, c, d):
        """Distance between segments a-b and c-d, each end an (x, y) pair of tensors."""
        ps, mn = self._pseg, self.t.minimum
        return mn(mn(ps(*a, *c, *d), ps(*b, *c, *d)), mn(ps(*c, *a, *b), ps(*d, *a, *b)))

    def _sample(self, x, y, lay):
        """The points at the arc-length fractions of every trace's samples, on the stacked
        vertices (``x``, ``y``) laid out by ``lay``."""
        t = self.t
        sa, sb, first, last = lay["seg_a"], lay["seg_b"], lay["first_u"], lay["last_u"]
        dx, dy = x[sb] - x[sa], y[sb] - y[sa]
        L = (dx * dx + dy * dy).sqrt()
        cum = t.cat([L.new_zeros(1), t.cumsum(L, 0)])
        c0, c1 = cum[first], cum[last + 1]
        s = c0 + self.u_all * (c1 - c0)
        i = t.searchsorted(cum.detach(), s.detach(), right=True) - 1
        i = t.maximum(t.minimum(i, last), first)
        f = (s - cum[i]) / L[i]
        return x[sa[i]] + f * dx[i], y[sa[i]] + f * dy[i]

    def _layout(self, counts: tuple, own: bool = False) -> dict:
        """Index tensors for a state whose traces have ``counts`` vertices (cached): the
        segments' ends, each sample's trace segments, which segments each sample row is
        measured against (``own``: its own trace only; else its own, then for the spacing
        rows their partner's), and the barrier's segment pairs."""
        lay = None if own else self._layouts.get(counts)
        if lay is not None:
            return lay
        t, m = self.t, self.m
        Li = lambda a: t.as_tensor(np.asarray(a), dtype=t.long)
        off = np.concatenate([[0], np.cumsum(counts)])
        tr = np.concatenate([[i] * (c - 1) for i, c in enumerate(counts)])
        ix = np.concatenate([np.arange(c - 1) for c in counts])
        seg_a = np.concatenate([off[i] + np.arange(c - 1) for i, c in enumerate(counts)])
        soff = np.concatenate([[0], np.cumsum([c - 1 for c in counts])])
        kmax = max(counts) - 1
        table = np.array([[soff[i] + min(k, c - 2) for k in range(kmax)] for i, c in enumerate(counts)])
        targets = self.tr_u if own else np.concatenate([self.tr_u, self.pair_to])
        interior = (ix > 0) & (ix < np.array(counts)[tr] - 2)
        i, j = np.triu_indices(len(tr), 1)
        keep = ~((tr[i] == tr[j]) & (np.abs(ix[i] - ix[j]) <= 1))
        i, j = i[keep], j[keep]
        lay = {"seg_a": Li(seg_a), "seg_b": Li(seg_a + 1), "off": off.tolist(),
               "first_u": Li(soff[:-1][self.tr_u]), "last_u": Li(soff[1:][self.tr_u] - 1),
               "rows": Li(table[targets]), "interior": t.as_tensor(interior),
               "req": self.req[Li(tr)], "pi": Li(i), "pj": Li(j),
               "need": self.T(np.where(tr[i] == tr[j], 0.0, m.mreq[tr[i], tr[j]])),
               "ones": [t.ones(c - 1, dtype=t.float64) for c in counts]}
        if not own:
            self._layouts[counts] = lay
        return lay

    def _stack(self, P: dict):
        ids = self.m.ids
        lay = self._layout(tuple(len(P[k]) for k in ids))
        V = self.t.cat([P[k] for k in ids])
        return lay, V, V[:, 0], V[:, 1]

    # --- the function -------------------------------------------------------------------

    def terms(self, P: dict, angle_boost: dict | None = None) -> dict:
        """Merit terms (torch scalars) of state ``P`` {trace: (n, 2) tensor};
        ``angle_boost`` {trace: per-segment weight tensor} weights the angle term for the
        optimiser (``adjust``), never for the reported score."""
        t, o, ids = self.t, self.o, self.m.ids
        lay, V, x, y = self._stack(P)
        sa, sb = lay["seg_a"], lay["seg_b"]
        ax, ay, bx, by = x[sa], y[sa], x[sb], y[sb]
        dx, dy = bx - ax, by - ay
        L = (dx * dx + dy * dy + 1e-18).sqrt()
        c, s = dx / L, dy / L
        c2, s2 = c * c - s * s, 2 * c * s
        c4, s4 = c2 * c2 - s2 * s2, 2 * c2 * s2
        f = ((1 - (c4 * c4 - s4 * s4)) / 2).clamp_min(0)
        w = L
        if angle_boost:
            w = L * t.cat([angle_boost.get(k, one) for k, one in zip(ids, lay["ones"])])
        total_len = L.sum()
        angle = (w * f).sum() / total_len
        zero = V.new_zeros(())

        px, py = self._sample(x, y, lay)
        # each row against the segments of one current trace: the original's samples against
        # their own trace, the gated samples against their spacing partner
        g = lay["rows"]
        rx = t.cat([self.qx, px[self.pair_rows]])[:, None]
        ry = t.cat([self.qy, py[self.pair_rows]])[:, None]
        dist = self._pseg(rx, ry, ax[g], ay[g], bx[g], by[g]).min(1).values
        n_q = len(self.qx)
        pref = o["preferred_spacing_mm"]
        if len(self.pair_rows):
            gap = dist[n_q:] - self.pair_half
            spacing = (((gap - pref) / pref) ** 2).sum() / len(self.pair_rows)
        else:
            spacing = zero

        margin, scale = o["preferred_edge_margin_mm"], o["displacement_scale_mm"]
        n_s = len(px)
        gap = self._pseg(px[:, None], py[:, None], *self.edges).min(1).values - self.half_u
        edge = ((t.relu(margin - gap) / margin) ** 2).sum() / n_s
        to_orig = self._pseg(px[:, None], py[:, None], *self.orig_seg).min(1).values
        displacement = (((to_orig / scale) ** 2).sum() + ((dist[:n_q] / scale) ** 2).sum()) / (2 * n_s)

        length = t.relu(total_len - self.L0_total) / self.L0_total

        short = o["short_segment_mm"]
        Vn, off = V.detach().numpy(), lay["off"]
        bends = sum(int((turns_deg(Vn[i:j]) > BEND).sum()) for i, j in zip(off[:-1], off[1:]))
        shorts = (t.relu(1 - L[lay["interior"]] / short) ** 2).sum()
        complexity = (max(0, bends - self.bends0) + shorts) / self.S0
        return {"angle": angle, "spacing": spacing, "edge": edge, "displacement": displacement,
                "length": length, "complexity": complexity * (zero + 1)}

    def total(self, terms: dict):
        """Weighted sum of ``terms``."""
        return sum(self.w[k] * terms[k] for k in TERMS)

    def barrier(self, P: dict):
        """Solver-only penalty for slack below the barrier margin (torch scalar)."""
        t = self.t
        lay, V, x, y = self._stack(P)
        sa, sb = lay["seg_a"], lay["seg_b"]
        a, b = (x[sa], y[sa]), (x[sb], y[sb])
        m = self.margin
        out = V.new_zeros(())
        if len(self.fa):
            col = lambda e: (e[0][:, None], e[1][:, None])
            fs = self._segdist(col(a), col(b), (self.fa[:, 0], self.fa[:, 1]), (self.fb[:, 0], self.fb[:, 1])) - lay["req"]
            out = out + ((t.relu(m - fs) / m) ** 2).sum()
        i, j = lay["pi"], lay["pj"]
        if len(i):
            ms = self._segdist((a[0][i], a[1][i]), (b[0][i], b[1][i]), (a[0][j], a[1][j]), (b[0][j], b[1][j]))
            out = out + ((t.relu(m - (ms - lay["need"])) / m) ** 2).sum()
        return self.bweight * out

    def evaluate(self, state: dict) -> tuple[float, dict]:
        """(total, {term: value}) of a numpy state, as plain floats."""
        with self.t.no_grad():
            terms = self.terms({k: self.T(v) for k, v in state.items()})
        vals = {k: float(v) for k, v in terms.items()}
        return sum(self.w[k] * vals[k] for k in TERMS), vals


__all__ = ["Merit", "TERMS", "VERSION", "deviation_deg", "turns_deg"]
