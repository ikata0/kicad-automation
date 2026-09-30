"""Nested SKiDL blocks for the block tests: top -> 2x ch (Interface) -> 3x rc (RC low-pass).

    python blocks_design.py VARIANT OUT.net      VARIANT: base | value | untagged | rc

- base: CH_A/CH_B, first stage ra = 1k / 3k3
- value: CH_B's first stage becomes 6k8 (a design change inside one instance)
- untagged: no tag= and positional arguments (the block record warns)
- rc: the RC block alone with a harness connector (the block board's design)
Writes OUT.net and OUT.blocks.json.
"""
import os
import sys

from kat.env import stock_symbol_dir
if symbols := stock_symbol_dir():
    os.environ.setdefault("KICAD10_SYMBOL_DIR", str(symbols))
from skidl import KICAD10, Interface, Net, Part, generate_netlist, set_default_tool, subcircuit  # noqa: E402

import kat  # noqa: E402

set_default_tool(KICAD10)
R_FP, C_FP = "Resistor_SMD:R_0603_1608Metric", "Capacitor_SMD:C_0603_1608Metric"
variant, out = sys.argv[1], sys.argv[2]


@subcircuit
def rc(inp, out, gnd, r_val="10k"):
    """RC low-pass: inp -> R -> out, C to gnd."""
    r = Part("Device", "R", value=r_val, footprint=R_FP, tag="r")
    c = Part("Device", "C", value="100n", footprint=C_FP, tag="c")
    inp += r[1]
    r[2] += out
    out += c[1]
    c[2] += gnd


@subcircuit
def ch(io, ra, rb="2k2"):
    """Three RC stages and an output resistor."""
    n1, n2, n3 = Net(), Net(), Net()
    if variant == "untagged":
        rc(io.inp, n1, io.gnd, ra)
        rc(n1, n2, io.gnd, rb)
        rc(n2, n3, io.gnd, "4k7")
    else:
        rc(inp=io.inp, out=n1, gnd=io.gnd, r_val=ra, tag="RC1")
        rc(inp=n1, out=n2, gnd=io.gnd, r_val=rb, tag="RC2")
        rc(inp=n2, out=n3, gnd=io.gnd, r_val="4k7", tag="RC3")
    r = Part("Device", "R", value="100", footprint=R_FP, tag="rout")
    n3 += r[1]
    r[2] += io.out


if variant == "rc":
    # the RC block on its own: a harness connector (its environment) and one instance
    gnd, a, b = Net("GND"), Net("IN"), Net("OUT")
    j = Part("Connector_Generic", "Conn_01x03", footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
             tag="j")
    j[1] += a
    j[2] += b
    j[3] += gnd
    rc(inp=a, out=b, gnd=gnd, tag="DUT")
    generate_netlist(file_=out)
    kat.skidl_blocks.write(out)
    sys.exit(0)

gnd = Net("GND")
j = Part("Connector_Generic", "Conn_01x05", footprint="Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
         tag="j")
for i, name in enumerate("AB"):
    a, b = Net(f"IN_{name}"), Net(f"OUT_{name}")
    j[2 * i + 1] += a
    j[2 * i + 2] += b
    ra = {"A": "1k", "B": "6k8" if variant == "value" else "3k3"}[name]
    io = Interface(inp=a, out=b, gnd=gnd)
    if variant == "untagged":
        ch(io, ra)
    else:
        ch(io=io, ra=ra, tag=f"CH_{name}")
j[5] += gnd
generate_netlist(file_=out)
kat.skidl_blocks.write(out)
