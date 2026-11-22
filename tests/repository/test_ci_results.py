from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="The CI summary gate runs on Ubuntu with POSIX bash."
)

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_ci_results.sh"
COMMON = {"CHANGE_SCOPE", "REPOSITORY", "SUPPLY_CHAIN", "WORKFLOW_SECURITY"}
TIERS = {
    "documentation": {"DOCUMENTATION"},
    "tests": {"QUALITY", "FAST_BUILD", "FAST_WHEEL", "NATIVE_ADOPTION"},
    "platform": {"QUALITY", "RUST_MATRIX", "FULL_BUILD", "FULL_WHEEL", "NATIVE_ADOPTION"},
}
JOBS = COMMON | set.union(*TIERS.values())


def results(scope: str) -> dict[str, str]:
    required = COMMON | TIERS[scope]
    return {"SCOPE": scope, **{job: "success" if job in required else "skipped" for job in JOBS}}


def check(values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if key not in JOBS | {"SCOPE"}}
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={**environment, **values},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.mark.parametrize("scope", TIERS)
def test_successful_selected_tier_passes(scope: str) -> None:
    completed = check(results(scope))
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("scope", TIERS)
def test_every_required_job_must_succeed(scope: str) -> None:
    baseline = results(scope)
    for job in COMMON | TIERS[scope]:
        for status in ("failure", "cancelled", "skipped", None):
            values = (
                {**baseline, job: status}
                if status
                else {k: v for k, v in baseline.items() if k != job}
            )
            completed = check(values)
            assert completed.returncode != 0, (scope, job, status)
            assert f"{job}: expected success, got {status or 'missing'}" in completed.stderr


@pytest.mark.parametrize("scope", TIERS)
def test_unselected_tiers_cannot_substitute_for_the_selected_tier(scope: str) -> None:
    baseline = results(scope)
    for job in JOBS - COMMON - TIERS[scope]:
        completed = check({**baseline, job: "success"})
        assert completed.returncode != 0, (scope, job)
        assert f"{job}: expected skipped, got success" in completed.stderr


@pytest.mark.parametrize(("scope", "equivalent"), [("code", "tests"), ("unknown", "platform")])
def test_code_and_unknown_scopes_use_their_required_tiers(scope: str, equivalent: str) -> None:
    completed = check({**results(equivalent), "SCOPE": scope})
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("scope", ["", "new-unrecognized-tier"])
def test_missing_or_unrecognized_scope_cannot_report_success(scope: str) -> None:
    completed = check({**results("platform"), "SCOPE": scope})
    assert completed.returncode != 0
    assert "unexpected CI scope" in completed.stderr


@pytest.mark.parametrize(
    ("scope", "full_matrix", "scope_result", "full_build", "fast_build", "runs"),
    [
        ("code", "false", "success", "skipped", "success", True),
        ("tests", "false", "success", "skipped", "success", True),
        ("platform", "true", "success", "success", "skipped", True),
        ("unknown", "true", "success", "success", "skipped", True),
        ("documentation", "false", "success", "skipped", "skipped", False),
        ("code", "false", "success", "success", "failure", False),
        ("platform", "true", "success", "failure", "success", False),
        ("code", "false", "failure", "skipped", "success", False),
        ("platform", "true", "success", "cancelled", "skipped", False),
        ("code", "false", "success", "skipped", "skipped", False),
    ],
)
def test_adoption_runs_with_the_selected_build_despite_the_other_skipped_dependency(
    scope: str,
    full_matrix: str,
    scope_result: str,
    full_build: str,
    fast_build: str,
    runs: bool,
) -> None:
    workflow = (SCRIPT.parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  native-adoption:\n", 1)[1].split("\n  ci-summary:\n", 1)[0]
    match = re.search(r"    if: >-\n((?:      .+\n)+)", job)
    assert match is not None
    expression = " ".join(line.strip() for line in match[1].splitlines())
    # These predicates share Bash's string comparisons and Boolean operators.
    # always() is essential: GitHub otherwise skips a job with a skipped need.
    assert "always()" in expression
    expression = expression.replace("always()", "'always' == 'always'")
    values = {
        "needs.change-scope.result": scope_result,
        "needs.change-scope.outputs.scope": scope,
        "needs.change-scope.outputs.full_matrix": full_matrix,
        "needs.build-wheel.result": full_build,
        "needs.build-wheel-fast.result": fast_build,
    }
    for key, value in values.items():
        expression = expression.replace(key, f"'{value}'")
    assert "needs." not in expression
    completed = subprocess.run(
        ["bash", "-c", f"[[ {expression} ]]"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode in (0, 1), completed.stderr
    assert (completed.returncode == 0) is runs, expression
