"""Autorouting with Freerouting (headless), through kat's own Specctra DSN/SES.

Freerouting is a separate program (Java, GPL-3), not a Python dependency. ``install()``
downloads the pinned release once into kat's cache; ``KAT_FREEROUTING`` points to another
jar. The router's own "all routed" is not trusted: every run ends with KiCad's DRC.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .. import env
from . import checks, specctra
from .board import Board

VERSION = "2.4.1"
URL = f"https://github.com/freerouting/freerouting/releases/download/v{VERSION}/freerouting-{VERSION}.jar"
SHA256 = "251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9"
JAVA_MIN = 21


def jar_path() -> Path:
    if os.environ.get("KAT_FREEROUTING"):
        return Path(os.environ["KAT_FREEROUTING"])
    return env.cache_dir() / f"freerouting-{VERSION}.jar"


def java() -> str:
    exe = shutil.which("java")
    if not exe:
        raise env.KatError(f"Freerouting needs Java {JAVA_MIN}+ (java not on PATH)")
    out = subprocess.run([exe, "-version"], capture_output=True, text=True)
    m = re.search(r'version "(\d+)', out.stderr + out.stdout)
    if m and int(m.group(1)) < JAVA_MIN:
        raise env.KatError(f"Freerouting needs Java {JAVA_MIN}+, found {m.group(1)}")
    return exe


def install() -> dict:
    """Download Freerouting (pinned version, checked by SHA-256) into kat's cache."""
    target = jar_path()
    if target.exists():
        return {"jar": str(target), "downloaded": False}
    tmp = target.with_suffix(".part")
    with urllib.request.urlopen(URL, timeout=120) as r, open(tmp, "wb") as out:
        shutil.copyfileobj(r, out)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != SHA256:
        tmp.unlink()
        raise env.KatError(f"Freerouting download has SHA-256 {digest}, expected {SHA256}")
    tmp.replace(target)
    return {"jar": str(target), "downloaded": True, "url": URL}


def _key(a, b, layer):
    pa, pb = (round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3))
    return (layer, min(pa, pb), max(pa, pb))


def route(pcb: Path | str, passes: int = 20, nets: list[str] | None = None, timeout: int = 1800,
          out: Path | str | None = None) -> dict:
    """Autoroute the board: DSN -> Freerouting -> SES -> new tracks and vias -> save ->
    KiCad DRC. Locked copper is kept as it is; ``nets`` restricts routing to those nets.
    DSN, SES and the router log go to ``out`` (default: history/<stamp>_route/)."""
    pcb = Path(pcb)
    jar = jar_path()
    if not jar.exists():
        raise env.KatError(f"Freerouting not installed: run `kat pcb route --install` ({URL})")
    exe = java()
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    work = Path(out) if out else pcb.parent / "history" / f"{stamp}_route"
    work.mkdir(parents=True, exist_ok=True)
    b = Board(pcb)
    dsn, ses = work / f"{pcb.stem}.dsn", work / f"{pcb.stem}.ses"
    info = specctra.write_dsn(b, dsn, nets)
    t0 = time.time()
    try:
        proc = subprocess.run([exe, "-Djava.awt.headless=true", "-jar", str(jar), "--gui.enabled=false",
                               "-de", str(dsn), "-do", str(ses), "-mp", str(passes)],
                              capture_output=True, text=True, timeout=timeout, cwd=work)
    except subprocess.TimeoutExpired:
        raise env.KatError(f"Freerouting did not finish within {timeout} s (log in {work})")
    log = proc.stdout + proc.stderr
    (work / "freerouting.log").write_text(log)
    seconds = round(time.time() - t0, 1)
    if not ses.exists():
        raise env.KatError(f"Freerouting wrote no session (exit {proc.returncode}); see {work}/freerouting.log")
    result = specctra.read_ses(ses)
    have = {_key(t["start"], t["end"], t["layer"]) for t in b.tracks()}
    have_vias = {(round(v["at"][0], 3), round(v["at"][1], 3)) for v in b.vias()}
    added_t = added_v = 0
    # Freerouting narrows tracks at fine-pitch pads ("neck-down") below the net class
    # width; widen them where the clearance allows, report the rest
    from .structure import _Copper
    cu, cl = _Copper(b), b._clearance_fn()
    widened, narrow = 0, []
    for w in result["wires"]:
        k = _key(w["start"], w["end"], w["layer"])
        if k in have:
            continue
        have.add(k)
        want = b.net_rules(w["net"])["track"]
        width = w["width"]
        if width < want - 1e-6:
            if not cu.conflicts(w["net"], {w["layer"]}, cl, seg=((w["start"], w["end"]), want / 2)):
                width, widened = want, widened + 1
            else:
                narrow.append({"net": w["net"], "layer": w["layer"], "width": width,
                               "start": list(w["start"]), "end": list(w["end"])})
        b._segment(w["start"], w["end"], width, w["layer"], w["net"], False)
        cu.add_track(w["start"], w["end"], width, w["layer"], w["net"])
        added_t += 1
    for v in result["vias"]:
        k = (round(v["at"][0], 3), round(v["at"][1], 3))
        if k in have_vias:
            continue
        have_vias.add(k)
        b._via(v["at"], v["net"], v["size"], v["drill"], False)
        added_v += 1
    b.save()
    score = re.findall(r"\((\d+) unrouted and (\d+) violations\)", log)
    c = checks.run(pcb)
    return {"dir": str(work), "dsn": info, "seconds": seconds, "passes": passes,
            "router": {"unrouted": int(score[-1][0]), "violations": int(score[-1][1])} if score else None,
            "added_tracks": added_t, "added_vias": added_v, "widened": widened, "narrow": narrow,
            "check": {k: c[k] for k in ("ok", "clean", "drc_errors", "drc_warnings", "unconnected",
                                        "parity", "islands", "by_type")}}
