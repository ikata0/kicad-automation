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

### FR-001: Import board mechanics from fabrication data

**Open.** Import outlines, holes, and slots from ODB++ profiles, outline Gerbers,
and Excellon (including G85/M15 slots). Create `Edge.Cuts` geometry and appropriate
plated/non-plated holes without requiring custom extraction scripts.
Current fabrication support in `src\kat\pcb\fabrication.py` exports files only.

### FR-002: Generate symbols and footprints from a pin table

**Open.** Generate a symbol and/or grid/connector footprint from CSV or JSON
containing pin numbers/names, positions, drill sizes, and pad dimensions. Support
component-side versus pin-side coordinates and an orientation marker. A proposed
entry point is `kat lib make --pins pins.csv`.
`src\kat\library.py` and `src\kat\pcb\footprints.py` currently load library parts,
not generate them from pin tables.

### FR-003: Validate a multi-board stack

**Open.** Given two boards, their relative transform, and mating connector pairs,
check pin-to-pin net consistency, guide-peg/hole alignment, and mounting-hole
alignment. This is a geometric/electrical assembly check, distinct from inspecting
one board's layer stack-up.

### FR-004: Check a netlist against an external mapping table

**Open.** Let schematic/board checks assert a CSV mapping such as connector
pin-to-net or source-pin-to-logical-channel-to-destination-pin. Report missing,
extra, and mismatched assignments. Existing netlist parity checks are not a
substitute for an independently specified mapping.

### FR-005: Extract legacy pinouts

**Open.** Extract component pin-to-net mappings from ODB++ component and EDA data,
with output suitable for seeding SKiDL definitions or a mapping-table check.

## Electrical checks

### FR-006: Estimate coupling across repeated blocks in 3D

**Open.** Evaluate foreign-net coupling with neighbouring copies of a block at a
specified pitch, including vias and copper extending beyond its outline. Report
mutual capacitance per net pair. The existing parallel-plate and 2D cross-section
estimates in `src\kat\pcb\coupling.py` and `src\kat\pcb\field2d.py` do not provide
a tiled 3D field solution.

### FR-007: Make layer rules configurable for via pass-through

**Open.** Allow a layer rule to distinguish copper used for routing from layers
that a blind/buried via merely passes through. Suggested modes are
`via_span="allow"` or track/pad-only checking. Keep the current strict behaviour
available. `layer_findings` in `src\kat\pcb\coupling.py` currently checks the full
via span.

### FR-008: Match plane-rule exemptions by keepout-name pattern

**Partial.** Plane rules already support exact named-zone/rule-area exemptions
and explicit rectangles. Add pattern-based selection of named keepouts so a
family of intentional cut-outs can be exempted without enumerating every name.
`plane_rule` and `_plane` in `src\kat\pcb\coupling.py` currently use exact names.

## Block building and placement

### FR-009: Check schematic locks before block-build writes

**Open.** Preflight the root schematic's lock before running the build script or
rewriting generated metadata, netlists, or child sheets. Alternatively, provide
an explicit deferred-root mode with clearly defined outputs. A locked root must
not leave the block half-built. `build` in `src\kat\blocks.py` currently starts
generation before schematic sync performs its lock check.

### FR-010: Clarify rotated stamp placement and return bounds

**Partial.** The library/SPEC document that `at` places the block origin before
rotation, but callers still need the resulting rotated outline bounds. Return
those bounds and make the pivot convention consistent and explicit in CLI help.
Alternatively, introduce an explicitly selected rotated-top-left convention
without silently changing existing placements.
See `StampMixin.stamp` in `src\kat\pcb\stamping.py` and SPEC.md section 4.18.

### FR-011: Check individual through holes on the opposite side

**Partial.** Stamping already distinguishes same-side courtyards from opposite-side
through-hole geometry. However, `_taken` in `src\kat\pcb\stamping.py` combines
all through-hole pad/hole bounds into one box. Check each hole/pad with appropriate
clearance instead, so empty space between distant guide holes is not treated as
an obstacle.

### FR-012: Expose footprint locking

**Partial.** Add a public footprint lock/unlock API, for example
`b.footprint(ref).lock()`, and expose its state for inspection.
`_room_for` in `src\kat\pcb\stamping.py` already refuses to move footprints whose
KiCad lock flag is set; `Footprint` in `src\kat\pcb\board.py` lacks a public setter.

### FR-013: Check floorplan port accessibility

**Open.** Given candidate cells, port positions, allowed layers, and a source
region, report which ports can reach the source without crossing occupied cells
or keepouts. A coarse maze/grid check should detect inaccessible placements
before detailed routing; fitting by area alone is insufficient.

### FR-014: Preview transformed ports before stamping

**Partial.** Expose port positions and ordering in the design frame for a proposed
side, rotation, and placement without modifying the board. This supports
mirror-aware pin assignment. Stamping already returns transformed ports and
`b.ports()` reads stamped instances; `kat pcb cell` currently reports cell
geometry, not a pre-placement port preview.

### FR-015: Support non-orthogonal stamp rotation

**Open.** Allow 45-degree placements, with corresponding geometry, layer-aware
collision checks, and transformed ports. `StampMixin.stamp` in
`src\kat\pcb\stamping.py` currently rejects rotations not divisible by 90 degrees.

### FR-016: Make batch restamping independent of input order

**Open.** Define deterministic batch placement/replacement semantics and report
the order and collisions. Replacing several existing instances should evaluate
their intended final geometry rather than fail against a neighbour's stale
placement. The dictionary form of `stamp` currently processes instances
sequentially in caller order.

## Routing and board-editing APIs

### FR-017: Build and route blind/buried vias

**Partial.** Add a layer-pair parameter to `b.via()`, infer or validate the via
type, and make routing respect legal spans. Stamping already maps existing
blind/buried vias, but `via` in `src\kat\pcb\structure.py` creates through vias
only, and the router's DSN padstacks in `src\kat\pcb\specctra.py` span all copper
layers.

### FR-018: Improve autorouter scaling and rule handling

**Needs reproduction.** Investigate poor convergence/runtime on dense multilayer
boards and reported ineffective net-class-ignore behaviour. Establish a generic
benchmark, identify the relevant upstream option, and report unsupported or
ignored settings rather than implying they took effect. Consider a grid-routing
alternative if needed. Do not assume the downstream report's Java requirement:
`src\kat\pcb\router.py` currently declares Java 21 or newer.

### FR-019: Joint escape routing and pin assignment

**Open.** Provide a generic flow-based river/escape router that jointly assigns
permutable source/destination pins and routes tracks to block ports. Accept
allowed layers, preserve planar/no-via constraints when requested, and return
both the assignment and tracks. Suggested entry points are `kat pcb escape` or
`kat pcb flow-route`; the current router wraps Freerouting with fixed net
assignments.

### FR-020: Expose exact pad geometry

**Partial.** Provide a supported analytic description of pad shapes, including
roundrect corner parameters, for external clearance/routing code. `Pad` already
exposes shape, dimensions, and angle, but its `outline` approximates curved pads
with polygons and does not expose a complete analytic shape API.
See `src\kat\pcb\board.py` and `src\kat\pcb\geometry.py`.

### FR-021: Query effective item-to-item clearance

**Partial.** Expose a public query for effective clearance between two items/nets
that accounts for applicable net classes, class-pair rules, board minimums, and
supported custom rules. `b.net_rules(net)` exposes one net's class and
`b.class_clearance(...)` writes class-pair rules, but the internal
`_clearance_fn` in `src\kat\pcb\structure.py` only takes the larger class
clearance. External routers should not have to duplicate rule resolution.

### FR-022: Back-annotate connector pin assignments

**Open.** Apply a computed connector pin-to-net assignment back to the circuit
source and regenerate/sync the netlist and schematic. Preserve the rule that
connectivity is owned by the design, not edited only on the PCB, and verify
netlist parity after the change.

### FR-023: Enforce drill-to-drill spacing during routing

**Partial.** Apply hole-to-hole minimums when generating/selecting vias, including
same-net vias, and expose that constraint to routing clients.
`b.constraints(hole_to_hole=...)` already writes the DRC minimum, but local
copper checks skip same-net copper and the DSN exporter does not translate
`min_hole_to_hole` into a routing constraint. KiCad DRC remains the final judge.

### FR-024: Configure zone-to-pad connection modes

**Open.** Add solid/thermal/none selection to `b.zone(...)`, plus per-pad
overrides. `zone` in `src\kat\pcb\setup.py` currently exposes thermal dimensions
but not a pad-connection mode, requiring direct node edits for exceptions.

### FR-025: Derive layer-scoped keepouts from copper

**Partial.** Generate keepouts around selected copper with a scope such as
same-layer or a specified sub-laminate. `b.keepout(layers=...)` already supports
explicit layer lists, and the DSN exporter honours them; the missing capability
is deriving geometry and layer scope from selected copper without hand-building
each area.

## Schematic sync and documentation

### FR-026: Preserve arrangement when sheet instances are renamed

**Open.** Accept an explicit old-to-new instance-name mapping, or another safe
identity mechanism, during hierarchical sync. Preserve sheet UUIDs, placement,
and wiring rather than removing/recreating renamed sheets. `_boxes` in
`src\kat\syncer_hier.py` currently matches by sheet name.

## Rendering reports requiring reproduction

### FR-028: Verify explicit inner-layer rendering isolation

**Needs reproduction.** A downstream report described an explicit inner-copper
view containing other layers, with a subsequent render working. Current
`src\kat\pcb\views.py` forwards explicit layer names and keys its SVG cache by
layer list, so this is not yet a confirmed missing feature or identified bug.
Reproduce with a minimal generic multilayer board, checking exported SVG contents
as well as cache reuse, before deciding on a fix.

## Resolved

### FR-027: Document literal SKiDL pin-name selection

Documented in `skills/kicad-schematic/references/troubleshooting.md` and SPEC.md
section 7. On SKiDL 2.3 names match exactly by default, so the requested escaping
is only right with `match_pin_regex=True`. `tests/test_skidl_blocks.py` checks each
statement against the installed SKiDL.
