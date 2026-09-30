"""Agent bootstrap must be portable, self-contained and safe without KiCad."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import _agent_assets, api, bootstrap, cli, env


@pytest.fixture(autouse=True)
def no_kicad(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("init must not discover or run KiCad")
    monkeypatch.setattr(env, "kicad_cli", forbidden)
    monkeypatch.setattr(env, "run_cli", forbidden)


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_fresh_current_directory_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = kat.init()
    assert set(out) == {"root", "kat_version", "skills", "created", "updated", "unchanged"}
    assert out["root"] == str(tmp_path.resolve())
    assert out["skills"] == list(_agent_assets.SKILLS)
    assert len(out["created"]) == len(_agent_assets.REQUIRED) + 1
    assert not out["updated"] and not out["unchanged"]
    before = tree(tmp_path)
    assert all(b"\r\n" not in data for data in before.values())
    mtimes = {p: (tmp_path / p).stat().st_mtime_ns for p in before}
    second = kat.init()
    assert not second["created"] and not second["updated"]
    assert second["unchanged"] == sorted(before)
    assert tree(tmp_path) == before
    assert {p: (tmp_path / p).stat().st_mtime_ns for p in before} == mtimes
    assert not list(tmp_path.glob("*.kicad*")) and not list(tmp_path.glob("*REPORT*"))
    assert not (tmp_path / ".git").exists()


def test_references_names_and_manifest(tmp_path):
    kat.init(tmp_path)
    manifest = json.loads((tmp_path / bootstrap._MANIFEST).read_bytes())
    assert bootstrap._valid_manifest(json.dumps(manifest).encode())
    assert set(manifest["files"]) == set(tree(tmp_path)) - {bootstrap._MANIFEST}
    for name in _agent_assets.SKILLS:
        skill = tmp_path / ".agents" / "skills" / name / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        assert text.startswith(f"---\nname: {name}\ndescription: ")
        assert "SPEC" not in text and "README" not in text
    for p in tmp_path.rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
            if "://" not in link and not link.startswith("#"):
                assert (p.parent / link.split("#")[0]).is_file(), (p, link)
        rel = p.relative_to(tmp_path).as_posix()
        data = p.read_bytes()
        if rel == "AGENTS.md":
            _, data, conflict = bootstrap._agents(data, _agent_assets.load()[0]["AGENTS.template.md"])
            assert not conflict
        assert manifest["files"][rel] == hashlib.sha256(data).hexdigest()


def test_new_nested_directory_with_unicode_and_spaces(tmp_path):
    target = tmp_path / "new directory" / "caf\u00e9"
    result = kat.init(target)
    assert result["root"] == str(target.resolve()) and (target / "AGENTS.md").exists()
    assert list(tmp_path.iterdir()) == [target.parent]


@pytest.mark.parametrize("content", [b"# Project\nKeep this.\n", b"# Project\r\nKeep this.\r\n",
                                   b"\xef\xbb\xbf# Project\r\nKeep this.", b""])
def test_append_preserves_existing_instructions_and_newlines(tmp_path, content):
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(content)
    extra = tmp_path / "notes.txt"
    extra.write_bytes(b"unrelated")
    kat.init(tmp_path)
    after = agents.read_bytes()
    assert after.startswith(content)
    if b"\r\n" in content:
        section = after[after.index(bootstrap._START.encode()):]
        assert b"\n" not in section.replace(b"\r\n", b"")
    assert extra.read_bytes() == b"unrelated"
    assert not kat.init(tmp_path)["updated"]


def test_external_agents_changes_do_not_conflict(tmp_path):
    kat.init(tmp_path)
    agents = tmp_path / "AGENTS.md"
    content = b"# Local guidance\n" + agents.read_bytes() + b"\nTail stays.\n"
    agents.write_bytes(content)
    assert not kat.init(tmp_path)["updated"]
    assert agents.read_bytes() == content


def test_conflicts_aggregate_before_any_write_and_force_preserves_unmanaged(tmp_path):
    kat.init(tmp_path)
    skill = tmp_path / ".agents" / "skills" / "kicad-design" / "SKILL.md"
    skill.write_bytes(b"local custom skill")
    agents = tmp_path / "AGENTS.md"
    text = agents.read_text(encoding="utf-8").replace("## KiCad development", "## Local changes")
    agents.write_bytes(("# Prefix\n" + text + "\n# Suffix\n").encode())
    ref = tmp_path / ".agents" / "skills" / "kicad-layout" / "references" / "routing.md"
    ref.unlink()
    extra = tmp_path / ".agents" / "skills" / "kicad-layout" / "obsolete.md"
    extra.write_bytes(b"preserve extra")
    before = tree(tmp_path)
    with pytest.raises(kat.KatError, match="init conflicts") as exc:
        kat.init(tmp_path)
    assert "kicad-design/SKILL.md" in str(exc.value) and "AGENTS.md" in str(exc.value)
    assert tree(tmp_path) == before and not ref.exists()
    out = kat.init(tmp_path, force=True)
    assert "AGENTS.md" in out["updated"] and ref.exists()
    assert agents.read_bytes().startswith(b"# Prefix\n")
    assert agents.read_bytes().endswith(b"\n# Suffix\n")
    assert extra.read_bytes() == b"preserve extra"
    assert not kat.init(tmp_path)["updated"]


@pytest.mark.parametrize("content", [
    "<!-- kat:init:start -->\nmissing end",
    "<!-- kat:init:end -->\n<!-- kat:init:start -->",
    "<!-- kat:init:start -->\n<!-- kat:init:end -->\n<!-- kat:init:start -->\n<!-- kat:init:end -->",
    "inline <!-- kat:init:start -->\n<!-- kat:init:end -->",
])
def test_malformed_markers_fail_even_with_force(tmp_path, content):
    (tmp_path / "AGENTS.md").write_text(content, encoding="utf-8")
    before = tree(tmp_path)
    with pytest.raises(kat.KatError, match="markers"):
        kat.init(tmp_path, force=True)
    assert tree(tmp_path) == before


def test_non_utf8_agents_fails_explicitly(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"\xff")
    with pytest.raises(kat.KatError, match="not UTF-8"):
        kat.init(tmp_path, force=True)
    assert tree(tmp_path) == {"AGENTS.md": b"\xff"}


@pytest.mark.parametrize("data", [b"not json", b"[]", b'{"schema": 99}', b'{"schema": true}'])
def test_invalid_manifest_requires_force(tmp_path, data):
    kat.init(tmp_path)
    manifest = tmp_path / bootstrap._MANIFEST
    manifest.write_bytes(data)
    before = tree(tmp_path)
    with pytest.raises(kat.KatError, match="kat-init.json"):
        kat.init(tmp_path)
    assert tree(tmp_path) == before
    kat.init(tmp_path, force=True)
    assert bootstrap._valid_manifest(manifest.read_bytes())


def test_version_change_requires_explicit_update(tmp_path, monkeypatch):
    kat.init(tmp_path)
    assets, _ = _agent_assets.load()
    monkeypatch.setattr(_agent_assets, "load", lambda: (assets, "9.9.9"))
    with pytest.raises(kat.KatError, match="kat-init.json"):
        kat.init(tmp_path)
    assert kat.init(tmp_path, force=True)["kat_version"] == "9.9.9"


@pytest.mark.parametrize("relative", [".agents", ".agents/skills", "AGENTS.md",
                                    ".agents/skills/kicad-design/SKILL.md"])
def test_structural_collisions(tmp_path, relative):
    target = tmp_path / relative
    if relative.endswith(".md"):
        target.mkdir(parents=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not a directory")
    before = tree(tmp_path)
    with pytest.raises(kat.KatError, match="needs a"):
        kat.init(tmp_path, force=True)
    assert tree(tmp_path) == before


def test_target_file_is_not_directory(tmp_path):
    target = tmp_path / "file"
    target.write_bytes(b"keep")
    with pytest.raises(kat.KatError, match="not a directory"):
        kat.init(target)
    assert target.read_bytes() == b"keep"


def test_redirected_managed_paths_are_refused(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / ".agents"
    if sys.platform == "win32":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                              capture_output=True, text=True)
        if proc.returncode:
            pytest.skip(f"junction creation unavailable: {proc.stderr}")
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(kat.KatError, match="redirected"):
            kat.init(root, force=True)
        assert not list(outside.iterdir())
    finally:
        if sys.platform == "win32":
            link.rmdir()
        else:
            link.unlink()


def test_write_failure_rolls_back_and_removes_new_directories(tmp_path, monkeypatch):
    root = tmp_path / "new" / "nested"
    replace = bootstrap.os.replace
    calls = 0
    def fail_second(source, dest):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        return replace(source, dest)
    monkeypatch.setattr(bootstrap.os, "replace", fail_second)
    with pytest.raises(OSError, match="injected"):
        kat.init(root)
    assert not list(tmp_path.iterdir())


def test_force_failure_restores_existing_content(tmp_path, monkeypatch):
    kat.init(tmp_path)
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(agents.read_bytes().replace(b"## KiCad", b"## Custom"))
    manifest = tmp_path / bootstrap._MANIFEST
    manifest.write_bytes(b"bad metadata")
    before = tree(tmp_path)
    replace = bootstrap.os.replace
    calls = 0
    def fail_second(source, dest):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected update failure")
        return replace(source, dest)
    monkeypatch.setattr(bootstrap.os, "replace", fail_second)
    with pytest.raises(OSError, match="injected"):
        kat.init(tmp_path, force=True)
    assert tree(tmp_path) == before


def test_rollback_failure_reports_and_retains_original(tmp_path, monkeypatch):
    kat.init(tmp_path)
    manifest = tmp_path / bootstrap._MANIFEST
    manifest.write_bytes(b"original metadata")
    agents = tmp_path / "AGENTS.md"
    agents.write_bytes(agents.read_bytes().replace(b"## KiCad", b"## Custom"))
    replace = bootstrap.os.replace
    calls = 0
    def fail_after_first(source, dest):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("injected update/rollback failure")
        return replace(source, dest)
    monkeypatch.setattr(bootstrap.os, "replace", fail_after_first)
    with pytest.raises(kat.KatError, match="rollback failed") as exc:
        kat.init(tmp_path, force=True)
    assert "original retained at" in str(exc.value)
    backups = list((tmp_path / ".agents").glob(".kat-init-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"original metadata"
    backups[0].unlink()


def test_cli_json_errors_no_journal_and_api(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "existing.kicad_pro").write_bytes(b"{}")
    assert cli.main(["init"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert json.loads(captured.out)["root"] == str(tmp_path.resolve())
    assert not list(tmp_path.glob("*REPORT*"))
    (tmp_path / "AGENTS.md").write_bytes(
        (tmp_path / "AGENTS.md").read_bytes().replace(b"## KiCad", b"## Custom"))
    assert cli.main(["init"]) == 2
    assert set(json.loads(capsys.readouterr().out)) == {"error"}
    assert cli.main(["init", "--force"]) == 0
    assert "AGENTS.md" in json.loads(capsys.readouterr().out)["updated"]
    assert api.lookup("init")["name"] == "kat.init"
    assert "kat_version" in api.lookup("init")["doc"]


def test_incomplete_editable_assets_are_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setattr(_agent_assets, "__file__", str(tmp_path / "src" / "kat" / "_agent_assets.py"))
    (tmp_path / "src" / "kat").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_bytes(b"")
    with pytest.raises(kat.KatError, match="assets are incomplete"):
        _agent_assets.load()


def test_packaged_assets_do_not_depend_on_checkout_or_cwd(monkeypatch, tmp_path):
    assets, version = _agent_assets.load()
    package = tmp_path / "installed" / "kat"
    for name, data in assets.items():
        target = package / "_agent_assets" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    monkeypatch.setattr(_agent_assets, "__file__", str(package / "_agent_assets.py"))
    monkeypatch.setattr(_agent_assets.resources, "files", lambda name: package)
    monkeypatch.chdir(tmp_path)
    assert _agent_assets.load() == (assets, version)


def test_missing_distribution_metadata_is_actionable(monkeypatch):
    def missing(name):
        raise _agent_assets.PackageNotFoundError(name)
    monkeypatch.setattr(_agent_assets, "version", missing)
    with pytest.raises(kat.KatError, match="distribution metadata is missing"):
        _agent_assets.load()


def test_cli_reports_io_errors(tmp_path, monkeypatch, capsys):
    def denied(*args):
        raise PermissionError("deployment permission denied")
    monkeypatch.setattr(bootstrap, "_commit", denied)
    assert cli.main(["init", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out) == {"error": "deployment permission denied"}
    assert not list(tmp_path.iterdir())
