"""Opening files in KiCad's own editors: ``kat open FILE`` for people, ``--verify`` as a
check that the editor loads what kat wrote, and the lock-file guard that keeps kat from
editing a file that is open in the GUI.

KiCad writes ``~NAME.kicad_pcb.lck`` (JSON: hostname, username; no process id) next to a
file it has open and removes it on a normal close. A lock is taken as live when it comes
from another host, or when a KiCad process of this user is running; a lock without any
KiCad process is stale (left behind by a crash) and ignored.

``--verify`` starts the editor with a private copy of the KiCad settings (IPC API
enabled) and a private ``TMPDIR`` (its own API socket and instance check, so a KiCad the
user has open is not involved), compares what the editor reports through kicad-python
with kat's own reading, and closes it without saving. KiCad 10.0.6 has no schematic API:
a schematic counts as verified when the editor opens it (lock file written) and keeps
running.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import env

EDITORS = {".kicad_pcb": "pcbnew", ".kicad_sch": "eeschema", ".kicad_pro": "kicad"}
KICAD_PROCESSES = ("kicad", "pcbnew", "eeschema")


def lock_file(path: Path | str) -> Path:
    path = Path(path)
    return path.parent / f"~{path.name}.lck"


def _kicad_running() -> bool | None:
    """True if a KiCad editor of this user runs; None where /proc is unavailable."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    uid = os.getuid()
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid == uid and (p / "comm").read_text().strip() in KICAD_PROCESSES:
                return True
        except OSError:
            continue
    return False


def holder(path: Path | str) -> dict | None:
    """Who has ``path`` open in KiCad (``host``, ``user``), or None. Stale locks count as
    not open."""
    lck = lock_file(path)
    if not lck.exists():
        return None
    try:
        info = json.loads(lck.read_text() or "{}")
    except (OSError, ValueError):
        info = {}
    host, user = info.get("hostname", "?"), info.get("username", "?")
    if host not in (socket.gethostname(), "?"):
        return {"host": host, "user": user, "lock": str(lck)}
    if _kicad_running() is False:
        return None
    return {"host": host, "user": user, "lock": str(lck)}


def guard(path: Path | str) -> None:
    """Refuse to write ``path`` while it is open in a KiCad editor."""
    h = holder(path)
    if h:
        raise env.KatError(
            f"{Path(path).name} is open in KiCad ({h['user']}@{h['host']}): close it there first, "
            f"so neither side overwrites the other (lock {h['lock']}; delete it if KiCad crashed)")


# --- opening ------------------------------------------------------------------------------

def _editor(path: Path) -> str:
    if path.suffix not in EDITORS:
        raise env.KatError("open takes a .kicad_pcb, .kicad_sch or .kicad_pro file")
    exe = shutil.which(EDITORS[path.suffix])
    if exe is None:
        raise env.KatError(f"{EDITORS[path.suffix]} not found (is KiCad installed?)")
    return exe


def open_file(path: Path | str) -> dict:
    """Start the matching KiCad editor on ``path`` for a person to look at (detached)."""
    path = Path(path)
    if not path.exists():
        raise env.KatError(f"{path}: no such file")
    exe = _editor(path)
    proc = subprocess.Popen([exe, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    return {"file": str(path), "editor": Path(exe).name, "pid": proc.pid,
            "note": "close the editor before kat edits this file again"}


def _unavailable(path: Path, reason: str) -> dict:
    return {"file": str(path), "verified": None, "ok": None, "reason": reason}


def _private_settings(tmp: Path) -> Path:
    """A copy of the user's KiCad settings with the IPC API server enabled."""
    src = env._config_dir()
    cfg = tmp / "config"
    dst = cfg / src.name
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("3d", "*.lck", "*cache*"))
    dst.mkdir(parents=True, exist_ok=True)
    common = dst / "kicad_common.json"
    data = json.loads(common.read_text()) if common.exists() else {}
    data.setdefault("api", {})["enable_server"] = True
    common.write_text(json.dumps(data, indent=2))
    return cfg


def _connect(sock: Path, proc: subprocess.Popen, timeout: float):
    from kipy import KiCad
    end = time.time() + timeout
    last = None
    while time.time() < end:
        if proc.poll() is not None:
            raise env.KatError(f"the editor exited (code {proc.returncode}) while loading")
        if sock.exists():
            try:
                k = KiCad(socket_path=f"ipc://{sock}", timeout_ms=5000)
                k.get_version()
                return k
            except Exception as e:     # noqa: BLE001 - not ready yet
                last = e
        time.sleep(0.5)
    raise env.KatError(f"no answer from the editor's API within {timeout:.0f} s ({last})")


def _board_mismatches(path: Path, kicad, timeout: float) -> tuple[list[str], dict]:
    from kipy.proto.board.board_types_pb2 import BoardLayer

    from .pcb.board import Board
    from .sexpr import find
    end = time.time() + timeout
    seen = None
    while True:                                # the board may still be loading: wait until
        try:                                   # two readings a second apart agree
            b = kicad.get_board()
            now = (len(b.get_footprints()), len(b.get_tracks()), len(b.get_zones()))
            if now == seen or time.time() > end:
                break
            seen = now
        except Exception:                      # noqa: BLE001
            if time.time() > end:
                raise
        time.sleep(1.0)
    mine = Board(path)
    theirs = {
        "footprints": sorted(f.reference_field.text.value for f in b.get_footprints()),
        "nets": sorted(n.name for n in b.get_nets() if n.name),
        "tracks": len(b.get_tracks()),
        "vias": len(b.get_vias()),
        "zones": sorted(z.name for z in b.get_zones()),
    }
    pts = []
    for s in b.get_shapes():
        if s.layer != BoardLayer.BL_Edge_Cuts:
            continue
        for attr in ("start", "end", "mid", "top_left", "bottom_right"):
            v = getattr(s, attr, None)
            if v is not None and hasattr(v, "x"):
                pts.append((v.x / 1e6, v.y / 1e6))
    theirs["outline"] = [round(min(p[0] for p in pts), 3), round(min(p[1] for p in pts), 3),
                         round(max(p[0] for p in pts), 3), round(max(p[1] for p in pts), 3)] if pts else None
    ob = mine.outline_box()
    ours = {
        "footprints": sorted(f.ref for f in mine.footprints()),
        "nets": sorted({p.net for p in mine.pads() if p.net} |
                       {t["net"] for t in mine.tracks() + mine.vias() if t["net"]}),
        "tracks": len(mine.tracks()),
        "vias": len(mine.vias()),
        "zones": sorted(str(n[1]) if (n := find(z, "name")) is not None else "" for z in mine.zones()),
        "outline": [round(v, 3) for v in ob] if ob else None,
    }
    bad = []
    for k in ours:
        a, t = ours[k], theirs[k]
        if k == "outline" and a and t:
            same = all(abs(x - y) < 0.01 for x, y in zip(a, t))
        elif k == "nets":
            same = set(a) <= set(t)            # KiCad also lists nets only zones carry
        else:
            same = a == t
        if not same:
            bad.append(f"{k}: kat {a} / KiCad {t}")
    return bad, theirs


def verify(path: Path | str, timeout: float = 90.0) -> dict:
    """Open ``path`` in KiCad's editor, compare what the editor loaded with kat's own reading
    (board: footprints, nets, tracks, vias, zone names, outline; schematic: opened and still
    running), then close it without saving. ``verified`` is None (not an error) when there
    is no display or, for boards, no kicad-python. Returns ``ok``, ``mismatches``, ``kicad``
    (what the editor reported), ``seconds``."""
    path = Path(path).resolve()
    if not path.exists():
        raise env.KatError(f"{path}: no such file")
    exe = _editor(path)
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return _unavailable(path, "no display (DISPLAY / WAYLAND_DISPLAY)")
    board = path.suffix == ".kicad_pcb"
    if board:
        try:
            import kipy  # noqa: F401
        except ImportError:
            return _unavailable(path, "kicad-python not installed (pip install 'kat[gui]')")
    guard(path)
    before = {p for p in path.parent.glob("~*.lck")}
    tmp = Path(tempfile.mkdtemp(prefix="katgui", dir="/tmp" if os.path.isdir("/tmp") else None))
    t0 = time.time()
    proc = None
    try:
        envv = dict(os.environ, KICAD_CONFIG_HOME=str(_private_settings(tmp)), TMPDIR=str(tmp))
        log = open(tmp / "editor.log", "w")
        proc = subprocess.Popen([exe, str(path)], stdout=log, stderr=subprocess.STDOUT,
                                env=envv, cwd=path.parent, start_new_session=True)
        if board:
            kicad = _connect(tmp / "kicad" / "api.sock", proc, timeout)
            bad, theirs = _board_mismatches(path, kicad, timeout)
            theirs["version"] = kicad.get_version().full_version
        else:
            lck, end = lock_file(path), time.time() + timeout
            while not lck.exists() and time.time() < end and proc.poll() is None:
                time.sleep(0.5)
            time.sleep(3)                      # still running after loading?
            bad = [] if lck.exists() and proc.poll() is None else [
                "the editor did not open the file" if proc.poll() is None
                else f"the editor exited (code {proc.returncode})"]
            theirs = {"opened": lck.exists(), "running": proc.poll() is None,
                      "note": "KiCad 10.0.6 has no schematic API; checked that it opens and runs"}
        return {"file": str(path), "editor": Path(exe).name, "verified": True, "ok": not bad,
                "mismatches": bad, "kicad": theirs, "seconds": round(time.time() - t0, 1)}
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
        for p in set(path.parent.glob("~*.lck")) - before:      # what our editor left behind
            p.unlink(missing_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
