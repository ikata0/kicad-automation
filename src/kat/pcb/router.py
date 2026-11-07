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
import sys
import time
import urllib.request
from pathlib import Path

from .. import env
from ..sexpr import find, num
from . import checks, specctra
from .board import Board

VERSION = "2.5.0"
URL = f"https://github.com/freerouting/freerouting/releases/download/v{VERSION}/freerouting-{VERSION}.jar"
SHA256 = "f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7"
JAVA_MIN = 25     # the pinned jar's classes are version 69 (test_java_minimum_matches_the_jar)
# Freerouting sends usage data to api.freerouting.app unless told not to; kat always tells it not to
NO_ANALYTICS = "--usage_and_diagnostic_data.disable_analytics=true"
# Its necks can break the board's minimum track (DRC), even where kat then widens what it can. A rare
# board routes only with them (test_a_tall_cutout_routes_only_with_necks): options can turn them back on.
NO_NECKDOWN = "--router.automatic_neckdown=false"


def jar_path() -> Path:
    if os.environ.get("KAT_FREEROUTING"):
        return Path(os.environ["KAT_FREEROUTING"])
    return env.cache_dir() / f"freerouting-{VERSION}.jar"


def java() -> str:
    exe = shutil.which("java")
    if not exe:
        raise env.KatError(f"Freerouting needs Java {JAVA_MIN}+ (java not on PATH)")
    out = subprocess.run([exe, "-version"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
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


# Settings that do not do what they say, or that kat sets itself
_IGNORE_CLASSES = ("Freerouting 2.5.0 still leaves copper on some of those nets, "
                   "and does nothing given a list: use skip_classes")
REFUSED_OPTIONS = {
    "ignore_net_classes": _IGNORE_CLASSES,
    "autorouter.ignore_net_classes": _IGNORE_CLASSES,
    "enabled": "kat runs the router in batch mode",
    "max_passes": "use passes",
    "autorouter.max_passes": "use passes",
    "job_timeout": "use timeout",
}
# Options seen to change what the router does, in tests/test_pcb_route.py
VERIFIED_OPTIONS = {"optimizer.enabled", "automatic_neckdown"}
_GRACE = 60         # s after the router's own time limit before it is killed


def _option_args(options: dict | None) -> tuple[list[str], list[str]]:
    """Command-line words for router settings, and those whose effect kat has not seen."""
    args, unverified = [], []
    for key, value in (options or {}).items():
        key = str(key).removeprefix("--").removeprefix("router.")
        if not re.fullmatch(r"[a-z_][a-z0-9_.]*", key):
            raise env.KatError(f"router option {key!r} is not a setting name")
        if key in REFUSED_OPTIONS:
            raise env.KatError(f"router option {key}: {REFUSED_OPTIONS[key]}")
        value = str(value).lower() if isinstance(value, bool) else str(value)
        args.append(f"--router.{key}={value}")
        if key not in VERIFIED_OPTIONS:
            unverified.append(key)
    return args, unverified


def read_log(log: str) -> dict:
    """What a Freerouting log says: ``stages`` (fanout, routing, optimizer, seconds), ``unrouted``
    after each routing pass, ``timed_out``, and ``failed`` settings it refused [{option, reason}]."""
    stages = {}
    cut_off = "completed with timeout" in log
    for name, label in (("fanout", "Fanout"), ("routing", "Auto-routing"), ("optimizer", "Optimization")):
        m = re.search(rf"{label} stage completed.*?completed in ([\d.]+) seconds", log)
        if m:
            stages[name] = float(m.group(1))
        # the limit can pass with no line about it, as when a computer sleeps through it
        elif f"{label} stage started" in log:
            cut_off = True
    return {"stages": stages,
            "unrouted": [int(n) for n in re.findall(r"Auto-routing pass #\d+.*?\((\d+) unrouted", log)],
            "timed_out": cut_off,
            "failed": [{"option": a, "reason": b.strip()} for a, b in
                       re.findall(r"Failed to apply CLI router setting: router\.([^:]+): (.*)", log)]}


def select_nets(b: Board, nets: list[str] | None, classes: list[str] | None) -> list[str] | None:
    """The nets named by ``nets`` (names or globs such as ``/SPI_*``) and by net ``classes``,
    together; None when neither is given (all nets). KatError for a glob or class that
    matches nothing."""
    import fnmatch
    if not nets and not classes:
        return None
    every = specctra._net_names(b)
    chosen: list[str] = []
    for n in nets or []:
        if any(c in n for c in "*?["):
            hit = sorted(fnmatch.filter(every, n))
            if not hit:
                raise env.KatError(f"no net matches {n!r}")
            chosen += hit
        else:
            chosen.append(n)
    if classes:
        of = {n: b.net_rules(n)["netclass"] for n in every}
        unknown = sorted(set(classes) - set(of.values()))
        if unknown:
            raise env.KatError(f"no net is in class {', '.join(unknown)}: {', '.join(sorted(set(of.values())))}")
        chosen += sorted(n for n in every if of[n] in classes)
    return list(dict.fromkeys(chosen))


def _nets_without(b: Board, nets: list[str] | None, classes: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """``nets`` (default all) without those in net classes ``classes``, and what was left out."""
    every = specctra._net_names(b)
    of = {n: b.net_rules(n)["netclass"] for n in every}
    unknown = sorted(set(classes) - set(of.values()))
    if unknown:
        raise env.KatError(f"no net is in class {', '.join(unknown)}: {', '.join(sorted(set(of.values())))}")
    keep = [n for n in (nets if nets is not None else every) if of.get(n) not in classes]
    return keep, {c: sorted(n for n in every if of[n] == c) for c in classes}


def route(pcb: Path | str, passes: int = 20, nets: list[str] | None = None, timeout: int = 1800,
          out: Path | str | None = None, skip_classes: list[str] | None = None, optimize: bool = True,
          options: dict | None = None, classes: list[str] | None = None) -> dict:
    """Autoroute the board: DSN -> Freerouting -> SES -> new tracks and vias -> save ->
    KiCad DRC. Locked copper is kept as it is; ``nets`` (names or globs) and ``classes`` (net
    classes) restrict routing to those nets, and ``skip_classes`` leaves out net classes, whose
    pads stay obstacles. On a library block's board
    the harness parts are obstacles only (not routed to). ``timeout`` (s) is the router's own limit:
    it stops there and what it has is kept, with ``timed_out`` in the result. ``optimize=False`` skips
    its optimizer stage. Necks are off unless ``options`` has {"automatic_neckdown": True}.
    ``options``: other Freerouting settings ({"optimizer.max_passes": 2}); some it ignores or kat sets
    itself, which are refused. ``settings`` says which it refused and which
    kat has not seen work. DSN, SES and the router log go to ``out`` (default:
    history/<stamp>_route/). Also returns ``stages`` (seconds) and ``unrouted`` per pass."""
    pcb = Path(pcb)
    extra, unverified = _option_args(options)
    if not optimize:
        extra.append("--router.optimizer.enabled=false")
    jar = jar_path()
    if not jar.exists():
        raise env.KatError(f"Freerouting not installed: run `kat pcb route --install` ({URL})")
    exe = java()
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    work = Path(out) if out else pcb.parent / "history" / f"{stamp}_route"
    work.mkdir(parents=True, exist_ok=True)
    b = Board(pcb)
    nets = select_nets(b, nets, classes)
    skipped = {}
    if skip_classes:
        nets, skipped = _nets_without(b, nets, list(skip_classes))
    dsn, ses = work / f"{pcb.stem}.dsn", work / f"{pcb.stem}.ses"
    # a library block's board: its harness is test equipment, not routed to (its routes
    # would not be stamped anyway); its parts stay obstacles
    harness = set()
    if pcb.with_suffix(".block.json").exists() and pcb.stem == pcb.resolve().parent.name:
        from ..blocks import _env_refs
        harness = _env_refs(pcb.resolve().parent)
    info = specctra.write_dsn(b, dsn, nets, harness)
    if harness:
        info["harness_not_routed"] = sorted(harness)
    tight = fine_pitch(b, nets)
    t0 = time.time()
    log_file = work / "freerouting.log"
    lines = []
    with open(log_file, "w", encoding="utf-8") as logf:
        # streamed: the log grows while it runs, and each pass is echoed on stderr
        proc = subprocess.Popen([exe, "-Djava.awt.headless=true", "-jar", str(jar), "--gui.enabled=false", NO_ANALYTICS,
                                 *([] if any("automatic_neckdown=" in a for a in extra) else [NO_NECKDOWN]),
                                 "-de", str(dsn.resolve()), "-do", str(ses.resolve()),
                                 f"--router.autorouter.max_passes={passes}",
                                 f"--router.job_timeout={timeout // 3600:02d}:{timeout % 3600 // 60:02d}:{timeout % 60:02d}",
                                 *extra],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, cwd=work)
        deadline = t0 + timeout + _GRACE
        import threading
        timer = threading.Timer(timeout + _GRACE, proc.kill)
        timer.start()
        try:
            for line in proc.stdout:
                lines.append(line)
                logf.write(line)
                logf.flush()
                # the optimizer can take minutes a pass: say so rather than look stuck
                m = re.search(r"(Auto-routing pass #\d+.*?\(\d+ unrouted and \d+ violations\)"
                              r"|Optimizer pass #\d+: optimizer score [\d.]+ -> [\d.]+)", line)
                if m:
                    print(f"route: {m.group(1)}", file=sys.stderr, flush=True)
            proc.wait()
        finally:
            timer.cancel()
        if time.time() >= deadline and proc.returncode != 0:
            raise env.KatError(f"Freerouting did not finish within {timeout} s (log in {work})")
    log = "".join(lines)
    seconds = round(time.time() - t0, 1)
    if not ses.exists():
        raise env.KatError(f"Freerouting wrote no session (exit {proc.returncode}); see {work}/freerouting.log")
    result = specctra.read_ses(ses)
    applied = apply_session(b, result)
    b.save()
    score = re.findall(r"\((\d+) unrouted and (\d+) violations\)", log)
    c = checks.run(pcb)
    said = read_log(log)
    settings = {}
    if said["failed"]:
        settings["refused_by_router"] = said["failed"]
    bad = {f["option"].removeprefix("router.") for f in said["failed"]}
    if [k for k in unverified if k not in bad]:
        settings["unverified"] = [k for k in unverified if k not in bad]
    return {"dir": str(work), "dsn": info, "seconds": seconds, "passes": passes,
            "router": {"unrouted": int(score[-1][0]), "violations": int(score[-1][1])} if score else None,
            "stages": said["stages"], "unrouted": said["unrouted"],
            **({"timed_out": True} if said["timed_out"] else {}), **({"settings": settings} if settings else {}),
            **({"skipped": skipped} if skipped else {}),
            **applied, **({"fine_pitch": tight} if tight else {}),
            "check": {k: c[k] for k in ("ok", "clean", "drc_errors", "drc_warnings", "unconnected",
                                        "parity", "islands", "by_type")}}


def apply_session(b: Board, result: dict) -> dict:
    """Add the router's new wires and vias (``specctra.read_ses``) to the board. Freerouting
    narrows tracks at fine-pitch pads ("neck-down") below the net class width; those are
    widened where the clearance allows, checked against everything the router added, or else to
    the board's minimum track unless that would touch other copper (``narrow`` gives the width). With
    ``route_vias(through=False)`` a net whose new copper uses another via gets none of it: it
    stays unconnected, listed in ``disallowed_vias``. New vias closer to another hole than the
    board's hole-to-hole minimum (same-net ones too, which the router does not keep apart) are
    listed in ``hole_to_hole``; DRC judges them. Returns ``added_tracks``, ``added_vias``,
    ``widened`` and the ``narrow`` ones left."""
    have = {_key(t["start"], t["end"], t["layer"]) for t in b.tracks()}
    copper = b.copper_layers()
    have_vias = {(round(v["at"][0], 3), round(v["at"][1], 3), v["span"][0], v["span"][-1]) for v in b.vias()}
    setting = b.route_settings().get("vias", {})
    kinds = {tuple(s["layers"]): s["kind"] for s in setting.get("spans", [])}

    def span_of(v) -> tuple[str, str]:
        top, bottom = v.get("span") or (0, len(copper) - 1)
        return copper[top], copper[bottom]

    refused = []
    if not setting.get("through", True):
        refused = [{"net": v["net"], "at": list(v["at"]), "layers": list(span_of(v))} for v in result["vias"]
                   if span_of(v) not in kinds and (round(v["at"][0], 3), round(v["at"][1], 3), *span_of(v)) not in have_vias]
    refused_nets = {r["net"] for r in refused}
    added_t = added_v = 0
    close: list[dict] = []
    # everything the router added goes in first, then the widening
    from .structure import _Copper, via_kind
    cu, cl = _Copper(b), b._clearance_fn()
    new_wires = []
    for w in result["wires"]:
        k = _key(w["start"], w["end"], w["layer"])
        if k in have or w["net"] in refused_nets:
            continue
        have.add(k)
        node = b._segment(w["start"], w["end"], w["width"], w["layer"], w["net"], False)
        cu.add_track(w["start"], w["end"], w["width"], w["layer"], w["net"])
        new_wires.append((w, node))
        added_t += 1
    for v in result["vias"]:
        span = span_of(v)
        top, bottom = copper.index(span[0]), copper.index(span[1])
        k = (round(v["at"][0], 3), round(v["at"][1], 3), *span)
        if k in have_vias or v["net"] in refused_nets:
            continue
        have_vias.add(k)
        kind, _ = via_kind(*span, copper, kinds.get(span))
        close += [{"net": v["net"], "at": list(v["at"]), "near": h["what"], "near_net": h["net"], "gap": h["gap"],
                   "need": h["need"]}
                  for h in cu.hole_conflicts(v["at"], v["drill"], copper[top:bottom + 1], kind)]
        b._via(v["at"], v["net"], v["size"], v["drill"], False, span, kind)
        cu.add_via(v["at"], v["size"], v["net"], set(copper[top:bottom + 1]), v["drill"], kind)
        added_v += 1
    widened, narrow = 0, []
    floor = b.board_minimums().get("min_track_width", 0.0) or 0.0
    for w, node in new_wires:
        want = b.net_rules(w["net"])["track"]
        if w["width"] >= want - 1e-6:
            continue
        seg = (w["start"], w["end"])
        if not cu.conflicts(w["net"], {w["layer"]}, cl, seg=(seg, want / 2)):
            find(node, "width")[1] = num(want)
            cu.add_track(w["start"], w["end"], want, w["layer"], w["net"])   # the wider copper counts now
            widened += 1
            continue
        # Under the board's minimum is a sure DRC error, while a clearance this check doubts may pass (it can
        # also fail): so up to the minimum, unless that would touch other copper. DRC judges the result.
        # A board that sets no minimum gets no widening here, though DRC then uses 0.2 mm.
        width = w["width"]
        if width < floor - 1e-6 and all(c.get("gap", 0) > 0 for c in cu.conflicts(w["net"], {w["layer"]}, cl,
                                                                                  seg=(seg, floor / 2))):
            find(node, "width")[1] = num(floor)
            cu.add_track(w["start"], w["end"], floor, w["layer"], w["net"])
            width = floor
        narrow.append({"net": w["net"], "layer": w["layer"], "width": width,
                       "start": list(w["start"]), "end": list(w["end"])})
    return {"added_tracks": added_t, "added_vias": added_v, "widened": widened, "narrow": narrow,
            **({"disallowed_vias": refused} if refused else {}), **({"hole_to_hole": close} if close else {})}


def fine_pitch(b: Board, nets: list[str] | None = None) -> list[dict]:
    """Pads a net's class track cannot leave straight without coming closer than the
    class clearance to the neighbouring pad (fine-pitch parts): Freerouting then necks the
    track down or gives up. One entry per net and part: the class width and the widest
    that fits (``fits``, mm)."""
    from .geometry import poly_seg
    out: dict[tuple, dict] = {}
    for f in b.footprints():
        pads = [p for p in f.pads() if p.net]
        for p in pads:
            if nets is not None and p.net not in nets:
                continue
            r = b.net_rules(p.net)
            near = [poly_seg(q.outline, (p.x, p.y), (p.x, p.y)) for q in pads
                    if q.net != p.net and any(p.on(l) and q.on(l) for l in b.copper_layers())]
            if not near:
                continue
            fits = round(2 * (min(near) - r["clearance"]), 3)
            if r["track"] > fits + 1e-6:
                k = (p.net, f.ref)
                if k not in out or fits < out[k]["fits"]:
                    out[k] = {"net": p.net, "part": f.ref, "netclass": r["netclass"],
                              "width": r["track"], "fits": max(fits, 0.0)}
    return sorted(out.values(), key=lambda e: (e["part"], e["net"]))
