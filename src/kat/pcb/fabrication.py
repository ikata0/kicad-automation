"""Fabrication output and the release gate, through kicad-cli.

``fab`` writes Gerbers (+ drill files and map), a pick-and-place file, the IPC-D-356
netlist, the BOM from the schematic (with part numbers), STEP and 3D renders, and zips
the Gerbers. ``ready`` is the gate before ordering: board check clean, title block filled,
fabrication set complete.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from .. import env
from ..sexpr import find
from . import checks, views
from .board import Board

TECH = ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
BOM_FIELDS = ("Reference,Value,Footprint,MPN,${QUANTITY},${DNP}", "Refs,Value,Footprint,MPN,Qty,DNP")


def _title(b: Board) -> dict:
    tb = find(b.tree, "title_block")
    out = {}
    for c in (tb or [])[1:]:
        if isinstance(c, list):
            key = str(c[0]) if str(c[0]) != "comment" else f"comment{c[1]}"
            out[key] = str(c[-1])
    return out


def fab(pcb: Path | str, out: Path | str | None = None, step: bool = True, three_d: bool = True) -> dict:
    """Write the fabrication set into ``out`` (default fab/<name>-<rev>/ next to the board)."""
    pcb = Path(pcb)
    b = Board(pcb)
    rev = _title(b).get("rev") or "draft"
    out = Path(out) if out else pcb.parent / "fab" / f"{pcb.stem}-{rev}"
    ger, asm = out / "gerbers", out / "assembly"
    ger.mkdir(parents=True, exist_ok=True)
    asm.mkdir(parents=True, exist_ok=True)
    layers = b.copper_layers() + TECH
    env.run_cli("pcb", "export", "gerbers", "--check-zones", "--layers", ",".join(layers),
                "-o", str(ger) + "/", str(pcb))
    env.run_cli("pcb", "export", "drill", "--format", "excellon", "--excellon-separate-th",
                "--generate-map", "--map-format", "gerberx2", "-o", str(ger) + "/", str(pcb))
    files = {"gerbers": sorted(p.name for p in ger.iterdir())}
    pos = asm / f"{pcb.stem}-pos.csv"
    env.run_cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
                "--exclude-dnp", "-o", str(pos), str(pcb))
    ipc = out / f"{pcb.stem}.ipc"
    env.run_cli("pcb", "export", "ipcd356", "-o", str(ipc), str(pcb))
    files.update(pos=pos.name, ipc=ipc.name)
    sch = pcb.with_suffix(".kicad_sch")
    if sch.exists():
        bom = asm / f"{pcb.stem}-bom.csv"
        env.run_cli("sch", "export", "bom", "--fields", BOM_FIELDS[0], "--labels", BOM_FIELDS[1],
                    "--group-by", "Value,Footprint,MPN", "-o", str(bom), str(sch))
        files["bom"] = bom.name
    if step:
        stp = out / f"{pcb.stem}.step"
        proc = env.run_cli("pcb", "export", "step", "--subst-models", "-o", str(stp), str(pcb), check=False)
        if proc.returncode == 0 and stp.exists():
            files["step"] = stp.name
    if three_d:
        files["renders"] = [Path(p).name for side in ("top", "bottom")
                            if (p := views.render_3d(pcb, out / f"{pcb.stem}-3d-{side}.png", side))]
    zpath = out / f"{pcb.stem}-{rev}-gerbers.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ger.iterdir()):
            z.write(p, p.name)
    files["zip"] = zpath.name
    return {"dir": str(out), "files": files}


REQUIRED = {"gerber copper": ("gerbers", "Cu"), "gerber outline": ("gerbers", "Edge_Cuts"),
            "gerber mask": ("gerbers", "Mask"), "drill": ("gerbers", ".drl")}


def ready(pcb: Path | str, fab_dir: Path | str | None = None, accept: dict[str, str] | None = None,
          overhang: list[str] | None = None) -> dict:
    """Release gate. Errors: board check not clean (DRC errors or warnings, parity,
    unconnected, parts outside the outline), title or revision empty, fabrication set
    missing or incomplete (Gerbers for copper, mask and outline, drill, position file,
    BOM, IPC-D-356), or older than the board. ``accept``: DRC warning kinds judged
    harmless, each with its reason ({"via_dangling": "..."}); ``overhang``: parts meant to
    reach beyond the edge (pads inside). Both are listed in the result; DRC errors can
    never be accepted."""
    pcb = Path(pcb)
    b = Board(pcb)
    c = checks.run(pcb)
    accept, overhang = dict(accept or {}), list(overhang or [])
    errors = []
    warn_kinds = {v["type"]: 0 for v in c["violations"] if v["severity"] == "warning"}
    for v in c["violations"]:
        if v["severity"] == "warning":
            warn_kinds[v["type"]] += 1
    left_warnings = {k: n for k, n in warn_kinds.items() if k not in accept}
    left_overhang = [r for r in c["overhang"] if r not in overhang]
    if not c["ok"] or left_warnings or c["outside_outline"] or left_overhang:
        errors.append({"problem": "check_not_clean",
                       "detail": {**{k: c[k] for k in ("drc_errors", "unconnected", "parity", "islands",
                                                        "outside_outline")},
                                  "warnings": left_warnings, "overhang": left_overhang}})
    accepted = {"warnings": {k: {"count": warn_kinds[k], "reason": r} for k, r in accept.items() if k in warn_kinds},
                "overhang": [r for r in overhang if r in c["overhang"]]}
    t = _title(b)
    for key in ("title", "rev"):
        if not t.get(key):
            errors.append({"problem": "title_block", "detail": f"{key} empty (kat title SCH; "
                                                              "board.title_from_schematic())"})
    rev = t.get("rev") or "draft"
    d = Path(fab_dir) if fab_dir else pcb.parent / "fab" / f"{pcb.stem}-{rev}"
    if not d.is_dir():
        errors.append({"problem": "no_fab_output", "detail": f"{d} missing (kat pcb fab)"})
    else:
        names = [p.name for p in (d / "gerbers").glob("*")] if (d / "gerbers").is_dir() else []
        for what, (_, needle) in REQUIRED.items():
            if not any(needle in n for n in names):
                errors.append({"problem": "fab_incomplete", "detail": what})
        for what, pattern in (("position file", "assembly/*-pos.csv"), ("BOM", "assembly/*-bom.csv"),
                              ("IPC-D-356", "*.ipc")):
            if not list(d.glob(pattern)):
                errors.append({"problem": "fab_incomplete", "detail": what})
        newest = max((p.stat().st_mtime for p in d.rglob("*") if p.is_file()), default=0)
        if newest and pcb.stat().st_mtime > newest:
            errors.append({"problem": "fab_outdated", "detail": "the board changed after kat pcb fab"})
    return {"file": str(pcb), "ready": not errors, "errors": errors, "fab_dir": str(d),
            **({"accepted": accepted} if accepted["warnings"] or accepted["overhang"] else {}),
            "check": {k: c[k] for k in ("ok", "clean", "drc_errors", "drc_warnings", "unconnected",
                                        "parity", "islands")}}
