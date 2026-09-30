"""Regression coverage for pytest's own setup and missing-dependency behavior."""

import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

import kat_test_support as support


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    for prefix in ("KICAD10", "KICAD9", "KICAD"):
        for stem in ("SYMBOL", "FOOTPRINT"):
            monkeypatch.delenv(f"{prefix}_{stem}_DIR", raising=False)
    monkeypatch.delenv("KICAD_CLI", raising=False)
    monkeypatch.delenv("KAT_FREEROUTING", raising=False)
    symbols, footprints = tmp_path / "symbols", tmp_path / "footprints"
    symbols.mkdir()
    (symbols / "Device.kicad_sym").write_text('(kicad_symbol_lib (version 20241209))',
                                             encoding="utf-8")
    pretty = footprints / "Test.pretty"
    pretty.mkdir(parents=True)
    (pretty / "Test.kicad_mod").write_text('(footprint "Test")', encoding="utf-8")
    monkeypatch.setattr(support.env, "stock_dir",
                        lambda kind: symbols if kind == "symbols" else footprints)
    monkeypatch.setattr(support.env, "kicad_cli", lambda: sys.executable)
    monkeypatch.setattr(support, "probe", lambda argv: "10.0.6")
    monkeypatch.setattr(support, "python_package",
                        lambda name: support.Capability(True, name))
    monkeypatch.setattr(support, "routing",
                        lambda strict: support.Capability(True, "routing"))
    return symbols, footprints


def test_discovery_sets_environment_before_child_imports(installation):
    capabilities = support.discover()
    assert not support.missing(capabilities)
    assert os.environ["KICAD_CLI"] == str(Path(sys.executable).resolve())
    assert Path(os.environ["KICAD10_SYMBOL_DIR"]) == installation[0]
    child = subprocess.run([sys.executable, "-c",
                            "import os; print(os.environ['KICAD10_SYMBOL_DIR'])"],
                           check=True, capture_output=True, text=True)
    assert Path(child.stdout.strip()) == installation[0]


def test_explicit_library_override_is_preserved(installation, monkeypatch):
    monkeypatch.setenv("KICAD10_SYMBOL_DIR", str(installation[0]))
    support.discover()
    assert Path(os.environ["KICAD10_SYMBOL_DIR"]) == installation[0]


def test_older_library_override_is_exposed_to_skidl(installation, monkeypatch):
    monkeypatch.setenv("KICAD9_SYMBOL_DIR", str(installation[0]))
    support.discover()
    assert os.environ["KICAD9_SYMBOL_DIR"] == os.environ["KICAD10_SYMBOL_DIR"]


@pytest.mark.parametrize("version", ["9.0.6", "11.0.0", "unparseable"])
def test_wrong_kicad_version_is_an_error(installation, monkeypatch, version):
    monkeypatch.setattr(support, "probe", lambda argv: version)
    with pytest.raises(support.SetupError, match="require KiCad 10"):
        support.discover()


def test_invalid_cli_override_is_an_error(installation, monkeypatch):
    monkeypatch.setattr(support.env, "kicad_cli", lambda: "nonexistent-explicit-cli")
    with pytest.raises(support.SetupError, match="not an executable"):
        support.discover()


def test_missing_kicad_is_a_capability_not_a_failure(installation, monkeypatch):
    def absent():
        raise support.env.KatError("kicad-cli not found; install KiCad 10")
    monkeypatch.setattr(support.env, "kicad_cli", absent)
    result = support.discover()
    assert not result["kicad"].available
    assert result["skidl"].available


def test_discovery_errors_are_not_hidden(installation, monkeypatch):
    def broken():
        raise support.env.KatError("cannot read KiCad's Windows registration")
    monkeypatch.setattr(support.env, "kicad_cli", broken)
    with pytest.raises(support.SetupError, match="registration"):
        support.discover()


@pytest.mark.parametrize("kind", ["symbols", "footprints"])
def test_missing_libraries_and_invalid_override_are_distinct(installation, monkeypatch, kind):
    monkeypatch.setattr(support.env, "stock_dir", lambda kind: None)
    assert not support.libraries(kind).available
    monkeypatch.setenv(f"KICAD10_{support.env._STOCK[kind]}_DIR", "missing")
    with pytest.raises(support.SetupError, match="missing directory"):
        support.libraries(kind)


def test_corrupt_library_is_an_error(installation):
    (installation[0] / "Device.kicad_sym").write_bytes(b"\xff")
    with pytest.raises(support.SetupError, match="cannot read KiCad library"):
        support.libraries("symbols")


@pytest.mark.parametrize("pymupdf,rsvg,available", [
    (False, False, False), (True, False, True), (False, True, True), (True, True, True)])
def test_rendering_alternatives(installation, monkeypatch, pymupdf, rsvg, available):
    original = support.shutil.which
    monkeypatch.setattr(support.shutil, "which",
                        lambda name: ("rsvg-convert" if rsvg else None)
                        if name == "rsvg-convert" else original(name))
    monkeypatch.setattr(support, "python_package",
                        lambda name: support.Capability(pymupdf if name == "pymupdf" else True, name))
    assert support.discover()["render"].available is available


def test_missing_python_package(monkeypatch):
    monkeypatch.setattr(support.importlib.util, "find_spec", lambda name: None)
    assert not support.python_package("skidl").available


def test_broken_python_package_is_an_error(monkeypatch):
    monkeypatch.setattr(support.importlib.util, "find_spec", lambda name: object())
    def broken(argv):
        raise support.SetupError("package import failed")
    monkeypatch.setattr(support, "probe", broken)
    with pytest.raises(support.SetupError, match="import failed"):
        support.python_package("skidl")


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
def test_probe_failures_are_setup_errors(monkeypatch, failure):
    def run(*args, **kwargs):
        assert kwargs["timeout"] == 30
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 30)
        if failure == "missing":
            raise FileNotFoundError("missing")
        return subprocess.CompletedProcess(args[0], 1, b"", b"broken")
    monkeypatch.setattr(support.subprocess, "run", run)
    with pytest.raises(support.SetupError):
        support.probe(["test-tool"])


@pytest.fixture
def route_installation(tmp_path, monkeypatch):
    monkeypatch.delenv("KAT_FREEROUTING", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(support.shutil, "which", lambda name: "java")
    monkeypatch.setattr(support, "probe", lambda argv: 'openjdk version "21.0.8"')
    return tmp_path / "kat" / f"freerouting-{support.router.VERSION}.jar"


def write_jar(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: app.Main\n")
        archive.writestr("app/Main.class", b"\xca\xfe\xba\xbe")


def test_absent_router_is_not_downloaded(route_installation):
    assert not support.routing(False).available
    assert not route_installation.parent.exists()


def test_invalid_router_override_fails(route_installation, monkeypatch):
    monkeypatch.setenv("KAT_FREEROUTING", str(route_installation))
    with pytest.raises(support.SetupError, match="missing jar"):
        support.routing(False)


def test_router_version_checksum_and_java(route_installation, monkeypatch):
    write_jar(route_installation)
    assert support.routing(False).available
    with pytest.raises(support.SetupError, match="pinned Freerouting"):
        support.routing(True)
    monkeypatch.setattr(support.router, "SHA256",
                        support.hashlib.sha256(route_installation.read_bytes()).hexdigest())
    assert support.routing(True).available
    monkeypatch.setattr(support.shutil, "which", lambda name: None)
    assert not support.routing(False).available


@pytest.mark.parametrize("version", ['version "17.0.1"', "unparseable"])
def test_wrong_java_version_fails(route_installation, monkeypatch, version):
    monkeypatch.setattr(support, "probe", lambda argv: version)
    with pytest.raises(support.SetupError, match="Java 21"):
        support.routing(False)


def test_corrupt_router_is_an_error(route_installation):
    route_installation.parent.mkdir()
    route_installation.write_bytes(b"not a jar")
    with pytest.raises(support.SetupError, match="cannot read Freerouting"):
        support.routing(False)


def test_router_missing_main_class_is_an_error(route_installation):
    route_installation.parent.mkdir()
    with zipfile.ZipFile(route_installation, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Main-Class: app.Missing\n")
    with pytest.raises(support.SetupError, match="cannot read Freerouting"):
        support.routing(False)


def test_windows_temp_keeps_pytest_numbering_and_explicit_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    (tmp_path / ".venv").mkdir()
    support.configure_windows_temp(tmp_path, None, "win32", Path("Z:\\different-drive"))
    parent = Path(os.environ["PYTEST_DEBUG_TEMPROOT"])
    assert parent == tmp_path / ".venv" / "pytest-tmp"
    assert list(parent.iterdir()) == []
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT")
    support.configure_windows_temp(tmp_path, "explicit", "win32", Path("Z:\\different-drive"))
    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ


def test_temp_without_local_venv_and_linux_default(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    support.configure_windows_temp(tmp_path, None, "linux", Path("Z:\\different-drive"))
    assert "PYTEST_DEBUG_TEMPROOT" not in os.environ
    support.configure_windows_temp(tmp_path, None, "win32", Path("Z:\\different-drive"))
    assert Path(os.environ["PYTEST_DEBUG_TEMPROOT"]) == tmp_path / ".pytest-tmp"


def configure_nested(pytester, unavailable=()):
    capabilities = {name: support.Capability(name not in unavailable, name)
                    for name in (*support.REQUIRED, "pymupdf", "gui")}
    source = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    pytester.makepyfile(kat_test_support=Path(support.__file__).read_text(encoding="utf-8"))
    pytester.makeconftest(
        "import kat_test_support\n"
        f"kat_test_support.discover = lambda *args: {capabilities!r}\n"
        f"kat_test_support.gui_capability = lambda: {capabilities['gui']!r}\n"
        "from kat_test_support import Capability\n"
        + source)


def test_pytest_lifecycle_warns_skips_and_runs_independent_tests(pytester):
    configure_nested(pytester, ("kicad", "routing", "gui"))
    pytester.makepyfile("""
        import pytest
        def test_independent():
            pass
        @pytest.mark.requires("kicad")
        def test_integration():
            pytest.fail("should have skipped")
        @pytest.mark.kicad_gui
        def test_gui():
            pytest.fail("should have skipped")
    """)
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1, skipped=2)
    assert "Unavailable test dependencies" in result.stdout.str()
    result.stdout.fnmatch_lines(["*Dependency skips*", "*requires kicad*"])


def test_strict_pytest_lists_missing_dependencies(pytester):
    configure_nested(pytester, support.REQUIRED)
    pytester.makepyfile("def test_independent(): pass")
    result = pytester.runpytest_subprocess("--strict-deps", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    for name in support.REQUIRED:
        assert f"{name}:" in result.stderr.str()


def test_complete_strict_pytest_environment_succeeds(pytester):
    configure_nested(pytester)
    pytester.makepyfile("def test_independent(): pass")
    pytester.runpytest_subprocess("--strict-deps", "-q").assert_outcomes(passed=1)


def test_unknown_requirement_is_not_silently_ignored(pytester):
    configure_nested(pytester)
    pytester.makepyfile("""
        import pytest
        @pytest.mark.requires("typo")
        def test_wrong_marker(): pass
    """)
    result = pytester.runpytest_subprocess("-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "unknown requirements" in result.stderr.str()


def test_pytest_sets_environment_before_test_module_import(pytester, monkeypatch):
    symbols, footprints = pytester.path / "symbols", pytester.path / "footprints"
    symbols.mkdir()
    footprints.mkdir()
    (symbols / "Device.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")
    (footprints / "Test.pretty").mkdir()
    (footprints / "Test.pretty" / "Test.kicad_mod").write_text('(footprint "Test")',
                                                            encoding="utf-8")
    for name in ("KICAD_CLI", "KICAD10_SYMBOL_DIR", "KICAD10_FOOTPRINT_DIR"):
        monkeypatch.delenv(name, raising=False)
    source = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    pytester.makepyfile(kat_test_support=Path(support.__file__).read_text(encoding="utf-8"))
    pytester.makeconftest(
        "import sys\n"
        "from pathlib import Path\n"
        "import kat_test_support as support\n"
        "support.env.kicad_cli = lambda: sys.executable\n"
        f"support.env.stock_dir = lambda kind: Path({str(symbols)!r}) if kind == "
        f"'symbols' else Path({str(footprints)!r})\n"
        "support.probe = lambda argv: '10.0.6'\n"
        "support.python_package = lambda name: support.Capability(True, name)\n"
        "support.routing = lambda strict: support.Capability(True, 'routing')\n"
        + source)
    pytester.makepyfile(f"""
        import os
        import sys
        from pathlib import Path
        import subprocess
        assert Path(os.environ["KICAD_CLI"]) == Path(sys.executable).resolve()
        assert Path(os.environ["KICAD10_SYMBOL_DIR"]) == Path({str(symbols)!r})
        def test_child_environment():
            child = subprocess.run([sys.executable, "-c",
                "import os; assert os.environ['KICAD10_SYMBOL_DIR']"],
                check=True, capture_output=True)
    """)
    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)
    assert "KICAD_CLI" not in os.environ
    assert "KICAD10_SYMBOL_DIR" not in os.environ


def test_strict_gui_is_required_only_when_selected(pytester):
    configure_nested(pytester, ("gui",))
    pytester.makepyfile("""
        import pytest
        def test_independent(): pass
        @pytest.mark.kicad_gui
        def test_gui(): pass
    """)
    pytester.runpytest_subprocess("--strict-deps", "-q").assert_outcomes(passed=1, skipped=1)
    pytester.runpytest_subprocess("--strict-deps", "-m", "not kicad_gui", "-q").assert_outcomes(
        passed=1, deselected=1)
    result = pytester.runpytest_subprocess("--strict-deps", "-m", "kicad_gui", "-q")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "gui:" in result.stderr.str()


def test_repository_runs_independent_tests_without_optional_dependencies():
    root = Path(__file__).resolve().parent.parent
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(root / 'tests')!r})\n"
        "import kat_test_support as support\n"
        "support.discover = lambda *args: {name: support.Capability(False, 'not installed') "
        "for name in (*support.REQUIRED, 'pymupdf', 'gui')}\n"
        "import pytest\n"
        "raise SystemExit(pytest.main(['tests', '-q', "
        "'--ignore=tests/test_test_setup.py']))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=root,
                            capture_output=True, timeout=120)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        "utf-8", errors="backslashreplace")
    assert b"Dependency skips" in result.stdout
    assert b"passed" in result.stdout and b"skipped" in result.stdout


def test_pytest_temp_run_directories_are_isolated_and_overrides_win(pytester, monkeypatch):
    configure_nested(pytester)
    parent = pytester.path / "temporary-parent"
    parent.mkdir()
    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(parent))
    pytester.makepyfile("""
        def test_temporary(tmp_path):
            (tmp_path / "owned.txt").write_text("test")
    """)
    processes = [subprocess.Popen([sys.executable, "-m", "pytest", "-q"],
                                  cwd=pytester.path, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for _ in range(2)]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stdout + stderr
    runs = [run for user in parent.glob("pytest-of-*")
            for run in user.glob("pytest-[0-9]*")]
    assert len(runs) == 2
    explicit = pytester.path / "explicit-temp"
    pytester.run(sys.executable, "-m", "pytest", "-q",
                f"--basetemp={explicit}").assert_outcomes(passed=1)
    assert list(explicit.rglob("owned.txt"))
