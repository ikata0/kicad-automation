"""Assembly variants (KiCad 10): declared beside the design, written into the schematic.

One schematic and one board serve several assemblies. KiCad 10 keeps the variant names in
the project (``.kicad_pro`` ``schematic.variants``) and each part's differences from the
base as ``(variant ...)`` children of the symbol's instance path; ``kicad-cli ... --variant
NAME`` exports a variant's BOM and position file.

**Declaration**: ``<design>.variants.json`` next to the design netlist (``kat.skidl_variants``
writes it from a SKiDL script; by hand works as well)::

    {"variants": {"LITE": {"description": "no USB",
        "parts": [{"tag": "usb_esd", "dnp": true},
                  {"ref": "R3", "Value": "10k", "MPN": "RC0603FR-0710KL"},
                  {"block": "CH_*", "tag": "r_gain", "Value": "4k7"}],
        "params": {"/CH_A": {"ra": "6k8"}}}}}

A part entry selects by ``ref``, by ``tag`` (qualified by the block path, as the design
netlist has it: ``CH_A/r_gain``) or by ``block`` (a glob on the block instance path) plus a
block-local ``tag``. Its other keys are the flags ``dnp``, ``in_bom``, ``on_board``,
``in_pos_files``, ``exclude_from_sim`` (true/false) or field values (``Value``, ``MPN``, ...).
``params`` changes block parameters of sheet instances (name path), inherited below them
like the box's own fields; every field that uses the parameter (``${ra}``) gets the
resulting value as an override. KiCad does not expand variant fields of a sheet box, so
this needs parameters drawn as ``${...}`` (hierarchical sheets); a flat drawing has plain
values, so give part overrides there.

**Writing** (``apply``, run by ``kat sync``): only differences from the base are written.
kat remembers what it wrote (``<root>.kat.json``, ``variant_owned``); an override or a
variant name it wrote and the declaration no longer has is removed and reported, anything
written in KiCad by hand stays. Without a declaration file nothing is touched. A field
override needs the field on the base symbol (KiCad drops it from the BOM otherwise), so a
missing one is added empty (``fields_added``).
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

from . import env, hierarchy, netlist, report
from .schematic import Schematic, _sexp
from .sexpr import Sym, find, find_all, head

__all__ = ["apply", "compare", "listing", "load", "precheck", "resolve", "variants_file"]

FLAGS = ("dnp", "exclude_from_sim", "in_bom", "on_board", "in_pos_files")     # KiCad's order
_DEFAULT = {"dnp": False, "exclude_from_sim": False, "in_bom": True, "on_board": True, "in_pos_files": True}
_SELECT = ("ref", "tag", "block")
_REFUSED = ("Reference", "Footprint")
OWNED = "variant_owned"


def variants_file(design: Path | str) -> Path:
    """``<design>.variants.json``: the variant declaration next to a design netlist."""
    return Path(design).with_suffix(".variants.json")


def load(design: Path | str) -> dict | None:
    """The checked variant declaration of a design netlist (None without a file):
    {"variants": {name: {"description", "parts": [entry], "params": {path: {k: v}}}}}."""
    f = variants_file(design)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise env.KatError(f"{f.name}: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("variants"), dict) or set(data) - {"variants"}:
        raise env.KatError(f'{f.name}: expected {{"variants": {{NAME: {{...}}}}}}')
    out = {}
    for name, v in data["variants"].items():
        where = f"{f.name}: variant {name!r}"
        if not name.strip() or not isinstance(v, dict) or set(v) - {"description", "parts", "params"}:
            raise env.KatError(f"{where}: a name, and only description, parts and params")
        parts = []
        for e in v.get("parts", []):
            if not isinstance(e, dict):
                raise env.KatError(f"{where}: a part entry is an object, not {e!r}")
            sel = [k for k in _SELECT if k in e]
            if sel not in (["ref"], ["tag"], ["tag", "block"]):
                raise env.KatError(f"{where}: {e}: select by ref, by tag, or by block and tag")
            entry = {k: str(e[k]).strip("/") for k in sel}
            for k, val in e.items():
                if k in _SELECT:
                    continue
                if k in _REFUSED:
                    raise env.KatError(f"{where}: {k} cannot differ between variants")
                if k in FLAGS:
                    if not isinstance(val, bool):
                        raise env.KatError(f"{where}: {k} is true or false, not {val!r}")
                    entry[k] = val
                elif isinstance(val, (str, int, float)) and not isinstance(val, bool):
                    entry[k] = str(val)
                else:
                    raise env.KatError(f"{where}: field {k} must be text, not {val!r}")
            if len(entry) == len(sel):
                raise env.KatError(f"{where}: {e} changes nothing")
            parts.append(entry)
        params = {}
        for path, kv in (v.get("params") or {}).items():
            if not isinstance(kv, dict) or not kv:
                raise env.KatError(f"{where}: params {path!r} is {{parameter: value}}")
            params["/" + path.strip("/")] = {str(k): str(x) for k, x in kv.items()}
        out[name] = {"description": str(v.get("description", "")), "parts": parts, "params": params}
    return {"variants": out}


def resolve(decl: dict, comps: dict) -> dict[str, dict[str, dict]]:
    """The declared overrides per variant and design reference: {variant: {ref: {key:
    value}}} (flags as bools, fields as text). ``comps``: ``netlist.read_design``'s parts.
    An entry that selects nothing, or two that disagree on one part, is an error."""
    out: dict[str, dict[str, dict]] = {}
    for name, v in decl["variants"].items():
        mine = out.setdefault(name, {})
        for e in v["parts"]:
            if "ref" in e:
                refs = [e["ref"]] if e["ref"] in comps else []
            elif "block" in e:
                refs = [r for r, c in comps.items() if c.block and fnmatch.fnmatchcase(c.block, e["block"])
                        and c.tag == f"{c.block}/{e['tag']}"]
            else:
                refs = [r for r, c in comps.items() if c.tag == e["tag"]]
            if not refs:
                raise env.KatError(f"variant {name}: {e} matches no part of the design")
            for ref in refs:
                cur = mine.setdefault(ref, {})
                for k, val in e.items():
                    if k in _SELECT:
                        continue
                    if k in cur and cur[k] != val:
                        raise env.KatError(f"variant {name}: {ref} {k} is both {cur[k]!r} and {val!r}")
                    cur[k] = val
    return out


def precheck(design: Path | str, flat: bool = False) -> dict | None:
    """Load and resolve ``design``'s declaration before a sync writes anything, so a bad
    one stops it early; ``flat``: block parameters are refused (a flat drawing has plain
    values). Returns the declaration (None without one)."""
    decl = load(design)
    if decl is None:
        return None
    comps, _ = netlist.read_design(Path(design))
    resolve(decl, comps)
    if flat:
        for name, v in decl["variants"].items():
            if v["params"]:
                raise env.KatError(f"variant {name}: params {', '.join(v['params'])}: a flat drawing has plain "
                                   "values, give part overrides")
    return decl


_VAR = re.compile(r"\$\{([^}]+)\}")


def _expand(text: str, params: dict[str, str]) -> str:
    for _ in range(10):
        new = _VAR.sub(lambda m: str(params.get(m.group(1), m.group(0))), text)
        if new == text:
            break
        text = new
    return text


def _flags(node: list) -> dict[str, bool]:
    out = {}
    for k in FLAGS:
        t = find(node, k)
        out[k] = _DEFAULT[k] if t is None else str(t[1]) == "yes"
    return out


def read_variant(node: list) -> dict:
    """A schematic ``(variant ...)`` node as {key: value}: flags as bools, fields as text."""
    out: dict = {}
    for c in node[1:]:
        if not isinstance(c, list):
            continue
        if head(c) in FLAGS:
            out[head(c)] = str(c[1]) == "yes"
        elif head(c) == "field":
            out[str(find(c, "name")[1])] = str(find(c, "value")[1])
    return out


def _write_variant(path_node: list, name: str, values: dict, old: list | None) -> None:
    keep = [c for c in (old or [])[1:] if isinstance(c, list) and head(c) not in (*FLAGS, "field", "name")]
    node = [Sym("variant"), [Sym("name"), name]]
    node += [[Sym(k), Sym("yes" if values[k] else "no")] for k in FLAGS if k in values]
    node += [[Sym("field"), [Sym("name"), k], [Sym("value"), v]] for k, v in values.items() if k not in FLAGS]
    node += keep
    if old is not None:
        idx = path_node.index(old)
        if len(node) > 2:
            path_node[idx] = node
        else:
            del path_node[idx]
    elif len(node) > 2:
        path_node.append(node)


def _project_vars(root: Path) -> dict[str, str]:
    pro = root.with_suffix(".kicad_pro")
    if not pro.exists():
        return {}
    return {str(k): str(v) for k, v in (json.loads(pro.read_text(encoding="utf-8")).get("text_variables") or {}).items()}


def _instance_path(sym_node: list, project: str, want: str) -> list | None:
    for proj in find_all(find(sym_node, "instances") or [], "project"):
        if str(proj[1]) == project:
            for p in find_all(proj, "path"):
                if str(p[1]) == want:
                    return p
    return None


def apply(root: Path | str, design: Path | str, dry_run: bool = False) -> dict:
    """Write ``design``'s variant declaration (``load``) into the schematic ``root`` (flat or
    the root of a hierarchy) and its project. Returns ``names`` (the project's variants),
    ``added`` / ``changed`` / ``removed`` overrides ({ref, variant, key, from, to}),
    ``fields_added`` (empty base fields a field override needs) and ``names_removed``;
    ``{}`` without a declaration. ``dry_run``: report only (that is ``compare``)."""
    decl = load(design)
    if decl is None:
        return {}
    root = Path(root).resolve()
    if not dry_run:
        report.touch(root)
    comps, _ = netlist.read_design(Path(design))
    want_parts = resolve(decl, comps)
    top = Schematic(root)
    project = top.project_name
    textvars = _project_vars(root)
    base = hierarchy.tree(root)
    paths = {i["path"]: i for i in base}
    var_trees = {}
    for name, v in decl["variants"].items():
        for path, kv in v["params"].items():
            inst = paths.get(path)
            if inst is None or path == "/":
                raise env.KatError(f"variant {name}: params {path}: no sheet instance there "
                                   f"({', '.join(p for p in paths if p != '/') or 'the drawing is flat'}); "
                                   "a flat drawing has plain values, give part overrides")
            unknown = sorted(set(kv) - set(inst["fields"]))
            if unknown:
                raise env.KatError(f"variant {name}: {path} has no parameter {', '.join(unknown)} "
                                   f"({', '.join(inst['fields']) or 'none'})")
        var_trees[name] = hierarchy.tree(root, v["params"]) if v["params"] else base
    settings = _settings(root)
    owned = settings.get(OWNED, {"names": [], "parts": {}})
    rep: dict = {"added": [], "changed": [], "removed": [], "fields_added": []}
    new_owned: dict[str, dict[str, list[str]]] = {}
    files: dict[str, Schematic] = {}
    touched: set[str] = set()
    for idx, inst in enumerate(base):
        s = files.setdefault(inst["file"], top if Path(inst["file"]) == root else Schematic(Path(inst["file"])))
        want_path = f"/{top.root_uuid}{inst['uuids']}"
        for sym in s.symbols():
            if sym.ref.startswith("#"):
                continue
            pnode = _instance_path(sym.node, project, want_path)
            if pnode is None:
                continue
            ref = str(find(pnode, "reference")[1])
            key = f"{inst['uuids']}/{sym.uuid}"
            flags = _flags(sym.node)
            props = {str(p[1]): str(p[2]) for p in find_all(sym.node, "property")}
            base_vars = {**textvars, **inst["params"]}
            mine = owned["parts"].get(key, {})
            for name in [*decl["variants"], *(n for n in mine if n not in decl["variants"])]:
                want = dict(want_parts.get(name, {}).get(ref, {}))
                vvars = {**textvars, **var_trees.get(name, base)[idx]["params"]}
                for fname, text in props.items():
                    if "${" in text and fname not in want and _expand(text, vvars) != _expand(text, base_vars):
                        want[fname] = _expand(text, vvars)
                diff = {k: v for k, v in want.items()
                        if (flags[k] if k in FLAGS else _expand(props.get(k, ""), base_vars)) != v}
                old = next((n for n in find_all(pnode, "variant") if str(find(n, "name")[1]) == name), None)
                cur = read_variant(old) if old is not None else {}
                new = dict(cur)
                for k in set(mine.get(name, [])) - set(diff):
                    if k in new:
                        rep["removed"].append({"ref": ref, "variant": name, "key": k, "from": new.pop(k)})
                for k, v in diff.items():
                    if k not in new:
                        rep["added"].append({"ref": ref, "variant": name, "key": k, "to": v})
                    elif new[k] != v:
                        rep["changed"].append({"ref": ref, "variant": name, "key": k, "from": new[k], "to": v})
                    new[k] = v
                if diff:
                    new_owned.setdefault(key, {})[name] = sorted(diff)
                for k in diff:
                    if k not in FLAGS and k not in props:
                        rep["fields_added"].append({"ref": ref, "field": k})
                        props[k] = ""
                        touched.add(inst["file"])
                        if not dry_run:
                            last = max(i for i, c in enumerate(sym.node) if head(c) == "property")
                            esc = k.replace("\\", "\\\\").replace('"', '\\"')
                            sym.node.insert(last + 1, _sexp(f'(property "{esc}" "" (at {sym.place.x} {sym.place.y} 0) '
                                                             f'(hide yes) (effects (font (size 1.27 1.27))))'))
                if new != cur:
                    touched.add(inst["file"])
                    if not dry_run:
                        _write_variant(pnode, name, new, old)
    # the project's variant list
    pro = root.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8")) if pro.exists() else {}
    listed = {e["name"]: e for e in (data.get("schematic") or {}).get("variants") or [] if e.get("name")}
    gone = [n for n in owned.get("names", []) if n not in decl["variants"] and n in listed]
    names = [e for n, e in listed.items() if n not in gone and n not in decl["variants"]]
    for n, v in decl["variants"].items():
        names.append({"name": n, **({"description": v["description"]} if v["description"] else {})})
    names_changed = names != list(listed.values())
    rep["names"] = [e["name"] for e in names]
    rep["names_removed"] = gone
    if not dry_run:
        for f, s in files.items():
            if f in touched:
                s.save()
        if names_changed:
            data.setdefault("schematic", {})["variants"] = names
            pro.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        from .syncer_hier import remember
        remember(root, **{OWNED: {"names": sorted(decl["variants"]), "parts": new_owned}})
    rep["names_differ"] = names_changed
    for k in ("added", "changed", "removed", "fields_added"):      # one entry per part, not per unit
        rep[k] = [dict(x) for x in dict.fromkeys(tuple(sorted(e.items())) for e in rep[k])]
    return rep


def _settings(root: Path) -> dict:
    from .syncer_hier import settings_file
    f = settings_file(Path(root))
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def compare(root: Path | str, design: Path | str) -> dict | None:
    """The schematic's variants against ``design``'s declaration, nothing written: ``ok``
    and the differences ``apply`` would write (``added`` / ``changed`` / ``removed``, project
    ``names``); None without a declaration."""
    rep = apply(root, design, dry_run=True)
    if not rep:
        return None
    diffs = {k: rep[k] for k in ("added", "changed", "removed", "fields_added")}
    ok = not any(diffs.values()) and not rep["names_differ"]
    return {"ok": ok, **diffs, **({"names": rep["names"]} if rep["names_differ"] else {})}


def listing(path: Path | str) -> dict:
    """The variants of a schematic (root, hierarchy included) or board: ``names``
    [{name, description}] and ``parts`` {ref: {variant: {key: value}}} (the differences
    from the base KiCad keeps)."""
    path = Path(path)
    report.touch(path)
    parts: dict[str, dict] = {}
    if path.suffix == ".kicad_pcb":
        from .pcb.board import Board
        b = Board(path)
        names = [{"name": str(find(v, "name")[1]),
                  **({"description": str(find(v, "description")[1])} if find(v, "description") else {})}
                 for v in find_all(find(b.tree, "variants") or [], "variant")]
        for f in b.footprints():
            for v in find_all(f.node, "variant"):
                vals = {}
                for c in v[1:]:
                    if isinstance(c, list) and head(c) in ("dnp", "exclude_from_bom", "exclude_from_pos_files"):
                        vals[head(c)] = len(c) < 2 or str(c[1]) in ("yes", "true")
                    elif isinstance(c, list) and head(c) == "field":
                        vals[str(find(c, "name")[1])] = str(find(c, "value")[1])
                parts.setdefault(f.ref, {})[str(find(v, "name")[1])] = vals
        return {"file": str(path), "names": names, "parts": parts}
    root = path.resolve()
    top = Schematic(root)
    pro = root.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8")) if pro.exists() else {}
    names = list((data.get("schematic") or {}).get("variants") or [])
    for inst in hierarchy.tree(root):
        s = Schematic(Path(inst["file"]))
        want = f"/{top.root_uuid}{inst['uuids']}"
        for sym in s.symbols():
            pnode = _instance_path(sym.node, top.project_name, want)
            if pnode is None:
                continue
            for v in find_all(pnode, "variant"):
                parts.setdefault(str(find(pnode, "reference")[1]), {})[str(find(v, "name")[1])] = read_variant(v)
    return {"file": str(path), "names": names, "parts": parts}
