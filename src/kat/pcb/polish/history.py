"""A polishing run's directory (``specs/ROUTE_POLISH_OUTPUT.md``): ``run.json`` (identity,
options, termination), ``history.jsonl`` (one event per line) and ``result.json`` (the
exported candidate). With no explicit run directory, a copy of the result is also written
beside the input. Existing files are never overwritten unless forced.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...env import KatError

FILES = ("run.json", "history.jsonl", "result.json")


def prepare(out: Path | str, force: bool = False, result_file: Path | None = None) -> Path:
    """The run directory ``out``, created; refuses one that holds run files unless
    ``force``. Also checks the optional standalone ``result_file`` before any writes."""
    out = Path(out)
    if result_file is not None and result_file.exists():
        if not result_file.is_file():
            raise KatError(f"{result_file} is not a result file")
        if not force:
            raise KatError(f"{result_file} already exists: choose another directory or force")
    if out.exists() and not out.is_dir():
        raise KatError(f"{out} is a file, not a run directory")
    there = [f for f in FILES if (out / f).exists()]
    if there and not force:
        raise KatError(f"{out} already holds {', '.join(there)}: choose another directory or force")
    out.mkdir(parents=True, exist_ok=True)
    return out


def run_record(solver, events: int, best_seq: int) -> dict:
    """The ``route-polish-run`` record of a finished ``solver``."""
    info = solver.info()
    return {"format": "route-polish-run", "version": 1,
            "input": {"sha256": solver.case.sha256, "format_version": solver.case.version},
            "group": solver.group, **info, "termination": solver.termination, "events": events, "best_seq": best_seq}


def write(out: Path, solver, events, result: dict | None = None, result_file: Path | None = None) -> dict:
    """Run ``solver`` to the end, writing each of ``events`` (its generator) to
    ``history.jsonl`` as it comes, then ``run.json`` and ``result.json`` (the best feasible
    state unless ``result`` is given), also copied to ``result_file`` when supplied.
    Returns the run record."""
    n = 0
    with open(out / "history.jsonl", "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev.to_json()) + "\n")
            n += 1
    res = result or solver.result()
    rec = run_record(solver, n, res["seq"])
    (out / "result.json").write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    if result_file is not None:
        result_file.write_text(json.dumps(res, indent=1) + "\n", encoding="utf-8")
    (out / "run.json").write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")
    return rec


def load(run_dir: Path | str) -> tuple[dict, list[dict], dict | None]:
    """(run record, events, result or None) of a run directory."""
    d = Path(run_dir)
    try:
        rec = json.loads((d / "run.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (d / "history.jsonl").read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as e:
        raise KatError(f"{d}: not a readable run directory ({e})") from None
    res = d / "result.json"
    return rec, events, json.loads(res.read_text(encoding="utf-8")) if res.exists() else None


def summary(rec: dict, events: list[dict], result: dict | None) -> dict:
    """Counts per event type (accepted / rejected), rejection reasons, the initial and the
    exported score, and for a run that started short of clearance its ``recovery``."""
    kinds: dict = {}
    reasons: dict = {}
    for e in events:
        k = kinds.setdefault(e["type"], {"accepted": 0, "rejected": 0})
        k["accepted" if e["accepted"] else "rejected"] += 1
        if not e["accepted"] and e["reason"]:
            reasons[e["reason"]] = reasons.get(e["reason"], 0) + 1
    out = {"termination": rec["termination"], "iterations": events[-1]["iter"] if events else 0,
           "events": kinds, "rejections": reasons}
    if result:
        out["initial"] = result["initial_score"]
        out["result"] = {"seq": result["seq"], **result["score"], "min_slack": result["constraints"]["min_slack"]}
        cons = result["constraints"]
        if cons.get("initial_violations"):
            short = {e["seq"]: e.get("shortfall", 0.0) for e in events}
            out["recovery"] = {"recovered": cons["feasible"], "shortfall_start": events[0].get("shortfall", 0.0) if events else None,
                               "shortfall_result": short.get(result["seq"]), "initial_violations": len(cons["initial_violations"]),
                               "violations": cons.get("violations", [])}
    return out


__all__ = ["FILES", "load", "prepare", "run_record", "summary", "write"]
