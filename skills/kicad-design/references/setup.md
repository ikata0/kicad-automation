# Environment setup

Read this before starting a design or when tools/libraries cannot be found.
`kat init` only deploys instructions: it does not install software or create a
KiCad project. Use `kat new DIR/NAME.kicad_sch` for the latter. Bootstrap records
provenance in .agents/kat-init.json, not the design journal. Reruns leave identical
files unchanged; differing managed content requires an explicit --force update.

Use Python 3.10+ and KiCad 10 with symbol/footprint libraries. Install the supplied
kat wheel with the `dev` extra for SKiDL; also use `render` without rsvg-convert,
and `gui` for board editor verification. For example:

```text
python -m pip install "./kat-0.1.0-py3-none-any.whl[dev,render]"
```

Use the actual supplied wheel path/version, not an unrelated package from an
index. A virtual environment is recommended. On Windows, direct invocations of
the environment's Scripts executables avoid activation execution-policy issues.

Kat resolves `KICAD_CLI`, then PATH, then registered/default Windows KiCad 10
installations or the macOS application bundle. Detection is cached within the
kat process and inherited by children, not written to the calling shell.
Standalone SKiDL needs `KICAD10_SYMBOL_DIR` before it imports skidl.

On Windows, use English in KiCad's language preferences: translated item
descriptions can break environment-part checks, and LANG=C is not honored.
Open both editors once, accept default global library tables, and close them.
Run this using the same Python environment that contains kat:

```powershell
$env:KICAD_CLI = & python -c "from kat.env import kicad_cli; print(kicad_cli())"
if ($LASTEXITCODE -ne 0) { throw 'KiCad discovery failed' }
$kicadBin = Split-Path $env:KICAD_CLI
$env:Path = "$kicadBin;$env:Path"
$env:KICAD10_SYMBOL_DIR = Join-Path (Split-Path $kicadBin) 'share\kicad\symbols'
```

Repeat those variables in new shells for standalone SKiDL. The automation
repository's pytest suite discovers KiCad and libraries automatically.
Kat explicitly uses strict UTF-8 for KiCad files and associated metadata, reports,
notes and exports, independent of Python's OS-default encoding: `PYTHONUTF8` is
not needed for kat's file handling. Invalid UTF-8 is an error, not silently
replaced. Terminal and third-party tool encodings remain unchanged.
Captured kicad-cli diagnostics use the Windows system ANSI code page on Windows,
UTF-8 elsewhere. Windows KiCad can truncate diagnostics containing characters
outside that code page; this does not change the UTF-8 file format.
Keep SKiDL's output files on the same drive as its source script:
cross-drive relative paths can fail. The automation repository's pytest suite
chooses a same-drive temporary parent automatically on Windows when needed.
An explicit `--basetemp` is still supported, but pytest clears that directory.

On Linux with stock libraries, SKiDL commonly uses
`KICAD10_SYMBOL_DIR=/usr/share/kicad/symbols`; adjust for the actual installation.
Use English diagnostics and initialize the library tables there too.

Routing needs Java 21+ and Freerouting: `kat pcb route --install` downloads the
pinned release into kat's cache. Board `kat open --verify` needs the gui extra
and a display. An unavailable verification result is not a passed verification.
For API details, use `kat api`; for the workflow, return to
[kicad-design](../SKILL.md).
