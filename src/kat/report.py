"""Design journal: ``<NAME>_REPORT.md`` in the project root, one numbered entry per kat
command, batch script, comment or snapshot, so a design can be reviewed step by step.

Entries are appended (never rewritten) under a file lock. Images are copied into
``<NAME>_REPORT/<NNN>_<slug>/`` next to the report and linked relatively.

- ``kat`` CLI commands on a project: ``command`` entries (``cli.main`` wraps them).
- ``kat batch-exec FILE.py`` / ``kat.batch_exec``: a ``batch`` entry with the script's
  original source; kat calls inside the script add no entries of their own, only their
  snapshot/view images and comments go into the batch entry.
- ``kat comment`` / ``kat.comment``: markdown from the agent (plan, milestone, problem, note).
- ``kat.snapshot`` / ``kat.pcb.snapshot`` / ``view`` called from plain Python: an entry of
  their own with the images.

``KAT_REPORT=0`` (or ``kat --no-report``) turns journaling off.
"""

from __future__ import annotations

import ast
import datetime as dt
import fcntl
import io
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import env

KINDS = ("plan", "milestone", "problem", "note")
JSON_LINES = 150          # longer results are cut in the report
OUTPUT_LINES = 80         # captured script output kept in a batch entry
_HEADING = re.compile(r"^## (\d+) · ", re.M)

_current: "Entry | None" = None     # the command or batch entry being recorded
_disabled = False                   # kat --no-report


def enabled() -> bool:
    return not _disabled and os.environ.get("KAT_REPORT", "1") != "0"


# --- project and file ----------------------------------------------------------------

def project_of(path: Path | str | None) -> tuple[Path, str] | None:
    """(root directory, project name) for a project file or directory: the directory of
    the ``.kicad_pro`` with the file's name, else the only ``.kicad_pro`` in the file's
    directory or its parents; a file without project uses its own directory and name."""
    if path is None:
        return None
    p = Path(path).resolve()
    start = p if p.is_dir() else p.parent
    if not p.is_dir() and (start / f"{p.stem}.kicad_pro").exists():
        return start, p.stem
    for d in (start, *start.parents):
        pros = sorted(d.glob("*.kicad_pro"))
        if len(pros) == 1:
            return d, pros[0].stem
        if len(pros) > 1 or d == Path.home() or (d / ".git").exists():
            break
    if p.suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro"):
        return start, p.stem
    return None


def find_project(hint: Path | str | None = None) -> tuple[Path, str] | None:
    """``hint`` (a file or directory), else ``KAT_PROJECT``, else the working directory,
    its parents, or its only project subdirectory."""
    for cand in (hint, os.environ.get("KAT_PROJECT")):
        if cand and (found := project_of(cand)):
            return found
    cwd = Path.cwd().resolve()
    here = sorted(cwd.glob("*.kicad_pro"))
    if len(here) == 1:
        return cwd, here[0].stem
    subs = sorted(cwd.glob("*/*.kicad_pro"))
    if len(subs) == 1:
        return subs[0].parent.resolve(), subs[0].stem
    if not here and not subs:
        return project_of(cwd)
    return None


def path_for(root: Path, name: str) -> Path:
    return root / f"{name}_REPORT.md"


def _kicad_version() -> str:
    try:
        return env.run_cli("version").stdout.strip()
    except (env.KatError, OSError, subprocess.SubprocessError):
        return "?"


def ensure(root: Path, name: str) -> Path:
    """Create the report with its header if it does not exist yet."""
    report = path_for(root, name)
    if not report.exists():
        from importlib.metadata import PackageNotFoundError, version
        try:
            kat_version = version("kat")
        except PackageNotFoundError:
            kat_version = "?"
        report.write_text(
            f"# {name}: design report\n\n"
            f"Started {dt.datetime.now():%Y-%m-%d %H:%M} · kat {kat_version} · "
            f"KiCad {_kicad_version()}\n\n"
            "Written by kat: every command on this project, every batch script (with its code),\n"
            "the agent's comments and the snapshot images, in the order they happened.\n",
            encoding="utf-8")
    return report


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40] or "img"


def append(root: Path, name: str, kind: str, body: str,
           images: list[tuple[str, Path]] = (), slug: str = "") -> int:
    """Append entry N (the next number) and return N. ``images`` (label, file) are copied
    to ``<NAME>_REPORT/<NNN>_<slug>/`` and embedded below ``body``."""
    report = ensure(root, name)
    with open(report, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        nums = [int(n) for n in _HEADING.findall(fh.read())]
        n = (max(nums) if nums else 0) + 1
        text = f"\n## {n} · {dt.datetime.now():%Y-%m-%d %H:%M:%S} · {kind}\n\n{body.rstrip()}\n"
        existing = [(label, Path(p)) for label, p in images if p and Path(p).exists()]
        if existing:
            folder = root / f"{name}_REPORT" / f"{n:03d}_{_slug(slug or kind)}"
            folder.mkdir(parents=True, exist_ok=True)
            links = []
            for label, src in existing:
                dst = folder / src.name
                if dst.exists():
                    dst = folder / f"{_slug(label)}_{src.name}"
                shutil.copy(src, dst)
                links.append(f"![{label}]({dst.relative_to(root).as_posix()})")
            text += "\n" + "\n".join(links) + "\n"
        fh.seek(0, os.SEEK_END)
        fh.write(text)
        fh.flush()
        fcntl.flock(fh, fcntl.LOCK_UN)
    return n


# --- rendering results -------------------------------------------------------------------

SUMMARY_FIRST = ("ok", "clean", "ready", "match", "netlist_match", "parity", "unconnected",
                 "islands", "drc_errors", "drc_warnings", "erc_errors", "erc_warnings", "errors",
                 "warnings", "added", "updated", "replaced", "removed", "renamed", "status")


def _short(v, root: Path | None) -> str | None:
    if isinstance(v, bool) or v is None:
        return json.dumps(v)
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, str):
        if root and v.startswith(str(root)):
            v = os.path.relpath(v, root)
        return (f"`{v}`" if v else '""') if len(v) <= 80 else None
    if isinstance(v, (list, tuple)):
        if not v:
            return "none"
        if all(isinstance(x, (int, float)) for x in v) and len(v) <= 4:
            return "[" + ", ".join(_short(x, root) for x in v) + "]"
        return f"{len(v)} items" if v else "none"
    if isinstance(v, dict):
        if v and len(v) <= 8 and all(isinstance(x, (bool, int, float, str)) or x is None for x in v.values()):
            return ", ".join(f"{k} {_short(x, root)}" for k, x in v.items())
        return None
    return None


def summarize(result, root: Path | None = None, limit: int = 14) -> list[str]:
    """Key lines of a JSON result: well-known fields first, then other short scalars and
    list lengths; a nested ``check`` is summarised with a ``check.`` prefix."""
    if not isinstance(result, dict):
        return [f"{len(result)} items"] if isinstance(result, list) else []
    keys = [k for k in SUMMARY_FIRST if k in result] + [k for k in result if k not in SUMMARY_FIRST]
    lines = []
    for k in keys:
        v = result[k]
        if k == "check" and isinstance(v, dict):
            lines += [f"check.{line}" for line in summarize(v, root, limit=8)]
            continue
        s = _short(v, root)
        if s is not None:
            lines.append(f"{k}: {s}")
        elif isinstance(v, dict):                  # one level down: its short fields
            inner = [f"{k2} {s2}" for k2, v2 in v.items()
                     if not isinstance(v2, dict) and (s2 := _short(v2, root)) is not None]
            if inner:
                lines.append(f"{k}: " + ", ".join(inner[:6]))
    return lines[:limit] + ([f"... ({len(lines) - limit} more fields in the result)"]
                            if len(lines) > limit else [])


def _json_block(result, root: Path | None) -> str:
    text = json.dumps(env.tidy(result), indent=1, default=list)
    if root:
        text = text.replace(str(root) + "/", "")
    lines = text.splitlines()
    if len(lines) > JSON_LINES:
        lines = lines[:JSON_LINES] + [f"... cut: {len(lines) - JSON_LINES} more lines"]
    body = "\n".join(lines)
    return f"<details><summary>result</summary>\n\n```json\n{body}\n```\n</details>"


def _images_of(result: dict) -> list[tuple[str, Path]]:
    """Overview pictures of a snapshot or view result (not tiles or finding crops)."""
    out: list[tuple[str, Path]] = []
    imgs = result.get("images")
    if isinstance(imgs, dict):                     # board snapshot: set name -> {png}
        out += [(k, Path(v["png"])) for k, v in imgs.items() if isinstance(v, dict) and v.get("png")]
    elif isinstance(imgs, list):                   # schematic snapshot: [{png}]
        out += [(Path(v["png"]).stem, Path(v["png"])) for v in imgs if isinstance(v, dict) and v.get("png")]
    out += [(Path(p).stem, Path(p)) for p in result.get("3d") or [] if "top" in Path(p).stem]
    if result.get("png") and not imgs:            # a view
        out.append(("view", Path(result["png"])))
    return out


# --- entries --------------------------------------------------------------------------------

class Entry:
    """A command or batch entry being recorded; written once at the end."""

    def __init__(self, kind: str, hint: Path | str | None = None):
        self.kind, self.hint = kind, hint
        self.touched: list[Path] = []
        self.parts: list[str] = []
        self.images: list[tuple[str, Path]] = []
        self.slug = ""

    def project(self) -> tuple[Path, str] | None:
        if self.hint and (found := project_of(self.hint)):
            return found
        if os.environ.get("KAT_PROJECT"):
            return find_project()
        for p in self.touched:
            if found := project_of(p):
                return found
        return find_project()

    def write(self, body: str) -> int | None:
        found = self.project()
        if found is None:
            return None
        root, name = found
        extra = "\n\n".join(self.parts)
        return append(root, name, self.kind, body + ("\n\n" + extra if extra else ""),
                      self.images, self.slug)


def touch(path: Path | str) -> None:
    """Record a file a running command or script works on (to find its project)."""
    if _current is not None:
        _current.touched.append(Path(path))


def snapshot_images(path: Path | str, result: dict, call: str, slug: str = "") -> None:
    """Journal a snapshot or view made through the library: into the running command or
    batch entry, else as an entry of its own."""
    if not enabled():
        return
    images = _images_of(result)
    if _current is not None:
        _current.touched.append(Path(path))
        _current.images += images
        _current.slug = _current.slug or slug
        return
    found = project_of(path)
    if found is None:
        return
    root = found[0]
    call = call.replace(str(Path(path).resolve().parent) + "/", "").replace(str(root) + "/", "")
    body = f"`{call}`\n\n" + "\n".join(f"- {line}" for line in summarize(result, root))
    append(root, found[1], "snapshot" if "snapshot" in call else "view", body, images, slug)


def comment(text: str, kind: str = "note", project: Path | str | None = None) -> dict:
    """Add the agent's markdown ``text`` to the report: ``kind`` plan (what comes next),
    milestone (what was reached), problem (what failed and needs another iteration) or
    note. ``project`` is a project file or directory (default: the running batch's project,
    ``KAT_PROJECT``, or the one in the working directory). Inside a batch script the
    comment goes into the batch entry. Returns ``report`` and ``entry``."""
    if kind not in KINDS:
        raise env.KatError(f"kind one of {', '.join(KINDS)}")
    if not text.strip():
        raise env.KatError("empty comment")
    if _current is not None and _current.kind == "batch" and project is None:
        _current.parts.append(f"**{kind}:** {text.strip()}")
        return {"report": None, "entry": "in the running batch entry"}
    found = find_project(project)
    if found is None:
        raise env.KatError("no project found: give --project DIR or set KAT_PROJECT")
    root, name = found
    n = append(root, name, kind, text.strip()) if enabled() else None
    return {"report": str(path_for(root, name)), "entry": n}


@contextmanager
def command(argv: list[str], hint: Path | str | None = None) -> Iterator[Entry]:
    """Record one CLI command: set ``entry.result`` / ``entry.exit`` inside the block."""
    global _current
    if not enabled() or _current is not None:
        yield Entry("command")
        return
    e = _current = Entry("command", hint)
    e.result, e.exit = None, 0
    t0 = time.time()
    try:
        yield e
    finally:
        _current = None
        found = e.project()
        if found is not None:
            root = found[0]
            line = shlex.join(argv).replace(str(root) + "/", "")
            head = f"`kat {line}` · {time.time() - t0:.1f} s · exit {e.exit}"
            lines = summarize(e.result, root)
            body = head + ("\n\n" + "\n".join(f"- {l}" for l in lines) if lines else "")
            if e.result is not None:
                body += "\n\n" + _json_block(e.result, root)
            e.write(body)


class _Tee(io.TextIOBase):
    def __init__(self, stream):
        self.stream, self.buf = stream, io.StringIO()

    def write(self, s):
        self.buf.write(s)
        return self.stream.write(s)

    def flush(self):
        self.stream.flush()


def batch_exec(file: Path | str, project: Path | str | None = None, argv: list[str] = ()) -> dict:
    """Run a Python pass (``runpy``, as ``__main__``) and journal it: its docstring as the
    title, the original source, its output, the exception if it failed, and the images of
    snapshots and views it made. kat calls inside write no entries of their own. The
    project is ``project``, else ``KAT_PROJECT``, else found from the files the script
    edits or snapshots, else the working directory. Returns ``file``, ``seconds``, ``ok``,
    ``error``, ``report``, ``entry``."""
    global _current
    file = Path(file)
    if not file.is_file():
        raise env.KatError(f"{file}: no such script")
    source = file.read_text(encoding="utf-8")
    e = Entry("batch", project)
    outer, _current = _current, e
    out, err = _Tee(sys.stdout), _Tee(sys.stderr)
    old_argv, sys.argv = sys.argv, [str(file), *argv]
    old_out, old_err, sys.stdout, sys.stderr = sys.stdout, sys.stderr, out, err
    t0, error, tb = time.time(), None, ""
    try:
        runpy.run_path(str(file), run_name="__main__")
    except SystemExit as x:
        if x.code not in (None, 0):
            error = f"SystemExit({x.code!r})"
    except BaseException as x:      # noqa: BLE001 - the report must show any failure
        if isinstance(x, KeyboardInterrupt):
            raise
        error = f"{type(x).__name__}: {x}"
        frames = traceback.extract_tb(x.__traceback__)
        mine = next((i for i, f in enumerate(frames) if Path(f.filename).resolve() == file.resolve()), 0)
        tb = "".join(["Traceback (most recent call last):\n"] + traceback.format_list(frames[mine:])
                     + traceback.format_exception_only(x))
    finally:
        sys.stdout, sys.stderr, sys.argv = old_out, old_err, old_argv
        _current = outer
    seconds = round(time.time() - t0, 1)
    res = {"file": str(file), "seconds": seconds, "ok": error is None, "error": error,
           "report": None, "entry": None}
    if not enabled():
        return res
    try:
        doc = ast.get_docstring(ast.parse(source)) or ""
    except SyntaxError:
        doc = ""
    title = doc.splitlines()[0] if doc else ""
    e.slug = e.slug or file.stem
    found = e.project()
    head = (f"`kat batch-exec {shlex.join([str(file), *argv])}` · {seconds} s · "
            f"{'ok' if error is None else 'FAILED (the edit block that raised saved nothing)'}")
    body = (f"**{title}**\n\n" if title else "") + head + f"\n\n```python\n{source.rstrip()}\n```"
    output = (out.buf.getvalue() + err.buf.getvalue()).strip()
    if output:
        lines = output.splitlines()
        if len(lines) > OUTPUT_LINES:
            lines = lines[:OUTPUT_LINES // 2] + [f"... {len(lines) - OUTPUT_LINES} lines cut ..."] + \
                lines[-OUTPUT_LINES // 2:]
        body += "\n\nOutput:\n\n```text\n" + "\n".join(lines) + "\n```"
    if error:
        body += f"\n\n**Error:** `{error}`\n\n```text\n{tb.strip()}\n```"
    n = e.write(body)
    if found:
        res.update(report=str(path_for(*found)), entry=n)
    return res
