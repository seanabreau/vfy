"""Contracts for scoped real-project mutation comparisons."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from benchmarks import benchmark_mutation, benchmark_real_mutation
from benchmarks.benchmark_mutation import (
    VfySample,
    VfySignature,
    _fixture_proof_digest,
    _KillerProofs,
)
from benchmarks.benchmark_real_mutation import (
    CASES,
    _args,
    _check_vfy_round,
    _function_body_ranges,
    _mutmut_dependency_overlay,
    _portable_outcomes,
    _prepare_copy,
    _successful_target_records,
)
from benchmarks.cli import ModeForwarding, _spec

from tests.support.vfy_project import VfyProject


@pytest.mark.parametrize("isolation", [None, "default", "serial", "file"])
@pytest.mark.parametrize("route", [None, "pytest-native"])
def test_real_measurement_uses_independent_default_and_explicit_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolation: str | None, route: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("src/app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    arguments = ["benchmark_real_mutation", "--project", "requests", "--workers", "1"]
    for option, value in (
        ("--root", project.root),
        ("--python", sys.executable),
        ("--preparation-manifest", tmp_path / "manifest.json"),
        ("--artifacts", tmp_path / "artifacts"),
        ("--mutmut", sys.executable),
        ("--mutmut-python", sys.executable),
        ("--mutmut-repository", tmp_path),
        ("--output", tmp_path / "result.json"),
    ):
        arguments.extend((option, str(value)))
    if isolation is not None:
        arguments.extend(("--test-isolation", isolation))
    if route is not None:
        arguments.extend(("--vfy-route", route))
    monkeypatch.setattr(sys, "argv", arguments)
    if route is None and isolation == "file":
        with pytest.raises(SystemExit) as error:
            _args()
        assert error.value.code == 2
        return
    args = _args()

    def measure() -> VfySample:
        return benchmark_real_mutation.measured_vfy(
            project.root,
            args,
            source_paths=(project.root / "src/app.py",),
            pytest_args=("tests/test_app.py::test_value",),
            retain_process=True,
        )

    if sys.platform == "darwin" and route is None:
        with pytest.raises(RuntimeError, match="independent serial mutations require Linux"):
            measure()
        return
    report = measure()
    command = cast(list[str], report["process"]["command"])
    assert report["summary"] == {"protected": 1}
    assert f"--engine={route or 'native'}" in command
    assert args.test_isolation == (isolation or "default")
    if isolation in {None, "default"}:
        assert "--test-isolation" not in command
    else:
        option = command.index("--test-isolation")
        assert command[option + 1] == isolation


def test_real_fixture_keeps_upstream_source_and_restricts_mutmut(
    tmp_path: Path,
) -> None:
    original = tmp_path / "upstream"
    source = original / "datasette" / "utils" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text("def truncate_url(value):\n    return value[:3]\n", encoding="utf-8")
    (original / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (original / "tests").mkdir()
    (original / "tests" / "test_utils.py").write_text("pass\n", encoding="utf-8")
    fixture = original / "tests" / "conftest.py"
    fixture.write_text(
        "def install_event_tracking_plugin():\n"
        '    pm.register(TrackEventPlugin(), name="TrackEventPlugin")\n',
        encoding="utf-8",
    )
    destination = tmp_path / "fixture"

    _prepare_copy(original, destination, CASES["datasette"])

    assert (destination / CASES["datasette"].source).read_bytes() == source.read_bytes()
    assert (original / "pyproject.toml").read_text() == "[project]\nname = 'example'\n"
    assert 'pm.unregister(name="TrackEventPlugin")' not in fixture.read_text()
    adapted_fixture = (destination / "tests" / "conftest.py").read_text()
    assert adapted_fixture.count('pm.unregister(name="TrackEventPlugin")') == 1
    assert adapted_fixture.count('pm.register(TrackEventPlugin(), name="TrackEventPlugin")') == 1
    configuration = (destination / "pyproject.toml").read_text()
    assert 'source_paths = ["datasette/"]' in configuration
    assert 'only_mutate = ["datasette/utils/__init__.py"]' in configuration
    assert 'also_copy = ["docs/"]' in configuration
    assert (
        'pytest_add_cli_args_test_selection = ["tests/test_utils.py::test_truncate_url"]'
        in configuration
    )


def test_utility_module_fixture_selects_all_upstream_tests_and_checks_outcomes(
    tmp_path: Path,
) -> None:
    original = tmp_path / "upstream"
    source = original / "src" / "requests" / "utils.py"
    source.parent.mkdir(parents=True)
    source.write_text("def useful(value):\n    return value + 1\n", encoding="utf-8")
    (original / "pyproject.toml").write_text("[project]\nname = 'example'\n", encoding="utf-8")
    (original / "tests").mkdir()
    (original / "tests" / "test_utils.py").write_text("pass\n", encoding="utf-8")
    case = CASES["requests-utils"]
    destination = tmp_path / "fixture"
    _prepare_copy(original, destination, case)
    config = (destination / "pyproject.toml").read_text()
    assert 'only_mutate = ["src/requests/utils.py"]' in config
    assert 'pytest_add_cli_args_test_selection = ["tests/test_utils.py"]' in config
    assert (destination / case.source).read_bytes() == source.read_bytes()
    records = [
        ("tests/test_utils.py::test_one", "call", "passed", None),
        ("tests/test_utils.py::test_two", "call", "skipped", None),
        ("tests/test_other.py::test_three", "call", "passed", None),
    ]
    assert _successful_target_records(records, case) == records[:2]
    with pytest.raises(RuntimeError, match="failing test"):
        _successful_target_records(
            [*records, ("tests/test_utils.py::test_four", "call", "failed", None)], case
        )


def test_upstream_observer_avoids_project_benchmarks_package(tmp_path: Path) -> None:
    project = VfyProject(tmp_path / "project")
    project.write("benchmarks/__init__.py", "raise RuntimeError('project package imported')\n")
    project.write("tests/test_utils.py", "def test_value():\n    assert True\n")
    records = benchmark_real_mutation._validate_upstream_test(
        benchmark_real_mutation.manifest("packaging"),
        CASES["packaging-utils"],
        project.root,
        Path(sys.executable),
        tmp_path / "artifacts",
    )
    assert ("tests/test_utils.py::test_value", "call", "passed", None) in records


def test_packaging_fixture_removes_only_upstream_pytest_selection_in_copy(
    tmp_path: Path,
) -> None:
    original = tmp_path / "upstream"
    source = original / "src" / "packaging" / "utils.py"
    source.parent.mkdir(parents=True)
    source.write_text("def useful(value):\n    return value + 1\n", encoding="utf-8")
    (original / "tests").mkdir()
    (original / "tests" / "test_utils.py").write_text("pass\n", encoding="utf-8")
    config = original / "pyproject.toml"
    config.write_text(
        "[tool.pytest.ini_options]\n"
        "addopts = [\n"
        '    "-ra",\n'
        '    "-m",\n'
        '    "not property",        # property based tests opt-in only\n'
        "]\n"
        'testpaths = ["tests"]\n'
        "[tool.coverage.report]\n"
        "fail_under = 100\n",
        encoding="utf-8",
    )
    destination = tmp_path / "copy"
    _prepare_copy(original, destination, CASES["packaging-utils"])
    assert '"-m"' in config.read_text()
    adapted = (destination / "pyproject.toml").read_text()
    assert '"-m"' not in adapted
    assert '"-ra"' in adapted
    assert 'testpaths = ["tests/test_utils.py"]' in adapted
    assert 'testpaths = ["tests"]' in config.read_text()
    assert "fail_under = 0" in adapted
    assert "fail_under = 100" in config.read_text()
    assert 'pytest_add_cli_args_test_selection = ["tests/test_utils.py"]' in adapted
    assert (destination / CASES["packaging-utils"].source).read_bytes() == source.read_bytes()
    ranges, names = _function_body_ranges(destination, CASES["packaging-utils"])
    assert names == ["useful"]
    body = (destination / CASES["packaging-utils"].source).read_bytes()
    assert b"return value + 1" in body[ranges[0][0] : ranges[0][1]]


def test_imported_utility_scope_includes_overloads_but_excludes_uncovered_functions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "requests" / "utils.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def direct(value, enabled=False):\n    return value + 1\n"
        "def overload(value: int):\n    return value\n"
        "def overload(value: str):\n    return value\n"
        "def unrelated(value):\n    return value - 1\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests" / "test_utils.py"
    tests.parent.mkdir()
    tests.write_text("from requests.utils import direct, overload\n", encoding="utf-8")
    ranges, names = _function_body_ranges(tmp_path, CASES["requests-utils"])
    body = source.read_bytes()
    assert names == ["direct", "overload", "overload"]
    assert len(ranges) == 3
    assert all(b"unrelated" not in body[start:end] for start, end in ranges)
    assert all(b"enabled=False" not in body[start:end] for start, end in ranges)


def test_upstream_file_parameter_outcomes_compare_across_disposable_copies(
    tmp_path: Path,
) -> None:
    original = tmp_path / "upstream"
    copy = tmp_path / "copy"
    original.mkdir()
    copy.mkdir()
    prefix = "tests/test_utils.py::test_file["
    left = [
        (f"{prefix}{original}/tests/test_utils.py]", "call", "passed", None),
        (f"{prefix}/external/pytest.py]", "call", "passed", None),
    ]
    right = [
        (f"{prefix}/external/pytest.py]", "call", "passed", None),
        (f"{prefix}{copy}/tests/test_utils.py]", "call", "passed", None),
    ]
    assert _portable_outcomes(left, original) == _portable_outcomes(right, copy)


def test_packaging_mutmut_overlay_requires_pinned_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def fake_run_checked(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        names = json.loads(command[-1])
        versions = {name: "8.4.2" if name == "click" else "0" for name in names}
        return subprocess.CompletedProcess(command, 0, json.dumps(versions))

    monkeypatch.setattr(benchmark_real_mutation, "run_checked", fake_run_checked)
    with pytest.raises(RuntimeError, match="installed mutmut dependencies differ"):
        _mutmut_dependency_overlay(Path(sys.executable), CASES["packaging-utils"])
    assert "click" in json.loads(observed[0][-1])


def test_mutmut_catalog_honors_explicit_real_project_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "datasette" / "utils" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text("def truncate_url(value): return value\n", encoding="utf-8")
    observed: list[list[str]] = []

    def fake_run_timed(
        command: list[str], **_kwargs: object
    ) -> tuple[float, subprocess.CompletedProcess[str]]:
        observed.append(command)
        return 1.0, subprocess.CompletedProcess(command, 0, '{"catalog": [], "names": []}')

    monkeypatch.setattr(benchmark_mutation, "run_timed", fake_run_timed)
    keys, elapsed, names = benchmark_mutation.mutmut_catalog(tmp_path, sys.executable, (source,))

    assert (keys, elapsed, names) == (set(), 1.0, {})
    assert json.loads(observed[0][-1]) == ["datasette/utils/__init__.py"]


def test_real_mutation_publication_requires_three_warmups_and_thirty_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    required = [
        "--project",
        "datasette",
        "--root",
        str(tmp_path),
        "--python",
        sys.executable,
        "--preparation-manifest",
        str(tmp_path / "manifest.json"),
        "--artifacts",
        str(tmp_path / "artifacts"),
        "--mutmut",
        sys.executable,
        "--mutmut-python",
        sys.executable,
        "--mutmut-repository",
        str(tmp_path),
        "--output",
        str(tmp_path / "result.json"),
    ]
    for samples in (("--warmups", "2"), ("--runs", "29")):
        monkeypatch.setattr(sys, "argv", ["benchmark_real_mutation", *required, *samples])
        with pytest.raises(SystemExit, match="2"):
            _args()
    monkeypatch.setattr(sys, "argv", ["benchmark_real_mutation", *required])
    args: argparse.Namespace = _args()
    assert (args.warmups, args.runs) == (3, 30)


def test_killer_fixture_digest_accepts_internal_symlinks_and_rejects_escapes(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    actual = fixture / "tests" / "certs" / "actual"
    actual.mkdir(parents=True)
    (actual / "ca.pem").write_text("certificate", encoding="utf-8")
    link = actual.parent / "link"
    link.symlink_to(actual, target_is_directory=True)
    first = _fixture_proof_digest(fixture)
    link.unlink()
    link.symlink_to(actual.parent.parent.parent, target_is_directory=True)
    assert _fixture_proof_digest(fixture) != first
    link.unlink()
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="escaping symlink"):
        _fixture_proof_digest(fixture)


def test_real_mutation_cli_supports_publication_and_validation() -> None:
    spec = _spec("real-mutation")
    assert spec.module == "benchmarks.benchmark_real_mutation"
    assert spec.mode_forwarding is ModeForwarding.MODE_OPTION
    assert spec.supports_validation
    assert spec.supports_preparation


def test_real_round_proves_changed_body_killer_but_rejects_status_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("x", encoding="utf-8")
    key = ("src/app.py", 0, 1, "x", "y")
    old_killer = "tests/test_app.py::test_old"
    new_killer = "tests/test_app.py::test_new"
    report = cast(
        VfySample,
        {
            "mutations": [
                {
                    "path": str(source),
                    "start": 0,
                    "end": 1,
                    "original": "x",
                    "replacement": "y",
                    "status": "protected",
                    "owner": {"phase": "body"},
                    "tests": [old_killer, new_killer],
                    "killer": new_killer,
                }
            ]
        },
    )
    proved: list[tuple[str, ...]] = []

    class RecordingProofs:
        def is_reference_body_mutation(self, *_arguments: object) -> bool:
            return True

        def prove(
            self, _root: Path, _mutation: object, killers: tuple[str, ...], **_kwargs: object
        ) -> None:
            proved.append(killers)

    proofs = cast(_KillerProofs, RecordingProofs())
    reference: VfySignature = ((key, "protected", old_killer),)
    reranked: VfySignature = ((key, "protected", new_killer),)
    _check_vfy_round(reference, reranked, report, root=tmp_path, label="round", proofs=proofs)
    assert proved == [(old_killer, new_killer)]

    drifted: VfySignature = ((key, "survived", None),)
    with pytest.raises(RuntimeError, match="semantic mutation result differs"):
        _check_vfy_round(reference, drifted, report, root=tmp_path, label="round", proofs=proofs)
