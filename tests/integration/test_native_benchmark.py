"""Installed pytest-benchmark runs through the native adapter like stock pytest."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_AUTOLOAD = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}

_TESTS = """\
import pytest


def test_plain(benchmark):
    assert benchmark(sum, [1, 2]) == 3


def test_pedantic(benchmark):
    result = benchmark.pedantic(sorted, setup=lambda: (([3, 1, 2],), {}), rounds=3)
    assert result == [1, 2, 3]


@pytest.mark.benchmark(group="grouped", min_rounds=2)
def test_grouped(benchmark):
    benchmark.extra_info["kind"] = "len"
    assert benchmark(len, "abc") == 3


def test_plain_test():
    assert True
"""


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("test_bench.py", _TESTS)
    return project


def _stock(project: VfyProject, *arguments: str) -> CommandResult:
    return project.run_python(
        "-m", "pytest", "-p", "no:cacheprovider", "-q", *arguments, env=_AUTOLOAD
    )


def _native(project: VfyProject, *arguments: str) -> CommandResult:
    return project.run_vfy(
        "test", "--engine=pytest-native", "--no-cov", "-q", *arguments, env=_AUTOLOAD
    )


def _summary(stdout: str) -> str:
    last = [line for line in stdout.splitlines() if line.strip()][-1]
    return re.sub(r" in \d+\.\d+s.*$", "", last)


def test_benchmark_outcomes_and_report_match_stock_pytest(tmp_path: Path) -> None:
    stock = _stock(_project(tmp_path / "stock"))
    native = _native(_project(tmp_path / "native"), "--workers=1")
    stock.expect(0)
    native.expect(0)
    assert "4 passed" in stock.stdout
    assert "4 passed" in native.stdout
    assert "Name (time in" in native.stdout
    assert "test_plain" in native.stdout
    assert "test_pedantic" in native.stdout


@pytest.mark.parametrize("option", ["--benchmark-disable", "--benchmark-skip", "--benchmark-only"])
def test_benchmark_mode_summary_matches_stock_pytest(tmp_path: Path, option: str) -> None:
    stock = _stock(_project(tmp_path / "stock"), option)
    native = _native(_project(tmp_path / "native"), "--workers=1", option)
    assert stock.returncode == native.returncode
    assert _summary(stock.stdout) == _summary(native.stdout)


def test_benchmark_json_lists_measured_benchmarks(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = _native(project, "--workers=1", "--benchmark-json=bench.json")
    result.expect(0)
    data = json.loads((project.root / "bench.json").read_text(encoding="utf-8"))
    assert sorted(bench["name"] for bench in data["benchmarks"]) == [
        "test_grouped",
        "test_pedantic",
        "test_plain",
    ]
    grouped = next(bench for bench in data["benchmarks"] if bench["name"] == "test_grouped")
    assert grouped["group"] == "grouped"
    assert grouped["extra_info"] == {"kind": "len"}


def test_native_workers_run_benchmarks_once_and_explain_why(tmp_path: Path) -> None:
    result = _native(_project(tmp_path), "--workers=2")
    result.expect(0)
    assert "4 passed" in result.stdout
    assert (
        "vfy: pytest-benchmark does not measure benchmarks in native workers; "
        "pass --workers=1 to measure them"
    ) in result.stdout
    assert "Name (time in" not in result.stdout


@pytest.mark.parametrize(
    ("arguments", "expect_note"),
    [
        ((), True),
        (("--benchmark-disable",), False),
        (("--benchmark-skip",), False),
        (("test_bench.py::test_plain_test",), False),
    ],
)
def test_fresh_workers_explain_unmeasured_benchmarks(
    tmp_path: Path, arguments: tuple[str, ...], expect_note: bool
) -> None:
    project = _project(tmp_path)
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--no-cov",
        "-q",
        "--workers=2",
        *arguments,
        env={**_AUTOLOAD, "VFY_WORKER_START": "exec"},
    )
    result.expect(0)
    assert (
        "vfy: pytest-benchmark does not measure benchmarks in native workers" in result.stdout
    ) is expect_note


def test_disabling_the_plugin_matches_stock_pytest(tmp_path: Path) -> None:
    stock = _stock(_project(tmp_path / "stock"), "-p", "no:benchmark")
    native = _native(_project(tmp_path / "native"), "-p", "no:benchmark")
    assert stock.returncode == native.returncode == 1
    assert "fixture 'benchmark' not found" in stock.stdout
    assert "fixture 'benchmark' not found" in native.stdout


def test_diagnostics_reports_pytest_benchmark_loaded(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = project.run_vfy("diagnostics", "--engine=pytest-native", "--json", env=_AUTOLOAD)
    result.expect(0)
    benchmark = next(
        row
        for row in json.loads(result.stdout)["pytest_plugin_inventory"]
        if row["name"] == "benchmark"
    )
    assert benchmark["native_status"] == "loaded"


def test_skipped_and_xfailed_benchmarks_do_not_warn_about_an_unused_fixture(
    tmp_path: Path,
) -> None:
    # Upstream suppresses its unused-fixture warning for skipped outcomes; under
    # filterwarnings = error, a missed suppression would be a teardown error.
    files = {
        "pytest.ini": "[pytest]\nfilterwarnings = error\n",
        "test_skips.py": "import pytest\n\n"
        "def test_skips(benchmark):\n    pytest.skip('no backend')\n\n"
        "@pytest.mark.xfail(reason='known')\n"
        "def test_xfails(benchmark):\n    assert False\n\n"
        "def test_unused(benchmark):\n    pass\n",
    }
    results = []
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        for name, source in files.items():
            project.write(name, source)
        results.append(_stock(project) if route == "stock" else _native(project, "--workers=1"))
    stock, native = results
    assert (
        _summary(stock.stdout)
        == _summary(native.stdout)
        == "1 passed, 1 skipped, 1 xfailed, 1 error"
    )
    assert stock.returncode == native.returncode


def test_conftest_benchmark_hooks_run_as_under_stock_pytest(tmp_path: Path) -> None:
    conftest = (
        "def pytest_benchmark_update_json(config, benchmarks, output_json):\n"
        "    output_json['project'] = 'demo'\n"
    )
    outputs = []
    for route in ("stock", "native"):
        project = _project(tmp_path / route)
        project.write("conftest.py", conftest)
        result = (
            _stock(project, "--benchmark-json=bench.json")
            if route == "stock"
            else _native(project, "--workers=1", "--benchmark-json=bench.json")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(json.loads((project.root / "bench.json").read_text(encoding="utf-8")))
    assert outputs[0]["project"] == outputs[1]["project"] == "demo"
