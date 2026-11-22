"""Exercise the public test command at each supported pytest version lane."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


def test_pytest_frontend_and_native_execution(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nmarkers = smoke: selected by the test command\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "@pytest.fixture\n"
        "def resource(request):\n"
        "    with Path('fixture-events.txt').open('a', encoding='utf-8') as output:\n"
        "        output.write(f'setup:{request.node.name}\\n')\n"
        "    yield 2\n"
        "    with Path('fixture-events.txt').open('a', encoding='utf-8') as output:\n"
        "        output.write(f'teardown:{request.node.name}\\n')\n",
    )
    project.write(
        "tests/test_frontend.py",
        "import pytest\n"
        "@pytest.mark.smoke\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_selected(resource, value):\n"
        "    assert resource * value == 2 * value\n"
        "def test_failing_assertion():\n"
        "    assert 1 == 2\n",
    )

    stock = project.run_pytest(
        "-q", "-m", "smoke", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    stock.expect(0)
    stock_events = (tmp_path / "fixture-events.txt").read_text(encoding="utf-8")
    (tmp_path / "fixture-events.txt").unlink()

    native = project.run_vfy(
        "test", "--engine=pytest-native", "--no-replay", "--no-cov", "-q", "-m", "smoke"
    )
    assert native.returncode == stock.returncode, native.stdout + native.stderr
    assert "2 passed, 1 deselected" in native.stdout
    assert (tmp_path / "fixture-events.txt").read_text(encoding="utf-8") == stock_events

    stock_failure = project.run_pytest(
        "-q",
        "-k",
        "failing_assertion",
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    native_failure = project.run_vfy(
        "test", "--engine=pytest-native", "--no-replay", "--no-cov", "-q", "-k", "failing_assertion"
    )
    assert stock_failure.returncode == native_failure.returncode == 1
    assert "assert 1 == 2" in stock_failure.stdout
    assert "assert 1 == 2" in native_failure.stdout


def test_native_workers_and_coverage(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("src/__init__.py", "")
    project.write("src/value.py", "def double(value):\n    return value * 2\n")
    project.write(
        "tests/test_one.py",
        "from src.value import double\ndef test_one():\n    assert double(1) == 2\n",
    )
    project.write(
        "tests/test_two.py",
        "from src.value import double\ndef test_two():\n    assert double(2) == 4\n",
    )

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "-j",
        "2",
        "--no-replay",
        "--cov=src",
        "--cov-report=term",
        "-q",
    )

    completed.expect(0)
    assert "2 passed" in completed.stdout
    assert project.lines("src/value.py") == [1, 2]


@pytest.mark.parametrize("workers", [1, 2])
def test_parameter_families_match_stock_pytest(tmp_path: Path, workers: int) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_parameters.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "def record(event):\n"
        "    with Path('events.txt').open('a', encoding='utf-8') as output:\n"
        "        output.write(event + '\\n')\n"
        "@pytest.fixture(scope='module', params=['a', 'b'])\n"
        "def resource(request):\n"
        "    record(f'setup:{request.param}')\n"
        "    yield request.param\n"
        "    record(f'teardown:{request.param}')\n"
        "@pytest.mark.parametrize('value', [1, 2, 3])\n"
        "@pytest.mark.parametrize('label', ['x', 'y'])\n"
        "def test_product(resource, value, label, request):\n"
        "    record(f'test:{request.node.nodeid}:{resource}:{value}:{label}')\n",
    )
    stock = project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    stock.expect(0)
    events = tmp_path / "events.txt"
    stock_events = events.read_text(encoding="utf-8")
    events.unlink()

    # Keep the file whole so module fixture families run in serial order.
    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "-j",
        str(workers),
        "--native-scheduling=file",
        "--no-replay",
        "--no-cov",
        "-q",
    )

    assert native.returncode == stock.returncode, native.stdout + native.stderr
    assert "12 passed" in native.stdout
    assert events.read_text(encoding="utf-8") == stock_events


def test_explicit_parameter_node_id_matches_stock_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_parameter_node.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2, 3])\n"
        "def test_selected(value):\n"
        "    assert value in {1, 2, 3}\n",
    )

    stock = project.run_pytest(
        "-q",
        "tests/test_parameter_node.py::test_selected[2]",
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--no-replay",
        "--no-cov",
        "-q",
        "tests/test_parameter_node.py::test_selected[2]",
    )

    assert stock.returncode == native.returncode == 0, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    assert "1 passed" in stock.stdout
    assert "1 passed" in native.stdout


def test_native_logging_and_unittest_xunit_match_stock_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_private_api_compat.py",
        "import logging\n"
        "import unittest\n"
        "import pytest\n"
        "logger = logging.getLogger('vfy.pytest.compat')\n"
        "@pytest.fixture\n"
        "def logged(caplog):\n"
        "    caplog.set_level(logging.INFO, logger=logger.name)\n"
        "    logger.info('setup')\n"
        "    yield caplog\n"
        "    logger.info('teardown')\n"
        "def test_logging_context(logged):\n"
        "    logger.info('call')\n"
        "    assert [record.getMessage() for record in logged.get_records('setup')] == ['setup']\n"
        "    assert [record.getMessage() for record in logged.get_records('call')] == ['call']\n"
        "class TestUnittestXunit(unittest.TestCase):\n"
        "    def setup_method(self, method):\n"
        "        self.prepared_for = method.__name__\n"
        "    def test_setup_method_is_bound(self):\n"
        "        assert self.prepared_for == 'test_setup_method_is_bound'\n",
    )

    stock = project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    native = project.run_vfy("test", "--engine=pytest-native", "--no-replay", "--no-cov", "-q")

    assert stock.returncode == native.returncode == 0, (
        stock.stdout + stock.stderr + native.stdout + native.stderr
    )
    assert "2 passed" in stock.stdout
    assert "2 passed" in native.stdout


@pytest.mark.parametrize("repeat_request", [False, True])
def test_parametrization_scope_check_matches_stock_pytest(
    tmp_path: Path, repeat_request: bool
) -> None:
    """First and repeated fixture requests have distinct rules before pytest 8.4."""
    project = VfyProject(tmp_path)
    project.write(
        "test_scope.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "def record(event):\n"
        "    with Path('events.txt').open('a', encoding='utf-8') as stream:\n"
        "        stream.write(event + '\\n')\n"
        "@pytest.fixture\n"
        "def value(request):\n"
        "    record(f'setup:{request.param}')\n"
        "    yield request.param\n"
        "    record(f'teardown:{request.param}')\n"
        "@pytest.fixture(scope='module', autouse=True)\n"
        "def consumer(request):\n"
        "    value = request.getfixturevalue('value')\n"
        "    record(f'consumer:{value}')\n"
        f"    if {repeat_request!r}:\n"
        "        request.getfixturevalue('value')\n"
        "    return value\n"
        "@pytest.mark.parametrize('value', [1, 2], indirect=True, scope='module')\n"
        "def test_scoped(value):\n"
        "    record(f'test:{value}')\n",
    )
    stock = project.run_pytest("-q", load_xdist=False, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    events = tmp_path / "events.txt"
    stock_events = events.read_text(encoding="utf-8") if events.exists() else ""
    events.unlink(missing_ok=True)
    native = project.run_vfy("test", "--engine=pytest-native", "--no-replay", "--no-cov", "-q")
    native_events = events.read_text(encoding="utf-8") if events.exists() else ""

    release = tuple(int(part) for part in pytest.version_tuple[:2])
    legacy = release < (8, 4)
    repeat_check = release >= (8, 2)
    expected_status = 0 if legacy and not (repeat_request and repeat_check) else 1
    assert stock.returncode == expected_status, stock.stdout + stock.stderr
    assert native.returncode == stock.returncode, native.stdout + native.stderr
    assert native_events == stock_events, native.stdout + native.stderr
    if expected_status:
        assert "ScopeMismatch" in stock.stdout
        assert "ScopeMismatch" in native.stdout
    else:
        assert "2 passed" in native.stdout


def test_native_session_rechecks_version_before_collection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "import pytest\npytest.__version__ = '9.0.0'\n")
    project.write(
        "test_uncollected.py",
        "from pathlib import Path\n"
        "Path('collected.txt').touch()\n"
        "def test_uncollected():\n"
        "    raise AssertionError('unsupported version ran a test')\n",
    )

    completed = project.run_vfy("test", "--engine=pytest-native", "--no-replay", "--no-cov", "-q")

    completed.expect(4)
    assert "found pytest 9.0.0" in completed.stderr
    assert not (tmp_path / "collected.txt").exists()


@pytest.mark.skipif(
    tuple(int(part) for part in pytest.version_tuple[:2]) < (9, 2),
    reason="pytest 9.2 parameter ID policy",
)
@pytest.mark.parametrize("workers", [1, 2])
def test_native_long_parameter_ids_and_fixture_groups_match_stock(
    tmp_path: Path, workers: int
) -> None:
    source = """import json
from pathlib import Path
import pytest

events = []

@pytest.fixture(scope='module', params=[1, 'a'])
def shared(request):
    events.append(['setup', request.param])
    yield request.param
    events.append(['teardown', request.param])
    Path('events.json').write_text(json.dumps(events))

@pytest.mark.parametrize('value', ['x' * 101, 'short'])
def test_values(shared, value, request):
    events.append(['test', request.node.nodeid, shared, value])
"""
    observations = {}
    for route in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nparametrize_long_str_id_strategy = sha256\n")
        project.write("test_values.py", source)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "-j",
                str(workers),
                "--native-scheduling=file",
                "--no-cov",
                "-q",
                "-W",
                "error::pytest.PytestRemovedIn10Warning",
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "4 passed" in result.stdout
        observations[route] = json.loads((project.root / "events.json").read_text())
    assert observations["pytest-native"] == observations["stock"]


@pytest.mark.parametrize("warning_mode", ["default", "cli", "interpreter"])
def test_unhandled_async_fixture_matches_stock_pytest(
    tmp_path: Path, warning_mode: Literal["default", "cli", "interpreter"]
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_async_fixture.py",
        "import pytest\n"
        "@pytest.fixture\n"
        "async def ordinary():\n"
        "    return 42\n"
        "def test_ordinary(ordinary):\n"
        "    ordinary.close()\n",
    )
    warning_args = ["-W", "ignore::DeprecationWarning"] if warning_mode == "cli" else []
    environment = {
        "PYTHONWARNINGS": "ignore::DeprecationWarning" if warning_mode == "interpreter" else None
    }
    stock = project.run_pytest(
        "-q",
        *warning_args,
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", **environment},
    )
    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--no-replay",
        "--no-cov",
        "-q",
        *warning_args,
        env=environment,
    )
    release = tuple(int(part) for part in pytest.version_tuple[:2])

    expected_error = release >= (9, 1) or (release >= (9, 0) and warning_mode != "cli")
    assert stock.returncode == int(expected_error), stock.stdout + stock.stderr
    assert native.returncode == stock.returncode, native.stdout + native.stderr
    diagnostic = "requested an async fixture 'ordinary'"
    for result in (stock, native):
        expected_diagnostic = expected_error or (release >= (8, 4) and warning_mode == "default")
        assert (diagnostic in result.stdout + result.stderr) == expected_diagnostic


@pytest.mark.parametrize("warning_mode", ["default", "ignore", "repr-error"])
def test_replaced_callable_return_diagnostics_match_the_installed_pytest(
    tmp_path: Path, warning_mode: str
) -> None:
    repr_result = (
        "raise RuntimeError('repr callback error')"
        if warning_mode == "repr-error"
        else "return 'returned value'"
    )
    source = f"""from pathlib import Path
import pytest

class Returned:
    def __repr__(self):
        with Path("repr-calls.txt").open("a") as stream:
            stream.write("called\\n")
        {repr_result}

@pytest.fixture(autouse=True)
def rewrite(request):
    def replacement():
        return Returned()
    request.node.obj = replacement
    yield
    Path("finalized.txt").touch()

def test_case():
    raise AssertionError("stale test body ran")
"""
    results = {}
    traces = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        options = ["-q", "--tb=auto"]
        if warning_mode == "ignore":
            options.extend(["-W", "ignore::Warning"])
        result = (
            project.run_pytest(*options, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-cov", *options
            )
        )
        results[engine] = result
        trace = project.root / "repr-calls.txt"
        traces[engine] = trace.read_text() if trace.exists() else ""
        assert (project.root / "finalized.txt").exists(), result.stdout + result.stderr
        assert "stale test body ran" not in result.stdout
    stock, native = results["pytest"], results["pytest-native"]
    assert native.returncode == stock.returncode, stock.stdout + native.stdout + native.stderr
    assert traces["pytest-native"] == traces["pytest"]
    warning = (
        r"PytestReturnNotNoneWarning: ([^\n]+"
        r"(?:\n  (?:Did you mean|See https:)[^\n]+)*)"
    )
    assert re.findall(warning, native.stdout) == re.findall(warning, stock.stdout), (
        stock.stdout + native.stdout
    )
    if stock.returncode:
        diagnostic = (
            "repr callback error"
            if warning_mode == "repr-error"
            else "Expected None, but test returned returned value. "
            "Did you mean to use `assert` instead of `return`?"
        )
        assert diagnostic in stock.stdout
        assert diagnostic in native.stdout
