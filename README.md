# kicad-automation

Tools for letting an agent (or a script) develop KiCad 10 schematics from the command line,
without MCP and with the KiCad GUI closed.

- `kat` — Python library and CLI (`kat …`, `python -m kat …`): sync a SKiDL design into a `.kicad_sch`, arrange the drawing, check, snapshot (`src/kat/`)
- `skills/kicad-schematic/` — the agent skill for the design → sync → review → arrange loop
- [`SPEC.md`](SPEC.md) — workflow, sync semantics, checks, and the pitfalls found so far

## In one picture

```
SKiDL design ──netlist──► kat sync ──► .kicad_sch ◄── passN.py (arrangement, import kat)
                                           │
                                 kat snapshot / check: ERC, netlist == design,
                                 drawing findings, PNG for review
```

The design owns parts and connections; the drawing owns positions. `kat sync` is
incremental, so a design change touches only the affected spots of an existing arrangement.

## Install

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'     # add ,render on Windows
.venv/bin/pytest                                              # needs kicad-cli; e2e test also SKiDL
```

Requirements: KiCad 10 (`kicad-cli` on PATH, or `KICAD_CLI=/path/to/kicad-cli`), Python ≥ 3.10.
PNG snapshots use `rsvg-convert` if present, otherwise PyMuPDF (`[render]` extra; AGPL-3.0).

## Example

```sh
python tests/e2e/tia_design.py v1 v1.net                  # SKiDL -> netlist
kat new tia/tia.kicad_sch
kat sync tia/tia.kicad_sch v1.net                          # parts + connectivity
kat snapshot tia/tia.kicad_sch --slug import --expect v1.net
python tests/e2e/tia_design.py v2 v2.net                  # design change
kat sync tia/tia.kicad_sch v2.net                          # arrangement kept, changes reported
python tests/e2e/arrange_v2.py tia/tia.kicad_sch         # arrangement pass (Python)
kat snapshot tia/tia.kicad_sch --slug arrange --expect v2.net
```

`tests/test_e2e_skidl.py` runs the full iterative flow (import, design change, re-sync,
arrangement, re-sync); set `KAT_E2E_OUT=dir` to keep its snapshots for inspection.

## Design decisions

- **SKiDL for connectivity, not for drawing** — its own schematic generator is not readable
  for real circuits. atopile was not chosen: it has no schematic at all.
- **SKiDL defines the circuit** — parts and connections exist only in the design code; its
  netlist is the reference.
- **KiCad reads the drawing** — whether the file loads, which nets the drawing actually forms
  and what ERC reports is always determined by `kicad-cli`, never by `kat`'s own geometry
  model. `check --expect` compares the two. Every save goes through `kicad-cli sch upgrade`.
- **Lossless editing** — `kat.sexpr` keeps every token. kiutils drops KiCad 8+ syntax;
  kicad-skip preserves tokens but has not been released since 2024 and lacks the geometry
  this needs.
