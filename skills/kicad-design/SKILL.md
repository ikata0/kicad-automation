---
name: kicad-design
description: Overview of agent-driven KiCad 10 hardware design with `kat` - the phases (circuit in SKiDL, schematic, PCB layout, fabrication), the gates between them and the one route for design changes. Use at the start of a KiCad project or when unsure which phase or skill applies; then follow kicad-schematic or kicad-layout.
---

# KiCad design with SKiDL + `kat`: phases and gates

| Phase | Tool | Skill | Gate to leave it |
|---|---|---|---|
| 1. Circuit | SKiDL (`tag=`, `ref=`, named nets, `footprint=`, `part.fields["MPN"]`) | kicad-schematic | netlist generates, ERC-worthy |
| 2. Schematic | `kat sync`, arrangement passes, `kat snapshot` | kicad-schematic | `kat ready SCH --expect design.net` → `ready` |
| 3. Layout | `kat pcb sync/…/route`, layout passes, `kat pcb snapshot` | kicad-layout | `kat pcb check` clean |
| 4. Fabrication | `kat pcb fab` | kicad-layout | `kat pcb ready PCB` → `ready` |

**One source of truth.** Connectivity lives in the SKiDL design. Whatever phase you are in,
a circuit change goes: SKiDL → `kat sync` (schematic, then arrange + snapshot) →
`kat ready` → `kat pcb sync` (board keeps placement and copper). Never edit connectivity in
the schematic drawing or on the board; `check --expect` and DRC schematic parity catch it.

**Going back is normal.** Layout often shows that a pin swap, another package or an extra
part is better. Make the change in SKiDL, pass the gates again, and continue the layout
where it was; `kat pcb sync` reports what moved or became stale.

**Record the work.** Every sync and pass gets a snapshot with a note; history steps are the
undo (`kat restore` for the schematic; board copies in `history/<stamp>_pcb_*/`).
