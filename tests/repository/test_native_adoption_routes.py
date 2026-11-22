"""Prove adoption routes and comparisons with real pytest/native records."""

from __future__ import annotations

import copy
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from coverage import CoverageData
from scripts.pytest_execution_record import file_sha256
from validation.pytest_runner import check_native_adoption_matrix as matrix
from validation.pytest_runner.check_native_adoption_matrix import (
    Project,
    compare_outcomes,
    first_difference,
    native_outcome_run,
    stock_outcome_run,
)

from tests.support import VfyProject


@dataclass(frozen=True)
class OutcomePair:
    stock_run: dict[str, Any]
    native_run: dict[str, Any]
    stock_raw: dict[str, Any]
    native_raw: dict[str, Any]
    root: Path
    basetemp: Path
    command: list[str]

    def compare(self, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        return compare_outcomes(
            self.stock_run,
            self.native_run,
            self.stock_raw,
            self.native_raw if candidate is None else candidate,
            self.root,
            self.root,
            Path(sys.executable),
            self.basetemp,
        )


@pytest.fixture(scope="module")
def outcome_pairs(tmp_path_factory: pytest.TempPathFactory) -> list[OutcomePair]:
    root = tmp_path_factory.mktemp("adoption-routes")
    project = VfyProject(root)
    project.write(
        "test_first.py",
        """import pytest
import warnings

@pytest.fixture
def noisy():
    print("setup capture")
    yield
    print("teardown capture")

def test_capture(noisy):
    print("call capture")
    warnings.warn("reviewed warning", UserWarning)

@pytest.mark.skip(reason="reviewed skip")
def test_skip():
    pass
""",
    )
    project.write(
        "test_second.py",
        """import pytest

@pytest.mark.xfail(reason="reviewed expected failure")
def test_expected_failure():
    assert False
""",
    )
    descriptor = Project("fixture", "", "", ".", (".",), ())
    python = Path(sys.executable)
    basetemp = root / "pytest-temp"
    stock_run, stock_raw = stock_outcome_run(descriptor, root, python, root / "stock", basetemp, 60)
    assert stock_raw is not None, stock_run
    pairs = []
    for workers in (1, 2):
        output = root / f"native-{workers}"
        native_run, native_raw = native_outcome_run(
            descriptor, root, python, output, basetemp, 60, workers=workers
        )
        assert native_raw is not None, native_run
        command = json.loads((output / "command.json").read_text(encoding="utf-8"))["argv"]
        pairs.append(
            OutcomePair(stock_run, native_run, stock_raw, native_raw, root, basetemp, command)
        )
    return pairs


def test_explicit_native_engine_and_two_worker_routes_match_real_stock_observations(
    outcome_pairs: list[OutcomePair],
) -> None:
    for workers, pair in enumerate(outcome_pairs, 1):
        comparison = pair.compare()
        assert comparison["equal"], comparison
        assert comparison["record_errors"] == []
        assert pair.native_raw["engine"] == "pytest-runner-native-v1"
        assert f"--workers={workers}" in pair.command
        assert [argument for argument in pair.command if argument.startswith("--engine")] == [
            "--engine=pytest-native"
        ]
    assert (
        "vfy.pytest_plugin"
        not in json.loads(
            (outcome_pairs[0].root / "stock" / "command.json").read_text(encoding="utf-8")
        )["argv"]
    )


@pytest.mark.parametrize(
    ("mutation", "mismatch"),
    [
        ("wrong-engine", "engine"),
        ("empty", "execution-record"),
        ("missing-case", "execution-record"),
        ("malformed-case", "execution-record"),
        ("missing-teardown", "execution-record"),
        ("missing-capture", "execution-record"),
        ("reordered", "ordered-nodeids"),
        ("changed-capture", "captured-output"),
        ("changed-diagnostic", "diagnostics"),
        ("changed-warning", "case-warnings"),
        ("changed-outcome", "case-outcomes"),
    ],
)
def test_comparison_rejects_incomplete_or_changed_real_native_records(
    outcome_pairs: list[OutcomePair], mutation: str, mismatch: str
) -> None:
    pair = outcome_pairs[0]
    raw = copy.deepcopy(pair.native_raw)
    if mutation == "wrong-engine":
        raw["engine"] = "pytest"
    elif mutation == "empty":
        raw["cases"] = []
        raw["collected"] = 0
    elif mutation == "missing-case":
        raw["cases"].pop()
    elif mutation == "malformed-case":
        raw["cases"].append({"nodeid": 42})
        raw["collected"] += 1
    elif mutation == "missing-teardown":
        raw["cases"][0]["phases"].pop()
    elif mutation == "missing-capture":
        raw["cases"][0]["phases"][1].pop("stdout")
    elif mutation == "reordered":
        raw["cases"].reverse()
    elif mutation == "changed-capture":
        raw["cases"][0]["phases"][1]["stdout"] = "changed capture"
    elif mutation == "changed-diagnostic":
        raw["cases"][0]["phases"][1]["detail"] = "changed diagnostic"
    elif mutation == "changed-warning":
        raw["warnings"][0]["message"] = "changed warning"
    elif mutation == "changed-outcome":
        raw["cases"][0]["phases"][1]["outcome"] = "skipped"
    comparison = pair.compare(raw)
    assert not comparison["equal"], comparison
    assert mismatch in comparison["mismatch_kinds"], comparison


def test_exit_status_and_collection_identity_are_part_of_parity(
    outcome_pairs: list[OutcomePair],
) -> None:
    pair = outcome_pairs[0]
    stock = copy.deepcopy(pair.stock_raw)
    stock["collected_nodeids"].reverse()
    comparison = compare_outcomes(
        pair.stock_run,
        {**pair.native_run, "exit_status": 1},
        stock,
        pair.native_raw,
        pair.root,
        pair.root,
        Path(sys.executable),
        pair.basetemp,
    )
    assert not comparison["equal"], comparison
    assert comparison["status_equal"] is False
    assert "ordered execution identities differ from collection" in comparison["record_errors"][0]


def test_stock_observer_preserves_duplicate_collection_and_execution_occurrences(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_duplicate.py", "def test_duplicate():\n    print('duplicate capture')\n")
    descriptor = Project(
        "duplicate",
        "",
        "",
        ".",
        ("test_duplicate.py", "test_duplicate.py"),
        (),
        extra_args=("--keep-duplicates",),
    )
    run, raw = stock_outcome_run(
        descriptor,
        tmp_path,
        Path(sys.executable),
        tmp_path / "records",
        tmp_path / "pytest-temp",
        60,
    )
    assert run["exit_status"] == 0, run
    assert raw is not None
    assert raw["collected_nodeids"] == ["test_duplicate.py::test_duplicate"] * 2
    assert [case["nodeid"] for case in raw["cases"]] == raw["collected_nodeids"]
    assert [[report["phase"] for report in case["reports"]] for case in raw["cases"]] == [
        ["setup", "call", "teardown"],
        ["setup", "call", "teardown"],
    ]


def test_coverage_context_difference_retains_the_changed_path_and_values() -> None:
    stock = {"files": {"pkg.py": {"contexts": {"12": ["test_first|run"]}}}}
    native = {"files": {"pkg.py": {"contexts": {"12": ["test_second|run"]}}}}
    assert first_difference(stock, native) == {
        "path": ["files", "pkg.py", "contexts", "12"],
        "index": 0,
        "stock": "test_first|run",
        "native": "test_second|run",
    }


@pytest.mark.parametrize("outside_root", [False, True], ids=["nested-root", "outside-root"])
def test_stock_observer_accepts_nested_pytest_root_and_rejects_outside_checkout(
    tmp_path: Path, outside_root: bool
) -> None:
    checkout = tmp_path / "checkout"
    project = VfyProject(checkout)
    project.write("tests/pytest.ini", "[pytest]\n")
    project.write("tests/test_boundary.py", "def test_boundary():\n    assert True\n")
    pytest_root = tmp_path / "outside" if outside_root else checkout / "tests"
    pytest_root.mkdir(exist_ok=True)
    descriptor = Project(
        "boundary",
        "",
        "",
        ".",
        ("tests",),
        (),
        extra_args=("--rootdir", str(pytest_root)) if outside_root else (),
    )
    output = tmp_path / "records"
    run, raw = stock_outcome_run(
        descriptor, checkout, Path(sys.executable), output, tmp_path / "pytest-temp", 60
    )
    assert run["exit_status"] == 0, run
    recorded = json.loads((output / "execution.json").read_text(encoding="utf-8"))
    assert recorded["rootdir"] == str(pytest_root.resolve())
    if outside_root:
        assert raw is None
        assert run["record_errors"] == ["observer root directory is outside the pinned checkout"]
    else:
        assert raw is not None, run
        assert run["record_errors"] == []
        assert raw["collected"] == 1


def test_literal_warning_difference_survives_comparison_normalization(
    outcome_pairs: list[OutcomePair],
) -> None:
    pair = outcome_pairs[0]
    original = copy.deepcopy(pair.native_raw)
    comparison = pair.compare()
    assert comparison["equal"], comparison
    literal = comparison["literal_case_differences"]
    assert literal["native"]["warnings"][0]["message"].endswith("\n")
    assert not literal["stock"]["warnings"][0]["message"].endswith("\n")
    assert comparison["case_differences"] is None
    assert pair.native_raw == original


@pytest.mark.parametrize("workers", [1, 2])
def test_adoption_file_glob_preserves_explicit_test_selection(tmp_path: Path, workers: int) -> None:
    project = VfyProject(tmp_path)
    project.write("example.py", "answer = 42\n")
    project.write(
        "selected_test.py", "import example\ndef test_case():\n    assert example.answer == 42\n"
    )
    project.write("test_ignored.py", "def test_ignored():\n    assert False\n")
    descriptor = Project("fixture", "", "", ".", ("*_test.py",), ("--cov=example",))
    python = Path(sys.executable)
    basetemp = tmp_path / "pytest-temp"
    stock_run, stock_raw = stock_outcome_run(
        descriptor, tmp_path, python, tmp_path / "stock", basetemp, 60
    )
    native_run, native_raw = native_outcome_run(
        descriptor, tmp_path, python, tmp_path / "native", basetemp, 60, workers=workers
    )
    comparison = compare_outcomes(
        stock_run, native_run, stock_raw, native_raw, tmp_path, tmp_path, python, basetemp
    )
    assert comparison["equal"], comparison
    assert comparison["native_case_count"] == 1
    coverage_run, result = matrix.coverage_run(
        descriptor, tmp_path, python, tmp_path / "coverage", "native", 60, workers=workers
    )
    assert coverage_run["exit_status"] == 0, coverage_run
    assert coverage_run["record_errors"] == [], coverage_run
    coverage_command = json.loads((tmp_path / "coverage/command.json").read_text())["argv"]
    assert "--engine=pytest-native" in coverage_command
    coverage_record = json.loads((tmp_path / "coverage/native.json").read_text())
    assert coverage_record["engine"] == "pytest-runner-native-v1"
    assert coverage_run["junit_outcomes"] == [
        {"classname": "selected_test", "name": "test_case", "outcomes": []}
    ]
    assert result is not None
    assert result["report_oracle"]["equal"], result


@pytest.mark.parametrize("route", ["pytest", "native"])
def test_coverage_oracle_uses_last_explicit_config_from_route_root(
    tmp_path: Path, route: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "answer = 42\n")
    project.write("test_case.py", "import app\ndef test_answer():\n    assert app.answer == 42\n")
    project.write(".coveragerc", "[run]\nplugins = unavailable_upstream_plugin\n")
    project.write("config/empty.ini", "")
    descriptor = Project(
        "fixture",
        "",
        "",
        ".",
        ("test_case.py",),
        ("--cov=app", "--cov-config=ignored.ini"),
        extra_args=("--cov-config", "config/empty.ini"),
    )

    run, result = matrix.coverage_run(
        descriptor, tmp_path, Path(sys.executable), tmp_path / "coverage", route, 60
    )

    assert run["exit_status"] == 0, run
    assert run["record_errors"] == [], run
    assert result is not None
    assert result["report_oracle"]["equal"], result


@pytest.mark.parametrize("native_only_skip", [False, True], ids=["shared-skip", "native-only-skip"])
def test_coverage_gate_compares_real_coverage_outcomes(
    tmp_path: Path, native_only_skip: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("example.py", "answer = 42\n")
    skip_condition = "os.getenv('PYTEST_NATIVE_RESULT')" if native_only_skip else "True"
    project.write(
        "test_case.py",
        f"""import os
import pytest
import example

def test_initial():
    assert example.answer == 42

def test_repeat():
    if os.getenv('COVERAGE_FILE') and {skip_condition}:
        pytest.skip('coverage-conditioned skip')
    assert example.answer == 42
""",
    )
    descriptor = Project(
        "rich", "", matrix.RICH_REVISION, ".", ("test_case.py",), ("--cov=example",)
    )
    coverage: dict[str, Any] = {"junit_cases": {}, "junit_outcomes": {}}
    for route, engine, workers in (
        ("stock", "pytest", 1),
        ("native", "native", 1),
        ("native_workers2", "native", 2),
    ):
        run, result = matrix.coverage_run(
            descriptor,
            tmp_path,
            Path(sys.executable),
            tmp_path / route,
            engine,
            60,
            workers=workers,
        )
        assert run["exit_status"] == 0, run
        assert run["record_errors"] == [], run
        assert result is not None
        assert result["report_oracle"]["equal"], result
        coverage["junit_cases"][route] = run["junit_cases"]
        coverage["junit_outcomes"][route] = run["junit_outcomes"]
    errors = matrix.coverage_outcome_errors(coverage)
    if native_only_skip:
        assert errors == ["coverage native: JUnit outcomes differ from stock coverage execution"]
    else:
        assert errors == []


def test_stock_coverage_replaces_alternate_provider_and_preserves_project_plugins(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "answer = 42\n")
    project.write(
        "base_plugin.py",
        "def pytest_addoption(parser):\n    parser.addoption('--required-base')\n",
    )
    project.write(
        "stock_plugin.py",
        "def pytest_addoption(parser):\n    parser.addoption('--required-stock')\n",
    )
    project.write(
        "test_case.py",
        "import app\n"
        "def test_registered_options(pytestconfig):\n"
        "    assert pytestconfig.getoption('required_base') == 'present'\n"
        "    assert pytestconfig.getoption('required_stock') == 'present'\n"
        "    assert pytestconfig.getoption('cov_source') in ([], ['app'])\n"
        "    assert app.answer == 42\n",
    )
    descriptor = Project(
        "fixture",
        "",
        "",
        ".",
        ("test_case.py",),
        ("--cov=app",),
        plugins=("base_plugin",),
        stock_plugins=("pytest_cov.plugin", "stock_plugin"),
        extra_args=("--required-base=present",),
        stock_extra_args=("--required-stock=present",),
        outcome_args=("--no-cov",),
    )
    python = Path(sys.executable)
    outcome, raw = stock_outcome_run(
        descriptor, tmp_path, python, tmp_path / "outcomes", tmp_path / "pytest-temp", 60
    )
    assert outcome["exit_status"] == 0, outcome
    assert raw is not None
    assert not (tmp_path / ".coverage").exists()
    run, result = matrix.coverage_run(
        descriptor, tmp_path, python, tmp_path / "coverage", "pytest", 60
    )
    assert run["exit_status"] == 0, run
    assert run["record_errors"] == [], run
    assert run["junit_outcomes"] == matrix.junit_outcomes(tmp_path / "outcomes/junit.xml")
    assert result is not None
    assert result["report_oracle"]["equal"], result
    payload = json.loads((tmp_path / "coverage/coverage.json").read_text())
    assert payload["meta"]["version"].startswith("vfy ")


def test_worker_temporary_roots_preserve_captured_path_suffixes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name in ("first", "second"):
        project.write(
            f"test_{name}.py",
            f"def test_{name}(tmp_path):\n"
            "    print(f'root={tmp_path}/native-worker-17/literal.txt; semantic=42')\n",
        )
    descriptor = Project("fixture", "", "", ".", (".",), ())
    python = Path(sys.executable)
    basetemp = tmp_path / "pytest-temp"
    stock, stock_raw = stock_outcome_run(
        descriptor, tmp_path, python, tmp_path / "stock", basetemp, 60
    )
    native, native_raw = native_outcome_run(
        descriptor, tmp_path, python, tmp_path / "native", basetemp, 60, workers=2
    )
    original = copy.deepcopy((stock_raw, native_raw))

    comparison = compare_outcomes(
        stock, native, stock_raw, native_raw, tmp_path, tmp_path, python, basetemp
    )

    assert comparison["equal"], comparison
    assert comparison["native_case_count"] == 2
    assert (stock_raw, native_raw) == original
    assert native["pytest_worker_roots"] == [
        str(basetemp / "native-worker-0"),
        str(basetemp / "native-worker-1"),
    ]
    assert native_raw is not None
    changed = copy.deepcopy(native_raw)
    for case in changed["cases"]:
        for report in case["phases"]:
            report["stdout"] = report["stdout"].replace("semantic=42", "semantic=43")
    mismatch = compare_outcomes(
        stock, native, stock_raw, changed, tmp_path, tmp_path, python, basetemp
    )
    assert mismatch["captured_output_equal"] is False


def test_parameter_paths_compare_across_separate_route_checkouts(tmp_path: Path) -> None:
    roots = [tmp_path / "pytest-0", tmp_path / "native-1"]
    for root in roots:
        project = VfyProject(root)
        project.write(
            "app.py", "def value(number):\n    if number:\n        return 42\n    return 0\n"
        )
        project.write(
            "test_case.py",
            """from pathlib import Path
import pytest
from app import value as result

@pytest.mark.parametrize(
    "value", [42], ids=[(Path(__file__).parent / "input[bracket]*?.txt").as_uri()]
)
def test_case(value):
    assert result(value) == 42
""",
        )
    descriptor = Project("fixture", "", "", ".", ("test_case.py",), ("--cov=app", "--cov-branch"))
    python = Path(sys.executable)
    basetemp = tmp_path / "pytest-temp"
    stock, stock_raw = stock_outcome_run(
        descriptor, roots[0], python, roots[0] / "outcomes", basetemp, 60
    )
    native, native_raw = native_outcome_run(
        descriptor, roots[1], python, roots[1] / "outcomes", basetemp, 60
    )
    original = copy.deepcopy((stock_raw, native_raw))

    comparison = compare_outcomes(
        stock, native, stock_raw, native_raw, roots[0], roots[1], python, basetemp
    )

    assert comparison["equal"], comparison
    assert comparison["native_case_count"] == 1
    assert (stock_raw, native_raw) == original
    summaries = [
        matrix.route_summary(run, raw, root / "outcomes", root=root)
        for run, raw, root in zip((stock, native), (stock_raw, native_raw), roots, strict=True)
    ]
    expected = [
        {
            "classname": "test_case",
            "name": "test_case[file://<UPSTREAM>/input%5Bbracket%5D%2A%3F.txt]",
        }
    ]
    assert [summary["ordered_junit_cases"] for summary in summaries] == [expected, expected]
    coverage = []
    for root, route in zip(roots, ("pytest", "native"), strict=True):
        output = root / "coverage"
        run, result = matrix.coverage_run(descriptor, root, python, output, route, 60)
        assert run["exit_status"] == 0, run
        assert result is not None
        assert result["report_oracle"]["equal"], result
        digest = matrix.file_sha256(output / ".coverage")
        assert matrix.coverage_signature(output / ".coverage", root) == result["data"]
        assert matrix.file_sha256(output / ".coverage") == digest
        coverage.append(result)
    assert coverage[0]["data"] == coverage[1]["data"]

    changed_path = tmp_path / "changed.coverage"
    shutil.copyfile(roots[1] / "coverage/.coverage", changed_path)
    changed = CoverageData(basename=str(changed_path))
    try:
        changed.read()
        filename = next(iter(changed.measured_files()))
        arcs = changed.arcs(filename)
        assert arcs
        changed.set_context("different-semantic-context")
        changed.add_arcs({filename: arcs})
        changed.write()
    finally:
        changed.close(force=True)
    changed_signature = matrix.coverage_signature(changed_path, roots[1])
    assert matrix.aggregate_coverage_data(changed_signature) == coverage[1]["aggregate_data"]
    assert changed_signature != coverage[1]["data"]


@pytest.mark.parametrize("filename", ["selected_test.py", "selected*_test.py"])
def test_parameterized_node_selection_preserves_literal_suffixes_across_public_routes(
    tmp_path: Path,
    filename: str,
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value(number):\n    return number\n")
    project.write(
        "selected_test.py",
        """import pytest
from app import value

@pytest.mark.parametrize(
    "number", [1, 2, 3], ids=["square[bracket]", "star*question?/path", "unselected"]
)
def test_case(number):
    assert number <= 2, "unexpected unselected parameter"
    assert value(number) == number
""",
    )
    project.write(
        "test_ignored.py", "def test_ignored(): assert False, 'unexpected default discovery'\n"
    )
    suffixes = ("::test_case[square[bracket]]", "::test_case[star*question?/path]")
    expected = [f"selected_test.py{suffix}" for suffix in suffixes]
    descriptor = matrix.Project(
        "fixture",
        "",
        "",
        ".",
        tuple(f"{filename}{suffix}" for suffix in suffixes),
        ("--cov=app", "--cov-branch"),
    )
    python = Path(sys.executable)
    basetemp = tmp_path / "pytest-temp"
    stock, stock_raw = matrix.stock_outcome_run(
        descriptor,
        tmp_path,
        python,
        tmp_path / "stock-outcomes",
        basetemp,
        60,
    )
    assert stock["exit_status"] == 0, stock
    assert stock_raw is not None
    assert stock_raw["collected_nodeids"] == expected
    stock_junit = matrix.junit_outcomes(tmp_path / "stock-outcomes/junit.xml")
    for workers in (1, 2):
        run, raw = matrix.native_outcome_run(
            descriptor,
            tmp_path,
            python,
            tmp_path / f"native-{workers}-outcomes",
            basetemp,
            60,
            workers=workers,
        )
        comparison = matrix.compare_outcomes(
            stock,
            run,
            stock_raw,
            raw,
            tmp_path,
            tmp_path,
            python,
            basetemp,
        )
        assert comparison["equal"], comparison
    for role, workers in (("pytest", 1), ("native", 1), ("native", 2)):
        destination = tmp_path / f"{role}-{workers}-coverage"
        run, coverage = matrix.coverage_run(
            descriptor,
            tmp_path,
            python,
            destination,
            role,
            60,
            workers=workers,
            basetemp=basetemp,
        )
        assert run["exit_status"] == 0, run
        assert run["record_errors"] == [], run
        assert run["junit_outcomes"] == stock_junit
        assert coverage is not None
        assert coverage["report_oracle"]["equal"], coverage


@pytest.mark.parametrize("include_matching_selector", [False, True])
def test_unmatched_adoption_file_glob_does_not_execute_other_cases(
    tmp_path: Path, include_matching_selector: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_unselected.py",
        """from pathlib import Path

def test_unselected():
    Path("executed.txt").write_text("unexpected execution")
""",
    )
    selectors = ("missing*_test.py::test_case[param/with[*?]]",)
    if include_matching_selector:
        selectors = ("test_unselected.py", *selectors)
    descriptor = Project("fixture", "", "", ".", selectors, ())
    with pytest.raises(ValueError, match="test filename glob matched no paths"):
        stock_outcome_run(
            descriptor,
            tmp_path,
            Path(sys.executable),
            tmp_path / "stock",
            tmp_path / "pytest-temp",
            60,
        )
    assert not (tmp_path / "executed.txt").exists()


def test_colorama_manifest_selects_nested_suite_and_excludes_default_discovery(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    filenames = (
        "ansi_test.py",
        "ansitowin32_test.py",
        "initialise_test.py",
        "isatty_test.py",
        "winterm_test.py",
    )
    for filename in filenames:
        project.write(f"colorama/tests/{filename}", "def test_case():\n    assert True\n")
    project.write(
        "test_unselected.py",
        "def test_unselected():\n    raise AssertionError('manifest lost its explicit scope')\n",
    )
    descriptor = matrix.PROJECTS_BY_KEY["colorama"]
    python = Path(sys.executable)
    basetemp = tmp_path / "pytest-temp"
    stock, stock_raw = stock_outcome_run(
        descriptor, tmp_path, python, tmp_path / "stock", basetemp, 60
    )
    assert stock["exit_status"] == 0, stock
    assert stock_raw is not None
    assert stock_raw["collected_nodeids"] == [
        f"colorama/tests/{filename}::test_case" for filename in filenames
    ]
    for workers in (1, 2):
        native, native_raw = native_outcome_run(
            descriptor,
            tmp_path,
            python,
            tmp_path / f"native-{workers}",
            basetemp,
            60,
            workers=workers,
        )
        comparison = compare_outcomes(
            stock, native, stock_raw, native_raw, tmp_path, tmp_path, python, basetemp
        )
        assert comparison["equal"], comparison
        assert comparison["native_case_count"] == len(filenames)


def test_same_run_report_oracle_uses_default_morfs_after_configured_alias_preparation(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("src/sample.py", "if flag:\n    a = 1\nelse:\n    a = 2\nb = 3\n")
    project.write("src/untouched.py", "unused = 1\n")
    project.write("src/empty.py", "")
    project.write(
        ".coveragerc",
        "[run]\nrelative_files = true\nsource = src\n[paths]\nsource =\n    src\n    remote/src\n",
    )
    project.write(
        "report.py",
        """from pathlib import Path
import coverage
import vfy

output = Path('output')
output.mkdir()
data_file = str((output / '.coverage').resolve())
data = coverage.CoverageData(basename=data_file)
data.set_context('absolute')
data.add_lines({str(Path('src/sample.py').resolve()): [1, 2]})
data.set_context('relative')
data.add_lines({'src/sample.py': [4, 5]})
data.touch_files(['src/untouched.py', 'src/empty.py'])
data.write()
data.close(force=True)
cov = vfy.Coverage(data_file=data_file, config_file=True)
cov.load()
cov.json_report(outfile=str(output / 'coverage.json'))
cov.get_data().close(force=True)
""",
    )
    run = project.run_python("report.py")
    run.expect(0)
    output = tmp_path / "output"
    before = file_sha256(output / ".coverage")
    result = matrix.coverage_result(tmp_path, output)
    assert result is not None
    assert result["report_oracle"]["equal"], result["report_oracle"]
    assert len(result["data"]["files"]) == 4
    assert result["data"]["files"]["<UPSTREAM>/src/sample.py"]["contexts"]["1"] == ["absolute"]
    assert result["data"]["files"]["src/sample.py"]["contexts"]["4"] == ["relative"]
    assert set(result["normalized_json"]["files"]) == {
        "src/sample.py",
        "src/untouched.py",
        "src/empty.py",
    }
    assert result["normalized_json"]["files"]["src/untouched.py"]["missing_lines"] == [1]
    assert result["normalized_json"]["files"]["src/empty.py"]["summary"]["num_statements"] == 0
    assert file_sha256(output / ".coverage") == before
    report = json.loads((output / "coverage.json").read_text())
    report["totals"]["covered_lines"] += 1
    (output / "coverage.json").write_text(json.dumps(report))
    changed = matrix.coverage_result(tmp_path, output)
    assert changed is not None
    assert changed["report_oracle"]["equal"] is False
    assert file_sha256(output / ".coverage") == before
