"""Arrangement passes for the 10x10 LED matrix (A2 sheet).

    python led_matrix_arrange.py SCH pass1|pass2

Target picture:
  - J1 (rows 1-5) and J2 (rows 6-10) stacked on the left, net labels on their pins.
  - The matrix to the right, laid out like the board: matrix row r = drawing row, column
    c = drawing column. Each cell reads left to right:
        LED_r_c label -- R -- wire -- LED (anode left) -- GND
All coordinates are multiples of 2.54 mm so pins land on the grid.
"""

import sys

import kat

G = 2.54
J_X = 20 * G                  # connector column
J_Y = {1: 40 * G, 2: 90 * G}  # connector centres (body spans ±32 pins * 1.27)
M_X0 = 48 * G                 # centre of R in column 1
M_Y0 = 18 * G                 # row 1
DX = 16 * G                   # column pitch (40.64 mm)
DY = 8 * G                    # row pitch (20.32 mm)
CELLS = [(r, c) for r in range(1, 11) for c in range(1, 11)]


def n_of(r: int, c: int) -> int:
    return 10 * (r - 1) + c


def origin(r: int, c: int) -> tuple[float, float]:
    return M_X0 + (c - 1) * DX, M_Y0 + (r - 1) * DY


def gnd_pin(k: int) -> bool:  # same rule as led_matrix.py
    return (k - 1) % 5 == 0 or k == 64


def pin_y(j: int, k: int) -> float:
    return J_Y[j] - 15 * G + ((k - 1) // 2) * G


def pass1(s: kat.Sheet) -> None:
    """Import -> grid. The import joined each R and LED by a label pair; that becomes a
    plain wire. All parts move in one block move, so a part may land on the import position
    of another part that is itself moving away."""
    for r, c in CELLS:
        n = n_of(r, c)
        for p in (f"R{n}.1", f"R{n}.2", f"D{n}.1", f"D{n}.2"):
            s.detach(p)
    moves = {f"J{j}": {"to": (J_X, y)} for j, y in J_Y.items()}
    for r, c in CELLS:
        n, (x, y) = n_of(r, c), origin(r, c)
        moves[f"R{n}"] = {"to": (x, y), "rot": 90}             # pin 1 left, pin 2 right
        moves[f"D{n}"] = {"to": (x + 4 * G, y), "rot": 180}    # anode left, cathode right
    s.move_many(moves)
    for r, c in CELLS:
        n = n_of(r, c)
        s.label(f"LED_{r}_{c}", f"R{n}.1", stub=(-G, 0), angle=180)
        s.connect(f"R{n}.2", f"D{n}.2")
        s.power("GND", f"D{n}.1", stub=(G, 0))


def pass2(s: kat.Sheet) -> None:
    """Review of pass 1:
    - connector: sideways GND symbols put their text onto the next pin's label
      -> GND pins wired to a GND rail outside the labels, one GND symbol per rail
    - matrix: 'GND' text of every cathode collides with the LED value
      -> GND 5.08 mm out, LED value moved
    - J1/J2 reference and value sit on the right-hand labels -> above / below the body
    - the import's PWR_FLAG went away with the detached stubs -> one flag on the J1 rail"""
    lx, rx = J_X - 10 * G, J_X + 11 * G    # pin tips at J_X-2G / J_X+3G, rails 8G further out
    for j in J_Y:
        left = [k for k in range(1, 65, 2) if gnd_pin(k)]
        right = [k for k in range(2, 65, 2) if gnd_pin(k)]
        for pins, x in ((left, lx), (right, rx)):
            for k in pins:
                s.detach(f"J{j}.{k}")
                s.connect(f"J{j}.{k}", (x, pin_y(j, k)))
            s.wire((x, pin_y(j, pins[0])), (x, pin_y(j, pins[-1])))
            s.power("GND", (x, pin_y(j, pins[-1])), stub=(0, 2 * G))
        s.set(f"J{j}", "Reference", at=(J_X, J_Y[j] - 17 * G))
        s.set(f"J{j}", "Value", at=(J_X, J_Y[j] + 18 * G))
    s.power("PWR_FLAG", (lx, pin_y(1, 1)), stub=(0, -2 * G))
    for r, c in CELLS:
        n, (x, y) = n_of(r, c), origin(r, c)
        s.detach(f"D{n}.1")
        s.power("GND", f"D{n}.1", stub=(2 * G, 0))
        s.set(f"D{n}", "Value", at=(x + 3.5 * G, y + 1.5 * G))


PASSES = {"pass1": pass1, "pass2": pass2}

if __name__ == "__main__":
    with kat.edit(sys.argv[1]) as sheet:
        PASSES[sys.argv[2]](sheet)
