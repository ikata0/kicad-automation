"""The block record of a SKiDL design: instances, their type, ports and parameters.

SKiDL's netlist keeps only the instance path of each part. What a block instance was
connected to and with which values lives in SKiDL's node tree while the script runs: the
keyword arguments of a ``@subcircuit`` call are stored on its instance node. Call this at
the end of the SKiDL script, after ``generate_netlist``::

    block1(io=Interface(inp=a, out=b, gnd=gnd), ra="1k", tag="CH_A")
    generate_netlist(file_="design.net")
    kat.skidl_blocks.write("design.net")        # -> design.blocks.json

- instance name: the ``tag=`` of the call (SKiDL's own automatic name otherwise, which
  changes when calls are reordered: reported as a warning)
- ports: arguments that are nets (named by the argument) or an ``Interface`` (``arg.key``)
- parameters: arguments that are numbers or strings, plus the defaults left unset
- arguments passed by position are not recorded by SKiDL: reported as a warning, and the
  defaults of that call are then left out (it is unknown which ones were used)
- ``no_connect``: pins joined to SKiDL's ``NC`` (``REF.PIN``). The netlist leaves them out
  entirely; sync draws a no-connect flag on each.
"""

from __future__ import annotations

import builtins
import inspect
import json
from pathlib import Path

from .netlist import blocks_file

__all__ = ["record", "write"]

_OWN = {"func", "tag", "description", "purpose", "name", "circuit", "parent", "children",
        "parts", "nets", "buses", "skidl_trace", "fields", "pins", "__wrapped__"}


def _names(v) -> list[str] | None:
    """Net names of a SKiDL net-like value, None if it is not one."""
    from skidl import Bus, Net, Pin
    if isinstance(v, Net):
        return [v.name]
    if isinstance(v, Bus):
        return [n.name for n in v.nets]
    if isinstance(v, Pin):
        net = getattr(v, "net", None)
        return [net.name if net is not None else ""]
    return None


def _ports_and_params(node) -> tuple[dict, dict, dict, list[str]]:
    ports, params, other, missing = {}, {}, {}, []
    attrs = {k: v for k, v in vars(node).items() if k not in _OWN and not k.startswith("_")}
    from skidl import Interface
    for k, v in attrs.items():
        if isinstance(v, Interface):
            for key, item in v.items():
                if (names := _names(item)) is not None:
                    ports[f"{k}.{key}"] = names[0] if len(names) == 1 else names
        elif (names := _names(v)) is not None:
            ports[k] = names[0] if len(names) == 1 else names
        elif isinstance(v, (str, int, float, bool)):
            params[k] = v
        else:
            other[k] = repr(v)
    func = getattr(node, "func", None)
    if func is not None:
        unset = {name: p for name, p in inspect.signature(func).parameters.items()
                 if name not in attrs and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
        missing = [name for name, p in unset.items() if p.default is p.empty]
        # with positional arguments it is unknown which defaults were really used
        if not missing:
            params.update({name: p.default for name, p in unset.items()
                           if isinstance(p.default, (str, int, float, bool))})
    return ports, params, other, missing


def record(circuit=None) -> dict:
    """The block record of the running SKiDL circuit (default: SKiDL's default circuit)."""
    circuit = circuit or builtins.default_circuit
    instances, warnings = {}, []
    for node in sorted(circuit.nodes, key=lambda n: n.hiertuple):
        path = "/".join(node.hiertuple[1:])
        if not path:
            continue
        func = getattr(node, "func", None)
        kind = func.__name__ if func is not None else node.name
        ports, params, other, missing = _ports_and_params(node)
        if not node.tag:
            warnings.append(f"{path}: no tag= on the {kind} call; its name changes when calls "
                            f"are reordered")
        if missing:
            warnings.append(f"{path}: arguments {', '.join(missing)} of {kind} were passed by "
                            f"position; SKiDL only records keyword arguments")
        entry = {"type": kind, "ports": ports, "params": params,
                 "parts": sorted(p.ref for p in node.parts)}
        if other:
            entry["other"] = other
        instances[path] = entry
    from skidl.net import NCNet
    nc = sorted(f"{part.ref}.{pin.num}" for part in circuit.parts for pin in part.pins
                if any(isinstance(n, NCNet) for n in pin.nets))
    return {"instances": instances, "warnings": warnings, "no_connect": nc}


def write(netlist: Path | str, circuit=None) -> dict:
    """Write the block record next to the netlist (``design.net`` -> ``design.blocks.json``)
    and return it."""
    rec = record(circuit)
    out = blocks_file(Path(netlist))
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return dict(rec, file=str(out))
