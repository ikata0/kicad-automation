---
name: kicad-layout
description: Lay out a KiCad 10 PCB from the command line with `kat pcb` / `kat.pcb` — board built from the schematic (`kat pcb sync`, proven by DRC schematic parity), fab minimums, outline and net classes with the layers and class-to-class clearances the router keeps, placement passes in Python, library blocks stamped with their copper and port points, structure (plane vias, fan-out, locked critical tracks), Freerouting through kat's own Specctra DSN/SES, silkscreen and fabrication files, judged by KiCad's DRC and rendered layer views and gated by `kat pcb ready`, closed with a report on kat's bugs and missing features (KAT_FEEDBACK.md). Use when asked to create, place, route, finish or check a PCB, or to bring a schematic change onto the board, without the KiCad GUI.
---

# KiCad PCB layout with `kat pcb`

`kat pcb --help` and
`kat pcb <action> --help` explain every command. "Layout" means the PCB; the schematic
side is the `kicad-schematic` skill.

Read [block workflow](../kicad-design/references/blocks.md) for reusable circuits,
[stamping](references/stamping.md) before placing checked block layouts, and
[routing](references/routing.md) for partial routing or router/DRC failures.
Environment requirements: [setup](../kicad-design/references/setup.md).

## Rules

1. **Connectivity comes from the schematic only.** Never connect anything on the board that
   the schematic does not connect; `track` refuses pads of different nets, and
   `kat pcb check` reports `parity`. A needed circuit change (pin swap, extra part) goes
   SKiDL → `kat sync` → `kat pcb sync`.
2. Start only when `kat ready SCH --expect design.net` says `ready` (footprints, MPNs).
3. KiCad GUI closed while kat edits the board (kat refuses while KiCad's lock file says the
   board is open).
4. **Numbers first, pictures second.** DRC counts (`unconnected`, `islands`, clearances,
   `parity`) are exact; pictures show what numbers cannot (fan-out, detours, crowding).
5. Never trust the router's own "all routed": check `check.unconnected` and `check.islands`.

`ok` means no DRC errors, parity errors or unconnected items, with an outline present.
`clean` also requires no DRC warnings or kat findings. Release `ready` additionally
requires title/revision and current fabrication outputs: copper/mask/outline Gerbers,
drills, position file, BOM and IPC-D-356. Review warnings explicitly; accepted warnings
need reasons, intentional edge overhang needs named references. DRC errors never pass.

`kat open PCB --verify` compares editor-loaded footprints, nets, copper counts, zones and
outline. `verified: null` is unavailable verification, not success; report the reason.
Length tuning, differential pairs, teardrops and impedance-controlled stack-ups are not
automated: do not claim those requirements are satisfied by autorouting or generic DRC.

## Loop

Every step goes into the project's design report `NAME_REPORT.md` (see "Journal" below):
kat commands by themselves, passes through `kat batch-exec`, your reasoning through
`kat comment`.

1. **Sync**: `kat pcb sync PCB` (creates the board; later brings design changes). Read
   `added`, `replaced`, `removed`, `renamed_nets`, `stale_tracks`.
2. **Setup pass** (`setup.py`): outline, layer count, planes, mounting holes, net classes
   from the specification (current → width, voltage → clearance), keep-outs, pours. It is
   safe to run again: when the specification changes, edit it and re-run it.
3. **Placement passes** (`place1.py`, `place2.py`, ...); **snapshot** after each:
   `kat pcb snapshot PCB --slug place1 --note ...`. Review `placement.png`, then
   `top.png`/`bottom.png`, close-ups with `kat pcb view`.
4. **Structure pass**: plane vias (`fanout`), critical nets by hand, lock them.
5. **Route**: `kat pcb route PCB` → check → snapshot → review. If unconnected or islands:
   change structure/keep-outs/classes, `remove_tracks()` (locked stays), route again.
6. **Finish pass**: `place_refs`, board text, title, fiducials; then `kat pcb refill PCB`.
7. **Fabricate**: `kat pcb fab PCB`; `kat open PCB --verify` (KiCad's editor loads the board
   and reports the same footprints, nets, copper and outline); `kat pcb ready PCB` must say
   `ready`. Then ask the user to open the board in KiCad and look at it.
8. **Report on kat**: `KAT_FEEDBACK.md` (below), before you hand over.

A **pass** is a short Python script you write and run with `kat batch-exec passN.py`. It
starts with a docstring stating what the last review found and what this pass changes, then
edits inside `with kat.pcb.edit(PCB) as b:` - saved once at the end, nothing saved if it
raises. There are no CLI commands for setup, placement or structure: positions computed in
loops and relative to other parts are clearer in Python.

## Journal

- `kat batch-exec passN.py` runs the pass and records its docstring, its code as written
  (loops stay loops), its output, errors and the images of snapshots it takes.
- `kat comment --kind plan "..."` before a pass or phase: what you will do and why.
- `kat comment --kind problem "..."` when a check fails and you iterate (what failed,
  what you will change).
- `kat comment --kind milestone "..."` at the end of a phase: placed, routed, ready.
- CLI commands (`sync`, `check`, `route`, `snapshot`, `fab`, `ready`, ...) are recorded by
  themselves, with their results; snapshots with their pictures.

## API lookup

`kat api` lists the library; `kat api Board` the board methods; `kat api Board.keepout`
one call with its signature and documentation. Calls by phase:

| Phase | `b.` methods |
|---|---|
| setup | `constraints`, `outline`, `set_copper_layers`, `plane`, `set_layer_type`, `zone`, `keepout`, `netclass`, `class_clearance`, `rules`, `mounting_hole`, `add_board_part` |
| placement | `move`, `move_many`, `flip`, `align`, `distribute`, `place_near`, `collisions`, `near`, `blocks`, `move_block`, `replicate` |
| blocks | `stamp`, `instances`, `ports` (and `kat.pcb.block_environment`, `kat.pcb.block_port`) |
| structure | `track`, `via`, `via_to_plane`, `fanout`, `stitch`, `lock`, `remove_tracks`, `remove`, `remove_at`, `net_rules` |
| finishing | `place_refs`, `board_text`, `title_from_schematic`, `fiducials` |
| reading | `footprints`, `footprint(ref).pads()`, `pads`, `tracks`, `vias`, `zones`, `outline_box`, `copper_layers` |

## Recipes

```python
"""setup: 132 x 100 mm, 4 layers with a GND plane on In1, +5V class, holes, no copper
under the connector latches."""
import kat

PCB = "board/board.kicad_pcb"
with kat.pcb.edit(PCB) as b:
    b.outline(rect=(0, 0, 132, 100), radius=2)
    b.set_copper_layers(4)
    b.plane("GND", "In1.Cu")                     # layer type power: the router lays no tracks
    b.zone("+5V", "In2.Cu", priority=1)          # a pour; name defaults to "+5V In2.Cu"
    b.keepout(["F.Cu", "B.Cu"], rect=(2, 40, 6, 20), vias=False, pour=False)
    # the fab's minimums first; J1 is an edge-mount SMA (pads at the edge)
    b.constraints(min_track=0.1, min_clearance=0.1, min_via=0.45, min_via_drill=0.2,
                  hole_clearance=0.2, edge_clearance=0.3, edge_exempt=["J1"])
    b.netclass("POWER", track=0.5, clearance=0.25, nets=["+5V", "GND"])
    b.netclass("ANALOG", track=0.2, clearance=0.2, nets=["/AIN"], layers=["F.Cu", "In2.Cu"])
    b.netclass("SPI", track=0.15, clearance=0.1, nets=["/SPI_*"], layers=["F.Cu", "B.Cu"])
    b.class_clearance("SPI", "ANALOG", 1.0)      # DRC rule and the router's rule
    for x, y in ((3.5, 3.5), (128.5, 3.5), (3.5, 96.5), (128.5, 96.5)):
        b.mounting_hole((x, y), "M3")
```

```python
"""place 1: connectors on the left, the matrix as the board is read."""
import kat

PCB = "board/board.kicad_pcb"
with kat.pcb.edit(PCB) as b:
    moves = {"J1": {"to": (10, 10)}, "J2": {"to": (24, 10)}}
    for n in range(1, 101):                      # repeated cells: a loop, not 200 lines
        r, c = divmod(n - 1, 10)
        moves[f"R{n}"] = {"to": (40 + 9 * c, 12 + 8.5 * r)}
        moves[f"D{n}"] = {"to": (42.6 + 9 * c, 12 + 8.5 * r), "rot": 180}  # anode faces R
    b.move_many(moves)                           # checked on the final arrangement
kat.pcb.snapshot(PCB, slug="place1")             # its pictures go into the batch entry
```

```python
"""structure 1: every cathode reaches the GND plane; R-LED links laid and locked."""
import kat

PCB = "board/board.kicad_pcb"
with kat.pcb.edit(PCB) as b:
    res = b.fanout([f"D{n}" for n in range(1, 101)], nets=["GND"])
    assert not res["failed"], res["failed"]
    for n in range(1, 101):
        b.track([f"R{n}.2", f"D{n}.2"], lock=True)
    b.place_near("C1", "U1.8")                   # decoupling after the IC's plane vias
```

- **Planes**: give a plane its own layer (`b.plane(net, "In1.Cu")`). On two layers a pour on
  a routing layer gets cut into islands, and a track keep-out on the plane layer does not
  work with Freerouting (it counts the plane vias inside as violations). If two layers
  must do: pours on both sides, stitching vias, and check `islands`/`unconnected`.
- **Pours and keep-outs** are named; running setup again replaces them by name (several
  pours of one net on one layer need their own `name=`). A keep-out forbids what is set to
  `False`: `tracks` (default), `vias`, `pads`, `pour`, `footprints`.
- Order: fan out an IC's plane pads first, then `place_near` its decoupling (it keeps
  clear of copper already laid), then fan out the capacitors.
- Functional blocks: SKiDL `@subcircuit` instances (`Block = CH_A/RC1`, nested levels
  included), or `part.fields["Block"] = "ch1"` set by hand; then `b.blocks()`,
  `b.move_block("CH_A", to=(x, y))`, and `b.replicate("CH_A", {"CH_B": (x, y)})` arranges
  the other blocks like the first (parts pair by SKiDL tag without the block name).
- Repeated blocks, block first: lay the block out once on its own board (its parts, copper,
  keep-outs), get `kat pcb check` clean there, then `b.stamp(BLOCK_PCB, {"/CH_A": (x, y),
  ...})` or `kat pcb stamp PCB BLOCK_PCB all --at X,Y --step DX,DY` onto every instance,
  and route only what connects the blocks. Change the block board and stamp again; never
  edit a stamped copy by hand (see [stamping](references/stamping.md)). Re-stamping
  replaces the instance group's copied items, including its copper. On the block board:
  - name its test environment with `kat.pcb.block_environment`: the plane it relies on,
    and keep-outs that stand for the neighbouring blocks, so it is routed as it will sit
    in the design (none of them is stamped);
  - route it without its harness connector, or with it: the harness's routes are not
    stamped either way;
  - end each port on a locked via near the outline's edge and mark it,
    `kat.pcb.block_port(BLOCK_PCB, "ain", (x, y))`; after the stamp `b.ports("/ROW3")`
    gives the points the top level connects;
  - route it with the clearance its port nets will have in the design: a design class
    asking more is refused at the stamp.
  `stamp(..., make_room=True)` pushes design parts out of the block's way.
- Bottom-side parts (`side="B"`) are mirrored top to bottom: a two-pad part keeps its
  pad order at rot 0, parts with pins off their axis do not. Check them in the bottom view.
- Placement order: mechanical/fixed, connectors, critical analog parts and heat sources,
  decoupling, the rest. Keep sensitive nodes short and away from switching nets; give
  analog returns their own path to the plane.
- `b.move` refuses overlapping courtyards; `b.collisions()` lists them; `b.near(point)`
  tells what is at a spot; `b.net_rules(net)` gives width/clearance/via of its class.
- Stitching vias last: `b.stitch("GND", pitch=5)`.
- Finishing: `b.place_refs()`, `b.board_text("NAME rev A", (x, y))`,
  `b.title_from_schematic()`, `b.fiducials(3)`, then `kat pcb refill PCB`.
- A wrong via or track, locked or not: `b.remove_at((x, y), kind="via")` or
  `kat pcb remove PCB --at X,Y` - no board rebuild needed.
- The route result's `fine_pitch` names classes too wide to leave a fine-pitch pin; give
  those nets a narrower class instead of letting the router neck them.

## Review sub-loop

1. `check.json` / `kat pcb check`: `drc_errors`, `unconnected`, `islands`, `parity`,
   `outside_outline`, `by_type`. Each finding with a position → `--finding-crops N` or
   `kat pcb view PCB --at X,Y --layers copper`.
2. Overview pictures: `placement.png` (courtyards, fab), `top.png`, `bottom.png`,
   `In<n>.png`; `tiles/` when the board is large; `3d_top.png` for a last look.
3. Close-ups: `kat pcb view PCB --around U1 --layers copper` (sets: top, bottom, placement,
   copper, In1…, or `F.Cu,F.SilkS`). Pixel → mm: `x = region[0] + px / px_per_mm`.
4. Write down what you found; it goes into the next pass's docstring.
5. Warnings you judge harmless (a port via joined on one layer only: `via_dangling`) and
   edge-mount parts (`overhang` in the check) pass the gate only when named:
   `kat pcb ready PCB --accept via_dangling="port vias of the rows" --overhang J1`.

## Design change during layout

SKiDL → `kat sync SCH design.net` (+ schematic snapshot/arrangement) → `kat ready` →
`kat pcb sync PCB`. The board keeps placement and copper; a changed footprint is replaced
in place; `stale_tracks` lists copper that no longer fits (`--rip-stale` removes it).
Then place the new parts, re-run structure/route for the affected nets (`--net NAME`).

## Last step: report on kat

The task has two results: the board, and what it taught about kat. Before you hand over
(also when you stop early), write `KAT_FEEDBACK.md` next to `NAME.kicad_pro`, or add to it
when it exists. Note each finding when it happens, not from memory at the end:
- **Bugs**: kat did something wrong. Command or pass, expected / got (error text, DRC
  numbers), your workaround, reproduced or not.
- **Improvements**: it works but cost time or passes (a route needing hand-laid copper,
  a check that had to be accepted); what would have helped.
- **Feature requests**: something kat cannot do; the task that needed it.
- **Documentation**: this skill, its references or `--help` missing or wrong; where you looked.

Number the entries (B1, I1, F1, D1), keep facts apart from guesses, write "none" for an
empty section, and don't change kat's own code unless asked. Template: kicad-design skill,
"Last step". Name the file in your handover.

## Setup

As for the schematic skill, plus Freerouting for routing: Java 21+, then
`kat pcb route --install` (downloads the pinned release once into kat's cache). For
`kat open --verify` on boards: `pip install 'kat[gui]'` (kicad-python) and a display.
