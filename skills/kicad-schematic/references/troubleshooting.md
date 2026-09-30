# Schematic troubleshooting

- Missing tools/libraries, translated diagnostics or Windows encoding errors:
  read [setup](../../kicad-design/references/setup.md).
- Negative comma-separated CLI values: use `--stub=-5.08,0`, not
  `--stub -5.08,0`; argparse may treat the latter as another option.
- Moving a field on a multi-unit symbol: supply unit explicitly, e.g.
  `s.set("U1", "Value", unit=2, at=(x, y))`.
- A move lands on foreign wiring: use move_many for swaps, or detach/connect in
  one pass. Do not override contact protection merely to suppress an error.
- Steps in shared wires after dragging: detach/connect affected pins.
- A mirrored power symbol points the wrong way: detach its pin and redraw the
  power symbol with the intended stub/orientation.
- Replacing labels by a wire lost a named net: put a visible net label on the
  wire. A later sync restores labels when expected net names are absent.
- Sync unresolved pin contact: a pin directly touching another pin cannot
  always be reattached automatically. Inspect that location, separate/rearrange
  safely, sync, then compare the netlist.
- Wrong connectivity after an arrangement pass: restore the last good snapshot
  with `kat restore SCH STEP`. For an actual circuit change, fix SKiDL instead.
- Unexpected removed power driver: detach can remove a PWR_FLAG; sync restores
  missing flags. Do not add multiple flags to each stamped block instance.
- A large PNG looks clean but labels are unreadable: read all tiles and take
  close-ups. Overlap heuristics do not see every pin number or wire/text crossing.
- SKiDL no-connect import fails: NC is a SKiDL builtin; do not import it.
  Run kat.skidl_blocks.write after generating the netlist to record NC pins.

Use `kat check --full` for uncapped netlist findings. Review gate warnings
individually; an extra thermal/shield pad can be intentional, while an actual
symbol pin lacking its numbered footprint pad is a blocking error.
