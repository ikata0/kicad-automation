<!-- kat:init:start -->
## KiCad development with kat

Start with [kicad-design](.agents/skills/kicad-design/SKILL.md) for phases and gates.
Use [kicad-schematic](.agents/skills/kicad-schematic/SKILL.md) for circuit/schematic
work and [kicad-layout](.agents/skills/kicad-layout/SKILL.md) for PCB work.
Their linked references cover environment setup and advanced workflows.

Connectivity belongs in SKiDL: regenerate the netlist, sync and verify the
schematic, then sync the board. Keep KiCad editors closed during edits.
Review snapshots, use KiCad's ERC/DRC as the authority, and pass the schematic
and fabrication readiness gates before handover. Record passes and reasoning
in the design journal; report tool problems in KAT_FEEDBACK.md.

Look up commands with `kat --help` / `kat <command> --help`, and Python calls
with `kat api`. These instructions do not require an automation source checkout.
<!-- kat:init:end -->
