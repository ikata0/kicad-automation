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
            val(node, "connect_pads", "clearance"), val(node, "min_thickness"),
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

    def outline(self, rect: tuple[float, float, float, float] | None = None, radius: float = 0.0,
                polygon: list[Point] | None = None) -> dict:
        """Replace the board outline: ``rect`` (x, y, w, h) with optional corner ``radius``,
        or a ``polygon``. Everything on Edge.Cuts is replaced."""
        for n in [n for n in self.tree if head(n) and head(n).startswith("gr_")
                  and find(n, "layer") is not None and str(find(n, "layer")[1]) == "Edge.Cuts"]:
            self.tree.remove(n)
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
             spoke: float = 0.5, priority: int = 0, name: str = "") -> dict:
        """A copper pour of ``net`` on ``layers``; ``polygon`` (mm) defaults to the outline
        shrunk by 0.5 mm. ``clearance``, ``min_width``, ``thermal_gap`` and ``spoke`` (thermal
        spoke width) in mm; a higher ``priority`` pours first where zones overlap. Filled by
        KiCad (``refill``/``kat pcb check``). Safe to run again: a zone of the same ``name``
        (default ``"{net} {layers}"``) is replaced, or kept when unchanged. Several pours of one
        net on one layer need distinct names. Returns ``zone`` (name), ``net``, ``layers``,
        ``status``."""
        layers = [layers] if isinstance(layers, str) else list(layers)
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
        node += [[Sym("connect_pads"), [Sym("clearance"), num(clearance)]],
                 [Sym("min_thickness"), num(min_width)],
                 [Sym("fill"), Sym("yes"), [Sym("thermal_gap"), num(thermal_gap)],
                  [Sym("thermal_bridge_width"), num(spoke)], [Sym("island_removal_mode"), Sym("0")]],
                 [Sym("polygon"), _pts(polygon)]]
        status = self._put_zone(node, name)
        return {"zone": name, "net": net, "layers": layers, "status": status}

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

    def route_settings(self) -> dict:
        """What the router gets beyond KiCad's classes: ``layers`` per class and
        ``class_clearance`` ({"A B": mm}), with pending changes."""
        from ..syncer_hier import settings_file
        f = settings_file(self.path)
        saved = (json.loads(f.read_text()) if f.exists() else {}).get("route", {})
        out = {k: dict(saved.get(k, {})) for k in ("layers", "class_clearance")}
        for k, v in self._route_edits.items():
            out[k].update(v)
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
        ``warnings``: rules asking less than the board minimum (``constraints``), which
        DRC ignores."""
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
                out.append(f"rule {name}: {kind} {value} mm is below the board minimum {m:g} mm "
                           f"(DRC uses the minimum; lower it with constraints)")
        return out

    def _project_data(self) -> dict:
        """The .kicad_pro as it will be saved: the file plus the pending edits."""
        pro = self.path.with_suffix(".kicad_pro")
        data = json.loads(pro.read_text()) if pro.exists() else {}
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

    def _write_project_files(self) -> None:
        self._write_route_settings()
        pro = self.path.with_suffix(".kicad_pro")
        if self._pro_edits:
            pro.write_text(json.dumps(self._project_data(), indent=2) + "\n")
            self._pro_edits = []
        if self._rules_text is not None or self._gen_rules:
            dru = self.path.with_suffix(".kicad_dru")
            old = dru.read_text() if dru.exists() else ""
            own, gen = _split_rules(old)
            if self._rules_text is not None:
                own = [self._rules_text] if self._rules_text else []
            gen.update(self._gen_rules)
            body = "\n".join(own + [gen[k] for k in sorted(gen)])
            dru.write_text(f"(version 1)\n{body}\n" if body else "(version 1)\n")
            self._rules_text = None
            self._gen_rules = {}


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
