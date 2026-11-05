"""A generic router benchmark: reproducible dense multilayer boards with no design behind them.

A grid of SOIC-16 parts, each pad joined to a pad of a nearby part at random from ``seed``, so
that nets cross. ``build`` makes one board; ``run`` routes it and returns what counts: time, the
router's own count, and KiCad's DRC. From the command line it prints a table, for a study of how
routing scales:

    python tests/e2e/router_benchmark.py OUT_DIR --size 2x2:2 --size 4x4:4
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import kat
from kat.pcb.board import Board
from kat.sexpr import Sym, find

PART = "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm"


def build(pcb: Path | str, rows: int = 3, cols: int = 3, layers: int = 4, pitch: float = 11.0, seed: int = 1,
          classes: bool = False) -> dict:
    """Write the board: ``rows`` x ``cols`` parts at ``pitch`` mm on ``layers`` copper layers. Each net
    joins two pads of parts at most two cells apart. ``classes``: every third net is in class ``SLOW``,
    the others in ``FAST``. Returns nets and pads."""
    pcb = Path(pcb)
    pcb.parent.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)
    b = Board.create(pcb)
    margin = 8.0
    b.outline(rect=(0, 0, 2 * margin + (cols - 1) * pitch, 2 * margin + (rows - 1) * pitch))
    b.set_copper_layers(layers)
    pads = []
    for r in range(rows):
        for c in range(cols):
            f = b.add_board_part(PART, f"U{r * cols + c + 1}", (margin + c * pitch, margin + r * pitch))
            pads += [(r, c, p) for p in f.pads()]
    rnd.shuffle(pads)
    free, names = list(pads), []
    while len(free) > 1:
        r, c, p = free.pop()
        near = [i for i, (r2, c2, _) in enumerate(free) if max(abs(r - r2), abs(c - c2)) <= 2 and (r2, c2) != (r, c)]
        if not near:
            continue
        _, _, q = free.pop(rnd.choice(near))
        name = f"N{len(names) + 1}"
        names.append(name)
        for pad in (p, q):
            pad.node.insert(pad.node.index(find(pad.node, "layers")) + 1, [Sym("net"), name])
    # narrow enough to leave a 1.27 mm pitch, so that only routing can fail
    b.constraints(min_track=0.1, min_clearance=0.1)
    if classes:
        b.netclass("SLOW", track=0.15, clearance=0.15, nets=[n for k, n in enumerate(names) if k % 3 == 0])
        b.netclass("FAST", track=0.15, clearance=0.15, nets=[n for k, n in enumerate(names) if k % 3])
    else:
        b.netclass("SIG", track=0.15, clearance=0.15, nets=names)
    b.save()
    return {"nets": len(names), "pads": len(pads)}


def run(pcb: Path | str, passes: int = 25, timeout: int = 900, **kw) -> dict:
    """Route a built board. Returns seconds, passes the router ran and each pass's unrouted count,
    its own final count, and DRC's verdict."""
    t0 = time.monotonic()
    r = kat.pcb.route(pcb, passes=passes, timeout=timeout, **kw)
    c = r["check"]
    return {"seconds": round(time.monotonic() - t0, 1), "router": r.get("router"), "vias": r.get("added_vias"),
            "tracks": r.get("added_tracks"), "unconnected": c["unconnected"], "drc_errors": c["drc_errors"],
            "by_type": c["by_type"], "islands": c["islands"],
            **{k: r[k] for k in ("stages", "settings", "unrouted", "timed_out") if k in r}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--size", action="append", default=[], metavar="RxC:LAYERS")
    ap.add_argument("--passes", type=int, default=25)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-optimize", action="store_true", help="skip the router's optimizer stage")
    a = ap.parse_args()
    out = Path(a.out)
    results = {}
    for size in a.size or ["2x2:2", "3x3:4", "4x4:4", "6x6:6"]:
        grid, layers = size.split(":")
        rows, cols = (int(v) for v in grid.split("x"))
        pcb = out / size.replace(":", "_") / "bench.kicad_pcb"
        made = build(pcb, rows, cols, int(layers), seed=a.seed)
        try:
            results[size] = {**made, **run(pcb, a.passes, a.timeout, optimize=not a.no_optimize)}
        except kat.KatError as e:
            results[size] = {**made, "error": str(e)[:200]}
        print(json.dumps({size: results[size]}), flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
