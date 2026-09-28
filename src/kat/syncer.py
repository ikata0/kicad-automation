"""Bring a design netlist (e.g. from SKiDL) into a schematic without losing the arrangement.

The netlist is the source of truth for parts and connectivity; the schematic keeps
positions, wiring style and everything else that is only about readability.
Parts are matched by the hidden ``SKiDL Tag`` field first, then by reference.
"""

from __future__ import annotations

import math
import re
import tempfile
from collections import Counter
from pathlib import Path

from . import checks, env, library, netlist
from .netlist import Component
from .schematic import GRID, Placement, Schematic, same, snap, text_width
from .sexpr import dumps, find, find_all

TAG = "SKiDL Tag"
STUB = 2.54
PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
         "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8),
         "D": (863.6, 558.8), "E": (1117.6, 863.6), "USLetter": (279.4, 215.9)}

Pin = tuple[str, str]  # (ref, pin number)


def natural_key(ref: str) -> list:
    """R2 < R10: digits compare as numbers."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


def paper_size(s: Schematic) -> tuple[float, float]:
    p = find(s.tree, "paper")
    if p is None:
        return PAPER["A4"]
    if str(p[1]) == "User" and len(p) > 3:
        return float(p[2]), float(p[3])
    w, h = PAPER.get(str(p[1]), PAPER["A4"])
    return (h, w) if any(str(t) == "portrait" for t in p[2:]) else (w, h)


def _clean(name: str) -> str:
    return name.lstrip("/")


def drawn_nets(s: Schematic) -> dict[str, frozenset[Pin]]:
    """KiCad's view of the current in-memory schematic (via a temporary file)."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "sync.kicad_sch"
        tmp.write_text(dumps(s.tree) + "\n", encoding="utf-8")
        return {_clean(k): v for k, v in netlist.from_schematic(tmp).items()}


def _drawn_erc(s: Schematic) -> dict:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "sync.kicad_sch"
        tmp.write_text(dumps(s.tree) + "\n", encoding="utf-8")
        return checks.erc(tmp)


def power_names() -> set[str]:
    libs = env.symbol_libraries()
    if "power" not in libs:
        return set()
    return set(library._lib_symbols(libs["power"]))


def _dir_angle(d) -> float:
    return {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}.get((round(d[0]), round(d[1])), 0)


class Syncer:
    def __init__(self, s: Schematic, design: Path):
        self.s = s
        self.comps, raw_nets = netlist.read_design(design)
        self.nets = {_clean(k): v for k, v in raw_nets.items()}
        self.power = power_names()
        self.report: dict[str, list] = {k: [] for k in (
            "added", "removed", "renamed", "updated", "replaced", "detached", "attached",
            "named", "wired", "flags", "paper", "unresolved")}
        self.new_refs: set[str] = set()

    # --- parts ----------------------------------------------------------------------

    def placed(self) -> dict[str, list]:
        out: dict[str, list] = {}
        for sym in self.s.symbols():
            if not sym.ref.startswith("#"):
                out.setdefault(sym.ref, []).append(sym)
        return out

    def match(self) -> dict[str, str]:
        """design ref -> placed ref."""
        placed = self.placed()
        tag_of = {ref: (syms[0].field(TAG) or "") for ref, syms in placed.items()}
        design_tags = {c.tag for c in self.comps.values() if c.tag}
        by_tag = {t: r for r, t in tag_of.items() if t}
        matched: dict[str, str] = {}
        used: set[str] = set()
        for c in self.comps.values():
            if c.tag and c.tag in by_tag:
                matched[c.ref] = by_tag[c.tag]
                used.add(by_tag[c.tag])
        for c in self.comps.values():
            if c.ref in matched or c.ref not in placed or c.ref in used:
                continue
            if tag_of[c.ref] and tag_of[c.ref] in design_tags and tag_of[c.ref] != c.tag:
                continue  # that symbol belongs to another (renamed) design part
            matched[c.ref] = c.ref
            used.add(c.ref)
        return matched

    def sync_parts(self) -> None:
        s = self.s
        matched = self.match()
        placed = self.placed()
        for ref in sorted(set(placed) - set(matched.values())):
            s.remove_symbol(ref)
            self.report["removed"].append(ref)
        # renames in two phases so swaps (R1<->R2) cannot collide
        renames = {old: new for new, old in matched.items() if old != new}
        for i, old in enumerate(renames):
            s.set_field(old, "Reference", f"__kat_tmp_{i}")
        for i, (old, new) in enumerate(renames.items()):
            s.set_field(f"__kat_tmp_{i}", "Reference", new)
            self.report["renamed"].append({"from": old, "to": new})
        for ref, c in self.comps.items():
            if ref not in matched:
                continue
            syms = s.find_symbols(ref)
            if syms[0].lib_id != c.lib_id:
                self._replace(c, syms)
                continue
            for field, want in (("Value", c.value), ("Footprint", c.footprint)):
                have = syms[0].field(field) or ""
                if want != have and (want or field == "Footprint"):
                    s.set_field(ref, field, want)
                    self.report["updated"].append({"ref": ref, "field": field, "from": have, "to": want})
            if c.tag and (syms[0].field(TAG) or "") != c.tag:
                s.set_field(ref, TAG, c.tag, hide=True)
            for field, want in c.fields.items():
                have = syms[0].field(field)
                if have != want:
                    s.set_field(ref, field, want, hide=True if have is None else None)
                    self.report["updated"].append({"ref": ref, "field": field, "from": have or "", "to": want})
        new = [c for ref, c in self.comps.items() if ref not in matched]
        self.new_refs = {c.ref for c in new}
        if new:
            self._place_new(new)
        if self.report["removed"] or self.report["replaced"]:
            s.prune_dangling()

    def _replace(self, c: Component, syms) -> None:
        places = {sym.unit: sym.place for sym in syms}
        self.s.remove_symbol(c.ref)
        sdef = self.s.ensure_lib_symbol(c.lib_id)
        leftover = []
        for unit in range(1, library.unit_count(sdef) + 1):
            if unit in places:
                p = places[unit]
                self._add(c, unit, p.x, p.y, p.rot, p.mirror)
            else:
                leftover.append(unit)
        if leftover:
            self._place_new([c], units=leftover)
        self.report["replaced"].append({"ref": c.ref, "lib_id": c.lib_id})

    def _add(self, c: Component, unit: int, x: float, y: float, rot: float = 0, mirror: str = ""):
        sym = self.s.add_symbol(c.lib_id, x, y, rot, mirror, unit, c.ref, c.value or None,
                                c.footprint or None)
        if c.tag:
            self.s.set_field(c.ref, TAG, c.tag, hide=True, unit=unit)
        for field, value in c.fields.items():
            self.s.set_field(c.ref, field, value, hide=True, unit=unit)
        self.s.autoplace_fields(sym)
        return sym

    def _net_of(self) -> dict[Pin, str]:
        return {pin: name for name, pins in self.nets.items() for pin in pins}

    def _order(self, comps: list[Component]) -> list[list[Component]]:
        """Natural reference order (R2 before R10), with parts that share an unnamed two-pin
        net placed next to each other (D1 then R1), so that net can be a short wire, and
        the parts of one block instance together (top level first, then by block path)."""
        by_ref = {c.ref: c for c in comps}
        partners: dict[str, list[str]] = {r: [] for r in by_ref}
        for name, pins in self.nets.items():
            refs = sorted({r for r, _ in pins})
            if len(refs) == 2 and all(r in by_ref for r in refs) and self._pair_net(name):
                partners[refs[0]].append(refs[1])
                partners[refs[1]].append(refs[0])
        groups, seen = [], set()
        for ref in sorted(by_ref, key=natural_key):
            stack, group = [ref], []
            while stack:
                r = stack.pop()
                if r in seen:
                    continue
                seen.add(r)
                group.append(by_ref[r])
                stack += sorted(partners[r], key=natural_key, reverse=True)
            if group:
                groups.append(group)
        # parts of one block instance (SKiDL subcircuit) stay together, blocks in path order
        order = {id(g): i for i, g in enumerate(groups)}
        return sorted(groups, key=lambda g: (g[0].block, order[id(g)]))

    def _place_new(self, comps: list[Component], units: list[int] | None = None) -> None:
        """Put new parts in uniform rows in a free area below everything already drawn. If
        they do not fit on the page, the paper grows to the next ISO size (reported)."""
        s = self.s
        boxes = [b for sym in s.symbols() if (b := s.body_box(sym))]
        boxes += [(l["x"], l["y"] - 1.3, l["x"] + text_width(l["text"]), l["y"]) for l in s.labels()]
        x0 = 50.8 if not boxes else max(25.4, snap(min(b[0] for b in boxes)))
        y0 = 50.8 if not boxes else max(b[3] for b in boxes) + 20.32
        net_of = self._net_of()
        groups = []  # parts joined by pair nets stay together in one row
        for group in self._order(comps):
            items = []
            for c in group:
                sdef = s.ensure_lib_symbol(c.lib_id)
                for unit in units or range(1, library.unit_count(sdef) + 1):
                    items.append((c, unit, *self._footprint_box(c, sdef, unit, net_of)))
            groups.append(items)
        while True:
            page_w, page_h = paper_size(s)
            rows, row, x = [], [], x0
            for items in groups:
                for attempt in (0, 1):
                    placed, gx = [], x
                    for it in items:
                        box = it[2]  # library coordinates, including room for labels and fields
                        cx = math.ceil((gx - box[0]) / 2.54) * 2.54
                        if (placed or row) and cx + box[2] > page_w - 20:
                            if attempt == 0 and row:
                                break  # the group does not fit: start it on a new row
                            rows.append(row + placed)
                            row, placed, gx = [], [], x0
                            cx = math.ceil((gx - box[0]) / 2.54) * 2.54
                        placed.append((it, cx))
                        gx = cx + box[2] + 2.54
                    else:
                        row += placed
                        x = gx
                        break
                    rows.append(row)
                    row, x = [], x0
            rows.append(row)
            heights = [max(it[2][3] - it[2][1] for it, _ in r) + 2.54 for r in rows]
            # keep clear of the title block in the bottom right corner
            if y0 + sum(heights) <= page_h - 45 or not self._grow_paper():
                break
        y = y0
        for r, h in zip(rows, heights):
            mid = y + h / 2
            for (c, unit, box), cx in r:
                cy = round((mid + (box[1] + box[3]) / 2) / 2.54) * 2.54  # body centred in the row
                self._add(c, unit, cx, cy)
                self.report["added"].append({"ref": c.ref, "unit": unit, "x": round(cx, 2),
                                             "y": round(cy, 2)})
            y += h

    def _footprint_box(self, c: Component, sdef: list, unit: int, net_of: dict) -> tuple:
        """Library-coordinate box of a new part plus the room its stubs + labels (in each
        pin's direction) and its visible fields will take."""
        box = library.outline(sdef, unit) or (0, 0, 0, 0)
        room = {"l": 2.54, "r": 2.54, "t": 2.54, "b": 2.54}
        pins = [p for p in library.pins(sdef) if p.unit in (0, unit) and not p.hidden]
        for p in pins:
            name = net_of.get((c.ref, p.number), "")
            if name in self.power:
                widths = [text_width(n) for q in pins if q is not p and q.angle == p.angle
                          and _row_neighbours((p.x, p.y), (q.x, q.y), p.angle)
                          and (n := net_of.get((c.ref, q.number), "")) and n not in self.power]
                stub = _reach(widths) or STUB
                need = stub + 5.08 + text_width(name) + 1.27
            else:
                label = 0.0 if self._pair_net(name) else text_width(name)
                need = STUB + label + 1.27
            side = {0: "l", 180: "r", 90: "b", 270: "t"}.get(int(p.angle) % 360, "l")
            room[side] = max(room[side], need)
        fields = max(text_width(c.ref), text_width(c.value or ""))
        half = max(0.0, fields / 2 + 1.27 - (box[2] - box[0]) / 2)
        room["l"], room["r"] = max(room["l"], half), max(room["r"], half)
        room["t"] += 2.54
        room["b"] += 2.54
        return (box[0] - room["l"], box[1] - room["b"], box[2] + room["r"], box[3] + room["t"]),

    def _pair_net(self, name: str) -> bool:
        """An unnamed net joining exactly two pins: drawn as a wire, no label needed."""
        return (bool(name) and netlist._auto(name) and name not in self.power
                and len(self.nets.get(name, ())) == 2)

    def _wire_pair(self, a: Pin, b: Pin) -> bool:
        """Join two pins of freshly placed parts with a short L-shaped wire, if one of the
        two L shapes touches nothing else and crosses no part body."""
        s = self.s
        if a[0] not in self.new_refs or b[0] not in self.new_refs:
            return False
        pa, pb = s.pin(*a), s.pin(*b)
        if abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) > 30:
            return False
        for corner in ((pb[0], pa[1]), (pa[0], pb[1])):
            path = [p for i, p in enumerate([pa, corner, pb]) if i != 1 or not (same(p, pa) or same(p, pb))]
            if all(_segment_clear(s, p, q, (pa, pb)) for p, q in zip(path, path[1:])):
                s.add_wire(path, junctions=False)
                return True
        return False

    def _grow_paper(self) -> bool:
        p = find(self.s.tree, "paper")
        sizes = ["A4", "A3", "A2", "A1", "A0"]
        if p is None or str(p[1]) not in sizes[:-1]:
            return False
        old = str(p[1])
        p[1] = sizes[sizes.index(old) + 1]
        grown = [g for g in self.report["paper"]]
        self.report["paper"] = [{"from": grown[0]["from"] if grown else old, "to": str(p[1])}]
        return True

    # --- connectivity -----------------------------------------------------------------

    def reconcile(self) -> None:
        s = self.s
        net_of = self._net_of()
        existing = {sym.ref for sym in s.symbols()}
        # 1. detach pins that sit on the wrong net
        for dname, dpins in drawn_nets(s).items():
            if len(dpins) < 2:
                continue
            target = self._majority(dname, dpins, net_of)
            for ref, pin in sorted(dpins):
                if net_of.get((ref, pin)) != target:
                    try:
                        s.detach(ref, pin)
                        self.report["detached"].append(f"{ref}.{pin}")
                    except env.KatError as e:
                        self.report["unresolved"].append({"pin": f"{ref}.{pin}", "reason": str(e)})
        # 2. attach pins missing from their net, and make sure each net carries its name
        drawn = drawn_nets(s)
        group_of = {pin: name for name, pins in drawn.items() for pin in pins}
        for ename, epins in sorted(self.nets.items()):
            epins = {p for p in epins if p[0] in existing}
            if not epins or (len(epins) < 2 and ename not in self.power):
                continue  # a lone pin on a signal net needs no drawing; on a power net it does
            # the main group is an existing wired/labelled group (2+ pins), or a lone pin that
            # already carries the net's name (its partner was just detached): re-labelling
            # that pin would stack a second stub + label on it
            groups = Counter(g for p in epins
                             if (g := group_of.get(p)) and (len(drawn[g]) > 1 or g == ename))
            main = max(groups, key=lambda g: (groups[g], g == ename, g)) if groups else None
            main_pins = drawn[main] if main else frozenset()
            missing = sorted(p for p in epins if p not in main_pins)
            # generated names (N$1 ...) only need a label when pins are joined by label
            if main != ename and len(main_pins) > 1 and (missing or not netlist._auto(ename)):
                self._name_group(main, ename, sorted(epins & main_pins)[0])
            if len(missing) == 2 and missing == sorted(epins) and self._pair_net(ename) \
                    and self._wire_pair(*missing):
                self.report["wired"].append(ename)
                continue
            for ref, pin in missing:
                self._attach(ref, pin, ename)
        s.fix_junctions()

    def _majority(self, dname: str, dpins, net_of) -> str | None:
        counts = Counter(net_of[p] for p in dpins if p in net_of)
        if not counts:
            return None
        best = max(counts.values())
        tied = sorted(n for n, k in counts.items() if k == best)
        return dname if dname in tied else tied[0]

    def _attach(self, ref: str, pin: str, net: str) -> None:
        s = self.s
        _, p = s.pin_owner(ref, pin)
        at, d = (p["x"], p["y"]), p["dir"]
        if net in self.power:
            reach = self._neighbour_reach(ref, p)
            if reach:
                # dense pin row (connector): a sideways power symbol's text would sit on the
                # neighbours' labels, so it goes out beyond them on a longer stub
                end = (at[0] + d[0] * reach, at[1] + d[1] * reach)
                if _path_free(s, at, end, clearance=0):
                    s.add_wire([at, end], junctions=False)
                    at = end
            self._power_symbol(net, at, d)
        else:
            ends = [(at[0] + d[0] * k, at[1] + d[1] * k) for k in (STUB, 2 * STUB, 3 * STUB)]
            end = next((e for e in ends if _path_free(s, at, e, clearance=0)), ends[0])
            s.add_wire([at, end], junctions=False)
            s.add_label(net, end[0], end[1], _dir_angle(d))
        self.report["attached"].append({"pin": f"{ref}.{pin}", "net": net})

    def _neighbour_reach(self, ref: str, p: dict) -> float:
        """Stub length that clears the labels of neighbouring pins (same part, same side,
        2.54 mm or closer) for a power symbol on pin ``p``; 0 if there are none."""
        s = self.s
        net_of = self._net_of()
        widths = []
        for sym in s.find_symbols(ref):
            for q in s.pins(sym):
                if q["number"] == p["number"] or q["dir"] != p["dir"]:
                    continue
                angle = {(-1, 0): 0, (1, 0): 180, (0, 1): 90, (0, -1): 270}[tuple(round(v) for v in p["dir"])]
                name = net_of.get((ref, q["number"]), "")
                if _row_neighbours((p["x"], p["y"]), (q["x"], q["y"]), angle) and name and name not in self.power:
                    widths.append(text_width(name))
        return _reach(widths)

    def _power_symbol(self, name: str, at, direction, lib_id: str | None = None):
        """Power-type symbol with its pin on ``at`` and its body pointing along ``direction``."""
        lib_id = lib_id or f"power:{name}"
        sdef = self.s.ensure_lib_symbol(lib_id)
        pin = library.pins(sdef)[0]
        box = library.outline(sdef, 1) or (0, 0, 0, 0)
        best = None
        for rot in (0, 90, 180, 270):
            pl = Placement(0, 0, rot)
            px, py = pl.to_sheet(pin.x, pin.y)
            cx, cy = pl.to_sheet((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            score = (cx - px) * direction[0] + (cy - py) * direction[1]
            if best is None or score > best[0] + 1e-6:
                best = (score, rot, px, py)
        _, rot, px, py = best
        sym = self.s.add_symbol(lib_id, at[0] - px, at[1] - py, rot)
        self.s.place_power_text(sym, direction)
        return sym

    def _name_group(self, old: str, new: str, anchor: Pin) -> None:
        """Give the drawn group ``old`` the name ``new``: rename its labels / power symbols,
        or add a label on one of its wires if it had no name."""
        s = self.s
        renamed = False
        for kind in ("label", "global_label", "hierarchical_label"):
            for n in find_all(s.tree, kind):
                if str(n[1]) == old:
                    n[1] = new
                    renamed = True
        for sym in s.symbols():
            if sym.ref.startswith("#PWR") and sym.field("Value") == old:
                at = s.pins(sym)[0]
                s.remove_symbol(sym.ref, sym.unit)
                if new in self.power:
                    self._power_symbol(new, (at["x"], at["y"]), _power_dir(s, sym))
                else:
                    s.add_label(new, at["x"], at["y"])
                renamed = True
        if not renamed:
            self._label_on_group(anchor, new)
        self.report["named"].append({"from": old, "to": new})

    def _label_on_group(self, anchor: Pin, name: str) -> None:
        s = self.s
        _, p = s.pin_owner(*anchor)
        seg = _longest_wire_from(s, (p["x"], p["y"]))
        if seg is None:
            self._attach(*anchor, name)
            return
        (ax, ay), (bx, by) = seg
        mx, my = snap((ax + bx) / 2), snap((ay + by) / 2)
        if name in self.power:
            self._power_symbol(name, (mx, my), (0, 1) if name.upper().startswith(("GND", "-", "V", "0")) else (0, -1))
        else:
            s.add_label(name, mx, my, 90 if abs(ax - bx) < 1e-3 else 0)

    # --- ERC driven additions -----------------------------------------------------------

    def power_flags(self) -> None:
        s = self.s
        erc = _drawn_erc(s)
        done: set[str] = set()
        for v in erc["violations"]:
            if v["type"] != "power_pin_not_driven":
                continue
            m = re.match(r"Symbol (\S+) Pin (\S+)", v["items"][0]["description"])
            if not m:
                continue
            ref, pin = m.groups()
            net = None
            if ref.startswith("#"):
                net = next((x.field("Value") for x in s.symbols() if x.ref == ref), None)
            else:
                net = self._net_of().get((ref, pin))
            if not net or net in done:
                continue
            done.add(net)
            if self._flag(net):
                self.report["flags"].append(net)

    def _flag(self, net: str) -> bool:
        s = self.s
        spots = [(sym, s.pins(sym)[0]) for sym in s.symbols()
                 if sym.ref.startswith("#PWR") and sym.field("Value") == net]
        for sym, p in spots:
            at = (p["x"], p["y"])
            d = _power_dir(s, sym)
            for side, length in ((1, 5.08), (-1, 5.08), (1, 7.62), (-1, 7.62)):
                perp = (-d[1] * side, d[0] * side)
                end = (at[0] + perp[0] * length, at[1] + perp[1] * length)
                if not _path_free(s, at, end):
                    continue
                s.add_wire([at, end], junctions=False)
                flag = self._power_symbol("PWR_FLAG", end, d, "power:PWR_FLAG")
                s.set_field(flag.ref, "Value", hide=True)
                return True
        return False

    # --- driver -------------------------------------------------------------------------

    def run(self) -> dict:
        self.sync_parts()
        self.reconcile()
        self.power_flags()
        self.s.fix_junctions()
        return self.report


def _power_dir(s: Schematic, sym) -> tuple[float, float]:
    """Unit vector from a power symbol's pin towards its body."""
    p = s.pins(sym)[0]
    b = s.body_box(sym)
    vx, vy = (b[0] + b[2]) / 2 - p["x"], (b[1] + b[3]) / 2 - p["y"]
    if abs(vx) >= abs(vy):
        return (1.0 if vx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if vy > 0 else -1.0)


def _path_free(s: Schematic, a, b, clearance: float = 2.0) -> bool:
    """True if a new straight wire a->b (plus a small symbol at b) would touch nothing
    except at ``a``: no pins, wire points, labels on it, and no symbol body around ``b``."""
    def on_seg(p) -> bool:
        if same(p, a):
            return False
        (ax, ay), (bx, by) = a, b
        within = min(ax, bx) - 1e-3 <= p[0] <= max(ax, bx) + 1e-3 and \
            min(ay, by) - 1e-3 <= p[1] <= max(ay, by) + 1e-3
        return within and (abs(ax - bx) < 1e-3 and abs(p[0] - ax) < 1e-3 or
                           abs(ay - by) < 1e-3 and abs(p[1] - ay) < 1e-3)

    pts = [(p["x"], p["y"]) for sym in s.symbols() for p in s.pins(sym)]
    pts += [q for seg in s.segments() for q in seg] + [(l["x"], l["y"]) for l in s.labels()]
    if any(on_seg(p) for p in pts):
        return False
    for seg in s.segments():  # crossing an existing wire's interior at b's side
        if _touches(b, seg):
            return False
    for sym in s.symbols():
        box = s.body_box(sym)
        if box and box[0] - clearance < b[0] < box[2] + clearance and \
                box[1] - clearance < b[1] < box[3] + clearance:
            return False
    return True


def _row_neighbours(p, q, angle: float) -> bool:
    """Two pin ends of one part side by side in a row (same side, at most 2.54 mm apart)."""
    horizontal = int(angle) % 180 == 0  # pin points along x: the row runs along y
    along, across = (q[0] - p[0], q[1] - p[1]) if horizontal else (q[1] - p[1], q[0] - p[0])
    return abs(along) < 1e-3 and abs(across) <= 2.54 + 1e-3


def _reach(widths: list[float]) -> float:
    """Stub that takes a power symbol beyond the longest neighbouring label; 0 if none."""
    return math.ceil((STUB + max(widths) + 2.54) / 2.54) * 2.54 if widths else 0.0


def _segment_clear(s: Schematic, p, q, ends) -> bool:
    """A new wire p-q would touch no pin, wire or label except at ``ends``, end on no
    wire's interior, and run through no part body."""
    from .schematic import _on_interior
    seg = (p, q)
    pts = [(x["x"], x["y"]) for sym in s.symbols() for x in s.pins(sym)]
    pts += [e for w in s.segments() for e in w] + [(l["x"], l["y"]) for l in s.labels()]
    for pt in pts:
        if any(same(pt, e) for e in ends):
            continue
        if same(pt, p) or same(pt, q) or _on_interior(pt, seg):
            return False
    if any(_on_interior(e, w) for w in s.segments() for e in (p, q)):
        return False
    lo_x, hi_x = sorted((p[0], q[0]))
    lo_y, hi_y = sorted((p[1], q[1]))
    for sym in s.symbols():
        box = s.body_box(sym)
        if box and lo_x < box[2] - 0.1 and hi_x > box[0] + 0.1 and lo_y < box[3] - 0.1 and hi_y > box[1] + 0.1:
            return False
    return True


def _longest_wire_from(s: Schematic, start):
    """Longest wire segment of the wired group that contains ``start``."""
    segs = s.segments()
    seen, frontier, group = set(), [start], []
    while frontier:
        p = frontier.pop()
        for i, seg in enumerate(segs):
            if i in seen:
                continue
            a, b = seg
            if same(a, p) or same(b, p) or _touches(p, seg):
                seen.add(i)
                group.append(seg)
                frontier += [a, b]
    if not group:
        return None
    return max(group, key=lambda g: abs(g[0][0] - g[1][0]) + abs(g[0][1] - g[1][1]))


def _touches(p, seg) -> bool:
    from .schematic import _on_interior
    return _on_interior(p, seg)


def run(sch: Path, design: Path, dry_run: bool = False) -> dict:
    s = Schematic(sch)
    report = Syncer(s, design).run()
    changed = any(report[k] for k in report if k != "unresolved")
    if not dry_run and changed:
        s.save()
    report["dry_run"] = dry_run
    report["changed"] = changed
    report["check"] = checks.run(sch, design) if not dry_run else None
    return report
