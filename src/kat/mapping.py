"""Check a design against an independently specified pin mapping (CSV or JSON).

A netlist comparison says the drawing matches the design; it cannot say the design is what a
datasheet, a mating board or a legacy product asks for. A mapping table can. Rows name pins
and what they must be on:

    pin,net                  J1.3,CH1
    ref,pin,net              J1,3,CH1
    source,channel,destination     J1.3,5,U2.17

- ``net`` asserts the name: empty or ``NC`` means the pin must be unconnected.
- ``channel`` is a logical name: its pins share a net, and two channels never do.
- Several pins in one row (``source``, ``destination``, ...) must share a net.

A net name without ``/`` matches the last part of the actual name (``CH1`` matches
``/CH_A/CH1``); with one it must match in full, apart from the leading ``/``.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from . import env, netlist, report

__all__ = ["check", "read", "write"]

PIN_COLUMNS = ("pin", "source", "destination", "dest", "from", "to")
NOT_CONNECTED = ("", "nc")
_UNCONNECTED = "unconnected-("


def _split_pin(text: str, where: str) -> tuple[str, str]:
    ref, dot, number = text.strip().partition(".")
    if not dot or not ref or not number:
        raise env.KatError(f"{where}: {text!r} is not a pin (REF.NUMBER, or columns ref and pin)")
    return ref, number


def _table(path: Path) -> list[dict]:
    """The rows of a CSV or JSON file as dictionaries with lower-case keys."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):        # {"J1.3": "CH1"} or {"rows": [...]}
            data = data["rows"] if isinstance(data.get("rows"), list) else [
                {"pin": k, "net": v} for k, v in data.items()]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise env.KatError(f"{path.name}: expected a list of rows or an object of pin: net")
        return [{str(k).strip().lower(): "" if v is None else str(v) for k, v in r.items()} for r in data]
    lines = [l for l in io.StringIO(text) if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        return []
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(lines, dialect=dialect)
    return [{str(k).strip().lower(): (v or "").strip() for k, v in r.items() if k is not None} for r in reader]


def read(mapping: Path | str, pins: list[str] | None = None) -> dict:
    """Read a mapping table (CSV, ``;`` or tab also, ``#`` starts a comment line; or JSON, a
    list of rows or an object of pin: net). ``pins``: the columns holding pins, default
    ``ref``+``pin``, ``pin``, ``source``, ``destination``, ``dest``, ``from``, ``to``. Other
    columns than these, ``net`` and ``channel`` are listed in ``ignored``. Returns rows [{row,
    pins [(ref, number)], net (None: not asserted), channel}] and ignored. A pin given twice
    with different assignments is an error."""
    path = Path(mapping)
    table = _table(path)
    columns = [c.lower() for c in pins] if pins else None
    rows, ignored = [], set()
    seen: dict[tuple[str, str], tuple[int, str | None, str | None]] = {}
    for k, r in enumerate(table, start=1):
        where = f"{path.name} row {k}"
        use = columns or [c for c in PIN_COLUMNS if c in r]
        if not use and "ref" not in r:
            raise env.KatError(f"{where}: no pin column; expected one of {', '.join(PIN_COLUMNS)} or ref and pin")
        got = []
        if not columns and "ref" in r:
            if "pin" not in r:
                raise env.KatError(f"{where}: column ref needs a column pin")
            if r["ref"] or r["pin"]:
                if not r["ref"] or not r["pin"]:
                    raise env.KatError(f"{where}: ref and pin are both needed")
                got.append((r["ref"], r["pin"]))
            use = [c for c in use if c != "pin"]
        for c in use:
            if c not in r:
                raise env.KatError(f"{where}: no column {c!r}")
            if r[c]:
                got.append(_split_pin(r[c], where))
        if not got:
            raise env.KatError(f"{where}: no pin")
        net = r["net"] if "net" in r else None
        channel = r.get("channel") or None
        if net is not None and not net and channel:     # a channel that names no net asserts none
            net = None
        rows.append({"row": k, "pins": got, "net": net, "channel": channel})
        for pin in got:
            old = seen.setdefault(pin, (k, net, channel))
            if old[1:] != (net, channel) or (net is None and channel is None and old[0] != k):
                raise env.KatError(f"{path.name}: {pin[0]}.{pin[1]} is in row {old[0]} and row {k} "
                                   "with different assignments")
        ignored |= set(r) - set(use) - {"ref", "pin", "net", "channel"}
    return {"file": str(path), "rows": rows, "ignored": sorted(ignored)}


def _same_name(expected: str, actual: str) -> bool:
    e, a = expected.strip("/"), actual.strip("/")
    return e == a if "/" in e else e == a.rsplit("/", 1)[-1]


def _connected(net: str | None) -> bool:
    return net is not None and not net.rsplit("/", 1)[-1].startswith(_UNCONNECTED)


def _actual(source: Path) -> dict[tuple[str, str], str]:
    """(ref, pin) -> net name of a schematic, board or netlist. A pin on no net is absent or
    has an ``unconnected-(`` name."""
    kind = source.suffix.lower()
    if kind == ".kicad_sch":
        nets = netlist.from_schematic(source)
    elif kind in (".net", ".sexp"):
        nets = netlist.read(source)
    elif kind == ".kicad_pcb":
        from .pcb.board import Board
        out: dict[tuple[str, str], str] = {}
        for f in Board(source).footprints():
            for p in f.pads():
                if p.number and (p.net or (f.ref, p.number) not in out):
                    out[(f.ref, p.number)] = p.net or f"{_UNCONNECTED}{f.ref}-Pad{p.number})"
        return out
    else:
        raise env.KatError(f"{source.name}: give a .kicad_sch, .kicad_pcb or netlist (.net)")
    return {pin: name for name, pins in nets.items() for pin in pins}


def _pin(pin: tuple[str, str]) -> str:
    return f"{pin[0]}.{pin[1]}"


def check(source: Path | str, mapping: Path | str, complete: bool | list[str] = True,
          pins: list[str] | None = None) -> dict:
    """Compare ``source`` (.kicad_sch, .kicad_pcb or a netlist) with a mapping
    table (``read``). ``complete``: connected pins of the parts in the mapping (True), of
    these references (a list) or of none (False) must be in the table. ``pins``: see ``read``.

    Returns ``ok`` and, each with its row: ``missing`` (pins or parts the design lacks),
    ``mismatched`` (expected and actual net), ``split`` (pins that should share a net but do
    not), ``merged`` (rows or channels that should be separate but share a net), ``extra``
    (connected pins the table lacks), with ``counts`` and the ``ignored`` columns."""
    source, table = Path(source), read(mapping, pins)
    report.touch(source)
    actual = _actual(source)
    known = {ref for ref, _ in actual}
    missing, mismatched, split = [], [], []
    groups: dict[str, list[dict]] = {}        # channel or row -> what it is on
    for r in table["rows"]:
        nets: dict[str, list[str]] = {}
        for pin in r["pins"]:
            net = actual.get(pin)
            wanted_open = r["net"] is not None and r["net"].lower() in NOT_CONNECTED
            if pin not in actual and not (wanted_open and pin[0] in known):
                missing.append({"row": r["row"], "pin": _pin(pin),
                                "problem": "pin" if pin[0] in known else "part"})
                continue
            if r["net"] is not None:
                if wanted_open:
                    if _connected(net):
                        mismatched.append({"row": r["row"], "pin": _pin(pin), "expected": "", "actual": net})
                    continue
                if not _connected(net):
                    mismatched.append({"row": r["row"], "pin": _pin(pin), "expected": r["net"], "actual": ""})
                    continue
                if not _same_name(r["net"], net):
                    mismatched.append({"row": r["row"], "pin": _pin(pin), "expected": r["net"], "actual": net})
            if _connected(net):
                nets.setdefault(net, []).append(_pin(pin))
            elif r["net"] is None and (len(r["pins"]) > 1 or r["channel"]):
                nets.setdefault("", []).append(_pin(pin))
        if r["net"] is None and not r["channel"] and len(r["pins"]) == 1:
            continue                          # a bare pin only has to exist
        key = f"channel {r['channel']}" if r["channel"] else f"row {r['row']}"
        groups.setdefault(key, []).append({"row": r["row"], "nets": nets})
    for key, entries in groups.items():
        nets: dict[str, list[str]] = {}
        for e in entries:
            for net, names in e["nets"].items():
                nets.setdefault(net, []).extend(names)
        if len(nets) > 1 or "" in nets:
            split.append({"what": key, "rows": [e["row"] for e in entries],
                          "nets": {net: sorted(names) for net, names in sorted(nets.items())}})
    merged = []
    on_net: dict[str, set[str]] = {}
    for key, entries in groups.items():
        for e in entries:
            for net in e["nets"]:
                if net:
                    on_net.setdefault(net, set()).add(key)
    # rows that assert one net name are meant to share it; a channel or a pin group is not
    asserted = {f"row {r['row']}" for r in table["rows"] if r["net"] and not r["channel"]}
    for net, keys in sorted(on_net.items()):
        separate = keys - asserted
        if len(keys) > 1 and separate:
            merged.append({"net": net, "what": sorted(keys)})
    listed = {pin for r in table["rows"] for pin in r["pins"]}
    if complete is True:
        refs = {pin[0] for pin in listed}
    else:
        refs = set(complete or [])
    unknown = sorted(refs - known)
    extra = [{"pin": _pin(pin), "net": net} for pin, net in sorted(actual.items())
             if pin[0] in refs and pin not in listed and _connected(net)]
    missing += [{"row": None, "pin": ref, "problem": "part"} for ref in unknown
                if not any(m["pin"] == ref or m["pin"].startswith(ref + ".") for m in missing)]
    out = {"source": str(source), "mapping": table["file"], "rows": len(table["rows"]),
           "missing": missing, "mismatched": mismatched, "split": split, "merged": merged, "extra": extra}
    out["counts"] = {k: len(out[k]) for k in ("missing", "mismatched", "split", "merged", "extra")}
    out["ok"] = not any(out["counts"].values())
    if table["ignored"]:
        out["ignored"] = table["ignored"]
    return out


def write(mapping: Path | str, assignment: dict[str, str]) -> dict:
    """Write ``assignment`` {"J1.3": "CH1", ...} as a ``ref,pin,net`` CSV, sorted, with an
    empty net for an unconnected pin. Returns file and pins."""
    from .syncer import natural_key
    path = Path(mapping)
    rows = sorted((_split_pin(p, "assignment") + (n or "",) for p, n in assignment.items()),
                  key=lambda r: (natural_key(r[0]), natural_key(r[1])))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(("ref", "pin", "net"))
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out.getvalue(), encoding="utf-8")
    return {"file": str(path), "pins": len(rows)}
