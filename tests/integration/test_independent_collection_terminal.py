"""Collection IDs are live terminal output; vfy retains its compact count format."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_DURATION = re.compile(r" in \d+\.\d{2}s\b")


def _without_duration(text: str) -> str:
    """Replace the wall-clock session duration that stock pytest also prints."""

    return _DURATION.sub(" in <duration>", text)


def _collect(
    project: VfyProject, engine: str, *arguments: str, expected_status: int = 0
) -> CommandResult:
    module = "pytest" if engine == "stock" else "vfy.independent.entry"
    result = project.run_python(
        "-B",
        "-m",
        module,
        "-q",
        "--collect-only",
        *arguments,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == expected_status, (result.stdout, result.stderr)
    return result


@pytest.mark.parametrize("engine", ["stock", "native"])
@pytest.mark.parametrize("custom", [False, True])
def test_collection_ids_precede_live_summary_and_unconfigure(
    tmp_path: Path, engine: str, custom: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value():\n    raise AssertionError('must not run')\n")
    if custom:
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

events = []
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    events.append('finish before')
    result = yield
    events.append('finish after')
    print('USER-FINISH-AFTER')
    return result

def pytest_terminal_summary(terminalreporter):
    events.append('summary')
    terminalreporter.write_line('USER-SUMMARY')
    terminalreporter.write_line('')

def pytest_unconfigure(config):
    events.append('unconfigure')
    print('USER-UNCONFIGURE')
    Path('events.json').write_text(json.dumps(events))
""",
        )
    result = _collect(project, engine)
    lines = result.stdout.splitlines()
    nodeid = "test_value.py::test_value"
    assert lines.count(nodeid) == 1
    assert lines[0] == nodeid
    if custom:
        assert json.loads((project.root / "events.json").read_text()) == [
            "finish before",
            "summary",
            "finish after",
            "unconfigure",
        ]
        assert lines.index(nodeid) < lines.index("USER-SUMMARY")
        assert lines.index("USER-SUMMARY") < lines.index("USER-FINISH-AFTER")
        assert lines.index("USER-FINISH-AFTER") < lines.index("USER-UNCONFIGURE")
        assert "USER-SUMMARY\n\n" in result.stdout
        if engine == "native":
            assert _without_duration(result.stdout) == (
                nodeid + "\n\nUSER-SUMMARY\n\n1 test collected in <duration>\n"
                "USER-FINISH-AFTER\nUSER-UNCONFIGURE\n"
            )
    elif engine == "native":
        assert _without_duration(result.stdout) == nodeid + "\n\n1 test collected in <duration>\n"


@pytest.mark.parametrize("engine", ["stock", "native"])
def test_blocked_terminal_keeps_only_user_collection_output(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value(): pass\n")
    project.write(
        "conftest.py",
        "def pytest_collection_finish(session):\n    print('USER-COLLECTION')\n",
    )
    result = project.run_python(
        "-B",
        "-m",
        "pytest" if engine == "stock" else "vfy.independent.entry",
        "--collect-only",
        "-p",
        "no:terminal",
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    result.expect(0)
    assert result.stdout == "USER-COLLECTION\n"


def test_collection_finish_report_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_value.py", "def test_value(): pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(tryfirst=True)
def pytest_collection_finish(session):
    events.append('collection finished')

@pytest.hookimpl(tryfirst=True)
def pytest_report_collectionfinish(config, start_path, items):
    events.append(['report', start_path == Path.cwd(),
                   config is items[0].config, [item.nodeid for item in items]])
    return ['REPORT-LIST-1', 'REPORT-LIST-2']

@pytest.hookimpl(trylast=True, specname='pytest_report_collectionfinish')
def pytest_report_collectionfinish_string():
    return 'REPORT-STRING'

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    outputs = []
    for engine in ("stock", "native"):
        result = (
            project.run_python("-B", "-m", "pytest", "-q", "--collect-only")
            if engine == "stock"
            else project.run_vfy(
                "test", "--engine=native", "--no-cov", "--workers=1", "-q", "--collect-only"
            )
        )
        result.expect(0)
        events = json.loads((project.root / "events.json").read_text())
        assert events == [
            "collection finished",
            ["report", True, True, ["test_value.py::test_value"]],
        ]
        outputs.append([line for line in result.stdout.splitlines() if line.startswith("REPORT-")])
    assert outputs == [["REPORT-STRING", "REPORT-LIST-1", "REPORT-LIST-2"]] * 2


@pytest.mark.parametrize("engine", ["stock", "native"])
def test_empty_quiet_collection_matches_stock_separator(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    # Quiet mode prints a blank separator before the summary; default verbosity prints the header.
    result = _collect(project, engine, expected_status=5)
    assert _without_duration(result.stdout) == "\nno tests collected in <duration>\n"


def test_two_collection_workers_replay_user_output_but_emit_ids_once(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name in ("one", "two"):
        project.write(f"test_{name}.py", f"def test_{name}(): pass\n")
    project.write(
        "conftest.py",
        """import os

def pytest_collection_finish(session):
    print(f'USER-COLLECTION:{os.getpid()}')

def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(f'USER-SUMMARY:{os.getpid()}')

def pytest_unconfigure(config):
    print(f'USER-UNCONFIGURE:{os.getpid()}')
""",
    )
    result = _collect(project, "independent", "--workers=2", "-s")
    lines = result.stdout.splitlines()
    ids = ["test_one.py::test_one", "test_two.py::test_two"]
    for nodeid in ids:
        assert lines.count(nodeid) == 1
    assert lines.index(ids[0]) < lines.index(ids[1])
    child_pids = [line.split(":")[1] for line in lines if line.startswith("USER-COLLECTION:")]
    assert len(child_pids) == len(set(child_pids)) == 2
    summaries = [line.split(":")[1] for line in lines if line.startswith("USER-SUMMARY:")]
    assert len(summaries) == len(set(summaries)) == 3
    controller = set(summaries) - set(child_pids)
    assert len(controller) == 1
    parent = controller.pop()
    assert lines.index(ids[1]) < lines.index(f"USER-SUMMARY:{parent}")
    assert lines.index(f"USER-SUMMARY:{parent}") < lines.index(f"USER-UNCONFIGURE:{parent}")
    collected = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"2 tests collected in \S+s", line)
    ]
    assert len(collected) == 1
    assert collected[0] < lines.index(f"USER-UNCONFIGURE:{parent}")
    for child in child_pids:
        assert lines.count(f"USER-SUMMARY:{child}") == 1
        assert lines.count(f"USER-UNCONFIGURE:{child}") == 1


@pytest.mark.parametrize(
    ("options", "expected", "status"),
    [
        ((), ["test_a.py::test_a", "test_a.py::test_b", "test_z.py::test_z"], 0),
        (("-q",), ["test_a.py: 2", "test_z.py: 1"], 0),
        (("-qq",), ["test_a.py: 2", "test_z.py: 1"], 0),
        (("-o", "verbosity_test_cases=-2"), ["test_a.py: 2", "test_z.py: 1"], 0),
        (("-q", "-k", "test_z"), ["test_z.py: 1"], 0),
        (("-q", "-k", "nomatch"), [], 5),
    ],
)
def test_quiet_collect_only_grouping_matches_stock(
    tmp_path: Path, options: tuple[str, ...], expected: list[str], status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_a.py", "def test_a(): pass\ndef test_b(): pass\n")
    project.write("test_z.py", "def test_z(): pass\n")
    outputs = []
    for engine in ("stock", "native"):
        result = _collect(project, engine, *options, expected_status=status)
        outputs.append([line for line in result.stdout.splitlines() if line.startswith("test_")])
    assert outputs == [expected, expected]


def test_parallel_quiet_collection_groups_match_stock(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_a.py", "def test_a(): pass\ndef test_b(): pass\n")
    project.write("test_z.py", "def test_z(): pass\n")
    stock = _collect(project, "stock", "-q")
    parallel = _collect(project, "independent", "-q", "--workers=2")

    def selected(result: CommandResult) -> list[str]:
        return [line for line in result.stdout.splitlines() if line.startswith("test_")]

    assert selected(stock) == selected(parallel) == ["test_a.py: 2", "test_z.py: 1"]
