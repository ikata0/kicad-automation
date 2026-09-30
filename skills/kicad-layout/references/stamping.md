# Checked PCB block stamping

Read before stamping layouts; first follow
[block workflow](../../kicad-design/references/blocks.md).

`kat pcb stamp PCB RC all --at X,Y --step DX,DY` or `b.stamp(...)` applies a
checked source layout. Instances are paths such as /CH_A/RC1. Hierarchical
instances pair by symbol path; flat instances pair by relative SKiDL tag.
Use `b.instances(block)` to inspect the matches.

The block outline's top-left goes to at; rotations are 0/90/180/270 degrees.
Parts keep their side; a mirrored block requires a separate source board.
The design must have the block's copper layers. Unpaired parts, differing
footprints or a conflicting net mapping are refused.

Copied items include part placement, copper, named zones/keepouts, non-outline
graphics and board-only parts. Copper is normally locked. Re-stamping removes
previous copied items in the instance group and applies the source anew:
do not edit instance copper expecting it to survive.

Harness parts are excluded, as are whole routes serving only the harness.
Use block_environment to name test planes/neighbor keepouts that must not be
copied. Mark each port with `kat pcb port BLOCK_PCB PORT --at X,Y`, preferably
on a locked via near the edge, and read transformed points with b.ports(instance).

Design net classes asking stricter clearance than the block's are refused;
route/check the source with the intended clearance first. A track-width
difference is a warning, not permission to ignore current/fabrication needs.

Overlapping foreign courtyards are refused. --make-room shifts unlocked design
parts aside; review the changed placement. Run kat pcb check, route remaining
top-level connections, snapshot, and inspect each affected area.
