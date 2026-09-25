"""Read KiCad s-expression netlists (from kicad-cli or SKiDL) and compare them."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import env
from .sexpr import find, find_all, loads

Net = frozenset  # of (ref, pin)


def read(path: Path) -> dict[str, Net]:
    """Net name -> set of (ref, pin). Accepts KiCad's ``kicadsexpr`` netlist format."""
    tree = loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for n in find_all(find(tree, "nets") or [], "net"):
        name = str(find(n, "name")[1])
        nodes = frozenset((str(find(x, "ref")[1]), str(find(x, "pin")[1])) for x in find_all(n, "node"))
        out[name] = nodes
    return out


@dataclass
class Component:
    ref: str
    lib_id: str
    value: str
    footprint: str
    tag: str  # SKiDL tag ("" if absent); stable identity across design changes


def read_design(path: Path) -> tuple[dict[str, Component], dict[str, Net]]:
    """Components (by ref) and nets of a KiCad s-expression netlist, e.g. from SKiDL."""
    tree = loads(Path(path).read_text(encoding="utf-8"))
    comps = {}
    for c in find_all(find(tree, "components") or [], "comp"):
        src = find(c, "libsource")
        fields = {str(find(f, "name")[1]): (str(f[2]) if len(f) > 2 else "")
                  for f in find_all(find(c, "fields") or [], "field")}
        val, fp = find(c, "value"), find(c, "footprint")
        ref = str(find(c, "ref")[1])
        comps[ref] = Component(
            ref=ref, lib_id=f'{find(src, "lib")[1]}:{find(src, "part")[1]}',
            value=str(val[1]) if val and len(val) > 1 else "",
            footprint=str(fp[1]) if fp and len(fp) > 1 else "",
            tag=fields.get("SKiDL Tag", ""))
    return comps, read(path)


def from_schematic(sch: Path) -> dict[str, Net]:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "net.net"
        env.run_cli("sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(sch))
        return read(out)


def _auto(name: str) -> bool:
    """KiCad/SKiDL generated names carry no design intent."""
    base = name.rsplit("/", 1)[-1]
    return base.startswith(("Net-(", "unconnected-(", "N$", "NET_")) or base == ""


def compare(actual: dict[str, Net], expected: dict[str, Net]) -> dict:
    """Compare connectivity as a partition of pins; names only matter when both sides
    chose them deliberately. Single-pin nets (unconnected pins) are ignored."""
    def groups(nets):
        return {pins: name for name, pins in nets.items() if len(pins) > 1}

    a, e = groups(actual), groups(expected)
    missing = [{"net": e[p], "pins": sorted(p)} for p in e if p not in a]
    extra = [{"net": a[p], "pins": sorted(p)} for p in a if p not in e]
    renamed, unnamed = [], []
    for p in set(a) & set(e):
        an, en = a[p].rsplit("/", 1)[-1], e[p].rsplit("/", 1)[-1]
        if an != en and not _auto(an) and not _auto(en):
            renamed.append({"pins": sorted(p), "expected": en, "actual": an})
        elif _auto(an) and not _auto(en):
            unnamed.append(en)  # connected correctly, but the design's net name is not shown
    return {"match": not missing and not extra, "missing": missing, "extra": extra,
            "name_differences": renamed, "unnamed": sorted(unnamed)}
