"""Minimal lossless S-expression reader/writer for KiCad files.

Lists become Python lists, quoted strings become ``str``, every other atom becomes
``Sym`` (its raw text, so numbers round-trip exactly). Nothing is dropped, so tokens
this module does not understand survive a load/save cycle unchanged.
"""

from __future__ import annotations

from typing import Iterator, Union


class Sym(str):
    """An unquoted atom: keyword, number, or bare word."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"Sym({str(self)!r})"


Node = Union[list, str, Sym]


def num(x: float) -> Sym:
    """Format a coordinate the way KiCad does: no trailing zeros, max 4 decimals."""
    s = f"{round(x, 4):.4f}".rstrip("0").rstrip(".")
    return Sym("0" if s in ("-0", "") else s)


def loads(text: str) -> list:
    tokens = _tokenize(text)
    stack: list[list] = [[]]
    for tok in tokens:
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) < 2:
                raise ValueError("unbalanced ')'")
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(tok)
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("unbalanced parentheses or multiple top-level forms")
    return stack[0][0]


def _tokenize(text: str) -> Iterator[Union[str, Sym]]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c in "()":
            yield c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i >= n:
                raise ValueError("unterminated string")
            i += 1
            yield "".join(buf)  # plain str marks a quoted string
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield Sym(text[i:j])
            i = j


def _atom(x: Node) -> str:
    if isinstance(x, Sym):
        return str(x)
    if isinstance(x, str):
        esc = x.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{esc}"'
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int, float)):
        return str(num(x))
    raise TypeError(f"cannot serialize {type(x).__name__}")


def dumps(node: Node, indent: int = 0) -> str:
    """Serialize with tab indentation; short all-atom lists stay on one line."""
    if not isinstance(node, list):
        return _atom(node)
    if all(not isinstance(c, list) for c in node):
        return "(" + " ".join(_atom(c) for c in node) + ")"
    pad = "\t" * (indent + 1)
    head = []
    rest = list(node)
    while rest and not isinstance(rest[0], list):
        head.append(_atom(rest.pop(0)))
    lines = ["(" + " ".join(head)]
    for child in rest:
        lines.append(pad + dumps(child, indent + 1))
    lines.append("\t" * indent + ")")
    return "\n".join(lines)


# --- tree helpers -----------------------------------------------------------

def head(node: Node) -> str | None:
    return str(node[0]) if isinstance(node, list) and node and isinstance(node[0], Sym) else None


def find(node: list, name: str) -> list | None:
    for c in node:
        if head(c) == name:
            return c
    return None


def find_all(node: list, name: str) -> list[list]:
    return [c for c in node if head(c) == name]


def prop(node: list, key: str) -> list | None:
    """Return the ``(property "key" ...)`` child of a symbol node."""
    for c in node:
        if head(c) == "property" and len(c) > 1 and c[1] == key:
            return c
    return None


def is_yes(node: list | None) -> bool:
    """True for ``(hide yes)``/``(hide)`` style flags; False if absent or ``no``."""
    if node is None:
        return False
    return len(node) == 1 or str(node[1]) == "yes"
