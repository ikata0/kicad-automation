# Route polisher input format

**Version 1 - proposed contract, 2026-11-12. Version 2 (anchored paths): section 11.**

This standalone document defines a small, board-independent input for
developing and visualising a topology-preserving route polisher. It specifies
data, not a particular solver or GUI toolkit. No polisher implementation or
command other than the proposed `--gui` option is implied.

## 1 Scope and conventions

An input is one UTF-8 JSON object describing a single-layer local problem:
independent movable traces, fixed surrounding copper, holes, keep-outs and a
board boundary. It contains geometry and opaque identifiers, not components,
footprints, pin numbers, schematic information or native board object IDs.

- Coordinates, widths, radii, distances and tolerances are in **millimetres**.
- Points are `[x, y]`, Cartesian, x right and y up. No implicit grid snapping.
- Numbers must be finite. Widths and radii are positive; clearances are
  non-negative. No expressions, external file references or executable data.
- IDs are unique across all traces, fixed-copper objects, holes and keep-outs.
  Net tokens identify electrical equivalence only; they carry no semantics.
- Polylines are ordered centreline vertices with round caps and round joins.
  Their copper is the centreline buffered by half the width.
- Polygon rings have at least three distinct points, no repeated closing point,
  no self-intersections and nonzero area. Winding direction is irrelevant.
  Polyline segments must have nonzero length.
- Geometry is exact as supplied. Version 1 has no native arcs; a synthetic
  polygonal approximation is a polygon, not an implicitly reconstructed arc.

The reader must reject unsupported versions, unsupported fields, malformed
geometry and missing references instead of silently ignoring them. A required
array may be empty only where stated below. Duplicate JSON object keys are
invalid. Numerical tolerance is for computation, never permission to reduce a
manufacturing clearance.

## 2 Top-level fields

All fields in this table are required.

| Field | Type | Meaning |
|---|---|---|
| `format` | string | Exactly `route-polish-input`. |
| `version` | integer | Exactly `1`. |
| `units` | string | Exactly `mm`. |
| `provenance` | object | Publication classification, section 3. |
| `scope` | object | Model limitations and context coverage, section 4. |
| `layer` | string | One layer token, e.g. `F.Cu`. All copper uses this layer. |
| `board` | object | `outline`: outer polygon; `cutouts`: array of strictly internal polygon rings, possibly empty. |
| `movement_region` | polygon ring | All movable trace copper, including fixed endpoints, must remain inside this region and the board material. This is an artificial search boundary, not a physical board edge. |
| `rules` | object | Hard clearance minima, section 6. |
| `traces` | array | One or more movable polylines, section 5. |
| `fixed_copper` | array | Fixed geometric copper shapes, possibly empty. |
| `holes` | array | Fixed circular drill voids, possibly empty. |
| `keepouts` | array | Fixed forbidden polygons, possibly empty. |
| `groups` | array | Explicit sets of traces to optimise together, section 7. |
| `objectives` | object | Soft targets, weights and scales, section 8. |
| `limits` | object | Run and discrete-edit limits, section 9. |

Board material is the outer polygon minus its cut-outs. Cut-outs must be
disjoint and must not touch the outline. Edge-connected notches belong to the
outline itself. Fixed objects must lie within the outer outline; they need not
be inside the movement region. Fixed copper and drill holes may overlap to
represent a plated hole. Validate movable geometry against both independently.

## 3 Provenance and publication

`provenance` has exactly these fields:

| Field | Value |
|---|---|
| `kind` | `synthetic` or `private-board-extract`. |
| `public_fixture` | Boolean publication-intent flag. |
| `description` | Short plain-text description with no sensitive information. |

`public_fixture: true` requires `kind: "synthetic"`. A flag is not proof of
safe provenance: publication still needs human review. Private board extracts
must remain private, including screenshots, logs and optimisation histories.
No automatic telemetry or upload of inputs is permitted.

Renaming nets and stripping components does **not** make a board extract
synthetic. Neither does rotating, reflecting, translating or scaling its
geometry. Build public fixtures independently from invented coordinates and
generic routing motifs. Do not retain source paths, UUIDs, company identifiers
or source-to-fixture mappings in public artifacts.

## 4 Model scope

`scope` has exactly these fields:

```json
{
  "profile": "single-layer-independent-paths",
  "context_complete": true,
  "electrical_checks": "not-modelled"
}
```

`context_complete` is the producer's assertion that every relevant planar
obstacle capable of constraining a move anywhere in `movement_region` has been
included, accounting for widths and clearance distances. Objects outside the
region may still matter. A consumer must refuse optimisation if this flag is
false; it cannot establish completeness from a JSON file alone.

The profile excludes branches, shared endpoints, intended contact with fixed
copper, multilayer coupling and changing pours. Each movable trace has a
distinct net. If the intended problem needs these features, reject it or use
a later format version; do not omit them to fit this profile.

`electrical_checks` is exactly `not-modelled` in version 1. This format is
suitable for geometry development, not electrical or full-board sign-off.
A static copper polygon does not predict the result of a later zone refill.

## 5 Geometric objects

### Movable traces

Each entry in `traces` has exactly:

```json
{
  "id": "t01",
  "net": "n01",
  "width_mm": 0.2,
  "points": [[3, 5], [6, 9], [10.4, 13], [11.1, 17]]
}
```

At least two points are required. The first and last are fixed anchors.
Interior points may move; local bend insertion is permitted within `limits`.
The original polyline remains the immutable displacement/topology reference.
Splitting a segment must not reset that reference. Endpoints represent virtual
attachments to unmodelled external routes, not freely movable open ends.

### Fixed copper

Each entry has `id`, `net` and `shape`. Shapes are exactly one of:

```json
{"type": "polyline", "points": [[20, 2], [20, 8]], "width_mm": 0.25}
{"type": "circle", "center": [22, 10], "radius_mm": 0.45}
{"type": "polygon", "points": [[24, 12], [27, 12], [27, 16], [24, 16]]}
```

These are geometric obstacles, not component records. Fixed copper is solid;
an associated drill void does not make it permissible to route into its
annular copper envelope. No same-net clearance exemption is implicit: this
profile does not permit new or existing trace-to-fixed-copper junctions.

### Holes

Each entry has `id`, `center` and `radius_mm`. It represents a physical drill
void, not an already inflated clearance disc. Pads or via copper, if present,
are separate fixed-copper objects. Slots require a later profile or an explicit
conservative keep-out; do not silently substitute an undersized circle.

### Keep-outs

Each entry has `id`, `points` and `clearance_mm`. It forbids every movable
trace's copper from entering or approaching the polygon by less than that
clearance. It can represent a generic protected region without disclosing why
it exists. No directional or electrical shielding guarantee is implied.

## 6 Hard clearance rules

`rules` has exactly these fields:

| Field | Meaning |
|---|---|
| `copper_clearance_mm` | Minimum copper-boundary distance between movable traces, and between movable and fixed copper. Applies even to equal net tokens. |
| `edge_clearance_mm` | Minimum copper-boundary distance to physical board edges and cut-outs. |
| `hole_clearance_mm` | Minimum copper-boundary distance to each drill void. |
| `pair_clearances` | Optional extra minima expressed as an array, possibly empty. |

Each pair entry is `{"a": "t01", "b": "h01", "clearance_mm": 0.4}`.
The unordered pair must reference distinct existing objects, with at least one
movable trace. Duplicate pairs are invalid. The effective minimum is the
**maximum** of this value and the applicable base rule; never a relaxation.
For a keep-out, its own `clearance_mm` is the base rule. Physical edges use
the edge rule, not pair entries.

For trace centrelines with widths `w1`, `w2`, require distance at least
`w1/2 + w2/2 + clearance`. A circle obstacle adds its radius; a hole uses
the hole rule. Containment in the board material and movement region is
required independently of boundary distances.

Clearance constraints apply to complete segments and swept movements, not
only vertices. Preserve the initial obstacle-passing side and relative route
ordering. A collision-free candidate reached by jumping through an obstacle
is invalid. The solver must establish topology preservation; this format does
not prescribe a particular corridor or homotopy algorithm.

Fail preflight on an initially infeasible movable trace. (kat's solver may still
run from an input that is only short of clearance, never letting a shortfall grow;
see `ROUTE_POLISH_SPEC.md` §11, Recovery.) Fixed context is not
itself a board DRC target; overlapping fixed copper and holes can be intentional.
Unsupported rules in a private board must not be dropped when creating input.
An adapter approximating physical curves must account conservatively for its
approximation error; this format supplies no implicit error allowance.

## 7 Groups

Each entry has `id` and `trace_ids`, an ordered array of unique movable trace
IDs. Groups are disjoint and every movable trace belongs to exactly one.
Group IDs are unique. Array order is the supplied neighbouring-route order,
not inferred from net names.

A prototype should accept one selected group per run. Other groups, if present,
are fixed at their input geometry for that run. Approximately 5-10 traces per
group is the intended use, not a format validity limit. All listed copper
still participates in collision checking.

## 8 Soft objectives

`objectives` contains:

| Field | Meaning |
|---|---|
| `angle_step_deg` | Exactly `45` in version 1. |
| `preferred_spacing_mm` | Preferred copper-to-copper spacing for adjacent, locally parallel group members. At least the base copper clearance. |
| `preferred_edge_margin_mm` | Preferred copper-to-edge margin, at least the hard edge minimum. No extra reward beyond it. |
| `parallel_tolerance_deg` | Maximum undirected angular difference for a pair of local runs to count as approximately parallel; greater than 0 and less than 45. |
| `displacement_scale_mm` | Positive scale for movement relative to the original polyline. |
| `short_segment_mm` | Positive length below which a segment attracts a complexity penalty. |
| `weights` | Non-negative finite weights named `angle`, `spacing`, `edge`, `displacement`, `length`, `complexity`; at least one is positive. |

These settings are soft; a pair-specific hard rule can override a spacing
target. Non-neighbours are still subject to hard clearance.

Merit components must be dimensionless and independent of vertex count.
Proposed normalisation: angle residual by half the angle step; spacing
residual by preferred spacing; edge shortfall by preferred margin; displacement
by its scale; excess length by original length; additional bends by original
segment count. Short-segment penalties use `short_segment_mm` as their scale.
Use arc-length-weighted averages for continuous geometric terms so that
inserting vertices cannot dilute them. For displacement, measure distance to
the original polyline, not a reindexed list of original vertices.

Exact sampling, neighbourhood matching and aggregation remain solver choices
to document and version before comparing scores across implementations. The
input weights do not promise bit-identical scores from different solvers.
The run record must store the solver identity and effective merit definition.

## 9 Limits and reproducibility

`limits` has exactly:

| Field | Meaning |
|---|---|
| `max_iterations` | Positive integer cap on solver iterations; solver must document its iteration unit. |
| `max_added_vertices_per_trace` | Non-negative integer cap relative to the input point count. Removing/reinserting vertices must not evade it. |
| `snap_tolerance_deg` | Positive angle deviation below which a snap may be attempted, less than half the angle step. Not a tolerance for claiming exact snapping. |
| `seed` | Non-negative integer seed for any random decisions. |

There is no distance limit: a trace may move anywhere in `movement_region` that the
hard constraints allow, and the `displacement` weight is what keeps it near the input.
Earlier drafts had `max_displacement_mm`; a reader refuses it with that explanation.

The JSON is sufficient as a replay input only when accompanied by the solver
version and effective options. Accepted/rejected event history and candidate
output formats are separate contracts. Input must never be overwritten.

## 10 Complete synthetic example

The companion file **`route_polish_example.json`** is a complete example.
It can be placed beside this document or in a project's fixture directory.
The sample is deliberately invented and has no source board or component map.
Its `F.Cu` token is a generic layer name, not evidence of extraction.

It contains five width-0.2 mm traces passing to the right of a concave,
polygonal board edge; non-45-degree segments; uneven bundle spacing; a
relatively edge-close inner trace; a fixed neighbouring route; fixed copper;
two drill voids; and a rectangular keep-out. Endpoints are fixed.

Suggested first exercise: load and preflight the sample, render actual copper
widths and inflated forbidden regions, then inspect continuous moves, snap
attempts and bounded bend insertions with `--gui`. A successful run need not
snap every segment or achieve every preferred spacing.

This fixture is not a golden optimised result or a substitute for tests of
infeasible inputs, topology changes, malformed JSON or unsupported features.
Only explicitly reviewed synthetic documents and fixture files should enter
a public repository; do not copy neighbouring files or directories wholesale.

## 11 Version 2: anchored paths

Every trace on a real board ends on copper of its own net, such as a pad, a via or
a stub that continues elsewhere. Version 1 forbids that contact. Version 2 is
version 1 with exactly the following changes, and is the format that
`kat pcb polish extract` writes. A reader accepts `version` 1 or 2 and applies
each version's rules only to documents of that version.

- `scope.profile` is exactly `single-layer-anchored-paths`.
- Each entry in `traces` has exactly `id`, `net`, `width_mm`, `points` and
  `anchors`. `anchors` is an array, possibly empty, of unique IDs. Each ID
  names either `fixed_copper` whose `net` equals the trace's `net`, or a
  `holes` entry: the drill of a plated pad or via that the trace ends on.
- Every copper anchor must touch the trace's first or last point: that point
  lies inside or on the anchor's copper (a polyline's copper is its buffered
  centreline). A hole anchor's centre must lie inside or on one of the same
  trace's copper anchors. An endpoint without an anchor is a virtual
  attachment, as in version 1.
- Clearance between a trace and its own anchors is not checked, and that
  includes hole clearance to an anchor drill. A trace's copper may overlap them
  anywhere, because same-net contact cannot short.
  Every other fixed copper keeps the full copper clearance, including copper of
  the same net that is not one of the trace's anchors. New same-net junctions
  therefore remain impossible.
- Several movable traces may carry the same net token. The copper clearance
  still applies between them.
- A `pair_clearances` entry between a trace and one of its own anchors is
  invalid.
- Traces still have distinct endpoints. Two traces that share an endpoint (a
  branch) are outside this profile; the extractor refuses such a selection.

`provenance.kind` of an extract is `private-board-extract`, and
`public_fixture` is `false`. The mapping back to the board, including net names,
segment UUIDs, the source path and its hash, lives in a separate private
`*.map.json` file. It is never part of the case.

The extractor excludes tracks belonging to recorded stamped block instances and
their nested member groups from movable traces, before constructing runs or
selecting a layer. Nearby excluded copper remains `fixed_copper`; external runs
end at its fixed boundary and may name touching same-net copper as an anchor.
Membership comes from stamp records and matching groups, not a block's geometric
bounds. Ordinary and unrecorded groups remain eligible. If no eligible straight
tracks remain because they are all block-owned, extraction reports that explicitly.
This uses the existing version-2 copper and anchor fields, without a format change.
