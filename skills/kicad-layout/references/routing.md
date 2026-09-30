# Routing and DRC troubleshooting

Read when autorouting fails or for partial-net routing. Build structure first:
plane vias, short critical tracks, then locked copper, then the router.
Judge the imported result with KiCad DRC, unconnected and islands, not
Freerouting's own unrouted count.

- Dedicated plane layers do not carry router tracks. On two layers, pours may
  be cut into islands; use stitching and verify connectivity.
- Freerouting can treat vias inside wire keepouts as violations even if vias
  were allowed there. Do not protect a plane with a broad track keepout that
  covers its necessary vias.
- Fan out IC plane pads before placing decoupling, then fan out the capacitors.
- Set fabrication constraints before classes/rules. A custom rule cannot ask
  less than the board minimum. Edge exemptions should be explicit references.
- `fine_pitch` lists nets whose class width/clearance cannot leave a pin.
  Choose a suitable class from the electrical/fabrication requirements rather
  than relying on automatic neckdown.
- `narrow` lists imported tracks kat could not widen to their class width;
  inspect and resolve them against DRC and the design requirements.
- `--net` routes only selected nets; other copper/pads remain obstacles.
  Class-to-class rules need represented nets of both classes in that run.
  `class_clearance_not_applied` identifies omitted rules: widen the selection
  or deliberately verify the required separation with DRC.
- Named planes/keepouts/pours make setup rerunnable. Multiple pours on one net
  and layer need distinct names. keepout False means forbidden.
- A locked port via joined on only one layer may yield via_dangling. Connect
  it or explain and accept the warning with `--accept via_dangling=REASON`;
  do not accept blindly. DRC errors cannot be accepted.

Inspect history's DSN/SES and freerouting.log when needed. Iterate structure,
classes or keepouts, then remove_tracks (locked copper stays) and route again.
For design changes, fix SKiDL, sync/check the schematic, then pcb sync and inspect
stale_tracks before using --rip-stale.

Finish/refill, fabricate, and run pcb ready after the final board change.
Automated length tuning, differential-pair routing and impedance-controlled
stack-ups are not provided; escalate those requirements instead of declaring
generic autorouting sufficient.
