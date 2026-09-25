"""Locate kicad-cli and the KiCad symbol libraries on Linux, macOS and Windows."""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

KICAD_MAJOR = "10.0"


class KatError(Exception):
    """User-facing error; the CLI prints it as JSON and exits non-zero."""


def tidy(obj):
    """Round floats to 4 decimals (no 506.72999999999996) throughout a JSON-able result."""
    if isinstance(obj, float):
        return round(obj, 4) + 0.0
    if isinstance(obj, dict):
        return {k: tidy(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [tidy(v) for v in obj]
    return obj


def kicad_cli() -> str:
    if env := os.environ.get("KICAD_CLI"):
        return env
    if found := shutil.which("kicad-cli"):
        return found
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
            candidates += glob.glob(os.path.join(base, "Programs", "KiCad", "*", "bin", "kicad-cli.exe"))
            candidates += glob.glob(os.path.join(base, "KiCad", "*", "bin", "kicad-cli.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
    for c in sorted(candidates, reverse=True):
        if os.path.exists(c):
            return c
    raise KatError("kicad-cli not found; put it on PATH or set KICAD_CLI")


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    # English output, so messages are stable regardless of the user's locale.
    env = dict(os.environ, LANG="C", LC_ALL="C", LANGUAGE="en")
    proc = subprocess.run([kicad_cli(), *args], capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise KatError(f"kicad-cli {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "kicad" / KICAD_MAJOR
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "kicad" / KICAD_MAJOR
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad" / KICAD_MAJOR


def stock_symbol_dir() -> Path | None:
    for var in ("KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD_SYMBOL_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var])
    cands = ["/usr/share/kicad/symbols", "/usr/local/share/kicad/symbols",
             "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"]
    if sys.platform == "win32":
        cli = Path(kicad_cli())
        cands.insert(0, str(cli.parent.parent / "share" / "kicad" / "symbols"))
    for c in cands:
        if os.path.isdir(c):
            return Path(c)
    return None


def _expand(uri: str, project_dir: Path | None) -> str:
    sym = stock_symbol_dir()
    vars_ = {"KIPRJMOD": str(project_dir or "."), "KICAD10_SYMBOL_DIR": str(sym or ""),
             "KICAD9_SYMBOL_DIR": str(sym or ""), "KICAD_SYMBOL_DIR": str(sym or "")}
    vars_.update(os.environ)
    return re.sub(r"\$\{(\w+)\}", lambda m: vars_.get(m.group(1), m.group(0)), uri)


def _read_lib_table(path: Path, project_dir: Path | None, depth: int = 0) -> dict[str, Path]:
    """Parse a sym-lib-table; KiCad 10 tables may nest other tables (type "Table")."""
    out: dict[str, Path] = {}
    if not path.exists() or depth > 5:
        return out
    from .sexpr import find, loads
    for lib in loads(path.read_text())[1:]:
        if not isinstance(lib, list) or str(lib[0]) != "lib":
            continue
        name, uri, typ = (find(lib, k) for k in ("name", "uri", "type"))
        if not (name and uri):
            continue
        target = Path(_expand(str(uri[1]), project_dir))
        if typ and str(typ[1]) == "Table":
            out.update(_read_lib_table(target, project_dir, depth + 1))
        else:
            out[str(name[1])] = target
    return out


def symbol_libraries(project_dir: Path | None = None) -> dict[str, Path]:
    """Nickname -> .kicad_sym path, from global + project sym-lib-table, else the stock dir."""
    libs = _read_lib_table(_config_dir() / "sym-lib-table", project_dir)
    if not libs and (sym := stock_symbol_dir()):
        libs = {p.stem: p for p in sorted(sym.glob("*.kicad_sym"))}
    if project_dir:
        libs.update(_read_lib_table(project_dir / "sym-lib-table", project_dir))
    return {k: v for k, v in libs.items() if v.exists()}


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (
        os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else str(Path.home() / ".cache"))
    d = Path(base) / "kat"
    d.mkdir(parents=True, exist_ok=True)
    return d
