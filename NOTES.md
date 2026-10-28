## Feature requests

1. **Net-class coupling check in `kat pcb check`.**
   - Flag copper of net class A (e.g. amplifier outputs `OUT*`) that overlaps,
     or comes closer than a given distance to, copper of a *different-index* net
     of class B (e.g. high-impedance inputs `IN*`) on the same or an adjacent
     layer.
   - Report the overlap length/area and a parallel-plate capacitance estimate
     from the stack-up.
   - Motivation: in multi-channel transimpedance amplifiers with high feedback
     resistance (MΩ range, feedback capacitance ≈ 0.1 pF), about 2 mm of
     overlap between one channel's output and another channel's input, on
     adjacent layers 0.1 mm apart, gives ≈ 0.2 pF. That is ≈ 0 dB crosstalk.
     Such a coupling is easy to miss in review and is not caught by DRC.
   - Also useful: a rule that "net class X is only allowed on layers …" and a
     rule that requires "a solid plane under net class X".
2. **`kat pcb stamp --side bottom` (flip a block to the other side).**
   Dense double-sided designs place the same checked block on both sides, e.g.
   half of many amplifier blocks on the bottom. Without this option a
   mirrored second block is needed, which doubles the design and check effort
   and lets the two copies drift apart.

   **CLI.** `kat pcb stamp PCB BLOCK_PCB INST… --at X,Y [--rot R] --side bottom
   [--layer-map auto|roles|"L1=L6,…"] [--allow-asymmetric]`.
   Re-stamping keeps the side recorded for the instance (stored in the group or
   instance data), so `stamp all` does not silently flip blocks back.

   **Geometry.**
   - Mirror about the block's vertical axis (KiCad's default left/right flip),
     **then** rotate. `--rot` is as seen from the top, the same as for
     top-side instances.
   - `--at` names the top-left corner of the flipped block outline as seen from
     the top, so top and bottom instances share one placement grid. Document
     the convention, because the block origin moves to the other corner when
     mirrored.
   - `--step` applies unchanged. Port points are mirrored too, and the port
     check uses the mirrored points.

   **Layer mapping.** This is the core of the feature.
   - `auto` (default) uses the mirror map L(k) ↔ L(N+1−k):
     F.Cu↔B.Cu, In1↔In(N−2), and so on. KiCad already has this map:
     `FlipLayer(layer, copperCount)`, the same one used by
     `PCB_TRACK::Flip`/`ZONE::Flip`.
     It also flips the paired technical layers: F/B Mask, Paste, SilkS, Fab,
     CrtYd and Adhes. `User.*` and `Edge.Cuts` stay as they are.
   - `auto` is only allowed if the stack-up is **mirror-symmetric**, judged on
     the design board's stackup:
     - dielectric thickness, εr and material mirror around the centre within a
       tolerance;
     - copper thickness of L(k) equals that of L(N+1−k);
     - the block board and the design board have the same layer count.

     Otherwise refuse with a per-layer diff, unless `--allow-asymmetric` is
     given. Why this matters: sensitive parasitics such as a sub-pF feedback
     capacitance, or an input guard, depend on the dielectric thickness to the
     reference plane. An asymmetric stack would silently change them on every
     flipped instance.
     Example of a symmetric 6-layer stack: dielectrics
     0.1 / 0.5 / 0.15 / 0.5 / 0.1 mm, all copper 35 µm.
   - `roles`: the block board and the design board each declare layer roles,
     for example in the board's `.kat` metadata or `kat pcb environment`:
     ```
     block (top):  L1=in_signal  L2=gnd_ref  L3=out  L4=out  L5=gnd_ref  L6=back_signal
     design:       L1=in_signal_top … L6=in_signal_bottom
     ```
     The flip maps by role ("in_signal on the component side",
     "gnd_ref adjacent to it"). This works for asymmetric stacks too, as long
     as the roles exist on both sides. If a role cannot be mapped, the stamp is
     refused.
   - An explicit map, `--layer-map "F.Cu=B.Cu,In1.Cu=In4.Cu,…"`, takes
     precedence. It is checked for bijectivity, and outer layers must map to
     outer layers.

   **Vias.**
   - Through vias are unchanged.
   - Blind, buried and µvias: the span (a, b) maps to (map(b), map(a)),
     e.g. a µvia L1–L2 becomes L5–L6 on a 6-layer board.
   - Refuse if the mapped span is not an allowed via type or layer pair in the
     design's stack-up or DRC rules. Also check the drill and annular ring
     constraints for that span.
   - KiCad 9+ padstacks with front/inner/back modes: swap the front and back
     definitions and map the per-layer inner definitions.

   **Other copied items.**
   - Footprints use KiCad's footprint flip. Pads, mask, paste, courtyard, fab
     layer, silk and reference/value text are mirrored and become readable from
     the bottom.
   - Zones and keep-out/rule areas have their layer sets mapped, keep their
     priority and get their fill invalidated; run `pcb refill` afterwards.
   - Board-only graphics and text are mirrored.
   - Net-ties and custom pad shapes are mirrored.

   **Checks on stamping.**
   - Overlap is checked against bottom-side courtyards only for SMD parts.
     THT pads and holes of the block and of the design (e.g. a through-hole
     socket's pin field) collide on both sides.
   - Net-class strictness is checked as today.
   - The group name records the side, e.g. `"/CH_A (NAME, bottom)"`.

   **Block side.**
   - `kat block check --sides both` builds a scratch board with the block flipped
     using the declared map. It runs DRC and the port check there, and stores a
     "bottom-OK" flag. `pcb stamp --side bottom` requires that flag, the same
     way it requires the check today.
   - Optional: render both sides in `kat block check` snapshots.

   **Interaction with request 6.** Layer roles make layer-placement rules
   checkable after flipping, e.g. "in_signal only on the outer layer of the
   component side, gnd_ref directly behind it". A block whose `IN*` nets end up
   on an inner layer after the map should be rejected by the request 6 check.
