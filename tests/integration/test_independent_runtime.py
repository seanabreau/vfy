"""Public subprocess proofs for the explicit dependency-independent engine."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support.independent_private_aliases import PRIVATE_ALIASES
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = requires_native_pytest


@pytest.fixture
def independent_project(tmp_path: Path) -> tuple[VfyProject, Path]:
    project = VfyProject(tmp_path)
    environment = tmp_path / ".independent-python"
    created = project.run_python("-m", "venv", "--without-pip", str(environment))
    created.expect(0)
    return project, environment / "bin" / "python"


def run_independent(project: VfyProject, python: Path, *arguments: str) -> CommandResult:
    return project.run(
        str(python),
        "-m",
        "vfy.independent.entry",
        *arguments,
        env={"PYTHONPATH": isolated_vfy_path()},
    )


def test_runs_without_pytest_ecosystem_distributions(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write("private_aliases.py", PRIVATE_ALIASES)
    project.write(
        "test_independence.py",
        """import importlib.metadata
import sys
import pytest
from private_aliases import assert_private_aliases

@pytest.mark.parametrize('value', [1, 2])
def test_runtime(value, tmp_path, monkeypatch):
    assert value in (1, 2)
    names = {item.metadata['Name'].lower() for item in importlib.metadata.distributions()}
    assert not names.intersection({'pytest', 'pluggy', 'packaging'})
    assert not any(name.split('.')[0] in {'pluggy', 'packaging'} for name in sys.modules)
    assert_private_aliases()
    monkeypatch.setenv('VFY_INDEPENDENT_CHECK', str(value))
    (tmp_path / 'value').write_text(str(value))
""",
    )
    result = run_independent(project, python, "--native-result-json=results.json")
    result.expect(0)
    record = json.loads((project.root / "results.json").read_text())
    assert record["engine"] == "vfy-independent-v1"
    assert [
        (case["nodeid"], [phase["outcome"] for phase in case["phases"]]) for case in record["cases"]
    ] == [
        ("test_independence.py::test_runtime[1]", ["passed", "passed", "passed"]),
        ("test_independence.py::test_runtime[2]", ["passed", "passed", "passed"]),
    ]


def test_fixture_scope_and_teardown_match_pytest(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

@pytest.fixture(scope='session')
def shared():
    event('session setup')
    yield 7
    event('session teardown')

@pytest.fixture
def resource(shared, request):
    event('function setup')
    request.addfinalizer(lambda: event('registered finalizer'))
    yield shared
    event('function teardown')
""",
    )
    for name in ("test_a.py", "test_b.py"):
        project.write(name, "def test_value(resource):\n    assert resource == 7\n")
    independent = run_independent(project, python)
    independent.expect(0)
    independent_events = (project.root / "events").read_text()
    (project.root / "events").unlink()
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    assert independent_events == (project.root / "events").read_text()


def test_failed_fixture_finalizers_and_phase_capture(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(
        "test_failure.py",
        """from pathlib import Path
import pytest

@pytest.fixture
def broken(request):
    request.addfinalizer(lambda: Path('finalized').write_text('done'))
    print('setup output')
    raise ValueError('fixture failed')

def test_setup(broken):
    raise AssertionError('body must not run')

def test_call():
    print('call output')
    assert False, 'body failed'
""",
    )
    result = run_independent(project, python, "--native-result-json=results.json")
    result.expect(1)
    assert (project.root / "finalized").read_text() == "done"
    record = json.loads((project.root / "results.json").read_text())
    assert record["counts"]["errors"] == 1
    assert record["counts"]["failed"] == 1
    assert record["cases"][0]["phases"][0]["stdout"] == "setup output\n"
    assert record["cases"][1]["phases"][1]["stdout"] == "call output\n"


def test_collection_node_selectors_and_no_tests_exit(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(
        "test_cases.py",
        "class TestCases:\n"
        "    def test_one(self):\n        pass\n"
        "    def test_two(self):\n        pass\n",
    )
    result = run_independent(
        project, python, "-q", "test_cases.py::TestCases::test_two", "--collect-only"
    )
    result.expect(0)
    collected = [re.sub(r" in [0-9.]+s$", "", line) for line in result.stdout.splitlines()]
    assert collected == ["test_cases.py::TestCases::test_two", "", "1 test collected"]
    result = run_independent(project, python, "-k", "missing")
    result.expect(5)


def test_same_named_classes_in_different_files_have_distinct_fixture_scopes(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

@pytest.fixture(scope='class')
def shared():
    event('class setup')
    yield
    event('class teardown')
""",
    )
    for name in ("test_a.py", "test_b.py"):
        project.write(name, "class TestCases:\n    def test_value(self, shared):\n        pass\n")
    independent = run_independent(project, python)
    independent.expect(0)
    independent_events = (project.root / "events").read_text()
    (project.root / "events").unlink()
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    assert independent_events == (project.root / "events").read_text()


def test_dictionary_parameters_and_multiple_skip_conditions(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(
        "test_cases.py",
        """import pytest

@pytest.mark.parametrize('record', [{'answer': 42}, pytest.param({'answer': 42}, id='wrapped')])
def test_record(record):
    assert record['answer'] == 42

@pytest.mark.skipif(False, True, reason='any true condition skips')
def test_multiple_conditions():
    raise AssertionError('body must not run')
""",
    )
    result = run_independent(project, python, "--native-result-json=results.json")
    result.expect(0)
    record = json.loads((project.root / "results.json").read_text())
    assert record["counts"]["passed"] == 2
    assert record["counts"]["skipped"] == 1
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    assert "2 passed, 1 skipped" in oracle.stdout


@pytest.mark.parametrize("definition", ["def", "async def"])
def test_generator_test_functions_are_rejected_before_execution(
    independent_project: tuple[VfyProject, Path], definition: str
) -> None:
    project, python = independent_project
    project.write(
        "test_generator.py",
        f"{definition} test_generator():\n"
        "    raise AssertionError('generator body must not be silently passed')\n"
        "    yield 1\n",
    )
    result = run_independent(project, python)
    expected = 2 if definition == "def" else 1
    oracle = project.run_python("-m", "pytest", "-q")
    assert oracle.returncode == result.returncode == expected, result.stdout + oracle.stdout
    message = (
        "generator test functions are unsupported"
        if definition == "def"
        else "async def functions are not natively supported"
    )
    assert message in result.stdout
    if definition == "def":
        assert "test_generator.py::test_generator" in result.stdout
    else:
        assert "test_generator" in result.stdout
        assert "1 failed" in result.stdout
    assert "1 passed" not in result.stdout


def test_fixture_parameters_and_indirect_values_match_pytest(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(
        "test_parameters.py",
        """from pathlib import Path
import pytest

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

@pytest.fixture(scope='session', params=['a', 'b'])
def shared(request):
    event('shared setup ' + request.param)
    yield request.param
    event('shared teardown ' + request.param)

@pytest.fixture(scope='session')
def derived(shared):
    event('derived setup ' + shared)
    yield shared
    event('derived teardown ' + shared)

def test_first(derived):
    event('first ' + derived)

def test_second(derived):
    event('second ' + derived)

@pytest.fixture
def doubled(request):
    assert request.scope == 'session'
    return request.param * 2

@pytest.mark.parametrize('doubled', [3, 4], indirect=True, scope='session')
def test_indirect(doubled):
    assert doubled in (6, 8)
""",
    )
    independent = run_independent(project, python, "--native-result-json=results.json")
    independent.expect(0)
    independent_events = (project.root / "events").read_text()
    (project.root / "events").unlink()
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    assert independent_events == (project.root / "events").read_text()
    record = json.loads((project.root / "results.json").read_text())
    assert record["counts"]["passed"] == 6


def test_inherited_methods_and_class_fixtures_share_each_test_instance(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(
        "test_classes.py",
        """import pytest

class TestBase:
    multiplier = 1

    @pytest.fixture(autouse=True)
    def prepare(self):
        self.ready = True

    @pytest.fixture(params=[2, 3])
    def value(self, request):
        assert self.ready
        self.current = request.param
        return request.param

    def test_inherited(self, value):
        assert self.current * self.multiplier == value

class TestDerived(TestBase):
    multiplier = 2

    @pytest.fixture(params=[2, 3])
    def value(self, request):
        self.current = request.param
        return request.param * 2

    def test_own(self, value):
        assert self.current * 2 == value
""",
    )
    independent = run_independent(project, python, "--native-result-json=results.json")
    independent.expect(0)
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    record = json.loads((project.root / "results.json").read_text())
    assert record["counts"]["passed"] == 6
    assert [case["nodeid"] for case in record["cases"]] == [
        "test_classes.py::TestBase::test_inherited[2]",
        "test_classes.py::TestBase::test_inherited[3]",
        "test_classes.py::TestDerived::test_inherited[2]",
        "test_classes.py::TestDerived::test_inherited[3]",
        "test_classes.py::TestDerived::test_own[2]",
        "test_classes.py::TestDerived::test_own[3]",
    ]


def test_explicit_basetemp_and_junit_use_native_publication(
    independent_project: tuple[VfyProject, Path],
) -> None:
    project, python = independent_project
    project.write(".temporary/stale", "old")
    project.write(
        "test_temp.py",
        """from pathlib import Path
Path('imported').touch()

def test_temp(tmp_path, tmp_path_factory):
    assert tmp_path.parent == tmp_path_factory.getbasetemp()
    (tmp_path / 'result').write_text('kept')
""",
    )
    invalid = run_independent(project, python, "--basetemp=.")
    invalid.expect(4)
    assert not (project.root / "imported").exists()
    result = run_independent(project, python, "--basetemp=.temporary", "--junitxml=result.xml")
    result.expect(0)
    assert not (project.root / ".temporary/stale").exists()
    artifacts = sorted((project.root / ".temporary").glob("*/result"))
    physical = [path for path in artifacts if not path.parent.is_symlink()]
    assert [path.parent.name for path in physical] == ["test_temp0"]
    current = project.root / ".temporary/test_tempcurrent"
    if current.is_symlink():
        assert current.resolve() == physical[0].parent.resolve()
        assert artifacts == [physical[0], current / "result"]
    else:
        assert artifacts == physical
    assert all(path.samefile(physical[0]) for path in artifacts)
    assert physical[0].read_text() == "kept"
    assert (
        '<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="1"'
        in (project.root / "result.xml").read_text()
    )
