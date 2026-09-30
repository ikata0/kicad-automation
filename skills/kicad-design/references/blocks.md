# Reusable block workflow

Read before creating reusable circuits, stamping them, or changing an existing
block. A block is designed, drawn, laid out and checked once in the library;
design instances consume it, not independently edited copies.

Kat finds the library through `KAT_BLOCKS`, otherwise the nearest `blocks`
directory in the working directory or its parents. CLI block commands also
accept `--lib DIR`.

1. `kat block new NAME` creates `blocks/NAME/NAME.py` and a project scaffold.
2. Implement its SKiDL subcircuit and a test harness under `__main__` calling it
   with `tag="DUT"`. Give local parts stable tags; call nested blocks with keyword
   arguments and tags. Generate the netlist, then call
   `kat.skidl_blocks.write(...)` to record ports, parameters and no-connects.
3. `kat block build NAME` runs the harness and builds/syncs its drawings.
4. Arrange the reusable drawing, lay out the block, define environment and port
   points, and iterate until `kat block check NAME` passes.
5. Use `rc = kat.blocks.use("RC")`, then
   `rc(inp=a, out=b, gnd=gnd, tag="CH_A")` in the design; regenerate, sync and stamp.

Know which drawing to edit:

| File | Purpose |
|---|---|
| `NAME.kicad_sch` | Test harness with the DUT sheet |
| `NAME_block.kicad_sch` | Reusable block drawing, ports and parameter expressions |
| `NAME_flat.kicad_sch` | Flat drawing for a block containing library blocks |
| `NAME.kicad_pcb` | Checked block layout, including its environment |
| `NAME.block.json` | Metadata, environment, port points and last check |

Port arguments are nets; power nets remain global. Parameters are numeric/string
arguments, including defaults. A part value equal to a harness parameter becomes
`${param}` in the reusable drawing: give parameters distinct default values so
the intended substitution is unambiguous.

Nested library blocks become sheets in the block drawing. The flat drawing is
built once, then keeps arrangement work; changed inner drawings are re-stamped.
Arrange that flat drawing when the composed block will be stamped flat.

Harness parts outside DUT are the environment, not deployed parts. The block
check exempts harness MPNs and expected environment connections. PCB stamps
exclude harness-only routes and explicitly named environment zones/keepouts.
Route with the clearance the design will require; mark port pads/locked vias
near the outline edge (checked within 2.5 mm of the edge).

Checks store content hashes; stamps warn when source content changed since the
last clean check. Rebuild/recheck deliberately, rather than dismissing warnings.
Change the library block, then re-sync/re-stamp consuming designs.

See [schematic stamping](../../kicad-schematic/references/stamping.md),
[hierarchy](../../kicad-schematic/references/hierarchy.md), and
[PCB stamping](../../kicad-layout/references/stamping.md) for the consuming side.
