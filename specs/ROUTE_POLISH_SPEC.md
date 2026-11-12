# Topology-preserving route polishing - Specification

Version: **0.2** (2026-11-12), discussion draft; no implementation authorised
by this document. **[A]** denotes a proposed assumption, **[Q]** an open question.

This is a tool specification, separate from the board requirements in
[`SPEC.md`](SPEC.md). It does not change those requirements or authorise
modification of either working board.

## 1 Purpose and scope

Improve the geometry of existing, electrically connected routes without
rerouting them. The existing topology is considered good. Optimisation should
make small movements to:

1. Prefer trace directions at multiples of 45 degrees.
2. Make spacing between neighbouring traces more uniform.
3. Increase board-edge clearance where it is unnecessarily small.

Optimise groups of approximately **5-10 neighbouring traces** together. Test
on copies of existing routes so that the user can inspect individual runs.
An optional **`--gui`** mode visualises the optimisation steps.

Non-goals: global routing, changing net assignments, moving components or vias,
adding vias, changing trace layers, or moving routes to the other side of a
hole or other obstacle. Trace widths remain fixed **[A]**.

## 2 Input and working model

Input shall be a set of traces and constraint-defining geometrical features, as specified in specs\ROUTE_POLISH_INPUT.md.
Use an explicitly selected route group.
A trace in this context is a connected, editable polyline between fixed
anchors, not an individual KiCad track segment **[A]**.
The `kat` tool shall get a command for the extraction of a traces subset from a .kicad_pro, and re-import of the optimized paths back.

The local model must include:

- Editable traces, with their original vertices, widths, layers and nets.
- Fixed anchors: pads, vias, selection boundaries and branch points **[A]**.
- Surrounding fixed copper, including same-net copper where connectivity or
  topology could change.
- Board outlines, cut-outs, holes, pads, vias and explicit keep-out areas.
- Applicable clearance rules and project-specific protected regions.
- Relevant copper on other layers where electrical constraints require it.

Context must extend far enough to cover every permitted movement, not merely
the original trace bounding box. Unsupported geometry or unresolved applicable
rules must be reported; they must not silently disappear from the model.

## 3 Hard constraints

No weighted improvement may compensate for violating a hard constraint.

1. Preserve net connectivity, fixed anchors, trace widths and layer assignments.
   Do not add, remove or move vias.
2. Preserve topology: obstacle-passing side, ordering of neighbouring routes,
   and intended junctions. Do not introduce crossings, self-intersections,
   shorts, disconnected ends or unintended same-net junctions.
3. Meet applicable copper, hole, keep-out and board-edge clearances.
4. Preserve project-specific electrical restrictions, including input/output
   separation, guard coverage and adjacent-layer proximity constraints.

For T0400, `SPEC.md` remains authoritative. Its current copper-to-board-edge
minimum is **0.3 mm**; distances must be measured from the copper boundary,
not the centreline. Use the actual rules applicable to each obstacle rather
than treating all holes or copper as identical.

Topology protection must cover the movement between accepted states, not
only collision-free start and end states. A large numerical step must not
allow a trace to jump across an obstacle.

If the initial selection violates a hard constraint, report it and do not
silently relax that constraint **[A]**. Ordinary DRC is not sufficient evidence
that T0400's crosstalk constraints have been preserved.

## 4 Merit function

Within the feasible region, minimise a weighted combination of:

| Component | Intended behaviour |
|---|---|
| Angle | Soft penalty for deviation from the nearest multiple of 45 degrees. |
| Spacing | Penalise deviation from a preferred edge-to-edge spacing along neighbouring, approximately parallel runs. |
| Edge proximity | Penalise clearance below a preferred margin larger than the hard minimum; no reward beyond that margin. |
| Displacement | Prefer small movements from the original route. |
| Length | Discourage unnecessary extra length. |
| Complexity | Discourage additional bends, very short segments and small zigzags. |

Uniform spacing is a local objective, not a requirement to equalise every pair
of traces. Pad escapes and bottlenecks may legitimately have different spacing.
Neighbour relationships should be stable enough that changing the objective's
pairing cannot hide a poor result **[A]**.

Terms should be normalised so that weights remain interpretable across groups
with different lengths and segment counts **[A]**. Expose the individual terms
and total score for inspection. Hard-constraint failures are separate from
the score.

Make weights of the contributors configurable.

Make the merit function **differentiable**, and implement automatic derivative for it. We could e.g. use PyTorch for that.

## 5 Optimisation and discrete geometry changes

Use constrained, group-wise optimisation with a small number of discrete
corner choices, rather than one unrestricted weighted optimiser.

The proposed iteration is:

1. Optimise movable vertices or segment offsets using the soft angle penalty.
2. When a segment approaches a multiple of 45 degrees, attempt an exact snap.
   Treat snapping as a constrained local solve: neighbouring bends and traces
   in the group may move to accommodate it.
3. Accept the snap only if geometry remains feasible and its objective trade-off
   meets a defined acceptance policy **[A]**. Keep the segment's direction exact
   in subsequent continuous steps **[A]**.   
4. If snapping fails, first try adjusting the surrounding group. If necessary,
   insert an additional bend point and retry optimisation with a small set of
   candidate corner patterns.
5. Reject candidates that violate constraints, change topology or add complexity
   without sufficient benefit. Retaining a non-45-degree segment is allowed
   when it is the better feasible result.

Adding a bend is a local geometric refinement, not permission to change the
route's obstacle corridor. Bound bend insertion and movement, avoid repeated
snap/unsnap cycles, and stop on convergence, iteration limit or user request.
Exact limits, snap tolerance and acceptance policy remain **[Q]**.

Maintain a best feasible state. Rejected trials must not corrupt it. Changing
the number of vertices must not make scores incomparable or artificially
improve displacement/angle terms **[A]**.

## 6 Optional GUI

With **`--gui`**, display the local problem in a simple 2D inspection window:

- Original traces faintly, current accepted traces prominently, and trial
  candidates with a distinct style.
- Fixed anchors, surrounding traces, obstacles, board edges and keep-out areas.
- Copper at its actual width and an optional clearance-envelope overlay **[A]**.
- Clearly distinguished layers; relevant other-layer context can be toggled
  without being removed from constraint checking **[A]**.

Proposed controls **[A]**: **Step**, **Run/Pause**, **Back**, **Reset**, zoom/pan,
and explicit **Export candidate**. Step advances one recorded optimisation
event; Back inspects history without silently creating a new solver branch.
Reset restores the original working geometry.

Show event type, iteration, total score, individual merit terms, minimum
clearances with the responsible object pair, and constraint status.
Distinguish continuous movement, snap attempts, bend insertions, acceptance and
rejection. Rejected candidates must be inspectable with a reason.

Long-running optimisation must not freeze the window, and pausing/stopping
must preserve the last accepted feasible state. Without `--gui`, use the same
optimisation and validation logic **[A]**.

## 7 Safe output and reproducibility

Never modify the input board, source route files or working layout.
Write candidates only to an explicitly chosen separate output; the GUI must
not export automatically. Export of the best feasible state is proposed
as the default **[A]**. Do not overwrite an existing output without explicit
permission.

Record enough information to replay the inspection history:

- Input snapshot identity, selected objects, fixed context and applicable rules.
- Settings, weights, tolerances, solver version and random seed if applicable.
- Accepted states and inspectable rejected candidates, including discrete edits.
- Scores, constraint results, rejection reasons and termination reason.

All experiments and outputs remain separate from production layout data.
Any later application to a working board is a separate, explicitly authorised
step requiring full board validation, including DRC and relevant electrical
checks. A successful local optimisation is not a board-level safety certificate.

## 8 Acceptance criteria for a prototype

1. Load a copied real route group with surrounding obstacles and fixed copper,
   and inspect a run in the GUI without changing any source files.
2. Demonstrate feasible movement, an exact snap, a rejected snap and a bounded
   bend-insertion attempt. Not every group must benefit from every operation.
3. Preserve anchors, connectivity, widths, layers and obstacle topology; enforce
   clearance against actual copper widths, holes, keep-outs and board edges.
4. Show how each merit term changes, including cases where constraints prevent
   visual improvement. Do not claim improvement solely from a lower angle term.
5. Step through and replay accepted and rejected events; pause, inspect earlier
   states and reset without altering the source or accepted history.
6. Export a separate candidate and validate the reconstructed geometry.
   Before accepting a board candidate, run full DRC and required project checks.

## 9 Open choices

- **[Q]** Initial board, region and 5-10-trace selection mechanism.
- **[Q]** Preferred spacing and edge margin; merit weights and normalisation.
- **[Q]** Solver, geometry representation and topology-preservation mechanism.
- **[Q]** Snap tolerance, bend/movement limits, stopping and acceptance policies.
- **[Q]** Encoding of T0400 electrical restrictions and treatment of copper pours
  whose shape may change after a refill.
- **[Q]** GUI toolkit and history and candidate file formats. The proposed
  portable input format is defined in section 10.

These are implementation choices to resolve before coding, not permission
to weaken the hard constraints.

## 10 Portable input and public development fixture

[`ROUTE_POLISH_INPUT.md`](ROUTE_POLISH_INPUT.md) defines a standalone JSON
input contract, version 1, for an initial single-layer local polishing problem.
[`specs/route_polish_example.json`](specs/route_polish_example.json) is its
complete, deliberately synthetic example: five movable traces bending around
a polygonal curved-edge approximation, with unequal spacing, arbitrary angles,
fixed surrounding copper, holes and a keep-out.

The fixture was constructed from invented coordinates, not extracted,
translated, mirrored, scaled or otherwise transformed from a company board.
Removing component information alone would not make actual route geometry
suitable for an open-source repository: outlines, hole patterns, trace
arrangements and other geometry can still disclose the design.

The version-1 profile intentionally does not model a complete multilayer board,
branch junctions or changing copper pours. It must not be used to claim that
project electrical constraints or production-board safety have been checked.
Later private board adapters may implement this contract without contributing
their input boards, generated cases or company-specific constraints to the
public repository.
