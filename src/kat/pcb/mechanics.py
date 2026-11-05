"""Board mechanics from fabrication data: outlines and holes from ODB++, Gerber and Excellon.

Readers return mm with y down, as KiCad's exports write -y, so Gerber, Excellon and ODB++ of one
board read back at its own coordinates. A hole is ``{x, y, d, length, angle, plating, kind}``:
``d`` the drill (mm), ``length`` the slot's overall length or None, ``angle`` the slot's angle
(degrees, counter-clockwise, as KiCad shows it), ``plating`` True, False or None when the file
does not say, ``kind`` "via" or "hole".
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .. import env
from ..schematic import new_uuid
from ..sexpr import Sym, num
from .geometry import chain
from .odb import Job, units_of

MECH_LIB = "kat_mech_hole"             # the lib id of imported holes, so an import can replace them
_INCH = 25.4
_WORD = re.compile(r"([A-Z])([+-]?\d*\.?\d*)")


def _r(v: float) -> float:
    return round(v, 6)


def _arc(s, e, c, clockwise: bool):
    """(a, mid, b) in y-down coordinates of an arc given in y-up coordinates. A full turn
    (start = end) is ("circle", centre, radius)."""
    if math.hypot(s[0] - e[0], s[1] - e[1]) < 1e-6:
        return ("circle", (_r(c[0]), _r(-c[1])), _r(math.hypot(s[0] - c[0], s[1] - c[1])))
    a0 = math.atan2(s[1] - c[1], s[0] - c[0])
    a1 = math.atan2(e[1] - c[1], e[0] - c[0])
    sweep = -((a0 - a1) % (2 * math.pi)) if clockwise else (a1 - a0) % (2 * math.pi)
    r = (math.hypot(s[0] - c[0], s[1] - c[1]) + math.hypot(e[0] - c[0], e[1] - c[1])) / 2
    mid = (c[0] + r * math.cos(a0 + sweep / 2), c[1] + r * math.sin(a0 + sweep / 2))
    return ("arc", (_r(s[0]), _r(-s[1])), (_r(mid[0]), _r(-mid[1])), (_r(e[0]), _r(-e[1])))


def _line(a, b):
    return ("line", (_r(a[0]), _r(-a[1])), (_r(b[0]), _r(-b[1])))


# --- Gerber ---------------------------------------------------------------------------------

class _Number:
    """A coordinate format: integer and decimal digits, and which zeros are left out."""

    def __init__(self, integer: int, decimals: int, trailing_omitted: bool = False):
        self.integer, self.decimals, self.trailing_omitted = integer, decimals, trailing_omitted

    def value(self, text: str) -> float:
        if "." in text:
            return float(text)
        sign = -1.0 if text.startswith("-") else 1.0
        digits = text.lstrip("+-")
        if self.trailing_omitted:
            digits = digits.ljust(self.integer + self.decimals, "0")
        return sign * int(digits or "0") / 10 ** self.decimals


def read_gerber(path: Path | str, tolerance: float = 0.01) -> dict:
    """The outline in a Gerber file (RS-274X): strokes and regions chained into closed loops,
    their ends joined within ``tolerance`` mm; a stroke's centre line is the edge. Returns loops
    (lists of segments), open (chains that do not close), widths (the apertures used, mm) and
    warnings."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="backslashreplace")
    fmt = None
    unit = None
    apertures: dict[int, float] = {}
    used: set[float] = set()
    segments = []
    warnings = []
    x, y = 0.0, 0.0
    mode, multi, region, aperture = 1, None, False, None
    flashes = 0

    def scale(v: float) -> float:
        if unit is None:
            raise env.KatError(f"{path.name}: coordinates before the units (%MOMM*% or %MOIN*%)")
        return v * unit

    for block in re.finditer(r"%([^%]*)%|([^%*]+)\*", text):
        if block.group(1) is not None:
            for ext in filter(None, (t.strip() for t in block.group(1).split("*"))):
                if ext.startswith("FS"):
                    m = re.fullmatch(r"FS([LTD]?)([AI])(?:N\d)?(?:G\d)?X(\d)(\d)Y(\d)(\d)(?:D\d)?(?:M\d)?", ext)
                    if not m:
                        raise env.KatError(f"{path.name}: cannot read the format {ext!r}")
                    if m.group(2) == "I":
                        raise env.KatError(f"{path.name}: incremental coordinates are not supported")
                    fmt = _Number(int(m.group(3)), int(m.group(4)), m.group(1) == "T")
                elif ext.startswith("MO"):
                    unit = {"MOMM": 1.0, "MOIN": _INCH}.get(ext[:4])
                    if unit is None:
                        raise env.KatError(f"{path.name}: unknown units {ext!r}")
                elif ext.startswith("AD") and (m := re.match(r"ADD(\d+)([A-Za-z_$.][\w.$-]*),?([^*]*)", ext)):
                    size = re.match(r"[+-]?\d*\.?\d+", m.group(3))
                    apertures[int(m.group(1))] = float(size.group()) if size else 0.0
                elif ext.startswith("SR") and ext not in ("SRX1Y1I0J0", "SR"):
                    raise env.KatError(f"{path.name}: step and repeat is not supported")
            continue
        stmt = block.group(2).strip()
        if not stmt or stmt.startswith("G04") or stmt.startswith("G4 "):
            continue
        if stmt.startswith("G70"):
            unit = _INCH
            continue
        if stmt.startswith("G71"):
            unit = 1.0
            continue
        if stmt.startswith("G91"):
            raise env.KatError(f"{path.name}: incremental coordinates are not supported")
        words = _WORD.findall(stmt)
        coords: dict[str, float] = {}
        d = None
        for k, v in words:
            if k == "G":
                g = int(v or 0)
                if g in (1, 2, 3):
                    mode = g
                elif g == 36:
                    region = True
                elif g == 37:
                    region = False
                elif g in (74, 75):
                    multi = g == 75
            elif k == "D":
                d = int(v or 0)
            elif k == "M" and int(v or 0) in (0, 2):
                break
            elif k in "XYIJ":
                if fmt is None:
                    raise env.KatError(f"{path.name}: coordinates before the format (%FS...*%)")
                coords[k] = scale(fmt.value(v))
        if d is not None and d >= 10:
            aperture = d
            continue
        if not coords and d is None:
            continue
        nx, ny = coords.get("X", x), coords.get("Y", y)
        if d == 3:
            flashes += 1
        elif d == 1 or (d is None and coords):
            if not region and aperture in apertures:
                used.add(round(scale(apertures[aperture]), 6))
            if mode == 1:
                if (nx, ny) != (x, y):
                    segments.append(_line((x, y), (nx, ny)))
            else:
                i, j = coords.get("I", 0.0), coords.get("J", 0.0)
                if multi:
                    c = (x + i, y + j)
                else:                         # single quadrant: offsets without sign, 90 degrees at most
                    c = min(((x + si * i, y + sj * j) for si in (1, -1) for sj in (1, -1)),
                            key=lambda q: (_quarter_error((x, y), (nx, ny), q, mode == 2)))
                segments.append(_arc((x, y), (nx, ny), c, mode == 2))
        x, y = nx, ny
    if unit is None or fmt is None:
        raise env.KatError(f"{path.name} is not a Gerber file with units and a format")
    if flashes:
        warnings.append(f"{flashes} flashed apertures are not part of an outline: left out")
    loops, opened = chain(segments, tolerance)
    return {"file": str(path), "loops": loops, "open": opened, "widths": sorted(used), "warnings": warnings}


def _quarter_error(s, e, c, clockwise: bool) -> tuple[float, float]:
    """How badly ``c`` fits as the centre of a single-quadrant arc: sweep beyond 90 degrees,
    then the difference of the radii."""
    a0 = math.atan2(s[1] - c[1], s[0] - c[0])
    a1 = math.atan2(e[1] - c[1], e[0] - c[0])
    sweep = (a0 - a1) % (2 * math.pi) if clockwise else (a1 - a0) % (2 * math.pi)
    return (max(0.0, sweep - math.pi / 2 - 1e-6),
            abs(math.hypot(s[0] - c[0], s[1] - c[1]) - math.hypot(e[0] - c[0], e[1] - c[1])))


# --- Excellon -------------------------------------------------------------------------------

def _slot(a, b, d: float) -> dict:
    """A slot milled from a to b (y up) with a tool of diameter d."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    return {"x": _r((a[0] + b[0]) / 2), "y": _r(-(a[1] + b[1]) / 2), "d": _r(d),
            "length": _r(math.hypot(dx, dy) + d), "angle": round(math.degrees(math.atan2(dy, dx)) % 180, 4)}


def _plating_word(text: str) -> bool | None:
    t = text.upper().replace("-", "_")
    if "NONPLATED" in t or "NON_PLATED" in t or "NPTH" in t:
        return False
    if "PLATED" in t or "PTH" in t:
        return True
    return None


def read_excellon(path: Path | str) -> dict:
    """The holes and slots of an Excellon drill file: drilled hits, ``G85`` slots and slots
    routed between ``M15`` and ``M16``. Plating comes per tool or per file from comments
    (``TF.FileFunction``, ``TA.AperFunction``, ``;TYPE=``), else from NPTH or PTH in the file
    name. Returns holes and warnings. Routed arcs are left out, with a warning."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="backslashreplace")
    file_plating = _plating_word(path.stem)
    tool_notes: dict[str, tuple[bool | None, str]] = {}
    tools: dict[str, float] = {}
    unit = None
    integer, decimals, trailing_omitted = None, None, None
    note: tuple[bool | None, str] = (None, "hole")
    holes, warnings = [], []
    arcs = 0
    tool = None
    x, y = 0.0, 0.0
    routing = down = False
    seen_header = False
    unsure = False

    def said(k: str) -> tuple[bool | None, str]:
        word, kind = tool_notes.get(k, (None, "hole"))
        return (file_plating if word is None else word), kind

    def number(v: str) -> float:
        nonlocal unsure
        if "." in v:
            return float(v) * unit
        unsure = unsure or trailing_omitted is None
        i = integer if integer is not None else (3 if unit == 1.0 else 2)
        k = decimals if decimals is not None else (3 if unit == 1.0 else 4)
        # LZ keeps leading zeros (trailing may be left out), TZ keeps trailing zeros
        return _Number(i, k, trailing_omitted is True).value(v) * unit

    def hit(px, py) -> None:
        if tool is None or tool not in tools:
            raise env.KatError(f"{path.name}: a hole with no tool (T{tool})")
        word, kind = said(tool)
        holes.append({"x": _r(px), "y": _r(-py), "d": _r(tools[tool]), "length": None, "angle": 0.0,
                      "plating": word, "kind": kind})

    def slot(a, b) -> None:
        if tool is None or tool not in tools:
            raise env.KatError(f"{path.name}: a slot with no tool (T{tool})")
        word, kind = said(tool)
        holes.append({**_slot(a, b, tools[tool]), "plating": word, "kind": kind})

    def coords(stmt: str) -> tuple[float, float]:
        got = dict(re.findall(r"([XY])([+-]?\d*\.?\d*)", stmt))
        return (number(got["X"]) if got.get("X") else x, number(got["Y"]) if got.get("Y") else y)

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            upper = line.upper()
            if "TF.FILEFUNCTION" in upper:
                word = _plating_word(line.split(",", 1)[1]) if "," in line and "MIXED" not in upper else None
                if word is not None:
                    file_plating = word
            elif "TA.APERFUNCTION" in upper:
                note = (_plating_word(line.split(",", 1)[1]), "via" if "VIADRILL" in upper else "hole")
            elif upper.lstrip("; ").startswith("TYPE="):
                file_plating = _plating_word(upper)
            elif m := re.search(r"(?:FILE_FORMAT=|FORMAT=\{)\s*(\d):(\d)", line):
                integer, decimals = int(m.group(1)), int(m.group(2))
            continue
        line = line.split(";")[0].strip()
        upper = line.upper()
        if upper == "M48":
            seen_header = True
            continue
        if re.match(r"(METRIC|INCH)\b", upper):
            unit = 1.0 if upper.startswith("METRIC") else _INCH
            if ",TZ" in upper:
                trailing_omitted = False
            elif ",LZ" in upper:
                trailing_omitted = True
            if m := re.search(r",(0+)\.(0+)", upper):
                integer, decimals = len(m.group(1)), len(m.group(2))
            continue
        if upper in ("M71", "M72"):
            unit = 1.0 if upper == "M71" else _INCH
            continue
        if upper.startswith("ICI") and "ON" in upper or upper == "G91":
            raise env.KatError(f"{path.name}: incremental coordinates are not supported")
        if m := re.fullmatch(r"T0*(\d+)(?:[FSBH][\d.]+)*C([\d.]+)(?:[FSBH][\d.]+)*", upper):
            if unit is None:
                raise env.KatError(f"{path.name}: tool sizes before the units (METRIC or INCH)")
            tools[m.group(1)] = float(m.group(2)) * unit
            tool_notes[m.group(1)] = note
            note = (None, "hole")
            continue
        if m := re.fullmatch(r"T0*(\d+)", upper):
            tool = m.group(1)
            continue
        if upper in ("%", "M95"):
            continue
        if upper in ("M30", "M00"):
            break
        if upper == "M15":
            down = True
            continue
        if upper == "M16" or upper == "M17":
            down = False
            continue
        if upper == "G05":
            routing = down = False
            continue
        if upper.startswith(("G90", "FMAT", "VER", "DETECT", "ATC", "AFS", "OM", "TCST", "RSB", "M47", "G93")):
            continue
        if not seen_header and unit is None and re.match(r"[XYGTR]", upper):
            raise env.KatError(f"{path.name} has no header (M48 with METRIC or INCH)")
        if m := re.fullmatch(r"R(\d+)(.*)", upper):
            dx = dict(re.findall(r"([XY])([+-]?\d*\.?\d*)", m.group(2)))
            sx = number(dx["X"]) if dx.get("X") else 0.0
            sy = number(dx["Y"]) if dx.get("Y") else 0.0
            for _ in range(int(m.group(1))):
                x, y = x + sx, y + sy
                hit(x, y)
            continue
        if "G85" in upper:
            first, second = upper.split("G85", 1)
            a, b = coords(first), None
            x, y = a
            b = coords(second)
            slot(a, b)
            x, y = b
            routing = False
            continue
        if upper.startswith(("G00", "G01", "G02", "G03")):
            g = int(upper[1:3])
            routing = True
            nx, ny = coords(upper[3:])
            if g == 1 and down:
                slot((x, y), (nx, ny))
            elif g in (2, 3) and down:
                arcs += 1
            x, y = nx, ny
            continue
        if re.match(r"[XY]", upper):
            nx, ny = coords(upper)
            if down:
                slot((x, y), (nx, ny))
            elif not routing:
                hit(nx, ny)
            x, y = nx, ny
            continue
        if re.match(r"[GM]\d", upper) and not upper.startswith(("G04", "M47")):
            warnings.append(f"{path.name}: {line!r} not understood: left out")
    if unit is None:
        raise env.KatError(f"{path.name} has no units (METRIC or INCH): is it an Excellon file?")
    if arcs:
        warnings.append(f"{path.name}: {arcs} routed arcs left out")
    if unsure:
        warnings.append(f"{path.name} does not say which zeros it leaves out: trailing digits taken as "
                        "kept; check the result")
    return {"file": str(path), "holes": holes, "warnings": warnings}


# --- ODB++ ----------------------------------------------------------------------------------

_DRILL_VALUES = {0: True, 1: False, 2: None}      # .drill: plated, non_plated, via
_SYMBOL = re.compile(r"(?i)^(?:r|oval)\s*([\d.]+)(?:x([\d.]+))?")


def _odb_profile(text: str, name: str) -> tuple[list, list[str]]:
    scale = units_of(text, name)
    loops, warnings = [], []
    current, start, at = None, None, None
    for raw in text.splitlines():
        f = raw.split()
        if not f or f[0].startswith("#"):
            continue
        k = f[0].upper()
        if k == "OB":
            at = start = (float(f[1]) * scale, float(f[2]) * scale)
            current = []
        elif current is None:
            continue
        elif k == "OS":
            p = (float(f[1]) * scale, float(f[2]) * scale)
            if p != at:
                current.append(_line(at, p))
            at = p
        elif k == "OC":
            p = (float(f[1]) * scale, float(f[2]) * scale)
            c = (float(f[3]) * scale, float(f[4]) * scale)
            current.append(_arc(at, p, c, f[5].upper() == "Y"))
            at = p
        elif k == "OE":
            if at != start and math.hypot(at[0] - start[0], at[1] - start[1]) > 1e-6:
                current.append(_line(at, start))
            if current:
                loops.append(current)
            current = None
    return loops, warnings


def _odb_holes(text: str, name: str, layer: str) -> tuple[list[dict], list[str]]:
    scale = units_of(text, name)
    symbols: dict[int, str] = {}
    attrs: dict[int, str] = {}
    holes, warnings = [], []
    guess = _plating_word(layer)

    def drill_of(attr_text: str):
        """(plating, kind) from a feature's attributes."""
        for item in attr_text.split(";")[1].split(",") if ";" in attr_text else []:
            key, _, value = item.partition("=")
            key = key.strip()
            if key.isdigit() and attrs.get(int(key), "").lower() == ".drill" and value.strip().isdigit():
                v = int(value)
                return _DRILL_VALUES.get(v), "via" if v == 2 else "hole"
            if key.lower() == ".drill":
                v = value.strip().lower()
                return {"plated": True, "non_plated": False, "via": None}.get(v), "via" if v == "via" else "hole"
        return guess, "hole"

    def size_of(k: int) -> tuple[float, float] | None:
        m = _SYMBOL.match(symbols.get(k, ""))
        if not m:
            return None
        unit = 0.001 if scale == 1.0 else _INCH / 1000       # micrometres or mils
        w = float(m.group(1)) * unit
        return (w, float(m.group(2)) * unit if m.group(2) else w)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("$"):
            k, _, sym = line[1:].partition(" ")
            symbols[int(k)] = sym.strip()
        elif line.startswith("@"):
            k, _, attr = line[1:].partition(" ")
            attrs[int(k)] = attr.strip()
        elif line.startswith(("P ", "L ", "A ")):
            f = line.split(";")[0].split()
            plating, kind = drill_of(line)
            sym = int(f[3] if f[0] == "P" else f[5])
            size = size_of(sym)
            if size is None:
                warnings.append(f"{name}: drill symbol {symbols.get(sym)!r} not understood: left out")
                continue
            if f[0] == "A":
                warnings.append(f"{name}: a routed arc left out")
                continue
            if f[0] == "L":
                a, b = (float(f[1]) * scale, float(f[2]) * scale), (float(f[3]) * scale, float(f[4]) * scale)
                holes.append({**_slot(a, b, size[0]), "plating": plating, "kind": kind})
                continue
            orient = int(f[6]) if len(f) > 6 else 0
            rot = float(f[7]) if orient in (8, 9) and len(f) > 7 else 90.0 * (orient % 4)
            w, h = size
            hole = {"x": _r(float(f[1]) * scale), "y": _r(-float(f[2]) * scale), "d": _r(min(w, h)),
                    "length": None, "angle": 0.0, "plating": plating, "kind": kind}
            if abs(w - h) > 1e-6:                            # an oval drill, turned clockwise
                hole["length"] = _r(max(w, h))
                hole["angle"] = round((-rot + (0 if w > h else 90)) % 180, 4)
            holes.append(hole)
    return holes, warnings


def read_odb(job: Path | str, step: str | None = None) -> dict:
    """Outline and holes of an ODB++ job: the step's profile (islands and holes) and its drill
    layers, with plating from the ``.drill`` attribute, else the layer's name. ``step``: which
    one, if several have a profile. Returns loops, holes, warnings."""
    j = Job(job)
    try:
        s = j.step(step, "profile")
        name = f"steps/{s}/profile"
        text = j.read(name)
        if text is None:
            raise env.KatError(f"step {s} of {j.path.name} has no profile")
        loops, warnings = _odb_profile(text, name)
        layers = [l["name"] for l in j.layers() if l["type"] == "DRILL"]
        holes = []
        for layer in layers:
            name = f"steps/{s}/layers/{layer}/features"
            text = j.read(name)
            if text is not None:
                found, more = _odb_holes(text, name, layer)
                holes += found
                warnings += more
    finally:
        j.close()
    return {"job": str(job), "step": s, "loops": loops, "open": [], "holes": holes, "warnings": warnings}


# --- onto the board ---------------------------------------------------------------------------

_EDGE_WIDTH = 0.05


def _stroke() -> list:
    return [Sym("stroke"), [Sym("width"), num(_EDGE_WIDTH)], [Sym("type"), Sym("default")]]


def _shift(p, offset):
    return (_r(p[0] + offset[0]), _r(p[1] + offset[1]))


def _outline_nodes(loop: list, offset) -> list[list]:
    """A loop as board items: a polygon if all straight, else lines, arcs, circles."""
    if all(s[0] == "line" for s in loop) and len(loop) >= 3:
        pts = [_shift(s[1], offset) for s in loop]
        return [[Sym("gr_poly"), [Sym("pts")] + [[Sym("xy"), num(x), num(y)] for x, y in pts], _stroke(),
                 [Sym("fill"), Sym("no")], [Sym("layer"), "Edge.Cuts"], [Sym("uuid"), new_uuid()]]]
    out = []
    for s in loop:
        if s[0] == "circle":
            c = _shift(s[1], offset)
            out.append([Sym("gr_circle"), [Sym("center"), num(c[0]), num(c[1])],
                        [Sym("end"), num(c[0] + s[2]), num(c[1])], _stroke(), [Sym("fill"), Sym("no")],
                        [Sym("layer"), "Edge.Cuts"], [Sym("uuid"), new_uuid()]])
        elif s[0] == "arc":
            a, m, b = (_shift(p, offset) for p in s[1:])
            out.append([Sym("gr_arc"), [Sym("start"), num(a[0]), num(a[1])], [Sym("mid"), num(m[0]), num(m[1])],
                        [Sym("end"), num(b[0]), num(b[1])], _stroke(), [Sym("layer"), "Edge.Cuts"],
                        [Sym("uuid"), new_uuid()]])
        else:
            a, b = (_shift(p, offset) for p in s[1:])
            out.append([Sym("gr_line"), [Sym("start"), num(a[0]), num(a[1])], [Sym("end"), num(b[0]), num(b[1])],
                        _stroke(), [Sym("layer"), "Edge.Cuts"], [Sym("uuid"), new_uuid()]])
    return out


def _loop_box(loop: list) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for s in loop:
        if s[0] == "circle":
            xs += [s[1][0] - s[2], s[1][0] + s[2]]
            ys += [s[1][1] - s[2], s[1][1] + s[2]]
        else:
            for p in s[1:]:
                xs.append(p[0])
                ys.append(p[1])
    return min(xs), min(ys), max(xs), max(ys)


def hole_node(ref: str, hole: dict, plated: bool, annular: float = 0.25) -> list:
    """A board-only footprint holding one imported hole or slot. Marked with ``MECH_LIB``, so a
    later import can replace it; a plated one has pad number 1, as KiCad's own plated mounting
    holes do, so it is an obstacle to the router."""
    d, length = hole["d"], hole["length"]
    ring = 2 * annular if plated else 0.0
    rot = hole["angle"] if length else 0.0
    w, h = (length + ring, d + ring) if length else (d + ring, d + ring)
    at = [Sym("at"), num(hole["x"]), num(hole["y"])]
    if rot:
        at.append(num(rot))
    pad_at = [Sym("at"), num(0), num(0)]
    if rot:
        pad_at.append(num(rot))
    drill = [Sym("drill"), Sym("oval"), num(length), num(d)] if length else [Sym("drill"), num(d)]
    value = f"{length:g}x{d:g} mm slot" if length else f"{d:g} mm hole"
    cw, ch = w / 2 + 0.25, h / 2 + 0.25

    def text(field: str, content: str, layer: str, y: float) -> list:
        return [Sym("property"), field, content, [Sym("at"), num(0), num(y), *([num(rot)] if rot else [])],
                [Sym("layer"), layer], [Sym("hide"), Sym("yes")], [Sym("uuid"), new_uuid()],
                [Sym("effects"), [Sym("font"), [Sym("size"), num(1), num(1)]]]]

    court: list
    stroke = [Sym("stroke"), [Sym("width"), num(0.05)], [Sym("type"), Sym("default")]]
    if length:
        court = [Sym("fp_rect"), [Sym("start"), num(-cw), num(-ch)], [Sym("end"), num(cw), num(ch)], stroke,
                 [Sym("fill"), Sym("no")], [Sym("layer"), "F.CrtYd"], [Sym("uuid"), new_uuid()]]
    else:
        court = [Sym("fp_circle"), [Sym("center"), num(0), num(0)], [Sym("end"), num(cw), num(0)], stroke,
                 [Sym("fill"), Sym("no")], [Sym("layer"), "F.CrtYd"], [Sym("uuid"), new_uuid()]]
    return [Sym("footprint"), MECH_LIB, [Sym("layer"), "F.Cu"], [Sym("uuid"), new_uuid()], at,
            text("Reference", ref, "F.SilkS", -ch - 1), text("Value", value, "F.Fab", ch + 1),
            [Sym("attr"), Sym("board_only"), Sym("exclude_from_pos_files"), Sym("exclude_from_bom")],
            court,
            [Sym("pad"), "1" if plated else "", Sym("thru_hole" if plated else "np_thru_hole"),
             Sym("oval" if length else "circle"), pad_at, [Sym("size"), num(w), num(h)], drill,
             [Sym("layers"), "*.Cu", "*.Mask"], [Sym("uuid"), new_uuid()]]]


def _same_hole(a: dict, b: dict) -> bool:
    return (abs(a["x"] - b["x"]) < 0.005 and abs(a["y"] - b["y"]) < 0.005 and abs(a["d"] - b["d"]) < 0.005
            and abs((a["length"] or 0) - (b["length"] or 0)) < 0.005)


def apply(b, loops: list, holes: list[dict], at=None, offset=None, vias: bool = False, min_drill: float = 0.0,
          annular: float = 0.25, assume: str | None = None, replace: bool = True) -> dict:
    """Put an outline and holes on board ``b``: every closed loop on Edge.Cuts and a board-only
    footprint per hole. See ``kat.pcb.import_mechanics``."""
    warnings = []
    if assume not in (None, "plated", "non_plated"):
        raise env.KatError("assume is plated or non_plated")
    outer = None
    if loops:
        boxes = [_loop_box(l) for l in loops]
        outer = max(boxes, key=lambda q: (q[2] - q[0]) * (q[3] - q[1]))
    if at is not None:
        if outer is None:
            raise env.KatError("at places the outline's top-left corner: there is no outline")
        if offset is not None:
            raise env.KatError("give at or offset, not both")
        offset = (at[0] - outer[0], at[1] - outer[1])
    offset = tuple(offset or (0.0, 0.0))
    if loops:
        b.clear_outline()
        for loop in loops:
            for n in _outline_nodes(loop, offset):
                b._insert(n)
        contained = [q for q in boxes if q is not outer and q[0] >= outer[0] - 1e-6 and q[1] >= outer[1] - 1e-6
                     and q[2] <= outer[2] + 1e-6 and q[3] <= outer[3] + 1e-6]
        if len(contained) != len(boxes) - 1:
            warnings.append(f"{len(boxes) - 1 - len(contained)} loops lie outside the largest one: check that "
                            "they are board edges and not drawing frames")
    if replace:
        for f in [f for f in b.footprints() if f.lib_id == MECH_LIB]:
            b.tree.remove(f.node)
    existing = [p for p in b.pads() if p.drill]
    used = {f.ref for f in b.footprints()}
    counter = 0
    placed: list[dict] = []
    skipped = {"vias": 0, "small": 0, "duplicates": 0, "on_pads": 0}
    unknown = 0
    for h in holes:
        if h["kind"] == "via" and not vias:
            skipped["vias"] += 1
            continue
        if h["d"] < min_drill:
            skipped["small"] += 1
            continue
        hole = {**h, "x": _r(h["x"] + offset[0]), "y": _r(h["y"] + offset[1])}
        if any(_same_hole(hole, q) for q in placed):
            skipped["duplicates"] += 1
            continue
        if any(abs(p.center[0] - hole["x"]) < 0.01 and abs(p.center[1] - hole["y"]) < 0.01 for p in existing):
            skipped["on_pads"] += 1
            continue
        plating = h["plating"]
        if plating is None and h["kind"] != "via":
            unknown += 1
            plating = assume == "plated"
        elif h["kind"] == "via":
            plating = True
        placed.append({**hole, "plating": plating})
        while f"MH{counter + 1}" in used:
            counter += 1
        counter += 1
        ref = f"MH{counter}"
        used.add(ref)
        b._insert(hole_node(ref, hole, plating, annular))
    if unknown and not assume:
        warnings.append(f"{unknown} holes do not say whether they are plated: taken as non_plated (assume=...)")
    return {"loops": len(loops), "cutouts": max(0, len(loops) - 1), "outline": list(b.outline_box() or []) or None,
            "offset": list(offset),
            "holes": {"plated": sum(1 for q in placed if q["plating"]), "non_plated": sum(1 for q in placed if not q["plating"]),
                      "slots": sum(1 for q in placed if q["length"]), "skipped": {k: v for k, v in skipped.items() if v}},
            "warnings": warnings}
