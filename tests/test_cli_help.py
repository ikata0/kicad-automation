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


def test_batch_rejects_bad_arguments_without_saving(tmp_path, capsys):
    sch = tmp_path / "b" / "b.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    before = sch.read_text()
    script = tmp_path / "s.kat"
    script.write_text("move R1 --by nonsense\n")
    assert cli.main(["batch", str(sch), str(script)]) == 2
    assert "line 1" in capsys.readouterr().out.splitlines()[-1]
    assert sch.read_text() == before
