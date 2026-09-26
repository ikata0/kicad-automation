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
3. KiCad GUI closed while kat edits the board.
4. **Numbers first, pictures second.** DRC counts (`unconnected`, `islands`, clearances,
   `parity`) are exact; pictures show what numbers cannot (fan-out, detours, crowding).
5. Never trust the router's own "all routed": check `check.unconnected` and `check.islands`.

## Loop

1. **Sync**: `kat pcb sync PCB` (creates the board; later brings design changes). Read
   `added`, `replaced`, `removed`, `renamed_nets`, `stale_tracks`.
2. **Setup** (once, and when the spec changes): outline, layer count, mounting holes, net
   classes from the specification (current → width, voltage → clearance), keep-outs, pours.
3. **Place** in a Python pass; **snapshot**: `kat pcb snapshot PCB --slug place1 --note ...`.
   Review `placement.png`, then `top.png`/`bottom.png`, close-ups with `kat pcb view`.
4. **Structure**: plane vias (`fanout`), critical nets by hand, lock them.
5. **Route**: `kat pcb route PCB` → check → snapshot → review. If unconnected or islands:
   change structure/keep-outs/classes, `remove_tracks()` (locked stays), route again.
6. **Finish**: `place_refs`, board text, title, fiducials, `kat pcb refill PCB`.
7. **Fabricate**: `kat pcb fab PCB`, then `kat pcb ready PCB` must say `ready`.

A pass starts with a docstring stating what the last review found, like arrangement passes.

## Recipes

```python
"""place 1: 4 layers with a GND plane, connectors on the left, matrix as the board is read."""
import kat

PCB = "board/board.kicad_pcb"
with kat.pcb.edit(PCB) as b:
    b.outline(rect=(0, 0, 132, 100), radius=2)
    b.set_copper_layers(4)
    b.plane("GND", "In1.Cu")                     # layer type power: the router lays no tracks
    b.mounting_hole((3.5, 3.5), "M3")
    b.netclass("POWER", track=0.5, clearance=0.25, nets=["+5V"])
    moves = {"J1": {"to": (10, 10)}, "J2": {"to": (24, 10)}}
    for n in range(1, 101):                      # repeated cells: a loop, not 200 lines
        r, c = divmod(n - 1, 10)
        moves[f"R{n}"] = {"to": (40 + 9 * c, 12 + 8.5 * r)}
        moves[f"D{n}"] = {"to": (42.6 + 9 * c, 12 + 8.5 * r), "rot": 180}  # anode faces R
    b.move_many(moves)                           # checked on the final arrangement
```

```python
"""structure 1: every cathode reaches the GND plane; R-LED links laid and locked."""
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
- Order: fan out an IC's plane pads first, then `place_near` its decoupling (it keeps
  clear of copper already laid), then fan out the capacitors.
- Functional blocks: set `part.fields["Block"] = "ch1"` in SKiDL; then `b.blocks()`,
  `b.move_block("ch1", to=(x, y))`, and `b.replicate("ch1", {"ch2": (x, y)})` arranges the
  other blocks like the first (parts pair by SKiDL tag without the block name).
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
`kat pcb route --install` (downloads the pinned release once into kat's cache).
