"""Read ODB++ jobs: what a legacy board's fabrication data says about its pins.

A job is a directory or an archive (.zip, .tgz, .tar.gz, .tar), read in memory and never
unpacked, because such data comes from elsewhere. Files may be plain or compressed one by one
(``.z`` from UNIX compress, ``.gz``). Coordinates in ODB++ have y up; kat's have y down, as
KiCad's export writes them, so a board exported by KiCad reads back at its own coordinates.
"""

from __future__ import annotations

import gzip
import tarfile
import zipfile
from pathlib import Path

from .. import env

NO_NET = "$NONE$"
_COMPRESSED = (".z", ".gz", ".gzip")
TO_MM = {"MM": 1.0, "INCH": 25.4}


def _unlzw(data: bytes) -> bytes:
    """Expand UNIX ``compress`` (.Z) data, which Python's standard library cannot."""
    if data[:2] != b"\x1f\x9d" or len(data) < 3:
        raise env.KatError("not UNIX compress data")
    limit, block = data[2] & 0x1f, bool(data[2] & 0x80)
    if not 9 <= limit <= 16:
        raise env.KatError(f"UNIX compress data with {limit} bit codes")
    fresh = [bytes([i]) for i in range(256)] + ([b""] if block else [])
    table, width, previous = list(fresh), 9, b""
    pos, mark, total = 24, 24, len(data) * 8
    out = []

    def aligned() -> int:
        """Codes come in groups of eight: a change of width skips to the group's end."""
        group = 8 * width
        return mark + ((pos - mark + group - 1) // group) * group

    while True:
        if len(table) >= (1 << width) and width < limit:
            pos = mark = aligned()
            width += 1
        if pos + width > total:
            break
        first, used = pos // 8, pos % 8
        code = (int.from_bytes(data[first:first + 4], "little") >> used) & ((1 << width) - 1)
        pos += width
        if block and code == 256:
            pos = mark = aligned()
            table, width, previous = list(fresh), 9, b""
            continue
        if code < len(table):
            entry = table[code]
        elif code == len(table) and previous:
            entry = previous + previous[:1]
        else:
            raise env.KatError("corrupt UNIX compress data")
        out.append(entry)
        if previous and len(table) < (1 << limit):
            table.append(previous + entry[:1])
        previous = entry
    return b"".join(out)


class Job:
    """The files of a job by lower-case path below its root (the folder holding ``matrix``),
    read on demand. ``path``: a directory or an archive."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._tar = None
        files = self._files()
        roots = {name[:-len("matrix/matrix")] for name in map(self._plain, files)
                 if name == "matrix/matrix" or name.endswith("/matrix/matrix")}
        if not roots:
            raise env.KatError(f"{self.path.name} is not an ODB++ job (no matrix/matrix)")
        root = min(roots, key=len)
        self.files = {name[len(root):]: read for name, read in files.items() if name.startswith(root)}

    @staticmethod
    def _plain(name: str) -> str:
        for ext in _COMPRESSED:
            if name.endswith(ext):
                return name[:-len(ext)]
        return name

    def _files(self) -> dict:
        p = self.path
        if p.is_dir():
            return {f.relative_to(p).as_posix().lower(): f.read_bytes for f in p.rglob("*") if f.is_file()}
        if not p.is_file():
            raise env.KatError(f"{p} does not exist")
        if zipfile.is_zipfile(p):
            z = zipfile.ZipFile(p)
            return {i.filename.lower(): (lambda n=i.filename: z.read(n)) for i in z.infolist() if not i.is_dir()}
        if tarfile.is_tarfile(p):
            t = self._tar = tarfile.open(p)
            return {m.name.lower().removeprefix("./"): (lambda m=m: t.extractfile(m).read())
                    for m in t.getmembers() if m.isfile()}
        raise env.KatError(f"{p.name}: give a directory, .zip, .tgz, .tar.gz or .tar")

    def read(self, name: str) -> str | None:
        """Text of a file (``matrix/matrix``), expanded; None if the job has none."""
        name = name.lower()
        for ext in ("", *_COMPRESSED):
            read = self.files.get(name + ext)
            if read is None:
                continue
            data = read()
            if data[:2] == b"\x1f\x9d":
                data = _unlzw(data)
            elif data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="backslashreplace")
        return None

    def steps(self) -> list[str]:
        """The job's steps (``pcb``, ``panel``, ...)."""
        return sorted({n.split("/")[1] for n in self.files if n.startswith("steps/") and n.count("/") >= 2})

    def step(self, step: str | None, needs: str) -> str:
        """``step``, or the only step that has the file ``needs`` (below the step's folder)."""
        names = self.steps()
        if step is not None:
            if step.lower() not in names:
                raise env.KatError(f"no step {step!r} in {self.path.name}: {', '.join(names)}")
            return step.lower()
        have = [s for s in names if self.read(f"steps/{s}/{needs}") is not None]
        if len(have) != 1:
            raise env.KatError(f"{self.path.name} has {len(have)} steps with {needs} "
                               f"({', '.join(have or names)}): name one")
        return have[0]

    def layers(self) -> list[dict]:
        """Layers of the matrix: {name, type, context, start, end}, names in lower case."""
        out, block = [], None
        for line in (self.read("matrix/matrix") or "").splitlines():
            line = line.strip()
            if line.upper().startswith("LAYER") and line.endswith("{"):
                block = {}
            elif line.startswith("}"):
                if block is not None:
                    out.append({"name": block.get("NAME", "").lower(), "type": block.get("TYPE", "").upper(),
                                "context": block.get("CONTEXT", "").upper(),
                                "start": block.get("START_NAME", "").lower(), "end": block.get("END_NAME", "").lower()})
                block = None
            elif block is not None and "=" in line:
                k, v = line.split("=", 1)
                block[k.strip().upper()] = v.strip()
        return out

    def close(self) -> None:
        """Close an open tar archive."""
        if self._tar is not None:
            self._tar.close()


def units_of(text: str, name: str) -> float:
    """Millimetres per unit of a file: ``UNITS=MM``, ``U INCH`` (inches unless it says)."""
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith(("UNITS=", "UNITS ", "U ")):
            unit = line.replace("=", " ").split()[-1].upper()
            if unit not in TO_MM:
                raise env.KatError(f"{name}: unknown units {unit!r}")
            return TO_MM[unit]
        if line and not line.startswith("#") and line.split()[0] in ("CMP", "NET", "F", "P", "L", "S"):
            break
    return TO_MM["INCH"]


def _net_names(text: str) -> list[str]:
    """Net names in the order of their records: components refer to them by this index."""
    return [line.split(";")[0].split()[1] for line in text.splitlines()
            if line.startswith("NET ") and len(line.split(";")[0].split()) > 1]


def _packages(text: str) -> list[str]:
    return [line.split()[1] for line in text.splitlines() if line.startswith("PKG ") and len(line.split()) > 1]


def _components(text: str, name: str, nets: list[str], packages: list[str], side: str) -> dict:
    scale = units_of(text, name)
    comps: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("CMP "):
            f = line.split(";")[0].split()
            if len(f) < 7:
                raise env.KatError(f"{name}: short record {line!r}")
            pkg = int(f[1])
            current = comps.setdefault(f[6], {
                "part": f[7] if len(f) > 7 else "", "package": packages[pkg] if 0 <= pkg < len(packages) else "",
                "side": side, "x": round(float(f[2]) * scale, 6), "y": round(-float(f[3]) * scale, 6),
                "rot": float(f[4]), "properties": {}, "pins": {}})
        elif line.startswith("PRP ") and current is not None:
            key, _, value = line[4:].partition(" ")
            current["properties"][key] = value.strip().rsplit("'", 1)[0].lstrip("'") if "'" in value else value.strip()
        elif line.startswith("TOP ") and current is not None:
            f = line.split(";")[0].split()
            if len(f) < 8:
                raise env.KatError(f"{name}: short record {line!r}")
            pin = f[8] if len(f) > 8 else str(int(f[1]) + 1)
            k = int(f[6])
            net = nets[k] if 0 <= k < len(nets) else ""
            if k >= len(nets):
                raise env.KatError(f"{name}: pin {pin} refers to net {k}, but the job has {len(nets)}")
            net = "" if net == NO_NET else net
            # one number on several pads: a connected one speaks for all
            if net or pin not in current["pins"]:
                current["pins"][pin] = net
    return comps


def pinout(job: Path | str, step: str | None = None, refs: list[str] | None = None) -> dict:
    """Pin-to-net mapping of a job's components, from its component layers and EDA data.
    ``step``: which one (needed when several have EDA data); ``refs``: only these. Returns job,
    step, components {ref: {part, package, side, x, y (mm, y down), rot (degrees, as the job
    says), properties, pins {pin: net, "" when on none}}} and nets {name: ["J1.3", ...]}."""
    j = Job(job)
    try:
        s = j.step(step, "eda/data")
        eda = j.read(f"steps/{s}/eda/data")
        if eda is None:
            raise env.KatError(f"step {s} of {j.path.name} has no EDA data")
        nets, packages = _net_names(eda), _packages(eda)
        layers = [l["name"] for l in j.layers() if l["type"] == "COMPONENT"] or ["comp_+_top", "comp_+_bot"]
        comps: dict[str, dict] = {}
        for layer in layers:
            name = f"steps/{s}/layers/{layer}/components"
            text = j.read(name)
            if text is not None:
                side = "bottom" if "bot" in layer else "top" if "top" in layer else layer
                comps.update(_components(text, name, nets, packages, side))
    finally:
        j.close()
    if refs:
        unknown = sorted(set(refs) - set(comps))
        if unknown:
            raise env.KatError(f"not in {Path(job).name}: {', '.join(unknown)}")
        comps = {r: comps[r] for r in refs}
    by_net: dict[str, list[str]] = {}
    for ref, c in comps.items():
        for pin, net in c["pins"].items():
            if net:
                by_net.setdefault(net, []).append(f"{ref}.{pin}")
    return {"job": str(job), "step": s, "components": comps, "nets": {k: sorted(v) for k, v in sorted(by_net.items())}}
