"""Protect the adoption checker's exit status and retained failure reports."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from coverage import Coverage, CoverageData
from validation.pytest_runner import check_native_adoption_matrix as matrix
from validation.pytest_runner.check_native_project import load_json

from tests.repository.test_native_adoption_routes import OutcomePair, outcome_pairs as outcome_pairs


def test_bulk_observation_paths_preserve_symlink_aliases_duplicates_and_validation(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    alias = tmp_path / "checkout-alias"
    try:
        alias.symlink_to(checkout, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    python = tmp_path / "environment/bin/python"
    basetemp = tmp_path / "pytest-tmp"
    worker = basetemp / "native-worker-0"
    case = {
        "nodeid": f"{checkout}/test_case.py::test_value[{checkout}/parameter]",
        "reports": [
            {
                "phase": "call",
                "outcome": "passed",
                "exception": None,
                "diagnostic": None,
                "stdout": f"{alias}/a {checkout}/b {worker}/value",
                "stderr": "",
                "log": "",
            }
        ],
        "warnings": [
            {
                "message": str(checkout / "warning"),
                "filename": str(worker / "warning.py"),
                "lineno": 1,
            }
        ],
    }
    source = [deepcopy(case), deepcopy(case)]
    before = deepcopy(source)
    actual = matrix.normalize_cases_paths(source, alias, python, basetemp, worker_roots=(worker,))
    assert actual == [
        matrix.normalize_case_paths(row, alias, python, basetemp, worker_roots=(worker,))
        for row in source
    ]
    assert source == before
    assert actual[0] is not actual[1]
    assert actual[0]["reports"] is not actual[1]["reports"]
    assert actual[0]["nodeid"] == "test_case.py::test_value[<UPSTREAM>/parameter]"
    assert actual[0]["reports"][0]["stdout"] == "<UPSTREAM>/a <UPSTREAM>/b <PYTEST_TMP>/value"
    assert actual[0]["warnings"] == [
        {
            "message": "<UPSTREAM>/warning",
            "filename": "<PYTEST_TMP>/native-worker-0/warning.py",
            "lineno": 1,
        }
    ]
    invalid: dict[str, Any] = deepcopy(case)
    invalid["reports"][0]["stdout"] = 42
    with pytest.raises(ValueError, match="execution observation text must be a string"):
        matrix.normalize_cases_paths([invalid], alias, python, basetemp)


def invoke_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record: object,
    *,
    selection: tuple[str, ...] = ("--project", "iniconfig"),
    provenance: dict[str, object] | None = None,
) -> tuple[int, dict[str, Any]]:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate artifact")
    manifest = tmp_path / "build.json"
    manifest.write_text(
        json.dumps(
            {
                "wheel_sha256": matrix.file_sha256(wheel),
                "native_profile": "release",
                "source_revision": "a" * 40,
                **(provenance or {}),
            }
        )
    )
    output = tmp_path / "results"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_native_adoption_matrix.py",
            "--wheel",
            str(wheel),
            "--build-manifest",
            str(manifest),
            "--output",
            str(output),
            *selection,
        ],
    )

    def run(project: matrix.Project, *_args: object) -> object:
        if isinstance(record, Exception):
            raise record
        if isinstance(record, list):
            return next(row for row in record if row["project"] == project.key)
        return record

    monkeypatch.setattr(matrix, "run_project", run)
    status = matrix.main()
    return status, json.loads((output / "result.json").read_text())


def test_matrix_fails_missing_execution_and_keeps_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = {"project": "iniconfig", "status": "native-setup-failure"}

    status, result = invoke_matrix(tmp_path, monkeypatch, record)

    assert status == 1
    assert result["projects"] == [record]
    assert result["validation"]["passed"] is False
    assert result["validation"]["errors"] == [
        "iniconfig: expected parity, got 'native-setup-failure'"
    ]


def parity_record(key: str = "iniconfig") -> dict[str, Any]:
    junit = [{"classname": "tests.test_example", "name": "test_example"}]
    summary = {
        "exit_status": 0,
        "collected": 1,
        "cases": 1,
        "engine": "pytest-runner-native-v1",
        "ordered_junit_cases": junit,
        "ordered_junit_outcomes": [{**deepcopy(case), "outcomes": []} for case in junit],
    }
    comparison = {
        "equal": True,
        "status_equal": True,
        "engine_equal": True,
        "collected_equal": True,
        "collected_nodeids_equal": True,
        "ordered_nodeids_equal": True,
        "outcome_equal": True,
        "diagnostics_equal": True,
        "captured_output_equal": True,
        "case_warnings_equal": True,
        "session_warnings_equal": True,
        "record_errors": [],
        "stock_collected": 1,
        "native_collected": 1,
        "stock_case_count": 1,
        "native_case_count": 1,
        "stock_exit_status": 0,
        "native_exit_status": 0,
    }
    coverage = {
        "normalized_json": {
            "files": {"src/module.py": {"executed_lines": [1]}},
            "totals": {"covered_lines": 1},
        },
        "data": {
            "has_arcs": False,
            "files": {
                "src/module.py": {
                    "lines": [1],
                    "arcs": [],
                    "tracer": "",
                    "contexts": {"1": [""]},
                    "context_arcs": {},
                }
            },
            "measured_contexts": [""],
        },
    }
    coverage["aggregate_data"] = {
        "has_arcs": False,
        "files": {"src/module.py": {"lines": [1], "arcs": [], "tracer": ""}},
    }
    coverage["report_oracle"] = {
        "equal": True,
        "expected": deepcopy(coverage["normalized_json"]),
        "error": None,
    }
    expectation = matrix.PROJECT_EXPECTATIONS[key]
    assert isinstance(expectation, matrix.ExpectedParity)
    return {
        "project": key,
        "status": "parity",
        "stock": {**deepcopy(summary), "engine": None},
        "native": deepcopy(summary),
        "native_workers2": deepcopy(summary),
        "outcome_comparisons": {
            "native-default": deepcopy(comparison),
            "native-workers2": deepcopy(comparison),
        },
        "coverage": {
            "status": "validated",
            "contract": expectation.coverage,
            "reason": expectation.reason,
            "source_files_equal": True,
            "junit_cases": {
                "stock": deepcopy(junit),
                "native": deepcopy(junit),
                "native_workers2": deepcopy(junit),
            },
            "junit_outcomes": {
                route: [{**deepcopy(case), "outcomes": []} for case in junit]
                for route in ("stock", "native", "native_workers2")
            },
            "stock_exit_status": 0,
            "native_exit_status": 0,
            "native_workers2_exit_status": 0,
            "stock_record_errors": [],
            "native_record_errors": [],
            "native_workers2_record_errors": [],
            "normalized_json_equal": True,
            "schema7_data_equal": True,
            "aggregate_data_equal": True,
            "native_workers2_normalized_json_equal": True,
            "native_workers2_schema7_data_equal": True,
            "native_workers2_aggregate_data_equal": True,
            "stock": deepcopy(coverage),
            "native": deepcopy(coverage),
            "native_workers2": deepcopy(coverage),
        },
    }


def test_matrix_succeeds_only_after_complete_execution_and_coverage_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, result = invoke_matrix(tmp_path, monkeypatch, parity_record())

    assert (status, result["validation"]) == (
        0,
        {
            "passed": True,
            "errors": [],
            "projects": [
                {
                    "project": "iniconfig",
                    "expected": "execution-parity",
                    "coverage_contract": "exact",
                    "coverage_reason": None,
                    "passed": True,
                }
            ],
        },
    )


def test_matrix_rejects_a_passing_test_skipped_by_every_coverage_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = parity_record()
    for cases in record["coverage"]["junit_outcomes"].values():
        cases[0]["outcomes"] = [{"outcome": "skipped", "type": "pytest.skip"}]

    status, result = invoke_matrix(tmp_path, monkeypatch, record)

    assert status == 1
    assert any(
        "outcomes differ from stock outcome execution" in error
        for error in result["validation"]["errors"]
    )


@pytest.mark.parametrize("difference", ["context-arcs", "file-tracer"])
def test_coverage_signature_distinguishes_exact_schema7_attribution(
    tmp_path: Path, difference: str
) -> None:
    source = str(tmp_path / "source.py")
    first_arcs = [(-1, 1), (1, 2), (2, 3), (3, 1), (1, -1)]
    second_arcs = [(-1, 1), (1, 3), (3, 2), (2, 1), (1, -1)]
    signatures = []
    for index in range(2):
        path = tmp_path / f"coverage-{index}"
        data = CoverageData(basename=str(path))
        try:
            arcs = (first_arcs, second_arcs)
            if difference == "context-arcs" and index == 1:
                arcs = tuple(reversed(arcs))
            for context, values in zip(("test_a|run", "test_b|run"), arcs, strict=True):
                data.set_context(context)
                data.add_arcs({source: values})
            if difference == "file-tracer" and index == 1:
                data.add_file_tracers({source: "example.Tracer"})
            data.write()
        finally:
            data.close(force=True)
        signatures.append(matrix.coverage_signature(path, tmp_path))

    assert signatures[0] != signatures[1]


@pytest.mark.parametrize("serialized", [False, True], ids=["in-memory", "retained-json"])
def test_matrix_accepts_real_route_summaries_and_generated_branch_data(
    tmp_path: Path, outcome_pairs: list[OutcomePair], serialized: bool
) -> None:
    source = tmp_path / "example.py"
    source.write_text("def choose(flag):\n    if flag:\n        return 1\n    return 0\n")
    data_path = tmp_path / ".coverage"
    data = CoverageData(basename=str(data_path))
    data.add_arcs({str(source): [(-1, 2), (2, 3), (3, -1)]})
    data.write()
    data.close(force=True)
    coverage = Coverage(data_file=str(data_path), branch=True, config_file=False)
    coverage.load()
    report_path = tmp_path / "coverage.json"
    coverage.json_report(outfile=str(report_path))
    signature = matrix.coverage_signature(data_path, tmp_path)
    report = matrix.normalized_coverage_json(report_path, tmp_path)
    result = {
        "data": signature,
        "aggregate_data": matrix.aggregate_coverage_data(signature),
        "normalized_json": report,
        "report_oracle": {"equal": True, "expected": deepcopy(report), "error": None},
    }
    record = parity_record()
    first = outcome_pairs[0]
    record["stock"] = matrix.route_summary(first.stock_run, first.stock_raw, first.root / "stock")
    for pair, route, comparison_key in zip(
        outcome_pairs,
        ("native", "native_workers2"),
        ("native-default", "native-workers2"),
        strict=True,
    ):
        workers = 1 if route == "native" else 2
        record[route] = matrix.route_summary(
            pair.native_run, pair.native_raw, pair.root / f"native-{workers}"
        )
        record["outcome_comparisons"][comparison_key] = pair.compare()
    for route in ("stock", "native", "native_workers2"):
        record["coverage"][route] = deepcopy(result)
        record["coverage"]["junit_cases"][route] = deepcopy(record["stock"]["ordered_junit_cases"])
        record["coverage"]["junit_outcomes"][route] = deepcopy(
            record["stock"]["ordered_junit_outcomes"]
        )
    if serialized:
        record = json.loads(json.dumps(record))

    verdict = matrix.validate_matrix([record], ["iniconfig"])

    assert verdict["passed"] is True, verdict["errors"]


def test_coverage_signature_accepts_empty_module_line_zero_alongside_executed_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "example.py").write_text("answer = 1\n")
    data_path = tmp_path / ".coverage"
    data = CoverageData(basename=str(data_path))
    data.add_lines({str(tmp_path / "__init__.py"): [0], str(tmp_path / "example.py"): [1]})
    data.write()
    data.close(force=True)

    signature = matrix.coverage_signature(data_path, tmp_path)

    assert matrix.coverage_data_errors(signature, "stock") == []
    signature["files"].pop("<UPSTREAM>/example.py")
    assert matrix.coverage_data_errors(signature, "stock") == [
        "coverage stock: no measured source lines"
    ]


@pytest.mark.parametrize(
    ("path", "value", "diagnostic"),
    [
        (("status",), "unsupported-native", "expected parity"),
        (("status",), "matching-test-failure", "expected parity"),
        (("stock", "exit_status"), 2, "stock: missing successful execution"),
        (("native_workers2", "exit_status"), 1, "native_workers2: missing successful execution"),
        (("native", "cases"), 0, "missing nonempty execution inventory"),
        (("native", "engine"), "pytest", "execution did not use the native engine"),
        (("stock", "exit_status"), False, "missing successful execution"),
        (("outcome_comparisons", "native-workers2"), None, "missing outcome comparison"),
        (
            ("outcome_comparisons", "native-default", "record_errors"),
            ["native execution record has an invalid call report"],
            "execution records are missing or malformed",
        ),
        (
            ("outcome_comparisons", "native-default", "engine_equal"),
            False,
            "engine_equal is not true",
        ),
        (
            ("outcome_comparisons", "native-default", "native_case_count"),
            0,
            "execution inventory is incomplete",
        ),
        (
            ("outcome_comparisons", "native-default", "captured_output_equal"),
            False,
            "captured_output_equal is not true",
        ),
        (
            ("outcome_comparisons", "native-workers2", "session_warnings_equal"),
            None,
            "session_warnings_equal is not true",
        ),
        (("coverage",), None, "missing coverage comparison"),
        (("coverage", "status"), "not-run-until-outcome-parity", "expected validated coverage"),
        (("coverage", "native_exit_status"), 1, "coverage native: exit status is not zero"),
        (
            ("coverage", "native_workers2_exit_status"),
            1,
            "coverage native_workers2: exit status is not zero",
        ),
        (("coverage", "schema7_data_equal"), False, "schema7_data_equal is not true"),
        (("coverage", "contract"), "same-run-report", "does not match the reviewed"),
        (("coverage", "stock_record_errors"), ["missing JUnit record"], "missing or malformed"),
        (("coverage", "junit_cases", "native"), [], "JUnit inventory differs"),
        (("coverage", "junit_outcomes"), None, "missing or malformed JUnit outcomes"),
        (("coverage", "junit_outcomes", "native"), [], "missing or malformed JUnit outcomes"),
        (
            ("coverage", "junit_outcomes", "native"),
            [
                {
                    "classname": "tests.test_example",
                    "name": "test_example",
                    "outcomes": [{"outcome": "skipped", "type": "pytest.skip"}],
                }
            ],
            "JUnit outcomes differ from stock coverage execution",
        ),
        (
            ("coverage", "junit_outcomes", "stock"),
            [{"classname": "tests.test_example", "name": "test_example", "outcomes": None}],
            "missing or malformed JUnit outcomes",
        ),
        (
            ("coverage", "native", "report_oracle", "equal"),
            False,
            "same-run Coverage.py report comparison is incomplete or differs",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "arcs"),
            None,
            "malformed schema-7 arcs",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "tracer"),
            None,
            "malformed schema-7 file tracer",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "context_arcs"),
            None,
            "malformed schema-7 context arcs",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "contexts"),
            {"1": None},
            "malformed schema-7 context attribution",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "contexts"),
            {},
            "context attribution does not cover measured lines",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "contexts"),
            {"1": ["missing-context"]},
            "attribution names an unmeasured context",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "lines"),
            [],
            "no measured source lines",
        ),
        (
            ("coverage", "native", "data", "files"),
            {},
            "data has missing or malformed files",
        ),
        (
            ("coverage", "native_workers2", "normalized_json", "totals"),
            None,
            "missing report totals",
        ),
        (
            ("coverage", "native", "data", "files", "src/module.py", "lines"),
            [1, 2],
            "schema-7 data differs from stock",
        ),
    ],
)
def test_matrix_rejects_partial_runs_and_differences_even_when_row_claims_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
    value: object,
    diagnostic: str,
) -> None:
    record = parity_record()
    target = record
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    status, result = invoke_matrix(tmp_path, monkeypatch, record)

    assert status == 1
    assert any(diagnostic in error for error in result["validation"]["errors"])
    assert result["projects"] == [record]


def test_setup_exception_is_retained_and_fails_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, result = invoke_matrix(tmp_path, monkeypatch, RuntimeError("wheel import failed"))

    assert status == 1
    assert result["projects"][0]["error"] == "RuntimeError: wheel import failed"
    assert result["validation"]["errors"] == [
        "iniconfig: expected parity, got 'setup-failure'",
        "iniconfig: RuntimeError: wheel import failed",
    ]


@pytest.mark.parametrize("record", [None, [], "broken", {"status": "parity"}])
def test_matrix_rejects_malformed_project_rows_without_losing_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: object
) -> None:
    # A list is the fake runner's per-project lookup; use the validator directly
    # for an absent row and the main boundary for malformed individual rows.
    if record == []:
        assert matrix.validate_matrix([], ["iniconfig"])["passed"] is False
        return
    status, result = invoke_matrix(tmp_path, monkeypatch, record)

    assert status == 1
    assert result["projects"] == [record]


def test_matrix_rejects_missing_duplicate_and_unconfigured_projects() -> None:
    record = parity_record()

    assert matrix.validate_matrix([record], ["iniconfig", "rich"])["passed"] is False
    assert matrix.validate_matrix([record, record], ["iniconfig", "iniconfig"])["passed"] is False
    unreviewed = {**record, "project": "unreviewed"}
    assert matrix.validate_matrix([unreviewed], ["unreviewed"])["passed"] is False


def test_pluggy_requires_exact_aggregate_data_while_retaining_context_differences() -> None:
    record = parity_record("pluggy")
    coverage = record["coverage"]
    native = coverage["native"]
    native["data"]["files"]["src/module.py"]["contexts"] = {"1": ["test_example|run"]}
    native["data"]["measured_contexts"] = ["test_example|run"]
    coverage["schema7_data_equal"] = False

    verdict = matrix.validate_matrix([record], ["pluggy"])

    assert verdict["passed"] is True
    assert verdict["projects"][0]["coverage_contract"] == "aggregate-only"
    assert "not compared" in verdict["projects"][0]["coverage_reason"]
    assert coverage["schema7_data_equal"] is False
    native["data"]["files"]["src/module.py"]["lines"] = [1, 2]
    native["data"]["files"]["src/module.py"]["contexts"]["2"] = ["test_example|run"]
    native["aggregate_data"]["files"]["src/module.py"]["lines"] = [1, 2]
    assert matrix.validate_matrix([record], ["pluggy"])["passed"] is False


@pytest.mark.parametrize("difference", ["line", "arc", "tracer", "report"])
def test_poetry_context_contract_rejects_semantic_coverage_changes(difference: str) -> None:
    record = parity_record("poetry-core")
    coverage = record["coverage"]
    for route in ("stock", "native", "native_workers2"):
        result = coverage[route]
        result["data"]["has_arcs"] = True
        source = result["data"]["files"]["src/module.py"]
        source["arcs"] = [[-1, 1], [1, -1]]
        source["context_arcs"] = {"": deepcopy(source["arcs"])}
        result["aggregate_data"] = deepcopy(matrix.aggregate_coverage_data(result["data"]))
    parallel = coverage["native_workers2"]
    source = parallel["data"]["files"]["src/module.py"]
    source["contexts"] = {"1": ["test_first_use|run"]}
    source["context_arcs"] = {"test_first_use|run": deepcopy(source["arcs"])}
    parallel["data"]["measured_contexts"] = ["test_first_use|run"]
    coverage["native_workers2_schema7_data_equal"] = False

    verdict = matrix.validate_matrix([record], ["poetry-core"])

    assert verdict["passed"] is True
    assert verdict["projects"][0]["coverage_contract"] == "aggregate-only"
    assert "worker-local lazy grammar" in verdict["projects"][0]["coverage_reason"]
    assert coverage["schema7_data_equal"] is True
    assert coverage["native_workers2_schema7_data_equal"] is False
    assert parallel["data"]["measured_contexts"] == ["test_first_use|run"]

    if difference == "line":
        source["lines"].append(2)
        source["contexts"]["2"] = ["test_first_use|run"]
    elif difference == "arc":
        source["arcs"].append([1, 1])
        source["context_arcs"]["test_first_use|run"].append([1, 1])
    elif difference == "tracer":
        source["tracer"] = "example.CustomTracer"
    else:
        parallel["normalized_json"]["totals"]["covered_lines"] = 0
        parallel["report_oracle"]["expected"] = deepcopy(parallel["normalized_json"])
    parallel["aggregate_data"] = deepcopy(matrix.aggregate_coverage_data(parallel["data"]))
    # Keep claimed equality flags true: the validator must compare the actual data.
    assert coverage["native_workers2_aggregate_data_equal"] is True
    assert coverage["native_workers2_normalized_json_equal"] is True
    verdict = matrix.validate_matrix([record], ["poetry-core"])
    assert verdict["passed"] is False
    assert verdict["errors"] == [
        "poetry-core: coverage native_workers2: report or schema-7 data differs from stock"
    ]


def test_rich_requires_each_runs_oracle_and_identical_source_inventory() -> None:
    record = parity_record("rich")
    coverage = record["coverage"]
    native = coverage["native_workers2"]
    native["data"]["files"]["src/module.py"]["lines"] = [1, 2]
    native["data"]["files"]["src/module.py"]["contexts"] = {"1": [""], "2": [""]}
    native["aggregate_data"]["files"]["src/module.py"]["lines"] = [1, 2]
    native["normalized_json"]["files"]["src/module.py"]["executed_lines"] = [1, 2]
    native["normalized_json"]["totals"]["covered_lines"] = 2
    native["report_oracle"]["expected"] = deepcopy(native["normalized_json"])
    coverage["native_workers2_schema7_data_equal"] = False
    coverage["native_workers2_normalized_json_equal"] = False
    coverage["native_workers2_aggregate_data_equal"] = False

    verdict = matrix.validate_matrix([record], ["rich"])

    assert verdict["passed"] is True
    assert verdict["projects"][0]["coverage_contract"] == "same-run-report"
    native["report_oracle"]["expected"]["totals"]["covered_lines"] = 1
    assert matrix.validate_matrix([record], ["rich"])["passed"] is False
    native["report_oracle"]["expected"] = deepcopy(native["normalized_json"])
    native["data"]["files"]["src/extra.py"] = deepcopy(native["data"]["files"]["src/module.py"])
    native["aggregate_data"]["files"]["src/extra.py"] = deepcopy(
        native["aggregate_data"]["files"]["src/module.py"]
    )
    verdict = matrix.validate_matrix([record], ["rich"])
    assert verdict["passed"] is False
    assert any("source inventory differs" in error for error in verdict["errors"])


def test_required_subset_selects_exactly_five_reviewed_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [parity_record(key) for key in matrix.REQUIRED_PROJECTS]
    monkeypatch.setattr(matrix, "git", lambda _root, *args: "" if args[0] == "status" else "a" * 40)

    status, result = invoke_matrix(
        tmp_path,
        monkeypatch,
        records,
        selection=("--required-subset",),
        provenance={"source_tree": "a" * 40},
    )

    assert status == 0
    assert [row["project"] for row in result["projects"]] == [
        "iniconfig",
        "sortedcontainers",
        "pluggy",
        "colorama",
        "rich",
    ]


@pytest.mark.parametrize(
    "selection",
    [
        ("--required-subset", "--project", "iniconfig"),
        ("--project", "iniconfig", "--project", "iniconfig"),
    ],
)
def test_matrix_rejects_conflicting_or_duplicate_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as failure:
        invoke_matrix(tmp_path, monkeypatch, parity_record(), selection=selection)

    assert failure.value.code == 2


@pytest.mark.parametrize("field", ["source_revision", "source_tree"])
def test_required_subset_rejects_candidate_from_a_different_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setattr(matrix, "git", lambda _root, *_args: "a" * 40)
    provenance: dict[str, object] = {"source_tree": "a" * 40, field: "b" * 40}

    with pytest.raises(ValueError, match="revision and tree must match"):
        invoke_matrix(
            tmp_path,
            monkeypatch,
            parity_record(),
            selection=("--required-subset",),
            provenance=provenance,
        )


@pytest.mark.parametrize("provenance", [{"native_profile": "debug"}, {"source_revision": None}])
def test_matrix_rejects_unverified_build_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provenance: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="build manifest must record"):
        invoke_matrix(tmp_path, monkeypatch, parity_record(), provenance=provenance)


def test_required_subset_rejects_uncommitted_source_before_running_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        matrix, "git", lambda _root, *args: " M checker.py" if args[0] == "status" else "a" * 40
    )

    with pytest.raises(ValueError, match="no uncommitted files"):
        invoke_matrix(
            tmp_path,
            monkeypatch,
            parity_record(),
            selection=("--required-subset",),
            provenance={"source_tree": "a" * 40},
        )


def test_coverage_signature_retains_absolute_and_relative_alias_evidence(tmp_path: Path) -> None:
    source = str(tmp_path / "sample.py")
    data_path = tmp_path / ".coverage"
    data = CoverageData(basename=str(data_path))
    try:
        data.set_context("absolute-test|run")
        data.add_arcs({source: [(-1, 1), (1, -1)]})
        data.set_context("relative-test|run")
        data.add_arcs({"sample.py": [(-1, 2), (2, -1)]})
        data.add_file_tracers({source: "absolute.Tracer", "sample.py": "relative.Tracer"})
        data.write()
    finally:
        data.close(force=True)

    signature = matrix.coverage_signature(data_path, tmp_path)

    assert set(signature["files"]) == {"<UPSTREAM>/sample.py", "sample.py"}
    absolute = signature["files"]["<UPSTREAM>/sample.py"]
    relative = signature["files"]["sample.py"]
    assert absolute["lines"] == [1]
    assert relative["lines"] == [2]
    assert absolute["tracer"] == "absolute.Tracer"
    assert relative["tracer"] == "relative.Tracer"
    assert absolute["context_arcs"] == {"absolute-test|run": [(-1, 1), (1, -1)]}
    assert relative["context_arcs"] == {"relative-test|run": [(-1, 2), (2, -1)]}
    changed = deepcopy(signature)
    changed["files"]["<UPSTREAM>/sample.py"]["context_arcs"] = {}
    assert changed != signature
    assert matrix.coverage_data_errors(changed, "native")


def test_coverage_report_normalization_rejects_alias_collisions(tmp_path: Path) -> None:
    report_path = tmp_path / "coverage.json"
    payload = {
        "files": {
            str(tmp_path / "sample.py"): {"executed_lines": [1]},
            "sample.py": {"executed_lines": [2]},
        },
        "totals": {"covered_lines": 3},
    }
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    before = report_path.read_bytes()

    with pytest.raises(ValueError, match="filename collision"):
        matrix.normalized_coverage_json(report_path, tmp_path)
    with pytest.raises(RuntimeError, match="filename collision"):
        load_json(report_path, tmp_path)

    assert report_path.read_bytes() == before


def test_sortedcontainers_recipe_retains_upstream_files_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = matrix.PROJECTS_BY_KEY["sortedcontainers"]
    monkeypatch.setattr(matrix, "git", lambda *_args: project.revision)
    upstream = tmp_path / "tests" / "test_stress_sortedlist.py"
    upstream.parent.mkdir()
    upstream.write_text("upstream source\n")
    matrix.prepare_project(project, tmp_path, tmp_path / "python", tmp_path / "result", 10)
    fixture = tmp_path / "conftest.py"
    recipe = json.loads((tmp_path / "result" / "workload-recipe.json").read_text())
    assert upstream.read_text() == "upstream source\n"
    assert recipe["fixture_sha256"] == matrix.file_sha256(fixture)
    with pytest.raises(RuntimeError, match="cannot replace an upstream conftest"):
        matrix.prepare_project(project, tmp_path, tmp_path / "python", tmp_path / "other", 10)
