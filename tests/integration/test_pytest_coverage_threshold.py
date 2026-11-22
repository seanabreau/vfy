"""Coverage thresholds preserve Coverage.py rounding and exact completion.

The exit status is compared because pytest-cov prints a different message when
rounding changes the outcome."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject


def _run(tmp_path: Path, route: str, functions: int, called: int, *arguments: str) -> CommandResult:
    project = VfyProject(tmp_path / route)
    project.write(
        "product.py",
        "".join(f"def f{index}():\n    return {index}\n" for index in range(functions)),
    )
    calls = "\n".join(f"    product.f{index}()" for index in range(called))
    project.write("test_product.py", f"import product\n\ndef test_calls():\n{calls}\n")
    options = ("--cov=product", "--cov-report=", "-q", *arguments)
    if route == "stock":
        return project.run_python("-m", "pytest", "-p", "pytest_cov", *options)
    return project.run_pytest(*options, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})


@pytest.mark.parametrize(
    ("functions", "called", "threshold", "precision"),
    [
        (4, 3, "87", 0),
        (4, 3, "88", 0),
        (4, 3, "88", 2),
        (4, 1, "62", 0),
        (4, 1, "63", 0),
        (4, 1, "62.5", 1),
        (4, 1, "62.51", 2),
        (3, 1, "66.67", 2),
        (3, 1, "66.67", 1),
        (3, 1, "66.68", 2),
        (3, 3, "100", 0),
        (3, 2, "100", 0),
        (3, 2, "0", 0),
    ],
)
def test_threshold_policy_matches_pytest_cov(
    tmp_path: Path, functions: int, called: int, threshold: str, precision: int
) -> None:
    arguments = (f"--cov-fail-under={threshold}", f"--cov-precision={precision}")
    stock = _run(tmp_path, "stock", functions, called, *arguments)
    candidate = _run(tmp_path, "vfy", functions, called, *arguments)
    assert candidate.returncode == stock.returncode, (stock.stdout, candidate.stdout)
    assert stock.returncode in {0, 1}, stock.stdout + stock.stderr
