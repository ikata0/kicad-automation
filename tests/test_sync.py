"""kat sync against synthetic netlists (KiCad s-expression format, as SKiDL writes it)."""

import shutil
from pathlib import Path

import pytest

import kat
from kat import cli, netlist
from kat.schematic import Schematic

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")

# ref: (library, part, value, tag)
BASE_PARTS = {
    "J1": ("Connector_Generic", "Conn_01x02", "SUPPLY", "tag-j1"),
    "R1": ("Device", "R", "330", "tag-r1"),
    "D1": ("Device", "LED", "red", "tag-d1"),
}
BASE_NETS = {
    "VIN": [("J1", "1"), ("R1", "1")],
    "LED_A": [("R1", "2"), ("D1", "2")],
    "GND": [("D1", "1"), ("J1", "2")],
}


def write_netlist(path: Path, parts: dict, nets: dict, footprints: dict | None = None,
                  fields: dict | None = None) -> Path:
    comps = []
    for ref, (lib, part, value, tag) in parts.items():
        fp = (footprints or {}).get(ref, "")
        extra = "".join(f' (field (name "{k}") "{v}")' for k, v in (fields or {}).get(ref, {}).items())
        comps.append(f'(comp (ref "{ref}") (value "{value}") (footprint "{fp}") '
                      f'(fields (field (name "SKiDL Tag") "{tag}"){extra}) '
                      f'(libsource (lib "{lib}") (part "{part}")))')
    net_txt = []
    for i, (name, pins) in enumerate(nets.items(), 1):
        nodes = " ".join(f'(node (ref "{r}") (pin "{p}"))' for r, p in pins)
        net_txt.append(f'(net (code "{i}") (name "{name}") {nodes})')
    path.write_text(f'(export (version "E") (components {" ".join(comps)}) (nets {" ".join(net_txt)}))\n')
    return path


@pytest.fixture
def project(tmp_path):
    sch = tmp_path / "p" / "p.kicad_sch"
    assert cli.main(["new", str(sch)]) == 0
    return sch


def positions(sch: Path) -> dict:
    return {(s.ref, s.unit): (s.place.x, s.place.y) for s in Schematic(sch).symbols()
            if not s.ref.startswith("#")}


def sync_ok(sch, net) -> dict:
    report = kat.sync(sch, net)
    assert report["check"]["ok"], report["check"]
    assert not report["unresolved"]
    return report


def test_first_import_is_correct_clean_and_idempotent(project, tmp_path):
    net = write_netlist(tmp_path / "d.net", BASE_PARTS, BASE_NETS)
    report = sync_ok(project, net)
    assert {a["ref"] for a in report["added"]} == {"J1", "R1", "D1"}
    assert report["check"]["clean"], report["check"]
    assert report["check"]["erc"]["warnings"] == 0
    s = Schematic(project)
    assert {x.field("SKiDL Tag") for x in s.symbols() if not x.ref.startswith("#")} == \
        {"tag-j1", "tag-r1", "tag-d1"}
    again = kat.sync(project, net)
    assert not again["changed"], again


def test_power_flag_added_once(project, tmp_path):
    net = write_netlist(tmp_path / "d.net", BASE_PARTS, BASE_NETS)
    report = sync_ok(project, net)
    assert report["flags"] == ["GND"]
    sync_ok(project, net)
    flags = [x for x in Schematic(project).symbols() if x.lib_id == "power:PWR_FLAG"]
    assert len(flags) == 1



def test_stacked_power_pins_get_one_symbol(project, tmp_path):
    """B12: MAX14662's GND pins 11/25/29 sit on one spot. Added to a GND net that is
    already drawn with more pins, they get one GND symbol, not three."""
    caps = {f"C{i}": ("Device", "C", "100n", f"tag-c{i}") for i in range(1, 4)}
    gnd = BASE_NETS["GND"] + [(c, "2") for c in caps]
    nets = dict(BASE_NETS, GND=gnd, VIN=BASE_NETS["VIN"] + [(c, "1") for c in caps])
    parts = dict(BASE_PARTS, **caps)
    sync_ok(project, write_netlist(tmp_path / "a.net", parts, nets))

    def gnd_symbols():
        return len([x for x in Schematic(project).symbols() if x.lib_id == "power:GND"])
    before = gnd_symbols()
    parts["U1"] = ("Analog_Switch", "MAX14662", "MAX14662", "tag-u1")
    nets["GND"] = gnd + [("U1", "11"), ("U1", "25"), ("U1", "29")]
    kat.sync(project, write_netlist(tmp_path / "b.net", parts, nets))   # ERC: open inputs
    assert gnd_symbols() == before + 1

def test_rename_by_tag_keeps_position(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    before = positions(project)
    parts = dict(BASE_PARTS)
    parts["RLED"] = parts.pop("R1")
    nets = {k: [("RLED" if r == "R1" else r, p) for r, p in v] for k, v in BASE_NETS.items()}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert report["renamed"] == [{"from": "R1", "to": "RLED"}]
    assert not report["added"] and not report["removed"] and not report["attached"]
    after = positions(project)
    assert after[("RLED", 1)] == before[("R1", 1)]


def test_swapped_references_do_not_collide(project, tmp_path):
    parts = dict(BASE_PARTS, R2=("Device", "R", "1k", "tag-r2"))
    nets = dict(BASE_NETS, VIN=[("J1", "1"), ("R1", "1"), ("R2", "1")],
                LED_A=[("R1", "2"), ("R2", "2"), ("D1", "2")])
    sync_ok(project, write_netlist(tmp_path / "a.net", parts, nets))
    before = positions(project)
    swapped = dict(parts, R1=("Device", "R", "1k", "tag-r2"), R2=("Device", "R", "330", "tag-r1"))
    swap = {"R1": "R2", "R2": "R1"}
    nets2 = {k: [(swap.get(r, r), p) for r, p in v] for k, v in nets.items()}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", swapped, nets2))
    assert len(report["renamed"]) == 2
    assert positions(project)[("R1", 1)] == before[("R2", 1)]


def test_value_and_footprint_update(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    parts = dict(BASE_PARTS, R1=("Device", "R", "470", "tag-r1"))
    fps = {"R1": "Resistor_SMD:R_0603_1608Metric"}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, BASE_NETS, fps))
    fields = {(u["ref"], u["field"]): u["to"] for u in report["updated"]}
    assert fields == {("R1", "Value"): "470", ("R1", "Footprint"): "Resistor_SMD:R_0603_1608Metric"}
    r1 = Schematic(project).find_symbols("R1")[0]
    assert r1.field("Value") == "470"


def test_removed_part_takes_its_dangling_items_along(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    parts = {k: v for k, v in BASE_PARTS.items() if k != "D1"}
    nets = {"VIN": BASE_NETS["VIN"], "GND": [("J1", "2")]}
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert report["removed"] == ["D1"]
    s = Schematic(project)
    assert "D1" not in {x.ref for x in s.symbols()}
    # every remaining wire end touches a pin, label, or another wire
    assert s.prune_dangling() == 0
    assert report["check"]["erc"]["errors"] == 0


def test_pin_moved_to_another_net_is_rewired(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    parts = dict(BASE_PARTS, R2=("Device", "R", "10", "tag-r2"))
    nets = dict(BASE_NETS, GND=[("J1", "2"), ("R2", "2")], LED_K=[("D1", "1"), ("R2", "1")])
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert "D1.1" in report["detached"]
    assert {"pin": "D1.1", "net": "LED_K"} in report["attached"]


def test_net_rename_renames_labels(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    nets = dict(BASE_NETS)
    nets["VSUP"] = nets.pop("VIN")
    report = sync_ok(project, write_netlist(tmp_path / "b.net", BASE_PARTS, nets))
    assert report["named"] == [{"from": "VIN", "to": "VSUP"}]
    assert not report["detached"] and not report["attached"]
    texts = {l["text"] for l in Schematic(project).labels()}
    assert "VSUP" in texts and "VIN" not in texts
    assert not report["check"]["netlist_vs_expected"]["name_differences"]


def test_library_change_replaces_part_in_place(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    before = positions(project)
    parts = dict(BASE_PARTS, R1=("Device", "R_Small", "330", "tag-r1"))
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, BASE_NETS))
    assert report["replaced"] == [{"ref": "R1", "lib_id": "Device:R_Small"}]
    r1 = Schematic(project).find_symbols("R1")[0]
    assert r1.lib_id == "Device:R_Small" and (r1.place.x, r1.place.y) == before[("R1", 1)]


def test_new_parts_go_below_existing_drawing(project, tmp_path):
    sync_ok(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS))
    lowest = max(y for (_, _), (_, y) in positions(project).items())
    parts = dict(BASE_PARTS, C1=("Device", "C", "100n", "tag-c1"))
    nets = dict(BASE_NETS, VIN=BASE_NETS["VIN"] + [("C1", "1")], GND=BASE_NETS["GND"] + [("C1", "2")])
    report = sync_ok(project, write_netlist(tmp_path / "b.net", parts, nets))
    assert report["added"][0]["ref"] == "C1" and report["added"][0]["y"] > lowest


def test_dry_run_saves_nothing(project, tmp_path):
    before = project.read_text()
    report = kat.sync(project, write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS), dry_run=True)
    assert report["changed"] and report["check"] is None
    assert project.read_text() == before


def test_detach_then_connect_replaces_labels_by_wire(project, tmp_path, capsys):
    net = write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS)
    sync_ok(project, net)
    with kat.edit(project) as s:
        s.detach("R1.2")
        s.detach("D1.2")
        s.connect("R1.2", "D1.2")
    assert "LED_A" not in {l["text"] for l in Schematic(project).labels()}
    cmp = netlist.compare(netlist.from_schematic(project), netlist.read(net))
    assert cmp["match"], cmp
    # connected correctly, but the design's net name is no longer visible -> not clean
    assert cmp["unnamed"] == ["LED_A"]
    assert cli.main(["check", str(project), "--expect", str(net)]) == 0
    assert '"clean": false' in capsys.readouterr().out
    # a label on the new wire restores it; sync then has nothing to do
    assert cli.main(["label", str(project), "LED_A", "R1.2", "--stub", "0,0"]) == 0
    capsys.readouterr()
    assert not kat.sync(project, net)["changed"]


SWAP_PARTS = {
    "J1": ("Connector_Generic", "Conn_01x03", "IN", "tag-j1"),
    "R1": ("Device", "R", "1k", "tag-r1"),
    "R2": ("Device", "R", "1k", "tag-r2"),
}


def swap_nets(swapped: bool) -> dict:
    a, b = (("J1", "2"), ("J1", "1")) if swapped else (("J1", "1"), ("J1", "2"))
    return {"SIG_A": [a, ("R1", "1")], "SIG_B": [b, ("R2", "1")],
            "GND": [("J1", "3"), ("R1", "2"), ("R2", "2")]}


def drawn_items(sch: Path) -> list:
    s = Schematic(sch)
    return sorted([l["text"] for l in s.labels()]) + [len(s.segments())]


def test_swap_round_trip_adds_no_duplicates(project, tmp_path):
    """B2: pins whose net did not change (R1.1, R2.1) must not get another stub + label when
    their partner pin (J1.1 / J1.2) moves to another net."""
    base = write_netlist(tmp_path / "a.net", SWAP_PARTS, swap_nets(False))
    swapped = write_netlist(tmp_path / "b.net", SWAP_PARTS, swap_nets(True))
    sync_ok(project, base)
    before = drawn_items(project)
    report = sync_ok(project, swapped)
    assert sorted(a["pin"] for a in report["attached"]) == ["J1.1", "J1.2"], report["attached"]
    assert drawn_items(project) == before
    sync_ok(project, base)
    assert drawn_items(project) == before
    assert not kat.sync(project, base)["changed"]


def test_move_refuses_pin_contact_and_move_many_swaps(project, tmp_path):
    """B1: a pin placed on another part's pin joins the nets silently; a later move of the
    other part then drags wires between them. `move` refuses; `move_many` lets parts take
    each other's places without a parking step."""
    net = write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS)
    sync_ok(project, net)
    with kat.edit(project) as s:
        r1 = s.schematic.pin("R1", "1")
        d1 = s.schematic.pin("D1", "1")
        with pytest.raises(kat.KatError, match="would land on .*R1.1"):
            s.move("D1", by=(r1[0] - d1[0], r1[1] - d1[1]))
        pr, pd = s.pins("R1")[0], s.pins("D1")[0]
        s.move_many({"R1": {"to": (pd["x"], pd["y"])}, "D1": {"to": (pr["x"], pr["y"])}})
    check = kat.check(project, net)
    assert check["netlist_vs_expected"]["match"], check["netlist_vs_expected"]
    assert check["erc"]["errors"] == 0
    assert positions(project)[("R1", 1)] == (pd["x"], pd["y"])


def test_import_order_rows_and_paper(project, tmp_path):
    """B4/B5: natural order with partners side by side (joined by a wire when their net is
    unnamed), uniform rows, nothing off the page (the paper grows instead)."""
    n = 40
    parts = {f"R{i}": ("Device", "R", "1k", f"r{i}") for i in range(1, n + 1)}
    parts |= {f"C{i}": ("Device", "C", "1n", f"c{i}") for i in range(1, n + 1)}
    nets = {f"N${i}": [(f"R{i}", "1"), (f"C{i}", "1")] for i in range(1, n + 1)}
    nets["GND"] = [(f"{p}{i}", "2") for i in range(1, n + 1) for p in "RC"]
    net = write_netlist(tmp_path / "big.net", parts, nets)
    report = sync_ok(project, net)
    order = [a["ref"] for a in report["added"]]
    assert order[:6] == ["C1", "R1", "C2", "R2", "C3", "R3"]
    assert len(report["wired"]) == n
    assert not [l for l in Schematic(project).labels() if l["text"].startswith("N$")]
    assert report["paper"] == [{"from": "A4", "to": "A3"}]
    assert not report["check"]["off_page"]
    ys = {}
    for a in report["added"]:
        ys.setdefault(a["y"], []).append(a["ref"])
    assert all(len(v) > 1 for v in ys.values())  # every part shares its row line


def test_custom_fields_and_project_library(project, tmp_path):
    """SKiDL custom fields (part.fields["MPN"]) become hidden symbol fields and follow
    changes; a symbol from a project-local library (sym-lib-table with ${KIPRJMOD})."""
    from kat import library
    d = project.parent
    src = library.resolve("Device:R")
    src[1] = "MyR"
    for sub in src:
        if isinstance(sub, list) and str(sub[0]) == "symbol":
            sub[1] = str(sub[1]).replace("R_", "MyR_", 1)  # unit names carry the symbol name
    from kat.sexpr import dumps
    (d / "mylib.kicad_sym").write_text(
        f'(kicad_symbol_lib (version 20241209) (generator "kat_test") {dumps(src)})\n')
    (d / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "mylib") (type "KiCad") '
        '(uri "${KIPRJMOD}/mylib.kicad_sym") (options "") (descr "")))\n')
    parts = {"R1": ("mylib", "MyR", "1k", "r1"), "R2": ("Device", "R", "2k", "r2")}
    nets = {"A": [("R1", "1"), ("R2", "1")], "B": [("R1", "2"), ("R2", "2")]}

    def netfile(mpn):
        path = write_netlist(tmp_path / f"{mpn}.net", parts, nets)
        text = path.read_text().replace('(field (name "SKiDL Tag") "r1")',
                                        f'(field (name "SKiDL Tag") "r1") (field (name "MPN") "{mpn}")')
        path.write_text(text)
        return path

    sync_ok(project, netfile("RC0402-1K"))
    r1 = next(x for x in Schematic(project).symbols() if x.ref == "R1")
    assert r1.lib_id == "mylib:MyR" and r1.field("MPN") == "RC0402-1K"
    report = sync_ok(project, netfile("RC0402-1K2"))
    assert {"ref": "R1", "field": "MPN", "from": "RC0402-1K", "to": "RC0402-1K2"} in report["updated"]
    assert not kat.sync(project, netfile("RC0402-1K2"))["changed"]


def test_sheet_features(project, tmp_path):
    net = write_netlist(tmp_path / "a.net", BASE_PARTS, BASE_NETS)
    sync_ok(project, net)
    step = kat.snapshot(project, slug="before")["dir"]
    with kat.edit(project) as s:
        out = s.detach("R1")
        assert out["pins"] == 2 and out["labels"] == 2
        s.page("A3")
        assert s.title(title="LED test", rev="B", comments={1: "drive 3.3 V"}) == \
            {"title": "LED test", "rev": "B", "comment1": "drive 3.3 V"}
        t = s.text("Note: R1 detached", (30, 30), size=2.54)
        near = s.near((30, 30), radius=1)
        assert near[0]["kind"] == "text" and near[0]["uuid"] == t["uuid"]
        pin = s.near("D1.1", radius=0.1)
        assert pin[0]["kind"] == "pin" and pin[0]["pin"] == "D1.1"
    text = project.read_text()
    assert '(paper "A3")' in text and '(title "LED test")' in text and "Note: R1 detached" in text
    assert kat.check(project, net)["netlist_vs_expected"]["missing"]  # R1 is cut off now
    back = kat.restore(project, step, net)
    assert back["ok"]
    assert '(paper "A3")' not in project.read_text()
