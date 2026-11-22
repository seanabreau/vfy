"""The HTML benchmark must reject incomplete or incorrect reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.benchmark_pytest_html import (
    WORKLOADS,
    command,
    create_fixture,
    run,
    validate_report,
)


@pytest.mark.parametrize("workload", WORKLOADS)
def test_cli_html_workloads_use_native_execution(tmp_path: Path, workload: str) -> None:
    root = tmp_path / "project"
    create_fixture(root, "vfy-cli", 1)
    conftest = root / "conftest.py"
    conftest.write_text(
        conftest.read_text(encoding="utf-8")
        + "\nimport _pytest.runner\n"
        + "def forbidden(*args, **kwargs):\n"
        + "    raise AssertionError('stock execution must not run the CLI benchmark')\n"
        + "_pytest.runner.runtestprotocol = forbidden\n",
        encoding="utf-8",
    )
    arguments = command("vfy-cli", workload)
    run(arguments, root, 1)
    validate_report(root, 1)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (">Passed<", ">Failed<"),
        ("evidence-0", "missing evidence"),
        ("public-region", "secret-value"),
    ],
)
def test_report_parity_rejects_corrupted_results(
    tmp_path: Path, original: str, replacement: str
) -> None:
    root = tmp_path / "project"
    create_fixture(root, "vfy-plugin", 1)
    run(command("vfy-plugin", "self-contained"), root, 1)
    validate_report(root, 1)
    report = root / "reports/results.html"
    report.write_text(
        report.read_text(encoding="utf-8").replace(original, replacement), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"parity failed|lost configured metadata"):
        validate_report(root, 1)


@pytest.mark.parametrize("tool", ["pytest-html", "vfy-plugin", "vfy-cli"])
@pytest.mark.parametrize("workload", ["self-contained", "assets"])
def test_report_parity_rejects_attachments_assigned_to_the_wrong_test(
    tmp_path: Path, tool: str, workload: str
) -> None:
    root = tmp_path / "project"
    create_fixture(root, tool, 2)
    run(command(tool, workload), root, 2)
    validate_report(root, 2)
    report = root / "reports/results.html"
    report.write_text(
        report.read_text(encoding="utf-8")
        .replace("test_value[0]", "test_value[swapped]")
        .replace("test_value[1]", "test_value[0]")
        .replace("test_value[swapped]", "test_value[1]"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="attachment parity failed"):
        validate_report(root, 2)
