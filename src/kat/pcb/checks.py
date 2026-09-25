"""Board verification through KiCad's DRC (kicad-cli pcb drc)."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

from .. import env


def _items(v: dict) -> list[dict]:
    return [{"description": i["description"], "x": i["pos"]["x"], "y": i["pos"]["y"]}
            for i in v.get("items", [])]


def drc(pcb: Path, parity: bool = True, refill: bool = False, save: bool = False) -> dict:
    """KiCad's DRC as JSON: violations (with positions in mm), unconnected items and
    schematic parity problems. ``refill``: refill zones first (``save`` writes them back)."""
    args = ["pcb", "drc", "--format", "json", "--severity-all"]
    if parity:
        args.append("--schematic-parity")
    if refill:
        args.append("--refill-zones")
        if save:
            args.append("--save-board")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "drc.json"
        env.run_cli(*args, "-o", str(out), str(pcb), check=False)
        if not out.exists():
            raise env.KatError(f"kicad-cli pcb drc wrote no report for {pcb}")
        data = json.loads(out.read_text())
    conv = lambda vs: [{"severity": v["severity"], "type": v["type"],
                        "description": v["description"], "items": _items(v)} for v in vs]
    violations = conv(data.get("violations", []))
    return {"violations": violations,
            "unconnected": conv(data.get("unconnected_items", [])),
            "parity": conv(data.get("schematic_parity", [])),
            "by_type": dict(Counter(v["type"] for v in violations).most_common()),
            "errors": sum(v["severity"] == "error" for v in violations),
            "warnings": sum(v["severity"] == "warning" for v in violations)}


# copper split into pieces the router or the pour believed connected (spike: 29 unconnected
# after a "0 unrouted" autoroute)
ISLAND_TYPES = ("starved_thermal", "isolated_copper", "zone_has_empty_net")


def run(pcb: Path) -> dict:
    """KiCad's DRC (zones refilled in memory, parity against the schematic) plus kat's
    findings: board outline missing, parts outside the outline.

    ``ok``: no DRC errors, parity 0, nothing unconnected, outline present.
    ``clean``: ``ok`` and no DRC warnings and no kat findings."""
    from .board import Board
    pcb = Path(pcb)
    b = Board(pcb)
    d = drc(pcb, parity=pcb.with_suffix(".kicad_sch").exists(), refill=True)
    outline = b.outline_box()
    outside = []
    if outline:
        for f in b.footprints():
            box = f.box()
            if box and not (box[0] >= outline[0] and box[1] >= outline[1]
                            and box[2] <= outline[2] and box[3] <= outline[3]):
                outside.append(f.ref)
    islands = [v for v in d["violations"] if v["type"] in ISLAND_TYPES]
    result = {"file": str(pcb), "outline": list(outline) if outline else None,
              "footprints": len(b.footprints()), "tracks": len(b.tracks()), "vias": len(b.vias()),
              "drc_errors": d["errors"], "drc_warnings": d["warnings"], "by_type": d["by_type"],
              "unconnected": len(d["unconnected"]), "parity": len(d["parity"]),
              "islands": len(islands), "outside_outline": outside,
              "violations": d["violations"], "unconnected_items": d["unconnected"],
              "parity_items": d["parity"]}
    ok = bool(outline) and d["errors"] == 0 and not d["parity"] and not d["unconnected"]
    result["ok"] = ok
    result["clean"] = ok and d["warnings"] == 0 and not outside
    return result


def finding_spots(check: dict) -> list[tuple[str, tuple[float, float]]]:
    """(label, position) of every board finding with a position: violations first, then
    unconnected items."""
    spots = []
    for v in check.get("violations", []):
        # a zone's position is its first outline corner: show the other item (the pad)
        items = [i for i in v["items"] if not i["description"].startswith("Zone")] or v["items"]
        for it in items[:1]:
            spots.append((v["type"], (it["x"], it["y"])))
    for v in check.get("unconnected_items", []):
        for it in v["items"][:1]:
            spots.append(("unconnected", (it["x"], it["y"])))
    return spots
