# kicad-automation

Tools for letting an agent (or a script) develop KiCad 10 schematics and PCBs from the
command line, without MCP and with the KiCad GUI closed.

- `kat` — Python library and CLI (`kat …`, `python -m kat …`): sync a SKiDL design into a `.kicad_sch`, arrange the drawing, check, snapshot (`src/kat/`)
- `NAME_REPORT.md` — the design journal kat keeps in every project: each command with its
  result, each pass (`kat batch-exec`) with its code, the agent's comments, snapshot images
- `kat api [NAME]` — signatures and docstrings of the library, for agents; `kat open FILE
  --verify` — let KiCad's own editor load the result and compare
- `kat pcb` / `kat.pcb` — the board: build it from the schematic (proven by DRC parity), setup, placement, structure, routing (Freerouting through kat's own Specctra DSN/SES), finishing, fabrication files and a release gate (`src/kat/pcb/`)
- `kat init [DIR]` — deploy bundled agent skills, references and a managed `AGENTS.md`
  section into a design directory; no automation checkout required
- `skills/kicad-design/` (phases and gates), `skills/kicad-schematic/`, `skills/kicad-layout/` — the canonical agent skill sources
- [`SPEC.md`](SPEC.md) — workflow, sync semantics, checks, and the pitfalls found so far

## In one picture

```
SKiDL design ──netlist──► kat sync ──► .kicad_sch ◄── kat batch-exec passN.py (import kat)
                                           │
                                 kat snapshot / check: ERC, netlist == design,
                                 drawing findings, PNG for review
```

The design owns parts and connections; the drawing owns positions. `kat sync` is
incremental, so a design change touches only the affected spots of an existing arrangement.

```
.kicad_sch ──kat ready (gate)──► kat pcb sync ──► .kicad_pcb ◄── kat batch-exec (layout passes)
                                                     │  setup · placement · structure
                                   kat pcb route ────┤  (Freerouting, headless)
                                                     │
                     kat pcb check / snapshot: DRC, parity, islands, layer pictures
                     kat pcb fab ──► Gerbers, drill, BOM, pick-and-place ──► kat pcb ready
```

The board follows the schematic the same way: `kat pcb sync` keeps placement and copper and
reports what a design change made stale.

## Install

### Linux

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                              # needs kicad-cli; e2e test also SKiDL
```

### Windows (PowerShell)

Install Python 3.10+ and **KiCad 10**, including its symbol and footprint libraries.
For example, with Windows Package Manager (or use the installers from
[Python](https://www.python.org/downloads/windows/) and
[KiCad](https://www.kicad.org/download/windows/)):

```powershell
winget install --id Python.Python.3.12 --exact --source winget
winget install --id KiCad.KiCad --exact --source winget
```

On non-English Windows, select **Preferences > Language > English** in KiCad:
some checks parse English item descriptions, and Windows KiCad does not honor
the `LANG=C` override used on Linux. Open the Schematic Editor and PCB Editor
once and accept the default global symbol and footprint library tables when
prompted, then close the editors. Without those tables, KiCad's ERC/DRC reports
missing-library warnings.

Open a **new PowerShell window** after installation and run the following from the
repository root:

```powershell
$env:PYTHONUTF8 = '1'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev,render]'
.\.venv\Scripts\kat.exe --help
.\.venv\Scripts\python.exe -c "from kat.env import run_cli; print(run_cli('version').stdout.strip())"
```

No manual KiCad PATH setup is needed for kat. When a command needs KiCad, kat
checks `KICAD_CLI`, then PATH, then KiCad 10's per-user and machine-wide Windows
installer registrations (including custom installation directories), and finally
the standard installation folders. The discovered path is cached as `KICAD_CLI`
**inside the running kat process** and inherited by its child processes. A program
cannot change its calling PowerShell's environment; separate kat invocations
discover it again automatically. For an unregistered installation elsewhere, set
`$env:KICAD_CLI = 'D:\your-install\bin\kicad-cli.exe'` explicitly.

For development tests or standalone SKiDL scripts, populate the PowerShell
session's variables automatically from kat's discovery:

```powershell
$env:KICAD_CLI = & .\.venv\Scripts\python.exe -c "from kat.env import kicad_cli; print(kicad_cli())"
if ($LASTEXITCODE -ne 0) { throw 'KiCad discovery failed' }
$kicadBin = Split-Path $env:KICAD_CLI
$env:Path = "$kicadBin;$env:Path"
$env:KICAD10_SYMBOL_DIR = Join-Path (Split-Path $kicadBin) 'share\kicad\symbols'
.\.venv\Scripts\python.exe -m pytest -m 'not slow' --basetemp .venv\pytest-tmp
```

Known test limitation on Python 3.12: `test_cli_and_library_give_the_same_file`
passes a negative comma-separated `--stub` value in a form argparse rejects.
For CLI use, write `--stub=-5.08,0` rather than `--stub -5.08,0` (see SPEC.md §7).

Repeat the development-test setup in new terminals when running tests or SKiDL
scripts. Tests check PATH for `kicad-cli` even if `KICAD_CLI` is set, while SKiDL
needs `KICAD10_SYMBOL_DIR` to find the installed symbol libraries.
Repeat `PYTHONUTF8=1` when using kat too: KiCad files and
reports contain UTF-8 characters, but Windows Python may otherwise use a legacy
default encoding. Kat itself discovers the stock libraries relative to kicad-cli.
The dedicated `--basetemp` directory is cleared by pytest; it keeps test projects
on the same drive as the repository, avoiding SKiDL's cross-drive relative-path
error when Windows TEMP is on C: and the checkout is on another drive.
The `render` extra supplies PyMuPDF for PNG snapshots on
Windows, where `rsvg-convert` is usually unavailable. To run the optional autorouting
tests, install Java 21+ (for example,
`winget install --id EclipseAdoptium.Temurin.21.JDK --exact --source winget`),
open a new terminal, then run `.\.venv\Scripts\kat.exe pcb route --install`.
GUI tests are opt-in (`.\.venv\Scripts\python.exe -m pytest -m kicad_gui`); they
require the `[gui]` extra and currently skip on Windows because their display
check expects Linux `DISPLAY` or `WAYLAND_DISPLAY`.

The editable installation above is ready to use; activation is optional because
the commands use the environment's executables directly. To build a wheel and
install it into a separate local environment instead:

```powershell
.\.venv\Scripts\python.exe -m pip wheel . --no-deps --wheel-dir .venv\dist
python -m venv .venv\installed
.\.venv\installed\Scripts\python.exe -m pip install '.\.venv\dist\kat-0.1.0-py3-none-any.whl[render]'
.\.venv\installed\Scripts\python.exe -m pip check
.\.venv\installed\Scripts\kat.exe --help
```

Use the wheel filename produced by the build if the package version changes.
This installation also auto-discovers KiCad; keep the UTF-8 setting when using it.

Requirements: KiCad 10 (auto-discovered on Windows/macOS, or `kicad-cli` on PATH,
or `KICAD_CLI` pointing to the executable), Python ≥ 3.10.
PNG snapshots use `rsvg-convert` if present, otherwise PyMuPDF (`[render]` extra; AGPL-3.0).
Autorouting needs Java 21+ and Freerouting: `kat pcb route --install` downloads the pinned
release (GPL-3, run as a separate program) once into kat's cache; routing tests are skipped
without it. `kat open --verify` for boards needs kicad-python (`[gui]` extra, MIT) and a
display; its tests run only with `pytest -m kicad_gui` (they open KiCad windows).

## Initialize a design directory

After installing kat, run `kat init` in your design directory, or supply a directory
to create. With the Windows environment above:

```powershell
.\.venv\Scripts\kat.exe init D:\workspace\my-pcba
```

On Linux:

```sh
.venv/bin/kat init ../my-pcba
```

From an environment where kat is on PATH, `kat init` uses the current directory
(like `git init`). It deploys:

```text
AGENTS.md
.agents/
  kat-init.json
  skills/
    kicad-design/SKILL.md       + references/
    kicad-schematic/SKILL.md    + references/
    kicad-layout/SKILL.md       + references/
```

Start with kicad-design, then the schematic/layout skill. References are included
for setup, blocks, hierarchy, stamping and routing; SPEC.md need not be copied into
the design repository. The same assets work from editable installs and installed
wheels. AGENTS.md points agents to the skills and design rules.

Initialization needs no KiCad and installs no dependencies, creates no circuit or
KiCad files, initializes no Git repository, and changes no global agent settings.
`kat new DIR/NAME.kicad_sch` remains the command to create a KiCad project.
Commit deployed skills, references, AGENTS.md and the manifest with the design so
instructions and provenance travel with it. Agents must support `.agents/skills`
discovery to load skills automatically.

Rerunning identical content changes nothing. Differing managed files or an edited
kat section in AGENTS.md cause one JSON error listing conflicts before any writes.
Use `kat init DIR --force` for a deliberate bundle update or replacement of local
customizations. Text outside the marked kat section and extra files are preserved;
obsolete files are not automatically removed. Invalid/duplicate AGENTS markers
and redirected managed paths require explicit repair, even with --force.
The manifest records kat's version and content hashes (no timestamps); a version
or manifest change also requires a deliberate forced update. Bootstrap is not
journaled in NAME_REPORT.md, including inside existing KiCad projects.

## Example

```sh
python tests/e2e/tia_design.py v1 v1.net                  # SKiDL -> netlist
kat new tia/tia.kicad_sch
kat sync tia/tia.kicad_sch v1.net                          # parts + connectivity
kat snapshot tia/tia.kicad_sch --slug import --expect v1.net
python tests/e2e/tia_design.py v2 v2.net                  # design change
kat sync tia/tia.kicad_sch v2.net                          # arrangement kept, changes reported
kat batch-exec tests/e2e/arrange_v2.py -- tia/tia.kicad_sch   # arrangement pass, journaled
kat snapshot tia/tia.kicad_sch --slug arrange --expect v2.net
python tests/e2e/tia_design.py v3 v3.net                  # + footprints and part numbers
kat sync tia/tia.kicad_sch v3.net
kat ready tia/tia.kicad_sch --expect v3.net                # gate before the PCB layout
kat pcb sync tia/tia.kicad_pcb                             # board: parts + nets, parity-checked
P=tests/e2e/tia_passes
kat batch-exec $P/setup.py -- tia/tia.kicad_pcb            # layout passes (Python), journaled
kat batch-exec $P/place1.py -- tia/tia.kicad_pcb
kat batch-exec $P/structure1.py -- tia/tia.kicad_pcb
kat pcb route tia/tia.kicad_pcb                            # Freerouting, then DRC
kat batch-exec $P/finish.py -- tia/tia.kicad_pcb
kat pcb refill tia/tia.kicad_pcb
kat pcb fab tia/tia.kicad_pcb && kat pcb ready tia/tia.kicad_pcb
kat comment --kind milestone --project tia "Rev A ready."   # the agent's own note
kat open tia/tia.kicad_pcb --verify                        # KiCad loads the same board
less tia/tia_REPORT.md                                     # the whole story, step by step
```

`tests/test_e2e_pcb_tia.py` runs the board flow to fabrication files and back through a
design change (R1 → 0805, an added capacitor), driven like an agent, and checks the design
report it leaves behind. `tests/test_e2e_skidl.py` runs the full iterative flow (import, design change, re-sync,
arrangement, re-sync); `tests/test_e2e_led_matrix.py` (marker `slow`, about 75 s) does the same
for a 200-part LED matrix on A2. Set `KAT_E2E_OUT=dir` to keep their snapshots for
inspection; `pytest -m 'not slow'` skips the large one.

## Design decisions

- **SKiDL for connectivity, not for drawing** — its own schematic generator is not readable
  for real circuits. atopile was not chosen: it has no schematic at all.
- **SKiDL defines the circuit** — parts and connections exist only in the design code; its
  netlist is the reference.
- **KiCad reads the drawing** — whether the file loads, which nets the drawing actually forms
  and what ERC reports is always determined by `kicad-cli`, never by `kat`'s own geometry
  model. `check --expect` compares the two. Every save goes through `kicad-cli sch upgrade`.
- **Lossless editing** — `kat.sexpr` keeps every token. kiutils drops KiCad 8+ syntax;
  kicad-skip preserves tokens but has not been released since 2024 and lacks the geometry
  this needs.
