"""Ready-for-layout gate: is the schematic finished enough to start the PCB?

Reads the netlist KiCad exports from the schematic, the same data the board is built from,
so parts marked "not on board" are not part of it.
"""

from __future__ import annotations

import fnmatch
import tempfile
from pathlib import Path

from . import checks, env
from .pcb.footprints import summary as footprint_summary
from .sexpr import find, find_all, loads
from .syncer import natural_key


def _export(sch: Path) -> list:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "net.net"
        env.run_cli("sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(sch))
        return loads(out.read_text(encoding="utf-8"))


def _filters_match(filters: str, fp_id: str) -> bool:
    """KiCad footprint filters: globs on the footprint name, or on Lib:Name if they hold ':'."""
    name = fp_id.split(":", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(fp_id.lower() if ":" in f else name, f.lower())
               for f in filters.split())


def run(sch: Path, expect: Path | None = None, require: tuple[str, ...] = ("MPN",)) -> dict:
    """Errors block the layout; warnings are for a human look.

    errors:   schematic not ok/clean (ERC, netlist vs ``expect``, drawing findings),
              unannotated reference, empty value, no footprint, footprint library or file
              not found, symbol pin without a pad, missing ``require`` field (parts
              excluded from the BOM are exempt)
    warnings: no ``expect`` given, numbered pad without a symbol pin, footprint outside the
              symbol's footprint filters, 3D model missing
    """
    sch = Path(sch)
    project = sch.parent
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(ref, problem, detail=""):
        errors.append({"ref": ref, "problem": problem, "detail": detail})

    def warn(ref, problem, detail=""):
        warnings.append({"ref": ref, "problem": problem, "detail": detail})

    res = env.tidy(checks.run(sch, expect))
    nl = res.get("netlist_vs_expected")
    findings = {k: len(res[k]) for k in ("overlaps", "body_overlaps", "off_page", "duplicates")}
    summary = {"ok": res["ok"], "clean": res["clean"], "erc_errors": res["erc"]["errors"],
               "erc_warnings": res["erc"]["warnings"],
               "netlist_match": nl["match"] if nl else None, "findings": findings}
    if not res["ok"]:
        err(None, "schematic_not_ok", "ERC errors or netlist differs from the design; run kat check")
    elif not res["clean"]:
        err(None, "schematic_not_clean", "drawing findings; run kat check / look at the snapshot")
    if not expect:
        warn(None, "no_expect", "netlist not compared with the design netlist")

    tree = _export(sch)
    pins = {}
    for lp in find_all(find(tree, "libparts") or [], "libpart"):
        pins[(str(find(lp, "lib")[1]), str(find(lp, "part")[1]))] = {
            str(find(p, "num")[1]) for p in find_all(find(lp, "pins") or [], "pin")}
    fp_libs = env.footprint_libraries(project)

    comps = find_all(find(tree, "components") or [], "comp")
    for c in comps:
        ref = str(find(c, "ref")[1])
        val = find(c, "value")
        value = str(val[1]) if val and len(val) > 1 else ""
        fpn = find(c, "footprint")
        fp_id = str(fpn[1]) if fpn and len(fpn) > 1 else ""
        props = {str(find(p, "name")[1]): (str(find(p, "value")[1]) if find(p, "value") else "")
                 for p in find_all(c, "property")}
        fields = {str(find(f, "name")[1]): (str(f[2]) if len(f) > 2 else "")
                  for f in find_all(find(c, "fields") or [], "field")}
        src = find(c, "libsource")
        lib_pins = pins.get((str(find(src, "lib")[1]), str(find(src, "part")[1])), set())

        if ref.endswith("?"):
            err(ref, "unannotated")
        if value in ("", "~"):
            err(ref, "no_value")
        if "exclude_from_bom" not in props:
            for name in require:
                if not fields.get(name, "").strip():
                    err(ref, "missing_field", name)
        if not fp_id:
            err(ref, "no_footprint")
            continue
        nick, _, name = fp_id.partition(":")
        if nick not in fp_libs:
            err(ref, "footprint_library_unknown", fp_id)
            continue
        path = fp_libs[nick] / f"{name}.kicad_mod"
        if not path.is_file():
            err(ref, "footprint_not_found", fp_id)
            continue
        fp = footprint_summary(path)
        if missing := sorted(lib_pins - fp["pads"], key=natural_key):
            err(ref, "pin_without_pad", f"{fp_id}: pins {', '.join(missing)}")
        if extra := sorted(fp["pads"] - lib_pins, key=natural_key):
            warn(ref, "pad_without_pin", f"{fp_id}: pads {', '.join(extra)}")
        if (filters := props.get("ki_fp_filters", "")) and not _filters_match(filters, fp_id):
            warn(ref, "footprint_filter", f"{fp_id} not in {filters}")
        models = [Path(env.expand(m, project)) for m in fp["models"]]
        if not any(m.exists() or m.with_suffix(".wrl").exists() for m in models):
            warn(ref, "no_3d_model", fp_id)

    by_problem: dict[str, int] = {}
    for item in errors + warnings:
        by_problem[item["problem"]] = by_problem.get(item["problem"], 0) + 1
    return {"file": str(sch), "ready": not errors, "parts": len(comps), "check": summary,
            "by_problem": by_problem, "errors": errors, "warnings": warnings}
