"""Board verification through KiCad's DRC (kicad-cli pcb drc)."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

from .. import env
from ..sexpr import find, find_all


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
        data = json.loads(out.read_text(encoding="utf-8"))
    conv = lambda vs: [{"severity": v["severity"], "type": v["type"],
                        "description": v["description"], "items": _items(v)} for v in vs]
    violations = conv(data.get("violations", []))
    return {"violations": violations,
            "unconnected": conv(data.get("unconnected_items", [])),
            "parity": conv(data.get("schematic_parity", [])),
            "by_type": dict(Counter(v["type"] for v in violations).most_common()),
            "errors": sum(v["severity"] == "error" for v in violations),
            "warnings": sum(v["severity"] == "warning" for v in violations)}


FILL_TYPES = ("clearance", "shorting_items", "hole_clearance", "copper_edge_clearance")


def _fill_hits(d: dict) -> list[tuple[str | None, str, dict]]:
    """(zone name or None, zone net, violation) for each clearance or short of a zone's fill."""
    import re
    out = []
    for v in d["violations"]:
        if v["type"] not in FILL_TYPES:
            continue
        for i in v["items"]:
            if m := re.match(r"Zone (?:'([^']*)' )?\[([^\]]*)\]", i["description"]):
                out.append((m.group(1), m.group(2), v))
                break
    return out


def refill(pcb: Path, nudge: float = 0.001) -> dict:
    """Fill every pour (KiCad's filler through DRC) and save it, then check the fill against
    other nets' copper: a reported fill over foreign vias, tracks or pads (seen on KiCad 10,
    not reproduced here) is retried once with each such zone's minimum width changed by
    ``nudge`` mm. Returns drc_errors, unconnected and, if it came to that, ``fill_retry``
    {zones {name: new minimum width}, before, after (fill violations)}."""
    from .board import Board
    d = drc(pcb, parity=False, refill=True, save=True)
    hits = _fill_hits(d)
    out: dict = {}
    if hits:
        b = Board(pcb)
        changed = {}
        for z in b.zones():
            name, net, mt = find(z, "name"), find(z, "net"), find(z, "min_thickness")
            zn, znet = (str(name[1]) if name is not None else None), (str(net[1]) if net is not None else "")
            if mt is None or not any((n is not None and n == zn) or (n is None and nt == znet) for n, nt, _ in hits):
                continue
            mt[1] = round(float(mt[1]) + nudge, 4)
            changed[zn or znet] = mt[1]
        b.save()
        d = drc(pcb, parity=False, refill=True, save=True)
        out["fill_retry"] = {"zones": changed, "before": len(hits), "after": len(_fill_hits(d)),
                             **({"hint": "the fill still meets other nets: report it to KiCad with a reduced board"}
                                if _fill_hits(d) else {})}
    return {"drc_errors": d["errors"], "unconnected": len(d["unconnected"]), **out}


# copper split into pieces the router or the pour believed connected (spike: 29 unconnected
# after a "0 unrouted" autoroute)
ISLAND_TYPES = ("starved_thermal", "isolated_copper", "zone_has_empty_net")


def isolated_pads(pcb: Path, b, unconnected: list[dict]) -> list[dict]:
    """Pads that sit on a fragment of their own net's pour cut off from its largest part:
    DRC reports those only as a missing connection between parts of the zone, at the zone's
    corner. Only zones named in ``unconnected`` (DRC's items) are filled and looked at.
    Returns [{pad, net, zone, layer, at, fragment_area (mm²), zone_area}]."""
    import re
    names = {m.group(1) for u in unconnected for i in u["items"]
             if (m := re.match(r"Zone '([^']*)'", i["description"]))}
    if not names:
        return []
    from .coupling import _filled, _pts
    from .geometry import inside
    filled = _filled(pcb)
    area = lambda p: abs(sum(a[0] * c[1] - c[0] * a[1] for a, c in zip(p, p[1:] + p[:1]))) / 2  # noqa: E731
    out = []
    for z in find_all(filled.tree, "zone"):
        name, net = find(z, "name"), find(z, "net")
        if name is None or str(name[1]) not in names or net is None:
            continue
        by_layer: dict[str, list] = {}
        for fp in find_all(z, "filled_polygon"):
            by_layer.setdefault(str(find(fp, "layer")[1]), []).append(_pts(fp))
        for layer, frags in by_layer.items():
            if len(frags) < 2:
                continue
            main = max(frags, key=area)
            for p in b.pads():
                if p.net != str(net[1]) or not p.on(layer):
                    continue
                frag = next((f for f in frags if inside(f, (p.x, p.y))), None)
                if frag is not None and frag is not main:
                    out.append({"pad": f"{p.ref}.{p.number}", "net": p.net, "zone": str(name[1]), "layer": layer,
                                "at": [round(p.x, 4), round(p.y, 4)], "fragment_area": round(area(frag), 3),
                                "zone_area": round(area(main), 3)})
    return out


def run(pcb: Path, mapping: Path | None = None, complete: bool | list[str] = True) -> dict:
    """KiCad's DRC (zones refilled in memory, parity against the schematic) plus kat's
    findings: board outline missing, parts outside the outline (``outside_outline``: pads
    outside; ``overhang``: pads inside, only the body beyond the edge), the board's
    net rules (``net_rules``: coupling, layers, plane; see ``coupling``) and its departures
    from the fabrication profile (``profile``: forbidden vias, classes or rules below the
    minimum clearance; see ``fabprofile``). ``isolated_pads``: pads on a pour fragment cut off
    from the rest of their zone (``isolated_pads``), which DRC shows only as one unconnected
    item at the zone's corner. ``mapping``: a pin
    table the pads must agree with (``kat.mapping.check``), reported in ``mapping``.

    ``ok``: no DRC errors, parity 0, nothing unconnected, outline present, mapping agrees.
    ``clean``: ``ok`` and no DRC warnings and no kat findings."""
    from .board import Board
    pcb = Path(pcb)
    b = Board(pcb)
    d = drc(pcb, parity=pcb.with_suffix(".kicad_sch").exists(), refill=True)
    outline = b.outline_box()
    outside, overhang = [], []
    if outline:
        from .geometry import inside
        from .specctra import _outline_points
        poly = _outline_points(b)
        rect = len(poly) == 4 and poly == [(outline[0], outline[1]), (outline[2], outline[1]),
                                           (outline[2], outline[3]), (outline[0], outline[3])]

        def within(pt) -> bool:
            if rect:
                return outline[0] - 1e-6 <= pt[0] <= outline[2] + 1e-6 and outline[1] - 1e-6 <= pt[1] <= outline[3] + 1e-6
            return inside(poly, pt)

        for f in b.footprints():
            box = f.box()
            corners = f.courtyard_outline() or (box and ((box[0], box[1]), (box[2], box[1]),
                                                         (box[2], box[3]), (box[0], box[3])))
            if not box or all(within(c) for c in corners):
                continue
            # pads inside, only the body beyond the edge: an edge-mount part (overhang)
            pads_in = all(within(q) for p in f.pads() for q in p.outline)
            (overhang if pads_in and f.pads() else outside).append(f.ref)
    islands = [v for v in d["violations"] if v["type"] in ISLAND_TYPES]
    cut_off = isolated_pads(pcb, b, d["unconnected"]) if d["unconnected"] else []
    from . import coupling
    rules = coupling.run(pcb, b)
    from . import fabprofile as profile
    departures = profile.findings(b)
    result = {"file": str(pcb), "outline": list(outline) if outline else None,
              "footprints": len(b.footprints()), "tracks": len(b.tracks()), "vias": len(b.vias()),
              "drc_errors": d["errors"], "drc_warnings": d["warnings"], "by_type": d["by_type"],
              "unconnected": len(d["unconnected"]), "parity": len(d["parity"]),
              "islands": len(islands), "outside_outline": outside, "overhang": overhang,
              "violations": d["violations"], "unconnected_items": d["unconnected"],
              "parity_items": d["parity"], **({"net_rules": rules} if rules else {}),
              **({"profile": departures} if departures else {}),
              **({"isolated_pads": cut_off} if cut_off else {})}
    ok = bool(outline) and d["errors"] == 0 and not d["parity"] and not d["unconnected"]
    if mapping:
        from ..mapping import check as check_mapping
        result["mapping"] = check_mapping(pcb, mapping, complete)
        ok = ok and result["mapping"]["ok"]
    result["ok"] = ok
    result["clean"] = (ok and d["warnings"] == 0 and not outside and not overhang
                       and not (rules and rules["findings"]) and not departures)
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
    for v in (check.get("net_rules") or {}).get("findings", []):
        spots.append((v["kind"], (v["x"], v["y"])))
    return spots
