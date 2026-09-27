"""kat api: lookup by full and short names, and every public name has a docstring."""

import inspect
import json

from kat import api, cli


def test_every_public_name_has_a_docstring():
    missing = [n for n, o in api.surface().items() if not inspect.getdoc(o)
               or (inspect.isroutine(o) and inspect.getdoc(o) == inspect.getdoc(type(o)))]
    assert not missing, missing


def test_full_and_short_names_resolve_to_the_same():
    full = api.lookup("kat.pcb.Board.keepout")
    for name in ("pcb.Board.keepout", "Board.keepout", "keepout"):
        assert api.lookup(name)["name"] == full["name"] == "kat.pcb.Board.keepout"
    assert full["kind"] == "function" and full["signature"].startswith("keepout(layers: str")
    assert "self" not in full["signature"] and "'" not in full["signature"].split("name")[0]
    assert full["where"].startswith("kat/pcb/setup.py:")
    assert full["doc"] == inspect.getdoc(__import__("kat").pcb.Board.keepout)


def test_ambiguous_names_list_matches_and_exact_names_see_also():
    res = api.lookup("flip")
    assert {m["name"] for m in res["matches"]} >= {"kat.pcb.Board.flip", "kat.pcb.Footprint.flip"}
    res = api.lookup("sync")                    # kat.sync is exact; kat.pcb.sync is mentioned
    assert res["name"] == "kat.sync" and "kat.pcb.sync" in res["see_also"]


def test_classes_list_mixin_methods_and_properties():
    board = {m["name"] for m in api.lookup("Board")["members"]}
    assert {"keepout", "move_many", "fanout", "place_refs", "save"} <= board
    assert "x" in {m["name"] for m in api.lookup("Footprint")["members"]}


def test_cli_text_and_json(capsys):
    assert cli.main(["api", "Board.keepout"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("kat.pcb.Board.keepout  (function, kat/pcb/setup.py:")
    assert cli.main(["api", "--json", "kat.pcb.route"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "kat.pcb.route"
    assert cli.main(["api"]) == 0
    assert "kat.pcb:" in capsys.readouterr().out
    assert cli.main(["api", "no_such_thing"]) == 2
