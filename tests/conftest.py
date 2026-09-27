import pytest


def pytest_collection_modifyitems(config, items):
    """kicad_gui tests open KiCad windows: only when asked for (-m kicad_gui)."""
    if "kicad_gui" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="opens KiCad windows: run with -m kicad_gui")
    for item in items:
        if "kicad_gui" in item.keywords:
            item.add_marker(skip)
