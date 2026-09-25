# kicad-automation — specification

Status: 2026-09-25 · KiCad 10.0.6 · SKiDL 2.3.0 · Python ≥ 3.10

## 1. Purpose and scope

Let an agent develop KiCad schematics through the command line, without MCP servers and
with the KiCad GUI closed, such that

- the **circuit** (parts and connections) is defined once, as code, and
- the **drawing** is readable and provably equivalent to that definition, and
- **design changes** do not throw away the arrangement work already done.

In scope: single-sheet schematics, SKiDL as design language, KiCad 10 files.
Out of scope for now: hierarchical sheets, buses, PCB layout (see §8).

Terms: **arrangement** is placing parts, labels, wires and fields on the schematic sheet so
it reads well; an **arrangement pass** is one script doing that. **Layout** always means
the PCB and is not used for the schematic.

## 2. Architecture

```
  design.py (SKiDL)  ──generate_netlist()──►  design.net   (source of truth: parts + nets)
                                                  │
                                         kat sync │  incremental, keeps arrangement
                                                  ▼
  arrangement passes (kat batch) ────────►  design.kicad_sch  (the drawing)
                                                  │
                            kat check / snapshot  │  ERC, netlist vs design.net,
                                                  ▼  drawing findings, PNG for review
                                        history/<timestamp>/
```

- **SKiDL defines the circuit.** Parts and connections exist only in the design code; its
  netlist is the reference the drawing is checked against.
- **KiCad reads the drawing.** Whether the file loads, which nets the drawing actually
  forms and what ERC reports are always determined by `kicad-cli`, never by `kat`'s own
  geometry model. `kat check --expect` compares KiCad's reading with the design. Every save
  goes through `kicad-cli sch upgrade --force`, which proves KiCad can load the file and
  applies KiCad's own formatting (clean `git diff`).
- **Lossless file editing.** `kat.sexpr` keeps every token, including ones it does not
  understand. Library symbols are embedded exactly as KiCad would embed them.
- **One JSON document per command** on stdout; exit code 0 = fine, 1 = checks failed,
  2 = error (`{"error": ...}`).

## 3. Rules

1. **Connectivity changes only in the design (SKiDL).** Never add, remove or re-wire
   connections in the drawing by hand. Arrangement commands may re-route, but
   `kat check --expect design.net` must keep matching.
2. **Stable part identity.** Give every SKiDL part a `tag=` (and preferably an explicit
   `ref=`). `sync` matches parts by tag first, then by reference. Without tags, SKiDL
   generates random ones and renumbered references look like replaced parts.
3. **Name the nets that matter** in SKiDL (`Net("TIA_OUT")`). Named nets must stay visibly
   named in the drawing (label or power symbol); `check` reports `unnamed` otherwise.
4. **KiCad GUI closed** while `kat` edits the file (or reopen the sheet afterwards and never
   save from the GUI in between — it would overwrite the file).
5. **Every step leaves a trace:** `kat snapshot` after each sync and arrangement pass.

## 4. Workflow

### 4.1 First import

```sh
python design.py                                   # writes design.net (generate_netlist)
kat new board/board.kicad_sch                      # once: project + empty sheet
kat sync board/board.kicad_sch design.net          # parts in a grid, nets via stubs + labels
kat snapshot board/board.kicad_sch --slug import --expect design.net
```

After the import, every pin is connected by a short stub and a net label, or a power symbol
for power nets (names that exist in KiCad's `power` library, e.g. `GND`, `+3V3`, `-2V5`).
PWR_FLAGs are added where ERC needs a driver. The result is electrically complete and
passes the checks, but it is grouped by part rather than by signal flow.

### 4.2 Arrangement passes

1. Look at `history/<latest>/<name>.png` (the trimmed image, meant for review).
2. Write `passN.kat`: a comment saying what the review found, then commands.
3. `kat batch board.kicad_sch passN.kat`
4. `kat snapshot board.kicad_sch --slug passN --expect design.net --note "..."`
5. Repeat until the stop criteria (§6) hold.

Typical arrangement commands:

| Goal | Commands |
|---|---|
| arrange parts | `move REF --to X,Y [--rot R] [--mirror x/y] [--unit N]` |
| label pair → real wire | `detach A.1`, `detach B.2`, `connect A.1 B.2 [--via ...]`, then `label NET X,Y` on the wire so the net keeps its name |
| tidy fields | `set REF Value --unit N --at X,Y`, `set '#FLG1' Value --hide` |
| re-orient a power symbol after mirroring its part | `detach REF.PIN`, `power GND REF.PIN --stub DX,DY` |

### 4.3 Design change

```sh
python design.py                                   # new design.net
kat sync board/board.kicad_sch design.net          # report says exactly what changed
kat snapshot board/board.kicad_sch --slug change-x --expect design.net
```

Then review only the places the report lists (new parts in the strip below the drawing,
re-attached pins) and fold them into the arrangement with another pass.

### 4.4 Sync semantics

| Change in the design | Effect on the drawing |
|---|---|
| nothing | nothing (sync is idempotent; the file is not rewritten) |
| value / footprint | field updated in place |
| reference renamed (same tag) | renamed in place; position and wiring kept |
| references swapped | handled (two-phase rename) |
| new part | all units placed in a free strip below the drawing, pins attached by stub + label / power symbol |
| part removed | symbol deleted; wires, labels, power symbols and flags left dangling are pruned |
| part's library symbol changed | replaced at the same position and rotation; pins re-attached |
| pin moved to another net | pin detached (its own wiring up to the next branch point) and attached to the new net |
| net renamed | labels renamed; power symbols swapped (or replaced by a label if no such power symbol exists) |
| undriven power net | one PWR_FLAG added, value hidden |

A pin that directly touches another part's pin and must change net cannot be fixed
automatically; it is listed under `unresolved`.

## 5. Command reference

`kat --help` shows the workflow; `kat <command> --help` shows each command's arguments and
examples. Points are `REF.PIN` (number, or a unique pin name) or `X,Y` in mm; y grows
downwards; everything snaps to 1.27 mm, parts belong on multiples of 2.54 mm.

| Command | Purpose |
|---|---|
| `new PATH` | project + empty sheet |
| `lib search WORDS` / `lib show LIB:NAME` | find symbols; units, pins, body |
| `list SCH` / `pins SCH REF` / `netlist SCH` | inspect placement, pin positions + outward direction, KiCad's nets |
| `sync SCH NETLIST [--dry-run]` | bring the design into the drawing (§4.4) |
| `check SCH [--expect NETLIST]` | ERC + netlist comparison + drawing findings |
| `snapshot SCH [--slug] [--note] [--expect]` | SVG + trimmed PNG + check into `history/` |
| `batch SCH SCRIPT` | many editing commands, one save, all-or-nothing |
| `add`, `wire`, `connect`, `label`, `power`, `nc` | draw |
| `move`, `set`, `detach`, `rm` | rearrange, edit fields, disconnect, delete |

## 6. Checks and stop criteria

`kat check` (also written by `snapshot` as `check.json`) reports:

- `erc` — KiCad's ERC, all severities, positions in mm.
- `netlist_vs_expected` — pin groupings compared with the design: `missing`, `extra`,
  `name_differences` (both names deliberate but different), `unnamed` (named in the design,
  unnamed in the drawing). Generated names (`Net-(...)`, `N$1`) are ignored.
- `overlaps` — text/text and text/symbol collisions (estimated boxes, 0.3 mm clearance).
- `body_overlaps` — overlapping part bodies; `off_page` — items outside the frame.
- `ok` = ERC errors 0 and netlist match. `clean` = `ok` and no drawing finding.

**Stop when** `clean` is true *and* the image review finds nothing. The heuristics do not see
pin names/numbers or wires running through text, so the image review is mandatory.

## 7. Pitfalls found, and their solutions

| Pitfall | Solution |
|---|---|
| **kiutils** drops KiCad 8+ syntax (`(hide yes)`, `exclude_from_sim`, `embedded_fonts`, ...) when writing back: hidden fields become visible, the netlist still matches, so only the image shows it | own lossless parser (`kat.sexpr`); never rewrite files with lossy libraries |
| The former MCP server did not re-escape `\"` in power-symbol descriptions → file unloadable, calls reported success | correct escaping (tested); every save is validated by KiCad (`sch upgrade`) |
| Visible `#PWR` references after MCP edits (it dropped `hide`) | placed fields are copied from the library definition, including visibility |
| Derived library symbols (`extends`) must be flattened when embedded | flattening verified identical to KiCad's own embedded copies |
| KiCad 10 `sym-lib-table` can nest tables (`type "Table"`) | followed recursively |
| Pin positions: library y is up, sheet y is down; rotation then mirror | transform verified against KiCad's netlist for 4 rotations × 3 mirror states × 3 symbol types (108/108 pins) |
| `kicad-cli` cannot work on a running GUI session | file-based editing, GUI closed; renders and checks from the saved file |
| kicad-cli 10 ERC JSON positions are in units of 100 mm despite `coordinate_units: mm` | converted in `kat.check.erc` |
| `sch upgrade` takes the project name from the file location | always normalise in place, inside the project directory |
| Files saved on Windows have CRLF; `upgrade` writes LF → whole-file diffs | add a `.gitattributes` (`*.kicad_sch text eol=lf`) |
| SKiDL's own `generate_schematic()` is unreadable for real circuits (overlaps, missing unit body, dangling wires) | SKiDL for connectivity only; `kat` draws |
| SKiDL random tags / automatic references break identity across revisions | `tag=` (and `ref=`) on every part; `sync` matches tag first |
| Moving a part whose wires end on shared T-points leaves steps (like KiCad's drag) | `detach` + `connect` the affected pins |
| Power symbols / labels on a pin were left behind or bridged by a wire on `move` | `move` carries everything hanging only off the part |
| Mirroring a part does not re-orient its carried power symbols | `detach` + `power ... --stub` after mirroring |
| `set --at` on a multi-unit part moved all units' fields to one spot | refuses without `--unit` |
| Label pairs replaced by wires lose the net name; the next sync puts a label back | `check` reports `unnamed`; add a label on the new wire |
| A PWR_FLAG stub crossed a neighbouring pin and shorted two nets | stubs are only drawn where their whole path is free |
| Overlap heuristics are approximate (misses pin names, underestimated label height) | calibrated boxes; image review stays mandatory |
| ERC `power_pin_not_driven` for supply nets | PWR_FLAG per power net (added by `sync`) |
| No `rsvg-convert` on Windows; `kicad-cli` not on PATH there | PyMuPDF fallback (`[render]` extra, AGPL-3.0); `kicad-cli` auto-discovery or `KICAD_CLI` |

## 8. Open points

- Hierarchical sheets, buses, global labels across sheets in `sync`.
- Netlist → PCB: KiCad 10 has no command-line "update PCB from schematic".
- SKiDL `NC` pins → no-connect flags in the drawing.
- Automatic arrangement beyond the placement strip (signal-flow placement) is left to the agent.
