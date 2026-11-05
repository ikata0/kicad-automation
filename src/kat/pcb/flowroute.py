"""Escape routing with free pin assignment: which pin goes where is decided with the tracks.

Freerouting takes each net as given. Where pins of a group may be swapped, as on a connector, the
best assignment is the one whose tracks do not cross, and that is a flow problem. Tracks are
vertex-disjoint paths on a grid whose pitch keeps neighbours legal; a min-cost flow from the source
pins to the destination pins gives the most paths possible on that grid, at the least total length.
On one layer they cannot cross, so no vias are needed. A pin's net then has to change in the design
(``kat back-annotate``), since connectivity belongs to it; ``apply`` adds the tracks after that.

Only permutable groups are handled: pairs that are fixed go to the ordinary router. Tracks follow the
grid at right angles. KiCad's DRC judges the result.
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path

from .. import env, report
from .board import Board, Pad
from .geometry import inside, point_seg

_MAX_NODES = 4_000_000
_VIA_COST = 2.0


def _expand(b: Board, names) -> list[str]:
    """Pad names, with ``J1.*`` standing for all numbered pads of a part, in natural order."""
    from ..syncer import natural_key
    out: list[str] = []
    for name in names or []:
        name = str(name)
        if name.endswith(".*"):
            ref = name[:-2]
            numbers = sorted({p.number for p in b.footprint(ref).pads() if p.number}, key=natural_key)
            out += [f"{ref}.{n}" for n in numbers]
        else:
            out.append(name)
    return list(dict.fromkeys(out))


def _pad_of(b: Board, name: str) -> Pad:
    ref, dot, number = str(name).partition(".")
    if not dot:
        raise env.KatError(f"{name!r} is not a pad (REF.NUMBER)")
    found = [p for p in b.footprint(ref).pads() if p.number == number]
    if not found:
        raise env.KatError(f"{ref} has no pad {number!r}")
    return found[0]


class _Flow:
    """Min-cost max-flow by successive shortest paths, with potentials. Integer capacities."""

    def __init__(self, n: int):
        self.head = [-1] * n
        self.to: list[int] = []
        self.cap: list[int] = []
        self.cost: list[float] = []
        self.next: list[int] = []

    def add(self, u: int, v: int, cap: int, cost: float) -> None:
        for a, b, c, w in ((u, v, cap, cost), (v, u, 0, -cost)):
            self.to.append(b)
            self.cap.append(c)
            self.cost.append(w)
            self.next.append(self.head[a])
            self.head[a] = len(self.to) - 1

    def run(self, s: int, t: int) -> int:
        n = len(self.head)
        pot = [0.0] * n
        flow = 0
        while True:
            dist = [math.inf] * n
            came = [-1] * n
            dist[s] = 0.0
            heap = [(0.0, s)]
            while heap:
                d, u = heapq.heappop(heap)
                if d > dist[u]:
                    continue
                e = self.head[u]
                while e != -1:
                    if self.cap[e] > 0:
                        v = self.to[e]
                        nd = d + self.cost[e] + pot[u] - pot[v]
                        if nd < dist[v] - 1e-9:
                            dist[v], came[v] = nd, e
                            heapq.heappush(heap, (nd, v))
                    e = self.next[e]
            if dist[t] == math.inf:
                return flow
            for v in range(n):
                if dist[v] < math.inf:
                    pot[v] += dist[v]
            v = t
            while v != s:
                e = came[v]
                self.cap[e] -= 1
                self.cap[e ^ 1] += 1
                v = self.to[e ^ 1]
            flow += 1


def _read(spec: dict | Path | str) -> dict:
    if isinstance(spec, dict):
        return dict(spec)
    path = Path(spec)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise env.KatError(f"{path.name} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise env.KatError(f"{path.name}: expected an object")
    return data


def _box_of_outline(points) -> tuple[float, float, float, float]:
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def plan(b: Board, spec: dict) -> dict:
    """The flow itself: see ``kat.pcb.escape``."""
    source_names, target_names = _expand(b, spec.get("sources")), _expand(b, spec.get("targets"))
    if not source_names or not target_names:
        raise env.KatError("escape needs sources and targets: lists of pads such as J1.3 or J1.*")
    both = sorted(set(source_names) & set(target_names))
    if both:
        raise env.KatError(f"pads on both sides: {', '.join(both)}")
    sources = [_pad_of(b, s) for s in source_names]
    targets = [_pad_of(b, s) for s in target_names]
    names = {id(p): s for p, s in zip(sources + targets, source_names + target_names)}
    copper = b.copper_layers()
    layers = list(spec.get("layers") or ["F.Cu"])
    bad = sorted(set(layers) - set(copper))
    if bad:
        raise env.KatError(f"no layer {', '.join(bad)}: {', '.join(copper)}")
    vias = bool(spec.get("vias", False))
    if vias and len(layers) != 2:
        raise env.KatError("vias need exactly two layers: a through via blocks every layer for other paths")
    default = b.net_rules("")
    track = float(spec.get("track", default["track"]))
    clearance = float(spec.get("clearance", default["clearance"]))
    via, drill = float(spec.get("via", default["via"])), float(spec.get("via_drill", default["via_drill"]))
    pitch = track + clearance
    if vias:
        hole = b.board_minimums().get("min_hole_to_hole", 0.25)
        pitch = max(pitch, via / 2 + track / 2 + clearance, via + clearance, drill + hole)
    grid = float(spec.get("grid", pitch))
    if grid < pitch - 1e-9:
        raise env.KatError(f"grid {grid} mm is below {pitch:.3f} mm: neighbouring tracks would break the clearance")
    margin = track / 2 + clearance
    via_margin = via / 2 + clearance
    loops = b.outline_loops()
    if not loops:
        raise env.KatError("the board has no closed outline")
    edge_poly, cutouts = loops[0], loops[1:]
    # the search area: the pins and a margin, unless given
    pts = [(p.x, p.y) for p in sources + targets]
    region = spec.get("region")
    if region is not None:
        x0, y0, x1, y1 = (float(v) for v in region)
    else:
        room = float(spec.get("margin", 10.0))
        bx = _box_of_outline(pts)
        x0, y0, x1, y1 = bx[0] - room, bx[1] - room, bx[2] + room, bx[3] + room
    ob = _box_of_outline(edge_poly)
    x0, y0, x1, y1 = max(x0, ob[0]), max(y0, ob[1]), min(x1, ob[2]), min(y1, ob[3])
    nx, ny = int(math.floor((x1 - x0) / grid)) + 1, int(math.floor((y1 - y0) / grid)) + 1
    if nx * ny * len(layers) * 2 > _MAX_NODES:
        raise env.KatError(f"{nx} x {ny} grid points on {len(layers)} layers: narrow the region or use a coarser grid")
    size = nx * ny
    xs = [x0 + i * grid for i in range(nx)]
    ys = [y0 + j * grid for j in range(ny)]
    edge_cl = b.board_minimums().get("min_copper_edge_clearance", 0.5) or 0.0
    need = edge_cl + track / 2
    free_area = bytearray(size)
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            q = (x, y)
            if not inside(edge_poly, q) or any(point_seg(q, a, c) < need for a, c in zip(edge_poly, edge_poly[1:] + edge_poly[:1])):
                continue
            if any(inside(c, q) or any(point_seg(q, a, d) < need for a, d in zip(c, c[1:] + c[:1])) for c in cutouts):
                continue
            free_area[j * nx + i] = 1
    # owner per layer: 0 free, -1 blocked, k > 0 inside pin k only (its own copper)
    owner = {l: [0 if free_area[k] else -1 for k in range(size)] for l in layers}
    via_free = bytearray(free_area)
    pins = sources + targets
    pin_no = {(p.x, p.y, p.number): n + 1 for n, p in enumerate(pins)}
    off = [names[id(p)] for p in pins if not any(p.on(l) for l in layers)]
    if off:
        raise env.KatError(f"not on {', '.join(layers)}: {', '.join(off)}")

    def rasterise(poly, on: list[str], m_track: float, m_via: float, own: int = 0) -> None:
        box = _box_of_outline(poly)
        reach = max(m_track, m_via)
        i0, i1 = max(0, int((box[0] - reach - x0) / grid) - 1), min(nx - 1, int((box[2] + reach - x0) / grid) + 1)
        j0, j1 = max(0, int((box[1] - reach - y0) / grid) - 1), min(ny - 1, int((box[3] + reach - y0) / grid) + 1)
        closed = list(zip(poly, poly[1:] + poly[:1])) if len(poly) > 1 else [(poly[0], poly[0])]
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                q, k = (xs[i], ys[j]), j * nx + i
                d = 0.0 if len(poly) > 2 and inside(poly, q) else min(point_seg(q, a, c) for a, c in closed)
                if d < m_via - 1e-9:
                    via_free[k] = 0
                if d < m_track - 1e-9:
                    for l in on:
                        have = owner[l][k]
                        if have == -1:
                            continue
                        owner[l][k] = own if own and have in (0, own) else -1

    for p in b.pads():
        own = pin_no.get((p.x, p.y, p.number), 0)
        on = [l for l in layers if p.on(l)]
        drilled = p.hole_outline
        for poly in p.polygons(0.01):
            rasterise(poly, on, margin, via_margin, own)
        if drilled:           # no copper on a layer, a hole on all
            hole_cl = b.board_minimums().get("min_hole_clearance", 0.25)
            rasterise(drilled, [l for l in layers if l not in on], track / 2 + hole_cl, via_margin, 0)
    for t in b.tracks():
        if t["layer"] in layers:
            rasterise([t["start"], t["end"]], [t["layer"]], t["width"] / 2 + margin, t["width"] / 2 + via_margin)
        else:
            rasterise([t["start"], t["end"]], [], 0.0, t["width"] / 2 + via_margin)
    for v in b.vias():
        rasterise([v["at"]], [l for l in layers if l in v["span"]], v["size"] / 2 + margin, v["size"] / 2 + via_margin)
    from ..sexpr import find
    from .specctra import _zone_layers, _zone_polygon
    for z in b.zones():
        keep = find(z, "keepout")
        if keep is None:
            continue
        rule = {str(c[0]): str(c[1]) for c in keep[1:] if isinstance(c, list)}
        poly = _zone_polygon(z)
        if rule.get("tracks") == "not_allowed":
            rasterise(poly, [l for l in _zone_layers(z) if l in layers], margin, 0.0)
        if rule.get("vias") == "not_allowed":
            rasterise(poly, [], 0.0, via_margin)
    # the graph: in and out of each grid point on each layer, capacity 1
    n_layers = len(layers)
    count = 2 * size * n_layers + 2 + len(pins)
    s_node, t_node = count - 2, count - 1
    flow = _Flow(count)
    terminal = {n + 1: 2 * size * n_layers + n for n in range(len(pins))}
    n_src = len(sources)

    def node_in(li: int, k: int) -> int:
        return 2 * (li * size + k)

    for li, l in enumerate(layers):
        row = owner[l]
        for k in range(size):
            if row[k] == -1:
                continue
            u = node_in(li, k)
            flow.add(u, u + 1, 1, 0.0)
            j, i = divmod(k, nx)
            for nb, ok in ((k - 1, i > 0), (k + 1, i < nx - 1), (k - nx, j > 0), (k + nx, j < ny - 1)):
                if not ok or row[nb] == -1:
                    continue
                a, c = row[k], row[nb]
                # into a source pin's copper only from itself, out of a target pin's only to itself
                if 0 < c <= n_src and a != c:
                    continue
                if a > n_src and a != c:
                    continue
                flow.add(u + 1, node_in(li, nb), 1, grid)
            if vias and via_free[k] and row[k] == 0:
                other = 1 - li
                if owner[layers[other]][k] == 0:
                    flow.add(u + 1, node_in(other, k), 1, _VIA_COST)
            if row[k] > 0:
                pin = row[k]
                if pin <= n_src:
                    flow.add(terminal[pin], u, 1, math.hypot(xs[i] - pins[pin - 1].x, ys[j] - pins[pin - 1].y))
                else:
                    flow.add(u + 1, terminal[pin], 1, math.hypot(xs[i] - pins[pin - 1].x, ys[j] - pins[pin - 1].y))
    for n, p in enumerate(pins, 1):
        if n <= n_src:
            flow.add(s_node, terminal[n], 1, 0.0)
        else:
            flow.add(terminal[n], t_node, 1, 0.0)
    done = flow.run(s_node, t_node)
    # read the paths back
    routes = []

    def coord(node: int) -> tuple[int, int]:
        li, k = divmod(node // 2, size)
        return li, k

    for n in range(1, n_src + 1):
        e = flow.head[terminal[n]]
        start = None
        while e != -1:
            if e % 2 == 0 and flow.cap[e] == 0 and flow.to[e] < 2 * size * n_layers:
                start = flow.to[e]
                break
            e = flow.next[e]
        if start is None:
            continue
        chain = []
        node = start
        end = None
        while True:
            chain.append(coord(node))
            out = node + 1
            e = flow.head[out]
            nxt = None
            while e != -1:
                if e % 2 == 0 and flow.cap[e] == 0:
                    nxt = flow.to[e]
                    break
                e = flow.next[e]
            if nxt is None:
                break
            if nxt >= 2 * size * n_layers:
                end = next(p for p, q in terminal.items() if q == nxt)
                break
            node = nxt
        src, dst = pins[n - 1], pins[end - 1]
        segs, via_list = [], []
        prev = (src.x, src.y)
        li_prev = chain[0][0]
        for li, k in chain:
            j, i = divmod(k, nx)
            q = (round(xs[i], 6), round(ys[j], 6))
            if li != li_prev:
                via_list.append(q)
                li_prev = li
                prev = q
                continue
            if q != prev:
                segs.append((prev, q, layers[li]))
            prev = q
        segs.append((prev, (dst.x, dst.y), layers[chain[-1][0]]))
        merged = []
        for a, c, l in segs:
            if merged and merged[-1][2] == l and _straight(merged[-1][0], merged[-1][1], c):
                merged[-1] = (merged[-1][0], c, l)
            else:
                merged.append((a, c, l))
        length = sum(math.hypot(c[0] - a[0], c[1] - a[1]) for a, c, _ in merged)
        routes.append({"source": names[id(src)], "target": names[id(dst)], "net": dst.net,
                       "tracks": [{"start": list(a), "end": list(c), "layer": l} for a, c, l in merged],
                       "vias": [list(v) for v in via_list], "length": round(length, 3)})
    routed_s = {r["source"] for r in routes}
    routed_t = {r["target"] for r in routes}
    return {"routes": routes, "assignment": {r["source"]: r["net"] or r["target"] for r in routes},
            "unrouted_sources": [s for s in source_names if s not in routed_s],
            "unrouted_targets": [s for s in target_names if s not in routed_t],
            "max_paths": done, "grid": grid, "track": track, "clearance": clearance, "via": via, "via_drill": drill,
            "layers": layers, "vias_allowed": vias, "total_length": round(sum(r["length"] for r in routes), 3),
            "via_count": sum(len(r["vias"]) for r in routes)}


def _straight(a, b, c) -> bool:
    return abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) < 1e-9


def escape(pcb: Path | str, spec: dict | Path | str, apply: bool = False, out: Path | str | None = None) -> dict:
    """See ``kat.pcb.escape``."""
    report.touch(pcb)
    spec = _read(spec)
    pairs = [list(p) for p in spec.get("pairs") or []]
    b = Board(pcb)
    res = plan(b, spec) if pairs == [] or spec.get("sources") else {"routes": [], "assignment": {}}
    res["board"] = str(pcb)
    if pairs:
        res["not_handled"] = {"pairs": pairs, "why": "fixed pairs are for the ordinary router: kat pcb route --net NAME"}
    if out is not None:
        Path(out).write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
        res["out"] = str(out)
    res["applied"] = False
    if apply:
        wrong = []
        for r in res["routes"]:
            have = _pad_of(b, r["source"]).net
            if r["net"] and have != r["net"]:
                wrong.append(f"{r['source']}={r['net']}")
        if wrong:
            raise env.KatError("the design does not have this assignment yet, and connectivity belongs to it. "
                               f"First: kat back-annotate TABLE --assign {' '.join(wrong)} --run SCRIPT --sch SCH --pcb PCB")
        copper = b.copper_layers()
        for r in res["routes"]:
            net = r["net"] or _pad_of(b, r["source"]).net
            for t in r["tracks"]:
                b._segment(tuple(t["start"]), tuple(t["end"]), res["track"], t["layer"], net, False)
            for v in r["vias"]:
                b._via(tuple(v), net, res["via"], res["via_drill"], False, (copper[0], copper[-1]), "through")
        b.save()
        from . import checks
        c = checks.run(Path(pcb))
        res["applied"] = True
        res["check"] = {k: c[k] for k in ("ok", "drc_errors", "unconnected", "parity", "by_type")}
    return env.tidy(res)
