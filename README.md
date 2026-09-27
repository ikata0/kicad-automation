# kicad-automation

Tools for letting an agent (or a script) develop KiCad 10 schematics and PCBs from the
command line, without MCP and with the KiCad GUI closed.

- `kat` — Python library and CLI (`kat …`, `python -m kat …`): sync a SKiDL design into a `.kicad_sch`, arrange the drawing, check, snapshot (`src/kat/`)
- `NAME_REPORT.md` — the design journal kat keeps in every project: each command with its
  result, each pass (`kat batch-exec`) with its code, the agent's comments, snapshot images
- `kat api [NAME]` — signatures and docstrings of the library, for agents; `kat open FILE
  --verify` — let KiCad's own editor load the result and compare
- `kat pcb` / `kat.pcb` — the board: build it from the schematic (proven by DRC parity), setup, placement, structure, routing (Freerouting through kat's own Specctra DSN/SES), finishing, fabrication files and a release gate (`src/kat/pcb/`)
- `skills/kicad-design/` (phases and gates), `skills/kicad-schematic/`, `skills/kicad-layout/` — the agent skills
- [`SPEC.md`](SPEC.md) — workflow, sync semantics, checks, and the pitfalls found so far

## In one picture

```
SKiDL design ──netlist──► kat sync ──► .kicad_sch ◄── kat batch-exec passN.py (import kat)
                                           │
                                 kat snapshot / check: ERC, netlist == design,
                                 drawing findings, PNG for review
```

The design owns parts and connections; the drawing owns positions. `kat sync` is
incremental, so a design change touches only the affected spots of an existing arrangement.

```
.kicad_sch ──kat ready (gate)──► kat pcb sync ──► .kicad_pcb ◄── kat batch-exec (layout passes)
                                                     │  setup · placement · structure
                                   kat pcb route ────┤  (Freerouting, headless)
                                                     │
                     kat pcb check / snapshot: DRC, parity, islands, layer pictures
                     kat pcb fab ──► Gerbers, drill, BOM, pick-and-place ──► kat pcb ready
```

The board follows the schematic the same way: `kat pcb sync` keeps placement and copper and
reports what a design change made stale.

## Install

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'     # add ,render on Windows
.venv/bin/pytest                                              # needs kicad-cli; e2e test also SKiDL
```

Requirements: KiCad 10 (`kicad-cli` on PATH, or `KICAD_CLI=/path/to/kicad-cli`), Python ≥ 3.10.
PNG snapshots use `rsvg-convert` if present, otherwise PyMuPDF (`[render]` extra; AGPL-3.0).
Autorouting needs Java 21+ and Freerouting: `kat pcb route --install` downloads the pinned
release (GPL-3, run as a separate program) once into kat's cache; routing tests are skipped
without it. `kat open --verify` for boards needs kicad-python (`[gui]` extra, MIT) and a
display; its tests run only with `pytest -m kicad_gui` (they open KiCad windows).

## Example

```sh
python tests/e2e/tia_design.py v1 v1.net                  # SKiDL -> netlist
kat new tia/tia.kicad_sch
kat sync tia/tia.kicad_sch v1.net                          # parts + connectivity
kat snapshot tia/tia.kicad_sch --slug import --expect v1.net
python tests/e2e/tia_design.py v2 v2.net                  # design change
kat sync tia/tia.kicad_sch v2.net                          # arrangement kept, changes reported
kat batch-exec tests/e2e/arrange_v2.py -- tia/tia.kicad_sch   # arrangement pass, journaled
kat snapshot tia/tia.kicad_sch --slug arrange --expect v2.net
python tests/e2e/tia_design.py v3 v3.net                  # + footprints and part numbers
kat sync tia/tia.kicad_sch v3.net
kat ready tia/tia.kicad_sch --expect v3.net                # gate before the PCB layout
kat pcb sync tia/tia.kicad_pcb                             # board: parts + nets, parity-checked
P=tests/e2e/tia_passes
kat batch-exec $P/setup.py -- tia/tia.kicad_pcb            # layout passes (Python), journaled
kat batch-exec $P/place1.py -- tia/tia.kicad_pcb
kat batch-exec $P/structure1.py -- tia/tia.kicad_pcb
kat pcb route tia/tia.kicad_pcb                            # Freerouting, then DRC
kat batch-exec $P/finish.py -- tia/tia.kicad_pcb
kat pcb refill tia/tia.kicad_pcb
kat pcb fab tia/tia.kicad_pcb && kat pcb ready tia/tia.kicad_pcb
kat comment --kind milestone --project tia "Rev A ready."   # the agent's own note
kat open tia/tia.kicad_pcb --verify                        # KiCad loads the same board
less tia/tia_REPORT.md                                     # the whole story, step by step
```

`tests/test_e2e_pcb_tia.py` runs the board flow to fabrication files and back through a
design change (R1 → 0805, an added capacitor), driven like an agent, and checks the design
report it leaves behind. `tests/test_e2e_skidl.py` runs the full iterative flow (import, design change, re-sync,
arrangement, re-sync); `tests/test_e2e_led_matrix.py` (marker `slow`, about 75 s) does the same
for a 200-part LED matrix on A2. Set `KAT_E2E_OUT=dir` to keep their snapshots for
inspection; `pytest -m 'not slow'` skips the large one.

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
