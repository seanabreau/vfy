"""Stock doctests retain fixture and report contracts in collected fork workers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from validation.pytest_runner.check_native_xdist_adoption import junit_records

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


def test_collected_fork_doctests_preserve_globals_fixtures_and_reports(tmp_path: Path) -> None:
    projects = {route: VfyProject(tmp_path / route) for route in ("stock", "exec", "fork")}
    records = {}
    for route, project in projects.items():
        project.write(
            "conftest.py",
            """
import json, os
from pathlib import Path
import pytest

@pytest.fixture(scope='session', autouse=True)
def namespace(doctest_namespace):
    state = {'pid': os.getpid(), 'seen': []}
    doctest_namespace['worker_state'] = state
    yield
    Path(f'finalized.{os.getpid()}.json').write_text(json.dumps(state))

@pytest.fixture
def resource(tmp_path, tmpdir):
    assert tmp_path == Path(str(tmpdir))
    print('resource setup')
    yield tmp_path
    print('resource teardown')
""",
        )
        source = """
import json, os, sys
from pathlib import Path
import pytest

collection_pid = os.getpid()
shared = []
with open('collections.jsonl', 'a') as stream:
    stream.write(json.dumps({'pid': collection_pid}) + '\\n')

def mark_case(label, state, resource):
    assert state['pid'] == os.getpid()
    shared.append(os.getpid())
    assert set(shared) == {os.getpid()}
    state['seen'].append(label)
    Path(f'case.{label}.json').write_text(json.dumps({
        'pid': os.getpid(), 'collection_pid': collection_pid,
        'tmp': str(resource), 'shared_count': len(shared),
    }))
"""
        for index in range(8):
            ending = (
                ">>> pytest.skip('doctest skip sentinel')\n"
                if index == 6
                else ">>> print('failure sentinel')\nexpected sentinel\n"
                if index == 7
                else ""
            )
            source += (
                f'\ndef example_{index}():\n    """\n'
                "    >>> assert 'scratch' not in globals()\n"
                f"    >>> scratch = 'case-{index}'\n"
                f"    >>> mark_case('{index}', worker_state, getfixture('resource'))\n"
                "    resource setup\n"
                f"    >>> print('body {index}')\n    body {index}\n"
                f"    >>> print('error {index}', file=sys.stderr)\n"
                + "".join(f"    {line}\n" for line in ending.splitlines())
                + '    """\n'
            )
        project.write("examples.py", source)
        arguments = (
            "--capture=sys",
            "--doctest-modules",
            "--junitxml=results.xml",
            "--basetemp",
            str(project.root / "requested-base"),
            "-o",
            "junit_family=xunit1",
            "-o",
            "junit_logging=all",
            "-o",
            "junit_log_passing_tests=true",
            "-q",
            "examples.py",
        )
        if route == "stock":
            completed = project.run_python(
                "-m", "pytest", "-p", "xdist.plugin", "-n", "2", "--dist=load", *arguments
            )
        else:
            completed = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--history=history.json",
                "--no-replay",
                "--no-cov",
                *arguments,
                env={
                    "VFY_WORKER_START": route,
                    "VFY_NATIVE_SCHEDULING": "cases",
                    "VFY_WORKER_COLLECTION": "0",
                },
            )
        completed.expect(1)
        assert "failure sentinel" in completed.stdout + completed.stderr
        records[route] = junit_records(
            project.root / "results.xml", project.root, project.root / "requested-base"
        )
        assert len(records[route]) == 8
        cases = [
            json.loads((project.root / f"case.{index}.json").read_text()) for index in range(8)
        ]
        pids = {case["pid"] for case in cases}
        assert len(pids) == 2
        finalized = [json.loads(path.read_text()) for path in project.root.glob("finalized.*.json")]
        assert {state["pid"] for state in finalized} == pids
        assert sorted(label for state in finalized for label in state["seen"]) == [
            str(i) for i in range(8)
        ]
        assert len({case["tmp"] for case in cases}) == 8
        assert all((case["collection_pid"] != case["pid"]) == (route == "fork") for case in cases)
        if route == "fork":
            assert len((project.root / "collections.jsonl").read_text().splitlines()) == 1
            assert all(
                Path(case["tmp"]).parent.name in {"native-worker-0", "native-worker-1"}
                for case in cases
            )
    assert records["exec"] == records["stock"]
    assert records["fork"] == records["stock"]
