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

### FR-029: Non-rectangular courtyards in generated footprints

**Open.** `kat lib make` (`src\kat\libmake.py`) always draws a rectangular courtyard
around all pads. A slot-shaped part next to a pin row needed a stadium-shaped courtyard
to avoid a false collision. Request: a courtyard polygon or stadium option, also used
by the collision checks of `move` and `stamp`.

## Schematic sync and blocks

## Electrical checks

### FR-006: Estimate coupling across repeated blocks in 3D

**Open.** Evaluate foreign-net coupling with neighbouring copies of a block at a
specified pitch, including vias and copper extending beyond its outline. Report
mutual capacitance per net pair. The existing parallel-plate and 2D cross-section
estimates in `src\kat\pcb\coupling.py` and `src\kat\pcb\field2d.py` do not provide
a tiled 3D field solution.

### FR-039: Verify zone fills against foreign-net copper

**Needs reproduction.** Reported on KiCad 10: after routing, pours with a minimum width of
0.2 mm covered vias, tracks and through-hole pads of other nets (many DRC errors), also
through pcbnew's `ZONE_FILLER` directly, and keep-out areas around those items were
ignored. Changing the minimum width, clearance or priority made it disappear. Request:
`kat pcb refill` checks the fill against foreign-net items and retries with a slightly
changed minimum width, reporting it; report the bug to KiCad with a reduced board.

## Routing and board-editing APIs

### FR-046: Search a legal spot for loose parts

**Partial.** `Board.place_near` puts a two-pad part next to a pad, stepping sideways until
no courtyard overlaps. Request: any part near an anchor part or point, on a chosen side,
with keep-outs, searching outward for the nearest legal spot.

### FR-048: Plane-access helpers

**Partial.** `b.stitch` places a stitching-via lattice in the library. Remaining: a
`kat pcb stitch` command and a plane-stub helper (`kat pcb plane-stub`) that joins SMD
plane pads to the nearest legal via.

### FR-049: Post-route clean-up (`kat pcb tidy`)

**Open.** Router output is electrically fine but untidy: 90° and acute corners,
doubled-back segments, staircases of short segments, small kinks, and long parallel runs
at minimum clearance where space is free. KiCad has no batch equivalent of its
interactive optimizer. Proposal: `kat pcb tidy PCB [--nets GLOB] [--layers L,..]
[--region X,Y,W,H] [--corners 45|arc|keep] [--spread GAP] [--dry-run]`, also as
`b.tidy(...)`, with optional steps:
1. normalise: merge (near-)collinear segments, drop zero-length ones, doubled-back
   overlaps and dangling stubs;
2. de-staircase: replace runs of short segments with one straight or 45° segment;
3. corners: 90° to two 45° bends or arcs; no acute corner except at a pad entry;
4. pull tight within the homotopy class (vias fixed unless `--move-vias`);
5. spread neighbouring tracks towards a target gap where space allows.

Constraints: effective clearances (FR-021), exact pads (FR-020), keep-outs and foreign
zone fills; locked tracks stay unless `--include-locked`; no net gets longer in steps 1-4,
at most by a set percentage in step 5, not at all for `--freeze-length NETS`; net rules
and DRC re-run, and a violating change is rolled back per net; deterministic, idempotent
and journalled. Report (also `--dry-run`) per layer: corner-angle histogram, short
segments, reversals, length change per net, gap histogram before and after. The same
metrics as `kat pcb check` warnings would let a review find untidy routing without images.

### FR-050: Guide routing in chunks

**Partial.** `kat pcb route --net NAME` (repeatable), `--skip-class` and
`route(pcb, nets=[...])` exist. Remaining, in the kicad-layout skill and the `route`
help: Freerouting time grows steeply with nets, layers and pad density (give a rule of
thumb); route constrained nets first, then groups by region or connector, `lock` each
accepted chunk and run `kat pcb check` after it; the timeout and pass knobs; the
router's "all routed" is not the verdict. Optionally `--nets GLOB` / `--netclass NAME`.

### FR-051: Grid-router capabilities

**Open, waiting** (decided 2026-11-06: like FR-006, until a benchmark board fails with
Freerouting). FR-018 built no grid router, as nothing then showed it would beat Freerouting.
Downstream grid routers since needed, on dense multi-layer boards:
- speed: a native A* core or a coarse global stage (pure Python took minutes per
  rip-up iteration);
- targeted rip-up and reroute when negotiated routing plateaus;
- via spacing checked in all directions, not only orthogonally (FR-023 covers the rule;
  kat's own via placement checks it in every direction, see FR-037);
- zone fills rasterised per net as obstacles or targets;
- failed nets retried with a permuted order automatically.

Single tracks and vias can already be removed with `kat pcb remove --uuid / --at`.

### FR-052: Faster `kat batch-exec` on large boards

**Needs reproduction.** Reported: each pass on a board of several MB took minutes,
spent in loading, saving and the report. Request: measure where the time goes; a lighter
report mode and incremental save.

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
On 2.4.1 two limits remain. The cut-out at (27, 7)-(33, 23) on a 60 x 30 mm board leaves a net unrouted, while the
larger (28, 5)-(32, 25) routes. The optimizer's slow case is not cured, only avoidable: on the LED matrix with a
1.5 mm connector keep-out, the optimizer reports 5 unrouted right after a routing stage with none and spends 351 s
on one pass. That board took 483 s with it and 128 s with `--no-optimize`, with byte-identical sessions, so the pass changed
nothing. The optimizer stays on by default, as it took under 0.4 s on every benchmark board that finished, and `stages`
shows when it did not.

kat now pins Freerouting 2.5.0 and passes `router.automatic_neckdown=false` on every run, on this comparison. Same benchmark, seconds and unconnected nets, DRC errors in brackets
where there are any; "no neck" is `router.automatic_neckdown=false`, which works on 2.5.0 and not on 2.4.1:

| Board | 2.4.1 | 2.5.0 | 2.5.0, no neck |
|---|---|---|---|
| 287 nets, 2 layers | 275 s, 2 | 348 s, 0 | 363 s, 0 |
| 287 nets, 4 layers | 105 s, 0 | 94 s, 0 | 98 s, 0 |
| 510 nets, 4 layers | 281 s, 0 | 172 s, 0 (2 clearance) | 226 s, 0 |
| 798 nets, 4 layers | 915 s (limit), 37 | 463 s, 0 (2 clearance) | 471 s, 0 |
| 798 nets, 6 layers | 752 s, 0 | 862 s, 0 | 573 s, 0 |

Also on 2.5.0: the board of the optimizer's slow case takes 143 s, not 483 s. The cut-out at (27, 7)-(33, 23) routes only
with necks on, so kat's default leaves that net open, as 2.4.1 did: `--router-option automatic_neckdown=true` routes it, with DRC clean.
Its optimizer does real work now, which costs time: 350 s of the 862 s above, and 178 s of 307 s on the LED matrix. Its
narrow tracks broke the board minimum on the LED matrix (24 errors) and in one more test; with no neck, the LED matrix has none.
`ignore_net_classes` works there only as plain `SLOW`, still leaves copper on some of the class's nets, and does nothing as `["SLOW"]`.
kat moved off the deprecated `-mp`. One seed only.

Both versions send analytics by default; kat turns them off on every run. A laptop that sleeps during a run
ended it on waking, on 2.5.0 with no log line about it; kat now reports `timed_out` for a stage that never finished.

### FR-040: Write `kat pcb check` results to a file

`kat pcb check PCB --out FILE` and `kat.pcb.check(pcb, out=FILE)` write the result as UTF-8
JSON and name the file in `out`, as `kat pcb escape --out` does.

### FR-045: Accept one side vocabulary

Every `side` argument takes `F`/`top`/`front` or `B`/`bottom`/`back` in any case
(`kat.pcb.layers.fp_side`, `stamp_side`): `move`, `move_many`, `add_footprint`,
`add_board_part`, `fiducials`, `stamp`, `stamp_preview`, `min_pitch`, and the CLI's
`pcb stamp --side` and `pcb cell --side`. Anything else is refused: `move` used to flip a
part for any value that wasn't its own side.

### FR-047: Set a footprint's zone connection

`Footprint.zone_connection` / `set_zone_connection(mode)`, or `b.pad_connection("U1", mode)`
with a bare reference, set KiCad's footprint-level `zone_connect`. Pads without an override
of their own follow it, and KiCad's fill does too (`tests/test_pcb_zones.py`). Stamping
copies it, as it does the pad overrides.

### FR-036: Explain sync-gate refusals

A refused `kat pcb sync` carries the gate's `errors` (`gate.errors`) and names them in
`error`, e.g. "schematic_not_ok: schematic differs from the design netlist d.net: 1 design
nets not drawn as such, 1 drawn nets not in the design (e.g. ...)". The schematic-wide
findings name the ERC types, netlist differences (`check.netlist_differences`) or drawing
findings. `kat.ready` / `kat ready` without `--expect` now compare with the design netlist
the sheet was last synced from (`expect` in the result), as the gate does, so the two give
the same errors; `explain` names them in one line. The exit-code report was not reproduced
(1 on `ok: false`, 2 on errors).

### FR-032: Grow the paper when sync places sheet boxes

A hierarchical sync grows the paper (one ISO size at a time) when a row of sheet boxes
would reach the title block, and reports it in `paper` [{sheet, from, to}], which the
hierarchical report used to drop (`syncer.grow_paper`, `syncer_hier._next_spot`). New
parts are now placed below the sheet boxes instead of over them. Test:
`test_many_boxes_grow_the_paper` (30 boxes from A4).

### FR-030: Keep power symbols idempotent across syncs

Not reproduced. Three or four syncs in a row of hierarchical designs (two blocks on a
connector, a chain of five and of thirty blocks, a power net driving a block's signal port)
and repeated block builds each kept one power symbol per net and reported nothing.
The flat sync's idempotence was already tested (`tests/test_sync.py`). Checking this
found that the hierarchical report dropped `named`, `wired`, `flags` and `detached`, so a
repeated power symbol could have gone unnoticed. They are now reported, and
`test_many_boxes_grow_the_paper` requires a repeated sync to report nothing at all. Reopen
with the design if it recurs: the likeliest remaining path is a lone power pin whose drawn
net KiCad names differently from the design net (`Syncer.reconcile`).

### FR-031: Add one PWR_FLAG per label-driven power net at the top level

Reproduced: a supply that reached the blocks only through their ports (no root part on it)
stayed undriven, because the root's own ERC runs without the sheets. After a hierarchical
sync, kat now runs ERC on the whole hierarchy and puts one PWR_FLAG per undriven power net on
the root sheet, never in a block sheet (`syncer_hier._root_flags`, reported in `flags`).
A power-library name gets a global label with the flag; another name gets a local label,
joining the labels at the sheet pins. Checking this found that a flag placed apart on a power
symbol of its own was "pin not connected" to ERC (it does not count power-symbol pins), so
the flat sync's `_flag_apart` uses the global label as well. Test:
`test_supplies_reaching_only_blocks_get_one_flag_at_the_top` (one block twice, on `+5V` and
`VBAT_SW`; ERC clean, three flags at the top, none in the block, none added again).

### FR-034: Protect hand-edited block sheets in a forced block build

Reproduced: a forced build removed a part added by hand to the block sheet and set a
hand-changed value back to the design's, only reporting it afterwards. `kat.blocks.build`
now records content hashes of the sheets it wrote. When a sheet changed since, it first runs
a dry-run sync against that build's netlist. If the sync would remove, update, attach,
detach or rename anything, the build is refused before anything is written, with the list
(e.g. "RC_block.kicad_sch: removed R2; ... updated C1 Value '220n' (design '100n')").
`overwrite=True` / `kat block build --overwrite` lets the design win; `force` still only
passes the board gate. Arrangement and notes pass and are kept. A refused board gate still
leaves the newly synced schematic in place, because the gate judges that schematic.

### FR-035: Run SKiDL designs from a sub-folder

Reproduced, and both symptoms come from the working directory: SKiDL reads no
`sym-lib-table` and searches only the working directory. So a script in `sub/` could not find
a project library when run from `sub/` (`NO_LIB`), and when run from the project root it
found it but wrote `.erc`, `.log`, `_sklib.py` and the netlist there. `kat.skidl_blocks.
project_libraries()` adds the project's `sym-lib-table` libraries (with `${KIPRJMOD}`) to
SKiDL's search path; the project is found above the running script. The setup reference
(`skills/kicad-design/references/setup.md`) says to run scripts from their own folder.
Test: `test_a_script_in_a_sub_folder_finds_the_project_libraries`.

### FR-033: Per-project reference prefixes

`kat prefix DIR J=CON` / `kat.prefixes(path, {"J": "CON"})` store the project's reference
prefixes in `<project>.kat.json`. `kat lib make` uses the project's prefix for `J` unless
`--reference` is given, and symbols kat adds without a reference (`Schematic.add_symbol`,
`Sheet.add`) use the map. Sync never renames, by decision: a SKiDL design's references come
from its netlist, and renaming them would break every netlist comparison. Instead
`kat check` lists references off the map in `prefix_warnings` (changing neither `ok` nor
`clean`), and the schematic skill shows SKiDL's `ref_prefix=`.

### FR-041: Keep track-type port stubs when stamping

`_harness_copper` holds every port point like a pad (on its layer for a track port), so a
track port's stub to the outline is kept, and only copper leading to harness pads is pruned.
The stamp result counts that copper in `harness_copper` (no longer in
`skipped_environment`), and warns about each port point that no block copper reaches.
Test: `test_port_points_are_checked_and_stamped` (an edge track port with a harness route
beyond it; the stub survives at the transformed point).

### FR-044: Offset zone priorities when stamping

`b.stamp(..., zone_priority=N)` / `kat.pcb.stamp(..., zone_priority=N)` /
`kat pcb stamp --zone-priority N` add N to the copied pours' priorities (not below 0).
The offset is recorded per instance and used again on a restamp. A stamp warns when one of
its pours overlaps a board pour (not a stamped instance's) of another net on the same layer
at the same priority. The stamped-instance check (`cells._clash`) sees the offset
priorities. KiCad 10.0.6's DRC reported no `zones_intersect` for such overlaps, crossing
or contained, refilled or not, on a plain two-zone board either: the fill order decides,
which the warning says. Test: `test_zone_priority_lifts_block_pours_over_a_board_pour`.

### FR-043: Keep stamp records with the board

The stamp records are kept in the board file, one board property `kat.stamp.<instance>`
each, holding the record as JSON. KiCad 10's save keeps them. `Board.stamps()` and
`kat pcb stamps PCB` list them. An older `<board>.stamps.json` is read and moved into the
board on the next save. `kat pcb stamps PCB --rebuild [INSTANCE...]` / `Board.rebuild_stamps`
/ `kat.pcb.stamps(pcb, rebuild=True)` recover lost records: block and side from the group
name, rotation and position fitted to the footprints with the stamp's own placement, then
ports, outline and layer map as `stamp_preview` gives them, and the zone priority offset
from the pours. Pairing a hierarchical design's footprints still reads the schematic next
to the board. Test: `test_stamp_records_live_in_the_board_and_can_be_rebuilt`.

### FR-042: Stamp mirrored twins and site lists

`b.stamp(block, "/CH_A", at, twin="/CH_B")` (or `{instance: twin}` with several, CLI
`--twin /CH_A=/CH_B`, `kat.pcb.stamp(..., twin={...})`) stamps the twin mirrored on the
other side at the same site, in the same batch. Sites come from a floorplan list via
`kat pcb stamp PCB BLOCK --sites FILE` / `kat.pcb.stamp_sites`: a CSV with columns
`instance, x, y[, rot, side, twin]` or a JSON list, read by `kat.pcb.stamping.read_sites`.
A floorplan spec (`kat pcb access`) can name the same list file as its `cells`, with a
`name` column for candidates that are not design instances yet. Tests:
`test_twins_and_site_lists` and `test_a_library_block_as_a_candidate_cell_of_a_floorplan`.

### FR-037: Fabrication profile: via types and minimum clearance

`b.fab_profile(via_types=["through"], min_clearance=0.125)` / `kat.pcb.profile` /
`kat pcb profile PCB --via-types through --min-clearance 0.125` store the profile in
`<board>.kat.json`. KiCad gets what it can enforce: a generated rule `kat profile vias`
(`(constraint disallow blind_via buried_via micro_via)`, flagged by DRC as
`items_not_allowed`) and the board minimum clearance. kat holds its own steps to it:
- `b.via` and `route_vias` refuse forbidden kinds;
- `b.netclass` refuses a clearance below the minimum unless `below_profile=True`, which is
  recorded in the profile's `exceptions`;
- `stamp` warns about a block's forbidden vias and classes below the minimum;
- `kat pcb check` lists departures in `profile` (not clean), and `kat pcb ready` makes them
  errors (`fab_profile`);
- `kat pcb fab` refuses forbidden vias.
The layout skill's example now uses the profile and 0.125 mm. Not done: making `kat new`
and block import apply the profile, since classes start at KiCad's 0.2 mm default, above the
suggested minimum. FR-051's via-spacing point is covered: hole-to-hole spacing is checked in
every direction (diagonal case in the test). Test:
`test_a_fabrication_profile_holds_the_whole_flow`.

### FR-038: Name pads on isolated pour fragments

`kat pcb check` lists `isolated_pads`: [{pad, net, zone, layer, at, fragment_area,
zone_area}], the pads on a fill fragment other than the zone's largest. KiCad 10's DRC
reports such a pad only as "Missing connection between items" between two parts of the zone,
placed at the zone's corner. The fill is computed on a scratch copy, only for the zones DRC
names in its unconnected items (`checks.isolated_pads`). Test:
`test_a_pad_on_a_cut_off_fragment_is_named`.
