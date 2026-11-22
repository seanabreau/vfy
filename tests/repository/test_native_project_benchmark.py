"""Bounded real-project timings preserve scope, state, and public outcome evidence."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from benchmarks.benchmark_harness import BenchmarkMode, SamplingConfig, summarize
from scripts.pytest_execution_record import file_sha256, write_atomic
from validation.pytest_runner import (
    benchmark_native_projects as driver,
    check_native_adoption_matrix as matrix,
)
from validation.pytest_runner.benchmark_native_projects import (
    archive_run,
    preflight_coverage,
    restore_project,
    sample_commands,
    select_inventory,
)
from validation.pytest_runner.check_native_adoption_matrix import (
    PROJECTS_BY_KEY,
    Project,
    invoke,
    junit_outcomes,
    stock_outcome_run,
)

import vfy
from tests.support import VfyProject

# Hypothesis draws literals from modules outside site-packages. An editable
# checkout puts vfy's own modules there, so the native runner, which imports
# more of them than stock pytest, would change the generated examples. Published
# benchmarks install a wheel.
EDITABLE_VFY = not {"site-packages", "dist-packages"} & set(Path(vfy.__file__).parts)


@pytest.fixture
def rich_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Project]:
    root = tmp_path / "controller"
    patch_name = "validation/pytest_runner/patches/rich-deterministic-workload.patch"
    names = {*driver.SOURCE_INPUTS, *matrix.SOURCE_INPUT_SHA256, patch_name, "locks/rich.txt"}
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original input: {name}\n")
    project = replace(PROJECTS_BY_KEY["rich"], dependency_lock=root / "locks/rich.txt")
    for module in (driver, matrix):
        monkeypatch.setattr(module, "ROOT", root)
        monkeypatch.setattr(module, "PROJECTS_BY_KEY", {"rich": project})
    monkeypatch.setattr(
        driver,
        "SOURCE_INPUT_SHA256",
        {name: file_sha256(root / name) for name in driver.SOURCE_INPUTS},
    )
    monkeypatch.setattr(
        matrix,
        "SOURCE_INPUT_SHA256",
        {name: file_sha256(root / name) for name in matrix.SOURCE_INPUT_SHA256},
    )
    return root, root / patch_name, project


def test_prepared_runtime_rejects_changed_rich_patch_before_restoring_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rich_controller: tuple[Path, Path, Project],
) -> None:
    controller, patch, project = rich_controller

    def checkout(_project: Project, destination: Path) -> Path:
        destination.mkdir(parents=True)
        return destination

    def prepare(_project: Project, root: Path, *_args: object) -> None:
        (root / "prepared-input.txt").write_bytes(patch.read_bytes())

    monkeypatch.setattr(driver, "checkout", checkout)
    monkeypatch.setattr(driver, "prepare_python", lambda *_args: Path(sys.executable))
    monkeypatch.setattr(driver, "prepare_project", prepare)
    monkeypatch.setattr(
        driver, "git", lambda _root, *args: "" if args == ("diff",) else project.revision
    )
    monkeypatch.setattr(driver, "interpreter_details", lambda *_args: {"pytest": "9.1.1"})
    monkeypatch.setattr(driver, "require_controller_python", lambda *_args: None)
    monkeypatch.setattr(driver, "verify_installed_distribution_files", lambda *_args: None)
    monkeypatch.setattr(driver, "verify_build_identity", lambda *_args, **_kwargs: {})
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"unchanged candidate")
    args = argparse.Namespace(wheel=wheel, python_version="3.12", timeout=60, build_manifest=wheel)
    work = tmp_path / "work"
    root, snapshot, _, _ = driver.prepare_runtime(project, work, tmp_path / "output", args)
    original = (snapshot / "prepared-input.txt").read_bytes()
    canary = root / "active-canary.txt"
    canary.write_text("preserve the current checkout on refusal")
    patch.write_text("changed workload patch\n")
    monkeypatch.setitem(
        driver.SOURCE_INPUT_SHA256, patch.relative_to(controller).as_posix(), file_sha256(patch)
    )

    with pytest.raises(
        ValueError, match="prepared project, fixture snapshot, or candidate wheel changed"
    ):
        driver.prepare_runtime(project, work, tmp_path / "retry-output", args)

    assert canary.read_text() == "preserve the current checkout on refusal"
    assert (snapshot / "prepared-input.txt").read_bytes() == original


@pytest.mark.parametrize("command", ["benchmark", "adoption"])
def test_rich_patch_change_during_execution_rejects_source_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rich_controller: tuple[Path, Path, Project],
    command: str,
) -> None:
    controller, patch, _ = rich_controller
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate artifact")
    manifest = tmp_path / "build.json"
    manifest.write_text(
        json.dumps(
            {
                "wheel_sha256": file_sha256(wheel),
                "native_profile": "release",
                "source_revision": "a" * 40,
            }
        )
    )
    output = tmp_path / "output"
    arguments = [
        command,
        "--wheel",
        str(wheel),
        "--build-manifest",
        str(manifest),
        "--output",
        str(output),
        "--project",
        "rich",
    ]

    def mutate_patch(*_args: object) -> dict[str, str]:
        patch.write_text("changed workload patch\n")
        return {"project": "rich", "status": "blocked"}

    patch_name = patch.relative_to(controller).as_posix()
    if command == "benchmark":
        arguments.extend(["--work-root", str(tmp_path / "work"), "--validate-only"])
        monkeypatch.setattr(driver, "require_clean", lambda *_args: SimpleNamespace(as_dict=dict))
        monkeypatch.setattr(driver, "hardware_details", dict)
        monkeypatch.setattr(driver, "run_project", mutate_patch)
        monkeypatch.setattr(sys, "argv", arguments)
        with pytest.raises(ValueError, match="source input changed during execution") as error:
            driver.main()
        assert str(error.value).endswith(patch_name)
    else:
        monkeypatch.setattr(matrix, "run_project", mutate_patch)
        monkeypatch.setattr(
            matrix, "validate_matrix", lambda *_args: {"passed": True, "errors": []}
        )
        monkeypatch.setattr(sys, "argv", arguments)
        assert matrix.main() == 1
        result = json.loads((output / "result.json").read_text())
        assert result["validation"]["errors"] == [
            f"source input changed during execution: {patch_name}"
        ]


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.skipif(
    EDITABLE_VFY, reason="Hypothesis collects constants from an editable vfy checkout"
)
@pytest.mark.parametrize("project_key", ["attrs", "python-dateutil"])
def test_prepared_public_hypothesis_inputs_preserve_hooks_profiles_and_selected_inventory(
    tmp_path: Path, project_key: str
) -> None:
    snapshot = tmp_path / "snapshot"
    fixture = VfyProject(snapshot)
    fixture.write("pytest.ini", "[pytest]\nmarkers = upstream_hook_seen\n")
    conftest = fixture.write(
        "tests/conftest.py",
        "import pytest\nfrom hypothesis import settings\n"
        "def pytest_configure():\n"
        "    settings.register_profile('upstream', max_examples=7, database=None, deadline=None)\n"
        "    settings.load_profile('upstream')\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item.add_marker(pytest.mark.upstream_hook_seen)\n"
        "@pytest.fixture(autouse=True)\n"
        "def original_hook_canary(request):\n"
        "    assert request.node.get_closest_marker('upstream_hook_seen') is not None\n",
    )
    fixture.write(
        "tests/test_bound.py",
        "import json\nfrom pathlib import Path\nimport pytest\n"
        "from hypothesis import given, settings, strategies as st\n"
        "class TestGenerated:\n"
        "    @pytest.mark.parametrize('label', ['selected', 'excluded'])\n"
        "    @settings(max_examples=11, database=None, deadline=None)\n"
        "    @given(value=st.integers())\n"
        "    def test_values(self, label, value):\n"
        "        with Path('bound.jsonl').open('a') as output:\n"
        "            output.write(json.dumps([label, value]) + '\\n')\n",
    )
    fixture.write(
        "tests/test_free.py",
        "import json\nfrom pathlib import Path\n"
        "from hypothesis import given, strategies as st\n"
        "@given(value=st.lists(st.integers(), max_size=4))\n"
        "def test_values(value):\n"
        "    with Path('free.jsonl').open('a') as output:\n"
        "        output.write(json.dumps(value) + '\\n')\n",
    )
    fixture.git_init()
    original = conftest.read_bytes()
    metadata = driver.prepare_benchmark_inputs(PROJECTS_BY_KEY[project_key], snapshot)
    assert metadata["seed"] == 0
    assert conftest.read_bytes().startswith(original)
    assert "pytest_vfy_benchmark_seed" in driver.git(snapshot, "diff")
    selectors = (
        "tests/test_bound.py::TestGenerated::test_values[selected]",
        "tests/test_free.py::test_values",
    )
    root = tmp_path / "project"
    streams = []
    for role in ("stock", "native1", "native2", "stock-repeat"):
        restore_project(root, snapshot)
        project = VfyProject(root)
        junit = tmp_path / f"{role}.xml"
        arguments = ("-q", *selectors, f"--junitxml={junit}")
        if role.startswith("stock"):
            result = project.run_pytest(*arguments, load_xdist=False)
        else:
            result = project.run_vfy(
                "test", "--engine=pytest-native", f"--workers={role[-1]}", "--no-cov", *arguments
            )
        result.expect(0)
        outcomes = junit_outcomes(junit)
        assert [(case["classname"], case["name"]) for case in outcomes] == [
            ("tests.test_bound.TestGenerated", "test_values[selected]"),
            ("tests.test_free", "test_values"),
        ]
        assert all(not case["outcomes"] for case in outcomes)
        streams.append(
            [
                [json.loads(line) for line in (root / name).read_text().splitlines()]
                for name in ("bound.jsonl", "free.jsonl")
            ]
        )
    assert all(stream == streams[0] for stream in streams[1:])
    assert [len(stream) for stream in streams[0]] == [11, 7]
    assert all(example[0] == "selected" for example in streams[0][0])


def test_benchmark_seed_name_collision_fails_before_conftest_changes(tmp_path: Path) -> None:
    conftest = VfyProject(tmp_path).write(
        "tests/conftest.py", "pytest_vfy_benchmark_seed = 'upstream-owned'\n"
    )
    original = conftest.read_bytes()
    with pytest.raises(ValueError, match="collides with an existing conftest name"):
        driver.prepare_benchmark_inputs(PROJECTS_BY_KEY["python-dateutil"], tmp_path)
    assert conftest.read_bytes() == original


def test_scope_uses_stock_inventory_order_without_filtering_skip_or_xfail_ids() -> None:
    inventory = ["test.py::skip", "test.py::a", "test.py::xfail", "test.py::b", "test.py::last"]
    assert select_inventory(inventory, 3) == ("test.py::skip", "test.py::xfail", "test.py::last")
    assert select_inventory(inventory, 100) == tuple(inventory)
    assert select_inventory(inventory, 1) == ("test.py::skip",)
    with pytest.raises(ValueError, match="unique stock inventory"):
        select_inventory(["test.py::duplicate", "test.py::duplicate"], 100)


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize("collect_resources", [False, True])
def test_unobserved_public_samples_preserve_outcomes_and_restore_mutable_fixtures(
    tmp_path: Path, collect_resources: bool
) -> None:
    if collect_resources and sys.platform != "linux":
        pytest.skip("child resource accounting requires Linux")
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    project.write("state.txt", "ready")
    project.write("app.py", "def value(): return 1\n")
    project.write(
        "test_first.py",
        "from pathlib import Path\nimport pytest\nfrom app import value\n"
        "def test_pass():\n"
        "    state = Path('state.txt')\n"
        "    assert state.read_text() == 'ready'\n"
        "    state.write_text('spent')\n"
        "    assert value() == 1\n"
        "@pytest.mark.skip(reason='scope retains skipped cases')\n"
        "def test_skip(): pass\n",
    )
    project.write(
        "test_second.py",
        "import pytest\n"
        "@pytest.mark.xfail(reason='scope retains expected failures', strict=True)\n"
        "def test_xfail(): assert False\n",
    )
    root = tmp_path / "project"
    restore_project(root, snapshot)
    descriptor = Project("fixture", "", "", ".", ("test_first.py", "test_second.py"), ())
    run, raw = stock_outcome_run(
        descriptor, root, Path(sys.executable), tmp_path / "observed", tmp_path / "pytest-tmp", 60
    )
    assert run["exit_status"] == 0, run
    assert raw is not None
    expected = junit_outcomes(tmp_path / "observed/junit.xml")
    results = sample_commands(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        tmp_path / "samples",
        ["stock", "native1", "native2"],
        expected,
        SamplingConfig(warmups=0, runs=1, mode=BenchmarkMode.DIAGNOSTIC),
        60,
        collect_resources=collect_resources,
    )
    for record in results["samples"]:
        artifacts = record["artifacts"]
        command = json.loads(Path(artifacts["command.json"]["path"]).read_text())["argv"]
        assert "pytest_execution_probe" not in command
        assert "native.json" not in artifacts
        assert "execution.json" not in artifacts
        assert junit_outcomes(Path(artifacts["junit.xml"]["path"])) == expected
        assert results["summary"][record["role"]]["median_ms"] == record["seconds"] * 1000
        if collect_resources:
            resources = json.loads(Path(artifacts["resources.json"]["path"]).read_text())
            assert resources == record["resources"]
            assert resources["method"] == "linux-single-child-rusage"
            assert (
                resources["collector_sha256"]
                == driver.SOURCE_INPUT_SHA256[
                    "validation/pytest_runner/benchmark_child_resources.py"
                ]
            )
            assert resources["child_command"] == command
            assert resources["child_exit_status"] == 0
            assert resources["child_wall_ms"] == pytest.approx(record["seconds"] * 1000)
            assert resources["total_cpu_ms"] == pytest.approx(
                resources["user_cpu_ms"] + resources["system_cpu_ms"]
            )
            assert resources["peak_rss_bytes"] > 0
            wrapper = record["collector_artifacts"]
            wrapper_status = json.loads(Path(wrapper["status.json"]["path"]).read_text())
            assert wrapper_status["completed"] is True
            assert wrapper_status["exit_status"] == 0
            assert wrapper_status["seconds"] == record["outer_wrapper_seconds"]
        else:
            assert "resources" not in record
    assert len(expected) == 3
    assert sum(bool(case["outcomes"]) for case in expected) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize("full_suite", [False, True])
def test_project_scope_preserves_configured_full_selectors_and_public_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_suite: bool
) -> None:
    snapshot = tmp_path / "snapshot"
    fixture = VfyProject(snapshot)
    fixture.write("pytest.ini", "[pytest]\ntestpaths = tests\n")
    fixture.write("app.py", "def value(number):\n    return number % 2\n")
    fixture.write(
        "tests/test_scope.py",
        "import pytest\nfrom app import value\n"
        "@pytest.mark.skip(reason='retain skipped inventory')\n"
        "def test_skip(): pass\n"
        "@pytest.mark.parametrize('number', range(121))\n"
        "def test_value(number): assert value(number) == number % 2\n"
        "@pytest.mark.xfail(reason='retain expected failures', strict=True)\n"
        "def test_xfail(): assert False\n",
    )
    fixture.write("outside/test_excluded.py", "def test_excluded(): assert False\n")
    fixture.git_init()
    root = tmp_path / "project"
    restore_project(root, snapshot)
    descriptor = Project("iniconfig", "", "", ".", (), ("--cov=app", "--cov-branch"))
    monkeypatch.setitem(driver.PROJECTS_BY_KEY, "iniconfig", descriptor)
    installed = {"fixture-runtime": "unchanged"}
    monkeypatch.setattr(
        driver,
        "prepare_runtime",
        lambda *_args: (root, snapshot, Path(sys.executable), {"installed": installed}),
    )
    # Installed-artifact runs bypass wheel checks; scope and parity stay enabled.
    monkeypatch.setattr(driver, "verify_outcome_artifacts", lambda *_args: None)
    monkeypatch.setattr(driver, "interpreter_details", lambda *_args: installed)
    output = tmp_path / "output"
    args = argparse.Namespace(
        output=output,
        work_root=tmp_path / "work",
        cases=3,
        full_suite=full_suite,
        timeout=60,
        wheel=tmp_path / "unused-fixture-wheel.whl",
        coverage=True,
        validate_only=True,
    )

    row = driver.run_project(
        args, "iniconfig", SamplingConfig(warmups=3, runs=15, mode=BenchmarkMode.DIAGNOSTIC)
    )

    project_output = output / "iniconfig"
    scope = json.loads((project_output / "scope.json").read_text())
    assert len(scope["inventory"]) == 123
    selected = tuple(scope["inventory"]) if full_suite else select_inventory(scope["inventory"], 3)
    assert tuple(scope["selected"]) == selected
    assert row["selected_cases"] == len(selected)
    assert row["status"] == "validated"
    assert all(record["passed"] for record in row["outcome_parity"]["verdicts"].values())
    for phase in ("outcome-preflight", "coverage-preflight"):
        outcomes = []
        for role in ("stock", "native1", "native2"):
            artifacts = project_output / phase / role
            outcomes.append(junit_outcomes(artifacts / "junit.xml"))
            command = json.loads((artifacts / "command.json").read_text())["argv"]
            node_selectors = tuple(argument for argument in command if "::" in argument)
            assert node_selectors == (() if full_suite else tuple(scope["selectors"]))
        assert all(record == outcomes[0] for record in outcomes[1:])
        assert len(outcomes[0]) == len(selected)
        assert sum(bool(case["outcomes"]) for case in outcomes[0]) == 2


def test_large_raw_artifact_archive_round_trips_with_original_and_retained_hashes(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "stdout.txt"
    payload = ("literal output including null \0 and unicode é\n" * 2000).encode()
    raw.write_bytes(payload)
    expected_hash = file_sha256(raw)
    metadata = archive_run(tmp_path)[raw.name]
    retained = Path(metadata["path"])
    assert not raw.exists()
    assert gzip.decompress(retained.read_bytes()) == payload
    assert metadata["sha256"] == expected_hash
    assert metadata["bytes"] == len(payload)
    assert metadata["retained_sha256"] == file_sha256(retained)


def test_command_timeout_retains_partial_streams_and_failed_status(tmp_path: Path) -> None:
    output = tmp_path / "failed"
    command = [
        sys.executable,
        "-c",
        "import time; print('entered child', flush=True); time.sleep(30)",
    ]
    with pytest.raises(subprocess.TimeoutExpired):
        invoke(command, tmp_path, {}, output, 0.5)
    assert (output / "stdout.txt").read_text() == "entered child\n"
    status = json.loads((output / "status.json").read_text())
    assert status["completed"] is False
    assert status["reason"] == "timeout"


@pytest.mark.parametrize(
    ("failed_phase", "runtime_changed"),
    [("coverage", False), ("no_coverage", False), (None, True), ("coverage", True)],
)
def test_project_sampling_failure_retains_only_completed_valid_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_phase: str | None,
    runtime_changed: bool,
) -> None:
    output = tmp_path / "output"
    project_output = output / "iniconfig"
    root, snapshot = tmp_path / "project", tmp_path / "snapshot"
    installed = {"probe": "original-runtime"}
    inventory = ["test_sample.py::test_ok"]
    expected = [{"classname": "test_sample", "name": "test_ok", "outcomes": []}]
    for artifact in (
        "stock-full-inventory/execution.json",
        "outcome-preflight/parity.json",
    ):
        write_atomic(project_output / artifact, {})
    monkeypatch.setattr(
        driver,
        "prepare_runtime",
        lambda *_args: (root, snapshot, Path(sys.executable), {"installed": installed}),
    )
    monkeypatch.setattr(driver, "restore_project", lambda *_args: None)
    monkeypatch.setattr(
        driver,
        "stock_outcome_run",
        lambda *_args: ({"exit_status": 0, "record_errors": []}, {}),
    )
    monkeypatch.setattr(driver, "interpreter_metadata", lambda *_args: {"purelib": "probe"})
    monkeypatch.setattr(driver, "verify_outcome_artifacts", lambda *_args: None)
    monkeypatch.setattr(driver, "normalize_outcome_record", lambda *_args: (None, None, inventory))
    monkeypatch.setattr(driver, "cli_selectors", lambda _project, _root, selected: selected)
    monkeypatch.setattr(
        driver,
        "preflight_outcomes",
        lambda *_args: (
            ["stock", "native1"],
            expected,
            {"native1": {"errors": []}, "native2": {"errors": ["blocked"]}},
        ),
    )
    monkeypatch.setattr(
        driver,
        "preflight_coverage",
        lambda *_args: (
            ["stock", "native1"],
            {"stock": {}, "native1": {}},
            {"errors": {"stock": [], "native1": []}},
        ),
    )
    calls = []
    completed = {}

    def sample(
        _project: Project,
        _root: Path,
        _snapshot: Path,
        _python: Path,
        destination: Path,
        roles: list[str],
        _expected: list[dict[str, Any]],
        _sampling: SamplingConfig,
        _timeout: float,
        _reference: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        phase = destination.name.replace("-", "_")
        calls.append(phase)
        records = [{"phase": "sample", "index": 0, "role": role, "seconds": 1.0} for role in roles]
        if phase == failed_phase:
            write_atomic(destination / "samples.json", {"samples": records[:1]})
            raise ValueError("sample parity changed")
        write_atomic(destination / "samples.json", {"samples": records})
        result = {"samples": records, "summary": {role: summarize([1000.0]) for role in roles}}
        completed[phase] = result
        return result

    def final_runtime(*_args: object) -> dict[str, str]:
        calls.append("runtime_verification")
        return {"probe": "changed-runtime"} if runtime_changed else installed

    monkeypatch.setattr(driver, "sample_commands", sample)
    monkeypatch.setattr(driver, "interpreter_details", final_runtime)
    monkeypatch.setattr(driver, "project_environment", lambda *_args: {})
    args = argparse.Namespace(
        output=output,
        work_root=tmp_path / "work",
        cases=1,
        full_suite=False,
        timeout=60,
        wheel=tmp_path / "wheel.whl",
        coverage=True,
        validate_only=False,
    )
    row = driver.run_project(
        args, "iniconfig", SamplingConfig(warmups=0, runs=1, mode=BenchmarkMode.DIAGNOSTIC)
    )
    assert calls == (
        ["no_coverage", "runtime_verification"]
        if failed_phase == "no_coverage"
        else ["no_coverage", "coverage", "runtime_verification"]
    )
    assert row["outcome_parity"]["verdicts"]["native1"]["passed"]
    assert row["scope"]["sha256"] == file_sha256(project_output / "scope.json")
    if runtime_changed:
        assert row["status"] == "blocked"
        assert row["failed_phase"] == "runtime_verification"
        assert "installed runtime changed" in row["error"]
        assert "no_coverage" not in row
        assert "coverage" not in row
        assert row["artifacts"] == str(project_output)
        if failed_phase is not None:
            assert row["sampling_failure"]["failed_phase"] == failed_phase
            assert row["sampling_failure"]["error"] == "ValueError: sample parity changed"
    else:
        assert failed_phase is not None
        assert row["status"] == "incomplete"
        assert row["failed_phase"] == failed_phase
        assert row["error"] == "ValueError: sample parity changed"
        assert failed_phase not in row
        if failed_phase == "coverage":
            assert row["no_coverage"] == completed["no_coverage"]
        else:
            assert "coverage" not in row
        assert row["artifacts"] == str(project_output / failed_phase.replace("_", "-"))
    if failed_phase is not None:
        partial = project_output / failed_phase.replace("_", "-") / "samples.json"
        assert len(json.loads(partial.read_text())["samples"]) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
def test_coverage_samples_restore_controlled_tmp_path_and_match_public_reports(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    project.write("app.py", "def value(flag):\n    if flag:\n        return 1\n    return 0\n")
    project.write(
        "test_first.py",
        "import os\nfrom pathlib import Path\nfrom app import value\n"
        "def test_tmp_path(tmp_path):\n"
        "    controlled = Path(os.environ['CONTROLLED_TEMP'])\n"
        "    Path('observed-temp.txt').write_text(str(tmp_path))\n"
        "    assert controlled in tmp_path.parents, 'uncontrolled coverage basetemp'\n"
        "    assert not (controlled / 'state.txt').exists()\n"
        "    (controlled / 'state.txt').write_text('used')\n"
        "    assert value(True) == 1\n",
    )
    project.write(
        "test_second.py", "from app import value\ndef test_false(): assert value(False) == 0\n"
    )
    root = tmp_path / "project"
    basetemp = tmp_path / "pytest-tmp"
    descriptor = Project(
        "iniconfig",
        "",
        "",
        ".",
        ("test_first.py", "test_second.py"),
        ("--cov=app", "--cov-branch"),
        environment=(("CONTROLLED_TEMP", str(basetemp)),),
    )
    restore_project(root, snapshot)
    run, _ = stock_outcome_run(
        descriptor, root, Path(sys.executable), tmp_path / "observed", basetemp, 60
    )
    assert run["exit_status"] == 0, run
    expected = junit_outcomes(tmp_path / "observed/junit.xml")
    roles, references, proof = preflight_coverage(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        tmp_path / "preflight",
        60,
        ["stock", "native1", "native2"],
        expected,
    )
    assert roles == ["stock", "native1", "native2"], proof
    result = sample_commands(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        tmp_path / "samples",
        roles,
        expected,
        SamplingConfig(warmups=0, runs=1, mode=BenchmarkMode.DIAGNOSTIC),
        60,
        references,
    )
    assert len(result["samples"]) == 3
    assert basetemp in Path((root / "observed-temp.txt").read_text()).parents


@pytest.mark.skipif(sys.platform != "linux", reason="child resource accounting requires Linux")
@pytest.mark.parametrize("exit_status", [0, 7])
def test_resource_wrapper_preserves_child_exit_and_captured_streams(
    tmp_path: Path, exit_status: int
) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('child stdout'); print('child stderr', file=sys.stderr); "
        f"sys.exit({exit_status})",
    ]
    destination = tmp_path / "command"
    result = driver.invoke_with_resources(command, tmp_path, {}, destination, 60)
    status = json.loads((destination / "status.json").read_text())
    assert status["completed"] is True
    assert result["exit_status"] == status["exit_status"] == exit_status
    assert result["resources"]["child_exit_status"] == exit_status
    assert (destination / "stdout.txt").read_text() == "child stdout\n"
    assert (destination / "stderr.txt").read_text() == "child stderr\n"
    assert json.loads((destination / "command.json").read_text())["argv"] == command


@pytest.mark.skipif(sys.platform != "linux", reason="process-tree PSS requires Linux")
def test_tree_resource_wrapper_counts_a_live_descendant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "sampled.pipe"
    os.mkfifo(release)
    collector = Path(driver.__file__).with_name("benchmark_child_resources.py")
    wrapper = tmp_path / collector.name
    # Keep both processes alive until the real sampler observes them. A fixed
    # lifetime can expire between snapshots when the test suite is busy.
    wrapper.write_text(
        "import hashlib, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(collector.parent)!r})\n"
        "import benchmark_child_resources as collector\n"
        f"assert Path(collector.__file__).resolve() == Path({str(collector.resolve())!r})\n"
        "assert hashlib.sha256(Path(collector.__file__).read_bytes()).hexdigest() == "
        f"{file_sha256(collector)!r}\n"
        # The driver validates the bootstrap identity; these assertions also
        # bind its imported, unmodified sampler to the committed source.
        "collector.__file__ = __file__\n"
        "sample_tree = collector._sample_tree\n"
        "released = False\n"
        "def sample_and_release(*args):\n"
        "    global released\n"
        "    reading = sample_tree(*args)\n"
        "    if reading[2] >= 2 and not released:\n"
        f"        with open({str(release)!r}, 'wb') as stream:\n"
        "            stream.write(b'x')\n"
        "        released = True\n"
        "    return reading\n"
        "collector._sample_tree = sample_and_release\n"
        "raise SystemExit(collector.main())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(driver, "__file__", str(tmp_path / Path(driver.__file__).name))
    descendant = "import sys; stream=open(sys.argv[1], 'rb'); assert stream.read(1) == b'x'"
    command = [
        sys.executable,
        "-c",
        "import subprocess, sys; "
        f"subprocess.run([sys.executable, '-c', {descendant!r}, sys.argv[1]], check=True)",
        str(release),
    ]
    destination = tmp_path / "tree"
    run = driver.invoke_with_resources(command, tmp_path, {}, destination, 60, tree=True)
    resources = run["resources"]
    assert run["exit_status"] == 0
    assert resources["method"] == "linux-process-tree-smaps-rollup"
    assert resources["sample_interval_ms"] == 20
    assert resources["peak_process_count"] >= 2
    assert resources["peak_tree_pss_bytes"] > 0
    assert resources["peak_tree_rss_bytes"] >= resources["peak_tree_pss_bytes"]
    assert json.loads((destination / "resources.json").read_text()) == resources


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
def test_requested_paths_preserve_complete_inventory_and_upstream_marker_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    fixture = VfyProject(snapshot)
    fixture.write("pytest.ini", "[pytest]\naddopts = -m 'not stress'\nmarkers = stress\n")
    fixture.write(
        "tests/test_first.py",
        "import pytest\n"
        "@pytest.mark.parametrize('number', range(5))\n"
        "def test_value(number): assert number >= 0\n"
        "@pytest.mark.stress\n"
        "def test_stress(): assert False\n"
        "@pytest.mark.skip(reason='retain skipped inventory')\n"
        "def test_skip(): pass\n",
    )
    fixture.write(
        "tests/test_second.py",
        "import pytest\n"
        "@pytest.mark.xfail(reason='retain expected failure', strict=True)\n"
        "def test_expected_failure(): assert False\n",
    )
    fixture.write("tests/test_excluded.py", "def test_excluded(): assert False\n")
    fixture.git_init()
    root = tmp_path / "project"
    restore_project(root, snapshot)
    requested = ["tests/test_first.py", "tests/test_second.py"]
    descriptor = Project("iniconfig", "", "", ".", ("tests",), ())
    monkeypatch.setitem(driver.PROJECTS_BY_KEY, "iniconfig", descriptor)
    installed = {"fixture-runtime": "unchanged"}
    preparation = []

    def prepared(project: Project, *_args: object) -> tuple[Path, Path, Path, dict[str, Any]]:
        preparation.append(project.tests)
        return root, snapshot, Path(sys.executable), {"installed": installed}

    monkeypatch.setattr(driver, "prepare_runtime", prepared)
    monkeypatch.setattr(driver, "verify_outcome_artifacts", lambda *_args: None)
    monkeypatch.setattr(driver, "interpreter_details", lambda *_args: installed)
    args = argparse.Namespace(
        output=tmp_path / "output",
        work_root=tmp_path / "work",
        cases=1,
        full_suite=True,
        test_path=requested,
        timeout=60,
        wheel=tmp_path / "unused-fixture-wheel.whl",
        coverage=False,
        validate_only=True,
    )
    row = driver.run_project(
        args, "iniconfig", SamplingConfig(warmups=3, runs=15, mode=BenchmarkMode.DIAGNOSTIC)
    )
    scope = json.loads((args.output / "iniconfig/scope.json").read_text())
    assert preparation == [tuple(requested)]
    assert scope["mode"] == "full-requested-paths"
    assert scope["selectors"] == requested
    assert scope["inventory"] == scope["selected"]
    assert row["selected_cases"] == len(scope["inventory"]) == 7
    assert all(
        "test_stress" not in nodeid and "test_excluded" not in nodeid
        for nodeid in scope["selected"]
    )
    assert row["status"] == "validated"
    assert all(record["passed"] for record in row["outcome_parity"]["verdicts"].values())
    outcomes = [
        junit_outcomes(args.output / "iniconfig/outcome-preflight" / role / "junit.xml")
        for role in ("stock", "native1", "native2")
    ]
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert sum(bool(case["outcomes"]) for case in outcomes[0]) == 2


@pytest.mark.parametrize(
    "options",
    [
        ["--test-path", "tests"],
        ["--full-suite", "--test-path", "tests"],
        ["--full-suite", "--project", "click", "--project", "pluggy", "--test-path", "tests"],
        ["--full-suite", "--project", "click", "--test-path", "../outside"],
        ["--full-suite", "--project", "click", "--test-path", "tests/test.py::test_case[0]"],
        ["--resources", "--coverage"],
    ],
)
def test_invalid_scope_or_resource_options_preserve_output_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: list[str]
) -> None:
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "--wheel",
            str(tmp_path / "wheel.whl"),
            "--build-manifest",
            str(tmp_path / "build.json"),
            "--work-root",
            str(tmp_path / "work"),
            "--output",
            str(output),
            *options,
        ],
    )
    with pytest.raises(SystemExit) as error:
        driver.main()
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="child resource accounting requires Linux")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("child_command", ["another-command"], id="another-child"),
        pytest.param("collector_sha256", "0" * 64, id="another-collector"),
        pytest.param("child_exit_status", False, id="unreaped-status"),
        pytest.param("child_wall_ms", float("nan"), id="nonfinite-wall"),
        pytest.param("total_cpu_ms", float("inf"), id="nonfinite-cpu"),
        pytest.param("system_cpu_ms", -1, id="negative-cpu"),
        pytest.param("peak_rss_bytes", 0, id="missing-rss"),
        pytest.param("total_cpu_ms", 1e12, id="inconsistent-cpu"),
    ],
)
def test_resource_wrapper_rejects_corrupted_evidence_before_publishing_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    original_invoke = driver.invoke
    destination = tmp_path / "command"

    def corrupt_after_reaping(
        command: list[str],
        root: Path,
        environment: dict[str, str],
        output: Path,
        timeout: float,
    ) -> dict[str, Any]:
        result = original_invoke(command, root, environment, output, timeout)
        record_path = destination / "resources.json"
        record = json.loads(record_path.read_text())
        record[field] = value
        record_path.write_text(json.dumps(record))
        return result

    monkeypatch.setattr(driver, "invoke", corrupt_after_reaping)
    with pytest.raises(ValueError, match=r"resource|wall time|total CPU"):
        driver.invoke_with_resources(
            [sys.executable, "-c", "print('actual child')"], tmp_path, {}, destination, 60
        )
    assert not (destination / "status.json").exists()
    assert not (destination / "command.json").exists()
    assert json.loads((destination / "collector/status.json").read_text())["completed"] is True
    assert (destination / "stdout.txt").read_text() == "actual child\n"


@pytest.mark.parametrize("identity_fault", [None, "version", "compiled", "source", "extension"])
def test_compiled_workload_rejects_unbound_extension_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_fault: str | None
) -> None:
    root = tmp_path / "project"
    package = root / "wcwidth"
    package.mkdir(parents=True)
    extension = package / "_wcwidth_c.abi3.so"
    extension.write_bytes(b"prepared extension")
    identity: dict[str, Any] = {
        "version": "0.9.1",
        "compiled": True,
        "source": str(package / "__init__.py"),
        "extension": str(extension),
    }
    if identity_fault == "version":
        identity["version"] = "0.0.0"
    elif identity_fault == "compiled":
        identity["compiled"] = False
    elif identity_fault in {"source", "extension"}:
        identity[identity_fault] = str(tmp_path / "installed" / identity_fault)

    def invoke(*_args: object) -> dict[str, Any]:
        return {"exit_status": 0, "stdout": json.dumps(identity)}

    monkeypatch.setattr(matrix, "invoke", invoke)
    monkeypatch.setattr(matrix.sys, "platform", "linux")
    output = tmp_path / "setup"
    if identity_fault is not None:
        with pytest.raises(RuntimeError, match="compiled extension from the pinned checkout"):
            matrix.prepare_wcwidth_extension(
                PROJECTS_BY_KEY["wcwidth"], root, Path(sys.executable), output, 60
            )
        assert not (output / "extension.json").exists()
    else:
        matrix.prepare_wcwidth_extension(
            PROJECTS_BY_KEY["wcwidth"], root, Path(sys.executable), output, 60
        )
        evidence = json.loads((output / "extension.json").read_text())
        assert evidence["sha256"] == file_sha256(extension)
        assert evidence["source"] == str(package / "__init__.py")
