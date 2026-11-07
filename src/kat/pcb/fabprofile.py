"""Fabrication profile: a supplier's limits for the whole flow, kept with the board.

``b.fab_profile(via_types=["through"], min_clearance=0.125)`` stores the profile in
``<board>.kat.json`` (``fab_profile``) and writes what KiCad can enforce: a generated DRC rule
``kat profile vias`` that disallows the other via kinds, and the board's minimum clearance.
kat then holds every step to it:

- ``b.via`` refuses a forbidden kind, ``route_vias`` a forbidden span;
- ``b.netclass`` refuses a clearance below the minimum unless ``below_profile`` (recorded
  in the profile's ``exceptions``);
- ``stamp`` warns about a block's forbidden vias and classes below the minimum;
- ``kat pcb check`` lists the board's departures in ``profile`` (not clean), ``kat pcb ready``
  makes them errors, and ``kat pcb fab`` refuses a board with forbidden vias.
"""

from __future__ import annotations

import re

from .. import env
from . import layers as lay
from .structure import VIA_KINDS

KICAD_VIA = {"through": "through_via", "blind": "blind_via", "buried": "buried_via", "micro": "micro_via"}
RULE = "kat profile vias"


def read(pcb) -> dict:
    """The board's stored profile (``{}`` if it has none)."""
    return dict(lay.meta(pcb).get("fab_profile") or {})


def check_values(via_types, min_clearance) -> None:
    if via_types is not None:
        bad = [k for k in via_types if k not in VIA_KINDS]
        if bad or not via_types:
            raise env.KatError(f"via types {', '.join(bad) or '(none)'}: some of {', '.join(VIA_KINDS)}")
    if min_clearance is not None and not 0 < float(min_clearance) < 10:
        raise env.KatError(f"min_clearance {min_clearance}: mm, above 0")


def rule(via_types: list[str] | None) -> str | None:
    """The generated DRC rule disallowing the via kinds the profile leaves out."""
    if not via_types:
        return None
    banned = [KICAD_VIA[k] for k in VIA_KINDS if k not in via_types]
    return f'(rule "{RULE}" (constraint disallow {" ".join(banned)}))' if banned else None


def via_allowed(profile: dict, kind: str, where: str = "") -> None:
    """KatError if the profile forbids via ``kind``."""
    kinds = profile.get("via_types")
    if kinds and kind not in kinds:
        raise env.KatError(f"{where or 'a'} {kind} via: the board's fabrication profile allows "
                           f"{', '.join(kinds)} vias only (kat pcb profile)")


def _classes_below(data: dict, minimum: float, exceptions: dict) -> list[dict]:
    from .setup import DEFAULT_CLASS
    out = []
    for c in (data.get("net_settings") or {}).get("classes") or [DEFAULT_CLASS]:   # none: KiCad's default
        v = c.get("clearance")
        if v is not None and v < minimum - 1e-9 and c.get("name") not in exceptions:
            out.append({"class": c.get("name"), "clearance": v, "minimum": minimum})
    return out


def _rules_below(text: str, minimum: float) -> list[dict]:
    out = []
    for name, value in re.findall(r'\(rule\s+"?([^"\s)]+[^")]*)"?.*?\(constraint\s+clearance\s+\(min\s+([\d.]+)mm',
                                  text, re.S):
        if float(value) < minimum - 1e-9:
            out.append({"rule": name.strip(), "clearance": float(value), "minimum": minimum})
    return out


def findings(b) -> dict:
    """The board's departures from its profile: ``forbidden_vias`` [{kind, at, net}],
    ``classes_below`` and ``rules_below`` the minimum clearance (classes recorded as
    exceptions are left out). Empty without a profile or departures."""
    p = read(b.path)
    if not p:
        return {}
    out: dict = {}
    kinds = p.get("via_types")
    if kinds:
        bad = [{"kind": v["kind"], "at": list(v["at"]), "net": v["net"]} for v in b.vias() if v["kind"] not in kinds]
        if bad:
            out["forbidden_vias"] = bad
    m = p.get("min_clearance")
    if m:
        if cls := _classes_below(b._project_data(), m, p.get("exceptions") or {}):
            out["classes_below"] = cls
        if rules := _rules_below(b._dru_text(), m):
            out["rules_below"] = rules
    return out


def block_departures(src, profile: dict, block: str) -> list[str]:
    """Warnings for a block board stamped onto a design with ``profile``: its via kinds and
    net-class clearances the profile does not allow."""
    out = []
    kinds = profile.get("via_types")
    if kinds:
        used = sorted({v["kind"] for v in src.vias()} - set(kinds))
        if used:
            out.append(f"block {block} uses {', '.join(used)} vias; the design's fabrication profile allows "
                       f"{', '.join(kinds)} only")
    m = profile.get("min_clearance")
    if m:
        for c in _classes_below(src._project_data(), m, {}):
            out.append(f"block {block}: net class {c['class']} has clearance {c['clearance']:g} mm, below the "
                       f"design's profile minimum {m:g} mm")
    return out
