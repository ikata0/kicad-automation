---
name: kicad-schematic
description: Develop KiCad 10 schematics from the command line with the `kat` tool — circuit defined in SKiDL, brought into the .kicad_sch with `kat sync` (incremental, keeps the arrangement), arranged for readability with Python passes (`import kat`), verified by ERC, netlist comparison and rendered snapshots, and gated for the PCB with `kat ready`. Use when asked to create, update after a design change, arrange, tidy or check a KiCad schematic without the KiCad GUI, or to get it ready for PCB layout.
---

# KiCad schematics with SKiDL + `kat`

Full specification: `SPEC.md` in the kicad-automation repo. `kat --help` and
`kat <command> --help` explain every command with examples. Phases and gates:
`kicad-design` skill; the PCB: `kicad-layout` skill.

"Arrange" / "arrangement" below means placing things on the schematic sheet, never PCB
layout.

## Rules

1. **Connectivity lives in the SKiDL design only.** In the drawing you only arrange things;
   `kat check --expect design.net` must keep matching.
2. Every SKiDL part gets `tag=` and `ref=`; name the nets that matter (`Net("TIA_OUT")`).
   Every `@subcircuit` call gets keyword arguments and `tag=` (the instance name); after
   `generate_netlist`, call `kat.skidl_blocks.write("design.net")` (SPEC §4.16).
3. KiCad GUI closed while editing (kat refuses while KiCad's lock file says it is open).
4. Snapshot after every sync and every arrangement pass.
5. Keep the design report readable: passes run with `kat batch-exec`, your plans, problems
   and milestones go in with `kat comment --kind plan|problem|milestone "..."` (markdown).
   Every kat command on the project is recorded in `NAME_REPORT.md` by itself.

## Loop

1. **Design**: write/modify the SKiDL file, run it → `design.net`.
2. **Sync**: `kat sync SCH design.net` (first time after `kat new SCH`). Read the report:
   `added`, `renamed`, `detached`/`attached`, `removed`, `unresolved` tell you where to look.
3. **Snapshot**: `kat snapshot SCH --slug NAME --expect design.net --note "what changed"`.
4. **Review** — overview first, then details (sub-loop below). Look for: text touching
   text, symbols or wires; parts parked in the import rows below the drawing; signal flow
   (inputs left, outputs right, supplies up, ground down); labels that would read better
   as wires. `check.json` lists `overlaps`, `body_overlaps`, `off_page`, `duplicates`,
   `unnamed` — hints, not a verdict.
5. **Arrange** the drawing with a Python pass, `kat batch-exec passN.py` (runs it like
   `python passN.py` and records its code in the report). It starts with a docstring that
   states what the review found and edits inside `with kat.edit(SCH) as s:`, which saves
   once, or not at all if anything raises. Then back to 3. `kat api Sheet` lists the editing
   verbs, `kat api Sheet.move` explains one.

**Stop when** `check.json` says `"clean": true` and the review — at readable resolution,
over the whole sheet — shows no problems. Report remaining ERC warnings instead of
polishing forever.

**Before PCB layout**: `kat ready SCH --expect design.net` must say `"ready": true`;
`kat open SCH --verify` shows that KiCad's schematic editor opens the file.
Fix its errors in SKiDL, then sync (step 1–2): `footprint="Lib:Name"` on every part
(`kat ready` names the library/file it cannot find and pins without a pad),
`part.fields["MPN"] = "..."` for the chosen part. Look at each warning and say in the
report why it is fine (e.g. an extra pad for the exposed thermal pad, a footprint outside
the symbol's filters on purpose).

## Review sub-loop (step 4)

Image viewers downscale large images (about 2000 px), so on big sheets the overview shows
layout but not pin numbers or label text. Zoom in until every question is answered:

1. **Overview**: Read `history/<step>/<name>.png`. Note the spots that look wrong or that
   you cannot read.
2. **Tiles**: if the snapshot result lists `tiles` (automatic above 2000 px, i.e. about
   A3 and larger), Read every tile once — each has its `region_mm`.
3. **Close-ups**: for each noted spot and each finding with a position, render a view and
   Read it:
   `kat view SCH --around U1` · `kat view SCH --at X,Y [--margin 8]` ·
   `kat view SCH --region X,Y,W,H` (or `kat.view(...)` in Python). A pixel maps back to
   sheet mm as `x = region[0] + px / px_per_mm`. `kat snapshot ... --finding-crops 20`
   renders the findings in one go.
4. Still unsure about something? Take the next view (closer, or next to it). Views are
   cheap (under a second) and are not history steps.
5. Write down what the review found; it goes into the next pass's docstring.

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
  arrangement pass broke something (undo: `kat restore SCH history/<last good step>`) or the
  design is wrong (fix SKiDL and sync).
- `s.detach("R1")` frees every pin of a part; `s.near((x, y), radius=3)` lists what is at a
  spot (with uuids for `s.rm(uuid=...)`).
- Finish the sheet: `s.title(title=..., rev=..., date=..., comments={1: ...})`, notes and
  headings with `s.text("J1: rows 1-5", (x, y), size=2.54)`, paper with `s.page("A2")`.
- Part data such as MPN belongs in SKiDL (`part.fields["MPN"] = "..."`); sync carries it.

## Setup

`pip install -e <kicad-automation>[dev]` (SKiDL included; add `render` on systems without
`rsvg-convert`, e.g. Windows). Needs `kicad-cli` on PATH or `KICAD_CLI`; SKiDL needs
`KICAD10_SYMBOL_DIR` pointing at KiCad's symbol directory.
