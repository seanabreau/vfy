from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="The CI summary gate runs on Ubuntu with POSIX bash."
)

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_ci_results.sh"
COMMON = {"CHANGE_SCOPE", "REPOSITORY", "SUPPLY_CHAIN", "WORKFLOW_SECURITY"}
JOBS = COMMON | {"DOCUMENTATION", "LINUX_WHEEL", "RUST"}
PLANS = [
    ("documentation", "true", {"DOCUMENTATION"}),
    ("code", "false", {"LINUX_WHEEL"}),
    ("code", "true", {"LINUX_WHEEL", "DOCUMENTATION"}),
    ("rust", "false", {"LINUX_WHEEL", "RUST"}),
    ("rust", "true", {"LINUX_WHEEL", "RUST", "DOCUMENTATION"}),
    ("unknown", "true", {"LINUX_WHEEL", "RUST", "DOCUMENTATION"}),
]


def results(scope: str, check_docs: str, selected: set[str]) -> dict[str, str]:
    return {
        "SCOPE": scope,
        "CHECK_DOCS": check_docs,
        **{job: "success" if job in COMMON | selected else "skipped" for job in JOBS},
    }


def check(values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value for key, value in os.environ.items() if key not in JOBS | {"SCOPE", "CHECK_DOCS"}
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={**environment, **values},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize(("scope", "check_docs", "selected"), PLANS)
def test_successful_selected_jobs_pass(scope: str, check_docs: str, selected: set[str]) -> None:
    completed = check(results(scope, check_docs, selected))
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(("scope", "check_docs", "selected"), PLANS)
def test_every_required_job_must_succeed(scope: str, check_docs: str, selected: set[str]) -> None:
    baseline = results(scope, check_docs, selected)
    for job in COMMON | selected:
        for status in ("failure", "cancelled", "skipped", None):
            values = dict(baseline)
            if status is None:
                del values[job]
            else:
                values[job] = status
            completed = check(values)
            assert completed.returncode != 0, (scope, job, status)
            assert f"{job}: expected success, got {status or 'missing'}" in completed.stderr


@pytest.mark.parametrize(("scope", "check_docs", "selected"), PLANS)
def test_unselected_jobs_must_be_skipped(scope: str, check_docs: str, selected: set[str]) -> None:
    baseline = results(scope, check_docs, selected)
    for job in JOBS - COMMON - selected:
        completed = check({**baseline, job: "success"})
        assert completed.returncode != 0, (scope, job)
        assert f"{job}: expected skipped, got success" in completed.stderr


@pytest.mark.parametrize("scope", ["", "new-unrecognized-tier"])
def test_missing_or_unrecognized_scope_cannot_report_success(scope: str) -> None:
    completed = check(results(scope, "false", {"LINUX_WHEEL"}))
    assert completed.returncode != 0
    assert "unexpected CI scope" in completed.stderr


@pytest.mark.parametrize("check_docs", ["", "invalid"])
def test_invalid_documentation_selection_cannot_report_success(check_docs: str) -> None:
    completed = check(results("code", check_docs, {"LINUX_WHEEL"}))
    assert completed.returncode != 0
    assert "unexpected documentation check selection" in completed.stderr


@pytest.mark.parametrize(
    ("scope", "selected"),
    [("documentation", set()), ("unknown", {"LINUX_WHEEL", "RUST"})],
)
def test_documentation_is_required_for_documentation_and_unknown_paths(
    scope: str, selected: set[str]
) -> None:
    completed = check(results(scope, "false", selected))
    assert completed.returncode != 0
    assert "CHECK_DOCS: expected true, got false" in completed.stderr
