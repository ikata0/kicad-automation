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
