"""Layout passes for the 10x10 LED matrix board.

    python led_matrix_board.py PCB place|structure|finish

Target: 132 x 100 mm, connectors J1/J2 on the left, the matrix read like the schematic
(row r, column c), each cell R then LED (anode facing R) and the cathode's via to GND.
Review of the two-layer attempts (see SPEC 4.10): pours on B.Cu were cut into islands by
signal tracks, and Freerouting treats vias inside a B.Cu keep-out as violations. So: four
layers, In1.Cu a whole GND plane (layer type power, no tracks), signals on F/In2/B.
"""

import sys

import kat

X0, Y0, DX, DY = 40, 12, 9, 8.5


def place(b: kat.pcb.Board) -> None:
    """place 1: outline, 4 layers with the GND plane, connectors left, matrix grid."""
    b.outline(rect=(0, 0, 132, 100), radius=2)
    b.set_copper_layers(4)
    b.plane("GND", "In1.Cu")
    moves = {"J1": {"to": (10, 10)}, "J2": {"to": (24, 10)}}
    for n in range(1, 101):
        r, c = divmod(n - 1, 10)
        x, y = X0 + c * DX, Y0 + r * DY
        moves[f"R{n}"] = {"to": (x, y)}
        moves[f"D{n}"] = {"to": (x + 2.6, y), "rot": 180}   # anode faces the resistor
    b.move_many(moves)


def structure(b: kat.pcb.Board) -> None:
    """structure 1: every cathode reaches the plane; R-LED links laid and locked."""
    res = b.fanout([f"D{n}" for n in range(1, 101)], nets=["GND"])
    assert not res["failed"], res["failed"]
    for n in range(1, 101):
        b.track([f"R{n}.2", f"D{n}.2"], lock=True)


def finish(b: kat.pcb.Board) -> None:
    """finish: references, board name, title block from the schematic."""
    res = b.place_refs(size=0.8)
    assert not res["failed"], res["failed"]
    t = b.title_from_schematic()
    b.board_text(f"{t['title']} rev {t['rev']}", (80, 97.5), size=1.2)


PASSES = {"place": place, "structure": structure, "finish": finish}

if __name__ == "__main__":
    with kat.pcb.edit(sys.argv[1]) as board:
        PASSES[sys.argv[2]](board)
