"""Bounded portable supervisors share the public isolated-file worker budget."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="precollection supervision uses Linux waitid"
)


def test_two_portable_supervisors_keep_file_workers_and_body_progress(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write(
        "app.py",
        "def first(argument=1):\n    return argument\n\n"
        "def second(argument=1):\n    return argument\n\n"
        "def third(argument=1):\n    return argument\n\n"
        "def body():\n    return 1\n",
    )
    for name in (
        "peer-ready",
        "first-ready",
        "first-release",
        "second-ready",
        "second-release",
        "body-ready",
    ):
        os.mkfifo(tmp_path / name)
    project.write(
        "tests/test_a_value.py",
        "import app\n\n"
        "def test_value():\n"
        "    if app.first.__defaults__ == (2,):\n"
        "        with open('peer-ready') as peer:\n"
        "            assert peer.read() == 'go'\n"
        "        with open('body-ready', 'w') as body:\n"
        "            body.write('go')\n"
        "    elif app.body() == 2:\n"
        "        with open('body-ready') as body:\n"
        "            assert body.read() == 'go'\n"
        "    assert app.first.__defaults__ == (1,)\n"
        "    assert app.second.__defaults__ == (1,)\n"
        "    assert app.third.__defaults__ == (1,)\n"
        "    assert app.body() == 1\n",
    )
    project.write(
        "tests/test_b_metadata.py",
        "import app\n\n"
        "def test_metadata():\n"
        "    if app.first.__defaults__ == (2,):\n"
        "        with open('peer-ready', 'w') as peer:\n"
        "            peer.write('go')\n"
        "    assert app.first.__defaults__ == (1,)\n"
        "    assert app.second.__defaults__ == (1,)\n",
    )
    project.write(
        "conftest.py",
        "import fcntl, json, os\nfrom pathlib import Path\nimport pytest\nimport app\n\n"
        "def worker_identity(pid):\n"
        "    try:\n"
        "        fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()\n"
        "    except (FileNotFoundError, ProcessLookupError):\n"
        "        return None\n"
        "    return None if fields[0] == 'Z' else [pid, int(fields[19])]\n\n"
        "def record_worker():\n"
        "    with open('worker-counter-lock', 'a') as lock:\n"
        "        fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "        path = Path('worker-counter.json')\n"
        "        state = json.loads(path.read_text()) if path.exists() "
        "else {'live': [], 'peak': 0}\n"
        "        live = [row for row in state['live'] if worker_identity(row[0]) == row]\n"
        "        current = worker_identity(os.getpid())\n"
        "        assert current is not None\n"
        "        if current not in live:\n"
        "            live.append(current)\n"
        "        assert 1 <= len(live) <= 3, live\n"
        "        state = {'live': live, 'peak': max(state['peak'], len(live))}\n"
        "        path.write_text(json.dumps(state))\n"
        "@pytest.fixture(autouse=True)\n"
        "def count_workers():\n"
        "    record_worker()\n\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if str(os.getpid()) != os.getenv('VFY_TEST_CONTROLLER_PID'):\n"
        "        if os.getenv('VFY_MUTATION'):\n"
        "            for name in ('first', 'second'):\n"
        "                if getattr(app, name).__defaults__ == (2,):\n"
        "                    with open(name + '-ready', 'w') as parent:\n"
        "                        parent.write(str(os.getpid()))\n"
        "                    with open(name + '-release') as parent:\n"
        "                        assert parent.read() == 'go'\n"
        "        return\n"
        "    ready = []\n"
        "    for name in ('first', 'second'):\n"
        "        with open(name + '-ready') as child:\n"
        "            ready.append(child.read())\n"
        "    pid = os.getpid()\n"
        "    children = []\n"
        "    for path in Path('/proc').glob('[0-9]*/stat'):\n"
        "        try:\n"
        "            fields = path.read_text().rpartition(')')[2].split()\n"
        "        except (FileNotFoundError, ProcessLookupError, PermissionError):\n"
        "            continue\n"
        "        if int(fields[1]) == pid:\n"
        "            children.append(path.parent.name)\n"
        "    Path('observed-supervisors').write_text(str(len(children)))\n"
        "    assert len(children) == 2 and set(children) == set(ready), (children, ready)\n"
        "    for name in ('first', 'second'):\n"
        "        with open(name + '-release', 'w') as child:\n"
        "            child.write('go')\n",
    )
    arguments = [
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--workers",
        "3",
        "--timeout",
        "10",
        "--format",
        "json",
    ]
    project.write(
        "driver.py",
        "import os\nfrom vfy.cli import main\n"
        "os.environ['VFY_TEST_CONTROLLER_PID'] = str(os.getpid())\n"
        f"raise SystemExit(main({arguments!r}))\n",
    )

    completed = project.run(sys.executable, "driver.py", env={"VFY_MUTATION_ENGINE": "auto"})
    assert completed.stdout, completed
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (report, completed.stderr)
    assert report["summary"] == {"protected": 4}, report
    assert report["metrics"]["portable_fallbacks"] == 3, report
    assert report["metrics"]["fast_results"] == 1, report
    assert (tmp_path / "observed-supervisors").read_text() == "2"
    counter = json.loads((tmp_path / "worker-counter.json").read_text())
    assert 2 <= counter["peak"] <= 3, counter


_BUDGET_MODULE = """\
import fcntl
import json
import os
import time
from pathlib import Path

LIMIT = int(os.environ['VFY_MUTATION_WORKERS'])
STATE = Path('budget.json')


def _start_time(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None if fields[0] == 'Z' else int(fields[19])


def _update(change):
    with open('budget.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = (
            json.loads(STATE.read_text())
            if STATE.exists()
            else {'live': [], 'peak': 0, 'overlap': False}
        )
        live = change([row for row in state['live'] if _start_time(row[0]) == row[1]])
        kinds = {row[2] for row in live}
        STATE.write_text(json.dumps({
            'live': live,
            'peak': max(state['peak'], len(live)),
            'overlap': state['overlap'] or kinds == {'collecting', 'executing'},
        }))
        return len(live)


def register(kind):
    row = [os.getpid(), _start_time(os.getpid()), kind]
    _update(lambda live: live if row in live else [*live, row])


def unregister(kind):
    _update(lambda live: [row for row in live if [row[0], row[2]] != [os.getpid(), kind]])


def hold(seconds, done=lambda: False):
    # Stay live until the budget is exceeded, the caller's condition holds,
    # or the bound expires, so concurrent processes can observe each other.
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not done() and _update(lambda live: live) <= LIMIT:
        time.sleep(0.02)
"""


@pytest.mark.parametrize("workers", [2, 4])
def test_collecting_supervisors_and_executing_tests_share_the_worker_budget(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path, timeout=120)
    project.write(
        "app.py",
        "def first(argument=1):\n    return argument\n\n"
        "def second(argument=1):\n    return argument\n\n"
        "def third(argument=1):\n    return argument\n\n"
        "def body():\n    return 1\n",
    )
    project.write("budget.py", _BUDGET_MODULE)
    names = ("alpha", "beta", "gamma", "delta")
    for name in names:
        project.write(
            f"tests/test_{name}.py",
            "import app\nimport budget\n\n"
            f"def test_{name}():\n"
            "    budget.register('executing')\n"
            "    app.first(), app.second(), app.third()\n"
            "    budget.hold(0.5)\n"
            "    if app.body() == 2:\n"
            "        with open('body-mutant', 'a') as marker:\n"
            f"            marker.write('{name}\\n')\n",
        )
    project.write(
        "conftest.py",
        "import os\nfrom pathlib import Path\nimport budget\n\n"
        "def body_mutant_finished():\n"
        "    path = Path('body-mutant')\n"
        f"    return path.exists() and len(path.read_text().split()) == {len(names)}\n\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    # Only fresh mutation supervisors receive VFY_MUTATION before collection.\n"
        "    if not os.getenv('VFY_MUTATION'):\n"
        "        return\n"
        "    budget.register('collecting')\n"
        "    try:\n"
        "        budget.hold(15.0, body_mutant_finished)\n"
        "    finally:\n"
        "        budget.unregister('collecting')\n",
    )

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--workers",
        str(workers),
        "--timeout",
        "30",
        "--format",
        "json",
        env={"VFY_MUTATION_ENGINE": "auto"},
    )
    assert completed.stdout, completed
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, (report, completed.stderr)
    assert report["summary"] == {"survived": 4}, report
    assert report["metrics"]["portable_fallbacks"] == 3, report
    assert report["metrics"]["fast_results"] == 1, report
    assert sorted((tmp_path / "body-mutant").read_text().split()) == sorted(names)
    state = json.loads((tmp_path / "budget.json").read_text())
    assert state["overlap"], state
    assert state["peak"] == workers, state
