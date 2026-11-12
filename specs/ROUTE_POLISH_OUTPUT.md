# Route polisher output formats

**Version 1 - 2026-11-12.** This document covers the files that
`kat pcb polish run` writes into its run directory, and that `replay` and
`apply` read. The input is specified in [`ROUTE_POLISH_INPUT.md`](ROUTE_POLISH_INPUT.md).
Units and axes are the same as the input's: millimetres, x right, y up. A run
directory holds:

| File | Content |
|---|---|
| `run.json` | Run record: input identity, effective options, solver identity, termination. |
| `history.jsonl` | One event per line, in order. This file is enough to replay the inspection. |
| `result.json` | The exported candidate: the best feasible state unless another event is chosen. |

A run never writes outside its run directory and refuses a directory that
already contains any of these files unless forced. A run on a private extract is
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
  `max_attempts_per_segment`, `max_substeps` and `barrier_margin_mm`.
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
| `type` | `start`, `step`, `snap`, `adjust`, `bend`, `merge`, `commit`, `stop`. |
| `accepted` | Whether the event's candidate became the current state. |
| `reason` | Why a candidate was rejected or a trial was reverted, else `null`. Examples: `sweep`, `clearance t02#3-h01`, `displacement`, `vertex limit`, `merit worse`, `no gain`, `inconsistent directions`. |
| `target` | The segment the event is about, `"<trace>#<index>"`, else `null`. |
| `traces` | `{trace id: points}`: the **current accepted state after** the event, for every trace in the group. |
| `snapped` | `{trace id: [segment indices]}` for that state. |
| `candidate` | `{trace id: points}` of a rejected candidate, so that it can be inspected. Omitted when the event was accepted. |
| `terms` | Merit terms of the current state: `angle`, `spacing`, `edge`, `displacement`, `length`, `complexity`. These are unweighted and dimensionless. |
| `total` | Weighted sum of the terms. |
| `min_slack` | `{"value": mm, "a": ref, "b": ref}`: the smallest clearance margin (distance minus required distance) in the current state, and its pair of objects. |

Segment indices refer to the geometry stored in the same event.

Trials are bracketed. A `snap`, `adjust` or `bend` event starts a trial,
`step` events settle it, and a `commit` event ends it. `commit` with
`accepted: false` restores the state from before the trial, and its `traces`
holds that restored state. `step` events with `accepted: false` record a
proposal that every backtracking factor failed to make feasible.

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
A result says that its traces passed the polisher's own hard check. It is not
DRC and not a board-level safety certificate.
