"""Absent plug-ins retain functional tests and reject unavailable services."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import sys
import venv
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("collect_only", [False, True])
def test_absent_benchmark_dependency_keeps_nested_collection_skip(
    tmp_path: Path, workers: int, collect_only: bool
) -> None:
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site = (
        environment / "Lib/site-packages"
        if os.name == "nt"
        else environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    # Reuse only the installed vfy wheel and pytest's runtime dependencies.
    for name in ("vfy", "pytest", "_pytest", "pluggy", "iniconfig", "packaging", "pygments"):
        spec = importlib.util.find_spec(name)
        assert spec is not None
        assert spec.origin is not None
        package = Path(spec.origin).parent
        (site / name).symlink_to(package, target_is_directory=True)
    for name in ("vfy", "pytest", "pluggy", "iniconfig", "packaging", "pygments"):
        distribution = importlib.metadata.distribution(name)
        assert distribution.files is not None
        metadata = next(
            file for file in distribution.files if str(file).endswith(".dist-info/METADATA")
        )
        directory = Path(str(distribution.locate_file(metadata))).parent
        (site / directory.name).symlink_to(directory, target_is_directory=True)
    py_shim = Path(str(importlib.metadata.distribution("pytest").locate_file("py.py")))
    if py_shim.is_file():
        (site / "py.py").symlink_to(py_shim)

    project = VfyProject(tmp_path / "project", interpreter=python)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "branch/conftest.py",
        "from importlib.util import find_spec\nimport pytest\n"
        "if not (find_spec('pytest_benchmark') or find_spec('pytest_codspeed')):\n"
        "    pytest.skip('optional benchmark', allow_module_level=True)\n",
    )
    project.write("branch/test_skipped.py", "def test_unreachable(): assert False\n")
    project.write("branch/nested/test_nested.py", "def test_unreachable(): assert False\n")
    project.write("test_survives.py", "def test_survives(): pass\n")
    project.write(
        "observer.py",
        "import json\nfrom pathlib import Path\nreports=[]\n"
        "def pytest_collectreport(report):\n"
        "    if report.skipped: reports.append([report.nodeid, report.outcome])\n"
        "def pytest_sessionfinish(): Path('reports.json').write_text(json.dumps(reports))\n",
    )
    options = ["-q", "-p", "observer", "--junitxml=result.xml"]
    if collect_only:
        options.append("--collect-only")

    def evidence() -> tuple[list[tuple[str, str, list[tuple[str, str]]]], dict[str, str], object]:
        suite = ET.parse(project.root / "result.xml").getroot().find("testsuite")
        assert suite is not None
        cases = [
            (
                case.attrib["classname"],
                case.attrib["name"],
                [(child.tag, child.attrib.get("message", "")) for child in case],
            )
            for case in suite.findall("testcase")
        ]
        counts = {key: suite.attrib[key] for key in ("tests", "skipped", "errors", "failures")}
        return cases, counts, json.loads((project.root / "reports.json").read_text())

    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
    stock = project.run_pytest(*options, load_xdist=False, env=env)
    stock.expect(0)
    expected = evidence()
    assert expected[2] == [["branch", "skipped"]]
    assert expected[0][0] == ("", "branch", [("skipped", "collection skipped")])
    result = project.run_vfy(
        "test",
        "--engine=native",
        f"--workers={workers}",
        "--no-cov",
        "--native-result-json=record.json",
        *options,
        env=env,
    )
    result.expect(0)
    actual = evidence()
    assert actual[:2] == expected[:2]
    if workers == 1:
        assert actual[2] == expected[2]
    record = json.loads((project.root / "record.json").read_text())
    assert [
        [report["nodeid"], report["outcome"]]
        for report in record["collection_reports"]
        if report["outcome"] == "skipped"
    ] == expected[2]
    if workers == 1 and not collect_only:
        benchmark = VfyProject(tmp_path / "benchmark", interpreter=python)
        benchmark.write(
            "test_benchmark.py",
            "def test_benchmark(benchmark):\n    assert benchmark(lambda: 7) == 7\n",
        )
        replacement = benchmark.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        replacement.expect(0)


@pytest.mark.parametrize(
    ("arguments", "ini", "marker", "environment", "diagnostic"),
    [
        (["--session-timeout=1"], "", "", {}, "session_timeout requires pytest-timeout"),
        (["--timeout-method=thread"], "", "", {}, "thread timers require pytest-timeout"),
        ([], "timeout_method = thread\n", "", {}, "thread timers require pytest-timeout"),
        (
            [],
            "",
            "@pytest.mark.timeout(1, method='thread')\n",
            {},
            "thread timers require pytest-timeout",
        ),
        (
            ["--timeout-disable-debugger-detection"],
            "",
            "",
            {},
            "timeout_disable_debugger_detection requires pytest-timeout",
        ),
    ],
)
def test_timeout_replacement_rejects_unsupported_timers_before_test_execution(
    tmp_path: Path,
    arguments: list[str],
    ini: str,
    marker: str,
    environment: dict[str, str],
    diagnostic: str,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n" + ini)
    project.write(
        "test_timer.py",
        "import pytest\nfrom pathlib import Path\n"
        + marker
        + "def test_timer(): Path('ran').touch()\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
        *arguments,
        env=environment,
    )
    result.expect(4)
    assert diagnostic in result.stdout + result.stderr
    assert not (tmp_path / "ran").exists()


def test_timeout_replacement_preserves_explicit_disable_precedence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\ntimeout = 1\n")
    project.write(
        "test_timer.py",
        "import pytest\n@pytest.mark.timeout(0)\ndef test_timer(): pass\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.timeout",
        "--timeout=0",
        env={"PYTEST_TIMEOUT": "2"},
    )
    result.expect(0)
    assert "1 passed" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "marker", "diagnostic"),
    [
        (["--benchmark-json=result.json"], "", "--benchmark-json requires pytest-benchmark"),
        (["--benchmark-compare=0001"], "", "--benchmark-compare requires pytest-benchmark"),
        (["--benchmark-cprofile=time"], "", "--benchmark-cprofile requires pytest-benchmark"),
        (["--benchmark-enable"], "", "--benchmark-enable requires pytest-benchmark"),
        (["--codspeed"], "", "--codspeed requires pytest-codspeed"),
        (
            [],
            "@pytest.mark.benchmark(min_rounds=2)\n",
            "benchmark marker options require pytest-benchmark",
        ),
    ],
)
def test_benchmark_replacement_rejects_measurement_requests_before_test_execution(
    tmp_path: Path, arguments: list[str], marker: str, diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_benchmark.py",
        "import pytest\nfrom pathlib import Path\n"
        + marker
        + "def test_benchmark(benchmark): Path('ran').touch()\n",
    )
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark", *arguments
    )
    assert result.returncode == (1 if arguments == ["--benchmark-enable"] else 4), (
        result.stdout + result.stderr
    )
    assert diagnostic in result.stdout + result.stderr
    assert not (tmp_path / "ran").exists()
    assert not (tmp_path / "result.json").exists()


def test_unused_benchmark_warmup_configuration_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --benchmark-warmup=on\n")
    project.write("test_normal.py", "def test_normal(): pass\n")
    stock = project.run_pytest("-p", "pytest_benchmark.plugin", load_xdist=False)
    stock.expect(0)
    actual = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark"
    )
    actual.expect(0)
    assert "1 passed" in stock.stdout
    assert "1 passed" in actual.stdout


@pytest.mark.parametrize("dynamic", [False, True])
def test_benchmark_warmup_rejects_fixture_before_target(tmp_path: Path, dynamic: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = --benchmark-warmup=on\n")
    fixture = "request" if dynamic else "benchmark"
    benchmark = "request.getfixturevalue('benchmark')" if dynamic else "benchmark"
    project.write(
        "test_benchmark.py",
        "from pathlib import Path\n"
        f"def test_benchmark({fixture}):\n"
        f"    {benchmark}(lambda: Path('ran').touch())\n",
    )
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark"
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "--benchmark-warmup requires pytest-benchmark" in result.stdout + result.stderr
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize(
    "option",
    [
        "--benchmark-sort=fullname",
        "--benchmark-group-by=fullname",
        "--benchmark-columns=min,max",
        "--benchmark-name=short",
        "--benchmark-time-unit=ms",
    ],
)
def test_unused_benchmark_display_options_match_pytest(tmp_path: Path, option: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\naddopts = {option}\n")
    project.write("test_normal.py", "def test_normal(): pass\n")
    stock = project.run_pytest("-p", "pytest_benchmark.plugin", load_xdist=False)
    actual = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark"
    )
    assert stock.returncode == actual.returncode == 0, stock.stdout + actual.stdout + actual.stderr
    assert "1 passed" in stock.stdout
    assert "1 passed" in actual.stdout

    project.write(
        "test_normal.py",
        "from pathlib import Path\n"
        "def test_normal(benchmark): benchmark(lambda: Path('ran').touch())\n",
    )
    requested = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark"
    )
    requested.expect(1)
    diagnostic = f"{option.split('=')[0]} requires pytest-benchmark"
    assert diagnostic in requested.stdout + requested.stderr
    assert not (tmp_path / "ran").exists()


def test_invalid_benchmark_time_unit_matches_pytest_exit(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_normal.py", "def test_normal(): pass\n")
    option = "--benchmark-time-unit=invalid"
    stock = project.run_pytest("-p", "pytest_benchmark.plugin", option, load_xdist=False)
    actual = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark", option
    )
    assert stock.returncode == actual.returncode == 4, stock.stderr + actual.stderr
    assert "invalid choice" in stock.stderr
    assert "invalid choice" in actual.stderr


def test_unmeasured_benchmark_calls_match_upstream_disabled_mode(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_benchmark.py",
        """import json
from pathlib import Path
import pytest

events = []

def target(value=7):
    events.append(['target', value])
    return value

def setup():
    events.append(['setup'])
    return (9,), {}

def teardown(value):
    events.append(['teardown', value])

def test_direct(benchmark):
    assert benchmark(target, 3) == 3

@pytest.mark.benchmark(group='functional')
def test_repeated(benchmark):
    assert benchmark.disabled
    assert benchmark.group == 'functional'
    assert benchmark.pedantic(target, rounds=3, warmup_rounds=2, iterations=4) == 7

def test_prepared(benchmark):
    assert benchmark.pedantic(target, setup=setup, teardown=teardown) == 9

@pytest.mark.parametrize('options', [
    {'iterations': 0}, {'rounds': 0}, {'warmup_rounds': -1},
    {'iterations': 2, 'setup': setup},
])
def test_invalid(benchmark, options):
    with pytest.raises(ValueError): benchmark.pedantic(target, **options)

def test_conflicting_arguments(benchmark):
    with pytest.raises(TypeError): benchmark.pedantic(target, args=(1,), setup=setup)

def test_record():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_pytest(
        "-p", "pytest_benchmark.plugin", "--benchmark-disable", load_xdist=False
    )
    stock.expect(0)
    expected = json.loads((tmp_path / "events.json").read_text())
    (tmp_path / "events.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.benchmark",
        "--benchmark-disable",
    )
    actual.expect(0)
    assert (
        json.loads((tmp_path / "events.json").read_text())
        == expected
        == [
            ["target", 3],
            ["target", 7],
            ["setup"],
            ["target", 9],
            ["setup"],
        ]
    )


def test_explicitly_disabled_benchmark_ignores_measurement_settings(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_benchmark.py",
        """import gc, json, os
from pathlib import Path
import pytest

events = []

def test_call(benchmark, request):
    assert benchmark.disabled and benchmark.stats is None
    warmup = request.config.getoption('benchmark_warmup')
    iterations = request.config.getoption('benchmark_warmup_iterations')
    events.append(['options', warmup is True, type(iterations) is int, iterations])
    def target():
        events.append(['call', gc.isenabled()])
        return 42
    assert benchmark(target) == 42

def test_error(benchmark):
    def target():
        events.append(['error', gc.isenabled()])
        raise ValueError('target error')
    with pytest.raises(ValueError, match='target error'):
        benchmark(target)

def test_record():
    Path('events.json').write_text(json.dumps(events))
""",
    )
    options = (
        "--benchmark-disable",
        "--benchmark-disable-gc",
        "--benchmark-warmup=true",
        "--benchmark-warmup-iterations=5",
        "--benchmark-sort=fullname",
        "--benchmark-group-by=fullname",
    )
    stock = project.run_pytest("-p", "pytest_benchmark.plugin", *options, load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "events.json").read_text())
    (tmp_path / "events.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.benchmark",
        *options,
    )
    actual.expect(0)
    assert (
        json.loads((tmp_path / "events.json").read_text())
        == expected
        == [
            ["options", True, True, 5],
            ["call", True],
            ["error", True],
        ]
    )


def test_benchmark_enable_overrides_explicit_disable(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_benchmark.py",
        "def test_benchmark(benchmark):\n"
        "    assert benchmark.enabled\n"
        "    benchmark(lambda: None)\n"
        "    assert benchmark.stats is not None\n",
    )
    stock = project.run_pytest(
        "-p",
        "pytest_benchmark.plugin",
        "--benchmark-disable",
        "--benchmark-enable",
        "--benchmark-warmup=on",
        "--benchmark-min-time=0.000001",
        "--benchmark-max-time=0.001",
        "--benchmark-warmup-iterations=1",
        load_xdist=False,
    )
    stock.expect(0)
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.benchmark",
        "--benchmark-disable",
        "--benchmark-enable",
        "--benchmark-warmup=on",
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "--benchmark-enable requires pytest-benchmark" in result.stdout + result.stderr


def test_bare_benchmark_warmup_matches_disabled_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_benchmark.py",
        """import os
def test_benchmark(benchmark, request):
    assert request.config.getoption('benchmark_warmup') is None
    assert benchmark.disabled and benchmark.stats is None
    calls = []
    assert benchmark(lambda: calls.append(1)) is None
    assert calls == [1]
""",
    )
    options = ("--benchmark-warmup", "--benchmark-disable")
    stock = project.run_pytest("-p", "pytest_benchmark.plugin", *options, load_xdist=False)
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.independent.benchmark",
        *options,
    )
    assert stock.returncode == actual.returncode == 0, stock.stdout + actual.stdout + actual.stderr


@pytest.mark.parametrize(
    ("argument", "expected"),
    [("--benchmark-skip", ["ordinary"]), ("--benchmark-only", ["benchmark"])],
)
def test_benchmark_replacement_selection_preserves_functional_tests(
    tmp_path: Path, argument: str, expected: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_benchmark.py",
        """from pathlib import Path
def record(value):
    with Path('events').open('a') as output: output.write(value + '\\n')
def test_benchmark(benchmark): benchmark(record, 'benchmark')
def test_ordinary(): record('ordinary')
""",
    )
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark", argument
    )
    result.expect(0)
    assert (tmp_path / "events").read_text().splitlines() == expected


def test_benchmark_replacement_rejects_unavailable_instrumentation(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_benchmark.py",
        """import pytest
def test_instrumentation(benchmark):
    with pytest.raises(NotImplementedError, match='requires pytest-benchmark'):
        benchmark.weave('module.function')
""",
    )
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-p", "vfy.independent.benchmark"
    )
    result.expect(0)
