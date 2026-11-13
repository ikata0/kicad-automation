"""Reading ODB++ jobs: checked against KiCad's own export of a board, whose pads give the answer."""

import gzip
import io
import json
import shutil
import subprocess
import tarfile
import zipfile

import pytest

import kat
from kat import cli, env
from kat.pcb import odb
from test_pcb_placement import pcb  # noqa: F401 (fixture)
from test_sync import project  # noqa: F401 (fixture)

needs_kicad = pytest.mark.requires("kicad", "symbols", "footprints")

MATRIX = """STEP {
    COL=1
    NAME=PCB
}

LAYER {
    ROW=1
    CONTEXT=BOARD
    TYPE=COMPONENT
    NAME=COMP_+_TOP
    POLARITY=POSITIVE
}

LAYER {
    ROW=2
    CONTEXT=BOARD
    TYPE=COMPONENT
    NAME=COMP_+_BOT
    POLARITY=POSITIVE
}
"""
EDA = """HDR some cad
LYR top bottom
# NET 0
NET VCC;;ID=1
SNT TOP T 0 0
#NET 1
NET $NONE$
#NET 2
NET SIG_1
PKG CONN_2 1.27 0 0 1 1;;ID=2
PKG SO8 1.27 0 0 1 1;;ID=3
"""
TOP = """UNITS=INCH
#
# CMP 0
CMP 1 1 -2 90 N J1 MY_CONN ;;ID=5
PRP Value 'Header, 3 way'
TOP 0 1 -2 0 N 0 0 1
TOP 1 1 -2.1 0 N 2 0 2 ;;ID=6
TOP 2 1 -2.2 0 N 1 0 3
TOP 3 1 -2.3 0 N -1 -1 4
"""
BOT = """U MM
CMP 0 10 -20 0 M U1 ;
TOP 0 10 -20 0 M 2 0 A
TOP 1 10 -21 0 M 0 0 7
TOP 2 10 -22 0 M 1 0 7
"""
FILES = {"matrix/matrix": MATRIX, "misc/info": "UNITS=INCH\n", "steps/pcb/eda/data": EDA,
         "steps/pcb/layers/comp_+_top/components": TOP, "steps/pcb/layers/comp_+_bot/components": BOT}


def as_dir(root, files):
    for name, text in files.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text if isinstance(text, bytes) else text.encode())
    return root


def check_foreign(job):
    r = kat.pcb.odb_pinout(job)
    j, u = r["components"]["J1"], r["components"]["U1"]
    assert r["step"] == "pcb"
    assert j["pins"] == {"1": "VCC", "2": "SIG_1", "3": "", "4": ""}
    assert (j["x"], j["y"], j["side"], j["part"], j["package"]) == (25.4, 50.8, "top", "MY_CONN", "SO8")
    assert j["properties"]["Value"] == "Header, 3 way"
    assert (u["x"], u["y"], u["side"], u["package"]) == (10, 20, "bottom", "CONN_2")
    assert u["pins"] == {"A": "SIG_1", "7": "VCC"}, "a connected pad speaks for its number"
    assert r["nets"] == {"SIG_1": ["J1.2", "U1.A"], "VCC": ["J1.1", "U1.7"]}


def test_a_job_written_to_the_specification(tmp_path):
    check_foreign(as_dir(tmp_path / "job", FILES))


def test_upper_case_names_and_a_folder_in_a_zip(tmp_path):
    f = tmp_path / "j.zip"
    with zipfile.ZipFile(f, "w") as z:
        for name, text in FILES.items():
            z.writestr("Job_7/" + name.upper(), text)
        z.writestr("Other/readme.txt", "not part of it")
    check_foreign(f)


@pytest.mark.parametrize("mode, name", [("w:gz", "j.tgz"), ("w:gz", "j.tar.gz"), ("w", "j.tar")])
def test_tar_archives(tmp_path, mode, name):
    f = tmp_path / name
    with tarfile.open(f, mode) as t:
        for fname, text in FILES.items():
            info = tarfile.TarInfo("./job/" + fname)
            data = text.encode()
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    check_foreign(f)


def lzw(data: bytes, limit: int = 16) -> bytes:
    """A UNIX compress writer for the test; its output is proved readable by gzip."""
    out = bytearray(b"\x1f\x9d" + bytes([0x80 | limit]))
    acc = nbits = 0
    group = 0                                  # bits written in this group of 8 codes

    def put(code, width):
        nonlocal acc, nbits, group
        acc |= code << nbits
        nbits += width
        group += width
        while nbits >= 8:
            out.append(acc & 255)
            acc >>= 8
            nbits -= 8

    def pad(width):
        nonlocal acc, nbits, group
        while group % (8 * width):
            put(0, min(8, 8 * width - group % (8 * width)))
        group = 0

    table = {bytes([i]): i for i in range(256)}
    next_code, width, w = 257, 9, b""
    for b in data:
        c = bytes([b])
        if w + c in table:
            w += c
            continue
        put(table[w], width)
        if next_code < (1 << limit):
            table[w + c] = next_code
            next_code += 1
            if next_code > (1 << width) and width < limit:
                pad(width)
                width += 1
        else:                                  # table full: clear it, as compress may
            put(256, width)
            pad(width)
            table = {bytes([i]): i for i in range(256)}
            next_code, width = 257, 9
        w = c
    if w:
        put(table[w], width)
    if nbits:
        out.append(acc & 255)
    return bytes(out)


@pytest.mark.parametrize("limit, size", [(10, 9000), (12, 40000), (16, 300000)])
def test_unix_compress_roundtrip(limit, size):
    import random
    rnd = random.Random(limit)
    words = [bytes(rnd.randrange(32, 127) for _ in range(rnd.randrange(1, 12))) for _ in range(800)]
    data = b"".join(rnd.choice(words) for _ in range(size // 6))
    packed = lzw(data, limit)
    assert odb._unlzw(packed) == data
    if gzip_cli := shutil.which("gzip"):
        proc = subprocess.run([gzip_cli, "-dc"], input=packed, capture_output=True)
        assert proc.returncode == 0 and proc.stdout == data, proc.stderr


def test_corrupt_compress_data():
    with pytest.raises(kat.KatError, match="corrupt"):
        odb._unlzw(b"\x1f\x9d\x90" + b"\xff" * 20)
    with pytest.raises(kat.KatError, match="not UNIX"):
        odb._unlzw(b"abc")
    with pytest.raises(kat.KatError, match="bit codes"):
        odb._unlzw(b"\x1f\x9d\x98abc")


def test_files_compressed_one_by_one(tmp_path):
    files = {k: v for k, v in FILES.items() if "components" not in k and "eda" not in k}
    files["steps/pcb/eda/data.z"] = lzw(EDA.encode())
    files["steps/pcb/layers/comp_+_top/components.Z"] = lzw(TOP.encode())
    files["steps/pcb/layers/comp_+_bot/components.gz"] = gzip.compress(BOT.encode())
    check_foreign(as_dir(tmp_path / "job", files))


def test_steps_must_be_named_when_several_have_eda_data(tmp_path):
    files = dict(FILES, **{"steps/panel/eda/data": EDA, "steps/panel/layers/comp_+_top/components": TOP})
    job = as_dir(tmp_path / "job", files)
    with pytest.raises(kat.KatError, match="2 steps with eda/data"):
        kat.pcb.odb_pinout(job)
    assert kat.pcb.odb_pinout(job, step="PCB")["step"] == "pcb"
    with pytest.raises(kat.KatError, match="no step 'x'"):
        kat.pcb.odb_pinout(job, step="x")
    # a panel without EDA data is no reason to ask
    assert kat.pcb.odb_pinout(as_dir(tmp_path / "two", dict(FILES, **{"steps/panel/stephdr": "X\n"})))["step"] == "pcb"


@pytest.mark.parametrize("files, match", [
    ({"misc/info": "x"}, "not an ODB"),
    (dict(FILES, **{"steps/pcb/eda/data": "HDR\n"}), "refers to net 0, but the job has 0"),
    (dict(FILES, **{"steps/pcb/layers/comp_+_top/components": "UNITS=FURLONG\nCMP 0 0 0 0 N A B\n"}), "unknown units"),
    (dict(FILES, **{"steps/pcb/layers/comp_+_top/components": "UNITS=MM\nCMP 0 0\n"}), "short record"),
])
def test_bad_jobs_are_errors(tmp_path, files, match):
    with pytest.raises(kat.KatError, match=match):
        kat.pcb.odb_pinout(as_dir(tmp_path / "job", files))


def test_not_a_job_at_all(tmp_path):
    with pytest.raises(kat.KatError, match="does not exist"):
        kat.pcb.odb_pinout(tmp_path / "none.zip")
    (tmp_path / "f.bin").write_bytes(b"12345")
    with pytest.raises(kat.KatError, match="give a directory"):
        kat.pcb.odb_pinout(tmp_path / "f.bin")


def test_refs_tables_and_the_mapping_check(tmp_path, capsys):
    job = as_dir(tmp_path / "job", FILES)
    r = kat.pcb.odb_pinout(job, refs=["U1"])
    assert list(r["components"]) == ["U1"] and r["nets"] == {"SIG_1": ["U1.A"], "VCC": ["U1.7"]}
    with pytest.raises(kat.KatError, match="not in job: Z9"):
        kat.pcb.odb_pinout(job, refs=["U1", "Z9"])
    table = tmp_path / "pins.csv"
    assert cli.main(["pcb", "odb-pinout", str(job), "--out", str(table)]) == 0
    assert json.loads(capsys.readouterr().out)["out"] == str(table)
    assert table.read_text().splitlines()[:3] == ["ref,pin,net", "J1,1,VCC", "J1,2,SIG_1"]
    assert kat.mapping.check(job, table)["ok"]            # the job against its own table
    table.write_text("ref,pin,net\nJ1,1,SIG_1\n")
    assert kat.mapping.check(job, table)["counts"]["mismatched"] == 1
    whole = tmp_path / "a" / "pins.json"
    kat.pcb.odb_pinout(job, out=whole)
    assert json.loads(whole.read_text())["components"]["J1"]["part"] == "MY_CONN"


@needs_kicad
def test_kicad_export_gives_the_nets_of_the_pads(pcb, tmp_path):
    with kat.pcb.edit(pcb) as b:
        b.outline(rect=(0, 0, 40, 25))
        b.move_many({"J1": {"to": (4, 10)}, "Q1": {"to": (14, 12)}, "C1": {"to": (14, 6)},
                     "R1": {"to": (24, 8)}, "D1": {"to": (28, 8), "rot": 90},
                     "R2": {"to": (24, 16), "rot": 270}, "D2": {"to": (30, 16)}})
        b.flip("C1")
        b.flip("R2")
        b.mounting_hole((35, 5), "M3")
        want = {f.ref: {p.number: p.net for p in f.pads() if p.number} for f in b.footprints() if not f.board_only}
        where = {f.ref: (f.x, f.y, "bottom" if f.side == "B" else "top") for f in b.footprints() if not f.board_only}
    job = tmp_path / "job.zip"
    env.run_cli("pcb", "export", "odb", "--precision", "6", "-o", str(job), str(pcb))
    r = kat.pcb.odb_pinout(job)
    comps = r["components"]
    assert {ref: c for ref, c in comps.items() if ref in want}.keys() == want.keys(), comps.keys()
    for ref, pads in want.items():
        got = comps[ref]["pins"]
        assert got == {k: (v if not v.startswith("unconnected-(") else got[k]) for k, v in pads.items()}, ref
        c = comps[ref]
        assert c["side"] == where[ref][2], ref
        assert c["x"] == pytest.approx(where[ref][0], abs=1e-3) and c["y"] == pytest.approx(where[ref][1], abs=1e-3)
    # the job, used as the source of a mapping check, agrees with the board it came from
    table = tmp_path / "pins.csv"
    kat.pcb.odb_pinout(job, refs=sorted(want), out=table)
    for source in (pcb, job, pcb.with_suffix(".kicad_sch")):
        res = kat.mapping.check(source, table)
        assert res["ok"], (source, res)


@needs_kicad
def test_a_gzipped_tar_of_a_kicad_export(pcb, tmp_path):
    out = tmp_path / "plain.zip"
    env.run_cli("pcb", "export", "odb", "-o", str(out), str(pcb))
    t = tmp_path / "job.tgz"
    with zipfile.ZipFile(out) as z, tarfile.open(t, "w:gz") as tar:
        for i in z.infolist():
            if not i.is_dir():
                data = z.read(i.filename)
                info = tarfile.TarInfo(i.filename)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    assert kat.pcb.odb_pinout(t)["components"] == kat.pcb.odb_pinout(out)["components"]
