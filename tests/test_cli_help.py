import pytest

from kat import cli


def subcommands():
    parser = cli.build_parser()
    action = next(a for a in parser._actions if a.dest == "cmd")
    return sorted(action.choices)


def test_top_level_help_explains_workflow_and_points(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for text in ("Workflow", "kat sync", "REF.PIN", "Exit code"):
        assert text in out


@pytest.mark.parametrize("name", subcommands())
def test_every_command_has_description_and_examples(name, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([name, "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "Examples:" in out
    assert f"kat {name}" in out.split("Examples:")[1]


def test_python_module_runs_the_cli():
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "-m", "kat", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "kat.edit" in out.stdout


def test_edit_error_saves_nothing(tmp_path):
    import kat
    sch = tmp_path / "b" / "b.kicad_sch"
    kat.new(sch)
    before = sch.read_text()
    with pytest.raises(kat.KatError):
        with kat.edit(sch) as s:
            s.move("R1", by=(0, 2.54))  # no such part
    assert sch.read_text() == before
