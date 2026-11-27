# Stacked boards: 3D check and fixing collisions

`kat pcb mate --stack STACK.json` checks boards stacked on mezzanine connectors or standoffs.
It needs cqx (cadquery-agentic) on PATH or as `KAT_CQX`; without it, `--no-3d` runs only the
2D board-on-board checks.

## Stack file

```json
{"boards": {
   "main": {"pcb": "main/main.kicad_pcb", "variant": "A"},
   "io":   {"pcb": "io/io.kicad_pcb", "on": "main", "at": [12.5, 10], "rot": 0, "flip": true,
            "gap": 11.04, "pairs": [["J1", "P1"]], "holes": "auto"}},
 "clearance": 0.5}
```

- One board has no `on`; every other one lies on a parent. Paths are relative to the file.
- `at`, `rot`, `flip`, `below`: B's origin, turn and side in its parent's frame, as for
  `kat pcb mate`. Take them from the mechanical drawing. Prove them with the 2D part of the
  check (`links`: no `misaligned` pins, no `wrong_side`) before you trust anything in 3D.
- `gap`: mm between the facing board surfaces, which is the connector pair's **mated height**
  (datasheet) or the standoff length. Never tune it until the check passes.
- `pairs`: mating connectors `[ref on parent, ref on this board]`. They must be seated: the
  housings rest flush on each other. Add `"engage": "touch"` only for connectors whose housings
  do not meet when mated (datasheet).
- `clearance`: the least distance between parts of different boards, from the requirements
  (default 0.5 mm). `allow`: `[["main/H1", "io/STANDOFF1"]]` for pairs meant to touch or overlap.
  Add one only with the user's agreement.
- `variant`: an assembly variant, so its DNP parts are left out.

## Reading the result

The result has `ok`, `counts`, and one list per kind of finding, with parts named
`KEY/REF` and a board body named `KEY/body`. Look at the close-ups listed in `3d` before you
change anything.

| Finding | Means | Fix |
|---|---|---|
| `links` not ok | the stack file or the connectors' pinout is wrong | fix the placement or the boards first; the 3D result means nothing until this is clean |
| `not_seated` | the connectors do not mate as placed (`touching` false: too far apart) | check `gap` against the datasheet and the connector models' heights; do not move parts |
| `missing_models` | a fitted part is not in the 3D export and is invisible to the check | give the footprint a 3D model, or fix its path (KiCad warns about models it cannot find) |
| `collisions` | two parts, or a part and a board, overlap (`overlap_mm3`) | see below |
| `near` | closer than `clearance` (`distance`) | see below |

## Fixing a collision or near miss

Work in this order and stop at the first fix that works:

1. **Move the part on its own board** to where the other board leaves headroom. Usually
   it can go outside the other board's outline, or under a low part of it.
   `b.move(ref, (x, y))` in a pass refuses overlapping courtyards.
2. **Turn it, or put it on the other side** of its board, if that side faces away from the
   other board.
3. **A lower part** with the same function (a shorter capacitor, a right-angle connector)
   changes the schematic and BOM. Ask the user first.
4. **A larger `gap`** means other connectors or standoffs, which is a mechanical change. Ask
   the user first.

After each change: `kat pcb check` on the changed board (DRC and parity clean, re-route the
nets the move broke), then `kat pcb mate --stack` again. Finish when it says `ok`.
