# Topology-preserving route polishing - Specification

Version: **0.3** (2026-11-12). The open questions are resolved in section 11, and
the implementation follows them. **[A]** denotes a proposed assumption, **[Q]** an
open question.

This is a tool specification, separate from the board requirements in the
board project's own `SPEC.md`, which is not kat's [`SPEC.md`](../SPEC.md). It
does not change those requirements or authorise modification of either working
board.

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

Input shall be a set of traces and constraint-defining geometrical features, as specified in [`ROUTE_POLISH_INPUT.md`](ROUTE_POLISH_INPUT.md).
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

For T0400, that board project's `SPEC.md` remains authoritative. Its current copper-to-board-edge
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

Use constrained, group-wise optimisation with discrete snapping and local
vertex insertion, rather than one unrestricted weighted optimiser.

The proposed iteration is:

1. Optimise movable vertices or segment offsets using the soft angle penalty.
2. When a segment approaches a multiple of 45 degrees, attempt an exact snap.
   Treat snapping as a constrained local solve: neighbouring bends and traces
   in the group may move to accommodate it.
3. Accept the snap only if geometry remains feasible and its objective trade-off
   meets a defined acceptance policy **[A]**. Keep the segment's direction exact
   in subsequent continuous steps **[A]**.   
4. If snapping fails, first try adjusting the surrounding group. If necessary,
   replace the segment by an already-bent shape whose legs lie on multiples of
   45 degrees (a corner, then a Z, each both ways), moved into place as a checked
   move, and retry continuous optimisation; if none is kept, split it with a free
   collinear vertex. No leg is snapped at insertion, and a shape is only kept for
   its merit gain, so no bend is forced on the result.
5. Reject candidates that violate constraints, change topology or add complexity
   without sufficient benefit. Retaining a non-45-degree segment is allowed
   when it is the better feasible result.

Adding a vertex is a local geometric refinement, not permission to change the
route's obstacle corridor. Bound vertex insertion and movement, avoid repeated
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
   A: Shall be given manually to the `kat` extract command. I think an LLM agent should choose the traces eventually. During testing, I may do it as a user.
- **[Q]** Preferred spacing and edge margin; merit weights and normalisation.
   A: Edge margin: 1.0 mm preferred, so there will be space left for guard rings if desired. 
   Spacing: Not sure - e.g. twice the minimum trace distance preferred.
   Merit weights: configurable. Make a reasonable default choice, we can optimize them later.
- **[Q]** Solver, geometry representation and topology-preservation mechanism.
   A: I would try making the merit function differentiable, then trying PyTorch's gradient descent optimizers.
- **[Q]** Snap tolerance, bend/movement limits, stopping and acceptance policies.
   A: Snap tolerance: 1° ? And if a snap fails and a new bend was created, we must give the algorithm some iterations to optimize with the new set of bends.
   Stopping policy: Stop if all angles snapped sucessfully, or if no convergence could be seen. Also limit the number of bends.

   
- **[Q]** Encoding of electrical restrictions and treatment of copper pours whose shape may change after a refill.
A: Copper pours may change, surrounding traces should be hard constraints, same for pads, vias, holes. 

- **[Q]** GUI toolkit and history and candidate file formats. The proposed
  portable input format is defined in section 10.

These are implementation choices to resolve before coding, not permission
to weaken the hard constraints.

## 10 Portable input and public development fixture

[`ROUTE_POLISH_INPUT.md`](ROUTE_POLISH_INPUT.md) defines a standalone JSON
input contract, version 1, for an initial single-layer local polishing problem.
[`route_polish_example.json`](route_polish_example.json) is its
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

## 11 Implementation decisions (v0.3)

These close the **[Q]**s of section 9 for the prototype. Weights and limits stay
configurable.

- **Commands.** `kat pcb polish extract|check|run|replay|apply`. The library is
  `kat.pcb.polish_*`, and the code lives in `src/kat/pcb/polish/`. `extract`
  takes nets, a layer and an optional region, and writes a version-2 case
  ([`ROUTE_POLISH_INPUT.md`](ROUTE_POLISH_INPUT.md) §11) plus a private map file.
  `apply` writes the result into a **new** board file, never the input. It
  then runs DRC on that copy and compares it per net with the original.
- **Endpoints.** A trace's own endpoint pads, vias, their drills and stubs are its
  `anchors`. Clearance to them is waived, while all other copper of the same net
  keeps full clearance. When selected runs share an end, the longer one moves and
  the others stay fixed (`held`).
- **Group order.** It is inferred geometrically at extraction from the offset
  across the bundle, reported, and can be overridden with `--order`.
- **Topology.** Every change of state is a linear interpolation of vertex
  coordinates. Such a move is split into sub-steps that are each shorter than
  the current distance of the centrelines to contact with an obstacle or another
  trace (conservative advancement). Nothing can pass through anything while
  anchors stay fixed, so the obstacle side and the route order are preserved,
  including across discrete edits. Clearance is checked on the state a move ends
  in, not on the way: a trace that sits exactly at its clearance (common on
  router output) can still move away. A step whose end state fails the hard check
  is retried with what the failure names kept where it is: the vertices of a
  segment short of clearance, and only when that cannot help, the whole trace.
  One trace at a limit does not stop the group. There is no distance limit:
  the extractor's movement region (the traces' bounding box grown by
  `--move-margin`, default 2 mm) only bounds the context the case describes.
- **Solver.** The merit terms are computed in PyTorch (float64, autograd).
  `torch.optim.Adam` proposes each step, and projection keeps snapped directions
  exact. Backtracking continues until the sweep and the independent hard check
  (numpy, no autograd) pass. One iteration is one optimiser step. A
  solver-only barrier near the constraints is never reported as score.
- **Spacing pairs.** These are adjacent traces in group order that run locally
  parallel. The pairing is frozen from the original geometry, with samples
  placed by arc-length fraction.
- **Defaults for extracts.** Edge margin 2.0 mm; preferred spacing 2 × the copper
  clearance; short segment 2.5 trace widths, at least 0.25 mm; snap tolerance 1°;
  `max_added_vertices_per_trace` 4; `max_iterations` 5000, a cap that a run normally
  stops short of on stagnation; weights spacing 0.5, edge 1.0, displacement 0.05 (low
  displacement weight, so traces may move far), as in `route-polish/routing-fcu.json`.
  Chosen on that case by runs to stagnation: 1000 iterations end before convergence,
  and 0.25 mm short segments improved the angle, spacing and edge terms together.
- **Discrete edits.** Snap within tolerance or when progress stagnates (merit
  improvement below 1e-4 over 20 iterations). A snap is followed by 20 settle
  iterations. It is accepted if the merit is at most `snap_slack` (0.001) worse,
  and the best feasible state credits each snapped segment by that allowance. If it fails, the group
  is adjusted: the segment's angle weight is raised for 20 iterations and the
  snap is retried. If that fails, bend the segment, trying in order until one
  is kept: the corner (one vertex, legs on the two multiples of 45 degrees that
  bracket the segment's direction) with either leg first, the Z (two vertices: one
  direction in halves around the other) both ways, and last one free midpoint, whose
  child segments start collinear with unchanged geometry. A shape's vertices are
  inserted on the segment and moved into place as a checked move; a blocked shape is
  a rejected `bend` event and the next is tried. No leg is snapped. A Z needs two of
  the trace's added vertices; a shape that does not fit the allowance is skipped. Each trial runs
  `settle_iterations` (20 by default) before it is accepted or reverted;
  no merging or snapping occurs during that settling period. Shapes come first
  because a collinear vertex barely moves: turning one half towards 45 degrees turns
  the other away by as much, so the length-weighted angle term has no first-order
  gradient there. The iteration
  limit and user stop still apply. Keep the trial if clearance shortfall drops,
  or if it does not grow and merit improves by at least `bend_gain` (0.005).
  A visible bend is not required separately; collinear vertices may be merged
  outside trials. The tip of a detour that doubles back (a turn of at least
  `reversal_turn_deg`, default 150 degrees) is moved onto its nearer neighbour and
  dropped outside trials, as a checked move (`simplify`); the longer leg keeps its
  snap when the projection allows it. A trial without sufficient benefit is
  reverted. Snapped segments
  stay snapped except when explicitly split for rescue. Each
  segment gets one failed attempt (configurable), and inserted vertices count
  cumulatively per trace. These numbers are solver options (`--option KEY=V`):
  `kat pcb polish run --help` lists each with its default and meaning.
- **Recovery from a start short of clearance.** `check` reports such an input as
  not ok, and says whether it is `recoverable`: every violation is a clearance
  shortfall while the centrelines stay clear of the obstacles. Anything else (a
  centreline in an obstacle's copper, off the board, inside a pad or keep-out)
  is `blocking`, and `run` refuses it. `run` starts from a recoverable input
  without relaxing anything silently: each (trace, obstacle) pair may stay as
  short as it is but never get shorter, and once reduced a shortfall stays
  reduced. The barrier pushes out of the shortfall. A smaller total shortfall
  outranks a better merit for the best state and for committing a trial. When
  the shortfall gains less than 1e-4 mm over 20 iterations, the worst short
  segment not tried yet gets a `rescue` split. A free collinear vertex is inserted
  where it comes closest to the obstacle (a snapped segment too, with its
  direction lock removed), settled without imposing a bend, and kept if the shortfall
  dropped. The same bend is tried, once snapping has stagnated, on a segment that
  steps kept running into an obstacle (`blocked_bend_after`, 5 times). Rescue
  bends count against `max_added_vertices_per_trace`. The result lists the
  start's violations and any that remain. A result that still has violations
  says `feasible: false`, and `apply`'s read-back and DRC show the same.
- **Stopping.** The run stops when all segments are snapped, when it has
  stagnated with no candidates left, at the iteration limit, or on user stop.
- **Pours.** Copper pours are not obstacles, because they reflow after a refill
  and DRC on the applied copy judges them. They are listed as unmodelled.
  Tracks, pads, vias and holes are hard constraints. Rules that cannot be
  represented make `extract` fail.
- **GUI.** Qt (PySide6, optional extra `kat[polish-gui]`). The solver runs in a
  worker thread, and the window only browses the recorded history.
  Automatically inserted vertices are pink dots with white outlines, kept at
  a fixed screen size when zooming and drawn above traces and trial highlights.
  They follow the event's geometry in live runs, history navigation and replay;
  reverted or merged vertices lose their markers. Tooltips identify the trace
  and vertex, and the details panel gives the count and colour legend.
- **Output.** Run record, history and result formats:
  [`ROUTE_POLISH_OUTPUT.md`](ROUTE_POLISH_OUTPUT.md).
