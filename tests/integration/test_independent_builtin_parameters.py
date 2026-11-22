"""Same-name builtin overrides share pytest's parameter values and scopes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _in_worker_order[T](rows: list[T], workers: int) -> list[T]:
    """Workers interleave their appends, so only one worker preserves serial order."""
    return rows if workers == 1 else sorted(rows, key=json.dumps)


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize(
    ("kind", "outer_scope", "inner_params", "indirect"),
    [
        ("capsys", "function", True, False),
        ("capsys", "function", False, False),
        ("tmp_path_factory", "session", True, False),
        ("tmp_path_factory", "session", False, False),
        ("capsys", "function", True, True),
    ],
)
def test_same_name_parameter_source_and_scope_match_pytest(
    tmp_path: Path,
    workers: int,
    kind: str,
    outer_scope: str,
    inner_params: bool,
    indirect: bool,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import json, pytest
from pathlib import Path

@pytest.fixture(scope={outer_scope!r}, params=['outer1', 'outer2'])
def {kind}({kind}, request):
    with Path('fixture-events.jsonl').open('a') as output:
        output.write(json.dumps(['outer', request.param, request.scope]) + '\\n')
    return request.param, request.scope, {kind}
""",
    )
    parameters = ", params=['inner1', 'inner2']" if inner_params else ""
    marker = (
        f"@pytest.mark.parametrize({kind!r}, ['direct1', 'direct2'], indirect=True)\n"
        if indirect
        else ""
    )
    project.write(
        "test_parameters.py",
        f"""import json
from pathlib import Path
import pytest

@pytest.fixture(scope='function'{parameters})
def {kind}({kind}, request):
    with Path('fixture-events.jsonl').open('a') as output:
        output.write(json.dumps(['inner', request.param, request.scope]) + '\\n')
    return request.param, request.scope, {kind}

def observe(value, request):
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps([
            request.node.nodeid, value[0], value[1], value[2][0], value[2][1]
        ]) + '\\n')

{marker}def test_a({kind}, request):
    observe({kind}, request)

{marker}def test_b({kind}, request):
    observe({kind}, request)
""",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    observed = tmp_path / "observed.jsonl"
    expected = [json.loads(line) for line in observed.read_text().splitlines()]
    assert len(expected) == 4
    fixture_events = tmp_path / "fixture-events.jsonl"
    expected_fixture_events = fixture_events.read_text()
    project.write("oracle-fixture-events.jsonl", expected_fixture_events)
    fixture_events.unlink()
    project.write("oracle-observed.json", json.dumps(expected))
    observed.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-scheduling=file",
        "--native-result-json=result.json",
    )
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    actual = [json.loads(line) for line in observed.read_text().splitlines()]
    assert _in_worker_order(actual, workers) == _in_worker_order(expected, workers)
    assert _in_worker_order(fixture_events.read_text().splitlines(), workers) == (
        _in_worker_order(expected_fixture_events.splitlines(), workers)
    )
    result = json.loads((tmp_path / "result.json").read_text())
    assert [case["nodeid"] for case in result["cases"]] == [row[0] for row in expected]
    assert result["counts"]["passed"] == 4


@pytest.mark.parametrize("workers", [1, 2])
def test_shared_parameter_scope_preserves_declared_scope_access_checks(
    tmp_path: Path,
    workers: int,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path

def pytest_runtest_makereport(item, call):
    if call.excinfo is not None:
        with Path('scope-errors.jsonl').open('a') as output:
            output.write(json.dumps([
                call.when, call.excinfo.type.__name__,
                getattr(call.excinfo.value, 'pytrace', None)
            ]) + '\\n')

@pytest.fixture(scope='function', params=['outer1', 'outer2'])
def tmp_path_factory(tmp_path_factory, request):
    return request.param, tmp_path_factory
""",
    )
    project.write(
        "test_parameters.py",
        """import pytest
from pathlib import Path

@pytest.fixture(scope='session', params=['inner1', 'inner2'])
def tmp_path_factory(tmp_path_factory, request):
    return request.param, tmp_path_factory

def test_a(tmp_path_factory):
    Path('body-ran').touch()

def test_b(tmp_path_factory):
    Path('body-ran').touch()
""",
    )
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(1)
    assert "ScopeMismatch" in oracle.stdout, oracle.stdout + oracle.stderr
    assert not (tmp_path / "body-ran").exists()
    errors = tmp_path / "scope-errors.jsonl"
    expected_errors = [json.loads(line) for line in errors.read_text().splitlines()]
    assert expected_errors == [["setup", "Failed", False]] * 4
    errors.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=result.json",
    )
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert "ScopeMismatch" in candidate.stdout + candidate.stderr
    assert not (tmp_path / "body-ran").exists()
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["counts"]["errors"] == len(result["cases"]) == 4
    assert all(case["phases"][0]["outcome"] == "error" for case in result["cases"])
    assert [json.loads(line) for line in errors.read_text().splitlines()] == expected_errors


@pytest.mark.parametrize("workers", [1, 2])
def test_override_parameter_ids_precede_dependency_parameter_ids(
    tmp_path: Path,
    workers: int,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest

@pytest.fixture(params=['A1', 'A2'])
def auxiliary(request):
    return request.param

@pytest.fixture(params=['O1', 'O2'])
def capsys(capsys, auxiliary, request):
    return request.param, auxiliary, capsys
""",
    )
    project.write(
        "test_parameters.py",
        """import json
from pathlib import Path
import pytest

@pytest.fixture(params=['I1', 'I2'])
def capsys(capsys, request):
    return request.param, capsys

def test_case(capsys, request):
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps([
            request.node.nodeid, capsys[0], capsys[1][0], capsys[1][1]
        ]) + '\\n')
""",
    )
    _compare_observed(project, workers, 4)


@pytest.mark.parametrize("workers", [1, 2])
def test_mixed_direct_and_indirect_mark_infers_function_scope(
    tmp_path: Path,
    workers: int,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest

@pytest.fixture(scope='session')
def tmp_path_factory(tmp_path_factory, request):
    return request.param, request.scope, tmp_path_factory
""",
    )
    project.write(
        "test_parameters.py",
        """import json
from pathlib import Path
import pytest

@pytest.mark.parametrize(
    'tmp_path_factory,value', [('V1', 1), ('V2', 2)], indirect=['tmp_path_factory']
)
def test_case(tmp_path_factory, value, request):
    with Path('observed.jsonl').open('a') as output:
        output.write(json.dumps([
            request.node.nodeid, tmp_path_factory[0], tmp_path_factory[1], value
        ]) + '\\n')
""",
    )
    _compare_observed(project, workers, 2)


def _compare_observed(project: VfyProject, workers: int, count: int) -> None:
    oracle = project.run_python("-m", "pytest", "-q")
    oracle.expect(0)
    observed = project.root / "observed.jsonl"
    expected = [json.loads(line) for line in observed.read_text().splitlines()]
    assert len(expected) == count
    project.write("oracle-observed.json", json.dumps(expected))
    observed.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-result-json=result.json",
    )
    candidate.expect(0)
    actual = [json.loads(line) for line in observed.read_text().splitlines()]
    assert _in_worker_order(actual, workers) == _in_worker_order(expected, workers)
    result = json.loads((project.root / "result.json").read_text())
    assert [case["nodeid"] for case in result["cases"]] == [row[0] for row in expected]
    assert result["counts"]["passed"] == count
