"""Layout passes for the TIA board (design v3, and v4 after the design change).

    python tia_board.py PCB place|structure|finish|v4

Target: a 32 x 20 mm two-layer board read left to right like the schematic - photodiode
connector J1, amplifier U1, output resistor R2, output connector J2. Feedback R1 || CF1
right above U1's inverting input and output pins, so the high-impedance node stays short.
Decoupling capacitors next to their supply pins. B.Cu is a GND plane.
"""

import sys

import kat


def _fanout(b: kat.pcb.Board, refs: list[str]) -> None:
    res = b.fanout(refs, nets=["GND"])
    assert not res["failed"], res["failed"]


def place(b: kat.pcb.Board) -> None:
    """place 1: signal flow left to right, feedback above the amplifier's input pins."""
    b.outline(rect=(0, 0, 32, 20), radius=1)
    b.move_many({"J1": {"to": (3, 8.73)}, "U1": {"to": (16, 10)},
                 "R1": {"to": (12.5, 6.2)}, "CF1": {"to": (12.5, 4.4)},
                 "R2": {"to": (23, 9)}, "J2": {"to": (29, 8.73)}})
    b.zone("GND", "B.Cu")


def structure(b: kat.pcb.Board) -> None:
    """structure 1 (review of the first route: C3 next to U1.4 blocked the only way out
    of U1.3 to the plane): the amplifier's GND pads get their plane vias first, then the
    decoupling capacitors go next to their supply pins, clear of those vias, and get
    their own GND vias."""
    _fanout(b, ["U1"])
    b.place_near("C2", "U1.8")    # +2V5
    b.place_near("C3", "U1.4")    # -2V5
    _fanout(b, ["C2", "C3"])


def finish(b: kat.pcb.Board) -> None:
    """finish: references, board name, title block from the schematic."""
    b.place_refs()
    title = b.title_from_schematic()
    b.board_text(f"TIA rev {title.get('rev', '')}", (16, 18), size=1.0)


def v4(b: kat.pcb.Board) -> None:
    """v4 arrived by sync: R1 is 0805 now (replaced in place), C4 new in the import area.
    C4 goes next to the +2V5 pin as well; its GND pad gets a plane via."""
    b.place_near("C4", "U1.8")
    _fanout(b, ["C4"])


PASSES = {"place": place, "structure": structure, "finish": finish, "v4": v4}

if __name__ == "__main__":
    with kat.pcb.edit(sys.argv[1]) as board:
        PASSES[sys.argv[2]](board)
