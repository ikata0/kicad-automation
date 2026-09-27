"""setup: 32 x 20 mm two-layer board, rounded corners; B.Cu a GND pour.

Target: read left to right like the schematic - photodiode connector J1, amplifier U1,
output resistor R2, output connector J2.
"""
import sys

import kat

PCB = sys.argv[1]
with kat.pcb.edit(PCB) as b:
    b.outline(rect=(0, 0, 32, 20), radius=1)
    b.zone("GND", "B.Cu")
