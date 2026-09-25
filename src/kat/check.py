"""One-shot verification: file loads, ERC, netlist vs. expectation, text overlaps."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from . import env, netlist
from .schematic import Schematic, _font_size, text_width
from .sexpr import find, find_all, is_yes

Box = tuple[float, float, float, float]


def erc(sch: Path) -> dict:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "erc.json"
        env.run_cli("sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(sch))
        data = json.loads(out.read_text())
    violations = []
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            violations.append({
                "severity": v["severity"], "type": v["type"], "description": v["description"],
                # kicad-cli 10 reports positions in units of 100 mm despite "coordinate_units"
                "items": [{"description": i["description"],
                           "x": round(i["pos"]["x"] * 100, 3), "y": round(i["pos"]["y"] * 100, 3)}
                          for i in v.get("items", [])]})
    count = lambda s: sum(v["severity"] == s for v in violations)
    return {"errors": count("error"), "warnings": count("warning"), "violations": violations}


GLYPH = 1.15  # rendered glyph height relative to the font size
LABEL_LIFT = 0.4  # KiCad draws label text this far (mm) off its wire


def _text_box(x: float, y: float, text: str, size: float, vertical: bool, justify: set[str]) -> Box:
    w, h = text_width(text, size), size * GLYPH
    if vertical:
        w, h = h, w
    if "left" in justify:
        x0 = x if not vertical else x - w / 2
    elif "right" in justify:
        x0 = x - w if not vertical else x - w / 2
    else:
        x0 = x - w / 2
    y0 = y - h / 2
    return x0, y0, x0 + w, y0 + h


def _overlap(a: Box, b: Box, margin: float = -0.3) -> bool:
    """True if the boxes intersect by more than ``margin`` (negative: closer than |margin|)."""
    return a[0] < b[2] - margin and b[0] < a[2] - margin and a[1] < b[3] - margin and b[1] < a[3] - margin


def text_items(s: Schematic) -> list[dict]:
    """Approximate boxes of visible symbol fields and labels (heuristic, stroke font)."""
    items = []
    for sym in s.symbols():
        for p in find_all(sym.node, "property"):
            if is_yes(find(p, "hide")) or not str(p[2]):
                continue
            at = find(p, "at")
            ang = float(at[3]) if len(at) > 3 else 0
            vertical = (ang % 180 == 90) != (sym.place.rot % 180 == 90)
            just = {str(t) for t in (find(find(p, "effects") or [], "justify") or [])[1:]}
            items.append({"owner": sym.ref, "what": f"{sym.ref}.{p[1]}", "text": str(p[2]),
                          "box": _text_box(float(at[1]), float(at[2]), str(p[2]), _font_size(p),
                                           vertical, just)})
    for l in s.labels():
        w, h = text_width(l["text"]), 1.27 * GLYPH + LABEL_LIFT
        x, y, a = l["x"], l["y"], l["angle"] % 360
        box = {0: (x, y - h, x + w, y), 180: (x - w, y - h, x, y),
               90: (x - h, y - w, x, y), 270: (x - h, y, x, y + w)}.get(int(a), (x, y - h, x + w, y))
        items.append({"owner": None, "what": f'label "{l["text"]}"', "text": l["text"], "box": box})
    return items


def overlaps(s: Schematic) -> list[dict]:
    items = text_items(s)
    bodies = []
    for sym in s.symbols():
        box = s.body_box(sym)
        if box and (box[2] - box[0]) * (box[3] - box[1]) > 0.5:
            bodies.append({"ref": sym.ref, "box": box})
    found = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if _overlap(a["box"], b["box"]):
                found.append({"kind": "text/text", "a": a["what"], "b": b["what"],
                              "at": [round((a["box"][0] + a["box"][2]) / 2, 2),
                                     round((a["box"][1] + a["box"][3]) / 2, 2)]})
        for body in bodies:
            if body["ref"] != a["owner"] and _overlap(a["box"], body["box"], margin=0.3):
                found.append({"kind": "text/symbol", "a": a["what"], "b": body["ref"],
                              "at": [round((a["box"][0] + a["box"][2]) / 2, 2),
                                     round((a["box"][1] + a["box"][3]) / 2, 2)]})
    return found


def body_overlaps(s: Schematic) -> list[dict]:
    """Symbol bodies (graphics + pins) that intersect. Power symbols sitting on a part's pin
    end are expected to touch it and are ignored."""
    syms = [(sym, b) for sym in s.symbols() if (b := s.body_box(sym))]
    found = []
    for i, (a, ba) in enumerate(syms):
        for b, bb in syms[i + 1:]:
            if a.ref.startswith("#") or b.ref.startswith("#"):
                continue
            if _overlap(ba, bb, margin=0.5):
                found.append({"a": f"{a.ref}/{a.unit}", "b": f"{b.ref}/{b.unit}"})
    return found


def off_page(s: Schematic, margin: float = 10.0) -> list[str]:
    """Items outside the drawing frame (page minus ``margin`` on every side)."""
    from .sync import paper_size
    w, h = paper_size(s)
    inside = lambda b: b[0] >= margin and b[1] >= margin and b[2] <= w - margin and b[3] <= h - margin
    out = [f"{sym.ref}/{sym.unit}" for sym in s.symbols() if (b := s.body_box(sym)) and not inside(b)]
    out += [t["what"] for t in text_items(s) if not inside(t["box"])]
    return out


def run(sch: Path, expect: Path | None = None) -> dict:
    """``ok``: electrically right (ERC errors 0, netlist matches ``expect``).
    ``clean``: ``ok`` and no drawing findings (text overlaps, body overlaps, off-page items,
    design net names missing from or different in the drawing)."""
    s = Schematic(sch)  # parse error -> exception
    result: dict = {"file": str(sch)}
    result["erc"] = erc(sch)  # also proves KiCad can load it
    nets = netlist.from_schematic(sch)
    result["nets"] = len([n for n in nets.values() if len(n) > 1])
    if expect:
        result["netlist_vs_expected"] = netlist.compare(nets, netlist.read(expect))
    result["overlaps"] = overlaps(s)
    result["body_overlaps"] = body_overlaps(s)
    result["off_page"] = off_page(s)
    ok = result["erc"]["errors"] == 0 and (not expect or result["netlist_vs_expected"]["match"])
    result["ok"] = ok
    names = result.get("netlist_vs_expected", {})
    result["clean"] = ok and not (result["overlaps"] or result["body_overlaps"] or result["off_page"]
                                  or names.get("unnamed") or names.get("name_differences"))
    return result
