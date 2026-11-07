"""Project-level operations: create a project with an empty sheet; the project's reference
prefixes."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import env, report
from .schematic import Schematic


def new(path: Path | str, paper: str = "A4", force: bool = False) -> dict:
    """``DIR`` (project named after it) or ``DIR/NAME.kicad_sch``: KiCad's default project
    template plus an empty schematic, and the design report ``NAME_REPORT.md``."""
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
        pro.write_text('{"meta": {"filename": "%s.kicad_pro", "version": 3}}\n' % name,
                       encoding="utf-8")
    Schematic.create(sch, paper)
    out = {"project": str(pro), "schematic": str(sch)}
    if report.enabled():
        out["report"] = str(report.ensure(d.resolve(), name))
    return out


def _settings(path: Path | str) -> Path | None:
    """``<project>.kat.json`` of the project that ``path`` (a folder, or a file in it) is in;
    None without a .kicad_pro there."""
    p = Path(path)
    d = p if p.is_dir() or p.suffix == "" else p.parent
    pro = sorted(d.glob("*.kicad_pro"))
    return d / f"{pro[0].stem}.kat.json" if pro else None


def prefixes(path: Path | str, mapping: dict[str, str] | None = None, clear: bool = False) -> dict[str, str]:
    """The project's reference prefixes: {library prefix: project prefix}, e.g. {"J": "CON"},
    stored in ``<project>.kat.json``. ``path``: the project folder or a file in it.
    ``mapping`` adds or changes entries (an empty value removes one), ``clear`` removes all.
    Used by ``kat lib make`` (unless ``--reference``) and by symbols kat adds itself; sync
    never renames, since the references of a SKiDL design come from its netlist (give the
    part ``ref_prefix=`` there), and ``kat check`` warns about those that do not follow it.
    Returns the map."""
    f = _settings(path)
    data = json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else {}
    have = {} if clear else dict(data.get("prefixes") or {})
    if mapping is None and not clear:
        return have
    if f is None:
        raise env.KatError(f"no KiCad project (.kicad_pro) at {path}")
    for k, v in (mapping or {}).items():
        if not re.fullmatch(r"[A-Za-z_]+", k) or (v and not re.fullmatch(r"[A-Za-z_]+", v)):
            raise env.KatError(f"prefix {k}={v}: letters (and _) only, as KiCad's annotation needs")
        if v:
            have[k] = v
        else:
            have.pop(k, None)
    data["prefixes"] = have
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return have


def ref_prefix(ref: str) -> str:
    """The letters before a reference's number (``CON12`` -> ``CON``)."""
    m = re.match(r"#?[A-Za-z_]+", ref)
    return m.group(0) if m else ""
