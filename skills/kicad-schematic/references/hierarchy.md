# Hierarchical sheets

Read when selecting flat versus hierarchical drawings or editing a sheet tree.
Small repeated circuits often read well flat; large/deep blocks can be sheets.

`kat sync SCH design.net --sheets ch,rc` selects SKiDL function types recorded
in design.blocks.json, not instance tags or library folder names. Each selected
type must have a library block. Selection and design-netlist path are remembered
in the sheet's `.kat.json`; later syncs retain the hierarchy.

Boxes point to the library's reusable drawing. Edit it in the block library,
build/check the block, then sync the design; do not repair a shared child file
as if it were a private design instance. Draw ports at left/right edges:
box pin sides/order follow the port drawing.

Sheetfile paths are relative to the file containing the box, also when nested.
Sheet fields supply `${parameter}` text variables to children and deeper sheets;
the block project's text_variables provide defaults when opened independently.
Use caller net names different from port names beyond case: SPI_SCLK outside
and sclk inside avoids the similar-label warning caused by SCLK versus sclk.

For manual trees, use `s.sheet(...)`, `s.port(...)` and points like `"CH_A.IN"`,
then `kat.hierarchy.annotate(root)` after tree changes. Manual annotation numbers
references by page (default page * 100 + item); annotate a standalone block with
per_page=0 when appropriate. Sync-generated hierarchies instead assign instance
references from SKiDL; do not replace them with arbitrary page numbering.

`s.move("CH_A", to=(x, y))` uses the box's top-left corner and carries private
pin wiring; use move_many for swaps. `s.sheet_box(...)` changes size/pin sides.
These operations preserve UUIDs and sync preserves arrangement. Recreating boxes
breaks PCB symbol paths. Boxes do not rotate. Disambiguate matching part/box
names with `sheet:NAME` or `part:NAME`.

Snapshots normally render each child file once; `--pages instances` renders each
instance. Read the root and every child page. Use `kat check --full` for complete
netlist differences. SKiDL's per-instance numbered inner net names are accepted
as equivalent to the corresponding KiCad sheet-path names.
