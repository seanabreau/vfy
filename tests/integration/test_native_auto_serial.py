"""Auto-width single-file pytest runs stay on the native serial lifecycle."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.history import read_history


def _native_engine_supported() -> bool:
    if not sys.platform.startswith(("linux", "darwin")):
        return False
    try:
        return importlib.util.find_spec(
            "vfy._pytest.native_pytest"
        ) is not None and importlib.metadata.version("pytest").split(".", maxsplit=1)[0] in {
            "8",
            "9",
        }
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _native_engine_supported(),
    reason="native pytest requires Linux or macOS and a supported pytest version",
)


def _recording_conftest() -> str:
    return (
        "import os\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "def record(event):\n"
        "    path = Path('.vfy/lifecycle.txt')\n"
        "    path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    worker = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'unset')\n"
        "    with path.open('a', encoding='utf-8') as stream:\n"
        "        stream.write(f'{event}|{os.getpid()}|{worker}\\n')\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    record('collection')\n\n"
        "def pytest_configure(config):\n"
        "    record('session-start')\n\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    record(f'session-finish:{exitstatus}')\n\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def session_lifecycle():\n"
        "    record('session-setup')\n"
        "    yield\n"
        "    record('session-teardown')\n\n"
        "@pytest.fixture(scope='module', autouse=True)\n"
        "def module_lifecycle():\n"
        "    record('module-setup')\n"
        "    yield\n"
        "    record('module-teardown')\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def function_lifecycle(request):\n"
        "    record(f'function-setup:{request.node.name}')\n"
        "    yield\n"
        "    record(f'function-teardown:{request.node.name}')\n"
    )


def _lifecycle(project: VfyProject) -> list[tuple[str, str, str]]:
    rows = []
    for line in (project.root / ".vfy/lifecycle.txt").read_text(encoding="utf-8").splitlines():
        event, pid, worker = line.split("|")
        rows.append((event, pid, worker))
    return rows


def _coverage_semantics(project: VfyProject) -> tuple[object, ...]:
    data = project.data()
    source = str(project.root / "app.py")
    contexts = tuple(
        sorted(
            (line, tuple(sorted(values)))
            for line, values in data.contexts_by_lineno(source).items()
        )
    )
    return tuple(data.lines(source) or ()), tuple(data.arcs(source) or ()), contexts


def _coverage_report_semantics(path: Path) -> tuple[object, object]:
    report = json.loads(path.read_text(encoding="utf-8"))
    files = {Path(name).name: value for name, value in report["files"].items()}
    return report["totals"], files


def _junit_semantics(
    path: Path,
) -> tuple[tuple[str, ...], tuple[tuple[str, str, tuple[str, ...]], ...]]:
    root = ET.parse(path).getroot().find("testsuite")
    assert root is not None
    totals = tuple(root.attrib[name] for name in ("tests", "errors", "failures", "skipped"))
    cases = tuple(
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple(child.tag for child in case),
        )
        for case in root.findall(".//testcase")
    )
    return totals, cases


def _history_semantics(project: VfyProject) -> dict[str, tuple[tuple[str, str | None], ...]]:
    history = read_history(project.root / ".vfy/test-history.sqlite3")
    return {
        nodeid: tuple(
            (observation["outcome"], observation.get("failed_phase"))
            for observation in record["observations"]
        )
        for nodeid, record in history["tests"].items()
    }


def _write_outcome_project(project: VfyProject) -> None:
    project.write(
        "app.py",
        "def decide(case):\n"
        "    if case == 'fail':\n"
        "        return 'wrong'\n"
        "    return 'right'\n\n"
        "def fixture_event(phase):\n"
        "    if phase == 'setup':\n"
        "        return 'ready'\n"
        "    return 'closed'\n",
    )
    project.write("conftest.py", _recording_conftest())
    project.write(
        "tests/test_outcomes.py",
        "import pytest\n"
        "from app import decide, fixture_event\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def app_lifecycle():\n"
        "    fixture_event('setup')\n"
        "    yield\n"
        "    fixture_event('teardown')\n\n"
        "@pytest.mark.parametrize('case', ['pass', 'skip', 'fail', 'never'])\n"
        "def test_outcome(case):\n"
        "    print(f'captured-{case}')\n"
        "    result = decide(case)\n"
        "    if case == 'skip':\n"
        "        pytest.skip('intentional skip')\n"
        "    assert result == 'right'\n",
    )


@pytest.mark.parametrize("workers", ["auto", "logical"])
def test_auto_single_file_matches_serial_lifecycle_coverage_reports_and_history(
    tmp_path: Path,
    workers: str,
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("auto", "serial")}
    for project in projects.values():
        _write_outcome_project(project)

    common = (
        "--no-replay",
        "--cov=app",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=json:coverage.json",
        "--junitxml=results.xml",
        "--html=results.html",
        "--maxfail=1",
        "-q",
    )
    results = {
        name: project.run_vfy(
            "test",
            "--engine=pytest-native",
            f"--workers={workers if name == 'auto' else '1'}",
            *common,
            env={"VFY_NATIVE_PARALLEL_INDEX": None},
        )
        for name, project in projects.items()
    }
    for name, result in results.items():
        output = result.stdout + result.stderr
        assert result.returncode == 1, f"{name}: {output}"
        assert "1 passed" in output
        assert "1 skipped" in output
        assert "1 failed" in output

    expected_nodeids = {
        "tests/test_outcomes.py::test_outcome[pass]": "Passed",
        "tests/test_outcomes.py::test_outcome[skip]": "Skipped",
        "tests/test_outcomes.py::test_outcome[fail]": "Failed",
    }
    auto, serial = projects["auto"], projects["serial"]
    assert _coverage_semantics(auto) == _coverage_semantics(serial)
    assert _coverage_report_semantics(auto.root / "coverage.json") == _coverage_report_semantics(
        serial.root / "coverage.json"
    )
    assert _junit_semantics(auto.root / "results.xml") == _junit_semantics(
        serial.root / "results.xml"
    )
    assert _history_semantics(auto) == _history_semantics(serial)
    for project in projects.values():
        html = (project.root / "results.html").read_text(encoding="utf-8")
        for nodeid, outcome in expected_nodeids.items():
            assert nodeid in html
            assert outcome.lower() in html.lower()
        assert "captured-pass" in html

    events = {name: _lifecycle(project) for name, project in projects.items()}
    for name, rows in events.items():
        labels = [event for event, _pid, _worker in rows]
        pids = {pid for _event, pid, _worker in rows}
        worker_ids = {worker for _event, _pid, worker in rows}
        assert labels.count("collection") == 1, name
        assert labels.count("session-start") == 1, name
        assert labels.count("session-setup") == labels.count("session-teardown") == 1, name
        assert labels.count("module-setup") == labels.count("module-teardown") == 1, name
        assert sum(label.startswith("function-setup:") for label in labels) == 3, name
        assert sum(label.startswith("function-teardown:") for label in labels) == 3, name
        assert not any("[never]" in label for label in labels), name
        assert len(pids) == 1, name
        assert worker_ids == {"unset"}, name
    assert [event for event, _pid, _worker in events["auto"]] == [
        event for event, _pid, _worker in events["serial"]
    ]


def test_explicit_worker_count_keeps_single_file_worker_path(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _recording_conftest())
    project.write(
        "tests/test_cases.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2, 3])\n"
        "def test_value(value):\n"
        "    assert value > 0\n",
    )

    result = project.run_vfy("test", "--engine=pytest-native", "--workers=2", "--no-replay", "-q")

    result.expect(0)
    assert "3 passed" in result.stdout
    rows = _lifecycle(project)
    assert "0" in {worker for _event, _pid, worker in rows}


@pytest.mark.parametrize("failure", ["preflight", "collection"])
def test_auto_single_file_failures_preserve_coverage_and_history(
    tmp_path: Path, failure: str
) -> None:
    project = VfyProject(tmp_path)
    _write_outcome_project(project)
    baseline = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=auto",
        "--no-replay",
        "--cov=app",
        "--cov-report=json:coverage.json",
        "-q",
    )
    baseline.expect(1)
    preserved = {
        path: (project.root / path).read_bytes()
        for path in (".coverage", "coverage.json", ".vfy/test-history.sqlite3")
    }

    if failure == "preflight":
        project.write(
            "conftest.py",
            "def pytest_runtest_call(item):\n    pass\n",
        )
        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=auto",
            "--no-replay",
            "--cov=app",
            "--cov-report=json:coverage.json",
            "-q",
        )
        result.expect(4)
        assert "does not execute conftest hook 'pytest_runtest_call'" in result.stderr
    else:
        project.write(
            "tests/test_outcomes.py",
            "raise RuntimeError('single-file collection sentinel')\n"
            "def test_never_runs():\n"
            "    raise AssertionError('collection error reached test body')\n",
        )
        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=auto",
            "--no-replay",
            "--cov=app",
            "--cov-report=json:coverage.json",
            "-q",
        )
        output = result.stdout + result.stderr
        assert result.returncode == 2, output
        assert "single-file collection sentinel" in output

    assert {path: (project.root / path).read_bytes() for path in preserved} == preserved


@pytest.mark.parametrize("workers", ["auto", "logical"])
def test_automatic_large_host_width_keeps_single_file_in_the_serial_session(
    tmp_path: Path, workers: str
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("upstream", "native")}
    for project in projects.values():
        project.write(
            "conftest.py",
            "import os\n"
            "from pathlib import Path\n"
            "def pytest_configure(config):\n"
            "    os.cpu_count = lambda: 512\n"
            "    os.sched_getaffinity = lambda pid: set(range(512))\n"
            "    with (config.invocation_params.dir / 'sessions.txt').open('a') as stream:\n"
            "        worker = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'serial')\n"
            "        stream.write(worker + '\\n')\n",
        )
        project.write(
            "tests/test_many.py",
            "import pytest\n"
            "@pytest.mark.parametrize('case', range(300))\n"
            "def test_case(case):\n"
            "    assert case >= 0\n",
        )

    oracle = projects["upstream"].run_pytest("-q", load_xdist=False)
    native = projects["native"].run_vfy(
        "test", "--engine=pytest-native", f"--workers={workers}", "--no-replay", "--no-cov", "-q"
    )
    for result in (oracle, native):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "300 passed" in result.stdout
    for project in projects.values():
        assert (project.root / "sessions.txt").read_text(encoding="utf-8") == "serial\n"


def test_auto_multifile_worker_path_remains_active(tmp_path: Path) -> None:
    capacity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    if capacity is None or capacity < 2:
        pytest.skip("auto worker control requires at least two available CPUs")
    project = VfyProject(tmp_path)
    project.write("conftest.py", _recording_conftest())
    for name in ("a", "b"):
        project.write(f"tests/test_{name}.py", f"def test_{name}():\n    assert True\n")

    result = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=auto", "--no-replay", "-q"
    )

    result.expect(0)
    assert "2 passed" in result.stdout
    rows = _lifecycle(project)
    assert {worker for _event, _pid, worker in rows if worker != "unset"} == {"0", "1"}
