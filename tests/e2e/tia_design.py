"""Transimpedance amplifier described in SKiDL, in three revisions.

    python tia_design.py v1|v2|v3|v4 OUT.net

v2 changes relative to v1:
  - adds R2 (100 Ω, output resistor) between the amplifier output and the output connector (new net OUT)
  - renames the feedback capacitor C1 -> CF1 (same tag, so it keeps its place in the drawing)
v3 = v2 ready for the PCB: a footprint and a part number (MPN) on every part. The part
numbers are examples for the ready-for-layout gate, not a checked BOM.
v4 = v3 changed during layout: R1 in 0805 (voltage rating), a second +2V5 decoupling
capacitor C4.
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
cf = Part("Device", "C", ref="C1" if variant == "v1" else "CF1", value="0.1p", tag="c_fb")
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

if variant == "v1":
    j_out[1] += tia_out
else:
    out = Net("OUT")
    r_out = Part("Device", "R", ref="R2", value="100", tag="r_out")
    r_out[1, 2] += tia_out, out
    j_out[1] += out

if variant == "v4":
    c_p2 = Part("Device", "C", ref="C4", value="1u", tag="c_dec_p2")
    c_p2[1, 2] += vp, gnd

if variant in ("v3", "v4"):
    HDR = "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    parts_fp = [
            (u, "Package_SO:MSOP-8-1EP_3x3mm_P0.65mm_EP1.68x1.88mm", "LTC6229IMS8E#PBF"),
            (rf, "Resistor_SMD:R_0805_2012Metric" if variant == "v4" else "Resistor_SMD:R_0603_1608Metric",
             "RC0805FR-0710ML" if variant == "v4" else "RC0603FR-0710ML"),
            (cf, "Capacitor_SMD:C_0603_1608Metric", "GRM1885C1HR10BA01D"),
            (r_out, "Resistor_SMD:R_0603_1608Metric", "RC0603FR-07100RL"),
            (c_p, "Capacitor_SMD:C_0603_1608Metric", "CC0603KRX7R9BB104"),
            (c_n, "Capacitor_SMD:C_0603_1608Metric", "CC0603KRX7R9BB104"),
            (j_pd, HDR, "61300211121"), (j_out, HDR, "61300211121")]
    if variant == "v4":
        parts_fp.append((c_p2, "Capacitor_SMD:C_0603_1608Metric", "GRM188R71E105KA12D"))
    for part, fp, mpn in parts_fp:
        part.footprint = fp
        part.fields["MPN"] = mpn

generate_netlist(file_=out_file)
