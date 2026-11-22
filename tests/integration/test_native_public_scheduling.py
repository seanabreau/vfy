"""Public native scheduling preserves file affinity and separates replay modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_native_parallel import _native_parallel_available
from tests.integration.test_native_parallel_cases import _junit_cases
from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native parallel tests require Linux or macOS and a supported pytest version",
)


def test_public_case_scheduling_splits_family_and_preserves_results_and_finalizers(
    tmp_path: Path,
) -> None:
    records: dict[str, list[tuple[object, ...]]] = {}
    for route in ("stock", "file", "cases"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_family.py",
            "import os\nfrom pathlib import Path\nimport pytest\n"
            "@pytest.fixture\n"
            "def resource(value):\n"
            "    pid = os.getpid()\n"
            "    Path(f'start-{value}-{pid}').touch()\n"
            "    yield pid\n"
            "    Path(f'finish-{value}-{pid}').touch()\n"
            "@pytest.mark.parametrize('value', range(32))\n"
            "def test_family(value, resource, record_property):\n"
            "    Path(f'owner-{value}').write_text(str(resource))\n"
            "    record_property('value', str(value))\n"
            "    print(f'captured-{value}')\n"
            "    if value == 3:\n        pytest.skip('skip example')\n"
            "    if value == 8:\n        pytest.xfail('xfail example')\n"
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
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=4",
                f"--native-scheduling={route}",
                "--no-replay",
                "--no-cov",
                *arguments,
                env={"VFY_NATIVE_SCHEDULING": "file" if route == "cases" else "cases"},
            )
        )
        output = result.stdout + result.stderr
        assert result.returncode == 1, output
        records[route] = _junit_cases(project)
        owners = {path.read_text() for path in project.root.glob("owner-*")}
        assert len(owners) == (4 if route == "cases" else 1), output
        assert len(list(project.root.glob("owner-*"))) == 32
        starts = {path.name.removeprefix("start-") for path in project.root.glob("start-*")}
        finishes = {path.name.removeprefix("finish-") for path in project.root.glob("finish-*")}
        assert starts == finishes
        assert len(starts) == 32
    assert records["stock"] == records["file"] == records["cases"]


def test_default_native_scheduling_preserves_shared_module_state_and_order(tmp_path: Path) -> None:
    for route in ("stock", "default"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_family.py",
            "import os\nfrom pathlib import Path\nimport pytest\n"
            "next_value = 0\n"
            "@pytest.mark.parametrize('value', range(16))\n"
            "def test_family(value):\n"
            "    global next_value\n"
            "    assert value == next_value\n"
            "    next_value += 1\n"
            "    with Path(f'events-{os.getpid()}').open('a') as stream:\n"
            "        stream.write(str(value) + '\\n')\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=4",
                "--no-replay",
                "--no-cov",
                "-q",
                env={"VFY_NATIVE_SCHEDULING": None},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        events = [path.read_text().splitlines() for path in project.root.glob("events-*")]
        assert events == [[str(value) for value in range(16)]]


def test_public_case_ranges_preserve_reordered_calls_nextitem_and_finalizers(
    tmp_path: Path,
) -> None:
    conftest = (
        "import json\nimport os\nfrom pathlib import Path\nimport pytest\n"
        "def note(event, nodeid, nextitem=None):\n"
        "    with Path(f'events-{os.getpid()}.jsonl').open('a') as stream:\n"
        "        stream.write(json.dumps([event, nodeid, nextitem]) + '\\n')\n"
        "def pytest_collection_modifyitems(items):\n"
        "    if not os.getenv('ORDERED_SUBSET'):\n        items.reverse()\n"
        "def pytest_runtest_teardown(item, nextitem):\n"
        "    note('teardown-hook', item.nodeid, nextitem.nodeid if nextitem else None)\n"
        "@pytest.fixture\n"
        "def resource(request):\n"
        "    note('setup', request.node.nodeid)\n"
        "    request.addfinalizer(lambda: note('finalizer', request.node.nodeid))\n"
    )
    source = (
        "from conftest import note\nimport pytest\n"
        "@pytest.mark.parametrize('value', range(12))\n"
        "def test_family(value, resource, request):\n"
        "    note('call', request.node.nodeid)\n"
    )
    native = VfyProject(tmp_path / "native")
    native.write("conftest.py", conftest)
    native.write("test_family.py", source)
    record = native.root / "execution.json"
    result = native.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=4",
        "--native-scheduling=cases",
        "--no-replay",
        "--no-cov",
        "-q",
        env={"PYTEST_NATIVE_RESULT": str(record)},
    )
    result.expect(0)
    expected = [f"test_family.py::test_family[{value}]" for value in reversed(range(12))]
    assert [case["nodeid"] for case in json.loads(record.read_text())["cases"]] == expected
    traces = list(native.root.glob("events-*.jsonl"))
    assert len(traces) == 4
    assignments: list[list[str]] = []
    for index, trace in enumerate(traces):
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        assignment = [event[1] for event in events if event[0] == "call"]
        assignments.append(assignment)
        reference = VfyProject(tmp_path / f"reference-{index}")
        reference.write("conftest.py", conftest)
        reference.write("test_family.py", source)
        # The oracle receives the worker's final order, after the collection reversal.
        oracle = reference.run_pytest(
            *assignment, "-q", load_xdist=False, env={"ORDERED_SUBSET": "1"}
        )
        oracle.expect(0)
        assert [path.read_text() for path in reference.root.glob("events-*.jsonl")] == [
            trace.read_text()
        ]
    assert sorted(assignments, key=lambda assignment: expected.index(assignment[0])) == [
        expected[index : index + 3] for index in range(0, 12, 3)
    ]


def _replay_project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("src/app.py", "def read_value(value):\n    return value\n")
    project.write(
        "test_family.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "from src.app import read_value\n"
        "@pytest.mark.parametrize('value', range(8))\n"
        "def test_family(value):\n"
        "    assert read_value(value) == value\n"
        "    path = Path(f'.vfy/executions-{value}')\n"
        "    path.parent.mkdir(exist_ok=True)\n"
        "    with path.open('a') as stream:\n"
        "        stream.write(str(os.getpid()) + '\\n')\n",
    )
    project.git_init()
    return project


def test_native_replay_resolves_public_and_experimental_scheduling_consistently(
    tmp_path: Path,
) -> None:
    project = _replay_project(tmp_path)
    for option, experimental, executions, reused in (
        (None, None, 1, False),
        ("file", "cases", 1, True),
        ("cases", "file", 2, False),
        (None, "cases", 2, True),
        (None, "file", 3, False),
    ):
        result = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--workers=4",
            *([f"--native-scheduling={option}"] if option else []),
            "--source=src",
            "--cov-report=",
            "-q",
            env={"VFY_NATIVE_SCHEDULING": experimental},
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert ("Reused 8 tests from passing baseline" in result.stderr) is reused, output
        assert [
            len((tmp_path / f".vfy/executions-{value}").read_text().splitlines())
            for value in range(8)
        ] == [executions] * 8


def test_ci_baseline_runs_full_after_native_scheduling_changes(tmp_path: Path) -> None:
    project = _replay_project(tmp_path)
    exported = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=4",
        "--native-scheduling=file",
        "--all",
        "--export-baseline=.vfy/baseline.json",
        "--source=src",
        "--cov-report=",
        "-q",
        env={"VFY_NATIVE_SCHEDULING": None},
    )
    exported.expect(0)
    bundle = (tmp_path / ".vfy/baseline.json").read_bytes()
    selected = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=4",
        "--native-scheduling=cases",
        "--baseline=.vfy/baseline.json",
        "--plan-output=.vfy/plan.json",
        "--source=src",
        "--cov-report=",
        "-q",
    )
    output = selected.stdout + selected.stderr
    assert selected.returncode == 0, output
    assert json.loads((tmp_path / ".vfy/plan.json").read_text())["selection"]["fallback_full"]
    assert [
        len((tmp_path / f".vfy/executions-{value}").read_text().splitlines()) for value in range(8)
    ] == [2] * 8
    assert (tmp_path / ".vfy/baseline.json").read_bytes() == bundle


@pytest.mark.parametrize(
    ("arguments", "diagnostic"),
    [
        (("--engine=pytest-native", "--native-scheduling=unknown"), "invalid choice"),
        (
            ("--native-scheduling=cases", "--engine=pytest"),
            "--native-scheduling requires the native or independent engine",
        ),
        (
            ("--engine=pytest-native", "--native-scheduling=cases", "--numprocesses", "2"),
            "--native-scheduling cannot be combined with pytest-xdist",
        ),
    ],
)
def test_invalid_native_scheduling_contract_does_not_execute_or_replace_data(
    tmp_path: Path, arguments: tuple[str, ...], diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_case.py", "from pathlib import Path\ndef test_case():\n    Path('ran').touch()\n"
    )
    previous = project.write(".coverage", "prior data").read_bytes()
    rejected = project.run_vfy("test", "--engine=pytest-native", "--workers=1", *arguments)
    output = rejected.stdout + rejected.stderr
    assert rejected.returncode != 0, output
    assert diagnostic in output
    assert not (tmp_path / "ran").exists()
    assert (tmp_path / ".coverage").read_bytes() == previous
