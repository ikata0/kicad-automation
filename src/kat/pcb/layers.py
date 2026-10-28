"""Layer stack-up, layer roles and the layer map of a block stamped on the bottom side.

The stack-up is read from the board's ``(setup (stackup ...))``; a board without one has
KiCad's default build-up (copper 35 µm, equal FR4 dielectrics filling the board
thickness). Layer roles and kat's net rules live in the board's metadata file
``<board>.kat.json`` next to it.

Layer maps take the copper layers of a block board to the copper layers of a design:

- ``auto``: the mirror map L(k) <-> L(N+1-k) (KiCad's ``FlipLayer``) on the bottom side,
  the identity on the top side; refused on a stack-up that is not mirror-symmetric.
- ``roles``: by the layer roles both boards declare (``set_layer_roles``).
- explicit: ``"F.Cu=B.Cu,In1.Cu=In4.Cu"`` (or ``"L1=L6,..."``); unnamed layers follow ``auto``.

Technical layers (Mask, Paste, SilkS, Fab, CrtYd, Adhes) swap F./B. with the side;
``User.*`` and ``Edge.Cuts`` stay.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import env
from ..sexpr import Sym, find, head
from .footprints import swap_side

COPPER_UM = 0.035
EPS_FR4 = 4.5
TOL_MM = 0.002          # thickness tolerance of the symmetry check
TOL_EPS = 0.05


# --- board metadata ----------------------------------------------------------------------

def meta_file(pcb: Path | str) -> Path:
    """``<board>.kat.json``: layer roles and net rules of a board."""
    return Path(pcb).with_suffix(".kat.json")


def meta(pcb: Path | str) -> dict:
    """The board's metadata (``{}`` if there is none)."""
    f = meta_file(pcb)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def write_meta(pcb: Path | str, data: dict) -> None:
    meta_file(pcb).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def layer_name(token: str, copper: list[str]) -> str:
    """``L3`` -> the third copper layer from the top; KiCad names pass unchanged."""
    m = re.fullmatch(r"[Ll](\d+)", token.strip())
    if m:
        k = int(m.group(1))
        if not 1 <= k <= len(copper):
            raise env.KatError(f"{token}: the board has {len(copper)} copper layers")
        return copper[k - 1]
    if token.strip() not in copper:
        raise env.KatError(f"no copper layer {token!r} (layers: {', '.join(copper)})")
    return token.strip()


def set_layer_roles(pcb: Path | str, roles: dict[str, str] | None = None) -> dict:
    """Declare what each copper layer is for, e.g. ``{"L1": "in_signal", "L2": "gnd_ref"}``
    (keys ``L<k>`` or KiCad names). A block board names roles as seen from its component
    side; a design names side-specific roles with ``_top`` / ``_bottom`` (``in_signal_top``)
    and the rest plainly; a layer with several roles joins them with ``|``
    (``"in_signal_top|back_signal_bottom"``). ``roles=None`` reads them. Stored in
    ``<board>.kat.json``; returns {layer: role}."""
    from .board import Board
    data = meta(pcb)
    if roles is not None:
        copper = Board(pcb).copper_layers()
        data["layer_roles"] = {layer_name(k, copper): v for k, v in roles.items() if v}
        write_meta(pcb, data)
    return dict(data.get("layer_roles", {}))


# --- stack-up -----------------------------------------------------------------------------

def stackup(board) -> dict:
    """The board's stack-up top to bottom: ``copper`` [{name, thickness}], ``dielectrics``
    [{thickness, epsilon_r, material}] (the one between copper k and k+1 at index k),
    thicknesses in mm; ``default`` True when the board stores none (KiCad's default)."""
    copper = board.copper_layers()
    node = find(find(board.tree, "setup") or [], "stackup")
    if node is None:
        gen = find(board.tree, "general")
        total = float(find(gen, "thickness")[1]) if gen is not None and find(gen, "thickness") else 1.6
        n = len(copper)
        d = max(total - n * COPPER_UM, 0.1) / (n - 1)
        return {"default": True, "copper": [{"name": c, "thickness": COPPER_UM} for c in copper],
                "dielectrics": [{"thickness": round(d, 4), "epsilon_r": EPS_FR4, "material": "FR4"}
                                for _ in range(n - 1)]}
    cu, diel, pending = [], [], []
    for lay in node[1:]:
        if head(lay) != "layer":
            continue
        kind = str((find(lay, "type") or [None, ""])[1])
        if kind == "copper":
            if cu:
                diel.append(_merge(pending))
            pending = []
            cu.append({"name": str(lay[1]), "thickness": float((find(lay, "thickness") or [0, COPPER_UM])[1])})
        elif str(lay[1]).startswith("dielectric") and cu:
            pending += _sublayers(lay)
    return {"default": False, "copper": cu, "dielectrics": diel}


def write_stackup(board, dielectrics: list[float], copper=COPPER_UM, epsilon_r=EPS_FR4,
                  material: str = "FR4") -> dict:
    """Replace the board's stack-up (see ``Board.set_stackup``); returns ``stackup``."""
    from ..sexpr import loads
    cu = board.copper_layers()
    n = len(cu)
    if len(dielectrics) != n - 1:
        raise env.KatError(f"{n} copper layers need {n - 1} dielectric thicknesses, not {len(dielectrics)}")
    cut = copper if isinstance(copper, (list, tuple)) else [copper] * n
    eps = epsilon_r if isinstance(epsilon_r, (list, tuple)) else [epsilon_r] * (n - 1)
    if len(cut) != n or len(eps) != n - 1:
        raise env.KatError("copper: one value or one per copper layer; epsilon_r: one or one per dielectric")
    parts = ['(layer "F.SilkS" (type "Top Silk Screen"))', '(layer "F.Paste" (type "Top Solder Paste"))',
             '(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))']
    for k, name in enumerate(cu):
        parts.append(f'(layer "{name}" (type "copper") (thickness {cut[k]:g}))')
        if k < n - 1:
            kind = "prepreg" if k in (0, n - 2) or k % 2 == 0 else "core"
            parts.append(f'(layer "dielectric {k + 1}" (type "{kind}") (thickness {dielectrics[k]:g}) '
                         f'(material "{material}") (epsilon_r {eps[k]:g}) (loss_tangent 0.02))')
    parts += ['(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))',
              '(layer "B.Paste" (type "Bottom Solder Paste"))', '(layer "B.SilkS" (type "Bottom Silk Screen"))',
              '(copper_finish "None")', '(dielectric_constraints no)']
    node = loads("(stackup " + " ".join(parts) + ")")
    setup = find(board.tree, "setup")
    old = find(setup, "stackup")
    if old is not None:
        setup[setup.index(old)] = node
    else:
        setup.insert(1, node)
    return stackup(board)


def _sublayers(lay: list) -> list[dict]:
    """A dielectric's sub-layers (``addsublayer`` separates them)."""
    out, cur = [], {}
    for c in lay[2:]:
        if isinstance(c, Sym) and str(c) == "addsublayer":
            out.append(cur)
            cur = {}
        elif head(c) in ("thickness", "epsilon_r", "material"):
            cur[head(c)] = str(c[1]) if head(c) == "material" else float(c[1])
    out.append(cur)
    return out


def _merge(subs: list[dict]) -> dict:
    t = sum(s.get("thickness", 0.0) for s in subs)
    eps = sum(s.get("thickness", 0.0) * s.get("epsilon_r", EPS_FR4) for s in subs) / t if t else EPS_FR4
    return {"thickness": round(t, 4), "epsilon_r": round(eps, 3),
            "material": "/".join(dict.fromkeys(s.get("material", "?") for s in subs))}


def asymmetry(design, block=None) -> list[str]:
    """Why ``auto`` (the mirror map) would change a flipped block's parasitics: the
    design's stack-up not mirror-symmetric (copper and dielectric thickness, εr,
    material), or the block board with another layer count. Empty if symmetric."""
    s = stackup(design)
    cu, di = s["copper"], s["dielectrics"]
    out = []
    if block is not None and len(block.copper_layers()) != len(cu):
        out.append(f"layer count: block {len(block.copper_layers())}, design {len(cu)}")
    for k in range(len(cu) // 2):
        a, b = cu[k], cu[-1 - k]
        if abs(a["thickness"] - b["thickness"]) > TOL_MM:
            out.append(f"copper {a['name']} {a['thickness']:g} mm vs {b['name']} {b['thickness']:g} mm")
    for k in range(len(di) // 2):
        a, b = di[k], di[-1 - k]
        name = f"dielectric {k + 1} vs {len(di) - k}"
        if abs(a["thickness"] - b["thickness"]) > TOL_MM:
            out.append(f"{name}: thickness {a['thickness']:g} vs {b['thickness']:g} mm")
        if abs(a["epsilon_r"] - b["epsilon_r"]) > TOL_EPS:
            out.append(f"{name}: εr {a['epsilon_r']:g} vs {b['epsilon_r']:g}")
        if a["material"] != b["material"]:
            out.append(f"{name}: material {a['material']} vs {b['material']}")
    return out


def gap(board, a: str, b: str) -> tuple[float, float] | None:
    """(thickness mm, εr) of the dielectric between adjacent copper layers ``a`` and ``b``,
    None if they are not adjacent."""
    copper = board.copper_layers()
    i, j = sorted((copper.index(a), copper.index(b)))
    if j - i != 1:
        return None
    d = stackup(board)["dielectrics"][i]
    return d["thickness"], d["epsilon_r"]


# --- layer maps ---------------------------------------------------------------------------

def copper_map(spec: str | dict | None, bottom: bool, design, block, design_path=None,
               block_path=None, allow_asymmetric: bool = False) -> dict[str, str]:
    """Block copper layer -> design copper layer for a stamp on the top (``bottom`` False)
    or bottom side. ``spec``: None/"auto", "roles", or an explicit map (string
    ``"F.Cu=B.Cu,..."`` / ``"L1=L6,..."`` or dict). Checked: bijective, outer layers to
    the outer layer of the stamp's side, inner to inner."""
    dl, bl = design.copper_layers(), block.copper_layers()
    spec = spec or "auto"

    def mirror(name: str) -> str:
        if name not in dl:
            raise env.KatError(f"the block uses {name}, which the design lacks")
        return dl[len(dl) - 1 - dl.index(name)] if bottom else name

    if spec == "auto":
        if bottom and not allow_asymmetric:
            why = asymmetry(design, block)
            if why:
                raise env.KatError("the stack-up is not mirror-symmetric, so a flipped block would "
                                   "change its parasitics: " + "; ".join(why) + ". Use a layer map "
                                   "(roles or explicit) or allow_asymmetric")
        out = {name: mirror(name) for name in bl if name in dl}
    elif spec == "roles":
        out = _by_roles(set_layer_roles(block_path), set_layer_roles(design_path), bl, dl, bottom)
    else:
        if isinstance(spec, str):
            pairs = [p.split("=", 1) for p in spec.replace(" ", "").split(",") if p]
            if any(len(p) != 2 for p in pairs):
                raise env.KatError(f"layer map {spec!r}: give FROM=TO pairs, e.g. F.Cu=B.Cu,In1.Cu=In2.Cu")
            spec = {layer_name(a, bl): layer_name(b, dl) for a, b in pairs}
        out = {name: mirror(name) for name in bl if name in dl and name not in spec}
        out.update(spec)
    _check_map(out, bl, dl, bottom)
    return out


def _by_roles(broles: dict, droles: dict, bl: list[str], dl: list[str], bottom: bool) -> dict:
    """Pair block layers with design layers of the same role, from the component side
    inwards: block role R goes to the design's ``R_top``/``R_bottom`` (the stamp's side)
    layers, else its plain ``R`` layers."""
    if not broles or not droles:
        missing = [n for n, r in (("block", broles), ("design", droles)) if not r]
        raise env.KatError(f"layer roles missing on the {' and '.join(missing)} board: "
                           f"kat pcb roles BOARD L1=ROLE ...")
    side = "bottom" if bottom else "top"
    order = list(reversed(dl)) if bottom else dl
    has = {n: set(r.split("|")) for n, r in droles.items()}
    out = {}
    for role in dict.fromkeys(broles[n] for n in bl if n in broles):
        src = [n for n in bl if broles.get(n) == role]
        dst = [n for n in order if f"{role}_{side}" in has.get(n, ())] or \
              [n for n in order if role in has.get(n, ())]
        if len(dst) != len(src):
            raise env.KatError(f"role {role!r}: the block has it on {', '.join(src)}, the design "
                               f"on {', '.join(dst) or 'no layer'} ({side} side)")
        out.update(zip(src, dst))
    unmapped = [n for n in bl if n not in out]
    if unmapped:
        raise env.KatError(f"block layers without a role: {', '.join(unmapped)}")
    return out


def _check_map(m: dict, bl: list[str], dl: list[str], bottom: bool) -> None:
    targets = list(m.values())
    bad = [k for k in m if k not in bl] + [v for v in targets if v not in dl]
    if bad:
        raise env.KatError(f"layer map names layers the boards lack: {', '.join(bad)}")
    outer = {"F.Cu": "B.Cu" if bottom else "F.Cu", "B.Cu": "F.Cu" if bottom else "B.Cu"}
    for a, b in m.items():
        if a in outer and b != outer[a]:
            raise env.KatError(f"layer map: {a} -> {b}, but outer layers go to {outer[a]} on this side")
        if a not in outer and b in ("F.Cu", "B.Cu"):
            raise env.KatError(f"layer map: inner layer {a} -> outer layer {b}")
    dup = sorted({t for t in targets if targets.count(t) > 1})
    if dup:
        raise env.KatError(f"layer map not one-to-one: several layers go to {', '.join(dup)}")


def layer_fn(cmap: dict[str, str], bottom: bool):
    """Every layer name through the copper map; technical layers swap F./B. on the bottom
    side; wildcards (``*.Cu``), ``User.*`` and ``Edge.Cuts`` stay."""
    def f(name: str) -> str:
        if name in cmap:
            return cmap[name]
        if name.endswith(".Cu"):
            return name
        return swap_side(name) if bottom else name
    return f
