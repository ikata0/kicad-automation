"""Dependency discovery for pytest, without changing the calling shell."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from kat import env
from kat.pcb import router
from kat.sexpr import loads


class SetupError(Exception):
    """An installed dependency or an explicit override is unusable."""


@dataclass(frozen=True)
class Capability:
    available: bool
    detail: str


REQUIRED = ("kicad", "symbols", "footprints", "skidl", "render", "routing")


def probe(argv: list[str]) -> str:
    """Run a bounded dependency probe; preserve byte diagnostics on failure."""
    try:
        result = subprocess.run(argv, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SetupError(f"cannot probe {argv[0]}: {e}") from e
    if result.returncode:
        raise SetupError(f"{argv[0]} probe exited {result.returncode}: "
                         f"{result.stderr[-2000:]!r}")
    # Version strings are ASCII even when surrounding diagnostic bytes are not.
    return (result.stdout + result.stderr).decode("ascii", errors="backslashreplace")


def python_package(name: str) -> Capability:
    if importlib.util.find_spec(name) is None:
        return Capability(False, f"{name} not installed")
    version = probe([sys.executable, "-c",
                     f"import {name}; print('KAT_TEST_VERSION=' + "
                     f"str(getattr({name}, '__version__', 'installed')))"])
    match = re.search(r"^KAT_TEST_VERSION=(.+)$", version, re.MULTILINE)
    if not match:
        raise SetupError(f"{name} import probe produced no version result: {version[-2000:]}")
    return Capability(True, f"{name}: {match[1]}")


def libraries(kind: str) -> Capability:
    stem = env._STOCK[kind]
    overrides = (f"KICAD10_{stem}_DIR", f"KICAD9_{stem}_DIR", f"KICAD_{stem}_DIR")
    explicit = next((name for name in overrides if os.environ.get(name)), None)
    path = env.stock_dir(kind)
    if path is None or not path.is_dir():
        if explicit:
            raise SetupError(f"{explicit} points to a missing directory: {path}")
        return Capability(False, f"KiCad {kind} libraries not installed")
    files = sorted(path.glob("*.kicad_sym" if kind == "symbols" else "*.pretty/*.kicad_mod"))
    if not files:
        raise SetupError(f"KiCad {kind} directory contains no library files: {path}")
    # Verify a representative installed library, not just the directory's existence.
    sample = path / "Device.kicad_sym" if kind == "symbols" else files[0]
    if not sample.exists():
        sample = files[0]
    try:
        tree = loads(sample.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as e:
        raise SetupError(f"cannot read KiCad library {sample}: {e}") from e
    expected = "kicad_symbol_lib" if kind == "symbols" else "footprint"
    if not tree or str(tree[0]) != expected:
        raise SetupError(f"invalid KiCad library {sample}: expected {expected}")
    if explicit:
        os.environ[explicit] = str(path.resolve())
    os.environ[f"KICAD10_{stem}_DIR"] = str(path.resolve())
    return Capability(True, str(path.resolve()))


def routing(strict: bool) -> Capability:
    java = shutil.which("java")
    if java:
        version = probe([java, "-version"])
        match = re.search(r'version "(\d+)', version)
        if not match or int(match[1]) < router.JAVA_MIN:
            raise SetupError(f"routing requires Java {router.JAVA_MIN}+, found {version.strip()}")
    override = os.environ.get("KAT_FREEROUTING")
    if override:
        jar = Path(override).resolve()
        os.environ["KAT_FREEROUTING"] = str(jar)
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (
            os.environ.get("LOCALAPPDATA") if sys.platform == "win32"
            else str(Path.home() / ".cache"))
        jar = Path(base) / "kat" / f"freerouting-{router.VERSION}.jar"
    if not jar.is_file():
        if override:
            raise SetupError(f"KAT_FREEROUTING points to a missing jar: {jar}")
        return Capability(False, "Freerouting not installed; run `kat pcb route --install`"
                          + ("; Java 21+ also missing" if not java else ""))
    try:
        with zipfile.ZipFile(jar) as archive:
            manifest = archive.read("META-INF/MANIFEST.MF")
            main = re.search(rb"(?m)^Main-Class: ([^\r\n]+)", manifest)
            if not main:
                raise SetupError(f"Freerouting jar has no main class: {jar}")
            entry = main[1].decode("utf-8").replace(".", "/") + ".class"
            if not archive.read(entry).startswith(b"\xca\xfe\xba\xbe"):
                raise SetupError(f"Freerouting jar has an invalid main class: {jar}")
            if bad := archive.testzip():
                raise SetupError(f"corrupt Freerouting jar entry {bad}: {jar}")
        if strict and hashlib.sha256(jar.read_bytes()).hexdigest() != router.SHA256:
            raise SetupError(f"strict dependencies require pinned Freerouting "
                             f"{router.VERSION} with SHA-256 {router.SHA256}: {jar}")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as e:
        raise SetupError(f"cannot read Freerouting jar {jar}: {e}") from e
    if not java:
        return Capability(False, "Java 21+ not on PATH")
    return Capability(True, f"Java {match[1]}; {jar}")


def gui_capability() -> Capability:
    display = sys.platform != "win32" and bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    kipy = python_package("kipy")
    return Capability(display and bool(shutil.which("pcbnew")) and kipy.available,
                      "needs supported display, pcbnew and kicad-python")


def discover(strict: bool = False, gui: bool = False) -> dict[str, Capability]:
    """Discover test capabilities before test collection and SKiDL imports."""
    capabilities = {}
    try:
        cli = env.kicad_cli()
    except env.KatError as e:
        if str(e).startswith("kicad-cli not found;"):
            capabilities["kicad"] = Capability(False, str(e))
        else:
            raise SetupError(str(e)) from e
    else:
        resolved = shutil.which(cli)
        if resolved is None:
            raise SetupError(f"KICAD_CLI is not an executable: {cli}")
        cli = str(Path(resolved).resolve())
        version = probe([cli, "version"]).strip()
        if not re.match(r"10\.", version):
            raise SetupError(f"tests require KiCad 10, found {version!r} at {cli}")
        os.environ["KICAD_CLI"] = cli
        folder = str(Path(cli).parent)
        entries = os.environ.get("PATH", "").split(os.pathsep)
        if os.path.normcase(folder) not in {os.path.normcase(entry) for entry in entries}:
            os.environ["PATH"] = os.pathsep.join([folder, *entries])
        capabilities["kicad"] = Capability(True, f"{version}; {cli}")
    for kind in ("symbols", "footprints"):
        # Windows stock_dir calls kicad_cli, which cannot work without a CLI.
        explicit = any(os.environ.get(f"{prefix}_{env._STOCK[kind]}_DIR")
                       for prefix in ("KICAD10", "KICAD9", "KICAD"))
        if sys.platform == "win32" and not capabilities["kicad"].available and not explicit:
            capabilities[kind] = Capability(False, f"KiCad {kind} unavailable without KiCad")
        else:
            capabilities[kind] = libraries(kind)
    capabilities["skidl"] = python_package("skidl")
    capabilities["pymupdf"] = python_package("pymupdf")
    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        probe([rsvg, "--version"])
    capabilities["render"] = Capability(
        bool(rsvg) or capabilities["pymupdf"].available,
        str(rsvg) if rsvg else capabilities["pymupdf"].detail)
    capabilities["routing"] = routing(strict)
    if gui:
        capabilities["gui"] = gui_capability()
    else:
        capabilities["gui"] = Capability(False, "GUI tests are opt-in (-m kicad_gui)")
    return capabilities


def missing(capabilities: dict[str, Capability], names=REQUIRED) -> list[str]:
    return [f"{name}: {capabilities[name].detail}" for name in names
            if not capabilities[name].available]


def configure_windows_temp(root: Path, explicit: str | None, platform: str = sys.platform,
                           system_temp: Path | None = None) -> None:
    """Give pytest a same-drive temp parent, preserving its retention and locks."""
    if explicit or platform != "win32" or os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        return
    system_temp = system_temp or Path(tempfile.gettempdir())
    if PureWindowsPath(str(system_temp.resolve())).drive.lower() == (
        PureWindowsPath(str(root.resolve())).drive.lower()
    ):
        return
    venv = root / ".venv"
    parent = venv / "pytest-tmp" if (
        venv.is_dir() and venv.resolve().drive.lower() == root.resolve().drive.lower()
    ) else root / ".pytest-tmp"
    if parent.exists() and parent.resolve() != parent.absolute():
        raise SetupError(f"test temporary parent must not redirect elsewhere: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    # Pytest itself allocates numbered, locked run directories below this root.
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(parent.resolve())
