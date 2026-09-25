"""Project-level operations: create a project with an empty sheet."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import env
from .schematic import Schematic


def new(path: Path | str, paper: str = "A4", force: bool = False) -> dict:
    """``DIR`` (project named after it) or ``DIR/NAME.kicad_sch``: KiCad's default project
    template plus an empty schematic."""
    target = Path(path)
    d, name = (target, target.name) if target.suffix == "" else (target.parent, target.stem)
    d.mkdir(parents=True, exist_ok=True)
    pro, sch = d / f"{name}.kicad_pro", d / f"{name}.kicad_sch"
    if sch.exists() and not force:
        raise env.KatError(f"{sch} exists (use --force to overwrite)")
    tmpl = (env.stock_symbol_dir() or Path("/nonexistent")).parent / "template" / "kicad.kicad_pro"
    if tmpl.exists():
        shutil.copy(tmpl, pro)
    elif not pro.exists():
        pro.write_text('{"meta": {"filename": "%s.kicad_pro", "version": 3}}\n' % name)
    Schematic.create(sch, paper)
    return {"project": str(pro), "schematic": str(sch)}
