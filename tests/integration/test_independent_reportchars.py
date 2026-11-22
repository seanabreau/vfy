"""Report characters select the same terminal summary as the pytest CLI."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASES = """import pytest
import warnings

@pytest.fixture
def resource():
    print("setup output")
    yield
    print("teardown output")

def test_pass(resource):
    print("pass output")
    warnings.warn("reportchars warning")

@pytest.mark.parametrize("value", [1, 2])
@pytest.mark.skip(reason="same skip reason")
def test_skip(value):
    pass

@pytest.mark.parametrize("value", [
    pytest.param(1, marks=pytest.mark.skip(reason="parameter reason")),
    pytest.param(2, marks=pytest.mark.skip(reason="parameter reason")),
])
def test_parameter_skip(value):
    pass

@pytest.mark.xfail(reason="expected reason")
def test_xfail():
    assert False

@pytest.mark.xfail(reason="unexpected reason")
def test_xpass():
    print("xpass output")

@pytest.mark.xfail(reason="strict reason", strict=True)
def test_strict():
    pass

def test_fail():
    assert False, "failure reason"

@pytest.fixture
def broken():
    raise ValueError("setup reason")

def test_error(broken):
    pass
"""

_OBSERVER = """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    with Path("observed.jsonl").open("a") as output:
        value = [report.nodeid, report.when, report.outcome, getattr(report, "wasxfail", None)]
        output.write(json.dumps(value) + "\\n")
"""

_ENV = {"NO_COLOR": "1", "CI": None, "BUILD_NUMBER": None}


def _short_summary(output: str) -> list[str]:
    if "short test summary info" not in output:
        return []
    section = output.split("short test summary info", 1)[1]
    return [
        line
        for line in section.splitlines()
        if re.match(r"^(?:PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) ", line)
    ]


def _captures(output: str) -> list[str]:
    sections = re.findall(
        r"^=+ (PASSES|XPASSES) =+\n(.*?)(?=^=+ |^\d+ (?:failed|passed|skipped)|\Z)",
        output,
        re.MULTILINE | re.DOTALL,
    )
    return [heading + "\n" + body.rstrip() for heading, body in sections]


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("-ra",),
        ("-qra",),
        ("-rA",),
        ("-rN",),
        ("-rp",),
        ("-rP",),
        ("-rx",),
        ("-rX",),
        ("-rSF",),
        ("-rfEf",),
        ("-raNp",),
        ("-rZ",),
        ("-r", "s"),
        ("--report-chars=s",),
        ("-rs", "-rx"),
    ],
)
def test_report_characters_match_stock_summary_and_preserve_outcomes(
    tmp_path: Path, arguments: tuple[str, ...], workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_results.py", _CASES)
    project.write("conftest.py", _OBSERVER)
    project.write(
        "test_collection_skip.py",
        'import pytest\npytest.skip("module reason", allow_module_level=True)\n',
    )
    oracle = project.run_python("-m", "pytest", "-q", *arguments, env=_ENV)
    observations = tmp_path / "observed.jsonl"
    expected = observations.read_text()
    observations.unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "-q",
        *arguments,
        env=_ENV,
    )
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 1, diagnostic
    assert sorted(observations.read_text().splitlines()) == sorted(expected.splitlines()), (
        diagnostic
    )
    assert _short_summary(candidate.stdout) == _short_summary(oracle.stdout), diagnostic
    assert _captures(candidate.stdout) == _captures(oracle.stdout), diagnostic
    assert "reportchars warning" in candidate.stdout, diagnostic
    assert "AssertionError: failure reason" in candidate.stdout, diagnostic
    assert "ValueError: setup reason" in candidate.stdout, diagnostic
    if _captures(candidate.stdout) and _short_summary(candidate.stdout):
        assert candidate.stdout.index("ValueError: setup reason") < candidate.stdout.index(
            "PASSES"
        ), diagnostic
        assert candidate.stdout.index("PASSES") < candidate.stdout.index(
            "short test summary info"
        ), diagnostic


@pytest.mark.parametrize("source", ["configuration", "environment", "command"])
def test_report_character_precedence_matches_stock(tmp_path: Path, source: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_results.py", _CASES)
    project.write("pytest.ini", "[pytest]\naddopts = -ra\n")
    arguments: tuple[str, ...] = ("-r", "p") if source == "command" else ()
    env = {**_ENV, "PYTEST_ADDOPTS": "-rx" if source != "configuration" else None}
    oracle = project.run_python("-m", "pytest", "-q", *arguments, env=env)
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q", *arguments, env=env)
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 1, diagnostic
    assert _short_summary(candidate.stdout) == _short_summary(oracle.stdout), diagnostic


def test_report_character_warning_disable_preserves_recorded_warning(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_results.py", _CASES)
    oracle = project.run_python("-m", "pytest", "-q", "-rAw", "--disable-warnings", env=_ENV)
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-rAw",
        "--disable-warnings",
        "--native-result-json=native.json",
        env=_ENV,
    )
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 1, diagnostic
    assert "reportchars warning" not in oracle.stdout + candidate.stdout, diagnostic
    assert _short_summary(candidate.stdout) == _short_summary(oracle.stdout), diagnostic
    record = json.loads((tmp_path / "native.json").read_text())
    assert any("reportchars warning" in warning["message"] for warning in record["warnings"])


def test_short_summary_trimming_uses_terminal_width_and_verbose_override(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_failure.py", 'def test_failure():\n    assert False, "' + "wide 界" * 30 + '"\n'
    )
    for arguments in (("-q",), ("-vv",)):
        oracle = project.run_python("-m", "pytest", *arguments, "-rf", env=_ENV)
        candidate = project.run_vfy(
            "test", "--engine=native", "--no-cov", *arguments, "-rf", env=_ENV
        )
        diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
        assert oracle.returncode == candidate.returncode == 1, diagnostic
        assert _short_summary(candidate.stdout) == _short_summary(oracle.stdout), diagnostic


@pytest.mark.parametrize("workers", [1, 2])
def test_report_characters_include_collection_errors(tmp_path: Path, workers: int) -> None:
    project = VfyProject(tmp_path)
    project.write("test_import.py", 'raise ValueError("collection reason")\n')
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "def pytest_collectreport(report):\n"
        "    if report.failed:\n"
        "        Path('collection-hook').write_text(report.nodeid)\n",
    )
    oracle = project.run_python("-m", "pytest", "-q", "-ra", env=_ENV)
    marker = tmp_path / "collection-hook"
    expected = marker.read_text()
    marker.unlink()
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", f"--workers={workers}", "-q", "-ra", env=_ENV
    )
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 2, diagnostic
    assert marker.read_text() == expected, diagnostic
    assert _short_summary(candidate.stdout) == _short_summary(oracle.stdout), diagnostic
    assert re.search(r"^1 errors?\b", candidate.stdout, re.MULTILINE), diagnostic


def test_summary_uses_the_status_hooks_actual_markup_mapping(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_failure.py", 'def test_failure():\n    assert False, "mapping reason"\n')
    project.write(
        "conftest.py",
        "from collections import UserDict\n"
        "def pytest_report_teststatus(report):\n"
        "    if report.when == 'call' and report.failed:\n"
        "        return 'failed', 'F', ('CUSTOM_FAILED', UserDict(red=True))\n",
    )
    oracle = project.run_python("-m", "pytest", "-q", "-rf", env=_ENV)
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-rf", env=_ENV)
    diagnostic = oracle.stdout + oracle.stderr + candidate.stdout + candidate.stderr
    assert oracle.returncode == candidate.returncode == 1, diagnostic
    expected = [line for line in oracle.stdout.splitlines() if line.startswith("CUSTOM_FAILED ")]
    assert expected, diagnostic
    assert [
        line for line in candidate.stdout.splitlines() if line.startswith("CUSTOM_FAILED ")
    ] == (expected), diagnostic
