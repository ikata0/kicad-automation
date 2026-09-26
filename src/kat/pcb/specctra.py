"""Specctra DSN writer and SES reader for autorouting (Freerouting), without pcbnew.

Every footprint becomes its own image whose pins sit at their board positions relative to
the footprint origin, with padstacks carrying the rotated pad outline on the pad's own
copper layers; every component is placed "front, 0". So no rotation or mirroring is left
for the router to interpret. Specctra's y axis points up: y is negated both ways.
Units: um with resolution 10 (as KiCad writes them).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .. import env
from ..sexpr import find, find_all, head, loads
from .board import Board

RES = 10  # SES coordinates per um


def _q(s: str) -> str:
    """Quote a token unless it is a plain word."""
    return s if re.fullmatch(r"[A-Za-z0-9_.+\-/]+", s) and not s.startswith("-") else '"' + s.replace('"', "'") + '"'


def _um(v: float) -> str:
    return f"{v * 1000:.1f}".rstrip("0").rstrip(".")


def _xy(x: float, y: float) -> str:
    return f"{_um(x)} {_um(-y)}"


def _via_name(size: float, drill: float) -> str:
    return f"Via[0-1]_{_um(size)}:{_um(drill)}_um"


def _outline_points(b: Board) -> list[tuple[float, float]]:
    for n in b.tree:
        if head(n) == "gr_poly" and str(find(n, "layer")[1]) == "Edge.Cuts":
            return [(float(p[1]), float(p[2])) for p in find_all(find(n, "pts"), "xy")]
    box = b.outline_box()
    if not box:
        raise env.KatError("routing needs a board outline")
    return [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]


def _zone_polygon(z) -> list[tuple[float, float]]:
    return [(float(p[1]), float(p[2])) for p in find_all(find(find(z, "polygon"), "pts"), "xy")]


def _zone_layers(z) -> list[str]:
    lay = find(z, "layer")
    return [str(lay[1])] if lay is not None else [str(t) for t in find(z, "layers")[1:]]


def _poly(layer: str, pts, width: float = 0) -> str:
    return f"(polygon {_q(layer)} {_um(width)} " + " ".join(_xy(x, y) for x, y in pts) + ")"


def write_dsn(b: Board, path: Path, nets: list[str] | None = None) -> dict:
    """Write the board as a Specctra design. ``nets``: route only these (the others stay
    as they are; their pads are still obstacles)."""
    copper = b.copper_layers()
    lines = [f"(pcb {_q(b.path.stem)}",
             '  (parser (string_quote ") (space_in_quoted_tokens on) (host_cad "kat") (host_version "1"))',
             "  (resolution um 10)", "  (unit um)", "  (structure"]
    types = b.layer_types()
    for i, layer in enumerate(copper):
        kind = "power" if types.get(layer) == "power" else "signal"   # no tracks on planes
        lines.append(f"    (layer {_q(layer)} (type {kind}) (property (index {i})))")
    outline = _outline_points(b)
    lines.append("    (boundary (path pcb 0 " + " ".join(_xy(x, y) for x, y in outline + outline[:1]) + "))")
    for z in b.zones():
        pts = _zone_polygon(z)
        keep = find(z, "keepout")
        if keep is not None:
            rule = {str(k[0]): str(k[1]) for k in keep[1:] if isinstance(k, list)}
            for layer in _zone_layers(z):
                if layer not in copper:
                    continue
                if rule.get("tracks") == "not_allowed":
                    lines.append(f'    (wire_keepout "" {_poly(layer, pts)})')
                if rule.get("vias") == "not_allowed":
                    lines.append(f'    (via_keepout "" {_poly(layer, pts)})')
            continue
        net = find(z, "net")
        if net is not None and str(net[1]):
            for layer in _zone_layers(z):
                if layer in copper:
                    lines.append(f"    (plane {_q(str(net[1]))} {_poly(layer, pts)})")
    default = b.net_rules("")
    lines.append(f'    (via "{_via_name(default["via"], default["via_drill"])}")')
    lines.append(f"    (rule (width {_um(default['track'])}) (clearance {_um(default['clearance'])})"
                 f" (clearance 50 (type smd_smd)))")
    lines.append("  )")

    # placement: one image per footprint, placed front at 0 degrees
    fps = b.footprints()
    lines.append("  (placement")
    for f in fps:
        lines.append(f"    (component {_q('img_' + f.ref)} (place {_q(f.ref)} {_xy(f.x, f.y)} front 0))")
    lines.append("  )")

    padstacks: dict[str, str] = {}
    lines.append("  (library")
    for f in fps:
        pins = []
        for p, pid in _pins(f, copper):
            name = _padstack(p, [l for l in copper if p.on(l)], padstacks)
            pins.append(f"      (pin {_q(name)} {_q(pid)} {_xy(p.x - f.x, p.y - f.y)})")
        lines.append(f"    (image {_q('img_' + f.ref)}")
        lines += pins
        lines.append("    )")
    for name, text in padstacks.items():
        lines.append(text)
    vias = {(r["via"], r["via_drill"]) for r in [default] + [b.net_rules(n) for n in _net_names(b)]}
    vias |= {(v["size"], v["drill"]) for v in b.vias()}
    for size, drill in sorted(vias):
        shapes = " ".join(f"(shape (circle {_q(l)} {_um(size)}))" for l in copper)
        lines.append(f'    (padstack "{_via_name(size, drill)}" {shapes} (attach off))')
    lines.append("  )")

    # network: nets (with pins) and classes (rules per net class)
    pins_of: dict[str, list[str]] = {}
    for f in fps:
        for p, pid in _pins(f, copper):
            if p.net:
                pins_of.setdefault(p.net, []).append(f"{f.ref}-{pid}")
    routed = [n for n in sorted(pins_of) if nets is None or n in nets]
    lines.append("  (network")
    for n in routed:
        lines.append(f"    (net {_q(n)} (pins " + " ".join(_q(p) for p in pins_of[n]) + "))")
    classes: dict[str, list[str]] = {}
    for n in routed:
        classes.setdefault(b.net_rules(n)["netclass"], []).append(n)
    for cname, members in classes.items():
        r = b.net_rules(members[0])
        lines.append(f"    (class {_q(cname)} " + " ".join(_q(n) for n in members) +
                     f' (circuit (use_via "{_via_name(r["via"], r["via_drill"])}"))'
                     f" (rule (width {_um(r['track'])}) (clearance {_um(r['clearance'])})))")
    lines.append("  )")

    # existing copper: locked = protected, the rest may be changed by the router
    lines.append("  (wiring")
    for t in b.tracks():
        if t["kind"] != "segment" or not t["net"]:
            continue
        kind = "protect" if t["locked"] or (nets is not None and t["net"] not in nets) else "route"
        lines.append(f"    (wire (path {_q(t['layer'])} {_um(t['width'])} {_xy(*t['start'])} {_xy(*t['end'])})"
                     f" (net {_q(t['net'])}) (type {kind}))")
    for v in b.vias():
        if not v["net"]:
            continue
        kind = "protect" if v["locked"] or (nets is not None and v["net"] not in nets) else "route"
        lines.append(f'    (via "{_via_name(v["size"], v["drill"])}" {_xy(*v["at"])}'
                     f" (net {_q(v['net'])}) (type {kind}))")
    lines.append("  )")
    lines.append(")")
    path.write_text("\n".join(lines) + "\n")
    return {"dsn": str(path), "nets": len(routed), "footprints": len(fps), "padstacks": len(padstacks)}


def _pins(f, copper: list[str]) -> list[tuple]:
    """(pad, Specctra pin id) of a footprint's copper pads; repeated pad numbers (split
    thermal pads, shields) get unique ids 5, 5@1, 5@2, ..."""
    out, seen = [], {}
    for p in f.pads():
        if not p.number or p.kind == "np_thru_hole" or not any(p.on(l) for l in copper):
            continue
        k = seen[p.number] = seen.get(p.number, -1) + 1
        out.append((p, p.number if k == 0 else f"{p.number}@{k}"))
    return out


def _net_names(b: Board) -> set[str]:
    return {p.net for p in b.pads() if p.net}


def _padstack(p, layers: list[str], cache: dict) -> str:
    """Padstack for a pad: circle for round pads, else its rotated outline as polygon."""
    ang = round(p.angle % 360, 3)
    if p.shape == "circle" or (p.shape == "oval" and abs(p.w - p.h) < 1e-6):
        name = f"Round_{_um(p.w)}_{'-'.join(layers)}"
        if name not in cache:
            shapes = " ".join(f"(shape (circle {_q(l)} {_um(p.w)}))" for l in layers)
            cache[name] = f"    (padstack {_q(name)} {shapes} (attach off))"
        return name
    name = f"{p.shape}_{_um(p.w)}x{_um(p.h)}_a{ang:g}_{'-'.join(layers)}"
    if name not in cache:
        pts = [(x - p.x, y - p.y) for x, y in p.outline]
        shapes = " ".join("(shape " + _poly(l, pts + pts[:1]) + ")" for l in layers)
        cache[name] = f"    (padstack {_q(name)} {shapes} (attach off))"
    return name


def read_ses(path: Path) -> dict:
    """Wires and vias of a Freerouting session, in board mm (y down)."""
    tree = loads(Path(path).read_text(encoding="utf-8"))
    routes = find(tree, "routes")
    if routes is None:
        raise env.KatError(f"{path}: no routes in session")
    res = find(routes, "resolution")
    per_mm = float(res[2]) * 1000 if res is not None else RES * 1000   # units per mm
    wires, vias = [], []
    for net in find_all(find(routes, "network_out") or [], "net"):
        name = str(net[1])
        for w in find_all(net, "wire"):
            path_ = find(w, "path")
            layer, width = str(path_[1]), float(path_[2]) / per_mm
            coords = [float(v) for v in path_[3:] if not isinstance(v, list)]
            pts = [(coords[i] / per_mm, -coords[i + 1] / per_mm) for i in range(0, len(coords) - 1, 2)]
            for a, b in zip(pts, pts[1:]):
                if math.dist(a, b) > 1e-6:
                    wires.append({"net": name, "layer": layer, "width": width, "start": a, "end": b})
        for v in find_all(net, "via"):
            m = re.search(r"_([\d.]+):([\d.]+)_um", str(v[1]))
            size, drill = (float(m.group(1)) / 1000, float(m.group(2)) / 1000) if m else (0.6, 0.3)
            vias.append({"net": name, "at": (float(v[2]) / per_mm, -float(v[3]) / per_mm),
                         "size": size, "drill": drill})
    return {"wires": wires, "vias": vias}
