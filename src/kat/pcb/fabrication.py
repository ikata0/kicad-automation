"""Fabrication output and the release gate, through kicad-cli.

``fab`` writes Gerbers (+ drill files and map), a pick-and-place file, the IPC-D-356
netlist, the BOM from the schematic (with part numbers), STEP and 3D renders, and zips
the Gerbers. ``ready`` is the gate before ordering: board check clean, title block filled,
fabrication set complete.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .. import env
from ..sexpr import find
from . import checks, views
from .board import Board

TECH = ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Edge.Cuts"]
BOM_FIELDS = ("Reference,Value,Footprint,MPN,${QUANTITY},${DNP}", "Refs,Value,Footprint,MPN,Qty,DNP")
BOM_GROUP = "Value,Footprint,MPN,${DNP}"     # without ${DNP} a fitted and a DNP part share a row, marked DNP


def _title(b: Board) -> dict:
    tb = find(b.tree, "title_block")
    out = {}
    for c in (tb or [])[1:]:
        if isinstance(c, list):
            key = str(c[0]) if str(c[0]) != "comment" else f"comment{c[1]}"
            out[key] = str(c[-1])
    return out


def board_variants(b: Board) -> list[str]:
    """The assembly variant names on the board (KiCad 10, written by ``kat pcb sync``)."""
    return [str(find(v, "name")[1]) for v in (find(b.tree, "variants") or [])[1:]
            if isinstance(v, list) and find(v, "name") is not None]


def variant_stem(stem: str, variant: str) -> str:
    """``<board>-<variant>``: a variant's file name stem, unsafe characters as ``_``."""
    return f"{stem}-{re.sub(r'[^A-Za-z0-9_.-]', '_', variant)}"


def fab(pcb: Path | str, out: Path | str | None = None, step: bool = True, three_d: bool = True) -> dict:
    """Write the fabrication set into ``out`` (default fab/<name>-<rev>/ next to the board).
    Each assembly variant on the board gets its own position file and BOM in assembly/
    (``<board>-<variant>-pos.csv`` / ``-bom.csv``, listed in ``variants``); the base files
    keep their names. Refused when the board has vias its fabrication profile forbids
    (``fab_profile``)."""
    from . import fabprofile as profile
    pcb = Path(pcb)
    b = Board(pcb)
    if bad := profile.findings(b).get("forbidden_vias"):
        raise env.KatError(f"{len(bad)} vias the fabrication profile forbids ({', '.join(sorted({v['kind'] for v in bad}))}, "
                           f"e.g. at {bad[0]['at']}): the supplier cannot make the board")
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
                    "--group-by", BOM_GROUP, "-o", str(bom), str(sch))
        files["bom"] = bom.name
    if names := board_variants(b):
        files["variants"] = {}
        for v in names:
            vstem = variant_stem(pcb.stem, v)
            vpos = asm / f"{vstem}-pos.csv"
            env.run_cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
                        "--exclude-dnp", "--variant", v, "-o", str(vpos), str(pcb))
            entry = {"pos": vpos.name}
            if sch.exists():
                vbom = asm / f"{vstem}-bom.csv"
                env.run_cli("sch", "export", "bom", "--fields", BOM_FIELDS[0], "--labels", BOM_FIELDS[1],
                            "--group-by", BOM_GROUP, "--variant", v, "-o", str(vbom), str(sch))
                entry["bom"] = vbom.name
            files["variants"][v] = entry
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
    unconnected, parts outside the outline), departures from the fabrication profile
    (``fab_profile``: forbidden vias, classes or rules below its minimum), title or revision empty, fabrication set
    missing or incomplete (Gerbers for copper, mask and outline, drill, position file,
    BOM, IPC-D-356, and both per assembly variant), or older than the board; (error
    ``assembly``, one per variant) the assembly data check of ``assembly_check.check`` for
    the base and every variant when the schematic is next to the board, whose variants must
    be the board's (``variants_differ``). ``assembly`` in the result: per variant ``ok``,
    counts and the parts that ``differs`` from the base. ``accept``: DRC warning kinds judged
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
    if c.get("profile"):
        errors.append({"problem": "fab_profile", "detail": c["profile"]})
    accepted = {"warnings": {k: {"count": warn_kinds[k], "reason": r} for k, r in accept.items() if k in warn_kinds},
                "overhang": [r for r in overhang if r in c["overhang"]]}
    sch = pcb.with_suffix(".kicad_sch")
    assembly = None
    vnames = board_variants(b)
    if sch.exists():
        from .. import variants as _variants
        from . import assembly_check
        assembly = {}
        for v in [None, *vnames]:
            a = assembly_check.check(pcb, sch, v)
            assembly[v or "base"] = {k: a[k] for k in ("ok", "counts", "bom", "fitted", "placed")}
            if v:
                assembly[v]["differs"] = a["differs"]
            if not a["ok"]:
                errors.append({"problem": "assembly", "detail": {
                    "variant": v, "counts": a["counts"], "errors": a["errors"][:20],
                    "more": max(0, len(a["errors"]) - 20)}})
        in_sch = [n["name"] if isinstance(n, dict) else str(n) for n in _variants.listing(sch)["names"]]
        if sorted(in_sch) != sorted(vnames):
            errors.append({"problem": "variants_differ", "detail": {
                "schematic": sorted(in_sch), "board": sorted(vnames), "fix": "kat pcb sync"}})
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
        for v in vnames:
            vstem = variant_stem(pcb.stem, v)
            for what, suffix in (("position file", "pos"), ("BOM", "bom")):
                if what == "BOM" and not sch.exists():
                    continue
                if not (d / "assembly" / f"{vstem}-{suffix}.csv").exists():
                    errors.append({"problem": "fab_incomplete", "detail": f"{what} of variant {v}"})
        newest = max((p.stat().st_mtime for p in d.rglob("*") if p.is_file()), default=0)
        if newest and pcb.stat().st_mtime > newest:
            errors.append({"problem": "fab_outdated", "detail": "the board changed after kat pcb fab"})
    return {"file": str(pcb), "ready": not errors, "errors": errors, "fab_dir": str(d),
            **({"accepted": accepted} if accepted["warnings"] or accepted["overhang"] else {}),
            **({"assembly": assembly} if assembly else {}),
            "check": {k: c[k] for k in ("ok", "clean", "drc_errors", "drc_warnings", "unconnected",
                                        "parity", "islands")}}
