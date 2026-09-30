"""Read KiCad s-expression netlists (from kicad-cli or SKiDL) and compare them."""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
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
    tag: str  # SKiDL tag qualified by the block instance ("CH_A/RC1/r"), "" if absent
    fields: dict[str, str] = field(default_factory=dict)  # custom fields, e.g. MPN
    block: str = ""  # block instance path ("CH_A/RC1"), "" at the top level


# netlist fields that are not custom part data
_STANDARD = {"Reference", "Value", "Footprint", "Description", "SKiDL Tag", "SKiDL Line"}
BLOCK, BLOCK_TYPE = "Block", "Block Type"


def blocks_file(path: Path) -> Path:
    """The block record written next to a netlist by ``kat.skidl_blocks.write``."""
    return Path(path).with_suffix(".blocks.json")


def _block_types(path: Path) -> dict[str, str]:
    f = blocks_file(path)
    if not f.exists():
        return {}
    return {name: i["type"] for name, i in json.loads(f.read_text(encoding="utf-8")).get("instances", {}).items()}


def read_design(path: Path) -> tuple[dict[str, Component], dict[str, Net]]:
    """Components (by ref) and nets of a KiCad s-expression netlist, e.g. from SKiDL.

    A part inside a SKiDL subcircuit gets its instance path as ``block`` (from the
    netlist's sheet path, e.g. ``CH_A/RC1``), the ``Block`` field, ``Block Type`` (when the
    block record exists) and a tag qualified by the path (``CH_A/RC1/r``): SKiDL tags are
    local to a block, so they repeat across instances."""
    tree = loads(Path(path).read_text(encoding="utf-8"))
    types = _block_types(path)
    comps = {}
    for c in find_all(find(tree, "components") or [], "comp"):
        src = find(c, "libsource")
        fields = {str(find(f, "name")[1]): (str(f[2]) if len(f) > 2 else "")
                  for f in find_all(find(c, "fields") or [], "field")}
        val, fp = find(c, "value"), find(c, "footprint")
        ref = str(find(c, "ref")[1])
        sheet = find(c, "sheetpath")
        block = str(find(sheet, "names")[1]).strip("/") if sheet and find(sheet, "names") else ""
        tag = fields.get("SKiDL Tag", "")
        custom = {k: v for k, v in fields.items()
                  if k not in _STANDARD and not (k == "Datasheet" and not v)}
        if block:
            custom[BLOCK] = block
            if block in types:
                custom[BLOCK_TYPE] = types[block]
        comps[ref] = Component(
            ref=ref, lib_id=f'{find(src, "lib")[1]}:{find(src, "part")[1]}',
            value=str(val[1]) if val and len(val) > 1 else "",
            footprint=str(fp[1]) if fp and len(fp) > 1 else "",
            tag=f"{block}/{tag}" if block and tag else tag, fields=custom, block=block)
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


_COUNTER = re.compile(r"^(.*?)_?\d+$")


def _numbered(a: str, e: str) -> bool:
    """SKiDL numbers a net name that repeats across block instances (``LED_A`` in the
    second instance becomes ``LED_A1``, ``SW0`` becomes ``SW0_1``), while KiCad keeps the
    block's own name under the instance's sheet path (``/ROW0/CELL1/LED_A``). Such a pair
    is the same name. Only a name under a sheet path counts, so ``/SIG`` vs ``SIG2`` on
    the root sheet still differs."""
    for local, counted in ((a, e), (e, a)):
        m = _COUNTER.match(counted.rsplit("/", 1)[-1])
        if m and local.count("/") >= 2 and m.group(1) == local.rsplit("/", 1)[-1]:
            return True
    return False


def compare(actual: dict[str, Net], expected: dict[str, Net], limit: int | None = 10) -> dict:
    """Compare connectivity as a partition of pins; names only matter when both sides
    chose them deliberately (SKiDL's instance numbering of block-local names does not
    count). Single-pin nets (unconnected pins) are ignored. ``counts`` has the length of
    every list; the lists are cut to ``limit`` entries (None: all)."""
    def groups(nets):
        return {pins: name for name, pins in nets.items() if len(pins) > 1}

    a, e = groups(actual), groups(expected)
    missing = [{"net": e[p], "pins": sorted(p)} for p in e if p not in a]
    extra = [{"net": a[p], "pins": sorted(p)} for p in a if p not in e]
    renamed, unnamed = [], []
    for p in set(a) & set(e):
        an, en = a[p].rsplit("/", 1)[-1], e[p].rsplit("/", 1)[-1]
        if an != en and not _auto(an) and not _auto(en) and not _numbered(a[p], e[p]):
            renamed.append({"pins": sorted(p), "expected": en, "actual": an})
        elif _auto(an) and not _auto(en):
            unnamed.append(en)  # connected correctly, but the design's net name is not shown
    out = {"match": not missing and not extra, "missing": missing, "extra": extra,
           "name_differences": sorted(renamed, key=lambda r: r["expected"]),
           "unnamed": sorted(unnamed)}
    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    if limit is not None:
        for k in out["counts"]:
            out[k] = out[k][:limit]
    return out
