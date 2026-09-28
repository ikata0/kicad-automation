"""Stamping a checked block layout into a design: the block's placement, copper, keep-outs,
graphics and board-only parts are copied onto one instance of the block. Mixed into
``Board``.

A block board is the layout of one block, checked on its own (DRC clean). An instance is
named by its path, ``/CH_A`` or ``/CH_A/RC1``, the same way for both kinds of design:

- **hierarchical**: the instance is a sheet of the design's schematic. A part pairs with
  the block part whose footprint path is the end of its own path (the block's schematic
  file is shared, so symbol uuids are the same).
- **flat**: the instance is a SKiDL block (``Block`` field ``CH_A``). A part pairs with the
  block part that has the same tag relative to its instance (``CH_A/RC1/r`` ↔
  ``DUT/RC1/r``). On the block board, parts outside the block instance (a test harness,
  e.g. a connector) are its *environment* and are not copied, nor is copper touching them.

Nets are renamed through the paired pads; an instance wired differently from the block is
refused. Zones named in the block's ``environment`` (the block file ``NAME.block.json``,
see ``set_environment``) are what the block was checked against, e.g. a GND plane, and are
not copied. Everything copied goes into a KiCad group ``"/CH_A (NAME)"``; stamping the same
instance again replaces it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .. import env
from ..schematic import new_uuid
from ..sexpr import Sym, find, find_all, head, num
from . import footprints as fps
from .geometry import inside, rotate

COPPER = ("segment", "arc", "via", "zone")
GRAPHICS = ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve", "gr_text",
            "gr_text_box")


# --- the block file -----------------------------------------------------------------------

def block_file(block_pcb: Path | str) -> Path:
    """``NAME.block.json`` next to the block board."""
    return Path(block_pcb).with_suffix(".block.json")


def block_info(block_pcb: Path | str) -> dict:
    """The block file's content (``{}`` if there is none)."""
    f = block_file(block_pcb)
    return json.loads(f.read_text()) if f.exists() else {}


def set_environment(block_pcb: Path | str, zones: list[str]) -> dict:
    """Name the zones of a block board that are its test environment (e.g. the plane it
    was checked against): ``stamp`` does not copy them. Written to the block file."""
    info = block_info(block_pcb)
    info["environment"] = sorted(set(zones))
    block_file(block_pcb).write_text(json.dumps(info, indent=2) + "\n")
    return info


# --- helpers ------------------------------------------------------------------------------

def _norm(instance: str) -> str:
    return "/" + instance.strip("/")


def _xf(origin, at, rot):
    ox, oy = origin

    def f(x: float, y: float) -> tuple[float, float]:
        dx, dy = rotate(x - ox, y - oy, rot)
        return round(at[0] + dx, 4), round(at[1] + dy, 4)
    return f


def _move_points(node: list, f, rot: float) -> None:
    for c in node:
        if not isinstance(c, list):
            continue
        h = head(c)
        if h in ("start", "end", "mid", "center", "at", "xy") and len(c) >= 3:
            c[1], c[2] = (num(v) for v in f(float(c[1]), float(c[2])))
            if h == "at" and len(c) >= 4 and rot:
                c[3] = num((float(c[3]) + rot) % 360)
        elif h == "pts" or h not in ("net", "layer", "layers", "uuid"):
            _move_points(c, f, rot)


def _uuid(node: list) -> str | None:
    u = find(node, "uuid")
    return str(u[1]) if u is not None else None


def _fresh(node: list) -> str:
    u = new_uuid()
    find(node, "uuid")[1] = u
    return u


def _joined(instance: str, name: str) -> str:
    """Zone name of a copy: ``/CH_A: out node``; nested ``/CH_A`` + ``/RC1: out node``."""
    if name.startswith("/") and ": " in name:
        return instance + name
    return f"{instance}: {name}"


def _group_name(instance: str, name: str) -> str:
    return instance + name if name.startswith("/") else f"{instance} {name}"


def _item_layers(node: list) -> set[str]:
    out = set()
    for key in ("layer", "layers"):
        n = find(node, key)
        if n is not None:
            out |= {str(l) for l in n[1:] if isinstance(l, str) or not isinstance(l, list)}
    return {l for l in out if l.endswith(".Cu") and not l.startswith("*")}


class StampMixin:
    # --- instances ------------------------------------------------------------------------

    def _design_sheet(self) -> Path:
        return self.path.with_suffix(".kicad_sch")

    def _hier_prefix(self, instance: str) -> str | None:
        """uuid path of a sheet instance of the design's schematic, None if there is none."""
        from .. import hierarchy
        sch = self._design_sheet()
        if not sch.exists():
            return None
        try:
            return hierarchy.instance(sch, instance)["uuids"]
        except env.KatError:
            return None

    @staticmethod
    def _source_instance(src, name: str | None) -> str:
        """The block instance on a block board (flat): ``name``, or the only top-level
        ``Block`` there, or "" if the parts carry no Block (the whole board is the block)."""
        tops = {f.field("Block").split("/")[0] for f in src.footprints()
                if not f.board_only and f.field("Block")}
        if name is not None:
            return name.strip("/")
        if len(tops) > 1:
            raise env.KatError(f"the block board has several block instances {sorted(tops)}: "
                               f"name one with source_instance=")
        return tops.pop() if tops else ""

    def _pairs(self, src, instance: str, source_instance: str | None) -> tuple[dict, list, str]:
        """(source footprint uuid -> target Footprint, environment footprints, mode)."""
        from ..syncer import natural_key
        prefix = self._hier_prefix(instance)
        parts = [f for f in src.footprints() if not f.board_only]
        pairs: dict[str, object] = {}
        environment = []
        if prefix is not None:
            mode = "hierarchical"
            by_path = {f.path: f for f in self.footprints() if f.path}
            # a library block board: the block is the sheet instance /DUT, the rest harness
            sprefix = ""
            if source_instance and src.path.with_suffix(".kicad_sch").exists():
                from .. import hierarchy
                try:
                    sprefix = hierarchy.instance(src.path.with_suffix(".kicad_sch"), source_instance)["uuids"]
                except env.KatError:
                    sprefix = ""
            if sprefix:
                environment = [sf for sf in parts if not sf.path.startswith(sprefix + "/")]
                parts = [sf for sf in parts if sf.path.startswith(sprefix + "/")]
            for sf in parts:
                tf = by_path.get(prefix + sf.path[len(sprefix):])
                if tf is None:
                    raise env.KatError(f"{sf.ref}: no part {prefix + sf.path[len(sprefix):]} in {instance} "
                                       f"(is {instance} an instance of this block?)")
                pairs[sf.uuid] = tf
            inside_ = [f for f in self.footprints() if f.path.startswith(prefix + "/")]
        else:
            mode = "flat"
            name = instance.strip("/")
            sname = self._source_instance(src, source_instance)

            def local(f, root: str) -> str | None:
                tag, block = f.field("SKiDL Tag") or "", f.field("Block") or ""
                if root and not (block == root or block.startswith(root + "/")):
                    return None
                if root and tag.startswith(root + "/"):
                    return tag[len(root) + 1:]
                return tag or None

            hier_keys = _hier_source_keys(src, sname)

            keys: dict[str, object] = {}
            for sf in parts:
                k = hier_keys.get(sf.uuid, None) if hier_keys else local(sf, sname)
                if k is None:
                    environment.append(sf)
                    continue
                if k in keys:
                    raise env.KatError(f"block parts {keys[k].ref} and {sf.ref} share the tag {k!r}")
                keys[k] = sf
            if not keys:
                raise env.KatError("the block board has no parts with SKiDL tags to pair by")
            inside_ = [f for f in self.footprints() if not f.board_only and
                       ((f.field("Block") or "") == name or (f.field("Block") or "").startswith(name + "/"))]
            if not inside_:
                raise env.KatError(f"no instance {instance}: no sheet of that name and no parts "
                                   f"with Block = {name}")
            by_key = {local(f, name): f for f in inside_}
            for k, sf in keys.items():
                tf = by_key.get(k)
                if tf is None:
                    raise env.KatError(f"{sf.ref}: no part with tag {name}/{k} in {instance} "
                                       f"(is {instance} an instance of this block?)")
                pairs[sf.uuid] = tf
        extra = sorted({f.ref for f in inside_} - {f.ref for f in pairs.values()}, key=natural_key)
        if extra:
            raise env.KatError(f"{instance} has parts the block does not have: {', '.join(extra)}")
        return pairs, environment, mode

    def instances(self, block_pcb: Path | str, source_instance: str | None = None) -> list[str]:
        """Instances of a block in this design, as paths (``/CH_A``): sheets using the
        block's schematic file (hierarchical), or blocks whose parts pair with the block
        board's parts (flat)."""
        from .. import blocks as lib
        from .. import hierarchy
        from .board import Board
        block_pcb, lib_instance = lib.board_of(block_pcb, near=self.path)
        source_instance = source_instance or lib_instance
        sch = self._design_sheet()
        bsch = {block_pcb.with_suffix(".kicad_sch").resolve(),
                block_pcb.with_name(block_pcb.stem + "_block.kicad_sch").resolve()}
        if sch.exists():
            found = [i["path"] for i in hierarchy.tree(sch)
                     if Path(i["file"]).resolve() in bsch and i["path"] != "/"]
            if found:
                return found
        src = Board(block_pcb)
        out = []
        for name in sorted(self.blocks()):
            try:
                self._pairs(src, "/" + name, source_instance)
            except env.KatError:
                continue
            out.append("/" + name)
        return out

    # --- stamping ---------------------------------------------------------------------------

    def stamp(self, block_pcb: Path | str, instance: str | dict, at: tuple[float, float] | None = None,
              rot: float = 0.0, lock: bool = True, source_instance: str | None = None) -> dict:
        """Copy a checked block layout (a block board, or a block name from the library)
        onto one instance of the block: parts placed as on
        the block board, tracks, vias, zones, keep-outs, graphics and board-only parts
        copied with the instance's nets. ``at``: where the block board's origin (top-left
        of its outline) goes, mm; ``rot``: 0/90/180/270 degrees. Several instances at
        once: ``instance={"/CH_A": (x, y), "/CH_B": ((x, y), 180)}``. Copied copper is
        locked (``lock=False`` leaves it free), so routing keeps it. Stamping again
        replaces the earlier copy. Returns pairs, renamed nets and counts."""
        if isinstance(instance, dict):
            out = []
            for name, where in instance.items():
                pos, r = (where if len(where) == 2 and isinstance(where[0], (tuple, list)) else (where, rot))
                out.append(self.stamp(block_pcb, name, pos, r, lock, source_instance))
            return {"instances": out}
        from .. import blocks as lib
        from .board import Board
        if at is None:
            raise env.KatError("stamp needs at=(x, y)")
        if rot % 90:
            raise env.KatError("stamp turns blocks by multiples of 90 degrees only")
        block_pcb, lib_instance = lib.board_of(block_pcb, near=self.path)
        source_instance = source_instance or lib_instance
        warnings = []
        if block_info(block_pcb).get("type") and (why := lib.stale(block_pcb.parent)):
            warnings.append(f"block {block_pcb.stem}: {why}")
        src = Board(block_pcb)
        block = block_pcb.stem
        instance = _norm(instance)
        info = block_info(block_pcb)
        env_zones = set(info.get("environment", []))
        pairs, environment, mode = self._pairs(src, instance, source_instance)

        # layers: the block may only use copper the design has
        used = set()
        for n in src.tree:
            if head(n) in ("segment", "arc", "zone"):
                used |= _item_layers(n)
        for sf in src.footprints():
            if sf.uuid in pairs or sf.board_only:
                used |= {l for p in find_all(sf.node, "pad") for l in _item_layers(p)}
        missing = sorted(used - set(self.copper_layers()))
        if missing:
            raise env.KatError(f"the block uses copper layers the design lacks: {', '.join(missing)}")

        box = src.outline_box() or _parts_box(src, pairs)
        f = _xf(box[:2], at, rot)
        self._unstamp(instance)

        # parts
        netmap: dict[str, str] = {}
        env_pads = [p for ef in environment for p in ef.pads()]
        env_nets = {p.net for p in env_pads if p.net}
        members: dict[str, str] = {}          # source uuid -> target uuid (for groups)
        placed = {}
        for sf in src.footprints():
            if sf.uuid not in pairs:
                continue
            tf = pairs[sf.uuid]
            if tf.lib_id != sf.lib_id:
                raise env.KatError(f"{sf.ref} -> {tf.ref}: footprint {sf.lib_id} vs {tf.lib_id}")
            if tf.side != sf.side:
                tf.flip()
            x, y = f(sf.x, sf.y)
            tf.place(x, y, (sf.rot + rot) % 360)
            placed[sf.ref] = tf.ref
            members[sf.uuid] = tf.uuid
            for sp in sf.pads():
                if not sp.net:
                    continue
                tp = tf.pad(sp.number)
                if netmap.setdefault(sp.net, tp.net) != tp.net:
                    raise env.KatError(f"net {sp.net} maps to {netmap[sp.net]} and {tp.net}: "
                                       f"{instance} is wired differently from the block")
        target_nets = {p.net for p in self.pads() if p.net} | {
            str(find(z, "net")[1]) for z in self.zones() if find(z, "net") is not None}
        self._ungroup([tf.uuid for tf in pairs.values()])

        def net_of(name: str) -> str | None:
            if name in netmap:
                return netmap[name]
            if name in env_nets:
                return None
            if name in target_nets:
                return name
            raise env.KatError(f"block net {name!r} has no pads in the block and no net of that "
                               f"name in the design")

        def on_env_pad(pt) -> bool:
            return any(inside(p.outline, pt) for p in env_pads)

        counts = {"footprints": len(placed), "tracks": 0, "vias": 0, "zones": 0, "keepouts": 0,
                  "graphics": 0, "board_parts": 0, "skipped_environment": 0}
        for n in list(src.tree):
            h = head(n)
            if h in COPPER:
                if h == "zone":
                    zname = find(n, "name")
                    if zname is not None and str(zname[1]) in env_zones:
                        counts["skipped_environment"] += 1
                        continue
                c = copy.deepcopy(n)
                net = find(c, "net")
                if net is not None and str(net[1]):
                    mapped = net_of(str(net[1]))
                    if mapped is None:
                        counts["skipped_environment"] += 1
                        continue
                    net[1] = mapped
                if h in ("segment", "arc") and any(
                        on_env_pad((float(p[1]), float(p[2]))) for p in (find(n, "start"), find(n, "end"))):
                    counts["skipped_environment"] += 1
                    continue
                if h == "zone":
                    c[:] = [x for x in c if head(x) != "filled_polygon"]
                    zname = find(c, "name")
                    if zname is not None:
                        zname[1] = _joined(instance, str(zname[1]))
                    else:
                        c.insert(2, [Sym("name"), _joined(instance, "zone")])
                    counts["keepouts" if find(c, "keepout") is not None else "zones"] += 1
                else:
                    counts["vias" if h == "via" else "tracks"] += 1
                    if lock and find(c, "locked") is None:
                        c.insert(next(i for i, x in enumerate(c) if isinstance(x, list)
                                      and head(x) in ("layer", "layers")), [Sym("locked"), Sym("yes")])
            elif h in GRAPHICS:
                lay = find(n, "layer")
                if lay is not None and str(lay[1]) == "Edge.Cuts":
                    continue
                c = copy.deepcopy(n)
                counts["graphics"] += 1
            elif h == "footprint":
                if not any(str(x) == "board_only" for x in (find(n, "attr") or [])[1:]):
                    continue
                c = copy.deepcopy(n)
                ref = next(p for p in find_all(c, "property") if str(p[1]) == "Reference")
                ref[2] = self._next_board_ref(str(ref[2]).rstrip("0123456789") or "H")
                counts["board_parts"] += 1
            else:
                continue
            if h == "footprint":            # origin and rotation only: its insides are local
                sat = find(n, "at")
                fps.set_rotation(c, (float(sat[3]) if len(sat) > 3 else 0.0) + rot)
                cat = find(c, "at")
                cat[1], cat[2] = (num(v) for v in f(float(sat[1]), float(sat[2])))
            else:
                _move_points(c, f, rot)
            members[_uuid(n)] = _fresh(c)
            self._insert(c)

        groups = self._copy_groups(src, instance, block, members)
        return {"instance": instance, "block": block, "mode": mode, "at": list(at), "rot": rot,
                "pairs": placed, "nets": {k: v for k, v in netmap.items() if k != v},
                "groups": groups, **counts, **({"warnings": warnings} if warnings else {})}

    def _next_board_ref(self, prefix: str) -> str:
        used = {f.ref for f in self.footprints()}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    # --- groups ---------------------------------------------------------------------------

    def _groups(self) -> list[list]:
        return find_all(self.tree, "group")

    def _ungroup(self, uuids: list[str]) -> None:
        """Take items out of any group (KiCad allows one group per item)."""
        want = set(uuids)
        for g in self._groups():
            m = find(g, "members")
            if m is not None:
                m[1:] = [u for u in m[1:] if str(u) not in want]

    def _unstamp(self, instance: str) -> int:
        """Remove an earlier stamp of ``instance``: its group, sub-groups, and the copied
        copper, graphics and board-only parts in them (the design's parts stay)."""
        groups = {_uuid(g): g for g in self._groups()}
        roots = [g for g in groups.values() if str(g[1]).startswith(f"{instance} (")]
        if not roots:
            return 0
        drop_groups, drop_items = set(), set()
        todo = list(roots)
        while todo:
            g = todo.pop()
            drop_groups.add(_uuid(g))
            for u in (find(g, "members") or [None])[1:]:
                u = str(u)
                if u in groups and u not in drop_groups:
                    todo.append(groups[u])
                else:
                    drop_items.add(u)
        keep = []
        removed = 0
        for n in self.tree:
            h = head(n)
            u = _uuid(n) if isinstance(n, list) else None
            if h == "group" and u in drop_groups:
                removed += 1
                continue
            if u in drop_items and (h in COPPER or h in GRAPHICS or (
                    h == "footprint" and any(str(t) == "board_only" for t in (find(n, "attr") or [])[1:]))):
                removed += 1
                continue
            keep.append(n)
        self.tree[:] = keep
        self._ungroup(list(drop_groups | drop_items))   # a parent group of a nested stamp
        return removed

    def _copy_groups(self, src, instance: str, block: str, members: dict[str, str]) -> list[str]:
        """The instance group with the block board's own groups copied inside it."""
        sgroups = {_uuid(g): g for g in find_all(src.tree, "group")}
        new_uuid_of = {u: new_uuid() for u in sgroups}
        grouped = set()
        names = []
        for u, g in sgroups.items():
            mem = []
            for m in (find(g, "members") or [None])[1:]:
                m = str(m)
                if m in new_uuid_of:
                    mem.append(new_uuid_of[m])
                    grouped.add(m)
                elif m in members:
                    mem.append(members[m])
                    grouped.add(m)
            name = _group_name(instance, str(g[1]))
            names.append(name)
            self._insert([Sym("group"), name, [Sym("uuid"), new_uuid_of[u]], [Sym("members"), *mem]])
        top = [new_uuid_of[u] for u in sgroups if not any(
            u in {str(m) for m in (find(g2, "members") or [None])[1:]} for g2 in sgroups.values())]
        top += [t for s, t in members.items() if s not in grouped]
        name = f"{instance} ({block})"
        self._insert([Sym("group"), name, [Sym("uuid"), new_uuid()], [Sym("members"), *top]])
        return [name] + names


def _hier_source_keys(src, sname: str) -> dict[str, str]:
    """For a block board whose schematic has sheets (a library block with nested blocks):
    footprint uuid -> key relative to the block instance, ``RC1/r`` (sheet path + the
    part's own tag), because a shared sheet file gives every instance the same fields.
    Empty if the schematic has no sheets below the block instance."""
    from .. import hierarchy
    sch = src.path.with_suffix(".kicad_sch")
    if not sname or not sch.exists():
        return {}
    tree = hierarchy.tree(sch)
    base = "/" + sname
    below = [i for i in tree if i["path"] == base or i["path"].startswith(base + "/")]
    if not any(i["path"].startswith(base + "/") for i in below):
        return {}
    out = {}
    for f in src.footprints():
        if f.board_only or not f.path:
            continue
        inst = max((i for i in below if f.path.startswith(i["uuids"] + "/")),
                   key=lambda i: len(i["uuids"]), default=None)
        if inst is None:
            continue                                  # the harness: environment
        tag, block = f.field("SKiDL Tag") or "", f.field("Block") or ""
        seg = block.split("/")[0]
        own = tag[len(seg) + 1:] if seg and tag.startswith(seg + "/") else tag
        rel = inst["path"][len(base):].strip("/")
        out[f.uuid] = f"{rel}/{own}" if rel else own
    return out


def _parts_box(src, pairs) -> tuple[float, float, float, float]:
    boxes = [f.box() for f in src.footprints() if f.uuid in pairs and f.box()]
    if not boxes:
        raise env.KatError("the block board has no outline and no parts")
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))
