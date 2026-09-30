# Flat schematic stamping

Read before `kat stamp` draws repeated block instances. First build/check and
arrange the library block, as described in
[block workflow](../../kicad-design/references/blocks.md).

```text
kat stamp top.kicad_sch RC all --expect design.net --plan
kat stamp top.kicad_sch RC all --at 50.8,50.8 --step 0,40.64 --expect design.net
```

Parts pair by block instance and relative SKiDL tag. The source drawing is
NAME_block.kicad_sch, or NAME_flat.kicad_sch for a block containing blocks.
Build a missing flat drawing with `kat block build NAME`.

A stamp detaches the instance's pins, applies the template arrangement and
copies reachable wires, labels, junctions, no-connects and power symbols. Labels
use the consuming instance's net names. PWR_FLAGs are not copied per instance;
they are added where the complete design needs them.

For a source without a reusable block drawing, harness connections must be
labels: a direct wire from a block pin to a harness pin is refused.

Each target area includes fields/labels and frame space. Foreign drawing in it
is refused before edits; --plan reports areas/obstacles without saving.
--make-room can shift parts with private wiring, framed instances and boxes,
never wiring joining several parts. Without --at, kat prefers the current
location, then free areas. Enlarge the paper when room cannot be made safely.

Re-stamping reapplies the template, not a merge preserving instance-local
arrangement edits. Improve the source drawing instead. Connectivity is checked
against --expect after saving; a mismatch restores the original file and fails.
Snapshot and review after stamping. `kat frame SCH CH_A` draws a frame without
changing connectivity.
