"""``--cov-fail-under`` boundary totals report and exit like pytest-cov."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject

_ROUTES = ("oracle", "native", "independent")


def _run(project: VfyProject, route: str, *arguments: str) -> CommandResult:
    if route == "oracle":
        return project.run_python("-m", "pytest", "-p", "pytest_cov.plugin", *arguments)
    if route == "native":
        return project.run_pytest(*arguments, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    return project.run_vfy("test", "--engine=native", "--no-replay", *arguments)


def _outcome(result: CommandResult) -> tuple[int, list[str]]:
    lines = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "Required test coverage" in line or "Coverage failure" in line or "ERROR:" in line
    ]
    return result.returncode, lines


# (executed assignments, unexecuted statements, threshold, precision)
_BOUNDARIES = [
    pytest.param(6, 1, "88", 0, id="87.5-against-88-rounds-to-88"),
    pytest.param(6, 1, "87.5", 0, id="87.5-meets-87.5"),
    pytest.param(6, 1, "87.6", 1, id="87.5-below-87.6-at-one-digit"),
    pytest.param(6, 1, "88", 2, id="87.5-below-88-at-two-digits"),
    pytest.param(6, 1, "87", 0, id="87.5-above-87"),
    pytest.param(1, 1, "67", 0, id="66.67-against-67-rounds-to-67"),
    pytest.param(198, 1, "100", 0, id="99.5-against-100-is-special"),
    pytest.param(6, 1, "0", 0, id="zero-threshold-prints-no-line"),
]


@pytest.mark.parametrize(("executed", "missed", "threshold", "precision"), _BOUNDARIES)
def test_fail_under_boundaries_match_pytest_cov(
    tmp_path: Path, executed: int, missed: int, threshold: str, precision: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "product.py",
        "".join(f"value_{index} = {index}\n" for index in range(executed))
        + "def missed():\n"
        + "".join(f"    value_{index} = {index}\n" for index in range(missed)),
    )
    project.write("test_product.py", "import product\n\ndef test_product():\n    pass\n")
    arguments = (
        "--cov=product",
        "--cov-report=",
        f"--cov-fail-under={threshold}",
        f"--cov-precision={precision}",
        "-q",
    )

    observed = {route: _outcome(_run(project, route, *arguments)) for route in _ROUTES}

    assert observed["native"] == observed["oracle"]
    assert observed["independent"] == observed["oracle"]


@pytest.mark.parametrize("threshold", [None, "90"])
def test_nothing_measured_reports_and_exits_like_pytest_cov(
    tmp_path: Path, threshold: str | None
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_product.py", "def test_product():\n    pass\n")
    arguments = ["--cov=missing", "-q"]
    if threshold is not None:
        arguments.append(f"--cov-fail-under={threshold}")

    observed = {}
    for route in _ROUTES:
        result = _run(project, route, *arguments)
        lines = [
            line
            for line in (result.stdout + result.stderr).splitlines()
            if "WARNING:" in line or "Required test coverage" in line or "Coverage failure" in line
        ]
        # The stock plug-in writes the report warning inside its coverage section.
        observed[route] = (result.returncode, sorted(lines))

    assert observed["oracle"] == (
        0 if threshold is None else 1,
        sorted(
            [
                "WARNING: Failed to generate report: No data to report.",
                *(
                    []
                    if threshold is None
                    else [
                        "ERROR: Coverage failure: total of 0 is less than fail-under=90",
                        "FAIL Required test coverage of 90% not reached. Total coverage: 0.00%",
                    ]
                ),
            ]
        ),
    )
    assert observed["native"] == observed["oracle"]
    assert observed["independent"] == observed["oracle"]
