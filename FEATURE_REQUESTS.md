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

## Routing and board-editing APIs

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

### FR-013: Check floorplan port accessibility

`kat pcb access SPEC.json` and `kat.pcb.port_access`: a maze search per layer from a source region,
with candidate cells, library blocks placed as `stamp` would, keep-outs, and a board's edge, cut-outs and
pads. Reports `reachable`, `blocked` with what walls the port in, or `sealed`, and cells that overlap or
leave the board. On real boards Freerouting agrees with it (`tests/test_pcb_floorplan.py`). Each port
is judged alone, so the capacity of a gap is not checked.

### FR-019: Joint escape routing and pin assignment

`kat pcb escape PCB SPEC.json [--apply]` and `kat.pcb.escape`: a min-cost flow on a grid assigns permutable
sources to targets and routes them together, as vertex-disjoint paths on the allowed layers, with
vias only when asked for and then between exactly two layers. Returns assignment, tracks, and what did
not fit. The assignment goes through `kat back-annotate`, and `--apply` then adds the tracks, judged by
DRC (`tests/test_pcb_escape.py`, including a real SKiDL design). As agreed, this first version handles
permutable groups only: fixed pairs are listed in `not_handled`. Tracks are at right angles.

### FR-018: Improve autorouter scaling and rule handling

`kat pcb route --skip-class NAME --no-optimize --router-option KEY=VALUE` and the same arguments of
`kat.pcb.route`. The result now has `stages`, `unrouted` per pass, `timed_out`, `skipped` and
`settings {refused_by_router, unverified}`. `tests/e2e/router_benchmark.py` is the generic benchmark.

Reproduced on Freerouting 2.4.1: `router.ignore_net_classes` is accepted in every form and has no effect
(all 24 nets of the class were still routed), so kat refuses it and leaves the nets out of the DSN itself.
An unknown setting exits 0 with only a log line, which kat now reports. `--timeout` is now the router's own
`job_timeout`, so a partial result is kept, not lost. On a board that timed out, DRC also lists dangling tracks and
vias. A 4 s cut-off also showed that a neck-down of 0.075 mm could stay under a board minimum of 0.1 mm, because
only the class width was tried: such a track now goes up to the minimum unless it would touch other copper.

Benchmark, SOIC-16 grid, seed 1, up to 25 passes, 900 s limit, judged by DRC:

| Parts | Nets | Layers | Seconds | Unconnected | Unrouted per pass |
|---|---|---|---|---|---|
| 6x6 | 287 | 2 | 275 | 2 | stalls at 2 from pass 18 on |
| 6x6 | 287 | 4 | 105 | 0 | 93, 30, 7, 0, 0 |
| 8x8 | 510 | 4 | 281 | 0 | 199, 69, 22, 3, 1, 0, 0 |
| 10x10 | 798 | 4 | 915 (limit) | 37 | 309, 112, 37 |
| 10x10 | 798 | 6 | 752 | 0 | 111, 37, 12, 2, 0, 0 |

DRC found no errors on any of them, so what limits dense boards is time and layers, not bad copper. Almost all
the time goes to the routing stage, in its first passes. Two more layers help far more than more passes.

No grid router was built: nothing here shows it would do better, and `kat pcb escape` covers planar escape.
Two limits remain. The cut-out at (27, 7)-(33, 23) on a 60 x 30 mm board still leaves a net unrouted, while the
larger (28, 5)-(32, 25) routes. The optimizer's slow case is not cured, only avoidable: on the LED matrix with a
1.5 mm connector keep-out, the optimizer reports 5 unrouted right after a routing stage with none and spends 351 s
on one pass. That board took 483 s with it and 128 s with `--no-optimize`, with byte-identical sessions, so the pass changed
nothing. The optimizer stays on by default, as it took under 0.4 s on every benchmark board that finished, and `stages`
shows when it did not.

Freerouting 2.5.0 compared, kat still pinned to 2.4.1. Same benchmark, seconds and unconnected nets, DRC errors in brackets
where there are any; "no neck" is `router.automatic_neckdown=false`, which works on 2.5.0 and not on 2.4.1:

| Board | 2.4.1 | 2.5.0 | 2.5.0, no neck |
|---|---|---|---|
| 287 nets, 2 layers | 275 s, 2 | 348 s, 0 | not run |
| 287 nets, 4 layers | 105 s, 0 | 94 s, 0 | 98 s, 0 |
| 510 nets, 4 layers | 281 s, 0 | 172 s, 0 (2 clearance) | 226 s, 0 |
| 798 nets, 4 layers | 915 s (limit), 37 | 463 s, 0 (2 clearance) | 471 s, 0 |
| 798 nets, 6 layers | 752 s, 0 | 862 s, 0 | not run |

Also on 2.5.0: the cut-out at (27, 7)-(33, 23) routes, and the board of the optimizer's slow case takes 143 s, not 483 s.
Its optimizer does real work now, which costs time: 350 s of the 862 s above, and 178 s of 307 s on the LED matrix. Its
narrow tracks broke the board minimum on the LED matrix (24 errors) and in one more test; with no neck, the LED matrix has none.
`ignore_net_classes` works there only as plain `SLOW`, still leaves copper on some of the class's nets, and does nothing as `["SLOW"]`.
`-mp` still works but is deprecated. One seed only, and the full suite has not been run with no neck.

Both versions send analytics by default; kat turns them off on every run. A laptop that sleeps during a run
ended it at the 900 s limit on waking, on 2.5.0 with no log line, so kat did not report `timed_out`; DRC still showed the open nets.
