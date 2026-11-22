"""Native doctests share pytest's session ``doctest_namespace``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_doctests_see_names_added_by_earlier_autouse_fixtures(tmp_path: Path) -> None:
    outcomes: dict[str, tuple[int, bool]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini", "[pytest]\ntestpaths = tests docs\naddopts = --doctest-glob=*.rst\n"
        )
        # python-semver fills the namespace from a tests/ autouse fixture that
        # does not apply to docs/, so docs doctests depend on the session cache.
        project.write(
            "tests/conftest.py",
            "import pytest\n\n@pytest.fixture(autouse=True)\n"
            "def add_names(doctest_namespace):\n    doctest_namespace['answer'] = 42\n",
        )
        project.write("tests/test_first.py", "def test_first():\n    pass\n")
        project.write("docs/guide.rst", "Example\n\n>>> answer\n42\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            # Workers split files, so cross-file namespace state needs one worker.
            "--workers=1",
            "--no-cov",
            "-q",
        )
        result.expect(0)
        outcomes[engine] = (result.returncode, "2 passed" in result.stdout)
    assert outcomes["pytest-native"] == outcomes["pytest"] == (0, True)


def test_doctest_namespace_override_preserves_precedence_and_finalizers(tmp_path: Path) -> None:
    observed: dict[str, list[str]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture
def doctest_namespace(request):
    record('setup ' + request.node.name)
    request.addfinalizer(lambda: record('finalizer ' + request.node.name))
    yield {'answer': 42, 'getfixture': 'namespace override'}
    record('teardown ' + request.node.name)
""",
        )
        project.write(
            "examples.py",
            '''answer = 0

def first():
    """
    >>> answer, getfixture
    (42, 'namespace override')
    >>> scratch = 'first only'
    """

def second():
    """
    >>> answer, getfixture, 'scratch' in globals()
    (42, 'namespace override', False)
    """
''',
        )
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "--doctest-modules", "-q"
        )
        result.expect(0)
        assert "2 passed" in result.stdout, result.stdout + result.stderr
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            f"{phase} examples.{name}"
            for name in ("first", "second")
            for phase in ("setup", "teardown", "finalizer")
        ]
    )


@pytest.mark.parametrize("failure", ["setup", "mapping", "iterable", "missing"])
def test_doctest_namespace_failure_cleans_up_before_next_item(tmp_path: Path, failure: str) -> None:
    observed: dict[str, list[str]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            f"""from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture
def resource():
    record('resource setup')
    yield
    record('resource teardown')

@pytest.fixture
def doctest_namespace(request):
    request.getfixturevalue('resource')
    request.addfinalizer(lambda: record('namespace finalizer'))
    if request.node.name.endswith('.first'):
        if {failure!r} == 'setup':
            raise RuntimeError('namespace setup sentinel')
        if {failure!r} == 'missing':
            request.getfixturevalue('namespace_dependency_sentinel')
        if {failure!r} == 'iterable':
            return [('answer', 42)]
        return None
    return {{'answer': 42}}
""",
        )
        project.write(
            "examples.py",
            '''def first():
    """
    >>> answer
    42
    """

def second():
    """
    >>> answer
    42
    """
''',
        )
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "--doctest-modules", "-q"
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 1, transcript
        assert "1 passed, 1 error" in transcript, transcript
        diagnostic = {
            "setup": "RuntimeError: namespace setup sentinel",
            "mapping": "AttributeError: 'NoneType' object has no attribute 'items'",
            "iterable": "AttributeError: 'list' object has no attribute 'items'",
            "missing": "fixture 'namespace_dependency_sentinel' not found",
        }[failure]
        assert diagnostic in transcript, transcript
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "resource setup",
            "namespace finalizer",
            "resource teardown",
        ]
        * 2
    )
