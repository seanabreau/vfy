"""Compare dynamically requested shadow-fixture bindings with pinned pytest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native fixture execution requires Unix",
)


def test_dynamic_shadow_lookup_preserves_callspec_parameter(tmp_path: Path) -> None:
    conftest_source = """\
from pathlib import Path

import pytest


def record(value):
    with Path("fixture-events.txt").open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")


@pytest.fixture
def selected(request):
    record(f"parent setup {request.param}")
    yield f"parent:{request.param}"
    record(f"parent teardown {request.param}")
"""
    test_source = """\
import pytest

from conftest import record


@pytest.fixture(params=("amber", "indigo"))
def selected(request):
    parent = request.getfixturevalue("selected")
    record(f"override setup {request.param} {parent}")
    yield f"override:{request.param}:{parent}"
    record(f"override teardown {request.param} {parent}")


def test_dynamic_lookup(selected, request):
    assert request.getfixturevalue("selected") == selected
    record(f"call {request.node.nodeid} {selected}")
"""

    observations: dict[str, dict[str, object]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest_source)
        project.write("test_dynamic.py", test_source)
        project.write(
            "phase_probe.py",
            (
                Path(__file__).resolve().parents[2]
                / "validation"
                / "pytest_runner"
                / "stock_phase_probe.py"
            ).read_text(encoding="utf-8"),
        )
        phase_path = project.root / "phases.json"
        environment: dict[str, str | None] = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        observer: list[str] = []
        if engine == "pytest":
            environment["PYTEST_STOCK_PHASE_LOG"] = str(phase_path)
            observer = ["-p", "phase_probe"]
        else:
            environment["PYTEST_NATIVE_RESULT"] = str(phase_path)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *observer,
            env=environment,
        )
        result.expect(0)

        if engine == "pytest":
            phases = [json.loads(line) for line in phase_path.read_text().splitlines()]
        else:
            native_result = json.loads(phase_path.read_text())
            phases = [
                {
                    "nodeid": case["nodeid"],
                    **{field: phase[field] for field in ("phase", "outcome", "stdout", "stderr")},
                }
                for case in native_result["cases"]
                for phase in case["phases"]
            ]
        events = (project.root / "fixture-events.txt").read_text().splitlines()
        observations[engine] = {"phases": phases, "events": events}

    assert observations["pytest-native"] == observations["pytest"]
    assert observations["pytest"]["events"] == [
        "parent setup amber",
        "override setup amber parent:amber",
        "call test_dynamic.py::test_dynamic_lookup[amber] override:amber:parent:amber",
        "override teardown amber parent:amber",
        "parent teardown amber",
        "parent setup indigo",
        "override setup indigo parent:indigo",
        "call test_dynamic.py::test_dynamic_lookup[indigo] override:indigo:parent:indigo",
        "override teardown indigo parent:indigo",
        "parent teardown indigo",
    ]


def _missing_param_diagnostic(detail: str, project_root: Path) -> str:
    normalized = detail.replace(str(project_root), "<ROOT>")
    lines = normalized.splitlines()
    marker = "The requested fixture has no parameter defined for test:"
    start = next((index for index, line in enumerate(lines) if marker in line), None)
    if start is None:
        pytest.fail(f"missing pytest parameter diagnostic in:\n{normalized}")
    block = lines[start : start + 8]
    block[0] = block[0][block[0].index(marker) :]
    return "\n".join(block)


def test_uncollected_parametrized_fixture_keeps_pytest_missing_param_error(
    tmp_path: Path,
) -> None:
    conftest_source = """\
from pathlib import Path

import pytest


@pytest.fixture(params=("uncollected",))
def dynamic_value(request):
    Path("fixture-body-ran.txt").write_text("entered", encoding="utf-8")
    return request.param


import os

if os.environ.get("VFY_POISON_TOP_REQUEST") == "1":
    from _pytest.fixtures import TopRequest

    def reject_stock_fixture_resolution(*args, **kwargs):
        raise AssertionError("native parameter validation delegated to TopRequest")

    TopRequest.getfixturevalue = reject_stock_fixture_resolution
"""
    test_source = """\
def test_dynamic_lookup_without_callspec(request):
    request.getfixturevalue("dynamic_value")
"""
    stock_probe = """\
import json
import os
from pathlib import Path


def pytest_runtest_logreport(report):
    if report.when == "call":
        Path(os.environ["PYTEST_STOCK_PHASE_LOG"]).write_text(
            json.dumps({
                "nodeid": report.nodeid,
                "phase": report.when,
                "outcome": report.outcome,
                "longreprtext": report.longreprtext,
            }),
            encoding="utf-8",
        )
"""

    observations: dict[str, dict[str, object]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest_source)
        project.write("test_dynamic.py", test_source)
        project.write("phase_probe.py", stock_probe)
        phase_path = project.root / "phases.json"
        environment: dict[str, str | None] = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        observer: list[str] = []
        if engine == "pytest":
            environment["PYTEST_STOCK_PHASE_LOG"] = str(phase_path)
            observer = ["-p", "phase_probe"]
        else:
            environment["PYTEST_NATIVE_RESULT"] = str(phase_path)
            environment["VFY_POISON_TOP_REQUEST"] = "1"
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *observer,
            env=environment,
        )
        result.expect(1)
        assert phase_path.is_file(), result.stdout + result.stderr
        body_ran = (project.root / "fixture-body-ran.txt").exists()

        if engine == "pytest":
            phase = json.loads(phase_path.read_text())
            detail = phase["longreprtext"]
        else:
            native_result = json.loads(phase_path.read_text())
            case = native_result["cases"][0]
            phase = next(row for row in case["phases"] if row["phase"] == "call")
            detail = phase["detail"]
            phase = {"nodeid": case["nodeid"], "phase": "call", "outcome": phase["outcome"]}
        observations[engine] = {
            "nodeid": phase["nodeid"],
            "phase": phase["phase"],
            "outcome": phase["outcome"],
            "diagnostic": _missing_param_diagnostic(detail, project.root),
            "body_ran": body_ran,
        }

    assert observations["pytest-native"]["nodeid"] == observations["pytest"]["nodeid"]
    assert observations["pytest-native"]["diagnostic"] == observations["pytest"]["diagnostic"]
    fixture_line = 6 if tuple(map(int, pytest.version_tuple[:2])) >= (9, 2) else 7
    assert observations["pytest"]["diagnostic"] == (
        "The requested fixture has no parameter defined for test:\n"
        "    test_dynamic.py::test_dynamic_lookup_without_callspec\n\n"
        "Requested fixture 'dynamic_value' defined in:\n"
        f"conftest.py:{fixture_line}\n\n"
        "Requested here:\n"
        "test_dynamic.py:2"
    )
    for engine in ("pytest", "pytest-native"):
        observation = observations[engine]
        assert observation["phase"] == "call"
        assert observation["outcome"] == "failed"
        assert observation["body_ran"] is False


def test_callspec_dictionary_subclasses_keep_parameter_and_scope_lookups(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest
from _pytest.scope import Scope

class Parameters(dict):
    def __getitem__(self, name):
        value = super().__getitem__(name)
        return 42 if name in ('resource', 'bare') else value

class Scopes(dict):
    def __getitem__(self, name):
        value = super().__getitem__(name)
        return Scope.Module if name == 'resource' else value

def pytest_generate_tests(metafunc):
    metafunc.parametrize('resource,bare', [(1, 1), (2, 2)], indirect=['resource'])
    for callspec in metafunc._calls:
        object.__setattr__(callspec, 'params', Parameters(callspec.params))
        object.__setattr__(callspec, '_arg2scope', Scopes(callspec._arg2scope))

@pytest.fixture
def resource(request):
    assert request.scope == 'module'
    with Path('events.txt').open('a') as stream:
        stream.write('setup\\n')
    yield request.param
    with Path('events.txt').open('a') as stream:
        stream.write('teardown\\n')

@pytest.fixture
def late(request):
    return request.getfixturevalue('resource')

def test_case(resource, bare, request):
    assert resource == bare == request.getfixturevalue('late') == 42
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "2 passed" in result.stdout, result.stdout + result.stderr
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"] == ["setup", "teardown"]


def test_custom_callspec_lookup_error_stays_a_case_setup_failure(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest

class Parameters(dict):
    def __getitem__(self, name):
        if name == 'value':
            raise ValueError('parameter lookup error')
        return super().__getitem__(name)

def pytest_generate_tests(metafunc):
    metafunc.parametrize('value', [1])
    callspec = metafunc._calls[-1]
    object.__setattr__(callspec, 'params', Parameters(callspec.params))

def test_case(value):
    Path('body-ran.txt').write_text('entered')
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == 1, transcript
        assert "1 error" in transcript, transcript
        assert "INTERNALERROR" not in transcript, transcript
        assert not (project.root / "body-ran.txt").exists(), transcript
