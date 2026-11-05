"""Check two boards as they mate: connectors pin to pin, guide pegs, mounting holes.

One board's DRC cannot see that a mezzanine connector carries the wrong net, or that a screw
misses the hole above it. Board B is laid over board A by a transform given in A's frame:
mirrored if ``flip`` (about B's y axis: B then lies with its top towards A), turned by ``rot`` and
moved by ``at``. Everything is compared in A's coordinates, by position, not by pin number,
because that is what the metal does.
"""

from __future__ import annotations

import math
from pathlib import Path

from .. import env, report
from ..netlist import _auto
from .board import Board, Footprint, Pad
from .geometry import rotate

_UNCONNECTED = "unconnected-("


def transform(p: tuple[float, float], at, rot: float, flip: bool) -> tuple[float, float]:
    """A point of board B in board A's frame (mm)."""
    x, y = (-p[0], p[1]) if flip else p
    dx, dy = rotate(x, y, rot)
    return round(at[0] + dx, 6), round(at[1] + dy, 6)


def _where(pad: Pad) -> tuple[float, float]:
    """The point that mates: the hole of a pad with one, else the copper's centre."""
    return (pad.x, pad.y) if pad.drill else pad.center


def _open(net: str) -> bool:
    return not net or net.rsplit("/", 1)[-1].startswith(_UNCONNECTED)


def _short(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def _hole_only(f: Footprint) -> bool:
    """A mounting hole: nothing but holes, plated or not, and no net on them."""
    pads = f.pads()
    return bool(pads) and all(p.drill and _open(p.net) for p in pads)


def _nearest(point, candidates, used: set) -> tuple[int | None, float]:
    best, gap = None, math.inf
    for i, (q, _) in enumerate(candidates):
        d = math.hypot(point[0] - q[0], point[1] - q[1])
        if i not in used and d < gap:
            best, gap = i, d
    return best, gap


def _pair_pads(a: Footprint, b: Footprint, at, rot, flip, tol: float, hole_tol: float, out: dict) -> dict:
    """Mate two footprints: each pad of a with the nearest free pad of b."""
    pads_b = [(transform(_where(p), at, rot, flip), p) for p in b.pads()]
    used: set[int] = set()
    pins = []
    for p in a.pads():
        here = _where(p)
        k, gap = _nearest(here, pads_b, used)
        limit = tol if p.number else hole_tol
        name = f"{a.ref}.{p.number}" if p.number else f"{a.ref} hole"
        if k is None or gap > limit:
            out["misaligned"].append({"a": name, "b": None if k is None else _label(b, pads_b[k][1]),
                                      "at": [round(here[0], 4), round(here[1], 4)],
                                      "nearest": None if k is None else round(gap, 4), "limit": limit})
            continue
        used.add(k)
        q = pads_b[k][1]
        if not p.number or not q.number:
            continue                       # a guide peg in its hole carries no net
        pins.append({"a": f"{a.ref}.{p.number}", "b": f"{b.ref}.{q.number}", "net_a": p.net, "net_b": q.net,
                     "offset": round(gap, 4)})
    for i, (where, q) in enumerate(pads_b):
        if i not in used:
            near, gap = min(((p, math.hypot(where[0] - _where(p)[0], where[1] - _where(p)[1])) for p in a.pads()),
                            key=lambda t: t[1], default=(None, math.inf))
            out["misaligned"].append({"a": None if near is None else _label(a, near), "b": _label(b, q),
                                      "at": [round(where[0], 4), round(where[1], 4)],
                                      "nearest": None if near is None else round(gap, 4),
                                      "limit": tol if q.number else hole_tol})
    return {"a": a.ref, "b": b.ref, "mated": len(pins), "pins": pins}


def _label(f: Footprint, p: Pad) -> str:
    return f"{f.ref}.{p.number}" if p.number else f"{f.ref} hole"


def _check_nets(pins: list[dict], nets: dict[str, str], out: dict) -> None:
    """Names per pin, then the connection as a whole: nets joined through the connector."""
    links: dict[str, set] = {}
    for pin in pins:
        na, nb = pin["net_a"], pin["net_b"]
        open_a, open_b = _open(na), _open(nb)
        if open_a and open_b:
            continue
        if open_a or open_b:
            out["open"].append({k: pin[k] for k in ("a", "b", "net_a", "net_b")})
            continue
        links.setdefault(f"a:{na}", set()).add(f"b:{nb}")
        links.setdefault(f"b:{nb}", set()).add(f"a:{na}")
        wanted = nets.get(na, nets.get(_short(na)))
        if wanted is not None:
            same = nb == wanted or _short(nb) == _short(wanted)
        elif _auto(na) or _auto(nb):
            same = True                    # a generated name says nothing about intent
        else:
            same = _short(na) == _short(nb)
        if not same:
            out["net_mismatch"].append({k: pin[k] for k in ("a", "b", "net_a", "net_b")})
    seen: set[str] = set()
    for start in links:
        if start in seen:
            continue
        group, todo = set(), [start]
        while todo:
            n = todo.pop()
            if n not in group:
                group.add(n)
                todo += links[n]
        seen |= group
        side_a = sorted(n[2:] for n in group if n.startswith("a:"))
        side_b = sorted(n[2:] for n in group if n.startswith("b:"))
        if len(side_a) > 1 or len(side_b) > 1:
            out["shorts"].append({"a": side_a, "b": side_b})


def _check_holes(ba: Board, bb: Board, at, rot, flip, holes, tol: float, out: dict) -> list[dict]:
    """Mounting holes: named pairs, or every hole-only footprint on the part both boards share."""
    matched = []
    if holes == "auto":
        ha = [((f.x, f.y), f) for f in ba.footprints() if _hole_only(f)]
        hb = [(transform((f.x, f.y), at, rot, flip), f) for f in bb.footprints() if _hole_only(f)]
        used: set[int] = set()
        taken = set()
        for k, (where, f) in enumerate(ha):
            i, gap = _nearest(where, hb, used)
            if i is not None and gap <= tol:
                used.add(i)
                taken.add(k)
                matched.append(_hole_pair(f, hb[i][1], gap))
        outline_a, outline_b = ba.outline_loops(), bb.outline_loops()
        from .geometry import inside
        for k, (where, f) in enumerate(ha):
            if k not in taken and (not outline_b or inside([transform(q, at, rot, flip) for q in outline_b[0]], where)):
                out["holes_unmatched"].append({"board": "a", "ref": f.ref, "at": [round(where[0], 4), round(where[1], 4)]})
        for i, (where, f) in enumerate(hb):
            if i not in used and (not outline_a or inside(outline_a[0], where)):
                out["holes_unmatched"].append({"board": "b", "ref": f.ref, "at": [round(where[0], 4), round(where[1], 4)]})
    else:
        for ra, rb in holes or []:
            fa, fb = ba.footprint(ra), bb.footprint(rb)
            here, there = (fa.x, fa.y), transform((fb.x, fb.y), at, rot, flip)
            gap = math.hypot(here[0] - there[0], here[1] - there[1])
            if gap <= tol:
                matched.append(_hole_pair(fa, fb, gap))
            else:
                out["holes_unmatched"].append({"board": "both", "ref": f"{ra}={rb}", "gap": round(gap, 4), "limit": tol,
                                               "at": [round(here[0], 4), round(here[1], 4)]})
    return matched


def _hole_pair(fa: Footprint, fb: Footprint, gap: float) -> dict:
    size = lambda f: max((p.drill for p in f.pads() if p.drill), default=None)  # noqa: E731
    return {"a": fa.ref, "b": fb.ref, "offset": round(gap, 4), "drill_a": size(fa), "drill_b": size(fb)}


def mate(a: Path | str, b: Path | str, pairs: list[tuple[str, str]], at: tuple[float, float] = (0.0, 0.0),
         rot: float = 0.0, flip: bool = False, nets: dict[str, str] | None = None, holes="auto",
         above: bool = True, tolerance: float = 0.05, hole_tolerance: float = 0.1) -> dict:
    """See ``kat.pcb.mate``."""
    report.touch(a)
    ba, bb = Board(a), Board(b)
    if not pairs and holes in (None, "none"):
        raise env.KatError("nothing to check: name connector pairs or holes")
    out: dict = {"misaligned": [], "wrong_side": [], "net_mismatch": [], "shorts": [], "open": [], "holes_unmatched": []}
    facing_a = "F" if above else "B"
    facing_b = ("F" if flip else "B") if above else ("B" if flip else "F")
    done, pins = [], []
    for ra, rb in pairs:
        fa, fb = ba.footprint(ra), bb.footprint(rb)
        if fa.side != facing_a:
            out["wrong_side"].append({"board": "a", "ref": ra, "side": fa.side, "facing": facing_a})
        if fb.side != facing_b:
            out["wrong_side"].append({"board": "b", "ref": rb, "side": fb.side, "facing": facing_b})
        got = _pair_pads(fa, fb, at, rot, flip, tolerance, hole_tolerance, out)
        done.append(got)
        pins += got["pins"]
    _check_nets(pins, dict(nets or {}), out)
    matched = [] if holes in (None, "none") else _check_holes(ba, bb, at, rot, flip, holes, hole_tolerance, out)
    counts = {k: len(v) for k, v in out.items()}
    return env.tidy({"a": str(a), "b": str(b), "transform": {"at": list(at), "rot": rot, "flip": flip, "above": above},
                     "pairs": done, "holes": matched, **out, "counts": counts,
                     "ok": not any(v for k, v in counts.items() if k != "open")})
