from collections import Counter
import warnings

import pytest

from kat_test_support import (
    Capability, SetupError, configure_windows_temp, discover, gui_capability, missing,
)

pytest_plugins = ["pytester"]
DEPENDENCIES = pytest.StashKey[dict[str, Capability]]()


def pytest_addoption(parser):
    parser.addoption("--strict-deps", action="store_true",
                     help="Require all non-GUI integration dependencies, including pinned Freerouting.")


def pytest_configure(config):
    config.addinivalue_line("markers", "requires(*names): required test capabilities")
    try:
        configure_windows_temp(config.rootpath, config.option.basetemp)
        capabilities = discover(config.getoption("--strict-deps"))
    except SetupError as e:
        raise pytest.UsageError(f"test dependency setup failed: {e}") from e
    config.stash[DEPENDENCIES] = capabilities
    unavailable = missing(capabilities)
    if unavailable and config.getoption("--strict-deps"):
        message = "Unavailable test dependencies:\n  " + "\n  ".join(unavailable)
        raise pytest.UsageError(message)


def pytest_report_header(config):
    return ["Test dependencies:"] + [
        f"  {name}: {'available' if capability.available else 'unavailable'} ({capability.detail})"
        for name, capability in config.stash[DEPENDENCIES].items() if name != "gui"]


@pytest.fixture(scope="session")
def test_dependencies(pytestconfig):
    """Discovered capabilities for conditional portions of integration tests."""
    return pytestconfig.stash[DEPENDENCIES]


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """kicad_gui tests open KiCad windows: only when asked for (-m kicad_gui)."""
    capabilities = config.stash[DEPENDENCIES]
    gui_requested = "kicad_gui" in (config.option.markexpr or "") and any(
        "kicad_gui" in item.keywords for item in items)
    if gui_requested:
        try:
            capabilities["gui"] = gui_capability()
        except SetupError as e:
            raise pytest.UsageError(f"GUI test dependency setup failed: {e}") from e
    else:
        capabilities["gui"] = Capability(False, "GUI tests are opt-in (-m kicad_gui)")
    unavailable = missing(capabilities)
    if gui_requested and not capabilities["gui"].available:
        unavailable.append("gui: " + capabilities["gui"].detail)
    if unavailable:
        message = "Unavailable test dependencies:\n  " + "\n  ".join(unavailable)
        if config.getoption("--strict-deps"):
            raise pytest.UsageError(message)
        warnings.warn(pytest.PytestWarning(message), stacklevel=2)
    for item in items:
        requirements = {name for marker in item.iter_markers("requires") for name in marker.args}
        unknown = requirements - capabilities.keys()
        if unknown:
            raise pytest.UsageError(f"{item.nodeid}: unknown requirements {sorted(unknown)}")
        if "kicad_gui" in item.keywords:
            requirements.add("gui")
        unavailable = missing(capabilities, sorted(requirements))
        if unavailable:
            item.add_marker(pytest.mark.skip(reason="requires " + "; ".join(unavailable)))


def pytest_terminal_summary(terminalreporter):
    skipped = Counter(
        report.longrepr[2].removeprefix("Skipped: ")
        for report in terminalreporter.stats.get("skipped", [])
        if isinstance(report.longrepr, tuple) and "requires " in report.longrepr[2])
    if skipped:
        terminalreporter.section("Dependency skips")
        for reason, count in sorted(skipped.items()):
            terminalreporter.write_line(f"{count} test(s): {reason}")
