"""``report.md`` of a polishing run (``specs/ROUTE_POLISH_OUTPUT.md`` §4): what was selected,
how it looked before, how the run went and what came out, with pictures in ``report/``.
Built from the run directory and its case alone, so ``replay --report`` can (re)write it
for any finished run. Not to be confused with ``kat.report``, the project journal.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

from ...env import KatError
from . import history as _hist, picture

IMAGES = ("board", "board_region", "before", "after", "overlay", "merit")
DIR = "report"


def remove(run_dir: Path) -> None:
    """Delete a report (``report.md`` and its pictures) from ``run_dir``."""
    d = Path(run_dir)
    (d / "report.md").unlink(missing_ok=True)
    for name in IMAGES:
        (d / DIR / f"{name}.png").unlink(missing_ok=True)
    if (d / DIR).is_dir() and not any((d / DIR).iterdir()):
        (d / DIR).rmdir()


def _length(pts) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def _samples(pts, step: float = 0.05):
    for a, b in zip(pts, pts[1:]):
        n = max(1, math.ceil(math.dist(a, b) / step))
        for i in range(n):
            yield a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n
    yield pts[-1]


def _point_seg(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    u = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - u * dx, p[1] - a[1] - u * dy)


def deviation(a, b) -> float:
    """How far polylines ``a`` and ``b`` lie apart at most (mm; symmetric, sampled every
    0.05 mm): the largest distance a point of one has from the other."""
    def one(p, q):
        return max(min(_point_seg(s, u, v) for u, v in zip(q, q[1:])) for s in _samples(p))
    return max(one(a, b), one(b, a))


def _map_of(case_path: Path | None, case) -> tuple[dict | None, str | None]:
    """The private map file beside the case, when it belongs to it, else (None, why)."""
    from .board import map_path
    if case_path is None:
        return None, "case path unknown"
    mp = map_path(case_path)
    if not mp.exists():
        return None, f"no map file {mp.name} beside the case (a portable case has none)"
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return None, f"{mp.name} unreadable ({e})"
    if m.get("format") != "route-polish-map" or m.get("case_sha256") != case.sha256:
        return None, f"{mp.name} belongs to another case"
    return m, None


def _table(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _f(v, n: int = 4) -> str:
    return f"{v:.{n}f}" if isinstance(v, (int, float)) else str(v)


def write(run_dir: Path | str, case, case_path: Path | str | None = None, duration_s: float | None = None) -> dict:
    """Write ``report.md`` and its pictures (``report/before.png``, ``after.png``,
    ``overlay.png``, ``merit.png``; ``board.png`` and ``board_region.png`` when the map
    file beside ``case_path`` is there and kicad-cli can render its board) for the finished
    run in ``run_dir`` made from ``case`` (checked by sha256). ``duration_s`` is the run's
    wall time in seconds, if known. Returns report (path), images {name: {png}} and
    board_view (null, or why the board pictures are missing)."""
    d = Path(run_dir)
    rec, events, res = _hist.load(d)
    if rec["input"]["sha256"] != case.sha256:
        raise KatError(f"{d} was not made from this case (sha256 differs)")
    if res is None or not events:
        raise KatError(f"{d}: no result to report on")
    remove(d)
    case_path = Path(case_path) if case_path is not None else None
    group = rec["group"]
    ids = list(case.groups[group])
    mapping, why = _map_of(case_path, case)
    nets = {t: mapping["traces"][t]["net"] for t in ids if t in mapping.get("traces", {})} if mapping else {}
    labels = {t: f"{t} {nets[t]}" if t in nets else t for t in ids}
    img_dir = d / DIR
    images: dict[str, dict] = {}

    board_note = None
    if mapping:
        try:
            for k, v in picture.board_views(mapping, case, ids, img_dir, labels).items():
                images[k] = {"png": v["png"]}
        except (KatError, OSError) as e:
            board_note = f"board pictures skipped: {e}"
        else:
            from .case import sha256_of
            board = Path(mapping["board"])
            if sha256_of(board) != mapping.get("board_sha256"):
                board_note = f"{board.name} changed since the extraction: the board pictures show its current state"
    else:
        board_note = f"board pictures skipped: {why}"

    start, best = events[0], events[res["seq"]] if res["seq"] < len(events) else events[-1]
    after = {t["id"]: [tuple(p) for p in t["points"]] for t in res["traces"]}
    snapped = {t["id"]: t["snapped"] for t in res["traces"]}
    added = best.get("added_vertices", {})
    before = {t: case.traces[t].points for t in ids}
    images["before"] = {"png": str(picture.state(case, ids, before, img_dir / "before.png",
                                                 f"before: group {group}, {len(ids)} traces on {case.layer} (input)",
                                                 labels, snapped=start.get("snapped"), color="orig"))}
    images["after"] = {"png": str(picture.state(case, ids, after, img_dir / "after.png",
                                                f"after: event {res['seq']} ({res['termination']})", labels,
                                                snapped=snapped, added=added))}
    images["overlay"] = {"png": str(picture.state(case, ids, after, img_dir / "overlay.png",
                                                  "input (faint) under the result", labels, original=True))}
    images["merit"] = {"png": str(picture.merit_chart(events, img_dir / "merit.png", res["seq"]))}

    summ = _hist.summary(rec, events, res)
    opts = rec.get("options", {})
    weights = opts.get("objectives", {}).get("weights", {})
    private = case.provenance.get("kind") == "private-board-extract"
    rel = lambda name: f"{DIR}/{name}.png"  # noqa: E731
    md = [f"# Route polish report: group {group}", ""]
    if private:
        md += ["> Private: this run was made from a board extract; the report and its pictures show the "
               "board's geometry (and net names, from the map file). Do not publish them.", ""]
    md += ["## Run", "",
           _table(["", ""], [["case", case_path.name if case_path else "-"], ["case sha256", f"`{case.sha256[:16]}…`"],
                             ["layer", case.layer], ["group", f"{group} ({len(ids)} traces)"],
                             ["solver", " ".join(f"{k} {v}" for k, v in rec["solver"].items())],
                             ["termination", rec["termination"]], ["iterations", summ["iterations"]],
                             ["events", rec["events"]], ["exported event", res["seq"]],
                             ["written", dt.datetime.now().strftime("%Y-%m-%d %H:%M")],
                             ["duration", f"{duration_s:.1f} s" if duration_s is not None else "-"]]), ""]

    rows = []
    for t in ids:
        tr = case.traces[t]
        a, b = before[t], after.get(t, before[t])
        la, lb = _length(a), _length(b)
        rows.append([t, nets.get(t, tr.net), _f(tr.width, 3), len(tr.anchors), f"{_f(la, 3)} → {_f(lb, 3)}",
                     f"{lb - la:+.3f}", f"{len(a)} → {len(b)}", f"{len(snapped.get(t, []))}/{len(b) - 1}",
                     len(added.get(t, [])), _f(deviation(a, b), 3)])
    md += ["## Selection", "",
           "The traces of the group, in neighbour order. Lengths and deviation in mm; snapped counts "
           "segments on a 45-degree multiple; added vertices are the solver's bends still present.", "",
           _table(["trace", "net", "width", "anchors", "length before → after", "Δ length", "vertices",
                   "snapped", "added", "max deviation"], rows), "",
           f"Context: {len(case.copper)} fixed copper objects, {len(case.holes)} holes, {len(case.keepouts)} "
           f"keep-outs, {len(case.traces) - len(ids)} other traces. Rules: copper clearance "
           f"{case.copper_clearance} mm, edge {case.edge_clearance} mm, hole {case.hole_clearance} mm"
           + (f", {len(case.pairs)} pair clearances" if case.pairs else "") + ".", ""]

    md += ["## Board", ""]
    if "board" in images:
        md += [f"Where the selection sits (`{Path(mapping['board']).name}`, {case.layer}; movement region in red, "
               "selected traces in blue):", "", f"![board]({rel('board')})", "",
               f"![board region]({rel('board_region')})", ""]
    if board_note:
        md += [f"_{board_note}_", ""]

    ini = res["initial_score"]
    cons = res["constraints"]
    md += ["## Before", "", f"![before]({rel('before')})", "",
           f"Start merit {_f(ini['total'], 5)}; least clearance slack {_f(start['min_slack']['value'])} mm "
           f"({start['min_slack']['a']} - {start['min_slack']['b']})."]
    if cons.get("initial_violations"):
        md += ["", f"The input was short of clearance ({len(cons['initial_violations'])} violations, "
                   f"{_f(start.get('shortfall', 0.0))} mm in total): the run recovered from it.", "",
               *[f"- {v}" for v in cons["initial_violations"][:20]]]
    md += ["", "## Options", "", "Effective values after command-line overrides (`run.json`).", "",
           _table(["", "values"], [[k, ", ".join(f"{n}={v}" for n, v in vals.items() if not isinstance(v, dict))
                                    + (f"; weights: {', '.join(f'{n}={v}' for n, v in vals['weights'].items())}"
                                       if isinstance(vals.get('weights'), dict) else "")]
                                   for k, vals in opts.items() if isinstance(vals, dict)]), ""]

    md += ["## Progress", "", f"![merit]({rel('merit')})", "",
           _table(["event", "accepted", "rejected"],
                  [[k, v["accepted"], v["rejected"]] for k, v in summ["events"].items()]), ""]
    if summ["rejections"]:
        md += ["Rejection reasons:", "",
               _table(["reason", "count"], sorted(([k, v] for k, v in summ["rejections"].items()),
                                                  key=lambda r: -r[1])), ""]

    fin = res["score"]
    terms = [[k, weights.get(k, "-"), _f(ini["terms"].get(k, 0.0), 5), _f(v, 5),
              f"{(v - ini['terms'].get(k, 0.0)) * (weights.get(k, 0) or 0):+.5f}"] for k, v in fin["terms"].items()]
    terms.append(["**total**", "", _f(ini["total"], 5), _f(fin["total"], 5), f"{fin['total'] - ini['total']:+.5f}"])
    ms = cons["min_slack"]
    md += ["## After", "", f"![after]({rel('after')})", "", f"![overlay]({rel('overlay')})", "",
           "Merit terms (unweighted; Δ weighted, negative is better):", "",
           _table(["term", "weight", "before", "after", "Δ weighted"], terms), "",
           f"Hard constraints: {'all hold' if cons['feasible'] else 'NOT all hold'}; least slack "
           f"{_f(ms['value'])} mm ({ms['a']} - {ms['b']})."]
    if cons.get("violations"):
        md += ["", "Still short of clearance:", "", *[f"- {v}" for v in cons["violations"][:20]]]
    md += ["", "This is the polisher's own check, not KiCad's DRC: write the result into a copy with "
               "`kat pcb polish apply` and judge it with `kat pcb check`.", ""]
    (d / "report.md").write_text("\n".join(md), encoding="utf-8")
    order = {k: i for i, k in enumerate(IMAGES)}
    return {"report": str(d / "report.md"), "images": dict(sorted(images.items(), key=lambda kv: order[kv[0]])),
            "board_view": board_note}


__all__ = ["IMAGES", "deviation", "remove", "write"]
