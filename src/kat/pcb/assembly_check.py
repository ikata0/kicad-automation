"""Assembly-data check (FR-060): KiCad's own BOM and position exports, judged for release.

DRC and schematic/board parity say nothing about what an assembler receives. ``check``
exports the BOM (ungrouped, from the schematic) and the position file (from the board) the
way ``kat pcb fab`` does, for the base or one KiCad 10 assembly variant, and reports per
reference:

- ``no_mpn``: a fitted BOM item (in the BOM, not DNP) whose MPN is blank or a
  placeholder (``-``, ``~``, ``n/a``, ``tbd``, ...);
- ``missing_field``: another required field empty (``require``, and the fabrication
  profile's ``bom_fields``);
- ``mechanical_in_bom`` / ``mechanical_in_pos``: a part declared mechanical (field
  ``Mechanical`` = yes) without its BOM or position-file exclusion; a missing MPN alone
  never makes a part mechanical;
- ``pos_not_in_bom``: placed by the machine but bought by nobody (e.g. a board-only
  mounting hole without ``exclude_from_pos_files``); ``dnp_in_pos``: DNP in the BOM but
  placed; ``bom_not_in_pos``: bought, fitted, not excluded from the position file, yet not
  in it (no footprint on the board);
- ``flag_mismatch``: symbol and footprint disagree on DNP, BOM or position-file exclusion;
- ``dnp_only_in_value``: a fitted part whose value says DNP (that is no DNP flag).

Flags come from KiCad's netlist (symbol side, with the variant's overrides) and the board
(footprint attributes and ``(variant ...)`` children); KiCad's BOM ``${DNP}`` column is
localized, so only its being non-empty counts.
"""

from __future__ import annotations

import csv
import re
import tempfile
from pathlib import Path

from .. import env, gate, netlist
from ..sexpr import find, find_all
from ..syncer import natural_key
from .board import Board

FLAGS = ("dnp", "exclude_from_bom", "exclude_from_pos_files")
MECHANICAL = "Mechanical"
PLACEHOLDERS = {"", "-", "--", "~", "?", "n/a", "na", "none", "tbd", "tba", "x", "xxx", "dnp", "unknown"}


def _truthy(v: str) -> bool:
    return v.strip().lower() in netlist._YES - {""}


def symbol_flags(comp: list, variant: str | None = None) -> dict[str, bool]:
    """A netlist ``(comp ...)``'s assembly flags (``dnp``, ``exclude_from_bom``,
    ``exclude_from_pos_files``), with ``variant``'s overrides applied when given."""
    props = {str(find(p, "name")[1]) for p in find_all(comp, "property")}
    out = {k: k in props for k in FLAGS}
    if variant:
        for v in find_all(find(comp, "variants") or [], "variant"):
            if str(find(v, "name")[1]) != variant:
                continue
            for p in find_all(v, "property"):
                name = str(find(p, "name")[1])
                if name in out:
                    out[name] = str(find(p, "value")[1]) not in ("0", "")
    return out


def symbol_fields(comp: list, variant: str | None = None) -> dict[str, str]:
    """A netlist ``(comp ...)``'s fields (Value, Footprint and custom ones), with
    ``variant``'s field overrides applied when given."""
    out = {str(find(f, "name")[1]): (str(f[2]) if len(f) > 2 else "")
           for f in find_all(find(comp, "fields") or [], "field")}
    val = find(comp, "value")
    out["Value"] = str(val[1]) if val and len(val) > 1 else ""
    if variant:
        for v in find_all(find(comp, "variants") or [], "variant"):
            if str(find(v, "name")[1]) == variant:
                for f in find_all(find(v, "fields") or [], "field"):
                    out[str(find(f, "name")[1])] = str(f[2]) if len(f) > 2 else ""
    return out


def _bool_token(node: list) -> bool:
    return len(node) < 2 or str(node[1]).lower() in ("yes", "true")


def footprint_flags(node: list, variant: str | None = None) -> dict[str, bool]:
    """A board footprint's assembly flags from its ``(attr ...)``, with the ``(variant ...)``
    child of ``variant`` applied when given."""
    attr = find(node, "attr")
    tokens = {str(t) for t in (attr or [])[1:]}
    out = {k: k in tokens for k in FLAGS}
    if variant:
        for v in find_all(node, "variant"):
            if str(find(v, "name")[1]) != variant:
                continue
            for k in FLAGS:
                t = find(v, k)
                if t is not None:
                    out[k] = _bool_token(t)
    return out


def _csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def export_bom(sch: Path, fields: list[str], variant: str | None = None) -> list[dict[str, str]]:
    """KiCad's ungrouped BOM of ``sch`` (one row per reference, parts excluded from the BOM
    left out), with the columns ``Reference``, ``fields`` and ``DNP`` (non-empty = DNP)."""
    cols = ["Reference", *[f for f in fields if f != "Reference"]]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bom.csv"
        env.run_cli("sch", "export", "bom", "--fields", ",".join(cols + ["${DNP}"]),
                    "--labels", ",".join(cols + ["DNP"]), "--group-by", "",
                    *(["--variant", variant] if variant else []), "-o", str(out), str(sch))
        return _csv(out)


def export_pos(pcb: Path, variant: str | None = None, exclude_dnp: bool = True) -> list[str]:
    """References in KiCad's position file of ``pcb`` (both sides, as ``kat pcb fab`` writes
    it: DNP parts left out unless ``exclude_dnp`` is False)."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "pos.csv"
        env.run_cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
                    *(["--exclude-dnp"] if exclude_dnp else []),
                    *(["--variant", variant] if variant else []), "-o", str(out), str(pcb))
        return [r["Ref"] for r in _csv(out)]


def check(pcb: Path | str, sch: Path | str | None = None, variant: str | None = None,
          require: tuple[str, ...] | list[str] = ("MPN",)) -> dict:
    """Check the assembly data of ``pcb`` (schematic ``sch``, default next to it) for the
    base or the KiCad 10 assembly ``variant``. ``require``: fields every fitted BOM item
    needs (MPN placeholders count as empty); the fabrication profile's ``bom_fields`` are
    added. Returns ``ok``, ``errors`` [{ref, problem, detail}], ``counts`` per problem,
    and the numbers of ``bom`` rows, ``fitted`` BOM items and ``placed`` positions."""
    from . import fabprofile
    pcb = Path(pcb)
    sch = Path(sch) if sch else pcb.with_suffix(".kicad_sch")
    if not sch.exists():
        raise env.KatError(f"no schematic {sch.name} next to the board (give sch)")
    b = Board(pcb)
    names = {str(find(v, "name")[1]) for v in find_all(find(b.tree, "variants") or [], "variant")}
    if variant and variant not in names:
        raise env.KatError(f"the board has no variant {variant!r} ({', '.join(sorted(names)) or 'none'}; "
                           "kat pcb sync writes the schematic's)")
    require = list(dict.fromkeys([*require, *fabprofile.read(pcb).get("bom_fields", [])]))
    errors: list[dict] = []

    def err(ref, problem, detail=""):
        errors.append({"ref": ref, "problem": problem, "detail": detail})

    tree = gate._export(sch)
    comps = {str(find(c, "ref")[1]): c for c in find_all(find(tree, "components") or [], "comp")}
    sym = {r: symbol_flags(c, variant) for r, c in comps.items()}
    fields = {r: symbol_fields(c, variant) for r, c in comps.items()}
    fps = {f.ref: f for f in b.footprints() if f.ref}
    fpf = {r: footprint_flags(f.node, variant) for r, f in fps.items()}

    rows = export_bom(sch, ["Value", "Footprint", *require], variant)
    bom = {r["Reference"]: r for r in rows}
    fitted = {r for r, row in bom.items() if not row.get("DNP", "").strip()}
    placed = set(export_pos(pcb, variant))

    for ref in sorted(fitted, key=natural_key):
        row = bom[ref]
        for name in require:
            v = row.get(name, "").strip()
            if name == "MPN" and v.lower() in PLACEHOLDERS:
                err(ref, "no_mpn", f"MPN {v!r}" if v else "MPN empty")
            elif name != "MPN" and not v:
                err(ref, "missing_field", name)
        if re.search(r"(?<![A-Za-z])DNP(?![A-Za-z])", row.get("Value", ""), re.I):
            err(ref, "dnp_only_in_value", f"value {row['Value']!r}: set the DNP flag instead")
    for ref in sorted(comps, key=natural_key):
        if not _truthy(fields[ref].get(MECHANICAL, "")):
            continue
        if ref in bom:
            err(ref, "mechanical_in_bom", "a mechanical part needs in_bom no")
        if ref in placed:
            err(ref, "mechanical_in_pos", "a mechanical part needs exclude_from_pos_files")
    for ref in sorted(placed - fitted, key=natural_key):
        if ref in bom:
            err(ref, "dnp_in_pos", "DNP in the BOM, yet in the position file (footprint not DNP)")
        else:
            err(ref, "pos_not_in_bom", "placed but not bought: exclude it from the position file, "
                                       "or put it in the BOM")
    for ref in sorted(fitted - placed, key=natural_key):
        if ref in fpf and fpf[ref]["exclude_from_pos_files"]:
            continue                                    # declared: hand-fitted or not placed
        err(ref, "bom_not_in_pos", "no footprint on the board" if ref not in fps else
            "fitted and in the BOM but missing from the position file")
    for ref in sorted(set(sym) & set(fpf), key=natural_key):
        diff = [k for k in FLAGS if sym[ref][k] != fpf[ref][k]]
        if diff:
            err(ref, "flag_mismatch", ", ".join(f"{k}: symbol {sym[ref][k]}, footprint {fpf[ref][k]}"
                                                for k in diff) + " (kat pcb sync)")
    counts: dict[str, int] = {}
    for e in errors:
        counts[e["problem"]] = counts.get(e["problem"], 0) + 1
    return {"board": str(pcb), "variant": variant, "ok": not errors, "errors": errors, "counts": counts,
            "bom": len(bom), "fitted": len(fitted), "placed": len(placed), "require": require}
