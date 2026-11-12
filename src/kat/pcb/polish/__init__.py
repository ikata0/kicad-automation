"""Topology-preserving route polishing (``kat pcb polish``, ``specs/ROUTE_POLISH_SPEC.md``).

Board-independent core: ``case`` reads and checks the portable input
(``specs/ROUTE_POLISH_INPUT.md``), ``constraints`` holds the hard constraints and the swept
(topology-keeping) moves. Only ``board`` knows KiCad files.
"""


def require(module: str):
    """Import ``module`` (numpy, torch, PySide6) or raise ``KatError`` naming the extra that
    provides it."""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        from ...env import KatError
        extra = "polish-gui" if module.startswith("PySide6") else "polish"
        raise KatError(f"route polishing needs {module}: pip install 'kat[{extra}]'") from None
