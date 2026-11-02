"""Effective clearance between two nets, resolved the way KiCad's DRC does it (checked against
it in ``tests/test_pcb_rules.py``, on KiCad 10.0.6):

- the larger clearance of the two nets' classes, at least the board minimum;
- unless a custom rule in the ``.kicad_dru`` matches: then the last matching rule's value
  counts, whichever is larger, even below the classes and the board minimum. A rule matches
  if its condition holds with the two items either way round.

Conditions are evaluated for what is known from two nets: ``A.NetName``, ``A.NetClass``,
``A.hasNetclass('X')``, ``==`` and ``!=`` (with wildcards), ``&&``, ``||``, ``!``. Anything
else (``A.Type``, ``A.intersectsCourtyard(...)``) depends on the items, so such a rule is
*unresolved*: reported rather than guessed, and counted by ``worst_case``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from ..sexpr import find, find_all, head, loads

UNITS = {"mm": 1.0, "mil": 0.0254, "um": 0.001, "in": 25.4}


def _mm(token) -> float | None:
    m = re.fullmatch(r"([-\d.]+)\s*(mm|mil|um|in)?", str(token))
    return float(m.group(1)) * UNITS[m.group(2) or "mm"] if m else None


@dataclass
class Rule:
    """One custom DRC rule with a clearance constraint: ``name``, ``layer`` (or None),
    ``condition`` (or "") and ``clearance`` in mm."""
    name: str
    layer: str | None
    condition: str
    clearance: float


def parse(text: str) -> list[Rule]:
    """The clearance rules of ``.kicad_dru`` text, in file order."""
    if not text.strip():
        return []
    out = []
    for n in loads(f"(dru {text})")[1:]:
        if head(n) != "rule":
            continue
        for c in find_all(n, "constraint"):
            if len(c) < 2 or str(c[1]) != "clearance":
                continue
            value = find(c, "min")
            mm = _mm(value[1]) if value is not None and len(value) > 1 else None
            if mm is None:
                continue
            cond, lay = find(n, "condition"), find(n, "layer")
            out.append(Rule(str(n[1]), str(lay[1]) if lay is not None else None,
                            str(cond[1]) if cond is not None else "", mm))
    return out


_TOKEN = re.compile(r"\s*(&&|\|\||==|!=|!|\(|\)|'[^']*'|\"[^\"]*\"|[AB]\.\w+|\w+)")


def _tokens(text: str) -> list[str]:
    out, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            if text[i:].strip():
                out.append("?")
            break
        out.append(m.group(1))
        i = m.end()
    return out


class _Eval:
    """A condition over two nets in three values: True, False or None (depends on the items,
    or cannot be read). ``net``/``cls``: role ``A``/``B`` -> net name, net class."""

    def __init__(self, tokens: list[str], net: dict, cls: dict):
        self.t, self.i, self.net, self.cls = tokens, 0, net, cls

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> str | None:
        tok = self.peek()
        self.i += 1
        return tok

    def expr(self):
        v = self.conj()
        while self.peek() == "||":
            self.take()
            w = self.conj()
            v = True if v is True or w is True else None if v is None or w is None else False
        return v

    def conj(self):
        v = self.unary()
        while self.peek() == "&&":
            self.take()
            w = self.unary()
            v = False if v is False or w is False else None if v is None or w is None else True
        return v

    def unary(self):
        if self.peek() == "!":
            self.take()
            v = self.unary()
            return None if v is None else not v
        if self.peek() == "(":
            self.take()
            v = self.expr()
            if self.take() != ")":
                raise ValueError("unbalanced")
            return v
        return self.atom()

    def atom(self):
        tok = self.take()
        if tok is None or not re.fullmatch(r"[AB]\.\w+", tok):
            raise ValueError(f"cannot read {tok!r}")
        role, prop = tok.split(".")
        if self.peek() == "(":                               # a function of the item
            self.take()
            args = []
            while self.peek() not in (")", None):
                args.append(self.take())
            self.take()
            if prop.lower() == "hasnetclass" and len(args) == 1:
                return fnmatch.fnmatchcase(self.cls[role], args[0].strip("'\""))
            return None
        if self.peek() in ("==", "!="):
            op, value = self.take(), self.take()
            if value is None or value[0] not in "'\"":
                return None
            have = {"NetName": self.net, "NetClass": self.cls}.get(prop)
            if have is None:
                return None
            hit = fnmatch.fnmatchcase(have[role], value.strip("'\""))
            return hit if op == "==" else not hit
        return None


def matches(rule: Rule, a: str, b: str, class_a: str, class_b: str, layer: str | None) -> bool | None:
    """Does ``rule`` hold for nets ``a`` and ``b`` (either way round) on ``layer``? None: it
    depends on more than the nets. ``layer`` None: any layer."""
    known_layer = True
    if rule.layer is not None:
        if layer is None:
            known_layer = False
        elif rule.layer == "outer":
            if not layer.startswith(("F.", "B.")):
                return False
        elif rule.layer == "inner":
            if not layer.startswith("In"):
                return False
        elif not fnmatch.fnmatchcase(layer, rule.layer):
            return False
    results = []
    for na, nb, ca, cb in ((a, b, class_a, class_b), (b, a, class_b, class_a)):
        if not rule.condition.strip():
            results.append(True)
            continue
        try:
            e = _Eval(_tokens(rule.condition), {"A": na, "B": nb}, {"A": ca, "B": cb})
            v = e.expr()
            results.append(v if e.peek() is None else None)
        except ValueError:
            results.append(None)
    hit = True if True in results else None if None in results else False
    if hit is True and not known_layer:
        return None
    return hit


def resolve(rules: list[Rule], a: str, b: str, class_a: str, class_b: str, class_value: float,
            minimum: float, layer: str | None = None) -> dict:
    """The clearance between nets as DRC takes it: ``clearance`` (mm), ``source`` ("class",
    "board minimum" or "rule NAME"), ``unresolved`` (later rules that may apply, depending on
    the items) and ``worst_case`` (the largest value they could make it)."""
    value = max(class_value, minimum)
    source = "board minimum" if minimum > class_value else "class"
    open_rules: list[Rule] = []
    for r in rules:
        hit = matches(r, a, b, class_a, class_b, layer)
        if hit:
            value, source, open_rules = r.clearance, f"rule {r.name}", []
        elif hit is None:
            open_rules.append(r)
    return {"clearance": value, "source": source, "unresolved": [r.name for r in open_rules],
            "worst_case": max([value] + [r.clearance for r in open_rules])}
