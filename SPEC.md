# kicad-automation — specification

Status: 2026-09-25 · KiCad 10.0.6 · SKiDL 2.3.0 · Python ≥ 3.10

## 1. Purpose and scope

Let an agent develop KiCad schematics through the command line, without MCP servers and
with the KiCad GUI closed, such that

- the **circuit** (parts and connections) is defined once, as code, and
- the **drawing** is readable and provably equivalent to that definition, and
- **design changes** do not throw away the arrangement work already done.

In scope: single-sheet schematics, SKiDL as design language, KiCad 10 files.
Out of scope for now: buses, and `sync` into hierarchical sheets (sheets can be drawn and
annotated, §4.17); PCB layout is being added (§4.6, §8).

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
   generates random ones and renumbered references look like replaced parts. Inside a
   `@subcircuit` a tag only needs to be unique within the block; give each block call a
   `tag=` too (§4.16).
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

A part inside a SKiDL `@subcircuit` is identified by its tag qualified with the instance
path: `sync` writes `SKiDL Tag = CH_A/RC1/r` and the fields `Block = CH_A/RC1` and
`Block Type = rc` (§4.16).

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
| `replicate("ch1", {"ch2": (x, y), ...})` | arrange other blocks like the template; parts pair up by SKiDL tag with the block name removed (`CH_A/RC1/r` ↔ `CH_B/RC1/r`, `ch1_r` ↔ `ch2_r`), else by footprint and reference order |

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
| `kat pcb refill PCB` | KiCad's zone filler fills every pour and saves the board |
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

### 4.13 Design journal

Every project has a design report `NAME_REPORT.md` in its root (next to `NAME.kicad_pro`),
created by `kat new` or by the first entry. The user reads it to review, step by step, what
the agent did. Entries are only appended (under a file lock); each has a running number,
date, time and kind: `## 12 · 2026-09-27 14:03:11 · command`.

| Kind | Written by | Content |
|---|---|---|
| `command` | every `kat` CLI command on a project (not `lib`, `api`) | the command line, time, exit code, key result lines, the full JSON result collapsed (`<details>`, cut after 150 lines) |
| `batch` | `kat batch-exec FILE.py [--project DIR] [-- ARGS]` / `kat.batch_exec` | the script's docstring, its source as written, its output, the error and traceback if it failed, the images of snapshots/views it made; kat calls inside add no entries of their own |
| `plan`, `milestone`, `problem`, `note` | `kat comment --kind K TEXT` (`-` = stdin) / `kat.comment` | the agent's markdown: what comes next, what was reached, what failed and needs another iteration |
| `snapshot`, `view` | `kat.snapshot`, `kat.pcb.snapshot`, `kat.view`, `kat.pcb.view` called from plain Python | the call, the check summary, the images |

Images are copied to `NAME_REPORT/<NNN>_<slug>/` (overview pictures only: the sheet PNG;
board `placement`, `top`, `bottom`, `In*`, `3d_top`) and linked relatively. Paths in entries
are relative to the project root. The project of a batch is `--project`, else
`KAT_PROJECT`, else the files the script opens, else the working directory (a project there
or in its only project subdirectory). `KAT_REPORT=0` or `kat --no-report` turns it off.

### 4.14 API lookup

`kat api` prints the library's calls from the code itself: without a name an overview of
`kat` and `kat.pcb`; `kat api Board` a class with one line per method (mixins included);
`kat api Board.keepout` (or `keepout`, `pcb.Board.keepout`, `kat.pcb.Board.keepout`) the
signature, the full docstring and the source line - the same text as Python's `help()` for
a function. Ambiguous short names list their matches. Text output (`--json` for data).
Every public call has a docstring stating units and return value (`tests/test_api.py`).

### 4.15 Opening in KiCad

- **Guard:** kat refuses to save a schematic or board while KiCad's lock file
  (`~NAME.kicad_pcb.lck`) says the file is open: from another host, or while a KiCad
  process of this user runs. A lock without any KiCad process is stale and ignored.
- `kat open FILE` starts the matching editor (pcbnew, eeschema, kicad for a `.kicad_pro`)
  for a person.
- `kat open FILE --verify` starts it with a private copy of the KiCad settings (IPC API on)
  and a private `TMPDIR` (own API socket and instance check - a KiCad the user has open is
  not touched), then compares what the editor loaded with kat's reading and closes it
  without saving. Board (kicad-python, `pip install 'kat[gui]'`): footprints, nets,
  track/via counts, zone names, outline. Schematic: KiCad 10.0.6 has no schematic API, so
  "verified" means the editor opened it (lock file written) and kept running. Without a
  display or kicad-python the answer is `verified: null` with the reason.
- Tests: marker `kicad_gui`, only with `-m kicad_gui` (they open windows).

### 4.16 Blocks (SKiDL subcircuits)

A block is a SKiDL `@subcircuit` function; each call is an instance. Call blocks with
keyword arguments and a `tag=`, which names the instance:

```python
ch(io=Interface(inp=a, out=b, gnd=gnd), ra="1k", tag="CH_A")    # inside: rc(..., tag="RC1")
generate_netlist(file_="design.net")
kat.skidl_blocks.write("design.net")          # -> design.blocks.json
```

- **Instance path** (`CH_A/RC1`): from the netlist's sheet path. Every part inside gets the
  fields `Block` (the path) and `Block Type` (the function name, from the block record),
  and its tag is qualified by the path (`CH_A/RC1/r`). Tags may therefore repeat between
  blocks.
- **Block record** `design.blocks.json`, written by `kat.skidl_blocks.write` from SKiDL's
  node tree (the netlist does not carry it): per instance `type`, `ports` (arguments that
  are nets, named by the argument, or `arg.key` for an `Interface`), `params` (number and
  string arguments, plus the defaults left unset) and `parts`.
- **Warnings** in the record: a call without `tag=` (SKiDL's automatic name changes when
  calls are reordered) and arguments passed by position (SKiDL records keyword arguments
  only; that call's defaults are then left out).
- **Board:** `blocks()` lists nested blocks at every level (`CH_A` includes `CH_A/RC1`),
  so `move_block` and `replicate` move whole instances.
- A `Block` field set by hand (`part.fields["Block"] = "ch1"`) still works for parts outside
  subcircuits.

### 4.17 Hierarchical sheets

A sheet box on a parent sheet points to a child file; the child's ports (hierarchical
labels) are the box's pins. One child file can serve several boxes and several projects.

```python
with kat.edit("block1.kicad_sch") as s:
    s.port("IN", (60.96, 60.96), shape="input")
    s.sheet("RC1", "../blocks/RC/RC.kicad_sch", (71.12, 58.42), left={"IN": "input"},
            right={"OUT": "output"}, fields={"R_VAL": "${RA}"})
    s.connect((60.96, 60.96), "RC1.IN")                # SHEETNAME.PIN like REF.PIN
kat.hierarchy.annotate("top/top.kicad_sch")            # after every change of the tree
```

- **File paths** are relative to the file that holds the box (KiCad's rule, also when
  nested).
- **Parameters:** a sheet box's own fields are text variables inside the child
  (`${R_VAL}`), passed down to deeper levels; the child project's `text_variables` give
  the default when it is opened on its own. KiCad's netlist, BOM and PDF resolve them per
  instance.
- **`kat.hierarchy.tree(root)`**: every instance in page order with its name path
  (`/CH_A/RC1`), uuid path, file and parameters. A board footprint's path is
  `<uuid path>/<symbol uuid>`.
- **`kat.hierarchy.annotate(root, per_page=100)`**: pages in reading order, references
  `prefix + page × 100 + n` (R301 = first R on page 3), written into each symbol's
  `instances` for the root's project. Other projects' entries are kept, so a block keeps
  its own numbering; annotate the block as its own root with `per_page=0`.

### 4.18 Stamping block layouts

A block is laid out once on its own board, checked (DRC clean), and then stamped onto each
of its instances in a design:

```python
kat.pcb.block_environment("blocks/RC/RC.kicad_pcb", ["env GND In1"])  # not part of the block
with kat.pcb.edit("top/top.kicad_pcb") as b:
    b.stamp("blocks/RC/RC.kicad_pcb", {"/CH_A/RC1": (103, 103), "/CH_B/RC1": ((130, 118), 180)})
```

- **Instances** are paths, `/CH_A/RC1`. Hierarchical design: a sheet instance; a part pairs
  with the block part whose footprint path is the end of its own path. Flat design: a
  SKiDL block (`Block` field); a part pairs with the block part whose tag is the same
  relative to its instance (`CH_A/RC1/r` ↔ `DUT/r`). `b.instances(block)` lists them.
- **Environment:** parts on the block board outside the block instance (a SKiDL test
  harness) are not copied, nor are tracks ending on their pads or items only on their
  nets; neither are the zones named with `block_environment` (`NAME.block.json`).
- **Copied:** the parts' positions, rotations and sides; tracks, arcs, vias (locked unless
  `lock=False`), zones and keep-outs (named `/CH_A/RC1: out node`), graphics not on
  Edge.Cuts, board-only parts (new references). The block board's own groups become
  sub-groups of the instance group `"/CH_A (NAME)"`.
- **Transform:** the block origin (top-left of its outline) goes to `at`, turned by 0, 90,
  180 or 270 degrees. The same side as on the block board; a mirrored block is a separate
  block board.
- **Refused:** unpaired parts on either side, another footprint, an instance wired
  differently from the block (a net mapping to two nets), copper layers the design lacks.
- **Stamping again** removes the instance group's copied items first and places anew.
- The design is judged as usual: `kat pcb check`, then route what is left
  (`kat pcb route` keeps the locked block copper).

### 4.19 Block library

Each block is a project of its own in `blocks/NAME/` (`KAT_BLOCKS`, else the nearest
`blocks/` folder of the working directory or above):

| File | Content |
|---|---|
| `NAME.py` | the SKiDL block function; under `__main__` a test harness calling it with `tag="DUT"` |
| `NAME.kicad_pro/.kicad_sch/.kicad_pcb` | the block's project, synced from the harness |
| `NAME.block.json` | type, instance (`DUT`), ports, parameters, environment zones, last check |
| `NAME_REPORT.md` | the block's journal |

- `kat block new NAME` creates it from a template; `kat block build NAME` runs the harness,
  syncs schematic and board, and records type, ports and parameters.
- The harness parts (outside `DUT`) are the block's **environment**: they are on the
  block's board so that it can be checked, but `stamp` does not copy them.
- `kat block check NAME`: the schematic check against the harness netlist, the gate's part
  data (MPN by default) and the board check. Open connections to the environment are
  expected; anything else counts. The result is stored with the files' times; `stamp` warns
  (`warnings`) when a block changed since its last clean check.
- A design uses the function (`rc = kat.blocks.use("RC")`, then
  `rc(inp=a, out=b, gnd=gnd, tag="CH_A")`) and stamps the layout by name
  (`b.stamp("RC", {"/CH_A": (x, y)})`, `kat pcb stamp PCB RC all --at X,Y --step DX,DY`).

### 4.20 Flat sheets with many blocks

Small blocks read best on one flat sheet, each instance drawn the same way. After `kat
sync` (which places the parts of one block together), each instance is drawn like the
block's own checked schematic:

```
kat stamp top.kicad_sch RC all --at 50.8,50.8 --step 0,40.64 --expect design.net
```

- **Pairs** by `Block` + tag, as on the board (§4.18). Every pin of the instance's parts is
  detached, the parts are placed, turned and mirrored as in the block, with its field
  positions.
- **Copied:** the block's drawing: every wire reachable from its pins, and the labels,
  junctions, no-connects and power symbols on them. Labels are renamed to the design's net
  names for this instance. PWR_FLAGs are not copied (one per net, added where ERC needs it).
- **The block's harness must connect to it by labels**: a wire from a block pin to a
  harness pin is refused (the harness is not part of the block's drawing).
- Afterwards whatever the design still misses is attached as `sync` would, and the sheet
  must match `--expect`; otherwise the file is put back and the command fails.
- A warning names parts that the stamped drawing now covers; move them, or stamp elsewhere.
- **Frames:** each stamped instance gets a dashed frame with the heading `CH_A (rc)` above
  it; `kat frame SCH CH_A` / `s.frame("CH_A")` draws one for any block instance. Drawing
  it again replaces it.

## 5. Command reference

Library: `kat.new`, `kat.load`, `kat.edit`, `kat.sync`, `kat.stamp`, `kat.check`, `kat.snapshot`, `kat.view`,
`kat.ready`, `kat.pcb.sync` / `edit` / `check` / `snapshot` / `view` / `route` / `refill` / `fab` /
`ready` / `drc` / `stamp` / `instances` / `block_environment` (board), `kat.hierarchy`,
`kat.skidl_blocks`, `kat.blocks`, `kat.nets`, `kat.lib_search`, `kat.lib_show`, `kat.comment`,
`kat.batch_exec`, `kat.open_kicad`, and the `kat.Sheet` / `kat.pcb.Board` methods; each returns
what the matching command prints. `kat api NAME` shows any of them (§4.14).

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
| `pcb refill PCB` | fill all pours and save them (after finishing) |
| `pcb fab PCB` / `pcb ready PCB` | fabrication set / release gate (§4.11) |
| `pcb route PCB [--passes] [--net NAME] [--install]` | Freerouting through kat's DSN/SES, then the board check (§4.10) |
| `pcb check PCB` / `pcb snapshot PCB` / `pcb view PCB ...` | board DRC + findings, history step with pictures, zoomed view (§4.12) |
| `pcb stamp PCB BLOCK_PCB INSTANCE... --at X,Y [--step DX,DY] [--rot R] [--no-lock]` | copy a checked block layout onto instances (`all`), then the board check (§4.18) |
| `pcb instances PCB BLOCK_PCB` / `pcb environment BLOCK_PCB ZONE...` | a block's instances in a design / its test-environment zones |
| `block new/build/check NAME [--lib DIR]`, `block list` | the block library (§4.19); BLOCK_PCB above may be a block name |
| `stamp SCH BLOCK INSTANCE... --at X,Y [--step DX,DY] --expect NETLIST [--no-frame]` / `frame SCH BLOCK` | draw block instances like the block's schematic, framed (§4.20) |
| `snapshot SCH [--slug] [--note] [--expect] [--tiles] [--finding-crops N]` | SVG + trimmed PNG + check into `history/`; tiles and finding close-ups |
| `view SCH --around REF / --at X,Y / --region X,Y,W,H` | zoomed PNG of one region into `views/` |
| `add`, `wire`, `connect`, `label`, `power`, `nc` | draw |
| `move`, `move-many`, `set`, `detach`, `rm` | rearrange (one part, or a block move), edit fields, disconnect (a pin or a whole part), delete |
| `page`, `title`, `text` | paper size, title block, free text (notes, headings) |
| `restore SCH STEP` | put the schematic back to a history step (undo a pass) |
| `batch-exec FILE.py [--project DIR] [-- ARGS]` | run a pass and journal it with its code (§4.13) |
| `comment [--kind plan/milestone/problem/note] TEXT` | the agent's markdown into the design report |
| `api [NAME] [--json]` | signatures and docstrings of the library (§4.14) |
| `open FILE [--verify]` | open in KiCad's editor; `--verify` compares what it loaded and closes it (§4.15) |

Global option `--no-report`: do not journal this command.

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
| `kicad-cli sch upgrade` silently drops a sheet's instance entry whose path is not the parent's real path (e.g. `/` in a sub-sheet) | `annotate` writes `/<root uuid>/<parent sheet uuids>` |
| KiCad re-saves sheet boxes in its own order | pages are numbered from the boxes' positions (reading order), not the file order |
| Stamping a block drawing that contains its PWR_FLAG gives one flag per instance: ERC `pin_to_pin` (two power outputs) | flags are not copied; `stamp` adds one per net where ERC needs it |
| SKiDL tags are local to a `@subcircuit`: two instances both have tag `r`, and matching by the bare tag pairs the wrong parts | the tag is qualified by the instance path (`CH_A/RC1/r`) from the netlist's sheet path |
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
| Freerouting's result depends on the order of components, pins and wires in the DSN; KiCad orders board items by their random uuids, so the same LED matrix routed with 0 unrouted in one run and 3 in the next | kat writes the DSN in reference order (tracks and vias sorted by net and position) |
| KiCad's IPC API socket is shared: with the user's KiCad open, a second editor would find `/tmp/kicad/api.sock` taken and the running instance might take over the file | `--verify` starts the editor with its own `TMPDIR` (short path under /tmp - Unix socket paths are limited to ~108 characters) and its own settings copy with the API enabled |
| kicad-python 0.8: `kipy.schematic` does not import (`PageSettings` missing); KiCad 10.0.6 answers schematic requests with "no handler" | schematic verification = the editor opens the file (lock file) and keeps running |
| Quitting the editor through the API (`common.Control.quit`) does nothing; SIGTERM closes it but leaves `~NAME.*.lck` behind | kat terminates only the editor it started and removes the lock files that appeared during the run |
| KiCad's lock file holds hostname and user, no process id | a lock counts as live when it is from another host or a KiCad process of this user runs; otherwise stale |
| Freerouting narrows tracks at fine-pitch pads below the net-class width (DRC `track_width`); `--router.automatic_neckdown=false` is not applied | on import kat widens them to the class width where the clearance allows and reports the rest (`narrow`) |
| A reference without a number (`ROUT`) passes ERC and parity, but KiCad treats it as unannotated: the BOM prints `ROUT?` | `kat ready` error `reference_without_number` |
| No `rsvg-convert` on Windows; `kicad-cli` not on PATH there | PyMuPDF fallback (`[render]` extra, AGPL-3.0); `kicad-cli` auto-discovery or `KICAD_CLI` |

## 8. Open points

- `sync` into hierarchical sheets (drawing and annotating them works, §4.17), buses, global
  labels across sheets.
- Teardrops, length tuning, differential pairs and impedance-controlled stack-ups are not
  automated; set them in KiCad or as custom rules.
- Live mode with kicad-python (editor open) is not implemented; the pipeline is headless.
  kicad-python is used only to verify a finished board (`kat open --verify`).
- SKiDL `NC` pins → no-connect flags in the drawing.
- Automatic arrangement beyond the placement strip (signal-flow placement) is left to the agent.
