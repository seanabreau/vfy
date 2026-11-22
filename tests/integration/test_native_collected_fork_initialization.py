"""Prove completed initialization is inherited without admitting live resources."""

from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="collected fork workers require Linux"
)


def _run(project: VfyProject, *, reports: bool = False) -> CommandResult:
    arguments = ["test", "--engine=pytest-native", "--workers=2", "--no-replay"]
    if reports:
        arguments += ["--history", "history.json"]
    arguments += ["--no-cov", "--capture=sys", "-q"]
    if reports:
        arguments += ["--junitxml=results.xml", "-o", "junit_logging=all"]
    return project.run_vfy(
        *arguments,
        env={
            "VFY_WORKER_START": "fork",
            "VFY_NATIVE_SCHEDULING": "cases",
            "VFY_WORKER_COLLECTION": "0",
        },
    )


def test_collected_fork_inherits_completed_initialization(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """
import json, os
from pathlib import Path
import pytest

state = {'events': [], 'mutations': []}
factories = {}

@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    assert state['events'] == []
    state['parent_pid'] = os.getpid()
    state['events'].append('configure')
    factories['modern'] = config._tmp_path_factory
    factories['legacy'] = config._tmpdirhandler
    state['entropy_fd'] = os.open('/dev/urandom', os.O_RDONLY)
    Path(f'configured.{os.getpid()}').touch()

def pytest_collection_finish(session):
    assert state['events'] == ['configure']
    state['events'].append('collection')
    Path('initialization.json').write_text(json.dumps({
        name: value for name, value in state.items() if name != 'entropy_fd'
    }))

@pytest.fixture(scope='session')
def inherited_state(tmp_path_factory, tmpdir_factory):
    assert tmp_path_factory is factories['modern']
    assert tmpdir_factory is factories['legacy']
    base = tmp_path_factory.getbasetemp()
    assert Path(str(tmpdir_factory.getbasetemp())) == base
    assert base.name in {'tmp.0', 'tmp.1'}
    assert state['events'] == ['configure', 'collection']
    assert state['parent_pid'] != os.getpid()
    assert len(os.read(state['entropy_fd'], 8)) == 8
    assert state['mutations'] == []
    state['mutations'].append(os.getpid())
    observed = {
        'pid': os.getpid(), 'parent_pid': state['parent_pid'],
        'events': list(state['events']), 'mutations': state['mutations'], 'cases': [],
        'base': str(base),
    }
    yield observed
    Path(f'finalized.{os.getpid()}.json').write_text(json.dumps(observed))
""",
    )
    project.write(
        "tests/test_cases.py",
        """
import json, os
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', range(4))
def test_case(value, inherited_state, tmp_path):
    assert inherited_state['pid'] == os.getpid()
    assert inherited_state['mutations'] == [os.getpid()]
    inherited_state['cases'].append(value)
    assert tmp_path.parent == Path(inherited_state['base'])
    (tmp_path / 'fixture-used').touch()
    print(f'initialized case {value}')
    Path(f'case.{value}.json').write_text(json.dumps(inherited_state))
""",
    )

    completed = _run(project, reports=True)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "4 passed" in completed.stdout, output
    initialized = json.loads((tmp_path / "initialization.json").read_text())
    parent_pid = initialized["parent_pid"]
    assert initialized == {
        "events": ["configure", "collection"],
        "mutations": [],
        "parent_pid": parent_pid,
    }
    assert [path.name for path in tmp_path.glob("configured.*")] == [f"configured.{parent_pid}"]
    finalized = [json.loads(path.read_text()) for path in tmp_path.glob("finalized.*.json")]
    worker_pids = {record["pid"] for record in finalized}
    assert len(worker_pids) == 2
    assert len({record["base"] for record in finalized}) == 2
    assert parent_pid not in worker_pids
    assert sorted(record["cases"] for record in finalized) == [[0, 1], [2, 3]]
    for record in finalized:
        assert record["parent_pid"] == parent_pid
        assert record["events"] == initialized["events"]
        assert record["mutations"] == [record["pid"]]
    cases = [json.loads((tmp_path / f"case.{value}.json").read_text()) for value in range(4)]
    assert {case["pid"] for case in cases} == worker_pids
    assert all(case["parent_pid"] == parent_pid for case in cases)

    xml_cases = list(ET.parse(tmp_path / "results.xml").getroot().iter("testcase"))
    assert {case.attrib["name"] for case in xml_cases} == {
        f"test_case[{value}]" for value in range(4)
    }
    for case in xml_cases:
        assert not any(entry.tag in {"failure", "error", "skipped"} for entry in case)
        value = case.attrib["name"].removeprefix("test_case[").removesuffix("]")
        assert f"initialized case {value}" in case.findtext("system-out", "")
    history = json.loads((tmp_path / "history.json").read_text())
    assert {
        nodeid: entry["observations"][-1]["outcome"] for nodeid, entry in history["tests"].items()
    } == {f"tests/test_cases.py::test_case[{value}]": "passed" for value in range(4)}


@pytest.mark.parametrize(
    "boundary", ["cleanup", "thread", "fd", "entropy-path", "entropy-inheritable", "process"]
)
def test_collected_fork_rejects_resources_created_during_initialization(
    tmp_path: Path, boundary: str
) -> None:
    project = VfyProject(tmp_path, timeout=15)
    setup, diagnostic = {
        "cleanup": (
            "config.add_cleanup(lambda: Path('cleanup-ran').touch())\n",
            "unreviewed config cleanup callback",
        ),
        "thread": (
            "held = threading.Thread(target=threading.Event().wait, daemon=True)\nheld.start()\n",
            "reject threads, tracing and profiling",
        ),
        "fd": ("held = open('held-resource', 'w')\n", "reject unowned open descriptor"),
        "entropy-path": (
            "held = os.open('/dev/urandom', os.O_PATH)\n",
            "reject unowned open descriptor",
        ),
        "entropy-inheritable": (
            "held = os.open('/dev/urandom', os.O_RDONLY)\nos.set_inheritable(held, True)\n",
            "reject unowned open descriptor",
        ),
        "process": (
            (
                "held = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "Path('foreign-pid').write_text(str(held.pid))\n"
                "def release_process():\n    held.terminate()\n    held.wait(timeout=5)\n"
                "atexit.register(release_process)\n"
            ),
            "reject active foreign subprocesses",
        ),
    }[boundary]
    project.write(
        "conftest.py",
        "import atexit, os, subprocess, sys, threading\nfrom pathlib import Path\nheld = None\n\n"
        "def pytest_configure(config):\n"
        + textwrap.indent("global held\n" + setup + "Path('initialized').touch()\n", "    "),
    )
    project.write(
        "tests/test_cases.py",
        "from pathlib import Path\nimport pytest\n"
        "@pytest.mark.parametrize('value', [0, 1])\n"
        "def test_body(value):\n    Path(f'body-ran.{value}').touch()\n",
    )
    try:
        completed = _run(project)
        output = completed.stdout + completed.stderr
        assert (tmp_path / "initialized").is_file(), output
        assert completed.returncode != 0, output
        assert "collected fork workers" in output, output
        assert diagnostic in output, output
        assert not list(tmp_path.glob("body-ran.*")), output
        if boundary == "cleanup":
            assert (tmp_path / "cleanup-ran").is_file(), output
    finally:
        if (tmp_path / "foreign-pid").exists():
            with suppress(ProcessLookupError):
                os.kill(int((tmp_path / "foreign-pid").read_text()), signal.SIGKILL)


def test_collected_fork_preserves_unsupported_conftest_hook_rejection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "def pytest_runtest_call(item):\n    Path('call-hook-ran').touch()\n",
    )
    project.write(
        "tests/test_case.py",
        "from pathlib import Path\ndef test_body():\n    Path('body-ran').touch()\n",
    )
    completed = _run(project)
    output = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert "native pytest does not execute conftest hook 'pytest_runtest_call'" in output
    assert not (tmp_path / "call-hook-ran").exists()
    assert not (tmp_path / "body-ran").exists()
