"""Opening in KiCad: the lock-file guard (always) and `kat open --verify` against the real
editors (marker kicad_gui: needs a display and kicad-python; run with -m kicad_gui)."""

import json
import socket

import pytest

import kat
from kat import cli, gui
from test_pcb_sync import board_of, design
from test_sync import project  # noqa: F401 (fixture)

needs_cli = pytest.mark.requires("kicad", "symbols", "footprints")


def lock(path, host=None):
    gui.lock_file(path).write_text(json.dumps({"hostname": host or socket.gethostname(),
                                               "username": "someone"}))


@needs_cli
def test_edit_refuses_while_the_file_is_open_in_kicad(project, monkeypatch):
    monkeypatch.setattr(gui, "_kicad_running", lambda: True)
    lock(project)
    with pytest.raises(kat.KatError, match="open in KiCad"):
        with kat.edit(project) as s:
            s.add("Device:R", 50.8, 50.8, ref="R1")
    assert cli.main(["add", str(project), "Device:R", "50.8", "50.8"]) == 2


@needs_cli
def test_stale_lock_is_ignored_but_another_host_counts(project, monkeypatch):
    monkeypatch.setattr(gui, "_kicad_running", lambda: False)
    lock(project)                                   # no KiCad running here: crashed, stale
    with kat.edit(project) as s:
        s.add("Device:R", 50.8, 50.8, ref="R1")
    lock(project, host="other-machine")             # open on another machine (shared drive)
    with pytest.raises(kat.KatError, match="other-machine"):
        with kat.edit(project) as s:
            s.move("R1", to=(60.96, 50.8))


def test_open_rejects_other_files(tmp_path):
    (tmp_path / "x.txt").write_text("")
    with pytest.raises(kat.KatError, match="kicad_pcb"):
        gui.open_file(tmp_path / "x.txt")


@pytest.mark.kicad_gui
@needs_cli
def test_verify_board_and_schematic_in_the_editors(project, tmp_path):
    design(project, tmp_path)
    pcb = board_of(project)
    kat.pcb.sync(pcb)
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 60, 40))
        b.zone("GND", "B.Cu")
    res = kat.open_kicad(pcb, verify=True)
    assert res["verified"] and res["ok"], res["mismatches"]
    assert res["kicad"]["footprints"] == sorted(f.ref for f in kat.pcb.load(pcb).footprints())
    assert res["kicad"]["zones"] == ["GND B.Cu"] and res["kicad"]["outline"] == [0, 0, 60, 40]
    res = kat.open_kicad(project, verify=True)
    assert res["verified"] and res["ok"], res
    assert not list(project.parent.glob("~*.lck"))  # the editors are closed again
    text = pcb.read_text().replace("(segment", "(segmentx", 1) if "(segment" in pcb.read_text() \
        else pcb.read_text().replace("(zone", "(zonex", 1)
    pcb.write_text(text)                            # KiCad cannot read this any more
    res = kat.open_kicad(pcb, verify=True)
    assert res["verified"] and not res["ok"] and res["mismatches"]
