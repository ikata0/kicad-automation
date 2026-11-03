"""Generate a symbol and a footprint from a pin table.

Connectors and pin grids come from datasheets and mechanical drawings as tables. One row per pad
(CSV or JSON, case-insensitive columns):

    pin, name, x, y         the pad number, its name on the symbol, its position in mm
    row, col                instead of x, y: a grid position (row may be a letter), times ``pitch``
    drill                   mm, or WxH for a slot; empty: a surface pad, unless ``drill`` is given
    pad                     mm, or WxH; or pad_w and pad_h; default: drill plus 0.5 mm
    shape                   circle, rect, oval, roundrect
    type                    electrical type of the symbol pin (default passive)
    side                    left, right, top, bottom: where the symbol pin goes
    kind                    tht, smd, npth (a row with a drill but no pin is a plain hole)

KiCad reads what is written back through ``sym upgrade`` and ``fp upgrade`` before it is kept.
"""

from __future__ import annotations

import math
import shutil
import tempfile
from pathlib import Path

from . import env, report
from .mapping import _table
from .schematic import new_uuid, text_width
from .sexpr import Sym, dumps, find, find_all, head, loads, num

__all__ = ["footprint_node", "make", "pads_from_table", "symbol_node"]

ETYPES = ("input", "output", "bidirectional", "tri_state", "passive", "free", "unspecified", "power_in",
          "power_out", "open_collector", "open_emitter", "no_connect")
SHAPES = ("circle", "rect", "oval", "roundrect")
KINDS = ("tht", "smd", "npth")
SIDES = ("left", "right", "top", "bottom")
_FONT = [Sym("font"), [Sym("size"), num(1.27), num(1.27)]]
_FP_FONT = [Sym("font"), [Sym("size"), num(1), num(1)], [Sym("thickness"), num(0.15)]]
_SYM_VERSION, _FP_VERSION = 20251024, 20260206
_GRID = 2.54


def _size(text: str, where: str, what: str) -> tuple[float, float]:
    """"1.5" or "2x1.5" in mm."""
    try:
        parts = [float(v) for v in text.lower().replace(" ", "").split("x")]
        if len(parts) == 1:
            parts *= 2
        if len(parts) != 2 or min(parts) <= 0:
            raise ValueError
    except ValueError:
        raise env.KatError(f"{where}: {what} {text!r}: give mm, or WxH") from None
    return parts[0], parts[1]


def _number(text: str, where: str, what: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise env.KatError(f"{where}: {what} {text!r} is not a number") from None


def _grid_row(text: str, where: str) -> float:
    if text.strip().isalpha():
        k = 0
        for c in text.strip().upper():
            k = k * 26 + ord(c) - 64
        return k
    return _number(text, where, "row")


def _choice(text: str, options: tuple, where: str, what: str) -> str:
    v = text.strip().lower()
    if v not in options:
        raise env.KatError(f"{where}: {what} {text!r}: one of {', '.join(options)}")
    return v


def pads_from_table(pins: Path | str, drill: float | None = None, pad: tuple[float, float] | None = None,
                    shape: str | None = None, pitch: tuple[float, float] | None = None) -> list[dict]:
    """The rows of a pin table as pads: number, name, x, y (mm, as the table says, None if it
    gives none), drill (w, h mm or None), size (w, h mm or None), shape, kind (tht, smd, npth),
    etype, side. ``drill``, ``pad`` and ``shape`` are defaults for rows that leave them empty;
    ``pitch`` (dx, dy mm) turns ``row`` and ``col`` into positions."""
    path = Path(pins)
    out = []
    for k, r in enumerate(_table(path), start=1):
        where = f"{path.name} row {k}"
        number = (r.get("pin") or r.get("number") or "").strip()
        x, y = None, None
        if r.get("x") or r.get("y"):
            if not (r.get("x") and r.get("y")):
                raise env.KatError(f"{where}: x and y are both needed")
            x, y = _number(r["x"], where, "x"), _number(r["y"], where, "y")
        elif r.get("row") or r.get("col"):
            if not (r.get("row") and r.get("col")):
                raise env.KatError(f"{where}: row and col are both needed")
            if not pitch:
                raise env.KatError(f"{where}: row and col need a pitch")
            x = (_number(r["col"], where, "col") - 1) * pitch[0]
            y = (_grid_row(r["row"], where) - 1) * pitch[1]
        hole = _size(r["drill"], where, "drill") if r.get("drill") else ((drill, drill) if drill else None)
        kind = _choice(r["kind"], KINDS, where, "kind") if r.get("kind") else None
        if kind == "smd":
            hole = None
        if kind in ("tht", "npth") and hole is None:
            raise env.KatError(f"{where}: kind {kind} needs a drill")
        if kind is None:
            kind = "smd" if hole is None else ("tht" if number else "npth")
        if not number and kind != "npth":
            raise env.KatError(f"{where}: no pin number (a row without one is a hole: give a drill)")
        if number and kind == "npth":
            raise env.KatError(f"{where}: a non-plated hole has no pin number")
        if r.get("pad"):
            size = _size(r["pad"], where, "pad")
        elif r.get("pad_w") or r.get("pad_h"):
            if not (r.get("pad_w") and r.get("pad_h")):
                raise env.KatError(f"{where}: pad_w and pad_h are both needed")
            size = (_number(r["pad_w"], where, "pad_w"), _number(r["pad_h"], where, "pad_h"))
        else:
            size = pad
        out.append({"row": k, "number": number, "name": (r.get("name") or number).strip(), "x": x, "y": y,
                    "drill": hole, "size": size,
                    "shape": _choice(r["shape"], SHAPES, where, "shape") if r.get("shape") else shape,
                    "kind": kind,
                    "etype": _choice(r["type"], ETYPES, where, "type") if r.get("type") else "passive",
                    "side": _choice(r["side"], SIDES, where, "side") if r.get("side") else None})
    if not out:
        raise env.KatError(f"{path.name} has no rows")
    return out


# --- symbol -----------------------------------------------------------------------------------

def _effects(hide: bool = False) -> list:
    return [Sym("effects"), _FONT, *([[Sym("hide"), Sym("yes")]] if hide else [])]


def _sym_property(name: str, value: str, x: float, y: float, hide: bool = False) -> list:
    return [Sym("property"), name, value, [Sym("at"), num(x), num(y), num(0)], _effects(hide)]


def _symbol_pins(pads: list[dict]) -> list[dict]:
    """One symbol pin per pad number, in table order. A number on several pads is one pin."""
    pins: dict[str, dict] = {}
    for p in pads:
        if not p["number"]:
            continue
        old = pins.setdefault(p["number"], p)
        if old["name"] != p["name"]:
            raise env.KatError(f"pin {p['number']} is named {old['name']!r} and {p['name']!r}")
    return list(pins.values())


def symbol_node(name: str, pads: list[dict], footprint: str, reference: str) -> list:
    """A library symbol with one body: pins go where ``side`` says, others half left, half right."""
    pins = _symbol_pins(pads)
    if not pins:
        raise env.KatError("no pins to draw a symbol from")
    free = [p for p in pins if not p["side"]]
    half = math.ceil(len([p for p in pins if not p["side"]]) / 2)
    place = {s: [p for p in pins if p["side"] == s] for s in SIDES}
    place["left"] += free[:half]
    place["right"] += free[half:]
    rows = max(len(place["left"]), len(place["right"]), 1)
    cols = max(len(place["top"]), len(place["bottom"]))
    names = [p["name"] for p in pins if p["name"] != p["number"]]
    wide = max([text_width(n) for n in names] or [0])
    width = max(10.16, 2 * wide + 5.08, (cols + 1) * _GRID)
    half_w = math.ceil(width / 2 / _GRID) * _GRID
    top = math.ceil((rows - 1) / 2) * _GRID
    y0, y1 = top + _GRID, top - rows * _GRID
    length = 2.54
    body = [Sym("rectangle"), [Sym("start"), num(-half_w), num(y0)], [Sym("end"), num(half_w), num(y1)],
            [Sym("stroke"), [Sym("width"), num(0.254)], [Sym("type"), Sym("default")]],
            [Sym("fill"), [Sym("type"), Sym("background")]]]
    nodes = []

    def pin(p: dict, x: float, y: float, angle: int) -> None:
        nodes.append([Sym("pin"), Sym(p["etype"]), Sym("line"), [Sym("at"), num(x), num(y), num(angle)],
                      [Sym("length"), num(length)], [Sym("name"), p["name"], _effects()],
                      [Sym("number"), p["number"], _effects()]])

    for i, p in enumerate(place["left"]):
        pin(p, -half_w - length, top - i * _GRID, 0)
    for i, p in enumerate(place["right"]):
        pin(p, half_w + length, top - i * _GRID, 180)
    for s, y, angle in (("top", y0 + length, 270), ("bottom", y1 - length, 90)):
        n = len(place[s])
        left = -math.floor((n - 1) / 2) * _GRID
        for i, p in enumerate(place[s]):
            pin(p, left + i * _GRID, y, angle)
    above = y0 + (length if place["top"] else 0) + _GRID
    below = y1 - (length if place["bottom"] else 0) - _GRID
    hide_names = not names
    return [Sym("symbol"), name,
            [Sym("pin_names"), [Sym("offset"), num(1.016)], *([[Sym("hide"), Sym("yes")]] if hide_names else [])],
            [Sym("exclude_from_sim"), Sym("no")], [Sym("in_bom"), Sym("yes")], [Sym("on_board"), Sym("yes")],
            _sym_property("Reference", reference, 0, above), _sym_property("Value", name, 0, below),
            _sym_property("Footprint", footprint, 0, below - _GRID, hide=True),
            _sym_property("Datasheet", "", 0, 0, hide=True), _sym_property("Description", "", 0, 0, hide=True),
            [Sym("symbol"), f"{name}_0_1", body], [Sym("symbol"), f"{name}_1_1", *nodes]]


def _write_symbol(path: Path, node: list, force: bool) -> None:
    """Add the symbol to a library file (created if missing), through KiCad."""
    name = str(node[1])
    if path.exists():
        tree = loads(path.read_text(encoding="utf-8"))
        if head(tree) != "kicad_symbol_lib":
            raise env.KatError(f"{path.name} is not a symbol library")
        old = [c for c in find_all(tree, "symbol") if str(c[1]) == name]
        if old and not force:
            raise env.KatError(f"{path.name} already has {name}: force replaces it")
        for c in old:
            tree.remove(c)
    else:
        tree = [Sym("kicad_symbol_lib"), [Sym("version"), num(_SYM_VERSION)], [Sym("generator"), "kat"],
                [Sym("generator_version"), "10.0"]]
    tree.append(node)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / path.name
        tmp.write_text(dumps(tree) + "\n", encoding="utf-8")
        env.run_cli("sym", "upgrade", "--force", str(tmp))
        back = loads(tmp.read_text(encoding="utf-8"))
        if not any(str(c[1]) == name for c in find_all(back, "symbol")):
            raise env.KatError(f"KiCad did not keep symbol {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp, path)


# --- footprint --------------------------------------------------------------------------------

def _fp_text(field: str, value: str, x: float, y: float, layer: str, hide: bool = False) -> list:
    return [Sym("property"), field, value, [Sym("at"), num(x), num(y), num(0)], [Sym("layer"), layer],
            *([[Sym("hide"), Sym("yes")]] if hide else []), [Sym("uuid"), new_uuid()], [Sym("effects"), _FP_FONT]]


def _fp_rect(box, layer: str, width: float) -> list:
    return [Sym("fp_rect"), [Sym("start"), num(box[0]), num(box[1])], [Sym("end"), num(box[2]), num(box[3])],
            [Sym("stroke"), [Sym("width"), num(width)], [Sym("type"), Sym("default")]], [Sym("fill"), Sym("no")],
            [Sym("layer"), layer], [Sym("uuid"), new_uuid()]]


def _triangle(points, layer: str) -> list:
    return [Sym("fp_poly"), [Sym("pts"), *([Sym("xy"), num(x), num(y)] for x, y in points)],
            [Sym("stroke"), [Sym("width"), num(0.1)], [Sym("type"), Sym("default")]], [Sym("fill"), Sym("yes")],
            [Sym("layer"), layer], [Sym("uuid"), new_uuid()]]


def _pad_node(p: dict) -> list:
    kind = {"tht": "thru_hole", "smd": "smd", "npth": "np_thru_hole"}[p["kind"]]
    w, h = p["size"]
    node = [Sym("pad"), p["number"], Sym(kind), Sym(p["shape"]), [Sym("at"), num(p["x"]), num(p["y"])],
            [Sym("size"), num(w), num(h)]]
    if p["drill"]:
        dw, dh = p["drill"]
        node.append([Sym("drill"), Sym("oval"), num(dw), num(dh)] if abs(dw - dh) > 1e-9 else [Sym("drill"), num(dw)])
    if p["kind"] == "smd":
        node.append([Sym("layers"), "F.Cu", "F.Mask", "F.Paste"])
    else:
        node.append([Sym("layers"), "*.Cu", "*.Mask"])
    if p["shape"] == "roundrect":
        node.append([Sym("roundrect_rratio"), num(0.25)])
    node.append([Sym("uuid"), new_uuid()])
    return node


def _marker(pin: dict, box) -> list[tuple[float, float]]:
    """A triangle outside ``box`` pointing at the edge nearest to the pin."""
    x, y = pin["x"], pin["y"]
    gap, base, deep = 0.2, 0.5, 0.8
    edges = [(x - box[0], "left"), (box[2] - x, "right"), (y - box[1], "top"), (box[3] - y, "bottom")]
    side = min(edges, key=lambda e: e[0])[1]
    if side == "left":
        tip = box[0] - gap
        return [(tip, y), (tip - deep, y - base), (tip - deep, y + base)]
    if side == "right":
        tip = box[2] + gap
        return [(tip, y), (tip + deep, y - base), (tip + deep, y + base)]
    if side == "top":
        tip = box[1] - gap
        return [(x, tip), (x - base, tip - deep), (x + base, tip - deep)]
    tip = box[3] + gap
    return [(x, tip), (x - base, tip + deep), (x + base, tip + deep)]


def footprint_node(name: str, pads: list[dict], marker: str | None, reference: str) -> tuple[list, tuple]:
    """A footprint: the pads, an outline on silk and fab around them, the courtyard around all,
    and a triangle at the ``marker`` pin. Returns the node and the outline (x0, y0, x1, y1 mm)."""
    extent = []
    for p in pads:
        (w, h) = p["size"]
        extent.append((p["x"] - w / 2, p["y"] - h / 2, p["x"] + w / 2, p["y"] + h / 2))
    box = (min(e[0] for e in extent) - 0.3, min(e[1] for e in extent) - 0.3,
           max(e[2] for e in extent) + 0.3, max(e[3] for e in extent) + 0.3)
    points: list[tuple[float, float]] = []
    extra = []
    if marker is not None:
        pin = next(p for p in pads if p["number"] == marker)
        points = _marker(pin, box)
        extra = [_triangle(points, "F.SilkS"), _triangle(points, "F.Fab")]
    xs = [box[0], box[2], *(q[0] for q in points)]
    ys = [box[1], box[3], *(q[1] for q in points)]
    court = tuple(round(v, 2) for v in (min(xs) - 0.25, min(ys) - 0.25, max(xs) + 0.25, max(ys) + 0.25))
    through = any(p["kind"] == "tht" for p in pads)
    node = [Sym("footprint"), name, [Sym("version"), num(_FP_VERSION)], [Sym("generator"), "kat"],
            [Sym("generator_version"), "10.0"], [Sym("layer"), "F.Cu"],
            _fp_text("Reference", reference + "**", 0, court[1] - 1, "F.SilkS"),
            _fp_text("Value", name, 0, court[3] + 1, "F.Fab"),
            _fp_text("Datasheet", "", 0, 0, "F.Fab", hide=True), _fp_text("Description", "", 0, 0, "F.Fab", hide=True),
            [Sym("attr"), Sym("through_hole" if through else "smd")],
            _fp_rect(box, "F.SilkS", 0.12), _fp_rect(box, "F.Fab", 0.1), _fp_rect(court, "F.CrtYd", 0.05),
            *extra, *(_pad_node(p) for p in pads)]
    return node, box


def _write_footprint(pretty: Path, name: str, node: list, force: bool) -> Path:
    path = pretty / f"{name}.kicad_mod"
    if path.exists() and not force:
        raise env.KatError(f"{path} exists: force replaces it")
    with tempfile.TemporaryDirectory() as d:
        lib = Path(d) / pretty.name
        lib.mkdir()
        tmp = lib / path.name
        tmp.write_text(dumps(node) + "\n", encoding="utf-8")
        env.run_cli("fp", "upgrade", "--force", str(lib))
        if head(loads(tmp.read_text(encoding="utf-8"))) != "footprint":
            raise env.KatError(f"KiCad did not keep footprint {name}")
        pretty.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp, path)
    return path


def _register(project: Path, table: str, root: str, lib: str, file: str) -> bool:
    """Add a library to a project's table, leaving what is there. Returns True if it was added."""
    path = project / table
    tree = loads(path.read_text(encoding="utf-8")) if path.exists() else [Sym(root), [Sym("version"), num(7)]]
    if any(head(c) == "lib" and str(find(c, "name")[1]) == lib for c in tree):
        return False
    tree.append([Sym("lib"), [Sym("name"), lib], [Sym("type"), "KiCad"], [Sym("uri"), "${KIPRJMOD}/" + file],
                 [Sym("options"), ""], [Sym("descr"), "generated by kat lib make"]])
    path.write_text(dumps(tree) + "\n", encoding="utf-8")
    return True


def make(pins: Path | str, name: str, lib: str | None = None, out: Path | str | None = None,
         symbol: bool = True, footprint: bool = True, view: str = "component", y_up: bool = False,
         origin: str = "table", drill: float | None = None, pad: tuple[float, float] | None = None,
         shape: str | None = None, pitch: tuple[float, float] | None = None, marker: str | None = "auto",
         reference: str = "J", force: bool = False) -> dict:
    """Generate a symbol and a footprint from a pin table (see this module; CSV or JSON), so the
    pin assignment has one source. ``name``: the part; ``lib``: library name (default the part's);
    ``out``: folder for ``LIB.kicad_sym`` and ``LIB.pretty/`` (default here), and if it holds a
    .kicad_pro both are added to its library tables.

    ``view``: the table's positions are seen from the ``component`` side or the ``pin`` side, which
    mirrors x. ``y_up``: y grows upwards in the table (default downwards, as in KiCad). ``origin``:
    the footprint's origin is the table's, ``pin1`` (the marker pin) or the ``center``. ``drill``
    (mm), ``pad`` (w, h mm), ``shape``: defaults for rows that leave them empty; ``pitch`` (dx, dy
    mm) for ``row`` and ``col``. ``marker``: pin marked by a triangle and a rectangular pad, ``auto``
    for pin 1 or else the first one, None for no marker. ``force``: replace a part of that name.

    Returns lib_id, symbol and footprint (files, or None if not asked), pins, pads, holes, outline
    (x0, y0, x1, y1 mm around the pads) and registered."""
    if view not in ("component", "pin"):
        raise env.KatError("view is component or pin")
    if origin not in ("table", "pin1", "center"):
        raise env.KatError("origin is table, pin1 or center")
    if not (symbol or footprint):
        raise env.KatError("nothing to make: symbol or footprint")
    lib = lib or name
    out = Path(out) if out is not None else Path.cwd()
    report.touch(out / f"{lib}.kicad_sym")
    pads = pads_from_table(pins, drill, pad, shape, pitch)
    numbers = [p["number"] for p in pads if p["number"]]
    chosen = None
    if marker == "auto":
        chosen = "1" if "1" in numbers else (numbers[0] if numbers else None)
    elif marker is not None:
        if marker not in numbers:
            raise env.KatError(f"marker {marker!r} is no pin of the table")
        chosen = marker
    bbox = None
    result: dict = {"lib_id": f"{lib}:{name}", "symbol": None, "footprint": None,
                    "pins": len(set(numbers)), "pads": len(numbers), "holes": len(pads) - len(numbers)}
    if footprint:
        for p in pads:
            where = f"row {p['row']}"
            if p["x"] is None:
                raise env.KatError(f"{where}: a footprint needs x and y, or row and col")
            if p["size"] is None:
                if p["drill"] is None:
                    raise env.KatError(f"{where}: a surface pad needs a pad size")
                extra = 0.0 if p["kind"] == "npth" else 0.5
                p["size"] = (p["drill"][0] + extra, p["drill"][1] + extra)
            if p["drill"] and (p["size"][0] < p["drill"][0] or p["size"][1] < p["drill"][1]):
                raise env.KatError(f"{where}: pad {p['size']} is smaller than its drill {p['drill']}")
            if p["shape"] is None:
                slot = p["size"][0] != p["size"][1]
                if p["kind"] == "smd":
                    p["shape"] = "roundrect"
                else:
                    p["shape"] = "oval" if slot else "circle"
                if p["number"] == chosen:
                    p["shape"] = "rect"
            if view == "pin":
                p["x"] = -p["x"]
            if y_up:
                p["y"] = -p["y"]
        at = {(round(p["x"], 6), round(p["y"], 6)): p for p in pads}
        if len(at) != len(pads):
            raise env.KatError("two rows of the table are on one position")
        if origin != "table":
            if origin == "pin1":
                if chosen is None:
                    raise env.KatError("origin pin1 needs a marker pin")
                ref = next(p for p in pads if p["number"] == chosen)
                ox, oy = ref["x"], ref["y"]
            else:
                ox = (min(p["x"] for p in pads) + max(p["x"] for p in pads)) / 2
                oy = (min(p["y"] for p in pads) + max(p["y"] for p in pads)) / 2
            for p in pads:
                p["x"], p["y"] = round(p["x"] - ox, 6), round(p["y"] - oy, 6)
        node, bbox = footprint_node(name, pads, chosen, reference)
        path = _write_footprint(out / f"{lib}.pretty", name, node, force)
        result["footprint"] = str(path)
        result["outline"] = [round(v, 4) for v in bbox]
    if symbol:
        fp = f"{lib}:{name}" if footprint else ""
        path = out / f"{lib}.kicad_sym"
        _write_symbol(path, symbol_node(name, pads, fp, reference), force)
        result["symbol"] = str(path)
    registered = []
    if any(out.glob("*.kicad_pro")):
        if symbol and _register(out, "sym-lib-table", "sym_lib_table", lib, f"{lib}.kicad_sym"):
            registered.append("sym-lib-table")
        if footprint and _register(out, "fp-lib-table", "fp_lib_table", lib, f"{lib}.pretty"):
            registered.append("fp-lib-table")
    result["registered"] = registered
    return result

