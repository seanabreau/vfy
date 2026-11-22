"""Public cache lifecycle differentials against pytest-xdist 3.8."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent
from typing import Literal

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest
type Route = Literal["upstream", "native"]


def _run(project: VfyProject, route: Route, *arguments: str) -> CommandResult:
    if route == "native":
        return project.run_vfy(
            "test", "--engine=pytest-native", "--no-replay", "--no-cov", "-q", *arguments
        )
    return project.run_pytest("-p", "xdist.plugin", "-q", *arguments, load_xdist=False)


def _state(project: VfyProject) -> tuple[object, object]:
    directory = project.root / ".pytest_cache/v/cache"
    failed = directory / "lastfailed"
    nodeids = directory / "nodeids"
    return (
        json.loads(failed.read_text()) if failed.exists() else {},
        json.loads(nodeids.read_text()) if nodeids.exists() else None,
    )


@pytest.mark.parametrize("selection", ["empty", "deselected", "collection-error", "collect-only"])
def test_xdist_disabled_finishes_cache_when_no_test_body_runs(
    tmp_path: Path, selection: str
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        arguments: tuple[str, ...] = ()
        expected_code = pytest.ExitCode.NO_TESTS_COLLECTED
        expected_state: tuple[object, object] = ({}, [])
        if selection != "empty":
            project.write(
                "test_good.py",
                "from pathlib import Path\ndef test_valid(): Path('executed').touch()\n",
            )
        if selection == "deselected":
            arguments = ("-k", "does_not_match")
        elif selection == "collection-error":
            project.write("test_bad.py", "raise ValueError('collection-cache-sentinel')\n")
            expected_code = pytest.ExitCode.INTERRUPTED
            expected_state = ({"test_bad.py": True}, ["test_good.py::test_valid"])
        elif selection == "collect-only":
            arguments = ("--collect-only",)
            expected_code = pytest.ExitCode.OK
            expected_state = ({}, None)
        result = _run(project, route, "--numprocesses=0", *arguments)
        assert result.returncode == expected_code, result.stdout + result.stderr
        if selection == "collection-error":
            assert "collection-cache-sentinel" in result.stdout
        assert not (project.root / "executed").exists()
        assert _state(project) == expected_state
        observations.append(_state(project))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize("key", ["lastfailed", "nodeids"])
@pytest.mark.parametrize("existing", [False, True], ids=["empty-cache", "existing-cache"])
def test_xdist_session_cache_uses_its_snapshot_when_a_fixture_writes_the_same_key(
    tmp_path: Path, workers: str, key: str, existing: bool
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        previous_failures = {"test_previous.py::test_old": True} if existing else {}
        previous_ids = ["test_previous.py::test_old"] if existing else []
        if existing:
            project.write(".pytest_cache/v/cache/lastfailed", json.dumps(previous_failures))
            project.write(".pytest_cache/v/cache/nodeids", json.dumps(previous_ids))
        future = "test_future.py::test_later"
        fixture_value = {future: True} if key == "lastfailed" else [future]
        project.write(
            "test_writer.py",
            f"def test_write(cache):\n    cache.set('cache/{key}', {fixture_value!r})\n",
        )
        result = _run(project, route, "--numprocesses", workers)
        result.expect(0)
        expected_ids = (
            sorted([*previous_ids, "test_writer.py::test_write"])
            if workers == "0"
            else (previous_ids)
        )
        assert _state(project) == (previous_failures, expected_ids)
        observations.append(_state(project))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize("option", ["--cache-clear", "--capture=tee-sys"])
def test_native_xdist_invalid_configuration_preserves_session_cache(
    tmp_path: Path, workers: str, option: str
) -> None:
    project = VfyProject(tmp_path)
    previous = ({"test_previous.py::test_old": True}, ["test_previous.py::test_old"])
    project.write(".pytest_cache/v/cache/lastfailed", json.dumps(previous[0]))
    project.write(".pytest_cache/v/cache/nodeids", json.dumps(previous[1]))
    project.write(
        "test_valid.py", "from pathlib import Path\ndef test_valid(): Path('executed').touch()\n"
    )
    result = _run(project, "native", "--numprocesses", workers, option)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert f"does not implement {option}" in result.stderr
    assert not (project.root / "executed").exists()
    assert _state(project) == previous


@pytest.mark.parametrize(
    ("exception", "status"),
    [
        ("pytest.UsageError", pytest.ExitCode.USAGE_ERROR),
        ("KeyboardInterrupt", pytest.ExitCode.INTERRUPTED),
    ],
)
def test_xdist_disabled_incomplete_collection_preserves_session_cache(
    tmp_path: Path, exception: str, status: pytest.ExitCode
) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        previous = ({"test_previous.py::test_old": True}, ["test_previous.py::test_old"])
        project.write(".pytest_cache/v/cache/lastfailed", json.dumps(previous[0]))
        project.write(".pytest_cache/v/cache/nodeids", json.dumps(previous[1]))
        project.write(
            "conftest.py",
            f"import pytest\ndef pytest_collection_modifyitems(items):\n"
            f"    raise {exception}('incomplete-collection-cache-sentinel')\n",
        )
        project.write(
            "test_valid.py",
            "from pathlib import Path\ndef test_valid(): Path('executed').touch()\n",
        )
        result = _run(project, route, "--numprocesses=0")
        assert result.returncode == status, result.stdout + result.stderr
        assert "incomplete-collection-cache-sentinel" in result.stdout + result.stderr
        assert not (project.root / "executed").exists()
        assert _state(project) == previous


@pytest.mark.parametrize("workers", ["0", "2"])
def test_xdist_lastfailed_persists_phase_outcomes_and_selects_the_next_run(
    tmp_path: Path, workers: str
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_phases.py",
            dedent("""\
                from pathlib import Path
                import pytest
                @pytest.fixture
                def setup_error():
                    if not Path('fixed').exists(): raise ValueError('setup-sentinel')
                @pytest.fixture
                def teardown_error():
                    yield
                    if not Path('fixed').exists(): raise ValueError('teardown-sentinel')
                def test_call(): assert Path('fixed').exists(), 'call-sentinel'
                def test_setup(setup_error): pass
                def test_teardown(teardown_error): pass
                @pytest.mark.skip(reason='skip-sentinel')
                def test_skip(): pass
                @pytest.mark.xfail(reason='xfail-sentinel')
                def test_xfail(): assert False
                def test_pass(): Path('pass-count').touch()
                """),
        )
        failed = _run(project, route, "--numprocesses", workers)
        failed.expect(1)
        assert _state(project)[0] == {
            "test_phases.py::test_call": True,
            "test_phases.py::test_setup": True,
            "test_phases.py::test_teardown": True,
        }
        first = _state(project)
        project.write("fixed", "fixed\n")
        (project.root / "pass-count").unlink()
        passed = _run(project, route, "--numprocesses", workers, "--lf")
        passed.expect(0)
        assert "3 passed" in passed.stdout
        assert not (project.root / "pass-count").exists()
        assert _state(project)[0] == {}
        observations.append((first, _state(project)))
    assert observations[0] == observations[1]


def test_xdist_skips_clear_stale_failures_without_losing_unselected_failures(
    tmp_path: Path,
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            ".pytest_cache/v/cache/lastfailed",
            json.dumps(
                {
                    "test_skip.py::test_skip": True,
                    "test_skip.py::test_xfail": True,
                    "test_elsewhere.py::test_unselected": True,
                }
            ),
        )
        project.write(
            "test_skip.py",
            "import pytest\n"
            "@pytest.mark.skip(reason='setup skip')\ndef test_skip(): pass\n"
            "@pytest.mark.xfail(reason='expected failure')\ndef test_xfail(): assert False\n",
        )
        result = _run(project, route, "--numprocesses=2", "--lf", "test_skip.py")
        result.expect(0)
        assert _state(project)[0] == {"test_elsewhere.py::test_unselected": True}
        observations.append(_state(project))
    assert observations[0] == observations[1]


def test_xdist_controller_persists_collection_failures_and_recovery(tmp_path: Path) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_bad.py", "raise ValueError('collection-sentinel')\n")
        failed = _run(project, route, "--numprocesses=2")
        failed.expect(1)
        assert "collection-sentinel" in failed.stdout
        assert _state(project)[0] == {"test_bad.py": True}
        first = _state(project)
        project.write("test_bad.py", "def test_recovered(): pass\n")
        recovered = _run(project, route, "--numprocesses=2", "--lf")
        recovered.expect(0)
        assert "1 passed" in recovered.stdout
        observations.append((first, _state(project)))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize("maxfail", [None, "1"])
def test_xdist_partial_collection_preserves_valid_bodies_and_reports(
    tmp_path: Path, workers: str, maxfail: str | None
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "pytest.ini",
            "[pytest]\njunit_logging=all\njunit_log_passing_tests=true\n",
        )
        project.write("test_bad.py", "raise RuntimeError('partial-collection-sentinel')\n")
        project.write(
            "test_good.py",
            "from pathlib import Path\n"
            "import sys\n"
            "import pytest\n"
            "@pytest.fixture\n"
            "def resource(request, cache):\n"
            "    assert request.getfixturevalue('cache') is cache\n"
            "    assert request.config.cache is cache\n"
            "    print('partial setup stdout')\n"
            "    yield\n"
            "    print('partial teardown stdout')\n"
            "    Path('finalized').touch()\n"
            "def test_valid(resource):\n"
            "    print('partial body stdout')\n"
            "    print('partial body stderr', file=sys.stderr)\n"
            "    Path('body-started').touch()\n",
        )
        arguments = ["--numprocesses", workers, "--junitxml=partial.xml"]
        if maxfail is not None:
            arguments.append(f"--maxfail={maxfail}")
        result = _run(project, route, *arguments)
        transcript = result.stdout + result.stderr
        assert "partial-collection-sentinel" in transcript, transcript
        body_started = (project.root / "body-started").exists()
        finalized = (project.root / "finalized").exists()
        if maxfail is None:
            assert result.returncode == (2 if workers == "0" else 1), transcript
            assert body_started == (workers != "0"), transcript
        assert finalized == body_started, transcript
        lastfailed = _state(project)[0]
        assert lastfailed == {"test_bad.py": True}, lastfailed
        tree = ET.parse(project.root / "partial.xml").getroot()
        for element in tree.iter():
            element.attrib.pop("time", None)
            element.attrib.pop("timestamp", None)
        for suite in tree.iter("testsuite"):
            suite[:] = sorted(
                suite,
                key=lambda element: (
                    element.tag,
                    element.get("classname", ""),
                    element.get("name", ""),
                ),
            )
        xml = ET.tostring(tree, encoding="unicode").replace(str(project.root), "PROJECT")
        if body_started:
            for sentinel in (
                "partial setup stdout",
                "partial body stdout",
                "partial body stderr",
                "partial teardown stdout",
            ):
                assert sentinel in xml, xml
        observations.append((result.returncode, body_started, finalized, lastfailed, xml))
    assert observations[0] == observations[1]


def test_xdist_each_keeps_worker_cache_fixture_writes(tmp_path: Path) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_cache.py",
            "def test_cache(cache, worker_id):\n"
            "    cache.set('worker/' + worker_id, {'worker': worker_id})\n"
            "    assert cache.get('worker/' + worker_id, None) == {'worker': worker_id}\n",
        )
        result = _run(project, route, "--numprocesses=2", "--dist=each")
        result.expect(0)
        assert "2 passed" in result.stdout
        writes = {
            path.name: json.loads(path.read_text())
            for path in (project.root / ".pytest_cache/v/worker").iterdir()
        }
        assert writes == {"gw0": {"worker": "gw0"}, "gw1": {"worker": "gw1"}}
        observations.append((writes, _state(project)))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("interruption", ["maxfail", "crash"])
def test_xdist_interrupted_workers_leave_controller_cache_complete(
    tmp_path: Path, interruption: str
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(".pytest_cache/v/cache/lastfailed", '{"test_unselected.py::test_old": true}')
        body = (
            "import os\ndef test_failure(): os._exit(23)\n"
            if interruption == "crash"
            else (
                "def test_failure(): assert False, 'maxfail-sentinel'\n"
                "def test_pending(): assert False, 'pending-sentinel'\n"
            )
        )
        project.write("test_failure.py", body)
        arguments = ("--max-worker-restart=1",) if interruption == "crash" else ("--maxfail=1",)
        result = _run(project, route, "--numprocesses=2", "--dist=loadfile", *arguments)
        expected_status = 1 if interruption == "crash" else 2
        assert result.returncode == expected_status, result.stdout + result.stderr
        assert _state(project)[0] == {
            "test_unselected.py::test_old": True,
            "test_failure.py::test_failure": True,
        }
        observations.append(_state(project))
    assert observations[0] == observations[1]


def test_xdist_cache_commit_failure_preserves_the_existing_path(tmp_path: Path) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(".pytest_cache/v/cache/lastfailed/retained", "previous-state\n")
        project.write("test_fail.py", "def test_failure(): assert False, 'commit-sentinel'\n")
        result = _run(project, route, "--numprocesses=2")
        result.expect(1)
        assert "commit-sentinel" in result.stdout
        assert "PytestCacheWarning" in result.stdout
        assert (project.root / ".pytest_cache/v/cache/lastfailed/retained").read_text() == (
            "previous-state\n"
        )


@pytest.mark.skipif(
    int(pytest.version_tuple[0]) < 9, reason="pytest 9 provides the subtests fixture"
)
@pytest.mark.parametrize("workers", ["0", "2"])
def test_xdist_lastfailed_uses_the_reported_parent_subtest_outcome(
    tmp_path: Path, workers: str
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_subtests.py",
            "from pathlib import Path\n"
            "def test_parent(subtests):\n"
            "    with subtests.test(value=0):\n"
            "        assert Path('fixed').exists(), 'subtest-cache-sentinel'\n"
            "def test_other(): Path('other-executed').touch()\n",
        )
        failed = _run(project, route, "--numprocesses", workers)
        failed.expect(1)
        assert "subtest-cache-sentinel" in failed.stdout
        assert _state(project)[0] == {"test_subtests.py::test_parent": True}
        first = _state(project)
        project.write("fixed", "fixed\n")
        (project.root / "other-executed").unlink()
        recovered = _run(project, route, "--numprocesses", workers, "--lf")
        recovered.expect(0)
        assert not (project.root / "other-executed").exists()
        assert _state(project)[0] == {}
        observations.append((first, _state(project)))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize("option", ["--sw", "--sw-skip", "--sw-reset"])
def test_native_xdist_rejects_stepwise_before_running_cases(
    tmp_path: Path, workers: str, option: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_step.py",
        "from pathlib import Path\ndef test_step(): Path('executed').touch()\n",
    )
    result = _run(project, "native", "--numprocesses", workers, option)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    help_result = _run(project, "upstream", "--help")
    assert help_result.returncode == pytest.ExitCode.OK, help_result.stdout + help_result.stderr
    if option == "--sw-reset" and "--sw-reset" not in help_result.stdout:
        upstream = _run(project, "upstream", "--numprocesses", workers, option)
        assert upstream.returncode == pytest.ExitCode.USAGE_ERROR, upstream.stdout + upstream.stderr
        diagnostic = f"unrecognized arguments: {option}"
        assert diagnostic in upstream.stderr
        assert diagnostic in result.stderr
    else:
        assert "does not implement stepwise execution with xdist" in result.stderr
    assert not (project.root / "executed").exists()
