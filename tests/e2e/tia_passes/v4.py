"""v4 arrived by sync: R1 is 0805 now (replaced in place), C4 new in the import area.
C4 goes next to the +2V5 pin as well; its GND pad gets a plane via."""
import sys

import kat

PCB = sys.argv[1]
with kat.pcb.edit(PCB) as b:
    b.place_near("C4", "U1.8")
    res = b.fanout(["C4"], nets=["GND"])
    assert not res["failed"], res["failed"]
