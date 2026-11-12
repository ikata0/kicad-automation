"""Between a KiCad board and the route polisher: ``extract`` writes a version-2 case
(``specs/ROUTE_POLISH_INPUT.md`` §11) and a private map file, ``apply`` writes a result into a
**copy** of the board and judges the copy by KiCad's DRC against the original.

A trace is a ``trackclean`` chain: a run of straight segments of one net, layer and width
between fixed points. The case's y axis points up, KiCad's down: case (x, y) = board (x, -y).
Context is everything on the layer near the movement region: pads (exact polygons, never
inside the real copper), vias, holes, other tracks (arcs as chords, widened by the chord
error), track keep-outs and the board outline (edge clearance raised by the arc error).
Copper pours are not obstacles (they reflow; DRC on the copy judges them) but are listed.
Rules that cannot be represented stop the extraction unless accepted.
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from ...env import KatError
from ...sexpr import Sym, find, loads, num
from .. import trackclean as tc
from ..board import Board
from ..geometry import arc_points
from . import case as pc

ARC_ERROR = 0.001           # mm: chord error of arcs and round pads
REACH = 3.0                 # mm around the movement region in which context is collected
OBJECTIVES = {"preferred_edge_margin_mm": 1.0, "parallel_tolerance_deg": 15, "displacement_scale_mm": 1.0}
WEIGHTS = {"angle": 1, "spacing": 1, "edge": 0.8, "displacement": 0.25, "length": 0.1, "complexity": 0.3}
LIMITS = {"max_iterations": 300, "max_added_vertices_per_trace": 2, "max_displacement_mm": 1.0,
          "snap_tolerance_deg": 1.0, "seed": 0}
SAFE_DRU = {"clearance", "edge_clearance", "hole_clearance", "track_width", "via_diameter", "hole_size",
            "annular_width", "via_count", "courtyard_clearance", "silk_clearance", "text_height",
            "text_thickness", "thermal_relief_gap", "thermal_spoke_width", "min_resolved_spokes",
            "zone_connection", "hole_to_hole", "solder_mask_expansion", "solder_paste_abs_margin",
            "solder_paste_rel_margin", "bridged_mask", "connection_width"}


def _c(p) -> tuple[float, float]:
    """Board point -> case point (y flipped)."""
    return (float(p[0]), -float(p[1]))


def _ring(pts) -> list[tuple[float, float]]:
    """A clean polygon ring in case coordinates: no repeated or closing point."""
    out: list = []
    for p in (_c(q) for q in pts):
        p = (round(p[0], 6), round(p[1], 6))
        if not out or math.dist(out[-1], p) > 1e-6:
            out.append(p)
    while len(out) > 2 and math.dist(out[0], out[-1]) <= 1e-6:
        out.pop()
    return out


def _box(pts) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _meets(a, b) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def map_path(case_path: Path | str) -> Path:
    """The private map file beside a case: ``CASE.json`` -> ``CASE.map.json``."""
    p = Path(case_path)
    return p.with_name(p.stem + ".map.json")


def dru_constraints(text: str) -> list[tuple[str, str, float | None]]:
    """(rule name, constraint kind, min in mm or None) of every constraint in .kicad_dru text."""
    from ..rules import _mm
    if not text.strip():
        return []
    out = []
    for n in loads(f"(dru {text})")[1:]:
        if not isinstance(n, list) or str(n[0]) != "rule":
            continue
        for c in n[1:]:
            if isinstance(c, list) and str(c[0]) == "constraint" and len(c) > 1:
                m = find(c, "min")
                out.append((str(n[1]), str(c[1]), _mm(m[1]) if m is not None and len(m) > 1 else None))
    return out


def extract(pcb: Path | str, nets: list[str], out: Path | str, layer: str | None = None, region=None,
            order: list[str] | None = None, objectives: dict | None = None, weights: dict | None = None,
            limits: dict | None = None, accept_unmodelled: bool = False, force: bool = False) -> dict:
    """See ``kat.pcb.polish_extract``."""
    pcb, out = Path(pcb), Path(out)
    mp = map_path(out)
    for f in (out, mp):
        if f.exists() and not force:
            raise KatError(f"{f} exists: choose another name or force")
    if out.resolve() == pcb.resolve():
        raise KatError("the case cannot replace the board")
    b = Board(pcb)
    if layer and layer not in b.copper_layers():
        raise KatError(f"no copper layer {layer!r} (have {', '.join(b.copper_layers())})")
    unknown = _unknown_nets(b, nets)
    if unknown:
        raise KatError(unknown)
    chains = [c for c in tc.chains(b, nets, [layer] if layer else None, region, include_locked=True)]
    if not chains:
        raise KatError(_why_none(b, nets, layer, region))
    layers = sorted({c.layer for c in chains})
    if len(layers) > 1:
        raise KatError(f"tracks on {', '.join(layers)}: choose one with --layer")
    layer = layers[0]
    held = []                                          # runs sharing an end: the longer one moves
    for c in sorted(chains, key=lambda c: -c.length):
        mine = {tc._key(c.pts[0]), tc._key(c.pts[-1])}
        if any(mine & {tc._key(o.pts[0]), tc._key(o.pts[-1])} for o in chains if o is not c and o not in held
               and o.length >= c.length and (o.length > c.length or id(o) < id(c))):
            held.append(c)
    chains = [c for c in chains if c not in held]
    chains = _ordered(chains, order)
    uuids = {str(find(n, "uuid")[1]) for c in chains for n in c.nodes}
    lim = {**LIMITS, **(limits or {})}
    pts_all = [_c(p) for c in chains for p in c.pts]
    half = max(c.width for c in chains) / 2
    g = lim["max_displacement_mm"] + half + 0.05
    x0, y0, x1, y1 = _box(pts_all)
    region_box = (x0 - g, y0 - g, x1 + g, y1 + g)
    reach = (region_box[0] - REACH, region_box[1] - REACH, region_box[2] + REACH, region_box[3] + REACH)

    tokens: dict[str, str] = {}
    unconnected = [0]

    def token(net: str) -> str:
        """Opaque net token; every unconnected item gets its own."""
        if not net:
            unconnected[0] += 1
            return f"nc{unconnected[0]:03d}"
        if net not in tokens:
            tokens[net] = f"n{len(tokens) + 1:03d}"
        return tokens[net]

    copper, holes, keepouts, unmodelled = [], [], [], []
    net_of: dict[str, str] = {}                          # object id -> board net name
    pad_holes: dict[str, list[str]] = {}                 # hole id -> copper ids of its pad or via
    slots: dict[str, list[str]] = {}

    def add_copper(kind_prefix, net, shape) -> str:
        i = f"{kind_prefix}{len(copper) + 1:04d}"
        copper.append({"id": i, "net": token(net), "shape": shape})
        net_of[i] = net
        return i

    for p in b.pads():
        box = _box([_c(q) for q in p.outline]) if p.on(layer) else None
        hb = p.hole_box
        hbox = _box([_c((hb[0], hb[1])), _c((hb[2], hb[3]))]) if hb else None
        if not ((box and _meets(box, reach)) or (hbox and _meets(hbox, reach))):
            continue
        ids = []
        if p.on(layer):
            for poly in p.polygons(ARC_ERROR):
                ring = _ring(poly)
                if len(ring) >= 3:
                    ids.append(add_copper("p", p.net, {"type": "polygon", "points": [list(q) for q in ring]}))
        hole = p._hole()
        if hole:
            r, (dx, dy) = hole
            if math.hypot(dx, dy) < 1e-6:
                hid = f"h{len(holes) + 1:04d}"
                holes.append({"id": hid, "center": list(_c((p.x, p.y))), "radius_mm": r})
                pad_holes[hid] = ids
            else:
                kid = f"k{len(keepouts) + 1:04d}"
                keepouts.append({"id": kid, "points": [list(q) for q in _ring(p.hole_outline)], "clearance_mm": None})
                slots[kid] = ids
    for v in b.vias():
        if layer not in v["span"]:
            continue
        c = _c(v["at"])
        r = v["size"] / 2
        if not _meets((c[0] - r, c[1] - r, c[0] + r, c[1] + r), reach):
            continue
        cid = add_copper("v", v["net"], {"type": "circle", "center": list(c), "radius_mm": r})
        if v["drill"]:
            hid = f"h{len(holes) + 1:04d}"
            holes.append({"id": hid, "center": list(c), "radius_mm": v["drill"] / 2})
            pad_holes[hid] = [cid]
    for t in b.tracks():
        if t["layer"] != layer or t["uuid"] in uuids:
            continue
        if t["kind"] == "arc":
            m = find(t["node"], "mid")
            pts = [_c(q) for q in arc_points(t["start"], (float(m[1]), float(m[2])), t["end"], ARC_ERROR)]
            w = t["width"] + 2 * ARC_ERROR
        else:
            pts, w = [_c(t["start"]), _c(t["end"])], t["width"]
        bx = _box(pts)
        if not _meets((bx[0] - w, bx[1] - w, bx[2] + w, bx[3] + w), reach):
            continue
        clean = [pts[0]] + [q for a, q in zip(pts, pts[1:]) if math.dist(a, q) > 1e-6]
        if len(clean) < 2:
            add_copper("c", t["net"], {"type": "circle", "center": list(pts[0]), "radius_mm": w / 2})
        else:
            add_copper("c", t["net"], {"type": "polyline", "points": [list(q) for q in clean], "width_mm": w})
    for lay, poly in tc._track_keepouts(b):
        ring = _ring(poly)
        if lay == layer and len(ring) >= 3 and _meets(_box(ring), reach):
            keepouts.append({"id": f"k{len(keepouts) + 1:04d}", "points": [list(q) for q in ring], "clearance_mm": 0.0})
    for z in b.zones():
        if find(z, "keepout") is not None:
            continue
        lays = find(z, "layers") or find(z, "layer")
        if lays is not None and any(str(l) in (layer, "*.Cu", "F&B.Cu") for l in lays[1:]):
            net = find(z, "net_name")
            unmodelled.append(f"pour {str(net[1]) if net is not None else ''} on {layer} (reflows; judged by DRC)")

    loops = b.outline_loops(ARC_ERROR)
    if not loops:
        raise KatError("the board outline (Edge.Cuts) does not close")
    outline = _ring(loops[0])
    cutouts = [_ring(l) for l in loops[1:]]

    # rules
    mins = b.board_minimums()
    cl = b._clearance_fn()
    dru = dru_constraints(b._dru_text())
    from ..specctra import DEFAULT_EDGE_CLEARANCE
    edge = max([mins.get("min_copper_edge_clearance", DEFAULT_EDGE_CLEARANCE) or 0.0]
               + [v for _, k, v in dru if k == "edge_clearance" and v]) + ARC_ERROR
    hole_cl = max([mins.get("min_hole_clearance", 0.25) or 0.0] + [v for _, k, v in dru if k == "hole_clearance" and v])
    for name, kind, _ in dru:
        if kind not in SAFE_DRU:
            unmodelled.append(f"custom rule {name!r}: {kind}")
    from ..coupling import net_rules as _kat_rules
    for r in _kat_rules(pcb):
        if r.get("kind") in ("coupling", "plane"):
            unmodelled.append(f"kat net rule {r.get('kind')} ({r.get('nets') or r.get('a', '')})")
    for k in keepouts:
        if k["clearance_mm"] is None:
            k["clearance_mm"] = hole_cl                    # a slot: a conservative keep-out
    if unmodelled and not accept_unmodelled and any(not u.startswith("pour ") for u in unmodelled):
        raise KatError("rules the polisher cannot model: " + "; ".join(u for u in unmodelled if not u.startswith("pour "))
                       + " (accept them to go on; DRC and kat pcb check on the copy still judge)")

    traces = []
    for i, c in enumerate(chains, 1):
        tid = f"t{i:02d}"
        pts = [list(_c(p)) for p in c.pts]
        anchors = []
        for o in copper:
            if net_of[o["id"]] != c.net:
                continue
            obj = _copper_obj(o)
            if any(pc.copper_distance(obj, tuple(e)) <= pc.TOUCH for e in (pts[0], pts[-1])):
                anchors.append(o["id"])
        for hid, ids in pad_holes.items():
            if set(ids) & set(anchors):
                anchors.append(hid)
        for kid, ids in slots.items():
            if set(ids) & set(anchors):
                raise KatError(f"{c.net} ends on a slotted pad: not supported by the polisher yet")
        traces.append({"id": tid, "net": token(c.net), "width_mm": c.width, "points": pts, "anchors": anchors})
        net_of[tid] = c.net
    pair_cl = {}
    for t in traces:
        for o in copper + [x for x in traces if x["id"] != t["id"]]:
            if o["id"] in t["anchors"]:
                continue
            pair_cl[frozenset((t["id"], o["id"]))] = cl(net_of[t["id"]], net_of[o["id"]])
    base = min(pair_cl.values()) if pair_cl else max(mins.get("min_clearance") or 0.0, cl("", ""))
    pairs = [{"a": a, "b": b_, "clearance_mm": v} for k, v in sorted(pair_cl.items(), key=lambda kv: sorted(kv[0]))
             if v > base + 1e-9 for a, b_ in [sorted(k)]]
    widest = max([base, edge, hole_cl, *pair_cl.values()])
    if widest + half > REACH:
        raise KatError(f"a clearance of {widest} mm reaches beyond the {REACH} mm of collected context")

    obj = {"preferred_spacing_mm": round(2 * base, 6) or 0.2, **OBJECTIVES,
           "short_segment_mm": max(0.4, 4 * half)}
    obj["preferred_edge_margin_mm"] = max(obj["preferred_edge_margin_mm"], edge)
    obj.update(objectives or {})
    doc = {"format": pc.FORMAT, "version": 2, "units": "mm",
           "provenance": {"kind": "private-board-extract", "public_fixture": False,
                          "description": f"{len(traces)} traces on {layer}, extracted by kat pcb polish extract"},
           "scope": {"profile": pc.PROFILES[2], "context_complete": True, "electrical_checks": "not-modelled"},
           "layer": layer, "board": {"outline": [list(p) for p in outline], "cutouts": [[list(p) for p in c] for c in cutouts]},
           "movement_region": [[region_box[0], region_box[1]], [region_box[2], region_box[1]],
                               [region_box[2], region_box[3]], [region_box[0], region_box[3]]],
           "rules": {"copper_clearance_mm": base, "edge_clearance_mm": edge, "hole_clearance_mm": hole_cl,
                     "pair_clearances": pairs},
           "traces": traces, "fixed_copper": copper, "holes": holes, "keepouts": keepouts,
           "groups": [{"id": "g01", "trace_ids": [t["id"] for t in traces]}],
           "objectives": {"angle_step_deg": 45, **obj, "weights": {**WEIGHTS, **(weights or {})}},
           "limits": lim}
    text = pc.dumps(doc)
    case = pc.loads(text)                                  # the reader is the judge of the format
    out.write_text(text, encoding="utf-8")
    mapping = {"format": "route-polish-map", "version": 1, "board": str(pcb.resolve()),
               "board_sha256": pc.sha256_of(pcb), "case": out.name, "case_sha256": case.sha256, "layer": layer,
               "transform": "case (x, y) = board (x, -y)",
               "nets": {v: k for k, v in tokens.items()},
               "traces": {t["id"]: {"net": c.net, "width": c.width,
                                    "segments": [str(find(n, "uuid")[1]) for n in c.nodes],
                                    "locked": any(find(n, "locked") is not None for n in c.nodes),
                                    "start": list(c.pts[0]), "end": list(c.pts[-1])}
                          for t, c in zip(traces, chains)},
               "unmodelled": unmodelled}
    mp.write_text(json.dumps(mapping, indent=1) + "\n", encoding="utf-8")
    return {"case": str(out), "map": str(mp), "layer": layer, "order": [c.net for c in chains],
            "held": [{"net": c.net, "from": list(c.pts[0]), "to": list(c.pts[-1])} for c in held],
            "traces": [{"id": t["id"], "net": c.net, "points": len(c.pts), "length": round(c.length, 4),
                        "anchors": len(t["anchors"])} for t, c in zip(traces, chains)],
            "context": {"fixed_copper": len(copper), "holes": len(holes), "keepouts": len(keepouts)},
            "rules": {"copper": base, "edge": edge, "hole": hole_cl, "pairs": len(pairs)},
            "unmodelled": unmodelled}


def _unknown_nets(b: Board, nets: list[str]) -> str | None:
    """An error naming each of ``nets`` (globs) that matches no net on the board, with similar
    names; None when all match. A misspelled net must not silently drop out of a selection."""
    import difflib
    import fnmatch
    known = sorted(({p.net for p in b.pads()} | {t["net"] for t in b.tracks()} | {v["net"] for v in b.vias()}) - {""})
    missing = [g for g in nets if not any(fnmatch.fnmatch(n, g) for n in known)]
    if not missing:
        return None
    hints = sorted({h for g in missing for h in difflib.get_close_matches(g, known, n=3, cutoff=0.5)})
    return f"no net on the board matches {', '.join(missing)}" + (f" (similar: {', '.join(hints)})" if hints else "")


def _why_none(b: Board, nets: list[str], layer: str | None, region) -> str:
    """Why ``nets`` (all existing) gave no track runs: no tracks, or none on the layer / in
    the region."""
    import fnmatch
    tracks = b.tracks()
    mine = [t for t in tracks if any(fnmatch.fnmatch(t["net"], g) for g in nets)]
    if not mine:
        return f"{', '.join(nets)}: the nets exist but have no tracks (route them first)"
    layers = sorted({t["layer"] for t in mine})
    if layer and layer not in layers:
        return f"{', '.join(nets)}: no tracks on {layer} (they run on {', '.join(layers)})"
    if region:
        x0, y0, x1, y1 = region
        return f"{', '.join(nets)}: no track lies wholly inside the region {x0:g},{y0:g},{x1 - x0:g},{y1 - y0:g} (X,Y,W,H)"
    return f"{', '.join(nets)}: only arcs, no straight track runs"


def _nm(x: float) -> Sym:
    """A coordinate to the nanometre (KiCad keeps 6 decimals; ``sexpr.num`` writes 4, which
    would bend exact 45-degree segments and eat small clearance margins)."""
    s = f"{round(x, 6):.6f}".rstrip("0").rstrip(".")
    return Sym("0" if s in ("-0", "") else s)


def _segment(a, b, width, layer, net, lock) -> list:
    from ...schematic import new_uuid
    node = [Sym("segment"), [Sym("start"), _nm(a[0]), _nm(a[1])], [Sym("end"), _nm(b[0]), _nm(b[1])],
            [Sym("width"), num(width)]]
    if lock:
        node.append([Sym("locked"), Sym("yes")])
    return node + [[Sym("layer"), layer], [Sym("net"), net], [Sym("uuid"), new_uuid()]]


def _copper_obj(o: dict) -> pc.Copper:
    s = o["shape"]
    if s["type"] == "circle":
        return pc.Copper(o["id"], o["net"], "circle", center=tuple(s["center"]), radius=s["radius_mm"])
    if s["type"] == "polyline":
        return pc.Copper(o["id"], o["net"], "polyline", [tuple(p) for p in s["points"]], width=s["width_mm"])
    return pc.Copper(o["id"], o["net"], "polygon", [tuple(p) for p in s["points"]])


def _ordered(chains: list, order: list[str] | None) -> list:
    """Neighbour order: by offset across the bundle's mean direction, or ``order`` (nets)."""
    ref = None
    acc = [0.0, 0.0]
    for c in chains:
        v = (c.pts[-1][0] - c.pts[0][0], -(c.pts[-1][1] - c.pts[0][1]))
        if ref is None:
            ref = v
        s = 1 if v[0] * ref[0] + v[1] * ref[1] >= 0 else -1
        acc[0] += s * v[0]
        acc[1] += s * v[1]
    n = math.hypot(*acc) or 1.0
    nx, ny = -acc[1] / n, acc[0] / n

    def offset(c) -> float:
        tot = w = 0.0
        for a, b in zip(c.pts, c.pts[1:]):
            L = math.dist(a, b)
            mx, my = (a[0] + b[0]) / 2, -(a[1] + b[1]) / 2
            tot += L * (mx * nx + my * ny)
            w += L
        return tot / (w or 1.0)
    out = sorted(chains, key=offset)
    if order:
        nets = {c.net for c in chains}
        bad = [n for n in order if n not in nets] + [n for n in nets if n not in order]
        if bad or len(set(order)) != len(order):
            raise KatError(f"--order must name each selected net once (selected: {', '.join(sorted(nets))})")
        out = sorted(out, key=lambda c: order.index(c.net))
    return out


def apply(pcb: Path | str, result: Path | str, mapping: Path | str, out: Path | str, force: bool = False) -> dict:
    """See ``kat.pcb.polish_apply``."""
    from .. import checks
    from . import constraints as cs
    pcb, out, mapping = Path(pcb), Path(out), Path(mapping)
    m = json.loads(mapping.read_text(encoding="utf-8"))
    res = json.loads(Path(result).read_text(encoding="utf-8"))
    if m.get("format") != "route-polish-map" or res.get("format") != "route-polish-result":
        raise KatError("needs a route-polish result and its case's map file")
    if pc.sha256_of(pcb) != m["board_sha256"]:
        raise KatError(f"{pcb} changed since the extraction (or is another board): extract again")
    if res["input_sha256"] != m["case_sha256"]:
        raise KatError("the result was not made from the case this map belongs to")
    if out.resolve() == pcb.resolve():
        raise KatError("apply never writes into the input board: give a new --out")
    if out.exists() and not force:
        raise KatError(f"{out} exists: choose another name or force")
    case_file = mapping.with_name(m["case"])
    case = pc.load(case_file)
    if case.sha256 != m["case_sha256"]:
        raise KatError(f"{case_file} changed since the extraction")
    before_hash = pc.sha256_of(pcb)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pcb, out)
    for suffix in (".kicad_pro", ".kicad_dru", ".kat.json"):
        src = pcb.with_suffix(suffix)
        if src.exists():
            shutil.copyfile(src, out.with_suffix(suffix))
    b = Board(out)
    by_uuid = {t["uuid"]: t["node"] for t in b.tracks()}
    written = {}
    for tr in res["traces"]:
        info = m["traces"][tr["id"]]
        nodes = [by_uuid.get(u) for u in info["segments"]]
        if any(n is None for n in nodes):
            raise KatError(f"{tr['id']} ({info['net']}): its segments are no longer on the board")
        pts = [(round(x, 6), round(-y, 6)) for x, y in tr["points"]]
        if (math.dist(pts[0], info["start"]) > 1e-6 or math.dist(pts[-1], info["end"]) > 1e-6):
            raise KatError(f"{tr['id']}: its ends moved")
        gone = {id(n) for n in nodes}
        b.tree[:] = [n for n in b.tree if id(n) not in gone]
        for a, q in zip(pts, pts[1:]):
            b._insert(_segment(a, q, info["width"], m["layer"], info["net"], info["locked"]))
        written[tr["id"]] = pts
    b.save()
    if pc.sha256_of(pcb) != before_hash:
        raise KatError("the input board changed while applying (it must not)")
    # round trip: what KiCad saved, read back and judged by the polisher's own hard check
    back = Board(out)
    model = cs.Model(case, movable=list(case.traces))
    state = {}
    np = __import__("numpy")
    for tid, pts in written.items():
        info = m["traces"][tid]
        segs = [(t["start"], t["end"]) for t in back.tracks() if t["net"] == info["net"] and t["layer"] == m["layer"]]
        for a, q in zip(pts, pts[1:]):
            if not any(math.dist(a, s) < 1e-5 and math.dist(q, e) < 1e-5 or math.dist(a, e) < 1e-5 and math.dist(q, s) < 1e-5
                       for s, e in segs):
                raise KatError(f"{tid}: segment {a}-{q} not found in the saved board")
        state[tid] = np.array([_c(p) for p in pts])
    for t in case.traces:
        state.setdefault(t, model.orig[t])
    violations = model.violations(state)
    nets = {m["traces"][t]["net"] for t in written}
    old = checks.drc(pcb, parity=False, refill=True)
    new = checks.drc(out, parity=False, refill=True)
    was = tc.by_net(old["violations"] + old["unconnected"], nets)
    now = tc.by_net(new["violations"] + new["unconnected"], nets)
    worse = sorted(n for n in nets if now.get(n, 0) > was.get(n, 0))
    return {"board": str(out), "traces": len(written), "round_trip": {"ok": not violations, "violations": violations},
            "drc": {"before": {"errors": old["errors"], "unconnected": len(old["unconnected"])},
                    "after": {"errors": new["errors"], "unconnected": len(new["unconnected"])},
                    "worse_nets": worse},
            "ok": not violations and not worse and new["errors"] <= old["errors"]
            and len(new["unconnected"]) <= len(old["unconnected"]),
            "next": f"kat pcb check {out} (net rules, full checks) before using it"}


__all__ = ["apply", "dru_constraints", "extract", "map_path"]
