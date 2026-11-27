"""A stack of boards in 3D: every board pair checked for collisions and near misses, the
mating connectors for seating.

A stack file (JSON) names the boards; each one but the first lies on a parent board, placed
as for ``kat.pcb.mate`` (``at``, ``rot``, ``flip``, ``below``) plus ``gap``: the distance
between the facing board surfaces, i.e. the connectors' mated height or the standoff length::

    {"boards": {
       "main": {"pcb": "main/main.kicad_pcb"},
       "io":   {"pcb": "io/io.kicad_pcb", "on": "main", "at": [12.5, 10], "flip": true,
                "gap": 8.5, "pairs": [["J1", "P1"]]}},
     "clearance": 0.5, "allow": [["main/H*", "io/SCREW*"]]}

Each board is exported by KiCad (``kicad-cli pcb export step``); kat writes a cqx model
(``stack.py`` reading ``stack.json``) that loads the exports, places them and declares what
to check, and ``cqx inspect`` judges it. kat itself imports no CAD kernel.

KiCad's STEP frame is the board's (x, -y); z is 0 on the bottom of the board body, which is
the dielectric (and inner copper) only. KiCad seats a model's z = 0 at the outer copper plus
``SEAT`` on either side; those seats are the surfaces ``gap`` is measured between, since the
library models are drawn from them.
"""

from __future__ import annotations

import hashlib
import math
import json
import re
import subprocess
from pathlib import Path

from .. import env, report
from .assembly_check import footprint_flags
from .board import Board
from .layers import stackup
from .mating import mate

SEAT = 0.05            # mm beyond the outer copper where KiCad 10 seats a model (tests/test_pcb_stack3d.py)
CLEARANCE = 0.5        # mm, default
RENDER_FINDINGS = 4    # close-ups rendered at most
_KEY = re.compile(r"^[A-Za-z0-9_.-]+$")
_ROOT_KEYS = {"pcb", "variant"}
_CHILD_KEYS = _ROOT_KEYS | {"on", "at", "rot", "flip", "below", "gap", "pairs", "nets", "holes",
                            "tolerance", "hole_tolerance", "engage"}
_NAUO = re.compile(r"NEXT_ASSEMBLY_USAGE_OCCURRENCE\s*\(\s*'[^']*'\s*,\s*'([^']*)'")


def _pairs(items, what: str) -> list[tuple[str, str]]:
    out = []
    for p in items or []:
        if isinstance(p, str) and "=" in p:
            p = p.split("=", 1)
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise env.KatError(f"{what}: {p!r} is not a pair [A, B] or 'A=B'")
        out.append((str(p[0]), str(p[1])))
    return out


def load_stack(stack: dict | Path | str) -> dict:
    """The stack file (or dictionary) checked and normalized: ``boards`` {key: entry} with
    ``pcb`` resolved against the file's folder, ``order`` (parents before their children),
    ``root``, ``clearance`` (mm) and ``allow`` (``"a:b"`` glob pairs on ``key/REF``)."""
    base = Path.cwd()
    if not isinstance(stack, dict):
        path = Path(stack)
        try:
            stack = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise env.KatError(f"stack file {path}: {e}") from e
        base = path.resolve().parent
    raw = stack.get("boards")
    if not isinstance(raw, dict) or len(raw) < 2:
        raise env.KatError("a stack needs 'boards' with at least two entries")
    boards: dict[str, dict] = {}
    for key, e in raw.items():
        if not _KEY.match(key):
            raise env.KatError(f"board key {key!r}: letters, digits, '_', '.' and '-' only")
        if not isinstance(e, dict) or "pcb" not in e:
            raise env.KatError(f"board {key!r} needs 'pcb'")
        unknown = set(e) - (_CHILD_KEYS if "on" in e else _ROOT_KEYS)
        if unknown:
            raise env.KatError(f"board {key!r}: unknown keys {sorted(unknown)}")
        pcb = Path(e["pcb"])
        pcb = pcb if pcb.is_absolute() else base / pcb
        if not pcb.exists():
            raise env.KatError(f"board {key!r}: {pcb} not found")
        b = {"pcb": pcb, "variant": e.get("variant"), "on": e.get("on")}
        if b["on"] is not None:
            if "gap" not in e:
                raise env.KatError(f"board {key!r} needs 'gap' (mm between the facing board surfaces)")
            at = e.get("at", [0, 0])
            if not isinstance(at, (list, tuple)) or len(at) != 2:
                raise env.KatError(f"board {key!r}: 'at' must be [x, y]")
            holes = e.get("holes", "auto")
            engage = e.get("engage", "flush")
            if engage not in ("flush", "touch"):
                raise env.KatError(f"board {key!r}: 'engage' is flush (housings seated on each other) or touch")
            b.update(at=(float(at[0]), float(at[1])), rot=float(e.get("rot", 0.0)),
                     flip=bool(e.get("flip", False)), below=bool(e.get("below", False)),
                     gap=float(e["gap"]), pairs=_pairs(e.get("pairs"), f"board {key!r} pairs"),
                     nets=dict(e.get("nets") or {}),
                     holes=holes if holes in ("auto", "none", None) else _pairs(holes, f"board {key!r} holes"),
                     tolerance=float(e.get("tolerance", 0.05)),
                     hole_tolerance=float(e.get("hole_tolerance", 0.1)), engage=engage)
        boards[key] = b
    roots = [k for k, b in boards.items() if b["on"] is None]
    if len(roots) != 1:
        raise env.KatError(f"a stack has exactly one board without 'on', not {roots or 'none'}")
    order, todo = [roots[0]], [k for k in boards if k != roots[0]]
    while todo:
        ready = [k for k in todo if boards[k]["on"] in order]
        if not ready:
            bad = [k for k in todo if boards[k]["on"] not in boards]
            raise env.KatError(f"board {bad[0]!r} lies on unknown board {boards[bad[0]]['on']!r}" if bad
                               else f"boards {todo} lie on each other in a loop")
        order += ready
        todo = [k for k in todo if k not in ready]
    clearance = float(stack.get("clearance", CLEARANCE))
    allow = [f"{a}:{b}" for a, b in _pairs(stack.get("allow"), "allow")]
    return {"boards": boards, "order": order, "root": roots[0], "clearance": clearance, "allow": allow}


def frame_2d(at, rot: float, flip: bool) -> tuple[list[float], list[float]]:
    """``mating.transform`` in KiCad's STEP frame (x, -y): matrix [a, b, c, d] (row major) and
    offset [tx, ty] such that a child point (x, y) lands on (a x + b y + tx, c x + d y + ty)."""
    # transform: mirror x if flip, then rotate (x c + y s, -x s + y c), then add at; in KiCad's
    # frame (y down). The STEP frame negates y on both sides: M_S = N M_K N, t_S = N at.
    a = math.radians(rot % 360)
    c, s = (1.0, 0.0) if not rot % 360 else (math.cos(a), math.sin(a))
    f = -1.0 if flip else 1.0
    mk = [c * f, s, -s * f, c]                      # [[c, s], [-s, c]] @ diag(f, 1)
    m = [mk[0], -mk[1], -mk[2], mk[3]]
    return [v + 0.0 for v in m], [float(at[0]), -float(at[1])]


def step_instances(step: Path | str) -> set[str]:
    """Instance names in a STEP file (KiCad: the reference designators), read from its text."""
    return set(_NAUO.findall(Path(step).read_text(encoding="utf-8", errors="replace")))


def mounted_refs(b: Board, variant: str | None = None) -> list[str]:
    """References of the footprints that are fitted and should therefore have a 3D model:
    not DNP (in ``variant``) and not excluded from the BOM (KiCad's holes, fiducials and logos
    are). Board-only parts count: a heat sink placed on the board alone is fitted too."""
    out = []
    for f in b.footprints():
        flags = footprint_flags(f.node, variant)
        if not (flags["dnp"] or flags["exclude_from_bom"]) and f.ref:
            out.append(f.ref)
    return out


def export_step(pcb: Path, variant: str | None, step: Path) -> tuple[Path, bool]:
    """KiCad's STEP export of a board to ``step`` (reused while the board file and the
    options are unchanged); returns the file and whether it was exported now."""
    args = ["--no-dnp", "--subst-models", "--user-origin", "0x0mm"] + (["--variant", variant] if variant else [])
    digest = hashlib.sha256(pcb.read_bytes() + json.dumps(args).encode()).hexdigest()
    stamp = step.with_suffix(".sha256")
    if step.exists() and stamp.exists() and stamp.read_text(encoding="utf-8").strip() == digest:
        return step, False
    env.run_cli("pcb", "export", "step", "-f", *args, "-o", str(step), str(pcb))
    stamp.write_text(digest + "\n", encoding="utf-8")
    return step, True


_MODEL = '''"""The stack of boards as kat placed it (kat pcb mate --stack). Generated: change the stack
file and run kat again instead of editing this. Placement and checks come from stack.json."""
import json
from pathlib import Path

import cadquery as cq
from OCP.gp import gp_Trsf

from cqx.loader import exact_bbox, step_assembly

PLAN = json.loads(Path(__file__).with_name("stack.json").read_text(encoding="utf-8"))
EXPECTED_INTERFERENCE = PLAN["expected_interference"]
EXPECTED_CONTACT = PLAN["expected_contact"]
INTERFERENCE_BETWEEN = PLAN["between"]
CLEARANCE = PLAN["clearance"]


def _local(e, parent_seats, seats):
    """The board in its parent's frame: the 2D placement, turned over about y when flipped,
    lifted so its facing seat lies ``gap`` from the parent's."""
    (top, bot), (btop, bbot) = parent_seats, seats
    flip, gap = e["flip"], e["gap"]
    if not e["below"]:
        dz = top + gap + btop if flip else top + gap - bbot
    else:
        dz = bot - gap + bbot if flip else bot - gap - btop
    (a, b, c, d), (tx, ty) = e["m"], e["t"]
    t = gp_Trsf()
    t.SetValues(a, b, 0.0, tx, c, d, 0.0, ty, 0.0, 0.0, -1.0 if flip else 1.0, dz)
    return t


def build():
    stack = cq.Assembly(name="stack")
    world, seats = {}, {}
    for e in PLAN["boards"]:
        board = step_assembly(e["step"])
        sub = cq.Assembly(name=e["key"])
        body = None
        for ch in board.children:
            name = ch.name
            if name == e["body"]:
                name, body = "body", ch
            sub.add(ch, name=name, loc=ch.loc)
        if body is None:
            raise ValueError(f"no board body {e['body']!r} in {e['step']}")
        bb = exact_bbox(body.obj.moved(body.loc))
        seats[e["key"]] = (bb.zmax + e["cu_top"] + PLAN["seat"], bb.zmin - e["cu_bot"] - PLAN["seat"])
        if e["on"] is None:
            world[e["key"]] = gp_Trsf()
        else:
            world[e["key"]] = world[e["on"]].Multiplied(_local(e, seats[e["on"]], seats[e["key"]]))
        stack.add(sub, name=e["key"], loc=cq.Location(world[e["key"]]))
    return stack
'''


def _plan(st: dict, steps: dict[str, Path]) -> dict:
    boards = []
    for key in st["order"]:
        b = st["boards"][key]
        su = stackup(Board(b["pcb"]))
        e = {"key": key, "on": b["on"], "step": str(steps[key]), "body": f"{b['pcb'].stem}_PCB",
             "cu_top": su["copper"][0]["thickness"], "cu_bot": su["copper"][-1]["thickness"]}
        if b["on"] is not None:
            m, t = frame_2d(b["at"], b["rot"], b["flip"])
            e.update(m=m, t=t, flip=b["flip"], below=b["below"], gap=b["gap"])
        boards.append(e)
    links = [(f"{b['on']}/{ra}", f"{k}/{rb}", "|" if b["engage"] == "flush" else ":")
             for k, b in st["boards"].items() if b["on"] for ra, rb in b["pairs"]]
    keys = st["order"]
    return {"boards": boards, "seat": SEAT, "clearance": st["clearance"],
            "expected_interference": [f"{a}:{b}" for a, b, _ in links] + st["allow"],
            "expected_contact": [f"{a}{sep}{b}" for a, b, sep in links],
            "between": [f"{a}/*:{b}/*" for i, a in enumerate(keys) for b in keys[i + 1:]]}


def _cqx(args: list[str], cwd: Path) -> dict:
    proc = subprocess.run([env.cqx(), *args, "--json"], cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-12:]
        raise env.KatError(f"cqx {args[0]} failed (exit {proc.returncode}): " + "\n".join(tail)) from None


def _row(r: dict, **extra) -> dict:
    return {"a": r["path_a"], "b": r["path_b"], **extra}


def _render(out: Path, findings: list[dict]) -> list[str]:
    """An overview of the stack and a close-up of the first findings; returns the PNG paths."""
    def paths(res: dict) -> list[str]:
        return [str(Path(out, im["path"])) for im in res.get("images", [])]   # Path(out, abs) is abs

    shots = paths(_cqx(["render", "stack.py", "--views", "front,right,iso", "--up", "z", "--out", "render",
                        "--name", "stack"], out))
    for k, f in enumerate(findings[:RENDER_FINDINGS], 1):
        f["images"] = paths(_cqx(["render", "stack.py", "--views", "front,right", "--up", "z", "--out", "render",
                                  "--focus", f["a"], "--zoom", "0.5", "--highlight", f"{f['a']},{f['b']}",
                                  "--name", f"finding{k}"], out))
        shots += f["images"]
    return shots


def mate_stack(stack: dict | Path | str, clearance: float | None = None, solid: bool = True,
               out: Path | str | None = None, render: bool = True) -> dict:
    """See ``kat.pcb.mate_stack``."""
    st = load_stack(stack)
    if clearance is not None:
        st["clearance"] = float(clearance)
    for b in st["boards"].values():
        report.touch(b["pcb"])
    links = {}
    for key in st["order"][1:]:
        b = st["boards"][key]
        parent = st["boards"][b["on"]]
        holes = None if b["holes"] in ("none", None) else b["holes"]
        if not b["pairs"] and holes is None:
            links[f"{b['on']}-{key}"] = {"skipped": "no connector pairs or holes to compare"}
            continue
        links[f"{b['on']}-{key}"] = mate(parent["pcb"], b["pcb"], b["pairs"], b["at"], b["rot"], b["flip"],
                                         b["nets"], holes, not b["below"], b["tolerance"], b["hole_tolerance"])
    result: dict = {"links": links, "clearance": st["clearance"]}
    ok = all(v.get("ok", True) for v in links.values())
    if not solid:
        return env.tidy({**result, "ok": ok})

    if out is None:
        out = (Path(stack).with_suffix(".mate") if not isinstance(stack, dict)
               else st["boards"][st["root"]]["pcb"].parent / "stack.mate")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    steps, missing = {}, []
    for key in st["order"]:
        b = st["boards"][key]
        steps[key], _ = export_step(b["pcb"], b["variant"], out / f"{key}.step")
        have = step_instances(steps[key])
        missing += [f"{key}/{r}" for r in mounted_refs(Board(b["pcb"]), b["variant"]) if r not in have]
    (out / "stack.json").write_text(json.dumps(_plan(st, steps), indent=2) + "\n", encoding="utf-8")
    (out / "stack.py").write_text(_MODEL, encoding="utf-8")
    rep = _cqx(["inspect", "stack.py", "--brief"], out)
    rows = rep.get("interference", [])
    collisions = [_row(r, overlap_mm3=r["overlap_volume"]) for r in rows if r["interferes"] and not r["expected"]]
    near = [_row(r, distance=r["min_distance"]) for r in rows if r.get("near") and not r["expected"]]
    pair_rows = {frozenset((r["path_a"], r["path_b"])): r for r in rows}
    connectors, not_seated = [], []
    for c in rep.get("contacts", []):
        a, b = re.split(r"[|:]", c["pair"], maxsplit=1)
        r = pair_rows.get(frozenset((a, b)))
        row = {"a": a, "b": b, "seated": c["ok"], "engage": "flush" if c["kind"] == "flush" else "touch",
               "touching": r is not None and (r["interferes"] or r["min_distance"] == 0),
               "gap": c["gap"] if c["gap"] is not None else (r["min_distance"] if r else None),
               "contact_mm2": c.get("area"), "overlap_mm3": r["overlap_volume"] if r else 0.0}
        if c.get("error"):
            row["error"] = c["error"]
        connectors.append(row)
        if not c["ok"]:
            not_seated.append(row)
    invalid = [p["path"] for p in rep.get("parts", []) if not p.get("valid", True)]
    result.update(collisions=collisions, near=near, not_seated=not_seated, missing_models=missing,
                  connectors=connectors, invalid_models=invalid, model=str(out / "stack.py"))
    result["counts"] = {k: len(result[k]) for k in ("collisions", "near", "not_seated", "missing_models")}
    result["ok"] = ok and not any(result["counts"].values())
    if render:
        result["3d"] = _render(out, collisions + near + not_seated)
        report.snapshot_images(st["boards"][st["root"]]["pcb"], {"images": [{"png": p} for p in result["3d"]]},
                               "kat pcb mate --stack", "stack")
    return env.tidy(result)
