# Feature requests

Outstanding requests collected from downstream use and checked against the current
implementation. Examples are generic: no downstream project identifiers, design
files, measurements, pinouts, or proprietary code are included.

Proposed command names and options below are suggestions, not existing APIs.
**Open** means the requested capability is missing; **Partial** means only the
remaining gap is listed. **Needs reproduction** identifies a reported problem
that has not been confirmed against the current implementation.

Keep IDs stable when adding requests. Before implementing one, recheck the linked
code and SPEC.md; when it is resolved, record the replacement API or documentation
and move it out of the outstanding list. Do not copy private examples into this
file or its eventual tests.

## Import and library generation

## Electrical checks

### FR-006: Estimate coupling across repeated blocks in 3D

**Open.** Evaluate foreign-net coupling with neighbouring copies of a block at a
specified pitch, including vias and copper extending beyond its outline. Report
mutual capacitance per net pair. The existing parallel-plate and 2D cross-section
estimates in `src\kat\pcb\coupling.py` and `src\kat\pcb\field2d.py` do not provide
a tiled 3D field solution.

## Block building and placement

### FR-013: Check floorplan port accessibility

**Open.** Given candidate cells, port positions, allowed layers, and a source
region, report which ports can reach the source without crossing occupied cells
or keepouts. A coarse maze/grid check should detect inaccessible placements
before detailed routing; fitting by area alone is insufficient.

## Routing and board-editing APIs

### FR-018: Improve autorouter scaling and rule handling

**Needs reproduction.** Investigate poor convergence/runtime on dense multilayer
boards and reported ineffective net-class-ignore behaviour. Establish a generic
benchmark, identify the relevant upstream option, and report unsupported or
ignored settings rather than implying they took effect. Consider a grid-routing
alternative if needed. The Java minimum is settled: Freerouting 2.4.1 is compiled for Java 25, and
`src\kat\pcb\router.py` now asks for that. One known case: on a 60 x 30 mm board, a cut-out at
(27, 7)-(33, 23) leaves a net unrouted, while the larger (28, 5)-(32, 25) routes.

### FR-019: Joint escape routing and pin assignment

**Open.** Provide a generic flow-based river/escape router that jointly assigns
permutable source/destination pins and routes tracks to block ports. Accept
allowed layers, preserve planar/no-via constraints when requested, and return
both the assignment and tracks. Suggested entry points are `kat pcb escape` or
`kat pcb flow-route`; the current router wraps Freerouting with fixed net
assignments.

## Resolved

### FR-027: Document literal SKiDL pin-name selection

Documented in `skills/kicad-schematic/references/troubleshooting.md` and SPEC.md
section 7. On SKiDL 2.3 names match exactly by default, so the requested escaping
is only right with `match_pin_regex=True`. `tests/test_skidl_blocks.py` checks each
statement against the installed SKiDL.

### FR-028: Verify explicit inner-layer rendering isolation

Not reproduced. On a generic 6-layer board with pours, each explicit single-layer view
held only that layer's colour, in any order and from the SVG cache; the regression test is
in `tests/test_pcb_views.py`. Reopen with a failing board if it recurs. Vias and
through-hole pads do appear on inner layers, because they have copper there.
Checking this found a separate bug, now fixed: a layer the board lacks (`In9.Cu`) raised a
raw `FileNotFoundError`, because kicad-cli exits 0 and writes nothing. It is now a `KatError`.

### FR-009: Check schematic locks before block-build writes

`kat.blocks.build` checks the locks of the root and block sheets, the flat sheet and the
board before running the harness, so nothing generated changes. No deferred-root mode was
needed. The board is included because a lock there also left the block half-built.

### FR-008: Match plane-rule exemptions by keepout-name pattern

`kat.pcb.plane_rule(except_globs=[...])` and `kat pcb netrule plane --except-zone-glob GLOB`.
Globs are evaluated at check time, so areas added or stamped later count, and a glob
matching nothing yet gives a warning. `--except-zone` stays exact.

### FR-012: Expose footprint locking

`Footprint.lock()`, `unlock()` and `locked`, also in `info()`. Beyond the request, the
checked moves (`move`, `move_many`, `flip`, `align`, `distribute`, `place_near`,
`move_block`, `replicate`) now refuse a locked part that would change, unless `force=True`,
and `stamp` refuses to move one, because they used to ignore the flag.

### FR-011: Check individual through holes on the opposite side

`_taken` in `src/kat/pcb/stamping.py` returns one box per through-hole pad or hole, slots
included (`Pad.hole_box`), so empty space between distant guide holes stays free. No margin
was added: KiCad's DRC reports a hole overlapping a courtyard, and the pad-and-hole box is
already at least as large. Footprints with only holes and no courtyard are treated hole by
hole on their own side too. `kat pcb cell` and `min_pitch` use the same boxes, so their
results can shrink for blocks with distant holes.

### FR-010: Clarify rotated stamp placement and return bounds

`stamp` returns `origin` and the turned bounds as `outline`. The pivot convention is spelled
out in `kat pcb stamp --help` and SPEC.md section 4.18. `--anchor top-left` / `anchor=` is
the explicit alternative; the default stays `origin` and existing placements are unchanged.

### FR-014: Preview transformed ports before stamping

`Board.stamp_preview`, `kat.pcb.stamp_preview` and `kat pcb stamp ... --preview` report
origin, outline, ports in the design frame and `order` (ports per outline edge) without
changing the board, and work without an instance, using the block's own net names.
Collisions are not part of the preview.

### FR-016: Make batch restamping independent of input order

`Board.stamp` with a dict and `kat.pcb.stamp` take all named instances off first, stamp them
in natural order of their names (returned as `order`) and judge only their final geometry.
A refusal lists every collision and its pairs, and restores the board. Single stamps go the
same way, so they no longer leave a half-changed board in memory when refused.

### FR-015: Support non-orthogonal stamp rotation

`stamp` takes multiples of 45 degrees, on both sides. Collision checks use turned courtyard
polygons (`Footprint.courtyard_outline`) and copper. Board rectangles and text boxes become
polygons, as `gr_rect` is always axis-aligned, and coordinates keep 1 nm. Pad positions are
checked against IPC-D-356, and DRC agrees on interlocking. `Board.collisions` and `move`
still compare bounding boxes, so they stay stricter than DRC around such a part.

### FR-020: Expose exact pad geometry

`Pad.geometry()`, `Pad.polygons(max_error)`, `kat.pcb.pad_geometry` and `kat pcb pads`
describe corner radius, chamfers, trapezoid delta, drill offset, custom primitives and
per-layer shapes. The polygons match the distances KiCad's DRC reports for 11 shapes, turned
and flipped (`tests/test_pcb_pads.py`). This also fixed `Pad.outline`, which understated
circles, ovals, custom pads, trapezoids and offset pads, and footprint flips, which left
trapezoid tapers and chamfer corners unmirrored.

### FR-021: Query effective item-to-item clearance

`Board.clearance`, `kat.pcb.clearance` and `kat pcb clearance PCB A B` resolve the classes,
board minimum and custom rules as DRC does, and are checked against it on both sides of the
limit for 17 sets of rules (`tests/test_pcb_rules.py`). Conditions that depend on more than
the nets, such as `A.Type`, are listed as `unresolved` and counted by `worst_case`, which
local checks keep. Also corrected the docs: a matching rule can lower the board minimum.

### FR-024: Configure zone-to-pad connection modes

`b.zone(pads="thermal"|"solid"|"none"|"thru_hole_only")`, per pad `b.pad_connection("U1.8",
mode)` and `Pad.zone_connection`. Stamps carry a block's per-pad overrides. A changed mode now
counts as a changed zone. Judged by KiCad's filled copper and DRC (`tests/test_pcb_zones.py`).

### FR-017: Build and route blind/buried vias

`b.via(at, net, layers=(a, b), kind=None)` infers or validates the kind (`via_kind`), and
`vias()` reports `kind` and `span`. Clearance checks, the DSN and the session read-back use each
via's own span. `b.route_vias(spans, through=False)` sets what the router may lay. Two
Freerouting 2.4.1 limits are worked around and documented in SPEC.md section 4.10: it lays no
vias unless one spans every layer, so with `through=False` the session is checked and a net that
used a through via stays unconnected and reported; and it does not stack blind and buried vias.

### FR-007: Make layer rules configurable for via pass-through

`layer_rule(via_span="strict"|"ends"|"ignore", pads=False)` and `kat pcb netrule layers
--via-span MODE --pads`. `strict` stays the default. `ends` counts only the two layers a via
joins, `ignore` skips vias, and `pads` adds pads, which were never checked before. `stamp`
follows the same settings.

### FR-023: Enforce drill-to-drill spacing during routing

`Board.hole_conflicts` answers for a proposed via, same net included, as KiCad's DRC counts it:
not between two µvias, nor between spans that do not meet. `via`, `fanout`, `via_to_plane` and
`stitch` keep the minimum, and `clearance` reports it. The DSN carries it as via clearance
between nets. The router does not keep vias of one net apart, so `route` lists any that still
break it in `hole_to_hole`; DRC stays the final judge.

### FR-025: Derive layer-scoped keepouts from copper

`b.keepout_around(nets, uuids, refs, margin, scope, forbid, name)` builds an area per selected
item, on the `same` layer, `adjacent` layers, the selection's `span` or a list of layers.
It forbids pours by default, because KiCad's DRC reports copper inside its own track keep-out
as `items_not_allowed`; forbidding more is allowed and gives a warning. Judged by KiCad's fill
and DRC (`tests/test_pcb_zones.py`).

### FR-004: Check a netlist against an external mapping table

`kat.mapping.check` and `kat map SOURCE TABLE`, also through `kat check --map` and `kat pcb check
--map`, which then fail `ok`. Tables are CSV or JSON: `pin,net`, `ref,pin,net`, or pin columns
with `channel`, as in SPEC.md section 6.2. Reports `missing`, `mismatched`, `split`, `merged` and
`extra`. Schematics are read through KiCad's netlist, boards by their pads.

### FR-005: Extract legacy pinouts

`kat.pcb.odb_pinout` and `kat pcb odb-pinout JOB [--out pins.csv]` read component layers and EDA
data from a directory or archive, which is never unpacked, with files plain, UNIX compressed or
gzipped. `--out` writes the `ref,pin,net` table that `kat map` reads; a job can also be the source
of `kat map`. Checked against KiCad's own ODB++ export of boards with parts on both sides, whose
pads give the expected nets (`tests/test_pcb_odb.py`). Jobs from other CAD tools were not available:
their format is covered by hand-written files following the ODB++ specification.

### FR-001: Import board mechanics from fabrication data

`kat.pcb.import_mechanics` and `kat pcb import-mech PCB --odb JOB` or `--gerber FILE --drill FILE...`.
Reads ODB++ profiles and drill layers, outline Gerbers and Excellon (hits, G85 and M15 slots), and
makes Edge.Cuts and board-only hole footprints, plated or not. Board exports judge it: a board imported
from KiCad's own files, in every zero and unit format it writes, exports to the same holes and outline,
with a clean DRC (`tests/test_pcb_mechanics.py`). Other CAD tools' files were not available, so their
forms are covered by hand-written files. Routed arcs are left out with a warning. Beyond the request,
`Board.outline_loops` now gives the real board edge and cut-outs, so the router stays out of cut-outs
and `kat pcb check` sees parts past a rounded corner; it used the bounding box before.

### FR-002: Generate symbols and footprints from a pin table

`kat lib make --pins pins.csv --name NAME` and `kat.libmake.make`. Tables are CSV or JSON with
numbers, names, positions or grid places, drills, pad sizes, shapes and electrical types. `--view pin`
takes pin-side coordinates and `--marker` sets the marked pin. Checked in the full flow: KiCad's ERC
and netlist for the symbol, and DRC's library check and IPC-D-356 for the footprint
(`tests/test_libmake.py`). It draws an outline around the pads, not the part's body.

### FR-003: Validate a multi-board stack

`kat pcb mate A B --pair J1=P1 --at X,Y [--rot R] [--flip]` and `kat.pcb.mate`. Compares pads and
guide holes of mating connectors by position, the sides they are on, net names pin to pin, and
mounting holes, in board A's frame. Positions come from pads already checked against IPC-D-356, and
the tests compare the transform with KiCad's own export. Two boards at a time; stack height, body
collisions and connector gender are not checked.

### FR-026: Preserve arrangement when sheet instances are renamed

`kat sync SCH NETLIST --rename-sheet OLD=NEW` and `kat.sync(rename_sheets=...)` keep the box's uuid,
place, size, pins and wiring. A sync without it names look-alikes in `possible_renames`, which is
only a hint. `kat pcb sync` then sees the same sheet uuid under a new name and carries over stamped
groups, zone names and stamp records (`Board.rename_instances`). A sheet inside a block's own drawing
is renamed there, not by the design that uses it.

### FR-022: Back-annotate connector pin assignments

`kat back-annotate TABLE --assign J1.1=CH2 ... --run design.py --sch SCH --pcb PCB` and
`kat.mapping.back_annotate`. The SKiDL script is never edited: it reads the table with
`kat.skidl_blocks.connect_pins`, and the table keeps its comments and other columns. Netlist, sheet and
board are verified against the table and each other, with parity on the board, and all are put back if
a step fails. Tested with a real SKiDL script (`tests/test_back_annotate.py`). Sheets inside a block's
own drawing are not restored, since a design's sync only writes instance data there.
