"""Footprint libraries: load a .kicad_mod and turn it into a board footprint.

A library footprint and a board footprint are the same s-expression except for a few
header items; the board copy carries position, fresh uuids, absolute pad/text angles and,
on the bottom side, geometry mirrored in y (KiCad's own storage convention).
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path

from .. import env
from ..schematic import new_uuid
from ..sexpr import Sym, find, find_all, head, loads, num


@lru_cache(maxsize=512)
def _load(path: str) -> list:
    return loads(Path(path).read_text(encoding="utf-8"))


def path_of(lib_id: str, project_dir: Path | None = None) -> Path:
    """File of ``Lib:Name`` from fp-lib-table (global + project)."""
    nick, _, name = lib_id.partition(":")
    libs = env.footprint_libraries(project_dir)
    if nick not in libs:
        raise env.KatError(f"unknown footprint library {nick!r} (fp-lib-table)")
    path = libs[nick] / f"{name}.kicad_mod"
    if not path.is_file():
        close = sorted(p.stem for p in libs[nick].glob("*.kicad_mod") if name.lower()[:6] in p.stem.lower())
        raise env.KatError(f"footprint {name!r} not in {nick}; similar: {close[:10]}")
    return path


def summary(path: Path | str) -> dict:
    """Pad numbers and 3D model paths of a .kicad_mod file."""
    tree = _load(str(path))
    return {"pads": frozenset(str(p[1]) for p in find_all(tree, "pad") if str(p[1])),
            "models": [str(m[1]) for m in find_all(tree, "model")]}


_HEADER = {"version", "generator", "generator_version"}
_POINTS = {"at", "start", "end", "mid", "center", "xy", "offset"}
_TEXTS = {"property", "fp_text", "fp_text_box"}


def swap_side(layer: str) -> str:
    if layer.startswith("F."):
        return "B." + layer[2:]
    if layer.startswith("B."):
        return "F." + layer[2:]
    return layer


def mirror(node: list, top: bool = True) -> None:
    """Mirror footprint-local geometry in y and swap F./B. layers, in place (KiCad's flip)."""
    for c in node:
        if not isinstance(c, list):
            continue
        h = head(c)
        if h in _POINTS and not (top and h == "at"):
            if len(c) > 2:
                c[2] = _neg(c[2])
            if h == "at" and len(c) > 3 and not isinstance(c[3], list):
                c[3] = _neg(c[3])
        elif h in ("layer", "layers"):
            for j in range(1, len(c)):
                if isinstance(c[j], str) and not isinstance(c[j], Sym):
                    c[j] = swap_side(c[j])
        elif h == "effects":
            just = find(c, "justify")
            if just is None:
                c.append([Sym("justify"), Sym("mirror")])
            elif any(str(t) == "mirror" for t in just[1:]):
                just[:] = [t for t in just if str(t) != "mirror"]
                if len(just) == 1:
                    c.remove(just)
            else:
                just.append(Sym("mirror"))
            continue
        if h != "model":  # 3D model offsets follow the footprint side by themselves
            mirror(c, top=False)


def turn(node: list, delta: float) -> None:
    """Add ``delta`` degrees to the absolute angles of pads and texts."""
    if not delta % 360:
        return
    for c in node:
        if head(c) == "pad" or head(c) in _TEXTS:
            at = find(c, "at")
            if at is None:
                continue
            ang = ((float(at[3]) if len(at) > 3 else 0.0) + delta) % 360
            if len(at) > 3:
                at[3] = num(ang)
            elif ang:
                at.append(num(ang))


def instantiate(lib_id: str, x: float, y: float, rot: float = 0.0, side: str = "F",
                project_dir: Path | None = None) -> list:
    """Board footprint node for ``Lib:Name`` at (x, y), rotation ``rot`` (as KiCad shows
    it), ``side`` "F" or "B"."""
    src = _load(str(path_of(lib_id, project_dir)))
    node = [Sym("footprint"), lib_id]
    for c in src[2:]:
        if head(c) not in _HEADER:
            node.append(copy.deepcopy(c))
    _fresh_uuids(node)
    idx = next((i for i, c in enumerate(node) if head(c) == "layer"), 1) + 1
    node[idx:idx] = [[Sym("uuid"), new_uuid()], [Sym("at"), num(x), num(y)]]
    if side == "B":
        mirror(node)
    set_rotation(node, rot)
    return node


def set_rotation(node: list, rot: float) -> None:
    at = find(node, "at")
    old = float(at[3]) if len(at) > 3 else 0.0
    rot = rot % 360
    turn(node, rot - old)
    del at[3:]
    if rot:
        at.append(num(rot))


def _fresh_uuids(node: list) -> None:
    for c in node:
        if isinstance(c, list):
            if head(c) == "uuid" and len(c) > 1:
                c[1] = new_uuid()
            else:
                _fresh_uuids(c)


def _neg(tok: Sym) -> Sym:
    """Negate a number token exactly (library values carry 6 decimals)."""
    t = str(tok)
    if float(t) == 0:
        return Sym("0")
    return Sym(t[1:] if t.startswith("-") else "-" + t)
