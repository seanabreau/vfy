"""Active test state follows fixture and request lifetimes across setup failure."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="independent execution requires Unix"
)

_SOURCE = """\
import json
from pathlib import Path
import pytest


def record(event):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(event) + '\\n')


@pytest.fixture(scope='session')
def shared(request):
    record('shared setup')
    request.addfinalizer(lambda: record('shared finalizer'))
    return object()


@pytest.fixture(scope='module')
def holder(request, shared):
    record('holder setup')
    yield request, shared
    assert request.getfixturevalue('shared') is shared
    record('holder teardown')


@pytest.fixture
def broken(request):
    request.addfinalizer(lambda: record('broken finalizer'))
    record('broken setup')
    raise ValueError('fixture setup sentinel')


class TestCases:
    def test_failed_setup(self, holder, broken):
        raise AssertionError('not reached')

    def test_later_case(self, holder, request):
        saved, shared = holder
        assert saved.getfixturevalue('shared') is shared
        assert request.getfixturevalue('shared') is shared
        record('later call')
"""


def test_active_rows_preserve_saved_requests_after_setup_failure(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_cases.py", _SOURCE)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--workers=1", "--no-replay", "--no-cov", "-q"
            )
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "fixture setup sentinel" in result.stdout + result.stderr
        events = [
            json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()
        ]
        observations[engine] = events
    assert (
        observations["native"]
        == observations["pytest"]
        == [
            "shared setup",
            "holder setup",
            "broken setup",
            "broken finalizer",
            "later call",
            "holder teardown",
            "shared finalizer",
        ]
    )


_OVERLAP = """\
from pathlib import Path
import pytest


def record(event):
    with Path('generations').open('a') as stream:
        stream.write(event + '\\n')


@pytest.fixture(scope='module')
def resource(request):
    record('setup')
    request.addfinalizer(lambda: record('finalizer'))
    return object()


def test_first(resource, request):
    definition = request._fixture_defs['resource']
    definition._finalizers.clear()
    definition.cached_result = None
    record('cleared')


def test_second(resource):
    record('second')
"""


def test_live_fixture_definition_can_start_another_generation(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_generations.py", _OVERLAP)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=native", "--workers=1", "--no-replay", "--no-cov", "-q"
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout
        observations[engine] = (project.root / "generations").read_text().splitlines()
    assert (
        observations["native"]
        == observations["pytest"]
        == [
            "setup",
            "cleared",
            "setup",
            "second",
            "finalizer",
        ]
    )
