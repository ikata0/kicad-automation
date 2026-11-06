"""Board setup: outline, copper layers, board-only parts, pours, keep-outs, net classes and
custom DRC rules. Mixed into ``Board``; project-file and rule changes are written by
``Board.save()`` together with the board, so an edit block still saves once."""

from __future__ import annotations

import json
import math

from .. import env
from ..schematic import new_uuid
from ..sexpr import Sym, find, find_all, head, num

HOLES = {"M2": "MountingHole:MountingHole_2.2mm_M2", "M2.5": "MountingHole:MountingHole_2.7mm_M2.5",
         "M3": "MountingHole:MountingHole_3.2mm_M3", "M4": "MountingHole:MountingHole_4.3mm_M4"}
EDGE_WIDTH = 0.05
PAD_CONNECT = {"thermal": None, "solid": "yes", "none": "no", "thru_hole_only": "thru_hole_only"}
# KiCad 10's own Default net class (as it writes it into a .kicad_pro, net_settings v5)
DEFAULT_CLASS = {"bus_width": 12, "clearance": 0.2, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
                 "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3,
                 "microvia_drill": 0.1, "name": "Default", "pcb_color": "rgba(0, 0, 0, 0.000)",
                 "priority": 2147483647, "schematic_color": "rgba(0, 0, 0, 0.000)",
                 "track_width": 0.2, "tuning_profile": "", "via_diameter": 0.6, "via_drill": 0.3,
                 "wire_width": 6}

Point = tuple[float, float]


def _pts(points) -> list:
    return [Sym("pts")] + [[Sym("xy"), num(x), num(y)] for x, y in points]


def _rect(x: float, y: float, w: float, h: float) -> list[Point]:
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def _edge(kind: str, **pts) -> list:
    node = [Sym(kind)] + [[Sym(k), num(p[0]), num(p[1])] for k, p in pts.items()]
    return node + [[Sym("stroke"), [Sym("width"), num(EDGE_WIDTH)], [Sym("type"), Sym("default")]],
                   [Sym("layer"), "Edge.Cuts"], [Sym("uuid"), new_uuid()]]


def _zone_spec(node: list) -> tuple:
    """What a zone was asked for (net, layers, rules, outline), in comparable form -
    independent of the uuid, the fill and KiCad's reformatting."""
    def val(n, *path):
        for p in path:
            n = find(n, p) if n is not None else None
        return None if n is None else tuple(_norm(t) for t in n[1:] if not isinstance(t, list))
    lay = find(node, "layers") or find(node, "layer")
    ko = find(node, "keepout")
    pts = find(find(node, "polygon"), "pts")
    return (val(node, "net"), tuple(str(t) for t in lay[1:]), val(node, "priority"),
            val(node, "connect_pads"), val(node, "connect_pads", "clearance"), val(node, "min_thickness"),
            val(node, "fill", "thermal_gap"), val(node, "fill", "thermal_bridge_width"),
            tuple((str(k[0]), str(k[1])) for k in ko[1:]) if ko is not None else None,
            tuple((_norm(p[1]), _norm(p[2])) for p in pts[1:]))


def _norm(t):
    try:
        return round(float(str(t)), 4)
    except ValueError:
        return str(t)


class SetupMixin:
    # --- outline -----------------------------------------------------------------------

    def clear_outline(self) -> None:
        """Remove every drawing on Edge.Cuts."""
        for n in [n for n in self.tree if head(n) and head(n).startswith("gr_")
                  and find(n, "layer") is not None and str(find(n, "layer")[1]) == "Edge.Cuts"]:
            self.tree.remove(n)

    def outline(self, rect: tuple[float, float, float, float] | None = None, radius: float = 0.0,
                polygon: list[Point] | None = None) -> dict:
        """Replace the board outline: ``rect`` (x, y, w, h) with optional corner ``radius``,
        or a ``polygon``. Everything on Edge.Cuts is replaced."""
        self.clear_outline()
        items = []
        if polygon:
            items.append([Sym("gr_poly"), _pts(polygon),
                          [Sym("stroke"), [Sym("width"), num(EDGE_WIDTH)], [Sym("type"), Sym("default")]],
                          [Sym("fill"), Sym("no")], [Sym("layer"), "Edge.Cuts"], [Sym("uuid"), new_uuid()]])
        elif rect:
            x, y, w, h = rect
            r = min(radius, w / 2, h / 2)
            if r <= 0:
                node = _edge("gr_rect", start=(x, y), end=(x + w, y + h))
                node.insert(-2, [Sym("fill"), Sym("no")])
                items.append(node)
            else:
                x1, y1 = x + w, y + h
                items += [_edge("gr_line", start=(x + r, y), end=(x1 - r, y)),
                          _edge("gr_line", start=(x1, y + r), end=(x1, y1 - r)),
                          _edge("gr_line", start=(x1 - r, y1), end=(x + r, y1)),
                          _edge("gr_line", start=(x, y1 - r), end=(x, y + r))]
                k = r * (1 - math.cos(math.pi / 4))
                for (cx, cy, sx, sy, mx, my, ex, ey) in (
                        (x + r, y + r, x, y + r, x + k, y + k, x + r, y),
                        (x1 - r, y + r, x1 - r, y, x1 - k, y + k, x1, y + r),
                        (x1 - r, y1 - r, x1, y1 - r, x1 - k, y1 - k, x1 - r, y1),
                        (x + r, y1 - r, x + r, y1, x + k, y1 - k, x, y1 - r)):
                    items.append(_edge("gr_arc", start=(sx, sy), mid=(mx, my), end=(ex, ey)))
        else:
            raise env.KatError("outline needs rect=(x, y, w, h) or polygon=[(x, y), ...]")
        for n in items:
            self._insert(n)
        return {"outline": list(self.outline_box())}

    # --- layers ------------------------------------------------------------------------

    def copper_layers(self) -> list[str]:
        """Copper layer names, top to bottom (F.Cu, In1.Cu, ..., B.Cu)."""
        return [str(l[1]) for l in find(self.tree, "layers")[1:] if str(l[1]).endswith(".Cu")]

    def set_copper_layers(self, n: int) -> dict:
        """2, 4, 6, ... copper layers; inner layers In1.Cu... are added after F.Cu or
        removed (refused while anything is on them)."""
        if n < 2 or n % 2:
            raise env.KatError("copper layer count must be even and at least 2")
        layers = find(self.tree, "layers")
        inner = [l for l in layers[1:] if str(l[1]).startswith("In")]
        want = [f"In{i}.Cu" for i in range(1, n - 1)]
        for l in inner:
            if str(l[1]) not in want:
                if self._used_layer(str(l[1])):
                    raise env.KatError(f"{l[1]} is in use; clear it before removing the layer")
                layers.remove(l)
        have = {str(l[1]) for l in layers[1:]}
        idx = next(i for i, l in enumerate(layers) if isinstance(l, list) and str(l[1]) == "F.Cu") + 1
        for i, name in enumerate(want, 1):
            if name not in have:
                layers.insert(idx + i - 1, [Sym(str(2 + 2 * i)), name, Sym("signal")])
        return {"copper_layers": self.copper_layers()}

    def layer_types(self) -> dict[str, str]:
        """Copper layer -> KiCad layer type (signal, power, mixed, jumper)."""
        return {str(l[1]): str(l[2]) for l in find(self.tree, "layers")[1:] if str(l[1]).endswith(".Cu")}

    def set_layer_type(self, layer: str, kind: str = "power") -> dict:
        """``power`` marks a plane layer: the router lays no tracks on it, so the plane
        stays whole (Specctra layer type power)."""
        if kind not in ("signal", "power", "mixed", "jumper"):
            raise env.KatError("layer type: signal, power, mixed or jumper")
        for l in find(self.tree, "layers")[1:]:
            if str(l[1]) == layer:
                l[2] = Sym(kind)
                return {"layer": layer, "type": kind}
        raise env.KatError(f"no copper layer {layer!r}; set_copper_layers first")

    def set_stackup(self, dielectrics: list[float], copper: float | list[float] = 0.035,
                    epsilon_r: float | list[float] = 4.5, material: str = "FR4") -> dict:
        """Write the stack-up: ``dielectrics`` thicknesses in mm top to bottom (one fewer
        than copper layers), ``copper`` thickness in mm (one value or one per layer),
        ``epsilon_r`` (one or per dielectric). Dielectrics alternate prepreg / core, the
        outer ones prepreg (the type is a label; kat uses thickness, εr and material).
        Returns the stack-up as ``kat pcb stack`` reads it."""
        from . import layers
        return layers.write_stackup(self, dielectrics, copper, epsilon_r, material)

    def plane(self, net: str, layer: str, clearance: float = 0.3) -> dict:
        """A whole-board plane of ``net`` on ``layer`` (usually an inner layer): layer type
        power plus a pour over the outline. SMD pads reach it through ``fanout``."""
        self.set_layer_type(layer, "power")
        return self.zone(net, layer, clearance=clearance, name=f"{net} plane")

    def _used_layer(self, layer: str) -> bool:
        def walk(n):
            if isinstance(n, list):
                if head(n) in ("layer", "layers") and layer in [str(t) for t in n[1:]]:
                    return True
                return any(walk(c) for c in n if isinstance(c, list))
            return False
        return any(walk(n) for n in self.tree if head(n) != "layers")

    # --- board-only parts ----------------------------------------------------------------

    def add_board_part(self, lib_id: str, ref: str, at: Point, rot: float = 0.0, side: str = "F",
                       value: str | None = None):
        """A footprint that has no schematic symbol (mounting hole, fiducial, logo). Marked
        board-only, so board sync and schematic parity leave it alone."""
        f = self.add_footprint(lib_id, ref, at[0], at[1], rot, side, value)
        attr = find(f.node, "attr")
        if attr is None:
            attr = [Sym("attr")]
            f.node.insert(f.node.index(find_all(f.node, "property")[-1]) + 1, attr)
        for flag in ("board_only", "exclude_from_pos_files", "exclude_from_bom"):
            if flag not in [str(t) for t in attr[1:]]:
                attr.append(Sym(flag))
        return f

    def mounting_hole(self, at: Point, size: str = "M3", ref: str | None = None) -> dict:
        """A board-only mounting hole (``M2``, ``M2.5``, ``M3``, ``M4``) centred at ``at``
        (mm). Safe to run again: a hole already at that spot (or with that ``ref``) is kept
        when its size matches, else replaced. Returns ``ref``, ``at``, ``size`` and
        ``status`` (created / replaced / unchanged)."""
        if size not in HOLES:
            raise env.KatError(f"size one of {', '.join(HOLES)}")
        holes = [f for f in self.footprints() if f.board_only and f.lib_id in HOLES.values()]
        old = next((f for f in holes if (ref and f.ref == ref) or
                    (abs(f.x - at[0]) < 0.01 and abs(f.y - at[1]) < 0.01)), None)
        status = "created"
        if old is not None:
            if old.lib_id == HOLES[size] and abs(old.x - at[0]) < 0.01 and abs(old.y - at[1]) < 0.01:
                return {"ref": old.ref, "at": [old.x, old.y], "size": size, "status": "unchanged"}
            ref = old.ref
            self.remove_footprint(ref)
            status = "replaced"
        used = {f.ref for f in self.footprints()}
        ref = ref or next(f"H{i}" for i in range(1, 1000) if f"H{i}" not in used)
        f = self.add_board_part(HOLES[size], ref, at, value=f"{size} hole")
        return {"ref": f.ref, "at": [f.x, f.y], "size": size, "status": status}

    # --- zones ---------------------------------------------------------------------------

    def _put_zone(self, node: list, name: str) -> str:
        """Insert a zone, or replace the zone of the same name (kept as it is, fill
        included, when its settings and polygon are unchanged)."""
        old = next((z for z in self.zones() if find(z, "name") is not None
                    and str(find(z, "name")[1]) == name), None)
        if old is None:
            self._insert(node)
            return "created"
        if _zone_spec(old) == _zone_spec(node):
            return "unchanged"
        find(node, "uuid")[1] = find(old, "uuid")[1]
        self.tree[self.tree.index(old)] = node
        return "replaced"

    def _zone(self, layers: list[str], polygon: list[Point], name: str = "") -> list:
        lay = [Sym("layer"), layers[0]] if len(layers) == 1 else [Sym("layers")] + layers
        node = [Sym("zone"), lay, [Sym("uuid"), new_uuid()]]
        if name:
            node.append([Sym("name"), name])
        node.append([Sym("hatch"), Sym("edge"), Sym("0.5")])
        return node

    def zone(self, net: str, layers: str | list[str], polygon: list[Point] | None = None,
             clearance: float = 0.3, min_width: float = 0.25, thermal_gap: float = 0.5,
             spoke: float = 0.5, priority: int = 0, name: str = "", pads: str = "thermal") -> dict:
        """A copper pour of ``net`` on ``layers``; ``polygon`` (mm) defaults to the outline
        shrunk by 0.5 mm. ``clearance``, ``min_width``, ``thermal_gap`` and ``spoke`` (thermal
        spoke width) in mm; a higher ``priority`` pours first where zones overlap. ``pads``: how
        it joins its net's pads, ``thermal``, ``solid``, ``none`` or ``thru_hole_only`` (thermal
        for through-hole pads, solid for the others); ``pad_connection`` overrides it per pad.
        Filled by
        KiCad (``refill``/``kat pcb check``). Safe to run again: a zone of the same ``name``
        (default ``"{net} {layers}"``) is replaced, or kept when unchanged. Several pours of one
        net on one layer need distinct names. Returns ``zone`` (name), ``net``, ``layers``,
        ``status``."""
        layers = [layers] if isinstance(layers, str) else list(layers)
        if pads not in PAD_CONNECT:
            raise env.KatError(f"pads {pads!r}: {', '.join(PAD_CONNECT)}")
        name = name or f"{net} {'+'.join(layers)}"
        if polygon is None:
            b = self.outline_box()
            if not b:
                raise env.KatError("no outline: give a polygon or draw the outline first")
            polygon = _rect(b[0] + 0.5, b[1] + 0.5, b[2] - b[0] - 1, b[3] - b[1] - 1)
        node = self._zone(layers, polygon, name)
        node[1:1] = [[Sym("net"), net]]
        if priority:
            node.append([Sym("priority"), Sym(str(priority))])
        mode = [Sym(PAD_CONNECT[pads])] if PAD_CONNECT[pads] else []
        node += [[Sym("connect_pads"), *mode, [Sym("clearance"), num(clearance)]],
                 [Sym("min_thickness"), num(min_width)],
                 [Sym("fill"), Sym("yes"), [Sym("thermal_gap"), num(thermal_gap)],
                  [Sym("thermal_bridge_width"), num(spoke)], [Sym("island_removal_mode"), Sym("0")]],
                 [Sym("polygon"), _pts(polygon)]]
        status = self._put_zone(node, name)
        return {"zone": name, "net": net, "layers": layers, "status": status}

    def pad_connection(self, pads: str | list[str], mode: str | None) -> dict:
        """How pads join a zone of their net, whatever the zone says: ``pads`` ``"U1.8"`` (all
        pads of that number), ``"U1"`` (the footprint's default, which its pads without an
        override of their own follow) or a list, ``mode`` ``thermal``, ``solid``, ``none``,
        ``thru_hole_only``, or None to follow the zone again. Stamps carry a block's
        overrides. Returns ``pads`` (those set) and ``mode``."""
        done = []
        for name in [pads] if isinstance(pads, str) else pads:
            ref, dot, number = name.partition(".")
            if not dot:
                self.footprint(ref).set_zone_connection(mode)
                done.append(name)
                continue
            hit = [p for p in self.footprint(ref).pads() if p.number == number]
            if not hit:
                raise env.KatError(f"{ref} has no pad {number!r}")
            for p in hit:
                p.set_zone_connection(mode)
            done.append(name)
        return {"pads": done, "mode": mode}

    def keepout(self, layers: str | list[str], polygon: list[Point] | None = None,
                rect: tuple[float, float, float, float] | None = None, tracks: bool = False,
                vias: bool = True, pads: bool = True, pour: bool = True, footprints: bool = True,
                name: str = "") -> dict:
        """A rule area on ``layers``, given as ``polygon`` or ``rect`` (x, y, w, h in mm):
        ``False`` forbids that kind of item inside (default: tracks only). Note: Freerouting
        counts every via inside a track keep-out as a violation. Safe to run again: a keep-out
        of the same ``name`` (default ``"keepout {layers} {box}"``) is replaced, or kept when
        unchanged. Returns ``keepout`` (name), ``layers``, ``status``."""
        layers = [layers] if isinstance(layers, str) else list(layers)
        polygon = polygon or (_rect(*rect) if rect else None)
        if not polygon:
            raise env.KatError("keepout needs polygon or rect")
        if not name:
            xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
            name = (f"keepout {'+'.join(layers)} {min(xs):g},{min(ys):g}-{max(xs):g},{max(ys):g}")
        node = self._zone(layers, polygon, name)
        ok = lambda v: Sym("allowed" if v else "not_allowed")
        node += [[Sym("connect_pads"), [Sym("clearance"), Sym("0")]],
                 [Sym("min_thickness"), Sym("0.25")],
                 [Sym("keepout"), [Sym("tracks"), ok(tracks)], [Sym("vias"), ok(vias)],
                  [Sym("pads"), ok(pads)], [Sym("copperpour"), ok(pour)],
                  [Sym("footprints"), ok(footprints)]],
                 [Sym("fill"), [Sym("thermal_gap"), Sym("0.5")], [Sym("thermal_bridge_width"), Sym("0.5")]],
                 [Sym("polygon"), _pts(polygon)]]
        status = self._put_zone(node, name)
        return {"keepout": name, "layers": layers, "status": status}

    def keepout_around(self, nets: list[str] | None = None, uuids: list[str] | None = None,
                       refs: list[str] | None = None, margin: float = 0.2, scope: str | list[str] = "same",
                       forbid: tuple[str, ...] = ("pour",), name: str = "") -> dict:
        """Keep-outs derived from copper: one rule area per selected item, ``margin`` mm around
        it (straight runs of a track count as one). Selected: tracks and vias of ``nets``, or
        by ``uuids``, and pads in ``refs`` (``U1`` or ``U1.8``). ``scope``: the layers each goes
        on, ``"same"`` (the item's own), ``"adjacent"`` (the copper layers next to it), ``"span"``
        (all from the selection's top layer to its bottom layer) or a list of layers (a
        sub-laminate). ``forbid``: what may not be inside, of tracks, vias, pads, pour,
        footprints. Forbidding copper on a layer holding the selection makes that copper itself
        a DRC error (``items_not_allowed``): ``warnings`` says so. ``name``: a prefix, areas are
        ``NAME:1``, ...; running it again replaces those of that name. Returns ``keepouts``
        (names), ``layers``, ``removed`` (old ones replaced), ``warnings``."""
        from .coupling import _arc_points, _pt
        from .geometry import capsule_points, circle_points, hull
        bad = set(forbid) - {"tracks", "vias", "pads", "pour", "footprints"}
        if bad:
            raise env.KatError(f"forbid {', '.join(sorted(bad))}: of tracks, vias, pads, pour, footprints")
        if margin < 0:
            raise env.KatError("margin must be >= 0 mm")
        copper = self.copper_layers()
        ids, want = set(uuids or ()), set(nets or ())
        err = 0.02
        items: list[tuple[str, list]] = []             # (source layer, polygon)
        runs: list[dict] = []
        used = {"tracks": set(), "vias": set(), "pads": set()}
        for t in self.tracks():
            if t["net"] not in want and t["uuid"] not in ids:
                continue
            used["tracks"].add(t["layer"])
            r = t["width"] / 2 + margin
            pts = (_arc_points(t["start"], _pt(find(t["node"], "mid")), t["end"]) if t["kind"] == "arc"
                   else [t["start"], t["end"]])
            runs += [{"layer": t["layer"], "r": r, "a": a, "b": b} for a, b in zip(pts, pts[1:])]
        for run in _straight_runs(runs):
            items.append((run["layer"], capsule_points(run["a"], run["b"], run["r"], err)))
        for v in self.vias():
            if v["net"] in want or v["uuid"] in ids:
                used["vias"].update(v["span"])
                poly = circle_points(*v["at"], v["size"] / 2 + margin, err)
                items += [(l, poly) for l in v["span"]]
        for r in refs or ():
            ref, _, number = r.partition(".")
            pads = [p for p in self.footprint(ref).pads() if not number or p.number == number]
            if not pads:
                raise env.KatError(f"{ref} has no pad {number!r}")
            for p in pads:
                on = [l for l in copper if p.on(l)]
                used["pads"].update(on)
                for poly in p.polygons(err):
                    grown = hull(q for c in poly for q in circle_points(*c, margin, err)) if margin else poly
                    items += [(l, grown) for l in on]
        if not items:
            raise env.KatError("keepout_around: nothing selected (no such nets, uuids or pads)")
        if isinstance(scope, str):
            if scope not in ("same", "adjacent", "span"):
                raise env.KatError(f"scope {scope!r}: same, adjacent, span or a list of layers")
            top = min(copper.index(l) for l, _ in items)
            bottom = max(copper.index(l) for l, _ in items)

            def targets(layer):
                i = copper.index(layer)
                return {"same": [layer], "adjacent": copper[max(i - 1, 0):i] + copper[i + 1:i + 2],
                        "span": copper[top:bottom + 1]}[scope]
        else:
            if missing := [l for l in scope if l not in copper]:
                raise env.KatError(f"no copper layer {', '.join(missing)} on this board")
            targets = lambda layer: list(scope)                             # noqa: E731
        areas: dict[tuple, set] = {}
        for layer, poly in items:
            areas.setdefault(tuple(poly), set()).update(targets(layer))
        name = name or "around " + "+".join(sorted(want) or sorted(refs or ()) or ["selection"])
        old = [z for z in self.zones() if (n := find(z, "name")) is not None and str(n[1]).startswith(f"{name}:")]
        for z in old:
            self.tree.remove(z)
        made, layers = [], set()
        for k, (poly, on) in enumerate(sorted(areas.items(), key=lambda e: e[0]), 1):
            if not on:
                continue
            order = sorted(on, key=copper.index)
            self.keepout(order, polygon=list(poly), tracks="tracks" not in forbid, vias="vias" not in forbid,
                         pads="pads" not in forbid, pour="pour" not in forbid,
                         footprints="footprints" not in forbid, name=f"{name}:{k}")
            made.append(f"{name}:{k}")
            layers |= on
        clash = [f"{kind} of the selection on {', '.join(sorted(used[kind] & layers, key=copper.index))}"
                 for kind in used if kind in forbid and used[kind] & layers]
        return {"keepouts": made, "layers": sorted(layers, key=copper.index), "removed": len(old),
                **({"warnings": [f"these areas forbid what they surround, so DRC will report it "
                                 f"(items_not_allowed): {'; '.join(clash)}"]} if clash else {})}

    # --- net classes and rules (project files, written on save) ---------------------------

    def netclass(self, name: str, track: float, clearance: float, via: float = 0.6,
                 via_drill: float = 0.3, nets: list[str] | None = None,
                 layers: list[str] | None = None) -> dict:
        """Define or change a net class in the .kicad_pro; ``nets`` are net-name patterns
        (``GND``, ``/LED_*``) assigned to it. ``Default`` changes the default class.
        ``layers``: the copper layers the router may use for the class (kept in
        ``<board>.kat.json``; KiCad has no such setting, DRC does not check it)."""
        self._pro_edits.append(("netclass", dict(name=name, track=track, clearance=clearance,
                                                 via=via, via_drill=via_drill, nets=nets or [])))
        if layers is not None:
            bad = [l for l in layers if l not in self.copper_layers()]
            if bad:
                raise env.KatError(f"no copper layer {', '.join(bad)} on this board")
            self._route_edits.setdefault("layers", {})[name] = list(layers)
        return {"netclass": name, **({"layers": list(layers)} if layers is not None else {})}

    def class_clearance(self, a: str, b: str, clearance: float) -> dict:
        """Keep net classes ``a`` and ``b`` at least ``clearance`` mm apart: a generated DRC
        rule (``kat class A B``) and the router's class-to-class rule."""
        key = " ".join(sorted((a, b)))
        cond = (f"(A.NetClass == '{a}' && B.NetClass == '{b}') || "
                f"(A.NetClass == '{b}' && B.NetClass == '{a}')")
        self._gen_rules[f"kat class {key}"] = (f'(rule "kat class {key}" (condition "{cond}") '
                                               f'(constraint clearance (min {clearance:g}mm)))')
        self._route_edits.setdefault("class_clearance", {})[key] = clearance
        return {"classes": [a, b], "clearance": clearance}

    def route_vias(self, spans: list[tuple], through: bool = True) -> dict:
        """Which vias the router may lay (default: through vias only): ``spans`` entries
        (top, bottom), optionally with kind, size and drill mm, e.g. ``("F.Cu", "In1.Cu",
        "micro")`` or ``("In1.Cu", "In2.Cu", "buried", 0.45, 0.2)`` (kind as ``via_kind``; size
        and drill default to the class's via or µvia); the router prefers these. ``through=False``:
        no through vias: tracks stay on the layers the spans reach, and a net the router still
        joins by a through via gets none of its new copper (``disallowed_vias`` in the result, the
        net unconnected). Freerouting joins layers with one via only, so it does not stack blind
        and buried vias. Stored with the router's other settings. Returns the setting."""
        from .structure import via_kind
        copper = self.copper_layers()
        out = []
        for s in spans:
            top, bottom, kind, size, drill = (*s, None, None, None)[:5]
            kind, span = via_kind(top, bottom, copper, kind)
            out.append({"layers": list(span), "kind": kind, **({"size": size} if size else {}),
                        **({"drill": drill} if drill else {})})
        self._route_edits["vias"] = {"spans": out, "through": bool(through)}
        return self._route_edits["vias"]

    def route_settings(self) -> dict:
        """What the router gets beyond KiCad's classes: ``layers`` per class,
        ``class_clearance`` ({"A B": mm}) and ``vias`` (``route_vias``), with pending changes."""
        from ..syncer_hier import settings_file
        f = settings_file(self.path)
        saved = (json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}).get("route", {})
        out = {k: dict(saved.get(k, {})) for k in ("layers", "class_clearance", "vias")}
        for k, v in self._route_edits.items():
            out[k].update(v)
        if not out["vias"]:
            del out["vias"]
        return out

    def _write_route_settings(self) -> None:
        if not self._route_edits:
            return
        from ..syncer_hier import remember
        remember(self.path, route=self.route_settings())
        self._route_edits = {}

    def rules(self, text: str) -> dict:
        """Custom DRC rules (KiCad's rule language) for the .kicad_dru, replacing the earlier
        custom rules (kat's generated ones, named ``kat ...``, stay):
        ``(rule "hv" (condition "A.NetClass == 'HV'") (constraint clearance (min 2mm)))``.
        A matching clearance rule replaces the classes and the board minimum in DRC, so it can
        lower them too (``clearance`` tells the outcome). ``warnings``: rules asking less than
        the board minimum (``constraints``), which DRC then accepts."""
        self._rules_text = text.strip()
        return {"rules": text.count("(rule "), **({"warnings": w} if (w := self._below_minimum(text)) else {})}

    def constraints(self, min_track: float | None = None, min_clearance: float | None = None,
                    min_via: float | None = None, min_via_drill: float | None = None,
                    min_annular: float | None = None, hole_clearance: float | None = None,
                    hole_to_hole: float | None = None, edge_clearance: float | None = None,
                    edge_exempt: list[str] | None = None) -> dict:
        """The board's minimums (Board Setup > Constraints; mm), what the fab can make:
        DRC reports anything below them, and custom rules cannot ask less. ``edge_exempt``:
        parts that may reach the board edge (edge-mount connectors): the board minimum
        becomes 0 and a generated rule ``kat edge`` keeps ``edge_clearance`` for
        everything else. Values left None stay as they are. Written on save; the router
        gets the hole clearance too."""
        keys = {"min_track_width": min_track, "min_clearance": min_clearance,
                "min_via_diameter": min_via, "min_through_hole_diameter": min_via_drill,
                "min_via_annular_width": min_annular, "min_hole_clearance": hole_clearance,
                "min_hole_to_hole": hole_to_hole, "min_copper_edge_clearance": edge_clearance}
        values = {k: v for k, v in keys.items() if v is not None}
        if edge_exempt:
            if edge_clearance is None:
                raise env.KatError("edge_exempt needs edge_clearance (the rule for the other parts)")
            values["min_copper_edge_clearance"] = 0.0
            cond = " && ".join(f"A.Reference != '{r}' && B.Reference != '{r}'" for r in edge_exempt)
            self._gen_rules["kat edge"] = (f'(rule "kat edge" (condition "{cond}") '
                                           f'(constraint edge_clearance (min {edge_clearance:g}mm)))')
        self._pro_edits.append(("constraints", values))
        return {"constraints": values, **({"edge_exempt": list(edge_exempt)} if edge_exempt else {})}

    def board_minimums(self) -> dict:
        """The board's minimums now (with pending ``constraints``), KiCad's names, mm."""
        return dict(self._project_data().get("board", {}).get("design_settings", {}).get("rules", {}))

    def _below_minimum(self, text: str) -> list[str]:
        import re
        mins = self.board_minimums()
        which = {"clearance": "min_clearance", "track_width": "min_track_width",
                 "edge_clearance": "min_copper_edge_clearance", "hole_clearance": "min_hole_clearance"}
        out = []
        for name, kind, value in re.findall(r'\(rule\s+"?([^"\s)]+)"?.*?\(constraint\s+(\w+)\s+\(min\s+([\d.]+)mm', text, re.S):
            m = mins.get(which.get(kind, ""))
            if m is not None and float(value) < m - 1e-9:
                out.append(f"rule {name}: {kind} {value} mm is below the board minimum {m:g} mm: DRC "
                           f"follows the rule, so check it is what the fab can make")
        return out

    def _project_data(self) -> dict:
        """The .kicad_pro as it will be saved: the file plus the pending edits."""
        pro = self.path.with_suffix(".kicad_pro")
        data = json.loads(pro.read_text(encoding="utf-8")) if pro.exists() else {}
        if not self._pro_edits:
            return data
        ns = data.setdefault("net_settings", {})
        classes = ns.setdefault("classes", [])
        if not any(c.get("name") == "Default" for c in classes):
            classes.insert(0, dict(DEFAULT_CLASS))  # an empty v0 list means "KiCad defaults"
        patterns = ns.get("netclass_patterns") or []
        for kind, e in self._pro_edits:
            if kind == "constraints":
                data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {}).update(e)
                continue
            if kind != "netclass":
                continue
            cls = next((c for c in classes if c.get("name") == e["name"]), None)
            if cls is None:
                base = next(c for c in classes if c.get("name") == "Default")
                cls = dict(base, name=e["name"],
                           priority=sum(c.get("name") != "Default" for c in classes))
                classes.append(cls)
            cls.update(track_width=e["track"], clearance=e["clearance"],
                       via_diameter=e["via"], via_drill=e["via_drill"])
            patterns = [p for p in patterns if p.get("netclass") != e["name"]]
            patterns += [{"netclass": e["name"], "pattern": pat} for pat in e["nets"]]
        ns["netclass_patterns"] = patterns
        ns["meta"] = {"version": 5}
        return data

    def _dru_text(self) -> str:
        """The rules as they will be saved: the .kicad_dru plus the pending changes, the
        user's rules first."""
        dru = self.path.with_suffix(".kicad_dru")
        old = dru.read_text(encoding="utf-8") if dru.exists() else ""
        own, gen = _split_rules(old)
        if self._rules_text is not None:
            own = [self._rules_text] if self._rules_text else []
        gen = {**gen, **self._gen_rules}
        return "\n".join(own + [gen[k] for k in sorted(gen)])

    def _write_project_files(self) -> None:
        self._write_route_settings()
        pro = self.path.with_suffix(".kicad_pro")
        if self._pro_edits:
            pro.write_text(json.dumps(self._project_data(), indent=2) + "\n", encoding="utf-8")
            self._pro_edits = []
        if self._rules_text is not None or self._gen_rules:
            body = self._dru_text()
            self.path.with_suffix(".kicad_dru").write_text(f"(version 1)\n{body}\n" if body else "(version 1)\n",
                                                           encoding="utf-8")
            self._rules_text = None
            self._gen_rules = {}


def _joined(run: dict, s: dict) -> bool:
    """Extend ``run`` by segment ``s`` if it goes straight on from one of its ends."""
    for pa, pb in ((run["a"], run["b"]), (run["b"], run["a"])):
        for sa, sb in ((s["a"], s["b"]), (s["b"], s["a"])):
            if math.dist(pb, sa) > 1e-6:
                continue
            ux, uy, vx, vy = pb[0] - pa[0], pb[1] - pa[1], sb[0] - sa[0], sb[1] - sa[1]
            if abs(ux * vy - uy * vx) <= 1e-6 * math.hypot(ux, uy) * math.hypot(vx, vy) and ux * vx + uy * vy > 0:
                run["a"], run["b"] = pa, sb
                return True
    return False


def _straight_runs(segments: list[dict]) -> list[dict]:
    """Segments (layer, half width r, ends a, b) with collinear neighbours of one layer and width
    joined, so a long track makes one area."""
    out: list[dict] = []
    for s in sorted(segments, key=lambda e: (e["layer"], e["r"], min(e["a"], e["b"]))):
        if not any(run["layer"] == s["layer"] and abs(run["r"] - s["r"]) < 1e-9 and _joined(run, s) for run in out):
            out.append(dict(s))
    return out


def _split_rules(text: str) -> tuple[list[str], dict[str, str]]:
    """A .kicad_dru's rules: (the user's, as text; kat's generated ones by name)."""
    from ..sexpr import dumps, loads
    own, gen = [], {}
    if not text.strip():
        return own, gen
    for n in loads(f"(dru {text})")[1:]:
        if head(n) != "rule":
            continue
        name = str(n[1])
        (gen.__setitem__(name, dumps(n).replace("\n", " ")) if name.startswith("kat ")
         else own.append(dumps(n)))
    return own, gen
