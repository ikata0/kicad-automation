"""Locate kicad-cli and the KiCad symbol and footprint libraries on Linux, macOS and Windows."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
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


def _windows_kicad_candidates() -> Iterator[str]:
    import winreg

    key_path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\KiCad {KICAD_MAJOR}"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(root, key_path, 0, winreg.KEY_READ | view) as key:
                    for name in ("InstallLocation", "DisplayIcon"):
                        try:
                            value, _ = winreg.QueryValueEx(key, name)
                        except FileNotFoundError:
                            continue
                        if not isinstance(value, str) or not value.strip():
                            continue
                        value = os.path.expandvars(value.strip())
                        if name == "InstallLocation":
                            folder = Path(value.strip('"')) / "bin"
                        else:
                            icon = re.sub(r",\s*-?\d+$", "", value).strip('"')
                            folder = Path(icon).parent
                        yield os.path.normpath(folder / "kicad-cli.exe")
            except FileNotFoundError:
                continue
            except OSError as e:
                raise KatError(f"cannot read KiCad's Windows registration: {e}; "
                               "put kicad-cli on PATH or set KICAD_CLI") from e
    for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if base := os.environ.get(var):
            for prefix in (("Programs", "KiCad"), ("KiCad",)):
                yield os.path.join(base, *prefix, KICAD_MAJOR, "bin", "kicad-cli.exe")


def kicad_cli() -> str:
    """Return the kicad-cli executable path. Respect ``KICAD_CLI``, then PATH;
    otherwise discover KiCad 10 through Windows installer registration/default
    folders or the macOS app bundle. Cache discoveries in this process's
    ``KICAD_CLI`` environment variable, inherited by child processes; the calling
    shell's environment is not changed."""
    if env := os.environ.get("KICAD_CLI"):
        return env
    if found := shutil.which("kicad-cli"):
        os.environ["KICAD_CLI"] = found
        return found
    candidates: Iterable[str] = ()
    if sys.platform == "win32":
        candidates = _windows_kicad_candidates()
    elif sys.platform == "darwin":
        candidates = ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"]
    for c in candidates:
        if os.path.isfile(c):
            os.environ["KICAD_CLI"] = c
            return c
    raise KatError("kicad-cli not found; install KiCad 10, put kicad-cli on PATH or set KICAD_CLI")


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cli = kicad_cli()
    # English output, so messages are stable regardless of the user's locale.
    env = dict(os.environ, LANG="C", LC_ALL="C", LANGUAGE="en")
    proc = subprocess.run([cli, *args], capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise KatError(f"kicad-cli {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def _config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "kicad" / KICAD_MAJOR
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "kicad" / KICAD_MAJOR
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kicad" / KICAD_MAJOR


# stock library directory -> KiCad path variable stem (KICAD10_SYMBOL_DIR, ...)
_STOCK = {"symbols": "SYMBOL", "footprints": "FOOTPRINT", "3dmodels": "3DMODEL"}


def stock_dir(kind: str) -> Path | None:
    """KiCad's installed ``symbols``, ``footprints`` or ``3dmodels`` directory."""
    stem = _STOCK[kind]
    for var in (f"KICAD10_{stem}_DIR", f"KICAD9_{stem}_DIR", f"KICAD_{stem}_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var])
    cands = [f"/usr/share/kicad/{kind}", f"/usr/local/share/kicad/{kind}",
             f"/Applications/KiCad/KiCad.app/Contents/SharedSupport/{kind}"]
    if sys.platform == "win32":
        cli = Path(kicad_cli())
        cands.insert(0, str(cli.parent.parent / "share" / "kicad" / kind))
    for c in cands:
        if os.path.isdir(c):
            return Path(c)
    return None


def stock_symbol_dir() -> Path | None:
    return stock_dir("symbols")


def expand(uri: str, project_dir: Path | None) -> str:
    """Resolve ${KIPRJMOD}, ${KICAD10_FOOTPRINT_DIR}, ... in a library or model path."""
    vars_ = {"KIPRJMOD": str(project_dir or ".")}
    for kind, stem in _STOCK.items():
        d = str(stock_dir(kind) or "")
        vars_.update({f"KICAD10_{stem}_DIR": d, f"KICAD9_{stem}_DIR": d, f"KICAD_{stem}_DIR": d})
    vars_.update(os.environ)
    return re.sub(r"\$\{(\w+)\}", lambda m: vars_.get(m.group(1), m.group(0)), uri)


def _read_lib_table(path: Path, project_dir: Path | None, depth: int = 0) -> dict[str, Path]:
    """Parse a sym-lib-table or fp-lib-table; KiCad 10 tables may nest other tables
    (type "Table")."""
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
        target = Path(expand(str(uri[1]), project_dir))
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


def footprint_libraries(project_dir: Path | None = None) -> dict[str, Path]:
    """Nickname -> .pretty directory, from global + project fp-lib-table, else the stock dir."""
    libs = _read_lib_table(_config_dir() / "fp-lib-table", project_dir)
    if not libs and (fp := stock_dir("footprints")):
        libs = {p.stem: p for p in sorted(fp.glob("*.pretty"))}
    if project_dir:
        libs.update(_read_lib_table(project_dir / "fp-lib-table", project_dir))
    return {k: v for k, v in libs.items() if v.exists()}


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (
        os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else str(Path.home() / ".cache"))
    d = Path(base) / "kat"
    d.mkdir(parents=True, exist_ok=True)
    return d
