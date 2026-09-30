"""KiCad UTF-8 files must not depend on Python's platform-default encoding."""

import ast
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kat import blocks, checks, cli, env, gui, history, library, netlist, project, report
from kat import syncer, syncer_hier
from kat.pcb import checks as pcb_checks
from kat.pcb import specctra, stamping
from kat.pcb.board import Board

TEXT = "caf\u00e9 \u96fb\u8def \u03a9"


@pytest.fixture
def legacy_default(monkeypatch):
    original = Path.open

    def open_path(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return original(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_path)


def json_file(path, data):
    path.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def test_library_table_and_search_use_utf8(tmp_path, monkeypatch, legacy_default):
    sym = tmp_path / f"{TEXT}.kicad_sym"
    sym.write_bytes(
        f'(kicad_symbol_lib\n\t(symbol "{TEXT}" (property "Description" "{TEXT}"))\n)'
        .encode("utf-8"))
    table = tmp_path / "sym-lib-table"
    table.write_bytes(
        f'(sym_lib_table (lib (name "{TEXT}") (type "KiCad") (uri "${{KIPRJMOD}}/{sym.name}")))'
        .encode("utf-8"))
    libs = env._read_lib_table(table, tmp_path)
    assert libs == {TEXT: sym}
    monkeypatch.setattr(env, "symbol_libraries", lambda path: libs)
    monkeypatch.setattr(env, "cache_dir", lambda: tmp_path)
    result = library.search("\u96fb\u8def")
    assert result[0]["lib_id"] == f"{TEXT}:{TEXT}"
    assert result[0]["description"] == TEXT
    assert library.search("\u96fb\u8def") == result


@pytest.mark.parametrize("target", ["symbol", "cache"])
def test_search_rejects_invalid_utf8(tmp_path, monkeypatch, target):
    sym = tmp_path / "bad.kicad_sym"
    sym.write_bytes(b"\xff" if target == "symbol" else b"(kicad_symbol_lib)")
    cache = tmp_path / "symbol-index.json"
    if target == "cache":
        cache.write_bytes(b"\xff")
    monkeypatch.setattr(env, "symbol_libraries", lambda path: {"Bad": sym})
    monkeypatch.setattr(env, "cache_dir", lambda: tmp_path)
    with pytest.raises(env.KatError, match="not valid UTF-8") as error:
        library.search("bad")
    assert str(sym if target == "symbol" else cache) in str(error.value)


def test_metadata_and_rules_use_utf8(tmp_path, legacy_default):
    pcb = tmp_path / f"{TEXT}.kicad_pcb"
    pcb.write_bytes(b"(kicad_pcb)")
    pro = pcb.with_suffix(".kicad_pro")
    json_file(pro, {"text_variables": {"name": TEXT}})
    rules = pcb.with_suffix(".kicad_dru")
    rules.write_bytes(f'(version 1)\n(rule "{TEXT}" (constraint clearance (min 0.2mm)))\n'
                      .encode("utf-8"))
    b = Board(pcb)
    b.netclass(TEXT, track=0.25, clearance=0.2, nets=[TEXT])
    b._gen_rules[TEXT] = f'(rule "{TEXT} added" (constraint clearance (min 0.3mm)))'
    b._write_project_files()
    assert json.loads(pro.read_bytes())["text_variables"]["name"] == TEXT
    assert TEXT.encode("utf-8") in rules.read_bytes()
    assert f"{TEXT} added".encode("utf-8") in rules.read_bytes()
    syncer_hier.remember(pcb, sheets=[TEXT], design=f"{TEXT}.net")
    design = tmp_path / f"{TEXT}.net"
    design.write_bytes(b"(export)")
    assert syncer_hier.remembered_sheets(pcb) == [TEXT]
    assert syncer_hier.remembered_design(pcb) == design.resolve()
    b._route_edits = {"layers": {TEXT: ["F.Cu"]}}
    b._write_route_settings()
    assert b.route_settings()["layers"] == {TEXT: ["F.Cu"]}
    stamping.set_environment(pcb, [TEXT])
    assert stamping.block_info(pcb)["environment"] == [TEXT]
    b._stamps = {"/" + TEXT: {"ports": {TEXT: {"net": TEXT}}}}
    b._save_stamps()
    assert b.ports("/" + TEXT) == {TEXT: {"net": TEXT}}
    d = tmp_path / "RC"
    d.mkdir()
    blocks._write_info(d, {"type": TEXT})
    assert blocks._info(d)["type"] == TEXT
    json_file(netlist.blocks_file(design),
              {"instances": {TEXT: {"type": TEXT}}, "no_connect": [f"{TEXT}.1"]})
    assert netlist._block_types(design) == {TEXT: TEXT}
    assert syncer.read_no_connect(design) == {(TEXT, "1")}


def test_project_fallback_and_export_use_utf8(tmp_path, monkeypatch, legacy_default):
    monkeypatch.setattr(env, "stock_symbol_dir", lambda: tmp_path / "absent" / "symbols")
    monkeypatch.setattr(report, "enabled", lambda: False)
    monkeypatch.setattr("kat.schematic.normalize", lambda path: None)
    sch = tmp_path / f"{TEXT}.kicad_sch"
    project.new(sch)
    assert json.loads(sch.with_suffix(".kicad_pro").read_bytes())["meta"]["filename"] == f"{TEXT}.kicad_pro"
    pcb = tmp_path / f"{TEXT}.kicad_pcb"
    pcb.write_bytes(b'(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal)))')
    b = Board(pcb)
    b.outline(rect=(0, 0, 10, 10))
    out = tmp_path / "design.dsn"
    specctra.write_dsn(b, out)
    assert TEXT.encode("utf-8") in out.read_bytes()


@pytest.mark.parametrize("kind", ["erc", "drc"])
@pytest.mark.parametrize("invalid", [False, True])
def test_kicad_json_is_strict_utf8(tmp_path, monkeypatch, legacy_default, kind, invalid):
    violation = {"severity": "error", "type": "test", "description": TEXT,
                 "items": [{"description": TEXT, "pos": {"x": 1, "y": 2}}]}
    data = ({"sheets": [{"violations": [violation]}]} if kind == "erc"
            else {"violations": [violation]})

    def run(*args, **kwargs):
        out = Path(args[args.index("-o") + 1])
        if invalid:
            out.write_bytes(b"\xff")
        else:
            json_file(out, data)

    monkeypatch.setattr(env, "run_cli", run)
    check = checks.erc if kind == "erc" else pcb_checks.drc
    if invalid:
        with pytest.raises(UnicodeDecodeError):
            check(tmp_path / "design")
    else:
        result = check(tmp_path / "design")
        assert result["violations"][0]["description"] == TEXT
        assert result["violations"][0]["items"][0]["description"] == TEXT


def test_gui_settings_and_locks_are_strict_utf8(tmp_path, monkeypatch, legacy_default):
    config = tmp_path / "config-source"
    config.mkdir()
    json_file(config / "kicad_common.json", {"other": TEXT})
    monkeypatch.setattr(env, "_config_dir", lambda: config)
    copied = gui._private_settings(tmp_path / "private") / config.name / "kicad_common.json"
    assert json.loads(copied.read_bytes()) == {"other": TEXT, "api": {"enable_server": True}}
    path = tmp_path / "design.kicad_sch"
    lock = gui.lock_file(path)
    json_file(lock, {"hostname": TEXT, "username": TEXT})
    assert gui.holder(path)["user"] == TEXT
    lock.write_bytes(b"\xff")
    with pytest.raises(env.KatError, match="not valid UTF-8"):
        gui.holder(path)


def test_history_note_uses_utf8(tmp_path, monkeypatch, legacy_default):
    sch = tmp_path / "design.kicad_sch"
    sch.write_bytes(b"(kicad_sch)")

    def render_snapshot(path, target, *args):
        target.mkdir(parents=True)
        return {}

    monkeypatch.setattr(history.render, "snapshot", render_snapshot)
    monkeypatch.setattr(checks, "run", lambda *args: {
        "ok": True, "clean": True, "erc": {"errors": 0, "warnings": 0},
        "overlaps": [], "body_overlaps": [], "off_page": [], "duplicates": [],
        "description": TEXT})
    result = history.snapshot(sch, note=TEXT)
    target = Path(result["dir"])
    assert (target / "note.md").read_bytes().decode("utf-8").strip() == TEXT
    assert json.loads((target / "check.json").read_bytes())["description"] == TEXT


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
@pytest.mark.parametrize("code", [0, 3])
def test_run_cli_decodes_real_child_pipes(monkeypatch, encoding, code):
    monkeypatch.setattr(env, "kicad_cli", lambda: sys.executable)
    monkeypatch.setattr(env, "_cli_encoding", lambda: encoding)
    raw = "caf\u00e9".encode(encoding)
    script = (f"import sys; sys.stdout.buffer.write({raw!r}); "
              f"sys.stderr.buffer.write({raw!r}); sys.exit({code})")
    result = env.run_cli("-c", script, check=False)
    assert result.stdout == result.stderr == "caf\u00e9"
    assert result.returncode == code
    if code:
        with pytest.raises(env.KatError, match="caf\u00e9"):
            env.run_cli("-c", script)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("encoding,raw", [("utf-8", b"\xff"), ("cp1252", b"\x81")])
def test_run_cli_reports_decode_failure(monkeypatch, stream, encoding, raw):
    monkeypatch.setattr(env, "kicad_cli", lambda: sys.executable)
    monkeypatch.setattr(env, "_cli_encoding", lambda: encoding)
    with pytest.raises(env.KatError, match=f"{stream} output is not valid {encoding}") as error:
        env.run_cli("-c", f"import sys; sys.{stream}.buffer.write({raw!r}); sys.exit(3)",
                    check=False)
    assert "exit 3" in str(error.value)


def test_run_cli_preserves_unicode_argv(monkeypatch):
    monkeypatch.setattr(env, "kicad_cli", lambda: sys.executable)
    monkeypatch.setattr(env, "_cli_encoding", lambda: "utf-8")
    result = env.run_cli("-c", "import sys; sys.stdout.buffer.write(sys.argv[1].encode('utf-8'))", TEXT)
    assert result.stdout == TEXT


@pytest.mark.parametrize("codepage", [1252, 65001])
def test_windows_pipe_encoding_is_independent_of_python_utf8(monkeypatch, codepage):
    monkeypatch.setattr(env.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll",
                        SimpleNamespace(kernel32=SimpleNamespace(GetACP=lambda: codepage)),
                        raising=False)
    monkeypatch.setenv("PYTHONUTF8", "1")
    assert env._cli_encoding() == f"cp{codepage}"


def test_cli_invalid_encoding_is_json_error(tmp_path, monkeypatch, capsys):
    sym = tmp_path / "bad.kicad_sym"
    sym.write_bytes(b"\xff")
    monkeypatch.setattr(env, "symbol_libraries", lambda path: {"Bad": sym})
    monkeypatch.setattr(env, "cache_dir", lambda: tmp_path)
    assert cli.main(["lib", "search", "bad"]) == 2
    error = json.loads(capsys.readouterr().out)["error"]
    assert str(sym) in error and "not valid UTF-8" in error


def test_text_boundaries_have_explicit_encodings():
    root = Path(env.__file__).parent
    missing = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_bytes().decode("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = next((k.value for k in node.keywords if k.arg == "mode"),
                            node.args[1] if len(node.args) > 1 else ast.Constant("r"))
                if isinstance(mode, ast.Constant) and "b" in mode.value:
                    continue
            elif isinstance(node.func, ast.Attribute) and node.func.attr in ("read_text", "write_text"):
                if (path.name == "gui.py" and node.func.attr == "read_text"
                        and ast.unparse(node.func.value) == "p / 'comm'"):
                    continue  # Linux /proc process names are OS-owned, not KiCad text.
            else:
                continue
            if any(k.arg == "encoding" for k in node.keywords):
                continue
            missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


def test_python_utf8_mode_off_in_fresh_process():
    child_env = dict(os.environ, PYTHONUTF8="0")
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c",
         "import sys, locale, json; print(json.dumps({'utf8': sys.flags.utf8_mode, "
         "'default': locale.getpreferredencoding(False)}))"],
        capture_output=True, encoding="ascii", env=child_env, check=True)
    info = json.loads(result.stdout)
    assert info["utf8"] == 0
    assert info["default"]
