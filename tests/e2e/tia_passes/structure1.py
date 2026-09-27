"""structure 1 (review of the first route: C3 next to U1.4 blocked the only way out of U1.3
to the plane): the amplifier's GND pads get their plane vias first, then the decoupling
capacitors go next to their supply pins, clear of those vias, and get their own GND vias."""
import sys

import kat

PCB = sys.argv[1]
with kat.pcb.edit(PCB) as b:
    res = b.fanout(["U1"], nets=["GND"])
    assert not res["failed"], res["failed"]
    b.place_near("C2", "U1.8")        # +2V5
    b.place_near("C3", "U1.4")        # -2V5
    res = b.fanout(["C2", "C3"], nets=["GND"])
    assert not res["failed"], res["failed"]
kat.pcb.snapshot(PCB, slug="structure", note="placed; GND vias; decoupling", three_d=False)
