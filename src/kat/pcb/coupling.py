"""kat's net rules, checked by ``kat pcb check`` next to KiCad's DRC: what DRC does not see.

- **coupling** ``A`` ~ ``B``: copper of a net in A (e.g. amplifier outputs ``OUT*``) that
  overlaps, or comes closer than ``distance`` to, copper of a net in B (high-impedance
  inputs ``IN*``) of *another index*, on the same or an adjacent copper layer. Reported
  with the overlap area and length and, across layers, a parallel-plate capacitance from
  the stack-up (C = ε0·εr·A/h; fringing left out, so a lower bound).
- **layers**: nets that may only lie on the named layers (names, ``L1``..., or layer roles).
- **plane**: tracks of the nets need a pour (of ``net``, or of any other net) on an
  adjacent layer under their whole length.

Nets are chosen by a glob over net names (``OUT*``, ``/*/IN``) or ``class:NAME`` (a net
class). The *index* of a net is what the glob's first wildcard matched (``OUT3`` -> ``3``,
``/*/IN`` on ``/CH_A/IN`` -> ``CH_A``; a glob without wildcard: the net itself); for a
class, the net's sheet path or its digits. Nets of the same
index (one channel's own output and input) are not a coupling finding.

The rules are stored in the board's ``<board>.kat.json``.
"""

from __future__ import annotations

import math
import re
import shutil
import tempfile
from pathlib import Path

from .. import env
from ..sexpr import find, find_all, head
from . import layers as lay
from .geometry import box_of, inside, point_seg, poly_seg, seg_seg, segs_cross

EPS0 = 8.854e-3          # pF/mm
RES = 0.025              # mm: grid of the overlap area
STEP = 0.1               # mm: samples along a track for the plane rule


# --- storing rules ------------------------------------------------------------------------

def _store(pcb: Path | str, rule: dict, key: tuple) -> list[dict]:
    data = lay.meta(pcb)
    rules = [r for r in data.get("net_rules", []) if tuple(r.get(k) for k in key) != tuple(rule.get(k) for k in key)]
    data["net_rules"] = rules + [rule]
    lay.write_meta(pcb, data)
    return data["net_rules"]


def coupling_rule(pcb: Path | str, a: str, b: str, distance: float = 0.3) -> list[dict]:
    """Flag copper of nets ``a`` (glob or ``class:NAME``) overlapping or within
    ``distance`` mm of copper of nets ``b`` of another index, on the same or an adjacent
    layer (``kat pcb check``: ``net_rules``). Replaces a rule for the same ``a``/``b``.
    Returns all net rules of the board."""
    if distance < 0:
        raise env.KatError("distance must be >= 0 mm")
    return _store(pcb, {"kind": "coupling", "a": a, "b": b, "distance": float(distance)}, ("kind", "a", "b"))


def layer_rule(pcb: Path | str, nets: str, layers: list[str]) -> list[dict]:
    """Nets ``nets`` (glob or ``class:NAME``) may only lie on ``layers``: KiCad names,
    ``L1``..., or layer roles of the board (``set_layer_roles``). Checked by ``kat pcb
    check`` and by ``stamp`` on the copper it copies. Returns all net rules."""
    from .board import Board
    _layers(Board(pcb), layers)                       # refuse unknown names now
    return _store(pcb, {"kind": "layers", "nets": nets, "layers": list(layers)}, ("kind", "nets"))


def plane_rule(pcb: Path | str, nets: str, net: str | None = None) -> list[dict]:
    """Tracks of ``nets`` need a pour on an adjacent copper layer under their whole length:
    of ``net`` (e.g. GND), or of any other net if None. Checked by ``kat pcb check`` with
    the pours filled. Returns all net rules."""
    return _store(pcb, {"kind": "plane", "nets": nets, "net": net}, ("kind", "nets"))


def net_rules(pcb: Path | str, clear: bool = False) -> list[dict]:
    """The board's net rules (``clear=True`` removes them all first)."""
    data = lay.meta(pcb)
    if clear:
        data.pop("net_rules", None)
        lay.write_meta(pcb, data)
    return data.get("net_rules", [])


# --- selecting nets -----------------------------------------------------------------------

def _selector(board, sel: str):
    """net -> index (tuple) if the net is selected, else None."""
    if sel.startswith("class:"):
        cls = sel[6:]

        def by_class(net: str):
            if board.net_rules(net)["netclass"] != cls:
                return None
            return (net.rsplit("/", 1)[0],) if "/" in net.strip("/") else tuple(re.findall(r"\d+", net))
        return by_class
    rx = re.compile("".join("(.*)" if ch == "*" else "(.)" if ch == "?" else re.escape(ch) for ch in sel))

    def by_glob(net: str):
        m = rx.fullmatch(net)
        if not m:
            return None
        return m.groups()[:1] if m.groups() else (net,)
    return by_glob


def _layers(board, tokens: list[str]) -> set[str]:
    copper = board.copper_layers()
    roles = lay.meta(board.path).get("layer_roles", {})
    out = set()
    has = {l: set(r.split("|")) for l, r in roles.items()}
    for t in tokens:
        if any(t in r for r in has.values()):
            out |= {l for l, r in has.items() if t in r}
        elif t in ("*.Cu", "*"):
            out |= set(copper)
        else:
            try:
                out.add(lay.layer_name(t, copper))
            except env.KatError:
                raise env.KatError(f"{t!r} is no copper layer and no layer role of {board.path.name} "
                                   f"(roles: {', '.join(sorted(set().union(*has.values()))) or 'none'})")
    return out


# --- copper as shapes ---------------------------------------------------------------------

class _Shape:
    """A capsule (track, via: ``a``-``b`` with radius ``r``) or a polygon (pad, pour)."""

    def __init__(self, a=None, b=None, r: float = 0.0, poly=None):
        self.a, self.b, self.r, self.poly = a, b, r, poly
        if poly is not None:
            self.box = box_of(poly)
        else:
            self.box = (min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r)

    def contains(self, p) -> bool:
        if self.poly is not None:
            return inside(self.poly, p)
        return point_seg(p, self.a, self.b) <= self.r

    def distance(self, o: "_Shape") -> float:
        if self.poly is None and o.poly is None:
            return max(seg_seg(self.a, self.b, o.a, o.b) - self.r - o.r, 0.0)
        if self.poly is not None and o.poly is not None:
            if inside(self.poly, o.poly[0]) or inside(o.poly, self.poly[0]):
                return 0.0
            e1 = list(zip(self.poly, self.poly[1:] + self.poly[:1]))
            e2 = list(zip(o.poly, o.poly[1:] + o.poly[:1]))
            if any(segs_cross(a, b, c, d) for a, b in e1 for c, d in e2):
                return 0.0
            return min(seg_seg(a, b, c, d) for a, b in e1 for c, d in e2)
        p, c = (self, o) if self.poly is not None else (o, self)
        return max(poly_seg(p.poly, c.a, c.b) - c.r, 0.0)


def _arc_points(s, m, e, n: int = 8) -> list:
    """Points along the arc start-mid-end."""
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [s, e]
    ux = ((ax ** 2 + ay ** 2) * (by - cy) + (bx ** 2 + by ** 2) * (cy - ay) + (cx ** 2 + cy ** 2) * (ay - by)) / d
    uy = ((ax ** 2 + ay ** 2) * (cx - bx) + (bx ** 2 + by ** 2) * (ax - cx) + (cx ** 2 + cy ** 2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    t0, t1, tm = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, e, m))
    span = (t1 - t0) % (2 * math.pi)
    if (tm - t0) % (2 * math.pi) > span:          # the other way round
        span -= 2 * math.pi
    return [(ux + r * math.cos(t0 + span * k / n), uy + r * math.sin(t0 + span * k / n)) for k in range(n + 1)]


def _span(layers_: list[str], copper: list[str]) -> list[str]:
    """Copper layers a via or a wildcard layer list covers."""
    if any(l in ("*.Cu", "F&B.Cu") for l in layers_):
        return list(copper)
    idx = [copper.index(l) for l in layers_ if l in copper]
    return copper[min(idx):max(idx) + 1] if idx else []


def _copper(board, filled, nets: set[str]) -> dict[tuple[str, str], list[_Shape]]:
    """(net, layer) -> shapes, for ``nets``: tracks, arcs, vias, pads and the pours of
    ``filled`` (a board with filled zones; the zone outline where it has no fill)."""
    copper = board.copper_layers()
    out: dict[tuple[str, str], list[_Shape]] = {}

    def add(net, layer, shape):
        out.setdefault((net, layer), []).append(shape)

    for t in board.tracks():
        if t["net"] not in nets:
            continue
        r = t["width"] / 2
        if t["kind"] == "arc":
            pts = _arc_points(t["start"], _pt(find(t["node"], "mid")), t["end"])
        else:
            pts = [t["start"], t["end"]]
        for a, b in zip(pts, pts[1:]):
            add(t["net"], t["layer"], _Shape(a, b, r))
    for v in board.vias():
        if v["net"] in nets:
            for l in _span(list(v["layers"]), copper):
                add(v["net"], l, _Shape(v["at"], v["at"], v["size"] / 2))
    for p in board.pads():
        if p.net in nets:
            for l in copper:
                if p.on(l):
                    add(p.net, l, _Shape(poly=p.outline))
    for z in find_all(filled.tree, "zone"):
        net = find(z, "net")
        if net is None or str(net[1]) not in nets:
            continue
        fills = find_all(z, "filled_polygon")
        if fills:
            for fp in fills:
                add(str(net[1]), str(find(fp, "layer")[1]), _Shape(poly=_pts(fp)))
        else:
            zl = [str(t) for k in ("layer", "layers") if (n := find(z, k)) for t in n[1:]]
            for l in _span(zl, copper) if any("*" in x or "&" in x for x in zl) else zl:
                add(str(net[1]), l, _Shape(poly=_pts(find(z, "polygon"))))
    return out


def _pt(n) -> tuple[float, float]:
    return float(n[1]), float(n[2])


def _pts(node) -> list[tuple[float, float]]:
    return [_pt(xy) for xy in find_all(find(node, "pts") or [], "xy")]


def _grow(b, d):
    return b[0] - d, b[1] - d, b[2] + d, b[3] + d


def _meets(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _union_box(shapes):
    return (min(s.box[0] for s in shapes), min(s.box[1] for s in shapes),
            max(s.box[2] for s in shapes), max(s.box[3] for s in shapes))


# --- the checks ---------------------------------------------------------------------------

def _coupling(board, filled, rule: dict) -> list[dict]:
    sa, sb = _selector(board, rule["a"]), _selector(board, rule["b"])
    nets = _nets(board)
    A = {n: i for n in nets if (i := sa(n)) is not None}
    B = {n: i for n in nets if (i := sb(n)) is not None}
    if not A or not B:
        return []
    cu = _copper(board, filled, set(A) | set(B))
    copper = board.copper_layers()
    d = rule.get("distance", 0.3)
    boxes = {k: _union_box(v) for k, v in cu.items()}
    out = []
    for (na, la), shapes_a in cu.items():
        if na not in A:
            continue
        for (nb, lb), shapes_b in cu.items():
            if nb not in B or nb == na or A[na] == B[nb]:
                continue
            k = abs(copper.index(la) - copper.index(lb))
            if k > 1 or not _meets(_grow(boxes[(na, la)], d), boxes[(nb, lb)]):
                continue
            near = [(x, y) for x in shapes_a for y in shapes_b if _meets(_grow(x.box, d), y.box)]
            if not near:
                continue
            dist, x, y = min(((x.distance(y), x, y) for x, y in near), key=lambda t: t[0])
            if dist >= d and dist > 0:
                continue
            f = {"rule": f"coupling {rule['a']} ~ {rule['b']}", "kind": "coupling", "a": na, "b": nb,
                 "layers": [la, lb], "distance": round(dist, 3)}
            cells = _overlap(near) if k == 1 else set()
            if cells:
                xs, ys = [c[0] for c in cells], [c[1] for c in cells]
                area = len(cells) * RES * RES
                h, eps = lay.gap(board, la, lb)
                f.update(area=round(area, 4), length=round((max(max(xs) - min(xs), max(ys) - min(ys)) + 1) * RES, 3),
                         capacitance_pf=round(EPS0 * eps * area / h, 4), dielectric=h, epsilon_r=eps,
                         x=round(sum(xs) / len(xs) * RES, 3), y=round(sum(ys) / len(ys) * RES, 3))
                f["message"] = (f"{na} ({la}) over {nb} ({lb}): {f['area']:g} mm² ({f['length']:g} mm) "
                                f"through {h:g} mm, ≈ {f['capacitance_pf']:g} pF")
            else:
                cx, cy = _closest(x, y)
                f.update(x=round(cx, 3), y=round(cy, 3))
                where = "on " + la if la == lb else f"on {la} / {lb}"
                f["message"] = f"{na} and {nb} {dist:g} mm apart {where} (rule: {d:g} mm)"
            out.append(f)
    return out


def _overlap(pairs) -> set[tuple[int, int]]:
    """Grid cells (RES) inside a shape of each side, over all pairs: the overlap area."""
    cells = set()
    for x, y in pairs:
        b = (max(x.box[0], y.box[0]), max(x.box[1], y.box[1]), min(x.box[2], y.box[2]), min(x.box[3], y.box[3]))
        if b[0] > b[2] or b[1] > b[3]:
            continue
        for i in range(math.ceil(b[0] / RES), math.floor(b[2] / RES) + 1):
            for j in range(math.ceil(b[1] / RES), math.floor(b[3] / RES) + 1):
                if (i, j) not in cells:
                    p = (i * RES, j * RES)
                    if x.contains(p) and y.contains(p):
                        cells.add((i, j))
    return cells


def _closest(x: _Shape, y: _Shape) -> tuple[float, float]:
    """A point between two shapes (for the finding's position)."""
    ca = ((x.box[0] + x.box[2]) / 2, (x.box[1] + x.box[3]) / 2)
    cb = ((y.box[0] + y.box[2]) / 2, (y.box[1] + y.box[3]) / 2)
    return (ca[0] + cb[0]) / 2, (ca[1] + cb[1]) / 2


def _nets(board) -> set[str]:
    nets = {p.net for p in board.pads()} | {t["net"] for t in board.tracks()} | {v["net"] for v in board.vias()}
    nets |= {str(n[1]) for z in board.zones() if (n := find(z, "net")) is not None}
    return {n for n in nets if n}


def _node_layers(node: list, copper: list[str]) -> list[str]:
    ls = [str(t) for k in ("layer", "layers") if (n := find(node, k)) is not None for t in n[1:]
          if not isinstance(t, list)]
    if head(node) == "via":
        return _span(ls, copper)
    if any("*" in l or "&" in l for l in ls):
        return _span(ls, copper)
    return [l for l in ls if l in copper]


def _where(node: list) -> tuple[float, float]:
    for k in ("at", "start"):
        if (n := find(node, k)) is not None:
            return _pt(n)
    pts = _pts(find(node, "polygon") or [])
    return pts[0] if pts else (0.0, 0.0)


def layer_findings(board, nodes: list | None = None) -> list[dict]:
    """The board's layer rules on copper nodes (tracks, arcs, vias, zones; default: all
    of the board's): one finding per node on a layer its net may not use."""
    rules = [r for r in lay.meta(board.path).get("net_rules", []) if r["kind"] == "layers"]
    if not rules:
        return []
    copper = board.copper_layers()
    if nodes is None:
        nodes = [n for n in board.tree if head(n) in ("segment", "arc", "via", "zone")]
    out = []
    for r in rules:
        sel, allowed = _selector(board, r["nets"]), _layers(board, r["layers"])
        for n in nodes:
            net = find(n, "net")
            if net is None or not str(net[1]) or sel(str(net[1])) is None:
                continue
            bad = [l for l in _node_layers(n, copper) if l not in allowed]
            if bad:
                x, y = _where(n)
                kind = {"segment": "track", "arc": "track"}.get(head(n), head(n))
                out.append({"rule": f"layers {r['nets']}: {', '.join(r['layers'])}", "kind": "layers",
                            "net": str(net[1]), "layers": bad, "x": x, "y": y,
                            "message": f"{kind} of {net[1]} on {', '.join(bad)} at {x:g},{y:g} "
                                       f"(allowed: {', '.join(sorted(allowed, key=copper.index))})"})
    return out


def _plane(board, filled, rule: dict) -> list[dict]:
    sel = _selector(board, rule["nets"])
    copper = board.copper_layers()
    pours: dict[str, list] = {}
    for z in find_all(filled.tree, "zone"):
        net = find(z, "net")
        net = str(net[1]) if net is not None else ""
        if not net or (rule.get("net") and net != rule["net"]):
            continue
        for fp in find_all(z, "filled_polygon"):
            poly = _pts(fp)
            pours.setdefault(str(find(fp, "layer")[1]), []).append((net, box_of(poly), poly))
    per_net: dict[str, dict] = {}
    for t in board.tracks():
        if not t["net"] or sel(t["net"]) is None:
            continue
        i = copper.index(t["layer"])
        cand = [p for l in copper[max(i - 1, 0):i + 2] if l != t["layer"] for p in pours.get(l, [])
                if p[0] != t["net"]]
        a, b = t["start"], t["end"]
        if t["kind"] == "arc":
            pts = _arc_points(a, _pt(find(t["node"], "mid")), b)
        else:
            pts = [a, b]
        for p, q in zip(pts, pts[1:]):
            L = math.hypot(q[0] - p[0], q[1] - p[1])
            n = max(1, math.ceil(L / STEP))
            ux, uy = ((q[0] - p[0]) / L, (q[1] - p[1]) / L) if L else (1.0, 0.0)
            w = t["width"] / 2
            for k in range(n + 1):
                c = (p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n)
                probes = [c, (c[0] - uy * w, c[1] + ux * w), (c[0] + uy * w, c[1] - ux * w)]
                if all(any(bx[0] <= s[0] <= bx[2] and bx[1] <= s[1] <= bx[3] and inside(poly, s)
                           for _, bx, poly in cand) for s in probes):
                    continue
                e = per_net.setdefault(t["net"], {"length": 0.0, "at": c, "layers": set()})
                e["length"] += L / n if k < n else 0.0
                e["layers"].add(t["layer"])
    under = f"a {rule['net']} pour" if rule.get("net") else "a pour"
    return [{"rule": f"plane under {rule['nets']}" + (f" ({rule['net']})" if rule.get("net") else ""),
             "kind": "plane", "net": net, "uncovered": round(e["length"], 2),
             "layers": sorted(e["layers"], key=copper.index), "x": round(e["at"][0], 3), "y": round(e["at"][1], 3),
             "message": f"{net}: {e['length']:.2f} mm of track without {under} on an adjacent layer "
                        f"(first at {e['at'][0]:.2f},{e['at'][1]:.2f})"}
            for net, e in per_net.items() if e["length"] > 1e-6]


def run(pcb: Path, board=None) -> dict | None:
    """All net rules of a board: {rules, findings}, None if it has none. Coupling and plane
    rules see the pours filled (KiCad's filler on a scratch copy)."""
    from .board import Board
    rules = lay.meta(pcb).get("net_rules", [])
    if not rules:
        return None
    board = board or Board(pcb)
    filled = board
    if any(r["kind"] in ("coupling", "plane") for r in rules) and board.zones():
        filled = _filled(Path(pcb))
    findings = layer_findings(board)
    for r in rules:
        if r["kind"] == "coupling":
            findings += _coupling(board, filled, r)
        elif r["kind"] == "plane":
            findings += _plane(board, filled, r)
    return {"rules": len(rules), "findings": findings}


def _filled(pcb: Path):
    """The board with its pours filled, from a scratch copy (the file stays as it is)."""
    from .board import Board
    from .checks import drc
    with tempfile.TemporaryDirectory() as d:
        for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
            if pcb.with_suffix(suffix).exists():
                shutil.copy(pcb.with_suffix(suffix), Path(d) / (pcb.stem + suffix))
        tmp = Path(d) / pcb.name
        drc(tmp, parity=False, refill=True, save=True)
        return Board(tmp)
