"""Prove transitive scoped-fixture invalidation at the public test boundary."""

from __future__ import annotations

import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native pytest lifecycle tests require Unix",
)


FIXTURE_SOURCE = """\
from pathlib import Path

import pytest


def record(value):
    with Path("fixture-events.txt").open("a", encoding="utf-8") as stream:
        stream.write(value + "\\n")


@pytest.fixture(scope="module", params=("one", "two"))
def root(request):
    record(f"setup-root:{request.param}")
    yield request.param
    record(f"teardown-root:{request.param}")


@pytest.fixture(scope="module")
def middle(root):
    record(f"setup-middle:{root}")
    yield root
    record(f"teardown-middle:{root}")


@pytest.fixture(scope="module")
def dependent(middle):
    record(f"setup-dependent:{middle}")
    yield middle
    record(f"teardown-dependent:{middle}")


@pytest.fixture(scope="module")
def independent():
    record("setup-independent")
    yield None
    record("teardown-independent")


def test_scoped_fixture_invalidation(dependent, independent):
    record(f"test:{dependent}")
"""


def _run(project: VfyProject, engine: str) -> tuple[int, str, list[str]]:
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_fixture_invalidation.py", FIXTURE_SOURCE)
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    events = (project.root / "fixture-events.txt").read_text(encoding="utf-8").splitlines()
    return result.returncode, result.stdout + result.stderr, events


def test_parameter_change_tears_down_transitive_dependents_before_root(
    tmp_path: Path,
) -> None:
    observations = {
        engine: _run(VfyProject(tmp_path / engine), engine)
        for engine in ("pytest", "pytest-native")
    }

    assert observations["pytest"][0] == 0, observations["pytest"][1]
    assert observations["pytest-native"][0] == observations["pytest"][0], observations[
        "pytest-native"
    ][1]
    assert observations["pytest-native"][2] == observations["pytest"][2], observations[
        "pytest-native"
    ][1]
    expected_events = [
        "setup-root:one",
        "setup-middle:one",
        "setup-dependent:one",
        "setup-independent",
        "test:one",
        "teardown-dependent:one",
        "teardown-middle:one",
        "teardown-root:one",
        "setup-root:two",
        "setup-middle:two",
        "setup-dependent:two",
        "test:two",
    ]
    pytest_version = tuple(int(part) for part in version("pytest").split(".")[:2])
    if pytest_version < (8, 2):
        expected_events.extend(
            [
                "teardown-independent",
                "teardown-dependent:two",
                "teardown-middle:two",
                "teardown-root:two",
            ]
        )
    else:
        expected_events.extend(
            [
                "teardown-dependent:two",
                "teardown-middle:two",
                "teardown-root:two",
                "teardown-independent",
            ]
        )
    assert observations["pytest-native"][2] == expected_events


@pytest.mark.parametrize("fails_first_package", [False, True], ids=["value", "setup-error"])
def test_package_dependency_teardown_invalidates_session_owned_dependents(
    tmp_path: Path, fails_first_package: bool
) -> None:
    files = {
        "conftest.py": """from pathlib import Path
import pytest

def record(event):
    with Path('fixture-events.txt').open('a', encoding='utf-8') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='session', autouse=True)
def independent():
    record('independent setup')
    yield
    record('independent teardown')

@pytest.fixture(scope='package')
def middle(dependency):
    record(f'middle setup {dependency}')
    yield dependency
    record(f'middle teardown {dependency}')

@pytest.fixture(scope='package')
def dependent(middle):
    record(f'dependent setup {middle}')
    if FAILS_FIRST_PACKAGE and middle == 'a':
        raise ValueError('first package setup failed')
    yield middle
    record(f'dependent teardown {middle}')
""",
    }
    files["conftest.py"] = files["conftest.py"].replace(
        "FAILS_FIRST_PACKAGE", str(fails_first_package)
    )
    for package in ("a", "b"):
        files[f"{package}/__init__.py"] = ""
        files[f"{package}/conftest.py"] = f"""import pytest
from conftest import record

@pytest.fixture(scope='package')
def dependency():
    record('dependency setup {package}')
    yield '{package}'
    record('dependency teardown {package}')
"""
        files[f"{package}/test_case.py"] = f"""def test_first(dependent):
    assert dependent == '{package}'

def test_second(dependent):
    assert dependent == '{package}'
"""
    expected = ["independent setup"]
    for package in ("a", "b"):
        expected.extend(
            [
                f"dependency setup {package}",
                f"middle setup {package}",
                f"dependent setup {package}",
                *(
                    []
                    if fails_first_package and package == "a"
                    else [f"dependent teardown {package}"]
                ),
                f"middle teardown {package}",
                f"dependency teardown {package}",
            ]
        )
    expected.append("independent teardown")
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        for name, source in files.items():
            project.write(name, source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == (1 if fails_first_package else 0), transcript
        assert ("2 passed, 2 errors" if fails_first_package else "4 passed") in transcript, (
            transcript
        )
        events = (project.root / "fixture-events.txt").read_text(encoding="utf-8").splitlines()
        assert events == expected, transcript


def test_dependency_teardown_preserves_nested_fixture_errors_and_finalizer_order(
    tmp_path: Path,
) -> None:
    observations: list[tuple[list[str], list[str]]] = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from pathlib import Path
import pytest

def record(event):
    with Path('fixture-events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='package')
def middle(dependency):
    dependency.addfinalizer(lambda: record('late dependency callback'))
    yield dependency
    record('middle teardown')
    raise ValueError('middle teardown failed')

@pytest.fixture(scope='package')
def dependent(middle):
    yield middle
    record('dependent teardown')
    raise ValueError('dependent teardown failed')
""",
        )
        project.write("a/__init__.py", "")
        project.write(
            "a/conftest.py",
            """import pytest
from conftest import record

@pytest.fixture(scope='package')
def dependency(request):
    yield request
    record('dependency teardown')
    raise ValueError('dependency teardown failed')
""",
        )
        project.write("a/test_case.py", "def test_case(dependent):\n    assert dependent\n")
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == 1, transcript
        assert "1 passed, 1 error" in result.stdout, transcript
        errors = [
            line
            for line in result.stdout.splitlines()
            if "| ExceptionGroup:" in line
            or "| ValueError:" in line
            or (line.startswith("E ") and "ValueError:" in line)
        ]
        events = (project.root / "fixture-events.txt").read_text().splitlines()
        observations.append((errors, events))

    assert observations[1] == observations[0]
    pytest_version = tuple(int(part) for part in version("pytest").split(".")[:2])
    assert len(observations[0][0]) == (5 if pytest_version >= (8, 1) else 1)
    assert observations[0][1] == [
        "late dependency callback",
        "dependent teardown",
        "middle teardown",
        "dependency teardown",
    ]


@pytest.mark.parametrize("callback_source", ["parameter-switch", "dependency-setup"])
def test_parameter_switch_registers_dependency_finish_in_pytest_order(
    tmp_path: Path, callback_source: str
) -> None:
    observations: list[list[str]] = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from pathlib import Path
import pytest

saved_requests = []

def record(event):
    with Path('fixture-events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='package'CHILD_PARAMETERS)
def child(parent, another_parent, request):
    parameter = getattr(request, 'param', 1)
    yield parameter
    record(f'child teardown {parameter}')
    if parameter == 1 and CALLBACK_SOURCE == 'parameter-switch':
        parent.addfinalizer(lambda: record('late parent callback'))

@pytest.fixture(scope='session')
def another_parent():
    if CALLBACK_SOURCE == 'dependency-setup':
        saved_requests[-1].addfinalizer(lambda: record('late parent callback'))
""".replace("CALLBACK_SOURCE", repr(callback_source)).replace(
                "CHILD_PARAMETERS",
                ", params=[1, 2]" if callback_source == "parameter-switch" else "",
            ),
        )
        project.write("a/__init__.py", "")
        project.write(
            "a/conftest.py",
            """import pytest
from conftest import record, saved_requests

@pytest.fixture(scope='package')
def parent(request):
    saved_requests.append(request)
    yield request
    record('parent teardown')
""",
        )
        project.write(
            "a/test_case.py",
            "def test_case(child):\n    assert child in (1, 2)\n"
            if callback_source == "parameter-switch"
            else "def test_case(request):\n    assert request.getfixturevalue('child') == 1\n",
        )
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert ("2 passed" if callback_source == "parameter-switch" else "1 passed") in (
            result.stdout
        ), transcript
        observations.append((project.root / "fixture-events.txt").read_text().splitlines())

    assert observations[1] == observations[0]
    pytest_version = tuple(int(part) for part in version("pytest").split(".")[:2])
    callbacks = [
        "late parent callback",
        "child teardown 2" if callback_source == "parameter-switch" else "child teardown 1",
    ]
    if pytest_version >= (8, 2):
        callbacks.reverse()
    assert observations[0] == [
        *(["child teardown 1"] if callback_source == "parameter-switch" else []),
        *callbacks,
        "parent teardown",
    ]
