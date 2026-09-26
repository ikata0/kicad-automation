"""10×10 LED matrix, every LED switched individually through a ribbon cable.

    python led_matrix.py [OUT.net] [probe]   (default: led_matrix.net)

`probe` is a throw-away variant for testing `kat sync` on the laid-out drawing: J1 pins 2
and 3 swap their signals (LED_1_1 <-> LED_1_2) and R1 becomes 1k.

Circuit per LED (row r, column c, both 1..10; n = 10*(r-1) + c):
    net LED_r_c (from the connector) -> Rn (680 Ω) -> Dn anode, Dn cathode -> GND

The driving side (not on this board) sources current into LED_r_c: ~2 mA at 3.3 V,
~4.5 mA at 5 V with Vf ≈ 1.9–2.1 V (Kingbright APT1608SGC, 570 nm, 0603).

Connectors: two 2×32 2.54 mm shrouded IDC headers (standard 1.27 mm ribbon, 64 wires each).
J1 carries rows 1–5, J2 rows 6–10. Ribbon wire k = header pin k. Pattern per 5 wires:
GND S S S S, and pin 64 is GND too -> 14 GND + 50 signals per cable, every signal wire at
most two wires away from a ground wire.
"""

import os
import sys

os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")

from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool  # noqa: E402

out_file = sys.argv[1] if len(sys.argv) > 1 else "led_matrix.net"
PROBE = sys.argv[2:] == ["probe"]
set_default_tool(KICAD10)

ROWS = COLS = 10
LED_FP = "LED_SMD:LED_0603_1608Metric"
R_FP = "Resistor_SMD:R_0402_1005Metric"
IDC_FP = "Connector_IDC:IDC-Header_2x32_P2.54mm_Vertical"

gnd = Net("GND")


def gnd_pin(k: int) -> bool:
    return (k - 1) % 5 == 0 or k == 64


signals = {}
for r in range(1, ROWS + 1):
    for c in range(1, COLS + 1):
        n = 10 * (r - 1) + c
        sig = signals[r, c] = Net(f"LED_{r}_{c}")
        mid = Net()  # R-LED node: unnamed, drawn as a plain wire
        res = Part("Device", "R", ref=f"R{n}", value="1k" if PROBE and n == 1 else "680", footprint=R_FP, tag=f"r_{r}_{c}")
        led = Part("Device", "LED", ref=f"D{n}", value="APT1608SGC", footprint=LED_FP,
                   tag=f"d_{r}_{c}")
        res.fields["MPN"] = "RC0402FR-07680RL" if not (PROBE and n == 1) else "RC0402FR-071KL"
        led.fields["MPN"] = "APT1608SGC"
        res[1] += sig
        res[2] += mid
        led["A"] += mid
        led["K"] += gnd

for j, rows in ((1, range(1, 6)), (2, range(6, 11))):
    conn = Part("Connector_Generic", "Conn_02x32_Odd_Even", ref=f"J{j}", value="IDC_2x32",
                footprint=IDC_FP, tag=f"j_{j}")
    conn.fields["MPN"] = "61206421621"   # part numbers here are examples for the gates
    order = [signals[r, c] for r in rows for c in range(1, COLS + 1)]
    if PROBE and j == 1:
        order[0], order[1] = order[1], order[0]
    for k in range(1, 65):
        conn[k] += gnd if gnd_pin(k) else order.pop(0)
    assert not order

generate_netlist(file_=out_file)
