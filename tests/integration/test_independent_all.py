"""Explicit full-suite requests execute independently without selection or replay."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

import pytest

from tests.integration.test_watch import _wait_for_watch_event
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="independent execution is unavailable on Windows"
)


@pytest.mark.parametrize(
    ("workers", "coverage"),
    [(1, False), (1, True), (2, False), (2, True)],
)
def test_all_executes_every_module_again_with_actual_worker_owners(
    tmp_path: Path, workers: int, coverage: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(".vfy/keep", "")
    project.write("subject.py", "def value():\n    return 7\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\nimport pytest\n"
        "@pytest.fixture(autouse=True)\ndef tracked(request):\n"
        "    path = Path('.vfy') / (request.node.name + '.events')\n"
        "    with path.open('a') as stream:\n        stream.write('setup\\n')\n"
        "    yield\n"
        "    with path.open('a') as stream:\n        stream.write('teardown\\n')\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import os\nfrom pathlib import Path\nfrom subject import value\n"
            f"def test_{name}():\n    assert value() == 7\n"
            f"    with Path('.vfy/test_{name}.events').open('a') as stream:\n"
            "        stream.write('body\\n')\n"
            f"    with Path('.vfy/test_{name}.pids').open('a') as stream:\n"
            "        stream.write(str(os.getpid()) + '\\n')\n",
        )
    coverage_args = ("--cov=subject.py", "--cov-report=") if coverage else ("--no-cov",)
    previous = (
        project.write(".coverage", "unread prior data").read_bytes() if not coverage else None
    )
    for attempt in range(1, 3):
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--all",
            f"--workers={workers}",
            *coverage_args,
            "--native-result-json=.vfy/actual.json",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        record = json.loads((tmp_path / ".vfy/actual.json").read_text())
        assert record["engine"] == "vfy-independent-v1"
        assert [case["nodeid"] for case in record["cases"]] == [
            "test_a.py::test_a",
            "test_b.py::test_b",
        ]
        assert record["counts"]["passed"] == 2
        assert all(
            [(phase["phase"], phase["outcome"]) for phase in case["phases"]]
            == [("setup", "passed"), ("call", "passed"), ("teardown", "passed")]
            for case in record["cases"]
        )
        actual_pids = []
        for name in ("a", "b"):
            assert (tmp_path / f".vfy/test_{name}.events").read_text().splitlines() == [
                "setup",
                "body",
                "teardown",
            ] * attempt
            pids = (tmp_path / f".vfy/test_{name}.pids").read_text().splitlines()
            assert len(pids) == attempt
            actual_pids.append(int(pids[-1]))
        assert len(set(actual_pids)) == workers
        assert "Reused" not in result.stderr
        if coverage:
            assert project.data().lines(str(tmp_path / "subject.py")) == [1, 2]
            assert project.lines("subject.py", ".vfy/replay.coverage") == [1, 2]
        else:
            assert not (tmp_path / ".vfy/replay.coverage").exists()
            assert (tmp_path / ".coverage").read_bytes() == previous


@pytest.mark.parametrize(
    "selection",
    [
        ("-k", "chosen"),
        ("-kchosen",),
        ("-m", "slow"),
        ("--ignore=test_scope.py",),
        ("--deselect=test_scope.py::test_other",),
        ("test_scope.py",),
        ("test_scope.py::test_chosen",),
        ("test_scope.py:3",),
        ("--collect-only",),
        ("--lf",),
    ],
)
def test_all_rejects_invocation_filters_before_collection_or_output_replacement(
    tmp_path: Path, selection: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_scope.py",
        "from pathlib import Path\nPath('imported').touch()\n"
        "def test_chosen(): pass\ndef test_other(): pass\n",
    )
    before = {
        name: project.write(name, "accepted bytes").read_bytes()
        for name in (".coverage", ".vfy/actual.json")
    }
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--no-cov",
        "--native-result-json=.vfy/actual.json",
        *selection,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(1)
    expected = (
        "--all requires the unfiltered safe test workflow"
        if selection == ("test_scope.py:3",)
        else "--all cannot be combined with pytest test-selection options"
    )
    assert expected in result.stderr
    assert not (tmp_path / "imported").exists()
    assert {name: (tmp_path / name).read_bytes() for name in before} == before


@pytest.mark.parametrize("source", ["environment", "argument-file"])
def test_all_uses_resolved_environment_and_recursive_argument_file_scope(
    tmp_path: Path, source: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_scope.py",
        "from pathlib import Path\nPath('imported').touch()\ndef test_chosen(): pass\n",
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    if source == "environment":
        environment["PYTEST_ADDOPTS"] = "-k chosen"
        arguments = ()
    else:
        project.write("inner.args", "-k\nchosen\n")
        project.write("outer.args", "@inner.args\n")
        arguments = ("@outer.args",)
    result = project.run_vfy(
        "test", "--engine=native", "--all", "--no-cov", *arguments, env=environment
    )
    result.expect(1)
    assert "--all cannot be combined with pytest test-selection options" in result.stderr
    assert not (tmp_path / "imported").exists()


@pytest.mark.parametrize("engine", ["native", "pytest-native", "pytest"])
def test_all_preserves_the_existing_cross_engine_contradiction_error(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_scope.py",
        "from pathlib import Path\nPath('imported').touch()\ndef test_chosen(): pass\n",
    )
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--all",
        "--no-cov",
        "-k",
        "chosen",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(1)
    assert "--all cannot be combined with pytest test-selection options" in result.stderr
    assert not (tmp_path / "imported").exists()


@pytest.mark.parametrize(
    "addopts", ["-k chosen", "-m slow", "--deselect tests/test_scope.py::test_other"]
)
def test_all_preserves_static_configured_selectors_and_directory_scope(
    tmp_path: Path, addopts: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini", f"[pytest]\naddopts = {addopts}\nmarkers = slow: configured suite\n"
    )
    project.write("test_outside.py", "raise AssertionError('outside requested directory')\n")
    project.write(
        "tests/test_scope.py",
        "import pytest\n@pytest.mark.slow\n"
        "def test_chosen(): pass\ndef test_other(): raise AssertionError('deselected')\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--no-cov",
        "--native-result-json=actual.json",
        "tests",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    record = json.loads((tmp_path / "actual.json").read_text())
    assert record["engine"] == "vfy-independent-v1"
    assert [case["nodeid"] for case in record["cases"]] == ["tests/test_scope.py::test_chosen"]
    assert record["counts"]["passed"] == 1


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
@pytest.mark.parametrize("workers", [1, 2])
def test_all_watch_executes_each_generation_and_finalizes_before_publication(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(".vfy/keep", "")
    project.write("subject.py", "VALUE = 1\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\nimport pytest\n"
        "from subject import VALUE\n@pytest.fixture(autouse=True)\n"
        "def tracked(request):\n    yield\n"
        "    Path(f'.vfy/final-{request.node.name}-{VALUE}').touch()\n",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            f"from subject import VALUE\ndef test_{name}():\n    assert VALUE != 2\n",
        )
    command = project.start_vfy(
        "test",
        "--engine=native",
        "--all",
        "--watch",
        f"--workers={workers}",
        "--no-cov",
        "--native-result-json=.vfy/actual.json",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    generation = 0
    try:
        for value in (1, 2, 3):
            if value != 1:
                project.write("subject.py", f"VALUE = {value}\n")
            generation = _wait_for_watch_event(
                command, after_run=generation, expected=f"pytest exited {int(value == 2)}"
            )
            assert all(
                (tmp_path / f".vfy/final-test_{name}-{value}").exists() for name in ("a", "b")
            )
            record = json.loads((tmp_path / ".vfy/actual.json").read_text())
            assert record["engine"] == "vfy-independent-v1"
            assert record["counts"]["failed" if value == 2 else "passed"] == 2
    finally:
        command.send_signal(signal.SIGINT)
        stopped = command.wait(timeout=15)
    stopped.expect(2)


@pytest.mark.parametrize("joined", [False, True])
def test_all_keeps_result_output_operands_distinct_from_selected_test_files(
    tmp_path: Path, joined: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_actual.py", "def test_actual():\n    assert True\n")
    arguments = (
        ("--native-result-json=report.py",)
        if joined
        else (
            "--native-result-json",
            "report.py",
        )
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--no-cov",
        *arguments,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    record = json.loads((tmp_path / "report.py").read_text())
    assert record["engine"] == "vfy-independent-v1"
    assert [case["nodeid"] for case in record["cases"]] == ["test_actual.py::test_actual"]
