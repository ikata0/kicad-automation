"""Arrangement pass after the v2 sync.

Review of the v2 import: parts sit in the placement strip, grouped by part and joined by
label pairs. Target: signal flow left to right, feedback above the amplifier, supply unit
and decoupling to the right. Connectivity is unchanged (check --expect), and every net
named in the design keeps a visible name (label or power symbol).

    python arrange_v2.py tia/tia.kicad_sch
"""

import sys

import kat


def rewire(s, pins):
    """Replace a pin's label/stub wiring by real wires drawn afterwards."""
    for p in pins:
        s.detach(p)


def arrange(s: kat.Sheet) -> None:
    # placement
    s.move("J1", to=(88.9, 99.06))
    s.move("U1", unit=1, to=(127, 101.6), mirror="x")
    s.move("R1", to=(127, 86.36), rot=90)
    s.move("CF1", to=(127, 73.66), rot=90)
    s.move("R2", to=(157.48, 101.6), rot=90)
    s.move("J2", to=(175.26, 101.6), mirror="y")
    s.move("U1", unit=2, to=(127, 127))
    s.move("U1", unit=3, to=(190.5, 127))
    s.move("C2", to=(210.82, 127))
    s.move("C3", to=(231.14, 127))

    # PD_ANODE: photodiode -> inverting input, feedback node as wires
    rewire(s, ["J1.1", "U1.2", "R1.1", "CF1.1"])
    s.connect("J1.1", "U1.2")
    s.connect("R1.1", (114.3, 99.06), via=[(114.3, 86.36)])
    s.connect("CF1.1", (114.3, 86.36), via=[(114.3, 73.66)])
    s.label("PD_ANODE", (101.6, 99.06))

    # TIA_OUT: output node -> feedback + series resistor
    rewire(s, ["U1.1", "R1.2", "CF1.2", "R2.1"])
    s.connect("U1.1", "R2.1")
    s.connect("R1.2", (139.7, 101.6), via=[(139.7, 86.36)])
    s.connect("CF1.2", (139.7, 86.36), via=[(139.7, 73.66)])
    s.label("TIA_OUT", (142.24, 101.6))

    # OUT: series resistor -> output connector; its ground downwards
    rewire(s, ["R2.2", "J2.1"])
    s.connect("R2.2", "J2.1")
    s.label("OUT", (162.56, 101.6))
    s.detach("J2.2")
    s.power("GND", "J2.2", stub=(-5.08, 0))

    # unused stage B as a wired follower
    rewire(s, ["U1.6", "U1.7"])
    s.connect("U1.7", "U1.6", via=[(137.16, 127), (137.16, 137.16), (116.84, 137.16), (116.84, 129.54)])
    s.label("U1B_FB", (119.38, 137.16))
    s.set("U1", "Value", unit=2, at=(147.32, 127))


if __name__ == "__main__":
    with kat.edit(sys.argv[1]) as sheet:
        arrange(sheet)
