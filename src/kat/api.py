"""API lookup for agents: ``kat api [NAME]`` prints signatures and docstrings straight
from the code, so the documentation cannot drift from it.

The public surface is ``kat.__all__`` and ``kat.pcb.__all__``; for classes, their public
methods and properties (mixins included)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from . import env

ROOTS = ("kat", "kat.pcb")


def _root(name: str):
    import importlib
    return importlib.import_module(name)


def _members(obj) -> list[tuple[str, object]]:
    """Public members: ``__all__`` of a module, public methods/properties of a class."""
    if inspect.ismodule(obj):
        return [(n, getattr(obj, n)) for n in getattr(obj, "__all__", [])]
    out = []
    for n in sorted(dir(obj)):
        if n.startswith("_"):
            continue
        attr = inspect.getattr_static(obj, n)
        if isinstance(attr, (staticmethod, classmethod)):
            attr = attr.__func__
        if inspect.isfunction(attr) or isinstance(attr, property):
            out.append((n, attr))
    return out


def surface() -> dict[str, object]:
    """Full dotted name -> object for everything ``kat api`` knows."""
    found: dict[str, object] = {}
    for root in ROOTS:
        mod = _root(root)
        found[root] = mod
        for n, obj in _members(mod):
            full = f"{root}.{n}"
            if inspect.ismodule(obj):
                full = obj.__name__
            if full in found:
                continue
            found[full] = obj
            if inspect.isclass(obj):
                for m, attr in _members(obj):
                    found[f"{full}.{m}"] = attr
    return found


def resolve(name: str) -> list[str]:
    """Full names matching ``name``: exact (``kat.pcb.Board.keepout``), with ``kat.``
    added (``pcb.Board.keepout``), or as a dotted suffix (``Board.keepout``, ``keepout``)."""
    names = surface()
    name = name.strip().removeprefix("kat.") if name.strip() != "kat" else "kat"
    for cand in (name, f"kat.{name}"):
        if cand in names:
            return [cand]
    return sorted(n for n in names if n.endswith("." + name))


def _summary(obj, width: int = 110) -> str:
    """First sentence of the docstring, shortened to ``width`` characters."""
    doc = inspect.getdoc(obj) or ""
    first = doc.split("\n\n")[0].replace("\n", " ")
    m = re.search(r"\.\s+(?=[A-Z`])", first)
    first = first[:m.start() + 1] if m else first
    return first if len(first) <= width else first[:width - 3].rstrip() + "..."


class _Ann:
    """An annotation shown as written (``from __future__ import annotations`` keeps them
    as strings, which inspect would print quoted)."""
    def __init__(self, text):
        self.text = text if isinstance(text, str) else inspect.formatannotation(text)

    def __repr__(self):
        return self.text


def _signature(name: str, obj) -> str:
    short = name.rsplit(".", 1)[-1]
    if isinstance(obj, property):
        return f"{short}  (property)"
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return short
    empty = inspect.Parameter.empty
    sig = str(sig.replace(
        parameters=[p.replace(annotation=_Ann(p.annotation)) if p.annotation is not empty else p
                    for p in sig.parameters.values()],
        return_annotation=(_Ann(sig.return_annotation)
                           if sig.return_annotation is not empty else empty)))
    if sig.startswith("(self, "):
        sig = "(" + sig[len("(self, "):]
    elif sig.startswith("(self)"):
        sig = "()" + sig[len("(self)"):]
    return f"{'class ' if inspect.isclass(obj) else ''}{short}{sig}"


def _where(obj) -> str:
    try:
        target = obj.fget if isinstance(obj, property) else obj
        f = Path(inspect.getsourcefile(target)).resolve()
        base = Path(__file__).resolve().parents[1]
        f = f.relative_to(base) if f.is_relative_to(base) else f
        return str(f) if inspect.ismodule(target) else f"{f}:{inspect.getsourcelines(target)[1]}"
    except (TypeError, OSError):
        return ""


def describe(name: str) -> dict:
    """What ``kat api NAME`` prints, as data: ``name``, ``kind``, ``signature``, ``doc``,
    ``where`` and, for modules and classes, ``members`` [{name, signature, summary}]."""
    full = name
    obj = surface()[full]
    kind = ("module" if inspect.ismodule(obj) else "class" if inspect.isclass(obj)
            else "property" if isinstance(obj, property) else "function")
    out = {"name": full, "kind": kind,
           "signature": "" if kind == "module" else _signature(full, obj),
           "doc": inspect.getdoc(obj) or "",
           "where": _where(obj)}
    if kind in ("module", "class"):
        out["members"] = [{"name": n, "signature": _signature(n, o), "summary": _summary(o)}
                          for n, o in _members(obj)]
    return out


def lookup(name: str | None = None) -> dict:
    """``kat api`` data: without ``name`` the overview of ``kat`` and ``kat.pcb``; with a
    unique ``name`` its description; with several matches ``matches``."""
    if not name:
        return {"overview": [describe(r) for r in ROOTS]}
    found = resolve(name)
    if not found:
        raise env.KatError(f"nothing named {name!r} in kat's API (kat api lists it)")
    if len(found) > 1:
        return {"matches": [{"name": n, "summary": _summary(surface()[n])} for n in found]}
    out = describe(found[0])
    also = [n for n in surface() if n.endswith("." + name.removeprefix("kat.")) and n != found[0]]
    if also:
        out["see_also"] = sorted(also)
    return out


def text(res: dict) -> str:
    """Plain-text rendering of ``lookup``'s result."""
    if "overview" in res:
        parts = []
        for r in res["overview"]:
            parts.append(f"{r['name']}: {r['doc'].splitlines()[0] if r['doc'] else ''}")
            parts += [f"  {m['signature']}\n      {m['summary']}" if m["summary"] else f"  {m['signature']}"
                      for m in r["members"]]
        return "\n".join(parts) + \
            "\n\nDetails: kat api NAME (e.g. kat api Board.keepout, kat api kat.pcb.route)"
    if "matches" in res:
        return "Several matches - give more of the name:\n" + "\n".join(
            f"  {m['name']:40s} {m['summary']}" for m in res["matches"])
    head = res["signature"] or res["name"]
    lines = [f"{res['name']}  ({res['kind']}{', ' + res['where'] if res['where'] else ''})"]
    if res["kind"] != "module":
        lines += ["", "    " + head]
    if res["doc"]:
        lines += [""] + ["    " + l if l else "" for l in res["doc"].splitlines()]
    if res.get("see_also"):
        lines += ["", "See also: " + ", ".join(res["see_also"])]
    if res.get("members"):
        lines += ["", "Members:"]
        for m in res["members"]:
            lines.append(f"  {m['signature']}")
            if m["summary"]:
                lines.append(f"      {m['summary']}")
    return "\n".join(lines)
