"""Transimpedance amplifier described in SKiDL, in two revisions.

    python tia_design.py v1|v2 OUT.net

v2 changes relative to v1:
  - adds ROUT (100 Ω) between the amplifier output and the output connector (new net OUT)
  - renames the feedback capacitor C1 -> CF1 (same tag, so it keeps its place in the drawing)
"""

import os
import sys

os.environ.setdefault("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols")

from skidl import KICAD10, Net, Part, generate_netlist, set_default_tool  # noqa: E402

variant, out_file = sys.argv[1], sys.argv[2]
set_default_tool(KICAD10)

gnd, vp, vn = Net("GND"), Net("+2V5"), Net("-2V5")
pd, tia_out, fb_b = Net("PD_ANODE"), Net("TIA_OUT"), Net("U1B_FB")

u = Part("Amplifier_Operational", "LTC6229xMS8E", ref="U1", value="LTC6229", tag="u_amp")
rf = Part("Device", "R", ref="R1", value="10M", tag="r_fb")
cf = Part("Device", "C", ref="CF1" if variant == "v2" else "C1", value="0.1p", tag="c_fb")
j_pd = Part("Connector", "Conn_01x02_Pin", ref="J1", value="PHOTODIODE", tag="j_pd")
j_out = Part("Connector", "Conn_01x02_Pin", ref="J2", value="OUTPUT", tag="j_out")
c_p = Part("Device", "C", ref="C2", value="100n", tag="c_dec_p")
c_n = Part("Device", "C", ref="C3", value="100n", tag="c_dec_n")

u[3] += gnd; u[2] += pd; u[1] += tia_out          # stage A: TIA
rf[1, 2] += pd, tia_out; cf[1, 2] += pd, tia_out  # feedback Rf || Cf
j_pd[1] += pd; j_pd[2] += vp                      # photodiode, cathode to +2V5
u[5] += gnd; u[6] += fb_b; u[7] += fb_b           # unused stage B: grounded follower
u[8] += vp; u[4, 9] += vn                         # supply
c_p[1, 2] += vp, gnd; c_n[1, 2] += gnd, vn        # decoupling
j_out[2] += gnd

if variant == "v2":
    out = Net("OUT")
    r_out = Part("Device", "R", ref="ROUT", value="100", tag="r_out")
    r_out[1, 2] += tia_out, out
    j_out[1] += out
else:
    j_out[1] += tia_out

generate_netlist(file_=out_file)
