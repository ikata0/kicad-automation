# Route polisher output formats

**Version 1 - 2026-11-12** (report: 2026-11-16). This document covers the files that
`kat pcb polish run` writes into its run directory, and that `replay` and
`apply` read. The input is specified in [`ROUTE_POLISH_INPUT.md`](ROUTE_POLISH_INPUT.md).
Units and axes are the same as the input's: millimetres, x right, y up. A run
directory holds:

| File | Content |
|---|---|
| `run.json` | Run record: input identity, effective options, solver identity, termination. |
| `history.jsonl` | One event per line, in order. This file is enough to replay the inspection. |
| `result.json` | The exported candidate: the best feasible state unless another event is chosen. |
| `report.md`, `report/*.png` | Headless runs only, unless `--no-report`: a readable account of the run with pictures (§4). |

A run with an explicit `--out DIR` writes only in that directory. Without `--out`,
`CASE.json` writes the candidate to `CASE-polished.json` beside the input and keeps
these run files in the sibling `CASE-polished` directory. Existing result files or
run files are refused unless forced; the GUI writes only on Export.
A run on a private extract is
itself private. This covers its history, screenshots and logs (input §3).

## 1 `run.json`

```json
{
  "format": "route-polish-run", "version": 1,
  "input": {"sha256": "…", "format_version": 2},
  "group": "g01",
  "solver": {"name": "kat-polish", "version": "0.1", "merit": 1, "torch": "2.15.0"},
  "options": {"objectives": {…}, "limits": {…}, "solver": {…}},
  "termination": "all-snapped",
  "events": 412,
  "best_seq": 398
}
```

- `input.sha256` is the hash of the input file's bytes. No path is stored.
- `options` holds the **effective** values after command-line overrides.
  `objectives` and `limits` use the input's field names. `solver` holds the
  solver's own knobs: `learning_rate`, `settle_iterations`,
  `stagnation_window`, `stagnation_tolerance`, `snap_slack`, `bend_gain`,
  `max_attempts_per_segment`, `max_substeps`, `barrier_margin_mm`,
  `adjust_boost` and `blocked_bend_after` (defaults and meanings:
  `kat pcb polish run --help`, set with `--option KEY=V`).
- `merit` is the version of the merit definition. Scores are only comparable
  between runs that share the same `solver.name`, `version` and `merit`.
- `termination` is one of `all-snapped`, `stagnated`, `max-iterations`,
  `user-stop`, or `paused` for an export from the inspection window before the
  run ended.

## 2 `history.jsonl`

Every line is one event object:

| Field | Meaning |
|---|---|
| `seq` | 0, 1, 2, …: position in the history. |
| `iter` | Solver iterations (optimiser steps) so far. |
| `type` | `start`, `step`, `snap`, `adjust`, `bend`, `rescue`, `merge`, `simplify`, `commit`, `stop`. A `simplify` moves the tip of a doubled-back detour (turn of at least `reversal_turn_deg`) onto its nearer neighbour and drops it; `target` names the tip vertex. A `bend` replaces the segment by an already-bent shape (corner, then Z, each both ways; legs on 45-degree multiples, not snapped) or, last, splits it at a free collinear midpoint; an accepted `bend` names its shape in `reason` (`corner 0/45`, `Z 0/45/0`, `midpoint`), a blocked one gives the shape and why (`corner 0/45: sweep`), and the trials of one segment follow each other until one is kept. A `rescue` inserts a free collinear vertex where a segment comes closest to the obstacle it is short of (or keeps running into). |
| `accepted` | Whether the event's candidate became the current state. |
| `reason` | Why a candidate was rejected or a trial was reverted, the shape of an accepted `bend` (`rescue`: `collinear`), else `null`. Examples: `sweep`, `clearance t02#3-h01`, `vertex limit`, `merit worse`, `no gain`, `inconsistent directions`. |
| `target` | The segment the event is about, `"<trace>#<index>"`, else `null`. |
| `traces` | `{trace id: points}`: the **current accepted state after** the event, for every trace in the group. |
| `snapped` | `{trace id: [segment indices]}` for that state: sorted, unique indices from 0 to `len(points) - 2`, not direction codes. |
| `added_vertices` | `{trace id: [vertex indices]}` of automatically inserted vertices still present in that state, including during trials. Sorted, unique interior indices; follow movement and index changes, and disappear on merge, simplify or trial rollback. Optional in older histories, which have no insertion markers. |
| `candidate` | `{trace id: points}` of a rejected candidate, so that it can be inspected. Omitted when the event was accepted. |
| `terms` | Merit terms of the current state: `angle`, `spacing`, `edge`, `displacement`, `length`, `complexity`. These are unweighted and dimensionless. |
| `total` | Weighted sum of the terms. |
| `min_slack` | `{"value": mm, "a": ref, "b": ref}`: the smallest clearance margin (distance minus required distance) in the current state, and its pair of objects. |
| `shortfall` | How far the current state is short of clearance, in mm summed over (trace, obstacle) pairs. It is 0 unless the run started short (recovery). |

Segment indices refer to the geometry stored in the same event.

Trials are bracketed. A `snap`, `adjust`, `bend` or `rescue` event starts a trial,
`step` events settle it, and a `commit` event ends it. `commit` with
`accepted: false` restores the state from before the trial, and its `traces`
holds that restored state. `step` events with `accepted: false` record a
proposal that every backtracking factor failed to make feasible.
An accepted `bend` or `rescue` event initially preserves the route exactly,
adding only a free vertex. Its child segments have no snapped direction.
Only subsequent settling steps may move that vertex out of line; they are
judged by the same swept constraints as every other move.

`start` holds the input state. `stop` repeats the final state, and its `reason`
is the termination. Because every event stores the full state, a viewer can
move back and forth without re-running the solver.

## 3 `result.json`

```json
{
  "format": "route-polish-result", "version": 1,
  "input_sha256": "…",
  "group": "g01",
  "seq": 398,
  "traces": [{"id": "t01", "points": [[3, 5], …], "snapped": [0, 2, 3]}],
  "score": {"total": 0.41, "terms": {…}},
  "initial_score": {"total": 0.97, "terms": {…}},
  "constraints": {"feasible": true, "min_slack": {"value": 0.012, "a": "t03#2", "b": "t04#2"}},
  "termination": "stagnated",
  "solver": {"name": "kat-polish", "version": "0.1", "merit": 1}
}
```

`seq` is the history event whose state was exported. `apply` refuses a result
whose `input_sha256` does not match the case that the map file belongs to.

`constraints.feasible` is the polisher's own hard check of the exported state.
A run that started short of clearance (recovery) also lists the start's
failures as `initial_violations`. A state still short lists `violations` and
has `feasible: false`. The best state is then the least short one, not
a feasible one.
A result says that its traces passed the polisher's own hard check. It is not
DRC and not a board-level safety certificate.

## 4 `report.md`

A run without `--gui` also writes `report.md` and the pictures it links in
`report/`, unless `--no-report` is given. The report is derived from the case,
`run.json`, `history.jsonl` and `result.json`. Nothing reads it back.
`replay DIR --case CASE --report` (re)writes it for any finished run, for example
after an export from the window. Like the other run files, an existing `report.md`
is refused unless forced, and a forced run removes a stale report.

Sections:

| Section | Content |
|---|---|
| Run | Case file name and hash, layer, group, solver, termination, iterations, events, exported event, date, wall time. |
| Selection | One row per trace in group order: id, net, width, anchors, length before → after and Δ, vertices, snapped segments, added vertices, largest deviation from the input (mm, sampled every 0.05 mm both ways). Context counts and clearance rules follow. |
| Board | `board.png` and `board_region.png`, or why they are missing. |
| Before | `before.png`, start merit, least slack, and the start's violations when the run recovered. |
| Options | Effective objectives, weights, limits and solver options from `run.json`. |
| Progress | `merit.png`, the accepted/rejected events per type, and the rejection reasons. |
| After | `after.png`, `overlay.png`, merit terms before → after (Δ weighted), the hard-constraint verdict and the remaining violations. |

Pictures (case area: movement region plus 3 mm; colours as in the window):

| File | Shows |
|---|---|
| `before.png` | The input: the selected traces labelled, fixed ends, fixed copper, holes, keep-outs, the dashed movement region. |
| `after.png` | The exported state: snapped segments green, added vertices pink. |
| `overlay.png` | The input faintly under the exported state. |
| `merit.png` | The weighted total over iterations, the clearance shortfall (right axis) when recovering, ticks for accepted snaps, bends, rescues and simplifications, and the exported event circled. |
| `board.png` | Only with the case's `CASE.map.json` beside it, matching the case's hash: a KiCad render (kicad-cli, copper layer and outline) of the whole board, the movement region boxed red, the selected input traces blue. |
| `board_region.png` | The same render around the movement region, with traces labelled by id and real net name. |

The net names in the report come from the map file, so the report and its
pictures of a private extract are private (see the top of this document). If the board changed after the
extraction, the board pictures show its current state, and the report says so.
