"""Project-local deployment of kat's agent instructions; no KiCad required."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath

from . import _agent_assets
from .env import KatError

_START = "<!-- kat:init:start -->"
_END = "<!-- kat:init:end -->"
_MANIFEST = ".agents/kat-init.json"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redirected(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _check_path(root: Path, relative: str) -> None:
    parts = PurePosixPath(relative).parts
    here = root
    for i, part in enumerate(parts):
        here = here / part
        if not os.path.lexists(here):
            continue
        if _redirected(here):
            raise KatError(f"init refuses redirected managed path: {relative} ({here})")
        if i < len(parts) - 1 and not here.is_dir():
            raise KatError(f"init needs a directory at {here}")
        if i == len(parts) - 1 and not here.is_file():
            raise KatError(f"init needs a regular file at {here}")


def _agents(before: bytes | None, template: bytes) -> tuple[bytes, bytes, bool]:
    text = (before or b"").decode("utf-8")
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    text = text.removeprefix(bom) if bom else text
    newline = "\r\n" if re.search(r"\r\n", text) else "\n"
    section = template.decode("utf-8").replace("\r\n", "\n").replace("\n", newline)
    starts = list(re.finditer(rf"(?m)^{re.escape(_START)}\r?$", text))
    ends = list(re.finditer(rf"(?m)^{re.escape(_END)}\r?$", text))
    if text.count(_START) != len(starts) or text.count(_END) != len(ends):
        raise KatError("AGENTS.md has malformed kat init markers; repair them before initializing")
    if not starts and not ends:
        gap = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        gap = gap.replace("\n", newline)
        return (bom + text + gap + section).encode("utf-8"), section.encode("utf-8"), False
    if len(starts) != 1 or len(ends) != 1 or starts[0].start() >= ends[0].start():
        raise KatError("AGENTS.md has duplicate or incomplete kat init markers; repair them first")
    start, end = starts[0].start(), ends[0].end()
    if text[end:end + 1] == "\n":
        end += 1
    old = text[start:end]
    # An end marker at EOF need not gain a newline on an otherwise identical rerun.
    if not old.endswith("\n"):
        section = section.rstrip("\r\n")
    result = (bom + text[:start] + section + text[end:]).encode("utf-8")
    return result, section.encode("utf-8"), old != section


def _valid_manifest(data: bytes) -> bool:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(value, dict) or set(value) != {"schema", "kat_version", "bundle_hash", "files"}:
        return False
    if type(value["schema"]) is not int or value["schema"] != 1:
        return False
    if not isinstance(value["kat_version"], str) or not value["kat_version"]:
        return False
    hashes = value["files"]
    if not isinstance(hashes, dict) or "AGENTS.md" not in hashes:
        return False
    for name, digest in hashes.items():
        if not isinstance(name, str) or "\\" in name:
            return False
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name != path.as_posix():
            return False
        if name != "AGENTS.md" and not (
                len(path.parts) >= 4 and path.parts[:2] == (".agents", "skills")
                and path.parts[2] in _agent_assets.SKILLS and path.suffix == ".md"):
            return False
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
    return isinstance(value["bundle_hash"], str) and bool(
        re.fullmatch(r"[0-9a-f]{64}", value["bundle_hash"]))


def _mkdir(path: Path, created: list[Path]) -> None:
    missing = []
    here = path
    while not here.exists():
        missing.append(here)
        here = here.parent
    for folder in reversed(missing):
        folder.mkdir()
        created.append(folder)


def _commit(root: Path, planned: dict[str, bytes], originals: dict[str, bytes | None]) -> None:
    folders: list[Path] = []
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    applied: list[str] = []
    retained: set[Path] = set()
    error: BaseException | None = None
    try:
        for name, data in planned.items():
            _check_path(root, name)
            dest = root.joinpath(*name.split("/"))
            _mkdir(dest.parent, folders)
            for content, table in ((data, staged), (originals[name], backups)):
                if content is None:
                    continue
                with tempfile.NamedTemporaryFile(prefix=".kat-init-", dir=dest.parent, delete=False) as fh:
                    table[name] = Path(fh.name)
                    fh.write(content)
                if originals[name] is not None:
                    os.chmod(table[name], stat.S_IMODE(dest.stat().st_mode))
        for name in planned:
            _check_path(root, name)
            dest = root.joinpath(*name.split("/"))
            current = dest.read_bytes() if dest.exists() else None
            if current != originals[name]:
                raise KatError(f"init destination changed during deployment: {name}; retry")
            os.replace(staged[name], dest)
            applied.append(name)
    except BaseException as exc:
        error = exc
        failures = []
        for name in reversed(applied):
            dest = root.joinpath(*name.split("/"))
            try:
                if name in backups:
                    os.replace(backups[name], dest)
                else:
                    dest.unlink()
            except OSError as rollback:
                if name in backups:
                    retained.add(backups[name])
                failures.append(f"{name}: {rollback}"
                                + (f" (original retained at {backups[name]})" if name in backups else ""))
        if failures:
            raise KatError(f"init failed ({exc}); rollback failed: {'; '.join(failures)}") from exc
        raise
    finally:
        cleanup = []
        for temp in (*staged.values(), *backups.values()):
            if temp in retained:
                continue
            try:
                temp.unlink(missing_ok=True)
            except OSError as exc:
                cleanup.append(f"{temp}: {exc}")
        if error is not None:
            for folder in reversed(folders):
                if any(folder in backup.parents for backup in retained):
                    continue
                try:
                    folder.rmdir()
                except OSError as exc:
                    cleanup.append(f"{folder}: {exc}")
        if cleanup:
            raise KatError(f"init cleanup failed: {'; '.join(cleanup)}"
                           + (f"; original failure: {error}" if error else "")) from error


def init(path: Path | str = ".", force: bool = False) -> dict:
    """Deploy agent skills, references and a managed AGENTS.md section into ``path``.

    ``path`` is a directory (default: current directory); missing directories are
    created. Identical files are unchanged; differing managed content requires
    ``force``. Unrelated instructions/files are preserved. No KiCad or design
    journal is required. Return a JSON-compatible dict with ``root``, ``kat_version``,
    ``skills`` and relative ``created``, ``updated`` and ``unchanged`` file lists.
    Provenance is recorded in .agents/kat-init.json. Conflicts raise KatError
    before writes; handled write failures restore previously changed files.
    """
    assets, kat_version = _agent_assets.load()
    root = Path(path).resolve()
    if root.exists() and not root.is_dir():
        raise KatError(f"init target is not a directory: {root}")
    expected = {f".agents/skills/{name}": data for name, data in assets.items()
                if name != "AGENTS.template.md"}
    originals: dict[str, bytes | None] = {}
    for name in (*expected, "AGENTS.md", _MANIFEST):
        _check_path(root, name)
        target = root.joinpath(*name.split("/"))
        originals[name] = target.read_bytes() if target.exists() else None
    try:
        agents, section, section_conflict = _agents(originals["AGENTS.md"], assets["AGENTS.template.md"])
    except UnicodeDecodeError as e:
        raise KatError("AGENTS.md is not UTF-8; convert it explicitly before initializing") from e
    expected["AGENTS.md"] = agents
    hashes = {name: _hash(section if name == "AGENTS.md" else data)
              for name, data in sorted(expected.items())}
    bundle_hash = _hash(json.dumps({name: _hash(data) for name, data in sorted(assets.items())},
                                  sort_keys=True).encode("utf-8"))
    manifest = {"schema": 1, "kat_version": kat_version, "bundle_hash": bundle_hash, "files": hashes}
    expected[_MANIFEST] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    conflicts = [name for name, data in expected.items()
                 if originals[name] is not None and originals[name] != data
                 and (name != "AGENTS.md" or section_conflict)]
    if originals[_MANIFEST] is not None and not _valid_manifest(originals[_MANIFEST]):
        if _MANIFEST not in conflicts:
            conflicts.append(_MANIFEST)
    if conflicts and not force:
        raise KatError(f"init conflicts: {', '.join(sorted(conflicts))}; "
                       "use --force to replace kat-managed content")
    result = {"root": str(root), "kat_version": kat_version, "skills": list(_agent_assets.SKILLS),
              "created": [], "updated": [], "unchanged": []}
    planned = {}
    for name, data in sorted(expected.items()):
        status = "created" if originals[name] is None else (
            "unchanged" if originals[name] == data else "updated")
        result[status].append(name)
        if status != "unchanged":
            planned[name] = data
    if planned:
        _commit(root, planned, originals)
    return result
