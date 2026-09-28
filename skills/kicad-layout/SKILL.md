---
name: kicad-layout
description: Lay out a KiCad 10 PCB from the command line with `kat pcb` / `kat.pcb` — board built from the schematic (`kat pcb sync`, proven by DRC schematic parity), outline and net-class rules, placement passes in Python, structure (plane vias, fan-out, locked critical tracks), Freerouting through kat's own Specctra DSN/SES, silkscreen and fabrication files, judged by KiCad's DRC and rendered layer views. Use when asked to create, place, route, finish or check a PCB, or to bring a schematic change onto the board, without the KiCad GUI.
---

# KiCad PCB layout with `kat pcb`

Full specification: `SPEC.md` §4.6–4.12 in the kicad-automation repo. `kat pcb --help` and
`kat pcb <action> --help` explain every command. "Layout" means the PCB; the schematic
side is the `kicad-schematic` skill.

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
| setup | `outline`, `set_copper_layers`, `plane`, `set_layer_type`, `zone`, `keepout`, `netclass`, `rules`, `mounting_hole`, `add_board_part` |
| placement | `move`, `move_many`, `flip`, `align`, `distribute`, `place_near`, `collisions`, `near`, `blocks`, `move_block`, `replicate` |
| structure | `track`, `via`, `via_to_plane`, `fanout`, `stitch`, `lock`, `remove_tracks`, `net_rules` |
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
    b.netclass("POWER", track=0.5, clearance=0.25, nets=["+5V", "GND"])
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
- Placement order: mechanical/fixed, connectors, critical analog parts and heat sources,
  decoupling, the rest. Keep sensitive nodes short and away from switching nets; give
  analog returns their own path to the plane.
- `b.move` refuses overlapping courtyards; `b.collisions()` lists them; `b.near(point)`
  tells what is at a spot; `b.net_rules(net)` gives width/clearance/via of its class.
- Stitching vias last: `b.stitch("GND", pitch=5)`.
- Finishing: `b.place_refs()`, `b.board_text("NAME rev A", (x, y))`,
  `b.title_from_schematic()`, `b.fiducials(3)`, then `kat pcb refill PCB`.

## Review sub-loop

1. `check.json` / `kat pcb check`: `drc_errors`, `unconnected`, `islands`, `parity`,
   `outside_outline`, `by_type`. Each finding with a position → `--finding-crops N` or
   `kat pcb view PCB --at X,Y --layers copper`.
2. Overview pictures: `placement.png` (courtyards, fab), `top.png`, `bottom.png`,
   `In<n>.png`; `tiles/` when the board is large; `3d_top.png` for a last look.
3. Close-ups: `kat pcb view PCB --around U1 --layers copper` (sets: top, bottom, placement,
   copper, In1…, or `F.Cu,F.SilkS`). Pixel → mm: `x = region[0] + px / px_per_mm`.
4. Write down what you found; it goes into the next pass's docstring.

## Design change during layout

SKiDL → `kat sync SCH design.net` (+ schematic snapshot/arrangement) → `kat ready` →
`kat pcb sync PCB`. The board keeps placement and copper; a changed footprint is replaced
in place; `stale_tracks` lists copper that no longer fits (`--rip-stale` removes it).
Then place the new parts, re-run structure/route for the affected nets (`--net NAME`).

## Setup

As for the schematic skill, plus Freerouting for routing: Java 21+, then
`kat pcb route --install` (downloads the pinned release once into kat's cache). For
`kat open --verify` on boards: `pip install 'kat[gui]'` (kicad-python) and a display.
