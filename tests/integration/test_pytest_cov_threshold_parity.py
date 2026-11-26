"""``--cov-fail-under`` boundary totals report and exit like pytest-cov."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

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


@pytest.mark.parametrize(
    ("options", "warns", "fails"),
    [
        ((), True, False),
        (("--cov-fail-under=90",), True, True),
        (("--cov-report=",), False, False),
        (("--cov-report=", "--cov-fail-under=0"), False, False),
        (("--cov-report=", "--cov-fail-under=90"), True, True),
    ],
    ids=["default-report", "report-and-gate", "no-report", "zero-gate", "gate-only"],
)
@pytest.mark.parametrize(
    "route", ["native", pytest.param("independent", marks=requires_native_pytest)]
)
def test_nothing_measured_reports_and_exits_like_pytest_cov(
    tmp_path: Path, options: tuple[str, ...], warns: bool, fails: bool, route: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_product.py", "def test_product():\n    pass\n")
    arguments = ("--cov=missing", "-q", *options)
    reference = _run(project, "oracle", *arguments)
    candidate = _run(project, route, *arguments)
    expected = []
    if fails:
        expected.extend(
            [
                "ERROR: Coverage failure: total of 0 is less than fail-under=90",
                "FAIL Required test coverage of 90% not reached. Total coverage: 0.00%",
            ]
        )
    if warns:
        expected.append("WARNING: Failed to generate report: No data to report.")
    for result in (reference, candidate):
        result.expect(int(fails))
        lines = (
            line
            for line in result.stdout.splitlines()
            if "WARNING:" in line or "Required test coverage" in line or "Coverage failure" in line
        )
        # The stock plug-in writes the report warning inside its coverage section.
        assert sorted(lines) == sorted(expected), result.stdout + result.stderr


@requires_native_pytest
@pytest.mark.parametrize(("options", "status"), [((), 0), (("--cov-fail-under=90",), 1)])
def test_empty_coverage_preserves_status_without_terminal_reporter(
    tmp_path: Path, options: tuple[str, ...], status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_product.py",
        "from pathlib import Path\n\ndef test_product():\n    Path('ran').touch()\n",
    )
    result = _run(project, "independent", "--cov=missing", "-p", "no:terminal", *options)
    result.expect(status)
    assert (project.root / "ran").is_file()
