"""Compare native fixture method binding with pinned pytest."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterator
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native fixture execution requires Unix",
)

REQUEST_GC_DIAGNOSTIC = (
    Path(__file__).resolve().parents[2]
    / "validation"
    / "pytest_runner"
    / "diagnostics"
    / "request_gc.py"
)


def _pytest_version() -> tuple[int, int]:
    major, minor, *_ = version("pytest").split(".")
    return int(major), int(minor)


@pytest.fixture
def tmp_project_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="vfy-project-", dir="/tmp") as root:
        yield Path(root)


@pytest.mark.parametrize("engine", ["pytest", "pytest-native"])
def test_fixture_methods_bind_to_each_test_instance(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path / engine)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_binding.py",
        """from pathlib import Path
import pytest

class Fixtures:
    @pytest.fixture
    def value(self, request):
        assert not hasattr(self, 'value_from_fixture')
        self.value_from_fixture = object()
        yield self.value_from_fixture
        assert self.checked
        with Path('finished.txt').open('a') as stream:
            stream.write(request.node.nodeid + '\\n')

class TestBinding(Fixtures):
    def test_plain(self, value):
        assert self.value_from_fixture is value
        self.checked = True

    @pytest.mark.parametrize('case', [0, 1])
    def test_family(self, value, case):
        assert self.value_from_fixture is value
        self.checked = True
""",
    )
    result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
    result.expect(0)
    assert "3 passed" in result.stdout
    assert (project.root / "finished.txt").read_text().splitlines() == [
        "test_binding.py::TestBinding::test_plain",
        "test_binding.py::TestBinding::test_family[0]",
        "test_binding.py::TestBinding::test_family[1]",
    ]


@pytest.mark.parametrize("engine", ["pytest", "pytest-native"])
def test_retained_request_cycle_releases_after_session_items_clear(
    tmp_project_root: Path, engine: str
) -> None:
    project = VfyProject(tmp_project_root / engine)
    project.write("runner-ready.txt", "ready\n")
    probe_root = project.root / "probe"
    result = project.run_python(
        str(REQUEST_GC_DIAGNOSTIC),
        f"--engine={engine}",
        env={"NATIVE_GC_PROBE_ROOT": str(probe_root)},
    )
    result.expect(0)

    records = {
        line.partition(" ")[0]: json.loads(line.partition(" ")[2])
        for line in result.stdout.splitlines()
        if line.startswith(("GC_PROBE ", "GC_PROBE_AFTER_SESSION_RELEASE "))
    }
    before = records["GC_PROBE"]
    after = records["GC_PROBE_AFTER_SESSION_RELEASE"]
    assert before["pytest_version"] == version("pytest")
    assert before["exit_status"] == 0
    assert before["weakrefs"] == 1
    assert before["alive_after_gc"] == 1
    assert before["request_view"] == {
        "nodeid": "test_cycle.py::TestCycle::test_request_cycle[1]",
        "params": {"value": 1},
        "instance_is_test_instance": True,
        "session_matches_node": True,
    }
    assert after["alive_after_gc"] == 0


def test_skip_reasons_and_locations_match_pytest(tmp_path: Path) -> None:
    source = """import pytest

@pytest.fixture(scope='session')
def unavailable():
    pytest.skip('shared prerequisite')

@pytest.mark.skip(reason='explicit marker\\n')
def test_marker():
    raise AssertionError('must not run')

def test_first(unavailable):
    raise AssertionError('must not run')

def test_second(unavailable):
    raise AssertionError('must not run')

@pytest.mark.xfail(run=False, reason='disabled case')
def test_notrun():
    raise AssertionError('must not run')
"""
    observer = """import json
from pathlib import Path

rows = []
def pytest_runtest_logreport(report):
    if report.skipped:
        reason = getattr(report, 'wasxfail', None)
        rows.append([report.nodeid, report.when,
                     'xfailed' if reason is not None else 'skipped',
                     reason if reason is not None else report.longrepr[2],
                     reason if reason is not None else report.longreprtext])

def pytest_sessionfinish():
    Path('skips.json').write_text(json.dumps(rows))
"""
    observed: dict[str, list[list[str]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_skip.py", source)
        if engine == "pytest":
            project.write("conftest.py", observer)
        record = project.root / "skips.json"
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_NATIVE_RESULT": str(record) if engine == "pytest-native" else None},
        )
        result.expect(0)
        data = json.loads(record.read_text())
        rows = (
            [
                [
                    case["nodeid"],
                    phase["phase"],
                    phase["outcome"],
                    phase["exception"],
                    phase["detail"],
                ]
                for case in data["cases"]
                for phase in case["phases"]
                if phase["outcome"] in {"skipped", "xfailed"}
            ]
            if engine == "pytest-native"
            else data
        )
        if engine == "pytest":
            rows = [
                row[:3] + [value.removeprefix("reason: ") for value in row[3:]]
                if row[2] == "xfailed"
                else row
                for row in rows
            ]
        observed[engine] = [
            [value.replace(str(project.root), "<ROOT>") for value in row] for row in rows
        ]
    assert observed["pytest-native"] == observed["pytest"]


@pytest.mark.parametrize(
    "case_file",
    [
        "test_compact_families.py",
        "test_direct_parameter_specialization.py",
        "test_fixture_override_invalidation.py",
        "test_scoped_parameters.py",
        "test_semantics.py",
    ],
)
def test_compact_fixture_contracts_match_stock_without_selection(
    tmp_path: Path, case_file: str
) -> None:
    reference = Path(__file__).resolve().parents[2] / "validation" / "pytest_runner"
    records = {}
    events = {}
    statuses = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_contract.py", (reference / "compat_cases" / case_file).read_text())
        project.write("conftest.py", (reference / "compat_cases" / "conftest.py").read_text())
        project.write("phase_probe.py", (reference / "stock_phase_probe.py").read_text())
        phase_path = project.root / "phases.json"
        event_path = project.root / "events.txt"
        environment = {"PYTEST_NATIVE_EVENT_LOG": str(event_path)}
        if engine == "pytest":
            environment["PYTEST_STOCK_PHASE_LOG"] = str(phase_path)
            observer = ["-p", "phase_probe"]
        else:
            environment["PYTEST_NATIVE_RESULT"] = str(phase_path)
            observer = []
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *observer,
            env=environment,
        )
        statuses[engine] = result.returncode
        assert phase_path.is_file(), result.stdout + result.stderr
        if engine == "pytest":
            records[engine] = [json.loads(line) for line in phase_path.read_text().splitlines()]
        else:
            records[engine] = [
                {
                    "nodeid": case["nodeid"],
                    **{key: phase[key] for key in ("phase", "outcome", "stdout", "stderr")},
                }
                for case in json.loads(phase_path.read_text())["cases"]
                for phase in case["phases"]
            ]
        events[engine] = event_path.read_text() if event_path.is_file() else ""
    expected_stock_status = (
        1
        if (
            case_file == "test_direct_parameter_specialization.py"
            or (case_file == "test_compact_families.py" and _pytest_version() < (8, 2))
            or (case_file == "test_fixture_override_invalidation.py" and _pytest_version() < (8, 2))
        )
        else 0
    )
    assert statuses["pytest"] == expected_stock_status
    assert statuses["pytest-native"] == statuses["pytest"]
    assert records["pytest-native"] == records["pytest"]
    assert events["pytest-native"] == events["pytest"]
