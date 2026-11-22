"""Warning scopes preserve ambient mutations and live filter callbacks."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run_pair(
    tmp_path: Path, prepare: str, persistent: bool, *, pytest_args: tuple[str, ...] = ()
) -> None:
    outcomes = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import pytest
import warnings

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    # The warnings plugin's configure scope survives until config cleanup.
    config._ambient_warning_filters = warnings.filters
    config._ambient_warning_filters.append(('ignore', None, UserWarning, None, 0))
""",
        )
        project.write(
            "test_case.py",
            f"""import warnings

def test_first(request):
    assert request.config._ambient_warning_filters is not warnings.filters
{prepare}

def test_second():
    warnings.warn('ambient-filter sentinel', UserWarning)
""",
        )
        arguments = ("-q", "--junitxml=result.xml", *pytest_args)
        environment: dict[str, str | None] = {"PYTHONWARNINGS": None}
        native_record = project.root / "native.json"
        if engine == "pytest":
            result = project.run_pytest(*arguments, load_xdist=False, env=environment)
        else:
            environment["PYTEST_NATIVE_RESULT"] = str(native_record)
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                *arguments,
                env=environment,
            )
        transcript = result.stdout + result.stderr
        report = project.root / "result.xml"
        assert report.exists(), transcript
        cases = [
            (
                case.attrib["name"],
                tuple(child.tag for child in case if child.tag in {"failure", "error", "skipped"}),
            )
            for case in ET.parse(report).getroot().iter("testcase")
        ]
        outcomes[engine] = (result.returncode, cases)
        if engine == "pytest-native":
            assert native_record.exists(), transcript
            payload = json.loads(native_record.read_text())
            assert [case["nodeid"] for case in payload["cases"]] == [
                "test_case.py::test_first",
                "test_case.py::test_second",
            ], (payload, transcript)
        if persistent and result.returncode == 1:
            failure = next(ET.parse(report).getroot().iter("failure"))
            assert "UserWarning: ambient-filter sentinel" in "".join(failure.itertext()), transcript
    expected_second = ("failure",) if persistent else ()
    expected = (1 if persistent else 0, [("test_first", ()), ("test_second", expected_second)])
    assert outcomes["pytest"] == expected, outcomes
    assert outcomes["pytest-native"] == outcomes["pytest"], outcomes


@pytest.mark.parametrize("mutation", ["insert", "replace", "item_scope"])
def test_warning_filters_observe_persistent_ambient_changes(tmp_path: Path, mutation: str) -> None:
    prepare = {
        "insert": (
            "    request.config._ambient_warning_filters.insert(0, "
            "('error', None, UserWarning, None, 0))"
        ),
        "replace": (
            "    request.config._ambient_warning_filters[-1] = "
            "('error', None, UserWarning, None, 0)"
        ),
        "item_scope": "    warnings.filterwarnings('error', category=UserWarning)",
    }[mutation]
    _run_pair(tmp_path, prepare, persistent=mutation != "item_scope")


@pytest.mark.parametrize("mutation", ["rebind", "code"])
def test_warning_filter_callback_mutations_are_observed(tmp_path: Path, mutation: str) -> None:
    prepare = """    original = warnings.filterwarnings
    def changed_filterwarnings(*args, **kwargs):
        original(*args, **kwargs)
        original('error', message='ambient-filter sentinel', category=UserWarning)
    warnings.filterwarnings = changed_filterwarnings"""
    if mutation == "code":
        prepare = """    def changed_filterwarnings(*args, **kwargs):
        __import__('warnings').filters.insert(0, ('error', None, UserWarning, None, 0))
    warnings.filterwarnings.__code__ = changed_filterwarnings.__code__"""
    _run_pair(tmp_path, prepare, persistent=True)


@pytest.mark.parametrize("mutation", ["rebind", "append"])
def test_warning_filters_observe_live_command_line_changes(tmp_path: Path, mutation: str) -> None:
    value = "error:ambient-filter sentinel:UserWarning"
    change = f" = [{value!r}]" if mutation == "rebind" else f".append({value!r})"
    prepare = f"    request.config.known_args_namespace.pythonwarnings{change}"
    _run_pair(tmp_path, prepare, persistent=True, pytest_args=("-W", "default::UserWarning"))


@pytest.mark.parametrize("replace_module", [False, True])
def test_warning_filters_resolve_registered_hook_globals(
    tmp_path: Path, replace_module: bool
) -> None:
    prepare = """    import _pytest.warnings as warning_plugin
    if not hasattr(warning_plugin, 'apply_warning_filters'):
        import _pytest.config as warning_plugin
    original = warning_plugin.apply_warning_filters
    def changed_filters(config_filters, cmdline_filters):
        original(config_filters, cmdline_filters)
        warnings.filterwarnings('error', message='ambient-filter sentinel', category=UserWarning)
    warning_plugin.apply_warning_filters = changed_filters"""
    if replace_module:
        prepare += """\n    import sys, types
    sys.modules['_pytest.warnings'] = types.ModuleType('_pytest.warnings')"""
    _run_pair(tmp_path, prepare, persistent=True)


def test_warning_filter_helpers_receive_original_configured_objects(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import pytest
import _pytest.config as config_module
import _pytest.warnings as warning_module
if not hasattr(warning_module, "apply_warning_filters"):
    warning_module = config_module

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    original = warning_module.apply_warning_filters
    def check_filters(config_filters, cmdline_filters):
        assert config_filters is config.getini('filterwarnings'), 'configured filters lost identity'
        assert cmdline_filters is config.known_args_namespace.pythonwarnings, 'CLI filter identity'
        original(config_filters, cmdline_filters)
    warning_module.apply_warning_filters = check_filters
    config_module.apply_warning_filters = check_filters
""",
        )
        project.write("test_case.py", "def test_identity(): pass\n")
        arguments = ("-q", "-W", "default::UserWarning")
        environment: dict[str, str | None] = {"PYTHONWARNINGS": None}
        record = project.root / "native.json"
        if engine == "pytest":
            result = project.run_pytest(*arguments, load_xdist=False, env=environment)
        else:
            environment["PYTEST_NATIVE_RESULT"] = str(record)
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                *arguments,
                env=environment,
            )
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "1 passed" in transcript, transcript
        if engine == "pytest-native":
            payload = json.loads(record.read_text())
            assert [case["nodeid"] for case in payload["cases"]] == ["test_case.py::test_identity"]
