"""Copper structure before routing: tracks, vias, a via per pad into a plane, fan-out,
stitching vias, locking and rip-up. Mixed into ``Board``.

kat never connects different nets: a track takes its net from the pads it touches and is
refused if they disagree. Clearances come from the net classes (.kicad_pro); new copper
that would violate them is refused, so DRC afterwards only confirms. KiCad's DRC stays
the judge (``kat pcb check``).
"""

from __future__ import annotations

import fnmatch
import json
import math

from .. import env
from ..schematic import new_uuid
from ..sexpr import Sym, find, num
from .geometry import Index, inside, point_seg, poly_seg, seg_seg

EDGE_CLEARANCE = 0.5   # copper to board edge, mm
VIA = (0.6, 0.3)       # default size and drill, as KiCad's Default class


class _Copper:
    """What is already on the board, for clearance checks: pads (polygons), tracks
    (segments with half width) and vias (circles), each with net and copper layers."""

    def __init__(self, board):
        self.index = Index(cell=2.0)
        for pad in board.pads():
            layers = {l for l in board.copper_layers() if pad.on(l)}
            self.index.add(pad.box, {"kind": "pad", "net": pad.net, "layers": layers,
                                     "poly": pad.outline, "ref": f"{pad.ref}.{pad.number}"})
        for t in board.tracks():
            self.add_track(t["start"], t["end"], t["width"], t["layer"], t["net"])
        for v in board.vias():
            self.add_via(v["at"], v["size"], v["net"], set(board.copper_layers()))

    def add_track(self, a, b, width, layer, net):
        h = width / 2
        box = (min(a[0], b[0]) - h, min(a[1], b[1]) - h, max(a[0], b[0]) + h, max(a[1], b[1]) + h)
        self.index.add(box, {"kind": "track", "net": net, "layers": {layer}, "seg": (a, b), "half": h})

    def add_via(self, at, size, net, layers):
        r = size / 2
        self.index.add((at[0] - r, at[1] - r, at[0] + r, at[1] + r),
                       {"kind": "via", "net": net, "layers": layers, "at": at, "r": r})

    def conflicts(self, net: str, layers: set, clearance, seg=None, circle=None) -> list[dict]:
        """Other-net copper closer than its clearance to a segment ((a, b), half width) or a
        circle ((x, y), r) on ``layers``."""
        if seg:
            (a, b), h = seg
            box = (min(a[0], b[0]) - h, min(a[1], b[1]) - h, max(a[0], b[0]) + h, max(a[1], b[1]) + h)
        else:
            (x, y), h = circle
            a = b = (x, y)
            box = (x - h, y - h, x + h, y + h)
        out = []
        for it in self.index.query(box, margin=2.0):
            if it["net"] == net and net or not (it["layers"] & layers):
                continue
            need = clearance(net, it["net"])
            if it["kind"] == "pad":
                d = poly_seg(it["poly"], a, b) - h
            elif it["kind"] == "track":
                d = seg_seg(a, b, *it["seg"]) - h - it["half"]
            else:
                d = point_seg(it["at"], a, b) - h - it["r"]
            if d < need - 1e-6:
                out.append({"kind": it["kind"], "net": it["net"], "gap": round(max(d, 0), 3),
                            "need": need, **({"pad": it["ref"]} if "ref" in it else {})})
        return out


class StructureMixin:
    # --- net classes -------------------------------------------------------------------------

    def _classes(self) -> tuple[dict, list]:
        pro = self.path.with_suffix(".kicad_pro")
        ns = json.loads(pro.read_text()).get("net_settings", {}) if pro.exists() else {}
        classes = {c["name"]: c for c in ns.get("classes", [])}
        return classes, ns.get("netclass_patterns") or []

    def net_rules(self, net: str) -> dict:
        """Track width, clearance and via size of a net's class (KiCad defaults if none)."""
        classes, patterns = self._classes()
        name = next((p["netclass"] for p in patterns if fnmatch.fnmatchcase(net, p["pattern"])), "Default")
        c = {**classes.get("Default", {}), **classes.get(name, {})}
        return {"netclass": name, "track": c.get("track_width", 0.2), "clearance": c.get("clearance", 0.2),
                "via": c.get("via_diameter", VIA[0]), "via_drill": c.get("via_drill", VIA[1])}

    def _clearance_fn(self):
        cache = {}

        def cl(a, b):
            for n in (a, b):
                if n not in cache:
                    cache[n] = self.net_rules(n)["clearance"] if n else 0.2
            return max(cache[a], cache[b])
        return cl

    # --- copper items --------------------------------------------------------------------------

    def _resolve(self, pt) -> tuple[tuple[float, float], object | None]:
        if isinstance(pt, str):
            ref, _, number = pt.partition(".")
            pad = self.footprint(ref).pad(number)
            return (pad.x, pad.y), pad
        return (float(pt[0]), float(pt[1])), None

    def _segment(self, a, b, width, layer, net, lock) -> list:
        node = [Sym("segment"), [Sym("start"), num(a[0]), num(a[1])], [Sym("end"), num(b[0]), num(b[1])],
                [Sym("width"), num(width)]]
        if lock:
            node.append([Sym("locked"), Sym("yes")])
        node += [[Sym("layer"), layer], [Sym("net"), net], [Sym("uuid"), new_uuid()]]
        self._insert(node)
        return node

    def _via(self, at, net, size, drill, lock) -> list:
        node = [Sym("via"), [Sym("at"), num(at[0]), num(at[1])], [Sym("size"), num(size)],
                [Sym("drill"), num(drill)], [Sym("layers"), "F.Cu", "B.Cu"]]
        if lock:
            node.append([Sym("locked"), Sym("yes")])
        node += [[Sym("net"), net], [Sym("uuid"), new_uuid()]]
        self._insert(node)
        return node

    def track(self, points: list, layer: str = "F.Cu", width: float | None = None,
              net: str | None = None, lock: bool = False, check: bool = True) -> dict:
        """A track along ``points`` ("REF.PAD" or (x, y)). The net comes from the pads it
        starts or ends on (all must agree; ``net`` only for tracks between free points).
        Width defaults to the net class. Refused if it would come too close to other-net
        copper (``check=False`` leaves that to DRC)."""
        pts, nets = [], set()
        for p in points:
            xy, pad = self._resolve(p)
            pts.append(xy)
            if pad is not None:
                if not pad.on(layer) and pad.kind != "thru_hole":
                    raise env.KatError(f"{pad.ref}.{pad.number} has no copper on {layer}")
                nets.add(pad.net)
        if net is not None:
            nets.add(net)
        if len(nets) != 1 or not next(iter(nets)):
            raise env.KatError(f"a track needs exactly one net; its pads say {sorted(nets) or 'none'} "
                               "(connections are made in the schematic, not on the board)")
        net = nets.pop()
        rules = self.net_rules(net)
        width = width or rules["track"]
        if check:
            cu, cl = _Copper(self), self._clearance_fn()
            for a, b in zip(pts, pts[1:]):
                hits = cu.conflicts(net, {layer}, cl, seg=((a, b), width / 2))
                if hits:
                    raise env.KatError(f"track {a}->{b} on {layer} too close to {hits[:3]}")
        uuids = [str(find(self._segment(a, b, width, layer, net, lock), "uuid")[1])
                 for a, b in zip(pts, pts[1:])]
        return {"net": net, "layer": layer, "width": width, "segments": uuids}

    def via(self, at, net: str | None = None, size: float | None = None, drill: float | None = None,
            lock: bool = False, check: bool = True) -> dict:
        """A through via at (x, y) or on a pad ("REF.PAD", net from the pad)."""
        xy, pad = self._resolve(at)
        net = pad.net if pad is not None else net
        if not net:
            raise env.KatError("a via needs a net (or a pad to sit on)")
        rules = self.net_rules(net)
        size, drill = size or rules["via"], drill or rules["via_drill"]
        if check:
            hits = _Copper(self).conflicts(net, set(self.copper_layers()), self._clearance_fn(),
                                           circle=(xy, size / 2))
            if hits:
                raise env.KatError(f"via at {xy} too close to {hits[:3]}")
        node = self._via(xy, net, size, drill, lock)
        return {"net": net, "at": list(xy), "uuid": str(find(node, "uuid")[1])}

    def _free_via_spot(self, cu, pad, net, size, width, layer, max_reach: float = 3.0):
        """First spot for a via next to ``pad``: along the pad's outward direction first,
        then sideways, then inward; stub and via both clear of other-net copper."""
        f = self.footprint(pad.ref)
        fb = f.box()
        cx, cy = (fb[0] + fb[2]) / 2, (fb[1] + fb[3]) / 2
        ox, oy = pad.x - cx, pad.y - cy
        n = math.hypot(ox, oy)
        if n < 1e-6:
            ox, oy, n = 1.0, 0.0, 1.0
        main = (ox / n, oy / n)
        # snap to the nearest axis: stubs look like KiCad fan-outs
        main = (1.0 if main[0] > 0 else -1.0, 0.0) if abs(main[0]) >= abs(main[1]) else (0.0, 1.0 if main[1] > 0 else -1.0)
        dirs = [main, (-main[1], main[0]), (main[1], -main[0]), (-main[0], -main[1])]
        dirs += [((main[0] + d[0]) / math.sqrt(2), (main[1] + d[1]) / math.sqrt(2)) for d in dirs[1:3]]
        cl = self._clearance_fn()
        edge = self.outline_box()
        r = size / 2
        half = max(pad.w, pad.h) / 2
        for dx, dy in dirs:
            d = half + r + cl(net, "") * 0.5
            while d <= half + max_reach:
                x, y = pad.x + dx * d, pad.y + dy * d
                d += 0.05
                if edge and not (edge[0] + EDGE_CLEARANCE + r <= x <= edge[2] - EDGE_CLEARANCE - r and
                                 edge[1] + EDGE_CLEARANCE + r <= y <= edge[3] - EDGE_CLEARANCE - r):
                    continue
                if inside(pad.outline, (x, y)):
                    continue
                if cu.conflicts(net, set(self.copper_layers()), cl, circle=((x, y), r)):
                    continue
                if cu.conflicts(net, {layer}, cl, seg=(((pad.x, pad.y), (x, y)), width / 2)):
                    continue
                return round(x, 4), round(y, 4)
        return None

    def via_to_plane(self, pad: str, lock: bool = True, _cu=None) -> dict:
        """A short stub from an SMD pad to a via next to it, so the pad reaches a plane of
        its net on another layer. The via goes outward from the part first, then sideways."""
        ref, _, number = pad.partition(".")
        p = self.footprint(ref).pad(number)
        if not p.net:
            raise env.KatError(f"{pad} has no net")
        layer = next(l for l in self.copper_layers() if p.on(l))
        rules = self.net_rules(p.net)
        cu = _cu or _Copper(self)
        spot = self._free_via_spot(cu, p, p.net, rules["via"], rules["track"], layer)
        if spot is None:
            raise env.KatError(f"no free spot for a via next to {pad}")
        self._segment((p.x, p.y), spot, rules["track"], layer, p.net, lock)
        self._via(spot, p.net, rules["via"], rules["via_drill"], lock)
        cu.add_track((p.x, p.y), spot, rules["track"], layer, p.net)
        cu.add_via(spot, rules["via"], p.net, set(self.copper_layers()))
        return {"pad": pad, "net": p.net, "via": list(spot)}

    def fanout(self, refs: str | list[str], nets: list[str] | None = None,
               skip_nets: list[str] = (), lock: bool = True) -> dict:
        """``via_to_plane`` for the SMD pads of ``refs`` (only ``nets`` if given, never
        ``skip_nets``). Pads that find no spot are listed in ``failed``."""
        refs = [refs] if isinstance(refs, str) else refs
        cu = _Copper(self)
        done, failed = [], []
        for ref in refs:
            for p in self.footprint(ref).pads():
                if not p.net or p.kind != "smd" or p.net in skip_nets or (nets and p.net not in nets):
                    continue
                try:
                    done.append(self.via_to_plane(f"{ref}.{p.number}", lock, _cu=cu))
                except env.KatError as e:
                    failed.append({"pad": f"{ref}.{p.number}", "why": str(e)})
        return {"vias": done, "failed": failed}

    def stitch(self, net: str, pitch: float = 5.0, margin: float = 2.0, lock: bool = True) -> dict:
        """Stitching vias of ``net`` on a grid inside the outline, where they clear other
        copper and no courtyard lies (they tie the net's pours on all layers together)."""
        edge = self.outline_box()
        if not edge:
            raise env.KatError("stitching needs a board outline")
        rules = self.net_rules(net)
        cu, cl = _Copper(self), self._clearance_fn()
        courts = [f.box() for f in self.footprints() if f.box()]
        placed = []
        nx = int((edge[2] - edge[0] - 2 * margin) // pitch)
        ny = int((edge[3] - edge[1] - 2 * margin) // pitch)
        x0 = edge[0] + (edge[2] - edge[0] - nx * pitch) / 2
        y0 = edge[1] + (edge[3] - edge[1] - ny * pitch) / 2
        r = rules["via"] / 2
        for i in range(nx + 1):
            for j in range(ny + 1):
                x, y = x0 + i * pitch, y0 + j * pitch
                if any(b[0] - r <= x <= b[2] + r and b[1] - r <= y <= b[3] + r for b in courts):
                    continue
                if cu.conflicts(net, set(self.copper_layers()), cl, circle=((x, y), r)):
                    continue
                self._via((x, y), net, rules["via"], rules["via_drill"], lock)
                cu.add_via((x, y), rules["via"], net, set(self.copper_layers()))
                placed.append([round(x, 3), round(y, 3)])
        return {"net": net, "vias": len(placed)}

    # --- locking and rip-up ----------------------------------------------------------------

    def lock(self, net: str | None = None, locked: bool = True) -> dict:
        """Lock (or unlock) tracks and vias, all or of one net: locked copper is kept by the
        router and by ``remove_tracks``."""
        n = 0
        for item in self.tracks() + self.vias():
            if net is not None and item["net"] != net:
                continue
            node = item["node"]
            cur = find(node, "locked")
            if locked and cur is None:
                node.insert(next(i for i, c in enumerate(node) if isinstance(c, list)
                                 and str(c[0]) in ("layer", "layers")), [Sym("locked"), Sym("yes")])
                n += 1
            elif not locked and cur is not None:
                node.remove(cur)
                n += 1
        return {"changed": n}

    def remove_tracks(self, net: str | None = None, layer: str | None = None,
                      keep_locked: bool = True, vias: bool = True) -> dict:
        """Rip up tracks (and vias) - all, of one net and/or one layer; locked ones stay."""
        gone = 0
        items = self.tracks() + (self.vias() if vias else [])
        for item in items:
            if net is not None and item["net"] != net:
                continue
            if layer is not None and item.get("layer", layer) != layer:
                continue
            if keep_locked and item["locked"]:
                continue
            self.tree.remove(item["node"])
            gone += 1
        return {"removed": gone}
