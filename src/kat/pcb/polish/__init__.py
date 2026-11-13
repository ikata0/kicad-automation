"""Topology-preserving route polishing (``kat pcb polish``, ``specs/ROUTE_POLISH_SPEC.md``).

Board-independent core: ``case`` reads and checks the portable input
(``specs/ROUTE_POLISH_INPUT.md``), ``constraints`` holds the hard constraints and the swept
(topology-keeping) moves. Only ``board`` knows KiCad files.
"""

# The solver's own knobs (``kat pcb polish run --option KEY=V``): default and meaning. Kept
# here, free of numpy and torch, so that ``--help`` can list them without the extra.
OPTIONS = {
    "learning_rate": (0.02, "Adam step, mm per vertex (a ceiling; a rejected step halves it)"),
    "settle_iterations": (20, "steps run after a snap, bend or adjust before it is judged"),
    "stagnation_window": (20, "steps without progress before the run counts as stagnated"),
    "stagnation_tolerance": (1e-4, "merit gain below which a step is no progress"),
    "snap_slack": (0.001, "merit a snap may cost and still be kept (credited per snap)"),
    "bend_gain": (0.005, "merit gain a collinear split must bring after settling to be kept"),
    "max_attempts_per_segment": (1, "failed snap attempts per segment before it is left as it is"),
    "max_substeps": (64, "sub-steps a swept move may need before it is refused ('sweep')"),
    "barrier_margin_mm": (0.05, "mm above the clearance where the solver's barrier starts"),
    "adjust_boost": (100.0, "angle-term factor while neighbours make room for a snap"),
    "blocked_bend_after": (5, "steps blocked by one obstacle before a rescue bend there"),
}


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
