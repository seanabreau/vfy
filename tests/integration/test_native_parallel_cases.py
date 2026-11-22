"""Compare explicit case scheduling and worker fixture cleanup with pytest."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.integration.test_native_parallel import _native_parallel_available
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native parallel tests require Linux or macOS and a supported pytest version",
)


def _junit_cases(project: VfyProject) -> list[tuple[object, ...]]:
    root = ET.parse(project.root / "result.xml").getroot()
    return [
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple(
                (child.tag, tuple(sorted(child.attrib.items())))
                for child in case
                if child.tag not in {"properties", "system-out", "system-err"}
            ),
            tuple(
                (prop.attrib["name"], prop.attrib["value"])
                for prop in case.findall("properties/property")
            ),
            case.findtext("system-out", default=""),
            case.findtext("system-err", default=""),
        )
        for case in root.iter("testcase")
    ]


def test_case_scheduling_splits_one_family_and_preserves_outcomes(
    tmp_path: Path,
) -> None:
    records: dict[str, list[tuple[object, ...]]] = {}
    for route in ("pytest", "default", "file", "cases"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_family.py",
            "import os\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "@pytest.fixture(scope='module')\n"
            "def resource():\n"
            "    pid = os.getpid()\n"
            "    Path(f'setup-{pid}').touch()\n"
            "    yield pid\n"
            "    Path(f'teardown-{pid}').touch()\n"
            "@pytest.mark.parametrize('value', range(32))\n"
            "def test_family(value, resource, record_property):\n"
            "    Path(f'owner-{value}').write_text(str(resource))\n"
            "    record_property('value', str(value))\n"
            "    print(f'captured-{value}')\n"
            "    if value == 3:\n"
            "        pytest.skip('skip example')\n"
            "    if value == 8:\n"
            "        pytest.xfail('xfail example')\n"
            "    assert value != 13\n",
        )
        arguments = (
            "-q",
            "--junitxml=result.xml",
            "-o",
            "junit_family=xunit1",
            "-o",
            "junit_logging=all",
            "-o",
            "junit_log_passing_tests=true",
        )
        if route == "pytest":
            result = project.run_pytest(*arguments, load_xdist=False)
        else:
            result = project.run_vfy(
                "test",
                "--engine=native",
                "--workers=4",
                "--no-replay",
                "--no-cov",
                *arguments,
                env={"VFY_NATIVE_SCHEDULING": None if route == "default" else route},
            )
        result.expect(1)
        records[route] = _junit_cases(project)
        owners = {path.read_text() for path in project.root.glob("owner-*")}
        # The default independent scheduler splits the family like an explicit `cases` request.
        assert len(owners) == (4 if route in {"cases", "default"} else 1)
        assert len(list(project.root.glob("owner-*"))) == 32
        assert {path.name.removeprefix("setup-") for path in project.root.glob("setup-*")} == owners
        assert {
            path.name.removeprefix("teardown-") for path in project.root.glob("teardown-*")
        } == owners
    assert records["pytest"] == records["default"] == records["file"] == records["cases"]


@pytest.mark.parametrize("scheduling", ["cases", "striped"])
def test_case_scheduling_preserves_broad_fixture_nesting_per_worker(
    tmp_path: Path, scheduling: str
) -> None:
    traces: dict[str, list[list[str]]] = {}
    for route in ("pytest", scheduling):
        project = VfyProject(tmp_path / route)
        project.write("tests/__init__.py", "")
        project.write(
            "tests/conftest.py",
            "import os\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "def note(text):\n"
            "    with Path(f'events-{os.getpid()}').open('a') as stream:\n"
            "        stream.write(text + '\\n')\n"
            "@pytest.fixture(scope='session')\n"
            "def session_resource():\n"
            "    note('session+')\n"
            "    yield\n"
            "    note('session-')\n"
            "@pytest.fixture(scope='package')\n"
            "def package_resource(session_resource):\n"
            "    note('package+')\n"
            "    yield\n"
            "    note('package-')\n"
            "@pytest.fixture(scope='module')\n"
            "def module_resource(package_resource):\n"
            "    note('module+')\n"
            "    yield\n"
            "    note('module-')\n"
            "@pytest.fixture(scope='class')\n"
            "def class_resource(module_resource):\n"
            "    note('class+')\n"
            "    yield os.getpid()\n"
            "    note('class-')\n",
        )
        project.write(
            "tests/test_family.py",
            "from pathlib import Path\n"
            "import pytest\n"
            "class TestFamily:\n"
            "    @pytest.mark.parametrize('value', range(32))\n"
            "    def test_family(self, value, class_resource):\n"
            "        Path(f'owner-{value}').write_text(str(class_resource))\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=4",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"VFY_NATIVE_SCHEDULING": scheduling},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        traces[route] = [path.read_text().splitlines() for path in project.root.glob("events-*")]
        owners = {path.read_text() for path in project.root.glob("owner-*")}
        assert len(owners) == len(traces[route]) == (1 if route == "pytest" else 4)
        assert len(list(project.root.glob("owner-*"))) == 32
    assert traces["pytest"] == [
        [
            "session+",
            "package+",
            "module+",
            "class+",
            "class-",
            "module-",
            "package-",
            "session-",
        ]
    ]
    assert traces[scheduling] == traces["pytest"] * 4


@pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")
@pytest.mark.parametrize(("scheduling", "workers"), [("cases", 4), ("striped", 2), ("striped", 4)])
def test_case_chunks_preserve_parameterized_fixture_transitions(
    tmp_path: Path, scheduling: str, workers: int
) -> None:
    observer = """\
import gc, json, os
from pathlib import Path
gc_was_enabled = gc.isenabled()

def pytest_collection():
    gc.disable()

def note(text):
    with Path(f'events-{os.getpid()}').open('a') as stream:
        stream.write(text + '\\n')

def pytest_collection_modifyitems(items):
    Path('inventory.json').write_text(json.dumps([item.nodeid for item in items]))
    if gc_was_enabled:
        gc.enable()

def pytest_runtest_setup(item):
    with Path(f'items-{os.getpid()}').open('a') as stream:
        stream.write(item.nodeid + '\\n')
"""
    source = """\
import gc, pytest
from conftest import note

@pytest.fixture(scope='session')
def session_resource():
    assert gc.isenabled()
    note('session+')
    yield
    note('session-')

@pytest.fixture(scope='module', params=['left', 'right', 'bad'])
def resource(request, session_resource):
    value = request.param
    note(f'setup:{value}')
    if value == 'bad':
        pytest.fail('module setup sentinel')
    yield value
    note(f'finish:{value}')

@pytest.fixture(scope='class')
def class_resource(resource):
    note(f'class+:{resource}')
    yield resource
    note(f'class-:{resource}')

@pytest.fixture
def phase_resource(value, class_resource):
    note(f'function+:{value}')
    if value == 0:
        pytest.fail('function setup sentinel')
    yield class_resource
    note(f'function-:{value}')
    if value == 2:
        pytest.fail('function teardown sentinel')

class TestFamily:
    @pytest.mark.parametrize('value', range(17))
    def test_family(self, value, phase_resource):
        assert gc.isenabled()
        note(f'call:{phase_resource}:{value}')
        print(f'captured-{value}')
        if value == 1:
            pytest.fail('call sentinel')

@pytest.mark.parametrize('value', range(3))
def test_plain(value):
    assert gc.isenabled()
    note(f'plain:{value}')
"""
    arguments = (
        "-q",
        "--capture=sys",
        "--junitxml=result.xml",
        "-o",
        "junit_logging=all",
        "-o",
        "junit_log_passing_tests=true",
    )
    native = VfyProject(tmp_path / "native")
    native.write("conftest.py", observer)
    native.write("test_family.py", source)
    result = native.run_vfy(
        "test",
        "--engine=pytest-native",
        f"--workers={workers}",
        f"--native-scheduling={scheduling}",
        "--no-replay",
        "--no-cov",
        *arguments,
        env={
            "CHUNK_ROUTE": "native",
            "VFY_WORKER_START": "fork",
            "PYTEST_NATIVE_RESULT": str(native.root / "native.json"),
        },
    )
    result.expect(1)
    inventory = json.loads((native.root / "inventory.json").read_text())
    record = json.loads((native.root / "native.json").read_text())
    assert record["collected"] == len(inventory) == 54
    assert [case["nodeid"] for case in record["cases"]] == inventory
    item_paths = list(native.root.glob("items-*"))
    assignments = [path.read_text().splitlines() for path in item_paths]
    assert len(assignments) == workers
    assert sorted(nodeid for assignment in assignments for nodeid in assignment) == sorted(
        inventory
    )
    if scheduling == "striped":
        assert sorted(assignments) == sorted(
            inventory[worker::workers] for worker in range(workers)
        )
    stock = VfyProject(tmp_path / "xdist")
    stock.write("conftest.py", observer)
    stock.write("test_family.py", source)
    oracle = stock.run_pytest(
        "-p",
        "xdist.plugin",
        "-n",
        str(workers),
        "--dist=load",
        *arguments,
        load_xdist=False,
    )
    oracle.expect(1)
    assert inventory == json.loads((stock.root / "inventory.json").read_text())
    assert sorted(_junit_cases(native)) == sorted(_junit_cases(stock))
    for index, (path, assignment) in enumerate(zip(item_paths, assignments, strict=True)):
        reference = VfyProject(tmp_path / f"reference-{index}")
        reference.write("conftest.py", observer)
        reference.write("test_family.py", source)
        oracle = reference.run_pytest(*assignment, *arguments, load_xdist=False)
        assert oracle.returncode in {0, 1}, oracle.stdout + oracle.stderr
        native_trace = (native.root / path.name.replace("items-", "events-")).read_text()
        oracle_traces = [trace.read_text() for trace in reference.root.glob("events-*")]
        assert oracle_traces == [native_trace]


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
@pytest.mark.parametrize("scheduling", ["cases", "striped"])
def test_case_scheduling_preserves_shared_maxfail_and_finalizers(
    tmp_path: Path, phase: str, scheduling: str
) -> None:
    for route in ("pytest", scheduling):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_family.py",
            "import os\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "@pytest.fixture(scope='session')\n"
            "def lifecycle():\n"
            "    pid = os.getpid()\n"
            "    Path(f'start-{pid}').touch()\n"
            "    yield\n"
            "    Path(f'finish-{pid}').touch()\n"
            "@pytest.fixture\n"
            "def resource(lifecycle):\n"
            f"    if {phase == 'setup'}:\n"
            "        pytest.fail('failed setup')\n"
            "    yield\n"
            f"    if {phase == 'teardown'}:\n"
            "        pytest.fail('failed teardown')\n"
            "@pytest.mark.parametrize('value', range(128))\n"
            "def test_family(value, resource):\n"
            "    Path(f'ran-{value}').touch()\n"
            f"    if {phase == 'call'}:\n"
            "        pytest.fail('failed call')\n",
        )
        record_path = project.root / "native.json"
        arguments = ("--maxfail=1", "--junitxml=result.xml", "-q")
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=4",
                "--no-replay",
                "--no-cov",
                *arguments,
                env={
                    "VFY_NATIVE_SCHEDULING": scheduling,
                    "PYTEST_NATIVE_RESULT": str(record_path),
                },
            )
        )
        assert result.returncode == 1, result.stdout + result.stderr
        starts = {path.name.removeprefix("start-") for path in project.root.glob("start-*")}
        finishes = {path.name.removeprefix("finish-") for path in project.root.glob("finish-*")}
        assert starts == finishes
        assert 1 <= len(starts) <= (1 if route == "pytest" else 4)
        failure_tag = "failure" if phase == "call" else "error"
        cases = list(ET.parse(project.root / "result.xml").getroot().iter("testcase"))
        assert (
            1
            <= sum(case.find(failure_tag) is not None for case in cases)
            <= (1 if route == "pytest" else 4)
        )
        if route == scheduling:
            record = json.loads(record_path.read_text())
            assert record["collected"] == 128
            assert 1 <= len(record["cases"]) <= 4
            indices = [int(case["nodeid"].split("[")[-1][:-1]) for case in record["cases"]]
            assert indices == sorted(set(indices))
            for case in record["cases"]:
                failures = [
                    (p["phase"], p["outcome"])
                    for p in case["phases"]
                    if p["outcome"] in {"failed", "error"}
                ]
                assert failures == [(phase, "failed" if phase == "call" else "error")]


def test_scope_scheduling_keeps_classes_whole_and_balances_recorded_durations(
    tmp_path: Path,
) -> None:
    records: dict[str, list[tuple[object, ...]]] = {}
    for route in ("pytest", "scope"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_unit.py",
            "import os\n"
            "import unittest\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "def record(name):\n"
            "    Path(f'owner-{name}').write_text(str(os.getpid()))\n"
            "class TestHeavy(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        Path(f'class-setup-{os.getpid()}').touch()\n"
            "    def test_a(self):\n"
            "        record('heavy-a')\n"
            "    def test_b(self):\n"
            "        record('heavy-b')\n"
            "class TestLight:\n"
            "    def test_a(self):\n"
            "        record('light-a')\n"
            "    def test_b(self):\n"
            "        record('light-b')\n"
            "@pytest.mark.parametrize('value', ['x::y', 'z'])\n"
            "def test_function(value):\n"
            "    record(f'function-{len(value)}')\n"
            "    assert value != 'z'\n",
        )
        arguments = ("-q", "--junitxml=result.xml", "-o", "junit_family=xunit1")
        if route == "pytest":
            result = project.run_pytest(*arguments, load_xdist=False)
        else:
            durations = {
                "test_unit.py::TestHeavy::test_a": 1_000.0,
                "test_unit.py::TestHeavy::test_b": 1_000.0,
                "test_unit.py::TestLight::test_a": 1.0,
                "test_unit.py::TestLight::test_b": 1.0,
                "test_unit.py::test_function[x::y]": 1.0,
                "test_unit.py::test_function[z]": 1.0,
            }
            project.write(
                "history.json",
                json.dumps(
                    {
                        "tests": {
                            nodeid: {"duration_ms": value} for nodeid, value in durations.items()
                        }
                    }
                ),
            )
            result = project.run_vfy(
                "test",
                "--engine=native",
                "--workers=2",
                "--native-scheduling=scope",
                "--history=history.json",
                "--no-replay",
                "--no-cov",
                *arguments,
            )
        result.expect(1)
        records[route] = _junit_cases(project)
        owners = {
            path.name.removeprefix("owner-"): path.read_text()
            for path in project.root.glob("owner-*")
        }
        assert len(owners) == 6
        assert owners["heavy-a"] == owners["heavy-b"]
        assert owners["light-a"] == owners["light-b"]
        assert owners["function-4"] == owners["function-1"]
        assert len(list(project.root.glob("class-setup-*"))) == 1
        if route == "scope":
            # Recorded durations isolate the expensive class; one worker runs
            # the light class and module functions without splitting either.
            assert owners["heavy-a"] != owners["light-a"]
            assert owners["light-a"] == owners["function-1"]
    assert records["pytest"] == records["scope"]


def test_case_scheduling_rejects_invalid_policy_before_test_execution(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_family.py",
        "from pathlib import Path\ndef test_family():\n    Path('ran').touch()\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        env={"VFY_NATIVE_SCHEDULING": "unknown"},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "VFY_NATIVE_SCHEDULING must be file, scope, cases, striped or demand"
        in result.stdout + result.stderr
    )
    assert not (project.root / "ran").exists()


@pytest.mark.parametrize("scheduling", ["file", "scope", "cases", "striped"])
def test_native_scheduling_preserves_pytest_exit_status_and_message(
    tmp_path: Path, scheduling: str
) -> None:
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_family.py",
            "import os\n"
            "from pathlib import Path\n"
            "import pytest\n"
            "@pytest.mark.parametrize('value', range(8))\n"
            "def test_family(value):\n"
            "    Path(f'ran-{os.getpid()}-{value}').touch(exist_ok=False)\n"
            "    pytest.exit('requested exit sentinel', returncode=7)\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=4",
                "--no-replay",
                "--no-cov",
                "--native-result-json=controller-result.json",
                "-q",
                env={"VFY_NATIVE_SCHEDULING": scheduling},
            )
        )
        # A peer may receive SIGINT immediately after creating its marker.
        markers = sorted(path.name for path in project.root.glob("ran-*"))
        workers = {name.split("-")[1] for name in markers}
        result_path = project.root / "controller-result.json"
        recorded = result_path.read_text() if result_path.exists() else "not published"
        diagnostic = (
            f"route={route} scheduling={scheduling} returncode={result.returncode}\n"
            f"executions={markers}\ncontroller result={recorded}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert result.returncode == 7, diagnostic
        if route == "native":
            assert json.loads(recorded)["engine"] == "vfy-independent-v1", diagnostic
        assert 1 <= len(markers) <= (1 if route == "pytest" or scheduling == "file" else 4)
        assert len(workers) == len(markers), diagnostic
        assert "requested exit sentinel" in result.stdout + result.stderr
