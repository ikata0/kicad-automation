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

``connect_pins`` connects a part from a pin table, so that a computed assignment can reach the
design without editing the script (``kat.mapping.back_annotate``).

``project_libraries`` lets a script in a sub-folder of the project find the project's own
symbol libraries: SKiDL reads no ``sym-lib-table`` and searches only the working directory.
"""

from __future__ import annotations

import builtins
import inspect
import json
from pathlib import Path

from .netlist import blocks_file

__all__ = ["connect_pins", "pin_map", "project_libraries", "record", "write"]

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


def pin_map(table: Path | str, ref: str | None = None) -> dict[str, str]:
    """Read a ``pin,net`` or ``ref,pin,net`` table: {"J1.3": "CH1"}, or with ``ref`` only that
    part's {"3": "CH1"}; "" for an unconnected pin. Such a table is what ``kat.mapping.back_annotate``
    updates and ``kat pcb odb-pinout`` writes, so a pin assignment can change without editing the
    script."""
    from .mapping import pin_nets
    return pin_nets(table, ref)


def connect_pins(part, table: Path | str, ref: str | None = None, nets: dict | None = None) -> dict:
    """Connect a SKiDL part's pins as a table says. ``ref``: the part's reference in the table
    (default ``part.ref``). ``nets``: {name: Net} of nets the script already has; others are
    fetched by name, so one name is one net. An unconnected pin is joined to SKiDL's ``NC``.
    Returns {pin: net name}."""
    from skidl import Net
    assigned = pin_map(table, ref or part.ref)
    if not assigned:
        from .env import KatError
        raise KatError(f"{Path(table).name} has no row for {ref or part.ref}")
    for pin, name in assigned.items():
        if name:
            part[pin] += (nets or {}).get(name) or Net.fetch(name)
        else:
            part[pin] += builtins.NC
    return assigned


def write(netlist: Path | str, circuit=None) -> dict:
    """Write the block record next to the netlist (``design.net`` -> ``design.blocks.json``)
    and return it."""
    rec = record(circuit)
    out = blocks_file(Path(netlist))
    out.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    return dict(rec, file=str(out))


def project_libraries(project: Path | str | None = None) -> dict:
    """Add the KiCad project's symbol libraries (its ``sym-lib-table``, with ``${KIPRJMOD}``)
    to SKiDL's search path, so ``Part("MyLib", "X")`` works from any working directory.
    ``project``: the project folder (default: the nearest folder above the running script
    with a ``.kicad_pro`` or ``sym-lib-table``). SKiDL finds a library by its file name, so a
    nickname that differs from it is listed in ``renamed`` (use the file name there).
    Returns project, added (folders), libraries {nickname: file}, renamed.

    SKiDL still writes its by-products (``NAME.erc``, ``NAME.log``, ``NAME_sklib.py``) and a
    relative ``generate_netlist(file_=...)`` into the working directory: run the script
    from its own folder to keep them there."""
    import sys

    import skidl

    from . import env
    if project is None:
        start = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else Path.cwd()
        project = next((d for d in (start, *start.parents)
                        if (d / "sym-lib-table").exists() or any(d.glob("*.kicad_pro"))), None)
        if project is None:
            raise env.KatError(f"no KiCad project (.kicad_pro or sym-lib-table) in {start} or above")
    project = Path(project).resolve()
    table = project / "sym-lib-table"
    libs = env._read_lib_table(table, project) if table.exists() else {}
    libs = {k: v for k, v in libs.items() if v.exists()}
    paths = skidl.lib_search_paths.setdefault(skidl.KICAD10, [])
    added = []
    for folder in dict.fromkeys(str(v.parent) for v in libs.values()):
        if folder not in paths:
            paths.append(folder)
            added.append(folder)
    return {"project": str(project), "added": added, "libraries": {k: str(v) for k, v in libs.items()},
            "renamed": {k: v.stem for k, v in libs.items() if k != v.stem}}
