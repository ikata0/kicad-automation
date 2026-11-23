"""Assembly variants declared in a SKiDL script, written next to its netlist.

Call ``variant`` for each assembly, ``override`` for the parts that differ from the base and
``param`` for block parameters that differ, then ``write`` after ``generate_netlist``::

    from kat import skidl_variants as kv
    kv.variant("LITE", "no USB, cheaper op-amp")
    kv.override(esd, "LITE", dnp=True)                         # a SKiDL Part
    kv.override("CH_A/r_gain", "LITE", Value="4k7", MPN="RC0603FR-074K7L")   # a tag
    kv.override("r_gain", "LITE", block="CH_*", Value="4k7")   # in every matching block
    kv.param("/CH_B", "LITE", "ra", "6k8")                     # a sheet instance's parameter
    generate_netlist(file_="design.net")
    kv.write("design.net")                                     # -> design.variants.json

Flags: ``dnp``, ``in_bom``, ``on_board``, ``in_pos_files``, ``exclude_from_sim``; other
keywords are field values (``fields={...}`` for names that are no Python identifiers). A
Part is named by its reference when ``write`` runs, so it may be renamed before. ``kat
sync`` writes the declaration into the schematic (``kat.variants``).
"""

from __future__ import annotations

import json
from pathlib import Path

from .variants import load, variants_file

__all__ = ["clear", "override", "param", "variant", "write"]

_state: dict = {"variants": {}}


def clear() -> None:
    """Forget every declaration (a script that builds several designs in one run)."""
    _state["variants"] = {}


def variant(name: str, description: str = "") -> None:
    """Declare the assembly variant ``name`` (as KiCad shows it) with a ``description``."""
    v = _state["variants"].setdefault(name, {"description": "", "parts": [], "params": {}})
    if description:
        v["description"] = description


def _get(name: str) -> dict:
    if name not in _state["variants"]:
        raise ValueError(f"declare variant {name!r} first: kat.skidl_variants.variant({name!r})")
    return _state["variants"][name]


def override(part, name: str, block: str | None = None, fields: dict[str, str] | None = None,
             **values) -> None:
    """In variant ``name``, ``part`` (a SKiDL Part, or a tag qualified by its block path)
    differs from the base by ``values`` (flags true/false, field texts). ``block``: a glob
    on block instance paths; ``part`` is then a block-local tag."""
    values = {**values, **(fields or {})}
    if not values:
        raise ValueError("override: give flags or field values")
    if isinstance(part, str):
        sel = {"tag": part, **({"block": block} if block else {})}
    elif block:
        raise ValueError("override: block= goes with a tag, not with a Part")
    else:
        sel = {"part": part}
    _get(name)["parts"].append((sel, values))


def param(instance: str, name: str, parameter: str, value) -> None:
    """In variant ``name``, the block instance ``instance`` (sheet path, ``/CH_A``) has
    ``parameter`` = ``value``; parts using it (``${parameter}``) follow."""
    _get(name)["params"].setdefault("/" + instance.strip("/"), {})[parameter] = str(value)


def write(netlist: Path | str) -> Path:
    """Write the declarations to ``<netlist>.variants.json`` (after ``generate_netlist``,
    so Parts carry their final references) and check them. Returns the file."""
    out = {}
    for name, v in _state["variants"].items():
        parts = []
        for sel, values in v["parts"]:
            if "part" in sel:
                sel = {"ref": sel["part"].ref}
            parts.append({**sel, **values})
        out[name] = {"description": v["description"], "parts": parts,
                     **({"params": v["params"]} if v["params"] else {})}
    f = variants_file(netlist)
    f.write_text(json.dumps({"variants": out}, indent=2) + "\n", encoding="utf-8")
    load(netlist)
    return f
