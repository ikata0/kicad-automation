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
import subprocess
import sys
from pathlib import Path

from . import env, netlist, report

__all__ = ["back_annotate", "check", "pin_nets", "read", "update", "write"]

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
    """(ref, pin) -> net name of a schematic, board, netlist or ODB++ job. A pin on no net is
    absent or has an ``unconnected-(`` name."""
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
    elif source.is_dir() or source.suffix.lower() in (".zip", ".tgz", ".tar", ".gz"):
        from .pcb import odb
        return {(ref, pin): net or f"{_UNCONNECTED}{ref}-Pad{pin})"
                for ref, c in odb.pinout(source)["components"].items() for pin, net in c["pins"].items()}
    else:
        raise env.KatError(f"{source.name}: give a .kicad_sch, .kicad_pcb, netlist (.net) or ODB++ job")
    return {pin: name for name, pins in nets.items() for pin in pins}


def _pin(pin: tuple[str, str]) -> str:
    return f"{pin[0]}.{pin[1]}"


def check(source: Path | str, mapping: Path | str, complete: bool | list[str] = True,
          pins: list[str] | None = None) -> dict:
    """Compare ``source`` (.kicad_sch, .kicad_pcb, a netlist or an ODB++ job) with a mapping
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


def pin_nets(mapping: Path | str, ref: str | None = None) -> dict[str, str]:
    """A ``pin,net`` or ``ref,pin,net`` table as a dictionary: {"J1.3": "CH1"}, or with ``ref``
    only that part's {"3": "CH1"}. An unconnected pin has the net "". This is what a SKiDL script
    reads (``kat.skidl_blocks.pin_map``)."""
    out: dict[str, str] = {}
    table = read(mapping)
    for r in table["rows"]:
        if r["net"] is None or len(r["pins"]) != 1:
            raise env.KatError(f"{Path(mapping).name} row {r['row']}: expected one pin and a net column")
        (part, number), = r["pins"]
        net = "" if r["net"].lower() in NOT_CONNECTED else r["net"]
        if ref is None:
            out[f"{part}.{number}"] = net
        elif part == ref:
            out[number] = net
    return out


def _update_csv(path: Path, assignment: dict[str, str]) -> None:
    """Change the net cells of a CSV table, leaving comments, other columns and order alone."""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    data = [i for i, l in enumerate(lines) if l.strip() and not l.lstrip().startswith("#")]
    try:
        delimiter = csv.Sniffer().sniff(lines[data[0]], delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","

    def row_of(line: str) -> list[str]:
        return next(csv.reader([line], delimiter=delimiter))

    def line_of(cells: list[str]) -> str:
        out = io.StringIO()
        csv.writer(out, delimiter=delimiter, lineterminator="").writerow(cells)
        return out.getvalue()

    header = [c.strip().lower() for c in row_of(lines[data[0]])]
    if "net" not in header or not ("pin" in header):
        raise env.KatError(f"{path.name}: back-annotation needs the columns pin (or ref and pin) and net")
    k_net, k_pin = header.index("net"), header.index("pin")
    k_ref = header.index("ref") if "ref" in header else None
    left = dict(assignment)
    for i in data[1:]:
        cells = row_of(lines[i])
        cells += [""] * (len(header) - len(cells))
        pin = cells[k_pin].strip() if k_ref is None else f"{cells[k_ref].strip()}.{cells[k_pin].strip()}"
        if pin in left:
            cells[k_net] = left.pop(pin)
            lines[i] = line_of(cells)
    for pin, net in left.items():
        cells = [""] * len(header)
        part, number = _split_pin(pin, "assignment")
        cells[k_net] = net
        if k_ref is None:
            cells[k_pin] = pin
        else:
            cells[k_ref], cells[k_pin] = part, number
        lines.append(line_of(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_json(path: Path, assignment: dict[str, str]) -> None:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = data if isinstance(data, list) else data.get("rows") if isinstance(data, dict) else None
    if isinstance(rows, list):
        left = dict(assignment)
        for r in rows:
            pin = f"{r['ref']}.{r['pin']}" if r.get("ref") else str(r.get("pin", ""))
            if pin in left:
                r["net"] = left.pop(pin)
        for pin, net in left.items():
            part, number = _split_pin(pin, "assignment")
            rows.append({"ref": part, "pin": number, "net": net} if any("ref" in r for r in rows)
                        else {"pin": pin, "net": net})
    else:
        data.update(assignment)
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def update(mapping: Path | str, assignment: dict[str, str]) -> list[dict]:
    """Set nets in a table: {"J1.3": "CH1"}, "" for unconnected. Comments, other columns and the
    order stay; pins it lacks are added at the end. Returns the changes [{pin, from, to}]."""
    path = Path(mapping)
    current = pin_nets(path)
    changes = [{"pin": pin, "from": current.get(pin), "to": net or ""} for pin, net in assignment.items()
               if current.get(pin) != (net or "")]
    if changes:
        wanted = {c["pin"]: c["to"] for c in changes}
        (_update_json if path.suffix.lower() == ".json" else _update_csv)(path, wanted)
    return changes


def back_annotate(mapping: Path | str, assignment: dict[str, str] | Path | str, script: Path | str,
                  sch: Path | str, netlist_file: Path | str | None = None, pcb: Path | str | None = None,
                  args: list[str] | None = None, dry_run: bool = False, force: bool = False,
                  rip_stale: bool = False, require: tuple[str, ...] | list[str] = ("MPN",)) -> dict:
    """Apply a computed pin assignment through the design, which owns connectivity. The SKiDL
    script reads the table (``kat.skidl_blocks.connect_pins``) and is never edited.

    Sets ``assignment`` ({"J1.3": "CH1"}, or a table of them) in ``mapping``, runs ``script`` in
    its folder (with ``args``), syncs ``sch`` from ``netlist_file`` (default the script's name with
    .net) and ``pcb`` if given, keeping placement. ``force``, ``rip_stale``, ``require``: as for
    ``kat.pcb.sync``. ``dry_run``: only report the changes.

    Then it verifies: the netlist has the table's assignment, the sheet matches the netlist and has
    it too, and the board has parity and has it too. If any step fails, the table, netlist,
    sheet and board are put back and KatError says why. Returns ok, changes, moved (nets that left a
    part: a warning, a pure swap has none), sync, board (with stale_tracks and unconnected) and check."""
    import kat

    from .gui import guard
    mapping, script, sch = Path(mapping), Path(script), Path(sch)
    report.touch(sch)
    net_file = Path(netlist_file) if netlist_file else script.with_suffix(".net")
    if not net_file.is_absolute() and not net_file.exists():
        net_file = script.parent / net_file
    wanted = pin_nets(assignment) if isinstance(assignment, (str, Path)) else {k: v or "" for k, v in assignment.items()}
    if not wanted:
        raise env.KatError("the assignment is empty")
    for pin in wanted:
        _split_pin(pin, "assignment")
    if not script.is_file():
        raise env.KatError(f"{script} does not exist")
    current = pin_nets(mapping)
    changes = [{"pin": p, "from": current.get(p), "to": n} for p, n in wanted.items() if current.get(p) != n]
    after = {**current, **wanted}
    moved = []
    for part in sorted({c["pin"].split(".", 1)[0] for c in changes}):
        had = sorted(n for p, n in current.items() if p.startswith(part + ".") and n)
        has = sorted(n for p, n in after.items() if p.startswith(part + ".") and n)
        if had != has:
            moved.append({"ref": part, "left": sorted(set(had) - set(has)), "came": sorted(set(has) - set(had))})
    out: dict = {"table": str(mapping), "changes": changes, "moved": moved, "dry_run": dry_run}
    if dry_run or not changes:
        return {**out, "ok": True, "changed": False}
    guard(sch)
    if pcb is not None:
        guard(Path(pcb))
    from .netlist import blocks_file
    from .syncer_hier import settings_file
    files = [mapping, net_file, blocks_file(net_file), sch, settings_file(sch)]
    files += [Path(pcb), Path(pcb).with_suffix(".stamps.json")] if pcb is not None else []
    saved = {f: f.read_bytes() if f.exists() else None for f in files}

    def put_back(why: str) -> env.KatError:
        for f, data in saved.items():
            if data is None:
                f.unlink(missing_ok=True)
            else:
                f.write_bytes(data)
        return env.KatError(f"back-annotation put back: {why}")

    try:
        update(mapping, wanted)
        proc = subprocess.run([sys.executable, script.name, *(args or [])], cwd=script.parent, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise put_back(f"{script.name} failed: {(proc.stderr or proc.stdout).strip()[-1500:]}")
        if not net_file.exists():
            raise put_back(f"{script.name} wrote no {net_file.name}")
        got = check(net_file, mapping, complete=False)
        if not got["ok"]:
            raise put_back(f"{net_file.name} does not have the table's assignment: does {script.name} read it? "
                           f"{ {k: got[k] for k in ('missing', 'mismatched')} }")
        sync = kat.sync(sch, net_file)
        if not sync.get("check", {}).get("ok", False):
            raise put_back(f"the sheet does not match after sync: {sync.get('check')}")
        on_sheet = check(sch, mapping, complete=False)
        if not on_sheet["ok"]:
            raise put_back(f"the sheet does not have the table's assignment: {on_sheet['mismatched']}")
        out["sync"] = {k: sync.get(k) for k in ("added", "removed", "updated", "detached", "attached", "renamed")
                       if sync.get(k)}
        out["check"] = {"netlist": True, "sheet": True}
        if pcb is not None:
            board = kat.pcb.sync(pcb, force=force, rip_stale=rip_stale, require=require)
            if board.get("ok") is False:
                raise put_back(board.get("error") or f"the board does not match: {board.get('check')}")
            on_board = check(pcb, mapping, complete=False)
            if not on_board["ok"]:
                raise put_back(f"the board does not have the table's assignment: {on_board['mismatched']}")
            out["board"] = {"parity": board["check"]["parity"], "unconnected": board["check"]["unconnected"],
                            "stale_tracks": board.get("stale_tracks", [])}
            out["check"]["board"] = True
    except env.KatError as e:
        if str(e).startswith("back-annotation put back"):
            raise
        raise put_back(str(e)) from e
    return {**out, "ok": True, "changed": True}
