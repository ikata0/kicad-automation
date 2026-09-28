"""Hierarchical schematics: sheet boxes, ports, the instance tree and its annotation.

A sheet box on a parent sheet points to a child file (``Sheetfile``, relative to the
parent file). The child's hierarchical labels are the box's pins. One child file can be
used by several boxes (instances) and by several projects: KiCad keeps each symbol's
reference per project and instance path in the symbol's ``instances``, and ``annotate``
writes them.

Paths come in two forms:

- **name path** ``/CH_A/RC1``: the Sheetname of each level (``/`` for the root)
- **uuid path** ``/<uuid CH_A>/<uuid RC1>``: the sheet boxes' uuids, without the root
  sheet (``""`` for the root). A board footprint's path is ``<uuid path>/<symbol uuid>``;
  a symbol instance path in the schematic is ``/<root uuid><uuid path>``.

Sheet fields other than Sheetname/Sheetfile work as text variables inside the child
(``${R_VAL}``) and are passed down to deeper levels; the child's project
``text_variables`` give the default when it is opened on its own.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import env
from .schematic import Schematic, _sexp, new_uuid, snap, text_width
from .sexpr import Sym, find, find_all, num

__all__ = ["add_port", "add_sheet", "annotate", "instance", "ports", "references", "sheets", "tree"]

PIN_SHAPES = ("input", "output", "bidirectional", "tri_state", "passive")
_OWN_FIELDS = ("Sheetname", "Sheetfile")


def _props(node: list) -> dict[str, str]:
    return {str(p[1]): str(p[2]) for p in find_all(node, "property")}


def sheets(s: Schematic) -> list[dict]:
    """Sheet boxes on one file, in reading order (top to bottom, then left to right):
    ``uuid``, ``name``, ``file`` (as written, relative to the parent), ``path`` (resolved),
    ``at``, ``size``, ``fields`` (parameters) and ``pins`` ({name: {x, y, shape}})."""
    out = []
    for n in find_all(s.tree, "sheet"):
        props = _props(n)
        at, size = find(n, "at"), find(n, "size")
        pins = {}
        for p in find_all(n, "pin"):
            pat = find(p, "at")
            pins[str(p[1])] = {"x": float(pat[1]), "y": float(pat[2]), "shape": str(p[2])}
        out.append({"node": n, "uuid": str(find(n, "uuid")[1]), "name": props.get("Sheetname", ""),
                    "file": props.get("Sheetfile", ""),
                    "path": (s.path.parent / props.get("Sheetfile", "")).resolve(),
                    "at": (float(at[1]), float(at[2])), "size": (float(size[1]), float(size[2])),
                    "fields": {k: v for k, v in props.items() if k not in _OWN_FIELDS},
                    "pins": pins})
    return sorted(out, key=lambda d: (d["at"][1], d["at"][0]))


def add_port(s: Schematic, name: str, x: float, y: float, angle: float = 180,
             shape: str = "passive") -> list:
    """Hierarchical label (a connection point of this sheet to its parent) at (x, y) mm.
    ``angle`` 180: the label text extends to the left of the point, 0: to the right."""
    if shape not in PIN_SHAPES:
        raise env.KatError(f"port shape must be one of {', '.join(PIN_SHAPES)}")
    x, y = snap(x), snap(y)
    just = "right" if angle in (180, 90) else "left"
    esc = name.replace("\\", "\\\\").replace('"', '\\"')
    node = _sexp(f'(hierarchical_label "{esc}" (shape {shape}) (at {num(x)} {num(y)} {num(angle)}) '
                 f'(effects (font (size 1.27 1.27)) (justify {just})) (uuid "{new_uuid()}"))')
    s._insert(node)
    return node


def ports(s: Schematic) -> dict[str, dict]:
    """The hierarchical labels of a file: {name: {x, y, shape}}."""
    out = {}
    for n in find_all(s.tree, "hierarchical_label"):
        at = find(n, "at")
        out[str(n[1])] = {"x": float(at[1]), "y": float(at[2]), "shape": str(find(n, "shape")[1])}
    return out


def add_sheet(s: Schematic, name: str, file: Path | str, x: float, y: float,
              w: float | None = None, h: float | None = None,
              left: list[str] | dict[str, str] = (), right: list[str] | dict[str, str] = (),
              fields: dict[str, str] | None = None) -> dict:
    """Sheet box ``name`` at (x, y) mm pointing to ``file`` (relative to this file's
    folder, as KiCad reads it; created empty if missing). Pins on the left/right edge, 2.54 mm apart, as a list
    of names or {name: shape}. ``fields`` are the instance's parameters (``${NAME}``
    inside). Size defaults to fit the pins. Returns uuid, name, file and pin points."""
    if any(sh["name"] == name for sh in sheets(s)):
        raise env.KatError(f"a sheet named {name!r} exists on {s.path.name}")
    file = Path(file)
    target = file if file.is_absolute() else (s.path.parent / file)
    if not target.exists():
        Schematic.create(target)
    rel = os.path.relpath(target.resolve(), s.path.parent.resolve())
    left = dict(left) if isinstance(left, dict) else {p: "passive" for p in left}
    right = dict(right) if isinstance(right, dict) else {p: "passive" for p in right}
    rows = max(len(left), len(right), 1)
    widest = max([text_width(p) for p in (*left, *right)] or [0])
    w = snap(w if w is not None else max(15.24, 2 * widest + 7.62))
    h = snap(h if h is not None else 2.54 * (rows + 1))
    x, y = snap(x), snap(y)
    u = new_uuid()
    pins, pin_txt = {}, []
    for side, names in (("left", left), ("right", right)):
        for i, (pn, shape) in enumerate(names.items()):
            if shape not in PIN_SHAPES:
                raise env.KatError(f"pin shape must be one of {', '.join(PIN_SHAPES)}")
            px, py = (x if side == "left" else x + w), snap(y + 2.54 * (i + 1))
            pins[pn] = (px, py)
            ang, just = (180, "left") if side == "left" else (0, "right")
            esc = pn.replace('"', '\\"')
            pin_txt.append(f'(pin "{esc}" {shape} (at {num(px)} {num(py)} {ang}) (uuid "{new_uuid()}") '
                           f'(effects (font (size 1.27 1.27)) (justify {just})))')
    extra = "".join(
        f'(property "{k}" "{v}" (at {num(x)} {num(y + h + 2.54 * (i + 2))} 0) '
        f'(effects (font (size 1.27 1.27)) (justify left top)))'
        for i, (k, v) in enumerate((fields or {}).items()))
    page = len(sheets(s)) + 2
    s._insert(_sexp(
        f'(sheet (at {num(x)} {num(y)}) (size {num(w)} {num(h)}) (exclude_from_sim no) (in_bom yes) '
        f'(on_board yes) (dnp no) (fields_autoplaced yes) (stroke (width 0.1524) (type solid)) '
        f'(fill (color 0 0 0 0.0000)) (uuid "{u}") '
        f'(property "Sheetname" "{name}" (at {num(x)} {num(y - 0.7116)} 0) '
        f'(effects (font (size 1.27 1.27)) (justify left bottom))) '
        f'(property "Sheetfile" "{rel}" (at {num(x)} {num(y + h + 0.5846)} 0) '
        f'(effects (font (size 1.27 1.27)) (justify left top))) {extra} {" ".join(pin_txt)} '
        f'(instances (project "{s.project_name}" (path "/{s.root_uuid}" (page "{page}")))))'))
    return {"uuid": u, "name": name, "file": rel, "pins": pins, "size": (w, h)}


def tree(root: Path | str) -> list[dict]:
    """All instances of a hierarchy, depth-first in reading order (the page order):
    ``path`` (name path), ``uuids`` (uuid path), ``file``, ``page``, ``params`` (the
    parameters seen there, inherited ones included) and ``block`` (the file's stem)."""
    root = Path(root).resolve()
    out: list[dict] = []

    def walk(file: Path, path: str, uuids: str, params: dict, stack: tuple) -> None:
        if file in stack:
            raise env.KatError(f"{file.name} contains itself (via {' -> '.join(p.name for p in stack)})")
        if not file.exists():
            raise env.KatError(f"sheet file {file} does not exist")
        s = Schematic(file)
        out.append({"path": path, "uuids": uuids, "file": str(file), "page": len(out) + 1,
                    "params": dict(params), "block": file.stem})
        for sh in sheets(s):
            walk(sh["path"], f"{path.rstrip('/')}/{sh['name']}", f"{uuids}/{sh['uuid']}",
                 {**params, **sh["fields"]}, stack + (file,))

    walk(root, "/", "", {}, ())
    return out


def instance(root: Path | str, path: str) -> dict:
    """One instance of ``tree(root)`` by name path (``/CH_A/RC1``)."""
    want = "/" + path.strip("/")
    for i in tree(root):
        if i["path"] == want:
            return i
    raise env.KatError(f"no sheet instance {want} under {Path(root).name}")


def annotate(root: Path | str, per_page: int = 100) -> dict:
    """Number every instance and write the per-instance data for the root's project:
    pages in reading order (root = 1), references ``prefix + page * per_page + n``
    (R301 = first R on page 3; ``per_page=0``: plain numbering per file). Entries of other
    projects are kept, so a block file keeps its own numbering. Returns the pages and the
    references per instance."""
    from .gui import guard
    root = Path(root).resolve()
    guard(root)
    top = Schematic(root)
    project = top.project_name
    files: dict[Path, Schematic] = {root: top}

    def load(p: Path) -> Schematic:
        if p not in files:
            files[p] = Schematic(p)
        return files[p]

    inst = tree(root)
    by_file: dict[Path, list[dict]] = {}
    for i in inst:
        by_file.setdefault(Path(i["file"]), []).append(i)
    # the sheet box of each (parent file, box uuid): instances listed under the parent's paths
    boxes: dict[tuple[Path, str], list[tuple[str, int]]] = {}
    for i in inst[1:]:
        parent_uuids, box = i["uuids"].rsplit("/", 1)
        parent = next(p for p in inst if p["uuids"] == parent_uuids)
        boxes.setdefault((Path(parent["file"]), box), []).append(
            (f"/{top.root_uuid}{parent_uuids}", i["page"]))
    for (pfile, box), entries in boxes.items():
        node = next(sh["node"] for sh in sheets(load(pfile)) if sh["uuid"] == box)
        _set_instances(node, project, [_sexp(f'(path "{pp}" (page "{pg}"))') for pp, pg in entries])
    refs: dict[str, dict[str, str]] = {}
    for file, uses in by_file.items():
        s = load(file)
        count: dict[str, int] = {}
        syms = sorted(s.symbols(), key=lambda y: (y.place.y, y.place.x))
        for sym in syms:
            prefix = sym.ref.rstrip("0123456789?") or "U"
            count[prefix] = count.get(prefix, 0) + 1
            n = count[prefix]
            paths = []
            for use in uses:
                ref = f"{prefix}{use['page'] * per_page + n if per_page else n}"
                refs.setdefault(use["path"], {})[sym.uuid] = ref
                paths.append(_sexp(f'(path "/{top.root_uuid}{use["uuids"]}" (reference "{ref}") '
                                   f'(unit {sym.unit}))'))
            _set_instances(sym.node, project, paths)
    for s in files.values():
        s.save()
    return {"project": project, "pages": [{k: i[k] for k in ("path", "page", "block")} for i in inst],
            "references": refs}


def _set_instances(node: list, project: str, paths: list) -> None:
    inst = find(node, "instances")
    if inst is None:
        inst = [Sym("instances")]
        node.append(inst)
    keep = [p for p in find_all(inst, "project") if str(p[1]) != project]
    inst[1:] = keep + [[Sym("project"), project, *paths]]


def references(root: Path | str) -> dict[str, dict[str, str]]:
    """Per instance name path: {symbol uuid: reference} as annotated for the root's project."""
    root = Path(root).resolve()
    top = Schematic(root)
    out: dict[str, dict[str, str]] = {}
    for i in tree(root):
        want = f"/{top.root_uuid}{i['uuids']}"
        for sym in Schematic(Path(i["file"])).symbols():
            for proj in find_all(find(sym.node, "instances") or [], "project"):
                if str(proj[1]) != top.project_name:
                    continue
                for p in find_all(proj, "path"):
                    if str(p[1]) == want:
                        out.setdefault(i["path"], {})[sym.uuid] = str(find(p, "reference")[1])
    return out
