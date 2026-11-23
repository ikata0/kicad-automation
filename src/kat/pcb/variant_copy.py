"""A copy of a board with one assembly variant made its base (``kat pcb variant-board``).

For an assembler, a design-review tool or a fab flow that does not read KiCad 10 variants:
the copy is an ordinary board whose DNP, BOM / position-file exclusions and field values are
those of the variant, with no variants left in it. Copper, placement and stamp records are
copied unchanged; the source is not touched.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..env import KatError
from ..sexpr import dumps, find, head
from .board import Board, normalize
from .fabrication import board_variants
from .stamping import stamps_file
from .syncer import VARIANT_FLAGS, _set_attr_flag, footprint_variants

# project files that go along (named after the board, like the board's own)
_PROJECT_FILES = (".kicad_pro", ".kicad_dru")
_TABLES = ("fp-lib-table", "sym-lib-table", "design-block-lib-table")
_PRJ = "${KIPRJMOD}"


def default_dir(pcb: Path, variant: str) -> Path:
    """``<board dir>/variants/<variant>/``: where ``variant_board`` writes by default."""
    from .fabrication import variant_stem
    return Path(pcb).resolve().parent / "variants" / variant_stem("", variant).lstrip("-")


def _rebase(text: str, rel: str) -> str:
    """``${KIPRJMOD}/x`` -> ``${KIPRJMOD}/<rel>/x``: the same file, seen from the copy."""
    return text if rel == "." else text.replace(_PRJ + "/", f"{_PRJ}/{rel}/")


def variant_board(pcb: Path | str, variant: str, out: Path | str | None = None) -> dict:
    """Write a copy of ``pcb`` into ``out`` (default ``<board dir>/variants/<variant>/``)
    with ``variant`` applied as the base: per footprint its dnp / exclude_from_bom /
    exclude_from_pos_files flags and field values, then every variant removed. The board's
    ``.kicad_pro`` / ``.kicad_dru`` and library tables are copied along; ``${KIPRJMOD}`` paths
    (library tables, 3D models) are rewritten to point at the source project's files.
    Returns {board, variant, applied: {ref: {key: value}}, files}."""
    pcb = Path(pcb).resolve()
    b = Board(pcb)
    names = board_variants(b)
    if variant not in names:
        raise KatError(f"{pcb.name} has no assembly variant {variant!r} "
                       f"(it has: {', '.join(names) or 'none'}; kat pcb sync writes them)")
    out = Path(out).resolve() if out else default_dir(pcb, variant)
    if out == pcb.parent:
        raise KatError("variant-board: --out must not be the board's own folder")
    out.mkdir(parents=True, exist_ok=True)
    rel = Path(os.path.relpath(pcb.parent, out)).as_posix()

    applied: dict[str, dict] = {}
    for f in b.footprints():
        vals = footprint_variants(f.node).get(variant, {})
        for k, v in vals.items():
            if k in VARIANT_FLAGS:
                _set_attr_flag(f.node, k, bool(v))
            else:
                f.set_field(k, v)
        if vals:
            applied[f.ref] = vals
        f.node[:] = [c for c in f.node if not (isinstance(c, list) and head(c) == "variant")]
    if (vs := find(b.tree, "variants")) is not None:
        b.tree.remove(vs)

    dst = out / pcb.name
    dst.write_text(_rebase(dumps(b.tree), rel) + "\n", encoding="utf-8")
    normalize(dst)                      # KiCad loads it and writes it its way
    files = [dst.name]
    if (legacy := stamps_file(pcb)).exists():   # stamp records of boards saved before they moved in
        (out / legacy.name).write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        files.append(legacy.name)
    for suffix in _PROJECT_FILES:
        src = pcb.with_suffix(suffix)
        if src.exists():
            (out / src.name).write_text(_rebase(src.read_text(encoding="utf-8"), rel), encoding="utf-8")
            files.append(src.name)
    for name in _TABLES:
        src = pcb.parent / name
        if src.exists():
            (out / name).write_text(_rebase(src.read_text(encoding="utf-8"), rel), encoding="utf-8")
            files.append(name)
    return {"board": str(dst), "variant": variant, "applied": applied, "files": files}

