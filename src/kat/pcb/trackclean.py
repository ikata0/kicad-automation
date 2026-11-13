"""Post-route clean-up of track geometry (``kat pcb tidy``), and the metrics that show untidy
routing.

Router output is electrically fine but often untidy: right-angle and acute corners, segments
that double back, staircases of short segments, kinks, and long runs at minimum clearance
where there is room. The pass works on **chains**: runs of straight segments of one net, layer
and width between fixed points (pads, vias, junctions of three or more segments, arcs, a change
of width or layer, locked copper). The ends of a chain never move, so connectivity stays.

Steps, in this order, each optional:

1. ``normalize``: drop zero-length segments, merge collinear neighbours, cut out spikes that
   double back, and remove dangling stubs (a chain ending in nothing);
2. ``staircase``: a run of short segments becomes one straight or one 45-degree-and-straight
   path;
3. ``tight``: pull the chain tight with straight or 45-degree-and-straight shortcuts, each
   kept only when it is clear and nothing lies between it and the old path (the same homotopy
   class);
4. ``spread``: move a segment that runs closer to other copper than ``spread`` mm sideways
   towards that gap where there is room, letting the net grow by at most ``max_growth``;
5. ``corners``: a right-angle or acute corner becomes two 45-degree bends (``"45"``) or an arc
   (``"arc"``).

Every new segment keeps its clearance to other nets' copper (exact pads, tracks, vias;
effective clearances) and stays out of track keep-outs and off the board edge. Steps 1-4
never make a net longer. ``kat.pcb.tidy_tracks`` then runs KiCad's DRC and puts back every
net whose change added a violation or an unconnected item. The pass is deterministic and
repeats until nothing changes, so running it again changes nothing.
"""

from __future__ import annotations

import fnmatch
import math
from collections import defaultdict

from ..schematic import new_uuid
from ..sexpr import Sym, find, num
from .geometry import inside, point_seg

STEPS = ("normalize", "staircase", "tight", "spread", "corners")
EPS = 1e-4                  # mm: points this close are one
SHORT = 0.6                 # mm: a segment shorter than this (or 3 widths) counts as short


def _key(p) -> tuple[int, int]:
    return (round(p[0] / EPS), round(p[1] / EPS))


def _length(pts) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def _turn(a, b, c) -> float:
    """The change of direction at ``b`` in degrees (0: straight on, 90: a right angle, 180:
    doubling back)."""
    u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    nu, nv = math.hypot(*u), math.hypot(*v)
    if nu < EPS or nv < EPS:
        return 0.0
    cos = max(-1.0, min(1.0, (u[0] * v[0] + u[1] * v[1]) / (nu * nv)))
    return math.degrees(math.acos(cos))


def _octilinear(a, b) -> bool:
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return dx < EPS or dy < EPS or abs(dx - dy) < EPS


class Chain:
    """Points of one run of segments (net, layer, width) and the nodes it came from."""

    def __init__(self, net: str, layer: str, width: float, pts: list, nodes: list, dangling: bool):
        self.net, self.layer, self.width = net, layer, width
        self.pts, self.nodes, self.dangling = pts, nodes, dangling
        self.arcs: list[tuple] = []          # (start, mid, end) of corners made round

    @property
    def length(self) -> float:
        return _length(self.pts)


def chains(b, nets=None, layers=None, region=None, include_locked: bool = False,
           exclude_uuids: set[str] | frozenset[str] = frozenset()) -> list[Chain]:
    """The board's straight track runs between fixed points, as ``Chain`` objects.
    Excluded segment UUIDs stay fixed and terminate adjacent runs."""
    segs = [t for t in b.tracks() if t["kind"] == "segment"]
    fixed = set()
    for p in b.pads():
        fixed.add(_key((p.x, p.y)))
    for v in b.vias():
        fixed.add(_key(v["at"]))
    for t in b.tracks():
        if t["kind"] == "arc":
            fixed |= {_key(t["start"]), _key(t["end"])}
    pad_polys = defaultdict(list)                    # (net, layer) -> pad outlines, for ends on pads
    for p in b.pads():
        for lay in b.copper_layers():
            if p.on(lay):
                pad_polys[(p.net, lay)].append(p.outline)

    def wanted(t) -> bool:
        if t["uuid"] in exclude_uuids:
            return False
        if t["locked"] and not include_locked:
            return False
        if nets and not any(fnmatch.fnmatch(t["net"], g) for g in nets):
            return False
        if layers and t["layer"] not in layers:
            return False
        if region:
            x0, y0, x1, y1 = region
            return all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in (t["start"], t["end"]))
        return True

    groups = defaultdict(list)
    for t in segs:
        groups[(t["net"], t["layer"])].append(t)
    out = []
    for (net, layer), ts in sorted(groups.items()):
        deg = defaultdict(int)
        for t in ts:
            deg[_key(t["start"])] += 1
            deg[_key(t["end"])] += 1
        free = [t for t in ts if wanted(t)]
        blocked = {_key(p) for t in ts if not wanted(t) for p in (t["start"], t["end"])}
        widths = defaultdict(set)
        for t in ts:
            widths[_key(t["start"])].add(t["width"])
            widths[_key(t["end"])].add(t["width"])

        def is_fixed(k) -> bool:
            return k in fixed or k in blocked or deg[k] != 2 or len(widths[k]) > 1
        at = defaultdict(list)
        for t in free:
            at[_key(t["start"])].append(t)
            at[_key(t["end"])].append(t)
        used = set()
        for t in sorted(free, key=lambda t: (t["start"], t["end"])):
            if id(t) in used:
                continue
            # walk to one end of the run, then collect it in order
            cur, k = t, _key(t["start"])
            seen = {id(t)}
            while not is_fixed(k):
                nxt = next((s for s in at[k] if id(s) != id(cur) and s["width"] == t["width"]), None)
                if nxt is None or id(nxt) in seen:
                    break
                seen.add(id(nxt))
                cur = nxt
                k = _key(nxt["end"]) if _key(nxt["start"]) == k else _key(nxt["start"])
            start_k = k
            pts, nodes, k, seg = [], [], start_k, cur
            first = seg["start"] if _key(seg["start"]) == k else seg["end"]
            pts.append(tuple(first))
            while True:
                used.add(id(seg))
                nodes.append(seg["node"])
                other = seg["end"] if _key(seg["start"]) == k else seg["start"]
                pts.append(tuple(other))
                k = _key(other)
                if is_fixed(k):
                    break
                seg = next((s for s in at[k] if id(s) not in used and s["width"] == t["width"]), None)
                if seg is None:
                    break
            ends = [_key(pts[0]), _key(pts[-1])]
            # an end that reaches nothing: degree 1, no pad, via or arc there
            dangling = any(deg[e] == 1 and e not in fixed and not any(
                inside(poly, pts[0] if e == ends[0] else pts[-1]) for poly in pad_polys[(net, layer)])
                for e in ends)
            out.append(Chain(net, layer, t["width"], pts, nodes, dangling))
    return out


# --- the steps ----------------------------------------------------------------------------

def normalize(pts: list) -> list:
    """Zero-length segments dropped, collinear points merged, spikes that double back cut."""
    out = []
    for p in pts:
        if out and math.dist(out[-1], p) < EPS:
            continue
        out.append(p)
    changed = True
    while changed and len(out) > 2:
        changed = False
        for i in range(1, len(out) - 1):
            t = _turn(out[i - 1], out[i], out[i + 1])
            if t < 0.01 or t > 179.99:
                del out[i]
                changed = True
                break
    return out


def destaircase(pts: list, width: float, ok) -> list:
    """Runs of three or more short segments become one straight segment, or a 45-degree leg and
    a straight one, when ``ok(a, b, path)`` accepts the new path."""
    short = max(SHORT, 3 * width)
    i = 0
    out = list(pts)
    while i < len(out) - 3:
        j = i
        while j + 1 < len(out) and math.dist(out[j], out[j + 1]) <= short:
            j += 1
        if j - i >= 3:
            for path in _replacements(out[i], out[j]):
                if ok(out[i:j + 1], path) and _length(path) <= _length(out[i:j + 1]) + EPS:
                    out[i:j + 1] = path
                    break
        i += 1
    return out


def _replacements(a, b) -> list[list]:
    """Straight, then the two 45-degree-and-straight paths from ``a`` to ``b``."""
    if _octilinear(a, b):
        return [[a, b]]
    dx, dy = b[0] - a[0], b[1] - a[1]
    d = min(abs(dx), abs(dy))
    sx, sy = math.copysign(1, dx), math.copysign(1, dy)
    diag_first = (a[0] + sx * d, a[1] + sy * d)
    straight_first = (b[0] - sx * d, b[1] - sy * d)
    return [[a, b], [a, diag_first, b], [a, straight_first, b]]


def pull_tight(pts: list, ok, between_free) -> list:
    """From each point the farthest later point a shortcut reaches: straight when that is a
    multiple of 45 degrees, else a 45-degree leg and a straight one, kept only when it is clear
    and nothing lies between it and the old path (the same homotopy class)."""
    out = [pts[0]]
    i = 0
    while i < len(pts) - 1:
        j, path = len(pts) - 1, None
        while j > i + 1:
            old = pts[i:j + 1]
            for cand in _replacements(pts[i], pts[j])[1:] if not _octilinear(pts[i], pts[j]) else [[pts[i], pts[j]]]:
                if (_length(cand) < _length(old) - EPS and ok(old, cand)
                        and between_free(old + list(reversed(cand[1:-1])))):
                    path = cand
                    break
            if path:
                break
            j -= 1
        out += path[1:] if path else [pts[i + 1]]
        i = j if path else i + 1
    return out


def chamfer(pts: list, width: float, ok, mode: str = "45", arcs: list | None = None) -> list:
    """Right-angle and acute corners cut: two 45-degree bends (or an arc, ``mode`` "arc",
    whose (start, mid, end) go to ``arcs``)."""
    out = list(pts)
    i = 1
    while i < len(out) - 1:
        a, c, b = out[i - 1], out[i], out[i + 1]
        if _turn(a, c, b) < 90 - 0.01:
            i += 1
            continue
        la, lb = math.dist(a, c), math.dist(c, b)
        cut = min(la, lb) * 0.45
        cut = min(cut, max(1.0, 2 * width))
        if cut < width:
            i += 1
            continue
        p = (c[0] + (a[0] - c[0]) * cut / la, c[1] + (a[1] - c[1]) * cut / la)
        q = (c[0] + (b[0] - c[0]) * cut / lb, c[1] + (b[1] - c[1]) * cut / lb)
        p, q = (round(p[0], 4), round(p[1], 4)), (round(q[0], 4), round(q[1], 4))
        if ok([a, c, b], [a, p, q, b]):
            out[i:i + 1] = [p, q]
            if mode == "arc" and arcs is not None:
                # the arc through the corner's chamfer, bowed towards the corner
                m = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
                bow = 0.29 * math.dist(p, q)
                mx, my = c[0] - m[0], c[1] - m[1]
                n = math.hypot(mx, my) or 1.0
                arcs.append((p, (round(m[0] + mx / n * bow, 4), round(m[1] + my / n * bow, 4)), q))
            i += 2
        else:
            i += 1
    return out


def spread(pts: list, target: float, nearest, ok) -> list:
    """Inner segments closer than ``target`` mm to other copper moved sideways, away from it,
    by the whole shortfall, where ``ok`` accepts the result and the segments it touches stay
    at multiples of 45 degrees."""
    out = list(pts)
    for i in range(1, len(out) - 2):
        a, b = out[i], out[i + 1]
        gap, away = nearest(a, b)
        if gap is None or gap >= target - 1e-3:
            continue
        need = target - gap
        na = (round(a[0] + away[0] * need, 4), round(a[1] + away[1] * need, 4))
        nb = (round(b[0] + away[0] * need, 4), round(b[1] + away[1] * need, 4))
        cand = out[:i] + [na, nb] + out[i + 2:]
        window = cand[i - 1:i + 3]
        if all(_octilinear(p, q) for p, q in zip(window, window[1:])) and ok(out[i - 1:i + 3], window):
            out = cand
    return out


# --- metrics ------------------------------------------------------------------------------

def metrics(b, cs: list[Chain] | None = None, gaps: bool = False) -> dict:
    """Per layer: ``corners`` (turns: 45, 90, acute, other), ``short`` segments (under
    0.6 mm or three widths, inside a chain), ``reversals`` (turns over 135 degrees), and with
    ``gaps`` a histogram of each segment's distance to other nets' copper (mm bins)."""
    cs = cs if cs is not None else chains(b, include_locked=True)
    out: dict = {}
    near = _Near(b) if gaps else None
    for c in cs:
        m = out.setdefault(c.layer, {"corners": {"45": 0, "90": 0, "acute": 0, "other": 0},
                                     "short": 0, "reversals": 0, **({"gaps": {}} if gaps else {})})
        for a, p, q in zip(c.pts, c.pts[1:], c.pts[2:]):
            t = _turn(a, p, q)
            k = "45" if abs(t - 45) < 1 else "90" if abs(t - 90) < 1 else "acute" if t > 91 else "other"
            m["corners"][k] += 1
            if t > 135:
                m["reversals"] += 1
        short = max(SHORT, 3 * c.width)
        m["short"] += sum(math.dist(a, p) < short for a, p in zip(c.pts[1:-1], c.pts[2:-1]))
        if gaps:
            for a, p in zip(c.pts, c.pts[1:]):
                g, _ = near.nearest(c, a, p)
                if g is not None:
                    k = f"<{_bin(g)}"
                    m["gaps"][k] = m["gaps"].get(k, 0) + 1
    return out


def _bin(g: float) -> str:
    for edge in (0.1, 0.15, 0.2, 0.3, 0.5, 1.0):
        if g < edge:
            return f"{edge:g}"
    return "inf"


class _Near:
    """Other nets' copper near a segment, for ``spread`` and the gap histogram."""

    def __init__(self, b):
        from .structure import _Copper
        self.cu = _Copper(b)

    def nearest(self, c: Chain, a, p) -> tuple[float | None, tuple[float, float]]:
        """Edge gap from segment a-p (width of ``c``) to the nearest other-net copper on its
        layer within 2 mm, and the unit normal pointing away from it."""
        h = c.width / 2
        box = (min(a[0], p[0]) - h, min(a[1], p[1]) - h, max(a[0], p[0]) + h, max(a[1], p[1]) + h)
        best, where = None, None
        for it in self.cu.index.query(box, margin=2.0):
            if it["net"] == c.net or c.layer not in it["layers"]:
                continue
            if it["kind"] == "pad":
                q = min(it["poly"], key=lambda v: point_seg(v, a, p))
                d = point_seg(q, a, p) - h
            elif it["kind"] == "track":
                s, e = it["seg"]
                q = min((s, e, ((s[0] + e[0]) / 2, (s[1] + e[1]) / 2)), key=lambda v: point_seg(v, a, p))
                d = point_seg(q, a, p) - h - it["half"]
            else:
                q = it["at"]
                d = point_seg(q, a, p) - h - it["r"]
            if best is None or d < best:
                best, where = d, q
        if best is None:
            return None, (0.0, 0.0)
        dx, dy = p[0] - a[0], p[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n, dx / n
        mx, my = (a[0] + p[0]) / 2, (a[1] + p[1]) / 2
        if (where[0] - mx) * nx + (where[1] - my) * ny > 0:
            nx, ny = -nx, -ny
        return round(best, 4), (nx, ny)


# --- the pass on a board ------------------------------------------------------------------

def net_lengths(b) -> dict[str, float]:
    """Track length per net (arcs by their chord through the mid point), mm."""
    out: dict[str, float] = defaultdict(float)
    for tr in b.tracks():
        n = tr["node"]
        if tr["kind"] == "arc":
            m = find(n, "mid")
            mid = (float(m[1]), float(m[2]))
            out[tr["net"]] += math.dist(tr["start"], mid) + math.dist(mid, tr["end"])
        else:
            out[tr["net"]] += math.dist(tr["start"], tr["end"])
    return dict(out)


def tidy_board(b, nets=None, layers=None, region=None, steps=STEPS, corners: str = "45",
               spread_gap: float | None = None, max_growth: float = 10.0, freeze_length=(),
               include_locked: bool = False) -> dict:
    """The clean-up on ``b`` in memory, in place (``kat.pcb.tidy_tracks`` saves it and judges
    it by DRC), repeated until a pass changes nothing (at most 4). Returns ``steps`` (changes
    per step), ``nets`` (changed) and ``length`` {net: [before, after]} (mm)."""
    if corners not in ("45", "arc", "keep"):
        from .. import env
        raise env.KatError(f"corners {corners!r}: 45, arc or keep")
    bad = [s for s in steps if s not in STEPS]
    if bad:
        from .. import env
        raise env.KatError(f"steps {', '.join(bad)}: some of {', '.join(STEPS)}")
    before = net_lengths(b)
    counts = {s: 0 for s in steps}
    touched: set[str] = set()
    for _ in range(4):
        got = _one_pass(b, nets, layers, region, steps, corners, spread_gap, max_growth,
                        set(freeze_length), include_locked, before)
        for k, v in got["steps"].items():
            counts[k] += v
        if not got["nets"]:
            break
        touched |= got["nets"]
    after = net_lengths(b)
    return {"steps": counts, "nets": sorted(touched),
            "length": {n: [round(before.get(n, 0.0), 4), round(after.get(n, 0.0), 4)] for n in sorted(touched)}}


def _one_pass(b, nets, layers, region, steps, corners, spread_gap, max_growth, freeze, include_locked,
              start_lengths) -> dict:
    from .structure import _Copper
    cu, cl = _Copper(b), b._clearance_fn()
    keepouts = _track_keepouts(b)
    loops = b.outline_loops()
    edge = loops[0] if loops else None
    edge_gap = (b.board_minimums().get("min_copper_edge_clearance") or 0.0)
    near = _Near(b)
    near.cu = cu
    counts = {s: 0 for s in steps}
    touched: set[str] = set()
    grown: dict[str, float] = defaultdict(float)
    # what a shortcut may not jump over: pads and vias of any net, other nets' track ends
    obstacles = defaultdict(list)
    for p in b.pads():
        for lay in b.copper_layers():
            if p.on(lay):
                obstacles[lay].append((p.net, (p.x, p.y)))
    for v in b.vias():
        for lay in v["span"]:
            obstacles[lay].append((v["net"], tuple(v["at"])))
    for tr in b.tracks():
        obstacles[tr["layer"]] += [(tr["net"], tuple(tr["start"])), (tr["net"], tuple(tr["end"]))]

    for c in chains(b, nets, layers, region, include_locked):
        before = c.length

        def ok(old_pts, new_pts, c=c) -> bool:
            if spread_gap and "spread" in steps:      # nothing else may undo what spreading gains
                was = [g for a, p in zip(old_pts, old_pts[1:]) if (g := near.nearest(c, a, p)[0]) is not None]
                floor = min([spread_gap, *was]) - 1e-3
            for a, p in zip(new_pts, new_pts[1:]):
                if math.dist(a, p) < EPS:
                    continue
                if cu.conflicts(c.net, {c.layer}, cl, seg=((a, p), c.width / 2)):
                    return False
                if spread_gap and "spread" in steps and (g := near.nearest(c, a, p)[0]) is not None and g < floor:
                    return False
                if any(_seg_in_poly(a, p, k) for lay, k in keepouts if lay == c.layer):
                    return False
                if edge and not (inside(edge, a) and inside(edge, p)):
                    return False
                if edge and edge_gap and min(point_seg(q, a, p) for q in edge) < edge_gap + c.width / 2:
                    return False
            return True

        def between_free(path, c=c) -> bool:
            poly = list(path)
            on_path = {_key(x) for x in path}
            return not any(inside(poly, q) for _, q in obstacles[c.layer] if _key(q) not in on_path)

        if c.dangling and "normalize" in steps:
            _replace(b, c, [], [])
            counts["normalize"] += 1
            touched.add(c.net)
            continue
        pts = list(c.pts)
        if "normalize" in steps:
            new = normalize(pts)
            counts["normalize"] += new != pts
            pts = new
        if "staircase" in steps and len(pts) > 3:
            new = normalize(destaircase(pts, c.width, ok))
            counts["staircase"] += new != pts
            pts = new
        if "tight" in steps and len(pts) > 2:
            new = normalize(pull_tight(pts, ok, between_free))
            counts["tight"] += new != pts
            pts = new
        if ("spread" in steps and spread_gap and c.net not in freeze and len(pts) > 3):
            new = spread(pts, spread_gap, lambda a, p, c=c: near.nearest(c, a, p), ok)
            allowed = start_lengths.get(c.net, 0.0) * max_growth / 100 - grown[c.net]
            if new != pts and _length(new) - _length(pts) <= allowed + EPS:
                counts["spread"] += 1
                grown[c.net] += max(0.0, _length(new) - _length(pts))
                pts = new
        arcs: list = []
        if "corners" in steps and corners != "keep" and len(pts) > 2:
            new = chamfer(pts, c.width, ok, corners, arcs)
            counts["corners"] += new != pts
            pts = new
        if pts == c.pts:
            continue
        if _length(pts) > before + EPS and (c.net in freeze or not grown[c.net]):
            continue                                   # steps 1-4 never lengthen a net
        _replace(b, c, pts, arcs)
        for a, p in zip(pts, pts[1:]):                 # later chains see the new copper
            if math.dist(a, p) >= EPS:
                cu.add_track(a, p, c.width, c.layer, c.net)
        touched.add(c.net)
    return {"steps": counts, "nets": touched}


def _replace(b, c: Chain, pts: list, arcs: list) -> None:
    """Chain ``c``'s segments out of the board, the path ``pts`` in (``arcs`` round corners)."""
    gone = {id(n) for n in c.nodes}
    lock = any(find(n, "locked") is not None for n in c.nodes)
    b.tree[:] = [n for n in b.tree if id(n) not in gone]
    for a, p in zip(pts, pts[1:]):
        if math.dist(a, p) < EPS:
            continue
        arc = next((x for x in arcs if (x[0], x[2]) == (a, p)), None)
        b._insert(_track_node(a, p, c.width, c.layer, c.net, lock, arc[1] if arc else None))


def _track_node(a, b, width, layer, net, lock, mid=None) -> list:
    node = [Sym("arc" if mid else "segment"), [Sym("start"), num(a[0]), num(a[1])],
            *([[Sym("mid"), num(mid[0]), num(mid[1])]] if mid else []),
            [Sym("end"), num(b[0]), num(b[1])], [Sym("width"), num(width)]]
    if lock:
        node.append([Sym("locked"), Sym("yes")])
    node += [[Sym("layer"), layer], [Sym("net"), net], [Sym("uuid"), new_uuid()]]
    return node


def _track_keepouts(b) -> list[tuple[str, list]]:
    """(layer, polygon) of the rule areas that forbid tracks."""
    from .coupling import _pts
    out = []
    for z in b.zones():
        k = find(z, "keepout")
        if k is None or not any(isinstance(c, list) and str(c[0]) == "tracks" and len(c) > 1
                                and str(c[1]) == "not_allowed" for c in k[1:]):
            continue
        lay = find(z, "layers") or find(z, "layer")
        poly = _pts(find(z, "polygon"))
        for l in lay[1:]:
            names = b.copper_layers() if str(l) in ("*.Cu", "F&B.Cu") else [str(l)]
            out += [(n, poly) for n in names]
    return out


def _seg_in_poly(a, b, poly) -> bool:
    from .geometry import segs_cross
    if inside(poly, a) or inside(poly, b):
        return True
    return any(segs_cross(a, b, p, q) for p, q in zip(poly, poly[1:] + poly[:1]))


def by_net(violations: list[dict], nets: set[str]) -> dict[str, int]:
    """How many DRC items name each of ``nets`` (``[NET]`` in an item's description)."""
    out: dict[str, int] = defaultdict(int)
    for v in violations:
        for i in v["items"]:
            for n in nets:
                if f"[{n}]" in i["description"]:
                    out[n] += 1
    return out


__all__ = ["STEPS", "chains", "metrics", "net_lengths", "tidy_board"]
