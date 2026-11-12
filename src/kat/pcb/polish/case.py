"""The route polisher's input (``specs/ROUTE_POLISH_INPUT.md``, versions 1 and 2): a strict
reader that rejects unknown fields, duplicate keys, malformed geometry and missing
references instead of ignoring them, and a canonical writer.

Coordinates in mm, x right, y up. Geometric feasibility of the traces (clearances) is not
checked here but in ``constraints.preflight``.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from ...env import KatError
from ..geometry import inside, point_seg, segs_cross

FORMAT = "route-polish-input"
VERSIONS = (1, 2)
PROFILES = {1: "single-layer-independent-paths", 2: "single-layer-anchored-paths"}
WEIGHTS = ("angle", "spacing", "edge", "displacement", "length", "complexity")
TOP = ("format", "version", "units", "provenance", "scope", "layer", "board", "movement_region", "rules",
       "traces", "fixed_copper", "holes", "keepouts", "groups", "objectives", "limits")
OBJECTIVES = ("angle_step_deg", "preferred_spacing_mm", "preferred_edge_margin_mm", "parallel_tolerance_deg",
              "displacement_scale_mm", "short_segment_mm", "weights")
LIMITS = ("max_iterations", "max_added_vertices_per_trace", "max_displacement_mm", "snap_tolerance_deg", "seed")
TOUCH = 1e-6                # mm: an endpoint this close to an anchor's copper touches it

Pt = tuple[float, float]


class CaseError(KatError):
    """An input that does not follow the format; ``problems`` lists every finding."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("route-polish input invalid: " + "; ".join(problems[:8])
                         + (f" (+{len(problems) - 8} more)" if len(problems) > 8 else ""))


@dataclass
class Trace:
    """A movable polyline: first and last point fixed, ``anchors`` the same-net fixed copper
    it may touch (version 2)."""
    id: str
    net: str
    width: float
    points: list[Pt]
    anchors: tuple[str, ...] = ()


@dataclass
class Copper:
    """Fixed copper: ``kind`` polyline (``points``, ``width``), circle (``center``,
    ``radius``) or polygon (``points``)."""
    id: str
    net: str
    kind: str
    points: list[Pt] = field(default_factory=list)
    width: float = 0.0
    center: Pt = (0.0, 0.0)
    radius: float = 0.0


@dataclass
class Hole:
    """A drill void (not an inflated clearance disc)."""
    id: str
    center: Pt
    radius: float


@dataclass
class Keepout:
    """A forbidden polygon with its own clearance."""
    id: str
    points: list[Pt]
    clearance: float


@dataclass
class Case:
    """A parsed and structurally valid input. ``pairs`` maps frozenset({a, b}) to the extra
    clearance (mm); ``groups`` maps group id to its ordered trace ids."""
    version: int
    sha256: str
    layer: str
    provenance: dict
    scope: dict
    outline: list[Pt]
    cutouts: list[list[Pt]]
    region: list[Pt]
    copper_clearance: float
    edge_clearance: float
    hole_clearance: float
    pairs: dict
    traces: dict[str, Trace]
    copper: dict[str, Copper]
    holes: dict[str, Hole]
    keepouts: dict[str, Keepout]
    groups: dict[str, list[str]]
    objectives: dict
    limits: dict
    raw: dict

    def group_of(self, trace_id: str) -> str:
        """The id of the group holding ``trace_id``."""
        return next(g for g, ts in self.groups.items() if trace_id in ts)

    def clearance(self, a: str, b: str, base: float) -> float:
        """The effective minimum between objects ``a`` and ``b``: the maximum of ``base`` and
        any pair entry (mm)."""
        return max(base, self.pairs.get(frozenset((a, b)), 0.0))


# --- reading ----------------------------------------------------------------------------

def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise CaseError([f"duplicate key {k!r}"])
        out[k] = v
    return out


def _no_constant(name):
    raise CaseError([f"non-finite number {name}"])


def loads(text: str | bytes) -> Case:
    """Parse and check an input document; raises ``CaseError`` listing every problem."""
    raw_bytes = text.encode("utf-8") if isinstance(text, str) else text
    try:
        doc = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CaseError([f"not JSON: {e}"]) from None
    return _Reader(doc, hashlib.sha256(raw_bytes).hexdigest()).case()


def load(path: Path | str) -> Case:
    """Read and check an input file (``loads`` on its bytes)."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise KatError(f"cannot read {path}: {e}") from None
    return loads(data)


def dumps(doc: dict) -> str:
    """Canonical text of an input document (two-space indent, keys in format order)."""
    return json.dumps(doc, indent=2) + "\n"


def sha256_of(path: Path | str) -> str:
    """Hex sha256 of a file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class _Reader:
    def __init__(self, doc, sha: str):
        self.doc, self.sha, self.problems = doc, sha, []
        self.ids: set[str] = set()

    def bad(self, msg: str) -> None:
        self.problems.append(msg)

    # small typed getters; each records a problem and returns a harmless default
    def keys(self, obj, want, where: str) -> bool:
        if not isinstance(obj, dict):
            self.bad(f"{where}: not an object")
            return False
        missing = [k for k in want if k not in obj]
        extra = [k for k in obj if k not in want]
        if missing:
            self.bad(f"{where}: missing {', '.join(missing)}")
        if extra:
            self.bad(f"{where}: unsupported {', '.join(extra)}")
        return not missing

    def num(self, v, where: str, positive=False, nonneg=False) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            self.bad(f"{where}: not a finite number")
            return 1.0
        if positive and v <= 0:
            self.bad(f"{where}: must be positive")
        if nonneg and v < 0:
            self.bad(f"{where}: must not be negative")
        return float(v)

    def integer(self, v, where: str, minimum: int) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            self.bad(f"{where}: not an integer >= {minimum}")
            return minimum
        return v

    def text(self, v, where: str) -> str:
        if not isinstance(v, str) or not v:
            self.bad(f"{where}: not a non-empty string")
            return ""
        return v

    def point(self, v, where: str) -> Pt:
        if not isinstance(v, list) or len(v) != 2:
            self.bad(f"{where}: not a point [x, y]")
            return (0.0, 0.0)
        return (self.num(v[0], where), self.num(v[1], where))

    def points(self, v, where: str, minimum: int) -> list[Pt]:
        if not isinstance(v, list) or len(v) < minimum:
            self.bad(f"{where}: needs at least {minimum} points")
            return []
        return [self.point(p, f"{where}[{i}]") for i, p in enumerate(v)]

    def ring(self, v, where: str) -> list[Pt]:
        pts = self.points(v, where, 3)
        if len(pts) < 3:
            return pts
        if len(set(pts)) != len(pts):
            self.bad(f"{where}: repeated point (no closing point, all distinct)")
            return pts
        if abs(_area(pts)) < 1e-12:
            self.bad(f"{where}: zero area")
        n = len(pts)
        for i in range(n):
            for j in range(i + 2, n):
                if i == 0 and j == n - 1:
                    continue
                if segs_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                    self.bad(f"{where}: self-intersecting (edges {i} and {j})")
                    return pts
        return pts

    def polyline(self, v, where: str) -> list[Pt]:
        pts = self.points(v, where, 2)
        for i, (a, b) in enumerate(zip(pts, pts[1:])):
            if a == b:
                self.bad(f"{where}: segment {i} has zero length")
        return pts

    def new_id(self, v, where: str) -> str:
        i = self.text(v, f"{where}.id")
        if i in self.ids:
            self.bad(f"{where}: id {i!r} not unique")
        self.ids.add(i)
        return i

    def case(self) -> Case:
        d = self.doc
        if not self.keys(d, TOP, "input"):
            raise CaseError(self.problems)
        if d["format"] != FORMAT:
            self.bad(f"format: must be {FORMAT!r}")
        version = d["version"]
        if version not in VERSIONS or isinstance(version, bool):
            raise CaseError(self.problems + [f"version {version!r}: supported {', '.join(map(str, VERSIONS))}"])
        if d["units"] != "mm":
            self.bad("units: must be 'mm'")
        prov = d["provenance"]
        if self.keys(prov, ("kind", "public_fixture", "description"), "provenance"):
            if prov["kind"] not in ("synthetic", "private-board-extract"):
                self.bad("provenance.kind: synthetic or private-board-extract")
            if not isinstance(prov["public_fixture"], bool):
                self.bad("provenance.public_fixture: not a boolean")
            elif prov["public_fixture"] and prov["kind"] != "synthetic":
                self.bad("provenance: a public fixture must be synthetic")
            self.text(prov["description"], "provenance.description")
        scope = d["scope"]
        if self.keys(scope, ("profile", "context_complete", "electrical_checks"), "scope"):
            if scope["profile"] != PROFILES[version]:
                self.bad(f"scope.profile: version {version} is {PROFILES[version]!r}")
            if scope["context_complete"] is not True:
                self.bad("scope.context_complete is not true: optimisation refused")
            if scope["electrical_checks"] != "not-modelled":
                self.bad("scope.electrical_checks: must be 'not-modelled'")
        layer = self.text(d["layer"], "layer")

        board = d["board"]
        outline, cutouts = [], []
        if self.keys(board, ("outline", "cutouts"), "board"):
            outline = self.ring(board["outline"], "board.outline")
            if not isinstance(board["cutouts"], list):
                self.bad("board.cutouts: not an array")
            else:
                cutouts = [self.ring(c, f"board.cutouts[{i}]") for i, c in enumerate(board["cutouts"])]
            for i, c in enumerate(cutouts):
                if outline and c and not all(inside(outline, p) and _ring_dist(outline, p) > 0 for p in c):
                    self.bad(f"board.cutouts[{i}]: not strictly inside the outline")
                for j in range(i):
                    if c and cutouts[j] and _rings_meet(c, cutouts[j]):
                        self.bad(f"board.cutouts[{i}]: meets cutout {j}")
        region = self.ring(d["movement_region"], "movement_region")

        traces: dict[str, Trace] = {}
        if not isinstance(d["traces"], list) or not d["traces"]:
            self.bad("traces: needs at least one trace")
        else:
            want = ("id", "net", "width_mm", "points") + (("anchors",) if version >= 2 else ())
            for i, t in enumerate(d["traces"]):
                w = f"traces[{i}]"
                if not self.keys(t, want, w):
                    continue
                tid = self.new_id(t["id"], w)
                anchors = ()
                if version >= 2:
                    if not isinstance(t["anchors"], list) or not all(isinstance(a, str) for a in t["anchors"]):
                        self.bad(f"{w}.anchors: not an array of ids")
                    elif len(set(t["anchors"])) != len(t["anchors"]):
                        self.bad(f"{w}.anchors: repeated id")
                    else:
                        anchors = tuple(t["anchors"])
                traces[tid] = Trace(tid, self.text(t["net"], f"{w}.net"), self.num(t["width_mm"], f"{w}.width_mm", positive=True),
                                    self.polyline(t["points"], f"{w}.points"), anchors)
            if version == 1 and len({t.net for t in traces.values()}) != len(traces):
                self.bad("traces: version 1 needs a distinct net per trace")

        copper: dict[str, Copper] = {}
        for i, c in enumerate(self.array(d["fixed_copper"], "fixed_copper")):
            w = f"fixed_copper[{i}]"
            if not self.keys(c, ("id", "net", "shape"), w):
                continue
            cid, net, s = self.new_id(c["id"], w), self.text(c["net"], f"{w}.net"), c["shape"]
            kind = s.get("type") if isinstance(s, dict) else None
            if kind == "polyline" and self.keys(s, ("type", "points", "width_mm"), f"{w}.shape"):
                copper[cid] = Copper(cid, net, kind, self.polyline(s["points"], f"{w}.shape.points"),
                                     width=self.num(s["width_mm"], f"{w}.shape.width_mm", positive=True))
            elif kind == "circle" and self.keys(s, ("type", "center", "radius_mm"), f"{w}.shape"):
                copper[cid] = Copper(cid, net, kind, center=self.point(s["center"], f"{w}.shape.center"),
                                     radius=self.num(s["radius_mm"], f"{w}.shape.radius_mm", positive=True))
            elif kind == "polygon" and self.keys(s, ("type", "points"), f"{w}.shape"):
                copper[cid] = Copper(cid, net, kind, self.ring(s["points"], f"{w}.shape.points"))
            elif kind not in ("polyline", "circle", "polygon"):
                self.bad(f"{w}.shape.type: polyline, circle or polygon")
        holes: dict[str, Hole] = {}
        for i, h in enumerate(self.array(d["holes"], "holes")):
            w = f"holes[{i}]"
            if self.keys(h, ("id", "center", "radius_mm"), w):
                hid = self.new_id(h["id"], w)
                holes[hid] = Hole(hid, self.point(h["center"], f"{w}.center"), self.num(h["radius_mm"], f"{w}.radius_mm", positive=True))
        keepouts: dict[str, Keepout] = {}
        for i, k in enumerate(self.array(d["keepouts"], "keepouts")):
            w = f"keepouts[{i}]"
            if self.keys(k, ("id", "points", "clearance_mm"), w):
                kid = self.new_id(k["id"], w)
                keepouts[kid] = Keepout(kid, self.ring(k["points"], f"{w}.points"),
                                        self.num(k["clearance_mm"], f"{w}.clearance_mm", nonneg=True))
        if outline:
            fixed_pts = [(o.id, p) for o in copper.values() for p in (o.points or [o.center])]
            fixed_pts += [(h.id, h.center) for h in holes.values()] + [(k.id, p) for k in keepouts.values() for p in k.points]
            outside = sorted({i for i, p in fixed_pts if not (inside(outline, p) or _ring_dist(outline, p) < TOUCH)})
            if outside:
                self.bad(f"fixed objects outside the board outline: {', '.join(outside)}")

        for t in traces.values():                                   # version 2 anchors
            for a in t.anchors:
                c = copper.get(a)
                if c is None:
                    self.bad(f"trace {t.id}: anchor {a!r} is not fixed copper")
                elif c.net != t.net:
                    self.bad(f"trace {t.id}: anchor {a} has net {c.net}, not {t.net}")
                elif t.points and not any(copper_distance(c, p) <= TOUCH for p in (t.points[0], t.points[-1])):
                    self.bad(f"trace {t.id}: anchor {a} touches neither end")
        ends = {}
        for t in traces.values():
            for p in (t.points[:1] + t.points[-1:]):
                if p in ends and ends[p] != t.id:
                    self.bad(f"traces {ends[p]} and {t.id} share an endpoint {list(p)}")
                ends[p] = t.id

        rules = d["rules"]
        cu = edge = hole = 0.0
        pairs: dict = {}
        if self.keys(rules, ("copper_clearance_mm", "edge_clearance_mm", "hole_clearance_mm", "pair_clearances"), "rules"):
            cu = self.num(rules["copper_clearance_mm"], "rules.copper_clearance_mm", nonneg=True)
            edge = self.num(rules["edge_clearance_mm"], "rules.edge_clearance_mm", nonneg=True)
            hole = self.num(rules["hole_clearance_mm"], "rules.hole_clearance_mm", nonneg=True)
            for i, p in enumerate(self.array(rules["pair_clearances"], "rules.pair_clearances")):
                w = f"rules.pair_clearances[{i}]"
                if not self.keys(p, ("a", "b", "clearance_mm"), w):
                    continue
                a, b = p["a"], p["b"]
                known = set(traces) | set(copper) | set(holes) | set(keepouts)
                if a not in known or b not in known or a == b:
                    self.bad(f"{w}: needs two distinct existing objects")
                    continue
                if a not in traces and b not in traces:
                    self.bad(f"{w}: needs a movable trace")
                    continue
                key = frozenset((a, b))
                if key in pairs:
                    self.bad(f"{w}: duplicate pair")
                if any(x in traces and y in traces[x].anchors for x, y in ((a, b), (b, a))):
                    self.bad(f"{w}: a trace and its own anchor")
                pairs[key] = self.num(p["clearance_mm"], f"{w}.clearance_mm", nonneg=True)

        groups: dict[str, list[str]] = {}
        seen: set[str] = set()
        for i, g in enumerate(self.array(d["groups"], "groups")):
            w = f"groups[{i}]"
            if not self.keys(g, ("id", "trace_ids"), w):
                continue
            gid = self.text(g["id"], f"{w}.id")
            if gid in groups:
                self.bad(f"{w}: group id {gid!r} not unique")
            ts = g["trace_ids"] if isinstance(g["trace_ids"], list) else []
            if not ts:
                self.bad(f"{w}.trace_ids: needs at least one trace")
            for t in ts:
                if t not in traces:
                    self.bad(f"{w}: {t!r} is not a movable trace")
                elif t in seen:
                    self.bad(f"{w}: trace {t} in more than one group")
                seen.add(t)
            groups[gid] = list(ts)
        missing = [t for t in traces if t not in seen]
        if missing:
            self.bad(f"groups: traces in no group: {', '.join(missing)}")

        obj = d["objectives"]
        objectives: dict = {}
        if self.keys(obj, OBJECTIVES, "objectives"):
            if obj["angle_step_deg"] != 45:
                self.bad("objectives.angle_step_deg: must be 45")
            objectives = {k: self.num(obj[k], f"objectives.{k}") for k in OBJECTIVES if k != "weights"}
            if objectives["preferred_spacing_mm"] < cu:
                self.bad("objectives.preferred_spacing_mm: below the copper clearance")
            if objectives["preferred_edge_margin_mm"] < edge:
                self.bad("objectives.preferred_edge_margin_mm: below the edge clearance")
            if not 0 < objectives["parallel_tolerance_deg"] < 45:
                self.bad("objectives.parallel_tolerance_deg: between 0 and 45")
            for k in ("displacement_scale_mm", "short_segment_mm"):
                if objectives[k] <= 0:
                    self.bad(f"objectives.{k}: must be positive")
            wts = obj["weights"]
            if self.keys(wts, WEIGHTS, "objectives.weights"):
                objectives["weights"] = {k: self.num(wts[k], f"objectives.weights.{k}", nonneg=True) for k in WEIGHTS}
                if not any(objectives["weights"].values()):
                    self.bad("objectives.weights: at least one must be positive")
        lim = d["limits"]
        limits: dict = {}
        if self.keys(lim, LIMITS, "limits"):
            limits = {"max_iterations": self.integer(lim["max_iterations"], "limits.max_iterations", 1),
                      "max_added_vertices_per_trace": self.integer(lim["max_added_vertices_per_trace"],
                                                                   "limits.max_added_vertices_per_trace", 0),
                      "max_displacement_mm": self.num(lim["max_displacement_mm"], "limits.max_displacement_mm", positive=True),
                      "snap_tolerance_deg": self.num(lim["snap_tolerance_deg"], "limits.snap_tolerance_deg", positive=True),
                      "seed": self.integer(lim["seed"], "limits.seed", 0)}
            if limits["snap_tolerance_deg"] >= 22.5:
                self.bad("limits.snap_tolerance_deg: below half the angle step (22.5)")
        if self.problems:
            raise CaseError(self.problems)
        return Case(version, self.sha, layer, prov, scope, outline, cutouts, region, cu, edge, hole, pairs,
                    traces, copper, holes, keepouts, groups, objectives, limits, d)

    def array(self, v, where: str) -> list:
        if not isinstance(v, list):
            self.bad(f"{where}: not an array")
            return []
        return v


# --- geometry helpers ------------------------------------------------------------------

def _area(pts) -> float:
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1])) / 2


def _ring_dist(ring, p) -> float:
    return min(point_seg(p, a, b) for a, b in zip(ring, ring[1:] + ring[:1]))


def _rings_meet(r1, r2) -> bool:
    if any(inside(r2, p) for p in r1) or any(inside(r1, p) for p in r2):
        return True
    e1, e2 = list(zip(r1, r1[1:] + r1[:1])), list(zip(r2, r2[1:] + r2[:1]))
    return any(segs_cross(a, b, c, d) or min(point_seg(a, c, d), point_seg(c, a, b)) < TOUCH
               for a, b in e1 for c, d in e2)


def copper_distance(c: Copper, p: Pt) -> float:
    """Distance from point ``p`` to fixed copper ``c``'s copper (0 inside it), mm."""
    if c.kind == "circle":
        return max(0.0, math.dist(p, c.center) - c.radius)
    if c.kind == "polyline":
        return max(0.0, min(point_seg(p, a, b) for a, b in zip(c.points, c.points[1:])) - c.width / 2)
    return 0.0 if inside(c.points, p) else _ring_dist(c.points, p)


__all__ = ["Case", "CaseError", "Copper", "Hole", "Keepout", "Trace", "copper_distance", "dumps", "load", "loads",
           "sha256_of"]
