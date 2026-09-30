from contextlib import nullcontext
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from kat import env


@pytest.fixture
def registry(monkeypatch):
    reg = SimpleNamespace(
        HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, KEY_READ=4,
        KEY_WOW64_64KEY=8, KEY_WOW64_32KEY=16, entries={}, calls=[],
    )

    def open_key(root, path, reserved, access):
        reg.calls.append((root, path, access))
        view = access & (reg.KEY_WOW64_64KEY | reg.KEY_WOW64_32KEY)
        if (root, view) not in reg.entries:
            raise FileNotFoundError(path)
        return nullcontext(reg.entries[root, view])

    def query_value(key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], 1

    reg.OpenKey, reg.QueryValueEx = open_key, query_value
    monkeypatch.setitem(sys.modules, "winreg", reg)
    monkeypatch.setattr(env.sys, "platform", "win32")
    monkeypatch.setattr(env.shutil, "which", lambda name: None)
    monkeypatch.setenv("KICAD_CLI", "")
    for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    return reg


def executable(root):
    path = root / "bin" / "kicad-cli.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return str(path)


def test_explicit_override_bypasses_discovery(registry, monkeypatch):
    monkeypatch.setenv("KICAD_CLI", "chosen-kicad-cli")
    assert env.kicad_cli() == "chosen-kicad-cli"
    assert not registry.calls


def test_path_discovery_is_cached_and_override_can_change_it(registry, monkeypatch):
    calls = []
    monkeypatch.setattr(env.shutil, "which", lambda name: calls.append(name) or "path-kicad-cli")
    assert env.kicad_cli() == "path-kicad-cli"
    assert env.kicad_cli() == "path-kicad-cli"
    assert os.environ["KICAD_CLI"] == "path-kicad-cli"
    assert calls == ["kicad-cli"] and not registry.calls
    monkeypatch.setenv("KICAD_CLI", "different-kicad-cli")
    assert env.kicad_cli() == "different-kicad-cli"


@pytest.mark.parametrize("root", [1, 2])
@pytest.mark.parametrize("view", [8, 16])
def test_registered_custom_installation(registry, tmp_path, root, view):
    install = tmp_path / "custom KiCad installation"
    cli = executable(install)
    registry.entries[root, view] = {"InstallLocation": str(install)}
    assert env.kicad_cli() == cli
    assert os.environ["KICAD_CLI"] == cli
    assert all(call[1].endswith(r"Uninstall\KiCad 10.0") for call in registry.calls)
    calls = len(registry.calls)
    assert env.kicad_cli() == cli
    assert len(registry.calls) == calls


def test_current_user_installation_precedes_machine_installation(registry, tmp_path):
    user, machine = tmp_path / "user", tmp_path / "machine"
    cli = executable(user)
    executable(machine)
    registry.entries[1, 8] = {"InstallLocation": str(user)}
    registry.entries[2, 8] = {"InstallLocation": str(machine)}
    assert env.kicad_cli() == cli


def test_valid_user_registration_does_not_probe_denied_machine_keys(registry, tmp_path, monkeypatch):
    cli = executable(tmp_path)
    registry.entries[1, 8] = {"InstallLocation": str(tmp_path)}
    open_key = registry.OpenKey

    def denied_machine(root, *args):
        if root == registry.HKEY_LOCAL_MACHINE:
            raise PermissionError("access denied")
        return open_key(root, *args)

    monkeypatch.setattr(registry, "OpenKey", denied_machine)
    assert env.kicad_cli() == cli


def test_display_icon_can_locate_installation(registry, tmp_path):
    install = tmp_path / "custom, KiCad"
    cli = executable(install)
    registry.entries[1, 8] = {"DisplayIcon": f'"{install / "bin" / "kicad.exe"}",0'}
    assert env.kicad_cli() == cli


def test_stale_registration_falls_back_to_default_folder(registry, tmp_path, monkeypatch):
    registry.entries[1, 8] = {"InstallLocation": str(tmp_path / "removed")}
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cli = executable(tmp_path / "Programs" / "KiCad" / "10.0")
    executable(tmp_path / "Programs" / "KiCad" / "11.0")
    assert env.kicad_cli() == cli


@pytest.mark.parametrize("var", ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"])
def test_machine_default_folders(registry, tmp_path, monkeypatch, var):
    monkeypatch.setenv(var, str(tmp_path))
    cli = executable(tmp_path / "KiCad" / "10.0")
    registry.entries[1, 8] = {"InstallLocation": None, "DisplayIcon": ""}
    assert env.kicad_cli() == cli


def test_registry_access_errors_are_reported(registry, monkeypatch):
    def denied(*args):
        raise PermissionError("access denied")

    monkeypatch.setattr(registry, "OpenKey", denied)
    with pytest.raises(env.KatError, match="cannot read KiCad's Windows registration"):
        env.kicad_cli()
    assert not os.environ.get("KICAD_CLI")


def test_missing_installation_has_actionable_error(registry):
    with pytest.raises(env.KatError, match="install KiCad 10"):
        env.kicad_cli()
    assert not os.environ.get("KICAD_CLI")


def test_linux_still_uses_path_or_override(registry, monkeypatch):
    monkeypatch.setattr(env.sys, "platform", "linux")
    with pytest.raises(env.KatError, match="kicad-cli not found"):
        env.kicad_cli()
    assert not registry.calls
    monkeypatch.setenv("KICAD_CLI", "/opt/kicad-cli")
    assert env.kicad_cli() == "/opt/kicad-cli"


def test_macos_bundle_discovery_is_cached(registry, monkeypatch):
    monkeypatch.setattr(env.sys, "platform", "darwin")
    cli = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    monkeypatch.setattr(env.os.path, "isfile", lambda path: path == cli)
    assert env.kicad_cli() == cli
    assert os.environ["KICAD_CLI"] == cli
    assert not registry.calls


def test_run_cli_passes_cached_path_to_child_environment(registry, monkeypatch):
    monkeypatch.setattr(env.shutil, "which", lambda name: "path-kicad-cli")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "10.0.6\n", "")

    monkeypatch.setattr(env.subprocess, "run", run)
    assert env.run_cli("version").stdout == "10.0.6\n"
    assert calls[0][0] == ["path-kicad-cli", "version"]
    assert calls[0][1]["env"]["KICAD_CLI"] == "path-kicad-cli"


def test_stock_symbols_follow_registered_installation(registry, tmp_path, monkeypatch):
    executable(tmp_path)
    symbols = tmp_path / "share" / "kicad" / "symbols"
    symbols.mkdir(parents=True)
    registry.entries[1, 8] = {"InstallLocation": str(tmp_path)}
    for name in ("KICAD10_SYMBOL_DIR", "KICAD9_SYMBOL_DIR", "KICAD_SYMBOL_DIR"):
        monkeypatch.delenv(name, raising=False)
    assert env.stock_symbol_dir() == symbols
