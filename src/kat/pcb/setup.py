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

    def mounting_hole(self, at: Point, size: str = "M3", ref: str | None = None):
        if size not in HOLES:
            raise env.KatError(f"size one of {', '.join(HOLES)}")
        used = {f.ref for f in self.footprints()}
        ref = ref or next(f"H{i}" for i in range(1, 1000) if f"H{i}" not in used)
        return self.add_board_part(HOLES[size], ref, at, value=f"{size} hole")

    # --- zones ---------------------------------------------------------------------------

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
        """A copper pour of ``net``; ``polygon`` defaults to the outline shrunk by 0.5 mm.
        Filled by KiCad (``refill``/``kat pcb check``)."""
        layers = [layers] if isinstance(layers, str) else list(layers)
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
        self._insert(node)
        return {"zone": str(node[node.index(find(node, "uuid"))][1]), "net": net, "layers": layers}

    def keepout(self, layers: str | list[str], polygon: list[Point] | None = None,
                rect: tuple[float, float, float, float] | None = None, tracks: bool = False,
                vias: bool = True, pads: bool = True, pour: bool = True, footprints: bool = True,
                name: str = "") -> dict:
        """A rule area: ``False`` forbids that kind of item inside (default: tracks only)."""
        layers = [layers] if isinstance(layers, str) else list(layers)
        polygon = polygon or (_rect(*rect) if rect else None)
        if not polygon:
            raise env.KatError("keepout needs polygon or rect")
        node = self._zone(layers, polygon, name)
        ok = lambda v: Sym("allowed" if v else "not_allowed")
        node += [[Sym("connect_pads"), [Sym("clearance"), Sym("0")]],
                 [Sym("min_thickness"), Sym("0.25")],
                 [Sym("keepout"), [Sym("tracks"), ok(tracks)], [Sym("vias"), ok(vias)],
                  [Sym("pads"), ok(pads)], [Sym("copperpour"), ok(pour)],
                  [Sym("footprints"), ok(footprints)]],
                 [Sym("fill"), [Sym("thermal_gap"), Sym("0.5")], [Sym("thermal_bridge_width"), Sym("0.5")]],
                 [Sym("polygon"), _pts(polygon)]]
        self._insert(node)
        return {"keepout": name or str(find(node, "uuid")[1]), "layers": layers}

    # --- net classes and rules (project files, written on save) ---------------------------

    def netclass(self, name: str, track: float, clearance: float, via: float = 0.6,
                 via_drill: float = 0.3, nets: list[str] | None = None) -> dict:
        """Define or change a net class in the .kicad_pro; ``nets`` are net-name patterns
        (``GND``, ``/LED_*``) assigned to it. ``Default`` changes the default class."""
        self._pro_edits.append(("netclass", dict(name=name, track=track, clearance=clearance,
                                                 via=via, via_drill=via_drill, nets=nets or [])))
        return {"netclass": name}

    def rules(self, text: str) -> dict:
        """Custom DRC rules (KiCad's rule language) for the .kicad_dru, replacing it:
        ``(rule "hv" (condition "A.NetClass == 'HV'") (constraint clearance (min 2mm)))``."""
        self._rules_text = text.strip()
        return {"rules": text.count("(rule ")}

    def _write_project_files(self) -> None:
        pro = self.path.with_suffix(".kicad_pro")
        if self._pro_edits:
            data = json.loads(pro.read_text()) if pro.exists() else {}
            ns = data.setdefault("net_settings", {})
            classes = ns.setdefault("classes", [])
            if not any(c.get("name") == "Default" for c in classes):
                classes.insert(0, dict(DEFAULT_CLASS))  # an empty v0 list means "KiCad defaults"
            patterns = ns.get("netclass_patterns") or []
            for kind, e in self._pro_edits:
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
            pro.write_text(json.dumps(data, indent=2) + "\n")
            self._pro_edits = []
        if self._rules_text is not None:
            dru = self.path.with_suffix(".kicad_dru")
            dru.write_text(f"(version 1)\n{self._rules_text}\n" if self._rules_text else "(version 1)\n")
            self._rules_text = None
