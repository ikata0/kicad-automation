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
        self.orig = {t: self.T(model.orig[t]) for t in ids}
        self.L0 = {t: float(np.linalg.norm(np.diff(model.orig[t], axis=0), axis=1).sum()) for t in ids}
        self.S0 = sum(len(model.orig[t]) - 1 for t in ids)
        self.bends0 = sum(int((turns_deg(model.orig[t]) > BEND).sum()) for t in ids)
        self.u = {t: self.T(_fractions(self.L0[t])) for t in ids}
        c = model.case
        edges = [(a, b) for ring in [c.outline, *c.cutouts] for a, b in zip(ring, ring[1:] + ring[:1])]
        self.ea, self.eb = self.T([e[0] for e in edges]), self.T([e[1] for e in edges])
        self.fa, self.fb = self.T(model.fa), self.T(model.fb)
        self.req = self.T(np.where(np.isfinite(model.req), model.req, -1e9))
        self.mreq = self.T(model.mreq)
        self.pairs = []                                   # (a, b, fractions on a, gate) frozen
        pref, tol = objectives["preferred_spacing_mm"], objectives["parallel_tolerance_deg"]
        for a, b in zip(ids, ids[1:]):
            for x, y in ((a, b), (b, a)):
                gate = self._gate(model.orig[x], model.orig[y], _fractions(self.L0[x]),
                                  model.half[ids.index(x)] + model.half[ids.index(y)], pref, tol)
                if gate.any():
                    self.pairs.append((x, y, self.u[x][self.T(gate) > 0]))

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

    # --- torch helpers ------------------------------------------------------------------

    def _at(self, P, u):
        t = self.t
        L = (P[1:] - P[:-1]).norm(dim=1)
        cum = t.cat([L.new_zeros(1), t.cumsum(L, 0)])
        s = u * cum[-1]
        i = (t.searchsorted(cum.detach(), s.detach(), right=True) - 1).clamp(0, len(L) - 1)
        f = (s - cum[i]) / L[i]
        return P[i] + f[:, None] * (P[i + 1] - P[i])

    def _pseg(self, p, a, b):
        v = b - a
        L2 = (v * v).sum(-1)
        u = (((p - a) * v).sum(-1) / L2.clamp_min(1e-18)).clamp(0, 1)
        q = a + u[..., None] * v - p
        return ((q * q).sum(-1) + 1e-18).sqrt()

    def _to_poly(self, pts, P):
        return self._pseg(pts[:, None], P[None, :-1], P[None, 1:]).min(1).values

    def _segdist(self, a, b, c, d):
        return self.t.minimum(self.t.minimum(self._pseg(a, c, d), self._pseg(b, c, d)),
                              self.t.minimum(self._pseg(c, a, b), self._pseg(d, a, b)))

    # --- the function -------------------------------------------------------------------

    def terms(self, P: dict, angle_boost: dict | None = None) -> dict:
        """Merit terms (torch scalars) of state ``P`` {trace: (n, 2) tensor};
        ``angle_boost`` {trace: per-segment weight tensor} weights the angle term for the
        optimiser (``adjust``), never for the reported score."""
        t, o, ids = self.t, self.o, self.m.ids
        zero = P[ids[0]].new_zeros(())
        num = den = zero
        lengths = {}
        for k in ids:
            d = P[k][1:] - P[k][:-1]
            L = (d * d).sum(1).add(1e-18).sqrt()
            lengths[k] = L
            c, s = d[:, 0] / L, d[:, 1] / L
            c2, s2 = c * c - s * s, 2 * c * s
            c4, s4 = c2 * c2 - s2 * s2, 2 * c2 * s2
            f = ((1 - (c4 * c4 - s4 * s4)) / 2).clamp_min(0)
            wseg = L * (angle_boost[k] if angle_boost and k in angle_boost else 1.0)
            num, den = num + (wseg * f).sum(), den + L.sum()
        angle = num / den

        pref = o["preferred_spacing_mm"]
        res, n = zero, 0
        for a, b, u in self.pairs:
            p = self._at(P[a], u)
            ia, ib = ids.index(a), ids.index(b)
            gap = self._to_poly(p, P[b]) - float(self.m.half[ia] + self.m.half[ib])
            res = res + (((gap - pref) / pref) ** 2).sum()
            n += len(u)
        spacing = res / n if n else zero

        margin, scale = o["preferred_edge_margin_mm"], o["displacement_scale_mm"]
        e_num = d_num = zero
        n_s = 0
        for i, k in enumerate(ids):
            p = self._at(P[k], self.u[k])
            gap = self._pseg(p[:, None], self.ea[None], self.eb[None]).min(1).values - float(self.m.half[i])
            e_num = e_num + ((t.relu(margin - gap) / margin) ** 2).sum()
            q = self._at(self.orig[k], self.u[k])
            d_num = d_num + ((self._to_poly(p, self.orig[k]) / scale) ** 2).sum() \
                + ((self._to_poly(q, P[k]) / scale) ** 2).sum()
            n_s += len(p)
        edge = e_num / n_s
        displacement = d_num / (2 * n_s)

        total_len = sum(L.sum() for L in lengths.values())
        L0 = sum(self.L0.values())
        length = t.relu(total_len - L0) / L0

        short = o["short_segment_mm"]
        bends = sum(int((turns_deg(P[k].detach().numpy()) > BEND).sum()) for k in ids)
        shorts = sum((t.relu(1 - lengths[k][1:-1] / short) ** 2).sum() if len(lengths[k]) > 2 else zero
                     for k in ids)
        complexity = (max(0, bends - self.bends0) + shorts) / self.S0
        return {"angle": angle, "spacing": spacing, "edge": edge, "displacement": displacement,
                "length": length, "complexity": complexity * (zero + 1)}

    def total(self, terms: dict):
        """Weighted sum of ``terms``."""
        return sum(self.w[k] * terms[k] for k in TERMS)

    def barrier(self, P: dict):
        """Solver-only penalty for slack below the barrier margin (torch scalar)."""
        t, ids = self.t, self.m.ids
        a = t.cat([P[k][:-1] for k in ids])
        b = t.cat([P[k][1:] for k in ids])
        tr = np.concatenate([[i] * (len(P[k]) - 1) for i, k in enumerate(ids)])
        ix = np.concatenate([np.arange(len(P[k]) - 1) for k in ids])
        m = self.margin
        out = a.new_zeros(())
        if len(self.fa):
            fs = self._segdist(a[:, None], b[:, None], self.fa[None], self.fb[None]) - self.req[self.T(tr).long()]
            out = out + ((t.relu(m - fs) / m) ** 2).sum()
        ms = self._segdist(a[:, None], b[:, None], a[None], b[None])
        same = tr[:, None] == tr[None, :]
        need = np.where(same, 0.0, self.m.mreq[tr][:, tr])
        skip = np.triu(np.ones_like(same), 0) | (same & (np.abs(ix[:, None] - ix[None, :]) <= 1))
        mask = self.T(~skip)
        out = out + ((t.relu(m - (ms - self.T(need))) / m) ** 2 * mask).sum()
        return self.bweight * out

    def evaluate(self, state: dict) -> tuple[float, dict]:
        """(total, {term: value}) of a numpy state, as plain floats."""
        with self.t.no_grad():
            terms = self.terms({k: self.T(v) for k, v in state.items()})
        vals = {k: float(v) for k, v in terms.items()}
        return sum(self.w[k] * vals[k] for k in TERMS), vals


__all__ = ["Merit", "TERMS", "VERSION", "deviation_deg", "turns_deg"]
