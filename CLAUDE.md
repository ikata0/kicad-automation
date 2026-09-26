# kicad-automation

Command-line tooling for agent-driven KiCad 10 work. Package `kat` in `src/kat/` (board side
in `src/kat/pcb/`), skills in `skills/` (kicad-design, kicad-schematic, kicad-layout).

- Dev setup: `python -m venv .venv && .venv/bin/pip install -e '.[dev]'`; tests: `.venv/bin/pytest`
  (most tests need `kicad-cli`).
- Experiments that produce KiCad projects belong in `../kicad-sandbox/experiments/`, not here.
- `sexpr.py` must stay lossless: never drop or reorder tokens it does not know.
- Geometry changes must be checked against KiCad's netlist (see `test_pin_positions_all_orientations`),
  not only against our own model. Board geometry: against IPC-D-356 and DRC's library check
  (`tests/test_pcb_geometry.py`).
- Library first: logic lives in the library (`kat.Sheet`, `kat.*` functions); `cli.py` only
  parses arguments and calls it. New commands get a library function or method first.
- CLI output is JSON only; errors are `{"error": ...}` with exit code 2.
- The workflow and sync semantics are specified in `SPEC.md`; keep it, the skill and
  `kat --help` texts in sync when behaviour changes. New pitfalls go into SPEC.md §7.
- Board changes are judged by KiCad's DRC (`kat pcb check`), never by our own geometry or
  the router's count; routing tests need Freerouting (`kat pcb route --install`).
- `tests/test_e2e_skidl.py` is the full iterative flow; run it with `KAT_E2E_OUT=<dir>` and
  look at the PNGs when changing sync, move or field placement — the checks are heuristics.
