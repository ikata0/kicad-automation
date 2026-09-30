"""Load the same canonical agent assets from editable or packaged installations."""

from __future__ import annotations

from importlib import resources
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .env import KatError

SKILLS = ("kicad-design", "kicad-schematic", "kicad-layout")
REQUIRED = (
    "AGENTS.template.md",
    "kicad-design/SKILL.md",
    "kicad-design/references/setup.md",
    "kicad-design/references/blocks.md",
    "kicad-schematic/SKILL.md",
    "kicad-schematic/references/hierarchy.md",
    "kicad-schematic/references/stamping.md",
    "kicad-schematic/references/troubleshooting.md",
    "kicad-layout/SKILL.md",
    "kicad-layout/references/stamping.md",
    "kicad-layout/references/routing.md",
)


def load() -> tuple[dict[str, bytes], str]:
    package = resources.files("kat")
    # Editable installs refer directly to src/kat; do not search the caller's tree.
    source = Path(__file__).resolve().parents[2]
    if (source / "src" / "kat").resolve() == Path(__file__).resolve().parent and (
            source / "pyproject.toml").is_file():
        root = source / "skills"
    else:
        root = package.joinpath("_agent_assets")
    assets = {}
    for name in REQUIRED:
        item = root.joinpath(*name.split("/"))
        if not item.is_file():
            raise KatError(f"kat agent assets are incomplete: {name}; reinstall kat from a complete wheel")
        data = item.read_bytes().replace(b"\r\n", b"\n")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise KatError(f"kat agent asset is not UTF-8: {name}") from e
        assets[name] = data
    try:
        kat_version = version("kat")
    except PackageNotFoundError as e:
        raise KatError("kat distribution metadata is missing; install kat before initializing") from e
    return assets, kat_version
