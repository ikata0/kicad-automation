"""Symbol-library access: lookup, flattening of derived symbols, pins, outlines, search."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import env
from .sexpr import Sym, find, find_all, head, loads, prop


@dataclass
class LibPin:
    number: str
    name: str
    etype: str
    x: float  # electrical end point, library coordinates (y up)
    y: float
    angle: float
    length: float
    unit: int  # 0 = common to all units
    hidden: bool


@lru_cache(maxsize=64)
def _load_lib(path: str) -> list:
    return loads(Path(path).read_text(encoding="utf-8"))


def _lib_symbols(path: Path) -> dict[str, list]:
    return {str(s[1]): s for s in find_all(_load_lib(str(path)), "symbol")}


def resolve(lib_id: str, project_dir: Path | None = None) -> list:
    """Return the symbol definition for ``Lib:Name``, flattened and named ``Lib:Name``,
    ready to be placed into a schematic's ``lib_symbols``."""
    if ":" not in lib_id:
        raise env.KatError(f"lib_id must look like 'Library:Symbol', got {lib_id!r}")
    nick, name = lib_id.split(":", 1)
    libs = env.symbol_libraries(project_dir)
    if nick not in libs:
        raise env.KatError(f"unknown symbol library {nick!r}")
    syms = _lib_symbols(libs[nick])
    if name not in syms:
        close = [s for s in syms if name.lower() in s.lower()][:10]
        raise env.KatError(f"symbol {name!r} not in {nick}; similar: {close}")
    sym = _flatten(syms, name)
    sym[1] = f"{nick}:{name}"
    return sym


def _flatten(syms: dict[str, list], name: str, depth: int = 0) -> list:
    node = copy.deepcopy(syms[name])
    ext = find(node, "extends")
    if not ext or depth > 10:
        return node
    parent = _flatten(syms, str(ext[1]), depth + 1)
    pname = str(parent[1])
    out: list = [Sym("symbol"), name]
    child_keys = {head(c) for c in node if isinstance(c, list)}
    child_props = {c[1]: c for c in node if head(c) == "property"}
    for c in parent[2:]:
        h = head(c)
        if h == "property":
            out.append(child_props.pop(c[1], c))
        elif h == "symbol":  # sub-unit: rename Parent_U_S -> Child_U_S
            c = copy.deepcopy(c)
            c[1] = name + str(c[1])[len(pname):]
            out.append(c)
        elif h not in child_keys:
            out.append(c)
    for c in node[2:]:
        if head(c) not in ("extends", "property", "symbol"):
            out.insert(2, c)
    out.extend(child_props.values())
    # KiCad keeps properties before sub-units
    props = [c for c in out[2:] if head(c) != "symbol"]
    units = [c for c in out[2:] if head(c) == "symbol"]
    return out[:2] + props + units


def _unit_of(sub_name: str) -> tuple[int, int]:
    m = re.search(r"_(\d+)_(\d+)$", sub_name)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def pins(symdef: list) -> list[LibPin]:
    out = []
    for sub in find_all(symdef, "symbol"):
        unit, style = _unit_of(str(sub[1]))
        if style not in (0, 1):  # skip De Morgan alternates
            continue
        for p in find_all(sub, "pin"):
            at, ln = find(p, "at"), find(p, "length")
            nm, no = find(p, "name"), find(p, "number")
            out.append(LibPin(
                number=str(no[1]) if no else "", name=str(nm[1]) if nm else "",
                etype=str(p[1]), x=float(at[1]), y=float(at[2]),
                angle=float(at[3]) if len(at) > 3 else 0.0,
                length=float(ln[1]) if ln else 0.0, unit=unit,
                hidden=any(isinstance(t, Sym) and t == "hide" for t in p) or
                (find(p, "hide") is not None and str(find(p, "hide")[1]) == "yes")))
    return out


def unit_count(symdef: list) -> int:
    return max([_unit_of(str(s[1]))[0] for s in find_all(symdef, "symbol")] + [1])


def outline(symdef: list, unit: int) -> tuple[float, float, float, float] | None:
    """Bounding box (xmin, ymin, xmax, ymax) of body graphics and pins, library coords."""
    xs: list[float] = []
    ys: list[float] = []
    for sub in find_all(symdef, "symbol"):
        u, style = _unit_of(str(sub[1]))
        if u not in (0, unit) or style not in (0, 1):
            continue
        for g in sub:
            h = head(g)
            if h == "rectangle":
                for k in ("start", "end"):
                    pt = find(g, k); xs.append(float(pt[1])); ys.append(float(pt[2]))
            elif h in ("polyline", "bezier"):
                for xy in find_all(find(g, "pts") or [], "xy"):
                    xs.append(float(xy[1])); ys.append(float(xy[2]))
            elif h == "circle":
                c, r = find(g, "center"), float(find(g, "radius")[1])
                xs += [float(c[1]) - r, float(c[1]) + r]; ys += [float(c[2]) - r, float(c[2]) + r]
            elif h == "arc":
                for k in ("start", "mid", "end"):
                    pt = find(g, k); xs.append(float(pt[1])); ys.append(float(pt[2]))
            elif h == "pin":
                at, ln = find(g, "at"), find(g, "length")
                a = math.radians(float(at[3]) if len(at) > 3 else 0)
                L = float(ln[1]) if ln else 0
                x, y = float(at[1]), float(at[2])
                xs += [x, x + L * math.cos(a)]; ys += [y, y + L * math.sin(a)]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# --- search -----------------------------------------------------------------

_ENTRY = re.compile(r'^\t\(symbol "([^"]+)"(.*?)(?=^\t\(symbol "|\Z)', re.S | re.M)
_PROP = re.compile(r'\(property "(Description|ki_keywords|Value|Footprint)" "((?:[^"\\]|\\.)*)"')


def _index(project_dir: Path | None) -> list[dict]:
    libs = env.symbol_libraries(project_dir)
    key = {k: [str(v), v.stat().st_mtime] for k, v in libs.items()}
    cache = env.cache_dir() / "symbol-index.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if data.get("key") == key:
            return data["entries"]
    except UnicodeDecodeError as e:
        raise env.KatError(f"{cache} is not valid UTF-8: {e}") from e
    except (OSError, ValueError):
        pass
    entries = []
    for nick, path in libs.items():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise env.KatError(f"{path} is not valid UTF-8: {e}") from e
        for m in _ENTRY.finditer(text):
            props = dict(_PROP.findall(m.group(2)[:4000]))
            entries.append({"lib_id": f"{nick}:{m.group(1)}",
                            "description": props.get("Description", ""),
                            "keywords": props.get("ki_keywords", ""),
                            "footprint": props.get("Footprint", "")})
    cache.write_text(json.dumps({"key": key, "entries": entries}), encoding="utf-8")
    return entries


def search(query: str, limit: int = 30, project_dir: Path | None = None) -> list[dict]:
    """All words must match (case-insensitive) in lib_id, description or keywords.
    Matches in the symbol name rank first."""
    words = query.lower().split()
    hits = []
    for e in _index(project_dir):
        name = e["lib_id"].lower()
        hay = f'{name} {e["description"].lower()} {e["keywords"].lower()}'
        if all(w in hay for w in words):
            score = sum(w in name for w in words) * 10 - len(name) / 100
            hits.append((score, e))
    hits.sort(key=lambda t: -t[0])
    return [e for _, e in hits[:limit]]
