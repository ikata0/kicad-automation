"""Large design: 10x10 LED matrix (200 parts, two 64-pin connectors) on an A2 sheet.

Import, two arrangement passes, checks. Guards the speed of the library (the same pass
took 193 s through `kat batch` before the geometry indexing) and the arrangement result.
Set KAT_E2E_OUT=/some/dir to keep the project and its snapshots for review.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import kat

HERE = Path(__file__).parent / "e2e"
sys.path.insert(0, str(HERE))
import led_matrix_arrange as arrange  # noqa: E402

pytestmark = [pytest.mark.slow, pytest.mark.skipif(
    shutil.which("kicad-cli") is None or importlib.util.find_spec("skidl") is None,
    reason="needs kicad-cli and skidl")]


def design(out: Path, *variant: str) -> Path:
    net = out / ("led_matrix.net" if not variant else f"led_matrix_{variant[0]}.net")
    subprocess.run([sys.executable, str(HERE / "led_matrix.py"), str(net), *variant],
                   check=True, capture_output=True, cwd=out)
    return net


def assert_clean(check: dict) -> None:
    assert check["erc"]["errors"] == 0 and check["erc"]["warnings"] == 0, check["erc"]
    assert check["netlist_vs_expected"]["match"], check["netlist_vs_expected"]
    for k in ("overlaps", "body_overlaps", "off_page"):
        assert not check[k], (k, check[k][:5])
    assert check["clean"]


@pytest.fixture
def root(tmp_path) -> Path:
    root = Path(os.environ["KAT_E2E_OUT"]) / "led_matrix" if os.environ.get("KAT_E2E_OUT") else tmp_path
    if root.exists() and root != tmp_path:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_led_matrix(root):
    sch = root / "led_matrix" / "led_matrix.kicad_sch"
    net = design(root)
    kat.new(sch, paper="A2")
    report = kat.sync(sch, net)
    assert len(report["added"]) == 202 and report["check"]["ok"]
    kat.snapshot(sch, slug="import", expect=net)

    t0 = time.time()
    with kat.edit(sch) as s:
        arrange.pass1(s)
    with kat.edit(sch) as s:
        arrange.pass2(s)
    elapsed = time.time() - t0
    assert elapsed < 60, f"arrangement took {elapsed:.0f} s"
    snap = kat.snapshot(sch, slug="arranged", expect=net, note="pass 1 + 2")
    assert_clean(kat.check(sch, net))
    assert Path(snap["images"][0]["png"]).stat().st_size > 10000

    # the arrangement survives a further sync of the unchanged design
    text = sch.read_text()
    assert not kat.sync(sch, net)["changed"]
    assert sch.read_text() == text
