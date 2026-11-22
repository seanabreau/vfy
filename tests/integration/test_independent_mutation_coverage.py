"""Fresh mutation coverage and fail-fast authority use worker invocations."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration.test_independent_mutation_serial_cli import _mutate, _project
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="fresh serial mutations require Linux descendant reaping"
)

_STOP_CONFTEST = """import json
from pathlib import Path
import pytest
from subject import value

events = []
def event(name):
    events.append([name, value()])

@pytest.fixture(scope='session')
def resource():
    event('enter')
    yield
    event('finalize')

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    try:
        return (yield)
    except session.Failed:
        event('stop')
        if MODE == 'swallow':
            return True
        if MODE == 'replace':
            raise RuntimeError('replaced actual native stop')
        raise

def pytest_sessionfinish(exitstatus):
    event('finish')
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps([int(exitstatus), events]) + '\\n')
"""

_STOP_TEST = """from pathlib import Path
from subject import value
from conftest import event

def test_first(resource):
    event('first')
    assert Path('repaired').exists()
    assert value() == 1

def test_after(resource):
    event('after')
"""


def test_automatic_mutation_baseline_retains_actual_per_phase_contexts(tmp_path: Path) -> None:
    project = _project(tmp_path)
    subject = project.write(
        "subject.py",
        "def value(default=1):\n    return default\n\ndef phase(name):\n    return name\n",
    )
    project.write(
        "conftest.py",
        (tmp_path / "conftest.py").read_text() + "\nfrom subject import phase\n"
        "@pytest.fixture\ndef resource():\n"
        "    phase('setup')\n    yield\n    phase('teardown')\n",
    )
    project.write(
        "test_subject.py",
        "from subject import value, phase\n\n"
        "def test_value(resource, cov):\n"
        "    actual = value()\n"
        "    if actual == 1:\n        assert cov is not None\n"
        "    phase('call')\n    assert actual == 1\n",
    )
    result = _mutate(project, "--max-mutations=1")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)["summary"] == {"protected": 1}
    data = project.data()
    assert data.lines(str(subject)) == [1, 2, 4, 5]
    assert set(data.contexts_by_lineno(str(subject))[5]) == {
        "test_subject.py::test_value|setup",
        "test_subject.py::test_value|run",
        "test_subject.py::test_value|teardown",
    }


def test_initial_failed_baseline_prefix_finalizes_then_repairs_without_kill_credit(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def value(default=1):\n    return default\n")
    project.write(
        "conftest.py", (tmp_path / "conftest.py").read_text() + "\nMODE='raise'\n" + _STOP_CONFTEST
    )
    project.write("test_subject.py", _STOP_TEST)
    failed = _mutate(project, "--max-mutations=1")
    assert failed.returncode != 0
    assert "baseline did not pass" in failed.stderr, failed.stderr
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".vfy/mutations.json").exists()
    assert json.loads((tmp_path / "events.jsonl").read_text()) == [
        1,
        [["enter", 1], ["first", 1], ["finalize", 1], ["stop", 1], ["finish", 1]],
    ]
    project.write("repaired", "ready")
    repaired = _mutate(project, "--max-mutations=1")
    assert repaired.returncode == 0, (repaired.stdout, repaired.stderr)
    assert json.loads(repaired.stdout)["summary"] == {"protected": 1}
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    # The cache probe finishes collection without fixture or test events.
    assert rows == [
        [1, [["enter", 1], ["first", 1], ["finalize", 1], ["stop", 1], ["finish", 1]]],
        [0, [["enter", 1], ["first", 1], ["after", 1], ["finalize", 1], ["finish", 1]]],
        [0, [["finish", 1]]],
        [1, [["enter", 2], ["first", 2], ["finalize", 2], ["stop", 2], ["finish", 2]]],
    ]


@pytest.mark.parametrize("mode", ["raise", "swallow", "replace"])
def test_only_original_fail_fast_stop_authorizes_fresh_assignment_prefix(
    tmp_path: Path, mode: str
) -> None:
    project = _project(tmp_path)
    project.write("subject.py", "def value(default=1):\n    return default\n")
    project.write(
        "conftest.py",
        (tmp_path / "conftest.py").read_text() + f"\nMODE={mode!r}\n" + _STOP_CONFTEST,
    )
    project.write("test_subject.py", _STOP_TEST)
    project.write("repaired", "ready")
    result = _mutate(project, "--max-mutations=1")
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert rows == [
        [0, [["enter", 1], ["first", 1], ["after", 1], ["finalize", 1], ["finish", 1]]],
        [0, [["finish", 1]]],
        [
            3 if mode == "replace" else 1,
            [["enter", 2], ["first", 2], ["finalize", 2], ["stop", 2], ["finish", 2]],
        ],
    ]
    if mode == "raise":
        assert (tmp_path / ".coverage").is_file()
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert json.loads(result.stdout)["summary"] == {"protected": 1}
        assert rows[2][0] == 1
    else:
        assert not (tmp_path / ".coverage").exists()
        assert result.returncode != 0
        assert "omitted assigned cases without its original native stop" in result.stderr
        assert not (tmp_path / ".vfy/mutations.json").exists()


def test_retained_selected_fail_fast_preserves_stock_collection_order(tmp_path: Path) -> None:
    stock = VfyProject(tmp_path)
    stock.write("subject.py", "def value():\n    return 2\n")
    stock.write(
        "test_subject.py",
        "from pathlib import Path\nfrom subject import value\n"
        "def event(name):\n    with Path('events').open('a') as stream:\n"
        "        stream.write(f'{name}:{value()}\\n')\n"
        "def test_z_first():\n    event('first')\n    assert value() == 1\n"
        "def test_a_after():\n    event('after')\n    assert value() == 1\n",
    )
    reference = stock.run_python("-B", "-m", "pytest", "-x", "-q")
    assert reference.returncode == 1, reference
    assert (tmp_path / "events").read_text().splitlines() == ["first:2"]
    (tmp_path / "events").unlink()
    project = _project(tmp_path)
    project.write("subject.py", "def value():\n    return 1\n")
    result = _mutate(project, "--max-mutations=1", "--no-cache")
    assert result.returncode == 0, result
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["fast_results"] == 1
    assert report["mutations"][0]["killer"] == "test_subject.py::test_z_first"
    assert report["mutations"][0]["tests"] == [
        "test_subject.py::test_z_first",
        "test_subject.py::test_a_after",
    ]
    assert (tmp_path / "events").read_text().splitlines() == ["first:1", "after:1", "first:2"]
