"""Design journal: NAME_REPORT.md with numbered entries for commands, batch scripts,
comments and snapshots."""

import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import kat
from kat import cli, report
from test_sync import project  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")


def entries(sch: Path) -> list[tuple[int, str, str]]:
    """(number, kind, body) of every entry."""
    text = (sch.parent / f"{sch.stem}_REPORT.md").read_text()
    parts = re.split(r"^## (\d+) · [\d-]+ [\d:]+ · (\w+)\n", text, flags=re.M)
    return [(int(parts[i]), parts[i + 1], parts[i + 2].strip("\n")) for i in range(1, len(parts), 3)]


def test_new_creates_report_and_commands_are_numbered(project):
    report_file = project.parent / "p_REPORT.md"
    assert report_file.read_text().startswith("# p: design report")
    assert cli.main(["add", str(project), "Device:R", "50.8", "50.8", "--ref", "R1"]) == 0
    assert cli.main(["check", str(project)]) == 1          # ERC: R1 unconnected
    got = entries(project)
    assert [(n, k) for n, k, _ in got] == [(1, "command"), (2, "command"), (3, "command")]
    body = got[2][2]
    assert body.startswith("`kat check p.kicad_sch` · ")       # paths relative to the project
    assert "- ok: false" in body and "<details><summary>result</summary>" in body
    assert str(project.parent) + "/" not in body            # paths relative to the project
    assert cli.main(["lib", "search", "resistor", "--limit", "1"]) == 0     # lookups: no entry
    assert cli.main(["api", "keepout"]) == 0
    assert len(entries(project)) == 3


def test_numbering_continues_across_processes(project):
    subprocess.run([sys.executable, "-m", "kat", "check", str(project)], check=True,
                   capture_output=True)
    assert cli.main(["check", str(project)]) == 0
    assert [n for n, _, _ in entries(project)] == [1, 2, 3]


def test_errors_are_journaled_with_exit_code(project):
    assert cli.main(["move", str(project), "R9", "--to", "10,10"]) == 2
    n, kind, body = entries(project)[-1]
    assert "· exit 2" in body and "no symbol R9" in body


def test_batch_keeps_the_code_output_and_images_but_no_nested_entries(project, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "pass1.py"
    script.write_text(
        '"""pass 1: two resistors in a row."""\n'
        "import kat\n"
        f"SCH = {str(project)!r}\n"
        "with kat.edit(SCH) as s:\n"
        "    for i in range(2):\n"
        "        s.add('Device:R', 50.8 + 10.16 * i, 50.8, ref=f'R{i + 1}')\n"
        "print('placed', len(kat.load(SCH).list()['symbols']))\n"
        "kat.check(SCH)\n"
        "kat.snapshot(SCH, slug='row')\n"
        "kat.comment('Row done.', 'milestone')\n")
    res = kat.batch_exec(script)
    assert res["ok"] and res["entry"] == 2, res
    got = entries(project)
    assert [k for _, k, _ in got] == ["command", "batch"]        # check/snapshot inside: none
    body = got[1][2]
    assert "**pass 1: two resistors in a row.**" in body
    assert script.read_text().rstrip() in body                    # the loop, not its result
    assert "placed 2" in body and "**milestone:** Row done." in body
    img = re.search(r"!\[p\]\((p_REPORT/002_row/p\.png)\)", body)
    assert img and (project.parent / img.group(1)).is_file()


def test_failed_batch_reports_the_error_and_saves_nothing(project, tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("import kat\n"
                      f"with kat.edit({str(project)!r}) as s:\n"
                      "    s.add('Device:R', 50.8, 50.8, ref='R1')\n"
                      "    s.move('R7', to=(10, 10))\n")
    before = project.read_text()
    assert cli.main(["batch-exec", str(script)]) == 1
    assert project.read_text() == before
    body = entries(project)[-1][2]
    assert "FAILED" in body and "KatError: no symbol R7" in body
    assert 'File "' in body and "bad.py" in body and "runpy" not in body


def test_comment_from_cli_and_stdin(project, monkeypatch):
    monkeypatch.chdir(project.parent.parent)                      # project found in a subdir
    assert cli.main(["comment", "--kind", "plan", "Place", "**U1**", "first."]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("Two lines:\n- DRC 3 errors\n"))
    assert cli.main(["comment", "--kind", "problem", "--project", str(project), "-"]) == 0
    got = entries(project)
    assert got[-2][1:] == ("plan", "Place **U1** first.")
    assert got[-1][1] == "problem" and "- DRC 3 errors" in got[-1][2]
    with pytest.raises(kat.KatError, match="kind"):
        kat.comment("x", "idea", project)


def test_library_snapshot_outside_a_batch_gets_its_own_entry(project):
    kat.snapshot(project, slug="lib")
    n, kind, body = entries(project)[-1]
    assert kind == "snapshot" and body.startswith("`kat.snapshot(")
    assert f"p_REPORT/{n:03d}_lib/p.png" in body


def test_long_results_are_cut(project):
    big = {"items": [{"n": i} for i in range(200)]}
    root = project.parent.resolve()
    block = report._json_block(big, root)
    assert "... cut:" in block and block.count("\n") < report.JSON_LINES + 10


def test_disabled_writes_nothing(project, monkeypatch):
    monkeypatch.setenv("KAT_REPORT", "0")
    assert cli.main(["check", str(project)]) == 0
    monkeypatch.delenv("KAT_REPORT")
    assert cli.main(["--no-report", "check", str(project)]) == 0
    assert len(entries(project)) == 1                             # only `kat new`


def test_summary_lines():
    lines = report.summarize({"ok": False, "check": {"ok": True, "parity": 0, "by_type": {"x": 2}},
                              "added": [{"ref": "R1"}, {"ref": "C4"}], "removed": [], "file": "/a/b.kicad_pcb",
                              "stale": [], "vias": ["V1"]}, Path("/a"))
    assert lines[:3] == ["ok: false", "added: R1, C4", "removed: none"]
    assert "vias: 1 item" in lines and not any(l.startswith("stale") for l in lines)
    assert "check.ok: true" in lines and "check.by_type: x 2" in lines and "file: `b.kicad_pcb`" in lines
