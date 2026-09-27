# kicad-automation — specification

Status: 2026-09-25 · KiCad 10.0.6 · SKiDL 2.3.0 · Python ≥ 3.10

## 1. Purpose and scope

Let an agent develop KiCad schematics through the command line, without MCP servers and
with the KiCad GUI closed, such that

- the **circuit** (parts and connections) is defined once, as code, and
- the **drawing** is readable and provably equivalent to that definition, and
- **design changes** do not throw away the arrangement work already done.

In scope: single-sheet schematics, SKiDL as design language, KiCad 10 files.
Out of scope for now: hierarchical sheets, buses; PCB layout is being added (§4.6, §8).

Terms: **arrangement** is placing parts, labels, wires and fields on the schematic sheet so
it reads well; an **arrangement pass** is one script doing that. **Layout** always means
the PCB and is not used for the schematic.

## 2. Architecture

```
  design.py (SKiDL)  ──generate_netlist()──►  design.net   (source of truth: parts + nets)
                                                  │
                                         kat sync │  incremental, keeps arrangement
                                                  ▼
  arrangement passes (passN.py) ─────────►  design.kicad_sch  (the drawing)
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
- **Library first.** `import kat` is the implementation; the `kat` command (also
  `python -m kat`) only parses arguments, calls the library and prints its result. Every
  editing command is a method of `kat.Sheet` with the same arguments.
- **One JSON document per command** on stdout; exit code 0 = fine, 1 = checks failed,
  2 = error (`{"error": ...}`). Library functions return the same documents and raise
  `kat.KatError`.

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

1. Look at `history/<latest>/<name>.png` (the trimmed image, meant for review), then at
   the details: `tiles/` (large sheets), `kat view` close-ups around parts, points or
   regions, `findings/` crops (§4.5).
2. Write `passN.py`: a docstring saying what the review found, then the edits.
3. `python passN.py`
4. `kat snapshot board.kicad_sch --slug passN --expect design.net --note "..."`
5. Repeat until the stop criteria (§6) hold.

A pass is an ordinary Python script. `kat.edit` saves once when the block ends, and leaves
the file untouched if anything raises, so a failed pass needs no clean-up. Loops and
functions handle repeated structures; `kat.edit(path, dry_run=True)` tries a pass without
saving.

```python
"""pass 2: R1 sits in the strip below; PD_ANODE is a label pair -> wire."""
import kat

with kat.edit("board/board.kicad_sch") as s:
    s.move("R1", to=(127, 86.36), rot=90)
    for p in ("J1.1", "U1.2"):
        s.detach(p)
    s.connect("J1.1", "U1.2")
    s.label("PD_ANODE", (101.6, 99.06))    # the net keeps its visible name
```

Typical arrangement edits (`s` is the `kat.Sheet`):

| Goal | Calls |
|---|---|
| arrange parts | `s.move(REF, to=(x, y), rot=R, mirror="x"/"y", unit=N)` |
| label pair → real wire | `s.detach("A.1")`, `s.detach("B.2")`, `s.connect("A.1", "B.2", via=[...])`, then `s.label(NET, (x, y))` on the wire so the net keeps its name |
| tidy fields | `s.set(REF, "Value", unit=N, at=(x, y))`, `s.set("#FLG1", "Value", hide=True)` |
| re-orient a power symbol after mirroring its part | `s.detach("REF.PIN")`, `s.power("GND", "REF.PIN", stub=(dx, dy))` |
| inspect | `s.pins(REF)` (sheet positions, outward `dir`), `s.list()` |

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
| value / footprint / custom fields (`part.fields["MPN"] = ...` in SKiDL) | field updated in place; custom fields are added hidden |
| reference renamed (same tag) | renamed in place; position and wiring kept |
| references swapped | handled (two-phase rename) |
| new part | all units placed in uniform rows below the drawing, in natural order (R2 before R10); parts joined by an unnamed two-pin net sit side by side and get a short wire (`wired`), other pins a stub + label / power symbol. If the rows do not fit, the paper grows to the next ISO size (`paper`) |
| part removed | symbol deleted; wires, labels, power symbols and flags left dangling are pruned |
| part's library symbol changed | replaced at the same position and rotation; pins re-attached |
| pin moved to another net | pin detached (its own wiring up to the next branch point) and attached to the new net |
| net renamed | labels renamed; power symbols swapped (or replaced by a label if no such power symbol exists) |
| undriven power net | one PWR_FLAG added, value hidden |
| power pin in a dense pin row (connector) | the power symbol sits beyond the neighbours' labels on a longer stub, so its text does not cover them |

A pin that directly touches another part's pin and must change net cannot be fixed
automatically; it is listed under `unresolved`.

### 4.5 Reviewing large sheets

An agent's image viewer downscales pictures to about 2000 px, so the overview PNG of an A2
sheet (5600 px at zoom 3) shows the arrangement but not pin numbers or label text. The
review is therefore a loop: overview → tiles → close-ups → next close-up, until every doubt
is resolved.

- `snapshot` writes `tiles/r<i>c<j>.png` (about 1600 px, 10 % overlap, each with its
  `region_mm`) when the PNG exceeds 2000 px (`--tiles` / `--no-tiles` to force).
- `snapshot --finding-crops N` writes close-ups of the first N findings with a position
  (ERC items, text overlaps, body overlaps) to `findings/`.
- `kat view SCH --around REF | --at X,Y | --region X,Y,W,H` renders one region to
  `views/` in under a second (the frameless SVG is cached per file version). The result
  has `region` and `px_per_mm` to map pixels back to sheet mm.

### 4.6 Board sync

`kat pcb sync PCB` (library: `kat.pcb.sync`) puts the schematic's parts and nets on the
board; it is the file-based counterpart of KiCad's "Update PCB from Schematic", which neither
kicad-cli nor kicad-python offers.

- **Source**: the netlist KiCad exports from the schematic (`sch export netlist`), not the
  SKiDL netlist: DRC schematic parity compares KiCad's own net names (`/LED_1_1`,
  `Net-(D1-A)`) and every symbol field.
- **Gate**: refuses unless `kat ready` passes (`--force` overrides; the report carries the
  gate result either way).
- **Identity**: symbol path `/<uuid>` (KiCad's link) → SKiDL tag → reference. A footprint
  still linked to another design symbol is never taken over by a fallback.
- **Add**: footprint from `fp-lib-table` with reference, value, all symbol fields (hidden),
  symbol path, sheet name/file, DNP / exclude-from-BOM flags, pad nets, pin names and types.
  New parts go in rows to the right of the board outline.
- **Update**: reference, value, fields (extra custom fields removed), flags, pad nets.
  A changed footprint is **replaced in place** (position, rotation, side, uuid kept).
- **Remove**: footprints whose symbol left the design.
- **Copper**: a net that disappeared while all its pads went to one new net is renamed on
  tracks, vias and zones (`renamed_nets`). Tracks touching a pad of another net and copper
  on vanished nets are **reported** (`stale_tracks`); `--rip-stale` removes them.
- **Proof**: every run ends with DRC; `ok` = parity 0 and nothing unresolved. A second run
  of the same schematic changes nothing.

### 4.7 Board setup

Methods of `kat.pcb.Board`, used inside `with kat.pcb.edit(PCB) as b:` (saved once):

| Method | Effect |
|---|---|
| `outline(rect=(x, y, w, h), radius=r)` / `outline(polygon=[...])` | replaces everything on Edge.Cuts |
| `set_copper_layers(n)` | 2, 4, 6 … copper layers (inner ones refused for removal while used) |
| `plane(net, "In1.Cu")`, `set_layer_type(layer, "power")` | a whole-board plane on its own layer; layer type power = the router lays no tracks there |
| `mounting_hole(at, "M3")`, `add_board_part(lib_id, ref, at)` | footprints without a symbol, marked board-only: sync and parity leave them alone |
| `zone(net, layers, polygon=None)` | copper pour (default: outline minus 0.5 mm); filled by KiCad on check |
| `keepout(layers, rect/polygon, tracks=False, vias=True, ...)` | rule area; `False` forbids that item kind |
| `netclass(name, track, clearance, via, via_drill, nets=[patterns])` | `.kicad_pro` net class + pattern assignment |
| `rules(text)` | `.kicad_dru` custom rules (KiCad rule language) |

A setup pass is safe to run again, so it is edited and re-run when the specification
changes: the outline, layers, net classes and rules are replaced; a zone or keep-out
replaces the one of the same name (default `"{net} {layers}"`, `"keepout {layers} {box}"`;
kept with its fill when unchanged - several pours of one net on one layer need distinct
names); a mounting hole at the same spot is kept or replaced; board text with the same text
and layer is moved; fiducials are placed anew. These calls report `status`: created,
replaced or unchanged.

Net classes and rules are what DRC and the router use; derive them from the specification
(current → width, voltage → clearance/creepage) before placing parts.

### 4.8 Placement

Methods of `kat.pcb.Board` (in `with kat.pcb.edit(PCB) as b:`); layout passes are Python,
like arrangement passes:

| Method | Effect |
|---|---|
| `move(ref, to, rot, side)` | refused when its courtyard would overlap another on the same side (`allow_overlap`) |
| `move_many({ref: {to, rot, side}})` | simultaneous; checked on the final arrangement (parts may swap places) |
| `flip(ref)`, `align(refs, axis)`, `distribute(refs, start, pitch)` | side, rows/columns, even spacing |
| `place_near(ref, "U1.8")` | a two-pad part beside a pin: outside the host's courtyard, on the side the pin faces, its pad on the pin's net nearest; steps sideways until free |
| `near(point, radius)`, `collisions(refs)` | what is around a point / courtyard overlaps |
| `blocks()`, `block_box(name)`, `move_block(name, to/by)` | functional blocks: the `Block` field set in SKiDL (`part.fields["Block"] = "ch1"`) |
| `replicate("ch1", {"ch2": (x, y), ...})` | arrange other blocks like the template; parts pair up by SKiDL tag with the block name removed (`ch1_r` ↔ `ch2_r`), else by footprint and reference order |

Order in a placement pass: mechanical and fixed parts, connectors, critical analog parts and
heat sources, decoupling (`place_near`), then the rest; repeated blocks once, then
`replicate` (or a loop).

### 4.9 Structure before routing

The spike showed the autorouter optimises "connected", not "good": with a free hand it cut
the GND pour into islands. So the agent lays the structure first and locks it:

| Method | Effect |
|---|---|
| `track(["R1.2", "D1.2"], layer, width)` | net from the pads it touches; refused if they disagree (connections come from the schematic) or if it comes closer than the net-class clearance to other-net copper |
| `via(at, net)` | through via; on a pad (`"U1.9"`) it takes the pad's net |
| `via_to_plane("D1.1")`, `fanout(refs, nets=["GND"])` | short stub + via next to an SMD pad (outward first, then sideways), so it reaches its plane |
| `stitch("GND", pitch)` | stitching vias on a grid where they clear copper and courtyards (after the structure) |
| `lock(net)`, `remove_tracks(net, layer)` | locked copper stays for the router and for rip-up |
| `net_rules(net)` | width, clearance, via size of the net's class |
| `kat.pcb.refill(PCB)` | fill the pours and save them (KiCad's filler) |

Recipe: give each plane its own layer (`plane`), place a via per plane pad (`fanout`), lay
critical nets (supplies, Kelvin, guards) by hand and lock them, then route the rest.
LED matrix: 100 cathode vias in 0.4 s. Two-layer attempts failed (islands, unconnected
pads); four layers with In1 as GND plane routed completely in about 2 minutes.

### 4.10 Routing

`kat pcb route PCB` (library `kat.pcb.route`) writes a Specctra DSN with kat's own writer,
runs Freerouting headless, reads the SES back, adds the new tracks and vias, saves and
runs `kat pcb check`. No pcbnew involved.

- Every footprint becomes its own DSN image with pins at their board positions and
  padstacks carrying the rotated pad outline on the pad's copper layers; everything is
  placed "front, 0". No rotation or mirroring is left for the router to interpret.
- Layers of type power (`plane()`) are Specctra power layers: no tracks. Pours are
  `plane`s, keep-outs `wire_keepout`/`via_keepout`, net classes `class` rules;
  locked copper is `protect`ed, other existing copper may be changed.
- `--net` limits routing to some nets; the others and their pads stay obstacles.
- DSN, SES and the Freerouting log go to `history/<stamp>_route/`.
- Freerouting (Java 21+, GPL-3) is an external program: `kat pcb route --install`
  downloads the pinned release once into kat's cache, checked by SHA-256
  (`KAT_FREEROUTING=/path/to.jar` uses another copy).
- Tracks the router narrowed at fine-pitch pads are widened to the net-class width where
  the clearance allows (`widened`; the rest in `narrow`).
- **Judge the result by `check.unconnected` and `check.islands`**, not by the router's
  own `unrouted`: in the spike Freerouting reported 0 unrouted while KiCad found 29
  unconnected pads in pour islands.

Loop: structure (§4.9) → route → check and snapshot → adjust structure, keep-outs or net
classes → `remove_tracks()` (locked copper stays) → route again.

### 4.11 Finishing and fabrication

| Call | Effect |
|---|---|
| `b.place_refs(size=0.8)` | reference designators beside each courtyard (above, below, left, right - first spot clear of pads, other references and the edge), upright or 90° for tall parts; unplaceable ones go to the fab layer and are listed |
| `b.board_text(text, at)`, `b.title_from_schematic()` | board name/revision on the silkscreen; title block copied from the schematic |
| `b.fiducials(3)` | board-only fiducials near the corners, clear of courtyards |
| `kat pcb fab PCB` | `fab/<name>-<rev>/`: Gerbers (copper, paste, silk, mask, outline) + drill files and map, zipped; `assembly/` position file and BOM (with MPN, from the schematic); IPC-D-356; STEP; 3D renders. Pours are refilled for the Gerbers |
| `kat pcb ready PCB` | release gate: board check clean (no DRC errors or warnings, parity 0, nothing unconnected or outside the outline), title and revision set, fabrication set complete and newer than the board |

### 4.12 Board checks and review

`kat pcb check PCB` runs KiCad's DRC with zones refilled in memory and schematic parity,
plus kat's findings (outline missing, parts outside the outline). `ok` = no DRC errors,
parity 0, nothing unconnected, outline present; `clean` = `ok` and no DRC warnings or kat
findings. `islands` counts copper cut off from its net (starved thermals, isolated copper).

On a board, numbers come first: unconnected items, islands, clearances and parity are
exact. Pictures are for what numbers cannot say (fan-out, detours, crowding, silkscreen):

- `kat pcb snapshot PCB --slug NAME --note "..."` → `history/<stamp>_pcb_<slug>/` with
  `top.png`, `bottom.png`, `placement.png`, `In<n>.png` (whole board, about 2000 px),
  `tiles/top|bottom/` at 30 px/mm when the overview is coarser, `findings/` close-ups
  (`--finding-crops N`), `3d_top.png` / `3d_bottom.png`, `check.json`, a board copy.
- `kat pcb view PCB --around REF | --at X,Y | --region X,Y,W,H --layers SET` → `views/`.
  Sets: `top`, `bottom`, `placement`, `copper`, `In1`…, or KiCad layer names.

SVGs are exported in page coordinates (`--page-size-mode 1`), so SVG units are board mm
and regions are cut by rewriting the viewBox, as for the schematic. Pours are filled for
the picture (`--check-zones`) without changing the file.

Board geometry (pad positions, courtyards) follows KiCad's storage convention — children
in footprint coordinates, bottom side mirrored in y, pad and text angles absolute — and is
tested against IPC-D-356 and DRC's library comparison.

## 5. Command reference

Library: `kat.new`, `kat.load`, `kat.edit`, `kat.sync`, `kat.check`, `kat.snapshot`, `kat.view`,
`kat.ready`, `kat.pcb.sync` / `edit` / `check` / `snapshot` / `view` / `route` / `refill` / `fab` /
`ready` / `drc` (board), `kat.nets`, `kat.lib_search`, `kat.lib_show`, and the `kat.Sheet` methods; each returns what
the matching command prints.

`kat --help` shows the workflow; `kat <command> --help` shows each command's arguments and
examples. Points are `REF.PIN` (number, or a unique pin name) or `X,Y` in mm; y grows
downwards; everything snaps to 1.27 mm, parts belong on multiples of 2.54 mm.

| Command | Purpose |
|---|---|
| `new PATH` | project + empty sheet |
| `lib search WORDS` / `lib show LIB:NAME` | find symbols; units, pins, body |
| `list SCH [--near X,Y --radius R]` / `pins SCH REF` / `netlist SCH` | inspect placement (or what is near a point, with uuids), pin positions + outward direction, KiCad's nets |
| `sync SCH NETLIST [--dry-run]` | bring the design into the drawing (§4.4) |
| `check SCH [--expect NETLIST]` | ERC + netlist comparison + drawing findings |
| `ready SCH [--expect NETLIST] [--require-field NAME]` | gate before the PCB layout (§6.1) |
| `pcb sync PCB [--sch] [--dry-run] [--force] [--rip-stale]` | parts and nets onto the board (§4.6) |
| `pcb fab PCB` / `pcb ready PCB` | fabrication set / release gate (§4.11) |
| `pcb route PCB [--passes] [--net NAME] [--install]` | Freerouting through kat's DSN/SES, then the board check (§4.10) |
| `pcb check PCB` / `pcb snapshot PCB` / `pcb view PCB ...` | board DRC + findings, history step with pictures, zoomed view (§4.12) |
| `snapshot SCH [--slug] [--note] [--expect] [--tiles] [--finding-crops N]` | SVG + trimmed PNG + check into `history/`; tiles and finding close-ups |
| `view SCH --around REF / --at X,Y / --region X,Y,W,H` | zoomed PNG of one region into `views/` |
| `add`, `wire`, `connect`, `label`, `power`, `nc` | draw |
| `move`, `move-many`, `set`, `detach`, `rm` | rearrange (one part, or a block move), edit fields, disconnect (a pin or a whole part), delete |
| `page`, `title`, `text` | paper size, title block, free text (notes, headings) |
| `restore SCH STEP` | put the schematic back to a history step (undo a pass) |

## 6. Checks and stop criteria

`kat check` (also written by `snapshot` as `check.json`) reports:

- `erc` — KiCad's ERC, all severities, positions in mm.
- `netlist_vs_expected` — pin groupings compared with the design: `missing`, `extra`,
  `name_differences` (both names deliberate but different), `unnamed` (named in the design,
  unnamed in the drawing). Generated names (`Net-(...)`, `N$1`) are ignored.
- `overlaps` — text/text and text/symbol collisions (estimated boxes, 0.3 mm clearance).
- `body_overlaps` — overlapping part bodies; `off_page` — items outside the frame;
  `duplicates` — the same wire, label or power symbol drawn more than once on one spot.
- `ok` = ERC errors 0 and netlist match. `clean` = `ok` and no drawing finding.

**Stop when** `clean` is true *and* the image review finds nothing. The heuristics do not see
pin names/numbers or wires running through text, so the image review is mandatory.

### 6.1 Ready for layout

`kat ready SCH --expect design.net` is the gate between schematic and PCB. It reads the
netlist KiCad exports from the schematic — the data the board is built from — so symbols
marked "not on board" are not board parts (and, if the design has them, show up as a
netlist difference).

| Errors (block the layout) | Warnings (look, then decide) |
|---|---|
| schematic not `ok` / not `clean` (as `kat check`) | no `--expect` given |
| reference not annotated (`R?`) or without a number (`ROUT`: KiCad's BOM prints `ROUT?`), empty value | numbered pad without a symbol pin (mounting, shield or thermal pads are often fine) |
| no footprint; footprint library not in `fp-lib-table` (global + project); footprint file missing | footprint outside the symbol's footprint filters (`ki_fp_filters`) |
| a symbol pin without a pad of that number (the part cannot be connected) | footprint has no 3D model file |
| a required field empty (default `MPN`, `--require-field` repeatable, `--no-require`); parts excluded from the BOM are exempt | |

`ready` is true when there are no errors; `by_problem` counts both lists. Footprints and
part numbers are chosen in SKiDL (`footprint=`, `part.fields["MPN"]`) and reach the
schematic through `sync`, so fixing the gate is a design change (§4.3), not a drawing edit.

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
| A part moved onto another part's pin joined the nets silently; moving the other part later dragged wires between them (LED matrix: rows 9/10 shorted to GND) | `move` refuses pin-on-pin/wire/label contact (`allow_contact` to override); `move_many` moves parts simultaneously, so they can take each other's places without parking |
| `sync` re-attached pins whose net had not changed when their partner pin left the net: each run stacked another stub + label | a lone pin that already carries the net's name counts as drawn; `check` reports `duplicates` |
| `detach` on stacked identical wires removed the wires but left the labels floating | stacked identical wires count as one run |
| The import strip ignored the page height (294 items off an A2 page, invisible in the PNG) | rows wrap within the page; the paper grows when needed |
| Agents' image viewers downscale large PNGs (A2 at zoom 3 = 5600 px → 2000 px): pin numbers and labels become unreadable, yet the image looks fine | snapshot tiles, `kat view` close-ups and the review sub-loop (§4.5) |
| Text layout scripts (`.kat`) had no loops: a 100-cell matrix needed a generator emitting 1,100 lines, run at ~0.2 s per line | arrangement passes are Python on the `kat` library; geometry indexed (LED matrix pass: 193 s → 9 s) |
| Sideways power symbols on dense connector pins put their text on the neighbours' labels | sync places them beyond the labels on a longer stub |
| Power symbols / labels on a pin were left behind or bridged by a wire on `move` | `move` carries everything hanging only off the part |
| Mirroring a part does not re-orient its carried power symbols | `detach` + `power ... --stub` after mirroring |
| `set --at` on a multi-unit part moved all units' fields to one spot | refuses without `--unit` |
| Label pairs replaced by wires lose the net name; the next sync puts a label back | `check` reports `unnamed`; add a label on the new wire |
| A PWR_FLAG stub crossed a neighbouring pin and shorted two nets | stubs are only drawn where their whole path is free |
| Overlap heuristics are approximate (misses pin names, underestimated label height) | calibrated boxes; image review stays mandatory |
| ERC `power_pin_not_driven` for supply nets | PWR_FLAG per power net (added by `sync`) |
| KiCad 10.0.6 has no command-line "Update PCB from Schematic", kicad-python neither, and pcbnew's Python module is deprecated | file-based board sync in kat, proven by DRC `--schematic-parity` (§4.6) |
| Board parity fails with SKiDL's net names and missing fields | the board is built from KiCad's own netlist of the schematic, all symbol fields copied (hidden) |
| Library mounting holes and fiducials are not marked board-only; a sync would delete them | kat adds them board-only; sync and parity skip board-only footprints |
| An empty `net_settings.classes` list in a template project means "KiCad defaults"; adding one class without `Default` breaks the class | kat writes the full Default class and format version 5 with the first custom class |
| Freerouting treats a pour as one plane; tracks on the plane layer cut it into islands (spike: "0 unrouted", 29 unconnected) | each plane on its own layer (`plane()`, layer type power: no tracks), a via per plane pad, check `islands` / `unconnected` after routing |
| DRC lists the zone first for starved thermals, and a zone's position is its first corner | finding close-ups use the non-zone item (the pad) |
| KiCad draws SVG layers in the given order; a pour listed last hides everything | copper sets are listed bottom-up |
| Freerouting counts a via closer than the clearance to its *own* pad as a violation (KiCad does not) | `via_to_plane` keeps the full clearance to its pad (also: no via in the solder area) |
| Freerouting counts every via inside a `wire_keepout` as a violation, even where vias are allowed - a plane layer cannot be protected that way when it carries the plane vias | keep-outs only where no vias are needed; for planes: pours on both layers, stitching vias, check `islands` |
| Freerouting narrows tracks at fine-pitch pads below the net-class width (DRC `track_width`); `--router.automatic_neckdown=false` is not applied | on import kat widens them to the class width where the clearance allows and reports the rest (`narrow`) |
| A reference without a number (`ROUT`) passes ERC and parity, but KiCad treats it as unannotated: the BOM prints `ROUT?` | `kat ready` error `reference_without_number` |
| No `rsvg-convert` on Windows; `kicad-cli` not on PATH there | PyMuPDF fallback (`[render]` extra, AGPL-3.0); `kicad-cli` auto-discovery or `KICAD_CLI` |

## 8. Open points

- Hierarchical sheets, buses, global labels across sheets in `sync`.
- Teardrops, length tuning, differential pairs and impedance-controlled stack-ups are not
  automated; set them in KiCad or as custom rules.
- Live mode with kicad-python (editor open) is not implemented; the pipeline is headless.
- SKiDL `NC` pins → no-connect flags in the drawing.
- Automatic arrangement beyond the placement strip (signal-flow placement) is left to the agent.
