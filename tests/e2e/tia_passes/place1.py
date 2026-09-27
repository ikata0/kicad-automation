"""place 1: signal flow left to right, feedback R1 || CF1 right above U1's inverting input
and output pins, so the high-impedance node stays short."""
import sys

import kat

PCB = sys.argv[1]
with kat.pcb.edit(PCB) as b:
    b.move_many({"J1": {"to": (3, 8.73)}, "U1": {"to": (16, 10)},
                 "R1": {"to": (12.5, 6.2)}, "CF1": {"to": (12.5, 4.4)},
                 "R2": {"to": (23, 9)}, "J2": {"to": (29, 8.73)}})
