"""A small 2D electrostatic solver on a board cross-section (finite differences, Laplace
with piecewise εr): the mutual capacitance per unit length of side-by-side copper, where
the parallel-plate estimate of the coupling rule sees nothing. Needs numpy and scipy
(``pip install kat[field]``).

Conductors are rectangles (y0, y1, z0, z1) in mm at a fixed potential; the domain's edges
are 0 V (a reference plane where the stack-up has one, far away elsewhere).
"""

from __future__ import annotations

from .. import env

EPS0 = 8.854e-15  # F/mm
MAX_NODES = 250_000


def _np():
    try:
        import numpy as np
        import scipy.sparse as sp
        import scipy.sparse.linalg as spla
    except ImportError:
        raise env.KatError("the 2D coupling estimate needs numpy and scipy: pip install kat[field]")
    return np, sp, spla


def mutual(conductors: dict[str, list[tuple[float, float, float, float]]],
           dielectrics: list[tuple[tuple[float, float, float, float], float]],
           aggressor: str, victim: str, y: tuple[float, float], z: tuple[float, float],
           h: float = 0.01) -> float:
    """Mutual capacitance (F/mm) between conductors ``aggressor`` (1 V) and ``victim``
    (0 V, as all others): the victim's induced charge. ``conductors``: name -> rectangles
    (y0, y1, z0, z1, mm); ``dielectrics``: (rectangle, εr), air elsewhere; domain
    ``y`` x ``z`` (mm) with 0 V edges; grid step ``h`` mm."""
    np, sp, spla = _np()
    ny = int(round((y[1] - y[0]) / h)) + 1
    nz = int(round((z[1] - z[0]) / h)) + 1
    yy, zz = np.meshgrid(y[0] + h * np.arange(ny), z[0] + h * np.arange(nz), indexing="ij")
    eps = np.ones((ny, nz))
    for (y0, y1, z0, z1), er in dielectrics:
        eps[(yy >= y0) & (yy <= y1) & (zz >= z0) & (zz <= z1)] = er
    fixed = np.zeros((ny, nz), bool)
    volt = np.zeros((ny, nz))
    owner = np.full((ny, nz), -1)
    names = list(conductors)
    for k, n in enumerate(names):
        for (y0, y1, z0, z1) in conductors[n]:
            m = (yy >= y0 - h / 2) & (yy <= y1 + h / 2) & (zz >= z0 - h / 2) & (zz <= z1 + h / 2)
            fixed |= m
            owner[m] = k
            if n == aggressor:
                volt[m] = 1.0
    fixed[0, :] = fixed[-1, :] = fixed[:, 0] = fixed[:, -1] = True
    free = ~fixed
    idx = -np.ones((ny, nz), int)
    nfree = int(free.sum())
    idx[free] = np.arange(nfree)
    rows, cols, vals = [], [], []
    rhs, diag = np.zeros(nfree), np.zeros(nfree)
    inner = np.s_[1:-1, 1:-1]
    steps = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for di, dj in steps:
        nb = np.s_[1 + di:ny - 1 + di, 1 + dj:nz - 1 + dj]
        e = 0.5 * (eps[inner] + eps[nb])
        fa, fb = free[inner], free[nb]
        ia, ib = idx[inner], idx[nb]
        np.add.at(diag, ia[fa], e[fa])
        both = fa & fb
        rows.append(ia[both]); cols.append(ib[both]); vals.append(-e[both])
        edge = fa & ~fb
        np.add.at(rhs, ia[edge], e[edge] * volt[nb][edge])
    ar = np.arange(nfree)
    a = sp.csr_matrix((np.concatenate(vals + [diag]), (np.concatenate(rows + [ar]), np.concatenate(cols + [ar]))),
                      shape=(nfree, nfree))
    v = volt.copy()
    v[free] = spla.spsolve(a.tocsc(), rhs)
    kv = names.index(victim)
    q = 0.0
    for di, dj in steps:
        nb = np.s_[1 + di:ny - 1 + di, 1 + dj:nz - 1 + dj]
        m = (owner[inner] == kv) & (owner[nb] != kv)
        e = 0.5 * (eps[inner] + eps[nb])
        q += float(np.sum(e[m] * (v[inner][m] - v[nb][m])))
    return abs(q) * EPS0


def side_by_side(width_a: float, width_b: float, gap: float, below: float, above: float | None = None,
                 epsilon_r: float = 4.5, copper: float = 0.035, h: float | None = None) -> float:
    """Mutual capacitance (pF/mm) of two tracks side by side on one layer: widths
    ``width_a``, ``width_b`` and edge ``gap`` (mm), ``copper`` thick, a reference plane
    ``below`` mm under them and, on an inner layer, one ``above`` mm over them (None: an
    outer layer, air above; solder mask left out), dielectric ``epsilon_r``. Multiply by
    the parallel length for pF. An estimate: the grid (``h`` mm, default from the
    geometry) makes it good to about 10 %."""
    if min(width_a, width_b, below, copper, gap) <= 0 or (above is not None and above <= 0):
        raise env.KatError("widths, gap, plane distances and copper must be > 0 mm")
    h = h or max(0.002, min(0.01, gap / 6 or 0.01, copper / 2, below / 6, (above or 1) / 6))
    top = below + copper + (above if above is not None else 4 * (below + gap + width_a + width_b))
    reach = 3 * (below + (above or below)) + 1.0
    y0, y1 = -width_a - reach, gap + width_b + reach
    h = max(h, ((y1 - y0) * top / MAX_NODES) ** 0.5)        # thick boards: a coarser grid
    z1 = below + copper
    cond = {"a": [(-width_a, 0.0, below, z1)], "b": [(gap, gap + width_b, below, z1)]}
    diel = [((y0, y1, 0.0, below), epsilon_r)]
    if above is not None:
        diel.append(((y0, y1, below, top), epsilon_r))
    return mutual(cond, diel, "a", "b", (y0, y1), (0.0, top), h) * 1e12
