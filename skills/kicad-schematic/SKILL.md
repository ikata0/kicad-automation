---
name: kicad-schematic
description: Develop KiCad 10 schematics from the command line with the `kat` tool — circuit defined in SKiDL, brought into the .kicad_sch with `kat sync` (incremental, keeps the arrangement), arranged for readability with Python passes (`import kat`), verified by ERC, netlist comparison and rendered snapshots. Use when asked to create, update after a design change, arrange, tidy or check a KiCad schematic without the KiCad GUI.
---

# KiCad schematics with SKiDL + `kat`

Full specification: `SPEC.md` in the kicad-automation repo. `kat --help` and
`kat <command> --help` explain every command with examples.

"Arrange" / "arrangement" below means placing things on the schematic sheet, never PCB
layout.

## Rules

1. **Connectivity lives in the SKiDL design only.** In the drawing you only arrange things;
   `kat check --expect design.net` must keep matching.
2. Every SKiDL part gets `tag=` and `ref=`; name the nets that matter (`Net("TIA_OUT")`).
3. KiCad GUI closed while editing.
4. Snapshot after every sync and every arrangement pass.

## Loop

1. **Design**: write/modify the SKiDL file, run it → `design.net`.
2. **Sync**: `kat sync SCH design.net` (first time after `kat new SCH`). Read the report:
   `added`, `renamed`, `detached`/`attached`, `removed`, `unresolved` tell you where to look.
3. **Snapshot**: `kat snapshot SCH --slug NAME --expect design.net --note "what changed"`.
4. **Review** the PNG it wrote (Read it). Look for: text touching text, symbols or wires;
   parts parked in the import rows below the drawing; signal flow (inputs left, outputs right,
   supplies up, ground down); labels that would read better as wires.
   `check.json` lists `overlaps`, `body_overlaps`, `off_page`, `duplicates`, `unnamed` —
   hints, not a verdict.
5. **Arrange** the drawing with a Python pass, `python passN.py`. It starts with a docstring
   that states what the review found and edits inside `with kat.edit(SCH) as s:`, which
   saves once, or not at all if anything raises. Then back to 3.

**Stop when** `check.json` says `"clean": true` and the image shows no problems. Report
remaining ERC warnings instead of polishing forever.

## Arrangement recipes

`s` is a `kat.Sheet`; every `kat` editing command is a method with the same arguments
(`kat move SCH R1 --to X,Y` = `s.move("R1", to=(x, y))`). Points: `"REF.PIN"` or `(x, y)`.

```python
"""pass 2: <what the review found>."""
import kat

with kat.edit("board/board.kicad_sch") as s:
    # arrange
    s.move("U1", unit=1, to=(127, 101.6), mirror="x")
    s.move("R1", to=(127, 86.36), rot=90)
    # replace a label pair by a wire, keep the net name visible
    for p in ("R1.1", "U1.2"):
        s.detach(p)
    s.connect("R1.1", "U1.2", via=[(114.3, 86.36)])
    s.label("PD_ANODE", (104.14, 99.06))
    # fields
    s.set("U1", "Value", unit=2, at=(147.32, 127))
    s.set("#FLG1", "Value", hide=True)
    # power symbol pointing the wrong way after a mirror
    s.detach("J2.2")
    s.power("GND", "J2.2", stub=(-5.08, 0))
```

Repeated structures (rows of LEDs, connector pins): write a function for one cell and call
it in a loop, instead of spelling out every line. Use `s.pins(REF)` to read pin positions
and outward directions while computing coordinates. Try a large pass with
`kat.edit(SCH, dry_run=True)` first.

## Behaviour worth knowing

- `sync` is idempotent and matches parts by SKiDL tag, then reference. New parts appear in
  rows below the drawing (partners on unnamed two-pin nets side by side, joined by a wire)
  with stub + label wiring elsewhere; the paper grows if they do not fit; power nets get power symbols;
  PWR_FLAGs are added for undriven supplies.
- `move` carries what hangs only off the part (stubs, labels, power symbols, flags) and
  drags wires into shared nets. Steps left by dragging: `detach` + `connect`.
- `move` refuses to put a pin on another pin, a wire or a label (it would join nets). To
  rearrange many parts, or let parts take each other's places, use one
  `s.move_many({"R1": {"to": (x, y), "rot": 90}, ...})` — no parking needed.
- `detach` warns when it removed a PWR_FLAG; `sync` re-adds a missing one.
- `detach REF.PIN` removes the pin's own wiring up to the next branch point, including labels
  and power symbols on it. Follow it with `connect`/`label`/`power` in the same pass.
- Coordinates: mm, y down, 1.27 mm grid; parts on multiples of 2.54 mm.
- Junctions are maintained automatically.
- Never fix connectivity in the drawing — if `check` reports `missing`/`extra`, either your
  arrangement pass broke something (undo by reverting the file from the last snapshot) or the
  design is wrong (fix SKiDL and sync).

## Setup

`pip install -e <kicad-automation>[dev]` (SKiDL included; add `render` on systems without
`rsvg-convert`, e.g. Windows). Needs `kicad-cli` on PATH or `KICAD_CLI`; SKiDL needs
`KICAD10_SYMBOL_DIR` pointing at KiCad's symbol directory.
