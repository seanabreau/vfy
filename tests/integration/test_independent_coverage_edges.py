"""Coverage report suppression and threshold edge cases match pytest-cov."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def _run(project: VfyProject, route: str, *arguments: str) -> CommandResult:
    if route == "oracle":
        return project.run_python(
            "-m",
            "pytest",
            "-p",
            "pytest_cov.plugin",
            *arguments,
            env={"COVERAGE_FILE": str(project.root / ".coverage.oracle")},
        )
    return project.run_python(
        "-m",
        "vfy.independent.entry",
        "-p",
        "vfy.independent.coverage_support",
        "--native-result-json=candidate-result.json",
        *arguments,
        env={
            "COVERAGE_FILE": str(project.root / ".coverage.candidate"),
            "PYTHONPATH": _SOURCE,
        },
    )


@pytest.mark.parametrize("mode", ["silent", "strict100", "rounded67"])
def test_empty_reports_and_rounded_thresholds_match_stock(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    statements = 1 if mode == "rounded67" else 200
    project.write(
        "product.py",
        "".join(f"value_{index} = {index}\n" for index in range(statements))
        + "def missed():\n    return -1\n",
    )
    project.write(
        "test_product.py",
        """from pathlib import Path
import product

def test_value(cov):
    assert cov is not None
    assert product.value_0 == 0
    Path('called').write_text('done')
""",
    )
    arguments = ["--cov=product", "--cov-report=", "--cov-precision=0", "-q"]
    if mode != "silent":
        arguments.append(f"--cov-fail-under={100 if mode == 'strict100' else 67}")
    observed = []
    for route in ["oracle", "candidate"]:
        result = _run(project, route, *arguments)
        assert (project.root / "called").is_file(), result.stdout + result.stderr
        (project.root / "called").unlink()
        if route == "candidate":
            record = json.loads((project.root / "candidate-result.json").read_text())
            assert len(record["cases"]) == 1
            assert [phase["outcome"] for phase in record["cases"][0]["phases"]] == [
                "passed",
                "passed",
                "passed",
            ]
        data_exists = (project.root / f".coverage.{route}").is_file()
        assert data_exists, result.stdout + result.stderr
        measured = project.data(f".coverage.{route}").lines(str(project.root / "product.py"))
        assert measured == list(range(1, statements + 2))
        required = [
            line
            for line in result.stdout.splitlines()
            if line.startswith(("FAIL Required test coverage", "Required test coverage"))
        ]
        observed.append((result.returncode, "tests coverage" in result.stdout, required))
    assert observed[1] == observed[0]
    assert (
        observed[0]
        == {
            "silent": (0, False, []),
            "strict100": (
                1,
                True,
                ["FAIL Required test coverage of 100% not reached. Total coverage: 99.50%"],
            ),
            "rounded67": (
                0,
                True,
                ["FAIL Required test coverage of 67% not reached. Total coverage: 66.67%"],
            ),
        }[mode]
    )


def test_downstream_test_loop_failure_does_not_publish_coverage_reports(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("product.py", "value = 1\n")
    project.write("test_product.py", "import product\ndef test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """from pathlib import Path

def pytest_runtestloop(session):
    Path('loop_entered').write_text('done')
    raise RuntimeError('loop exploded')
""",
    )
    observed = []
    for route in ["oracle", "candidate"]:
        result = _run(project, route, "--cov=product", f"--cov-report=json:{route}.json", "-q")
        output = result.stdout + result.stderr
        assert (project.root / "loop_entered").is_file(), output
        (project.root / "loop_entered").unlink()
        assert "RuntimeError: loop exploded" in output
        observed.append(
            (
                result.returncode,
                (project.root / f".coverage.{route}").exists(),
                (project.root / f"{route}.json").exists(),
                "tests coverage" in output,
            )
        )
    assert observed[1] == observed[0] == (3, False, False, False)


@pytest.mark.parametrize(
    ("arguments", "warns"),
    [
        (["--no-cov", "--cov=product"], True),
        (["--cov=product", "--no-cov"], False),
        (["--cov=product", "--no-cov", "--cov-report="], True),
        (["--no-cov", "--cov-report="], False),
    ],
)
def test_no_cov_warning_preserves_option_order_and_warning_category(
    tmp_path: Path, arguments: list[str], warns: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("product.py", "value = 1\n")
    project.write(
        "test_product.py",
        """from pathlib import Path
def test_value(cov):
    assert cov is None
    Path('called').write_text('done')
""",
    )
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_warning_recorded(warning_message):
    if str(warning_message.message) == 'Coverage disabled via --no-cov switch!':
        with Path('warnings.jsonl').open('a') as output:
            output.write(json.dumps([warning_message.category.__name__,
                                     str(warning_message.message)]) + '\\n')
""",
    )
    observed = []
    for route in ["oracle", "candidate"]:
        result = _run(project, route, *arguments, "-q")
        output = result.stdout + result.stderr
        assert (project.root / "called").is_file(), output
        (project.root / "called").unlink()
        records = project.root / "warnings.jsonl"
        warnings = (
            [json.loads(line) for line in records.read_text().splitlines()]
            if records.exists()
            else []
        )
        records.unlink(missing_ok=True)
        observed.append(
            (
                result.returncode,
                (project.root / f".coverage.{route}").exists(),
                "tests coverage" in output,
                output.count("WARNING: Coverage disabled via --no-cov switch!"),
                warnings,
            )
        )
    expected_warning = [["CovDisabledWarning", "Coverage disabled via --no-cov switch!"]]
    assert (
        observed[1]
        == observed[0]
        == (
            0,
            False,
            warns,
            int(warns),
            expected_warning if warns else [],
        )
    )


@pytest.mark.parametrize("threshold", [None, 1])
def test_report_failure_preserves_data_and_warns_before_enforcing_threshold(
    tmp_path: Path, threshold: int | None
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_product.py",
        """from pathlib import Path
import product

def test_value(cov):
    assert cov is not None
    assert product.value == 1
    Path('called').write_text('done')
    Path('product.py').unlink()
""",
    )
    project.write(
        "driver.py",
        """import importlib
import json
import sys
import warnings
from pathlib import Path

api = importlib.import_module(sys.argv[1])
plugin = 'pytest_cov.plugin' if sys.argv[1] == 'pytest' else 'vfy.independent.coverage_support'
records = []
previous = warnings.showwarning
def observe(message, category, filename, lineno, file=None, line=None):
    if str(message).startswith('Failed to generate report:'):
        records.append([category.__name__, str(message)])
    previous(message, category, filename, lineno, file=file, line=line)
warnings.showwarning = observe
try:
    status = int(api.main(['-p', plugin, '--cov=product',
                           '--cov-report=json:report.json', '-q', *sys.argv[2:]]))
finally:
    warnings.showwarning = previous
Path('warnings.json').write_text(json.dumps(records))
raise SystemExit(status)
""",
    )
    observed = []
    for route in ["oracle", "candidate"]:
        project.write("product.py", "value = 1\n")
        arguments = [] if threshold is None else [f"--cov-fail-under={threshold}"]
        result = project.run_python(
            "driver.py",
            "pytest" if route == "oracle" else "vfy.independent.entry",
            *arguments,
            env={
                "COVERAGE_FILE": str(project.root / f".coverage.{route}"),
                "PYTHONPATH": _SOURCE,
            },
        )
        output = result.stdout + result.stderr
        assert (project.root / "called").is_file(), output
        (project.root / "called").unlink()
        assert "WARNING: Failed to generate report: No data to report." in output
        records = json.loads((project.root / "warnings.json").read_text())
        observed.append(
            (
                result.returncode,
                (project.root / f".coverage.{route}").is_file(),
                (project.root / "report.json").exists(),
                "Total coverage: 0.00%" in output,
                records,
            )
        )
    assert (
        observed[1]
        == observed[0]
        == (
            0 if threshold is None else 1,
            True,
            False,
            threshold is not None,
            [["CovReportWarning", "Failed to generate report: No data to report.\n"]],
        )
    )
