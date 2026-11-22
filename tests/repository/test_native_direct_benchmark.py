"""Public process parity for the serial native pytest entry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from validation.pytest_runner import benchmark_native_xdist as benchmark
from validation.pytest_runner.check_native_adoption_matrix import Project

from tests.support import VfyProject


@pytest.mark.parametrize("scheduling", [None, "cases"])
def test_independent_benchmark_routes_apply_only_explicit_worker_scheduling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheduling: str | None
) -> None:
    monkeypatch.delenv("VFY_NATIVE_SCHEDULING", raising=False)
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    candidate = benchmark.route_environment(
        descriptor,
        tmp_path,
        Path(sys.executable),
        benchmark.Route("native", 4, False),
        scheduling,
    )
    reference = benchmark.route_environment(
        descriptor, tmp_path, Path(sys.executable), benchmark.Route("stock", 4), scheduling
    )
    assert candidate.get("VFY_NATIVE_SCHEDULING") == scheduling
    assert "VFY_NATIVE_SCHEDULING" not in reference


@pytest.mark.parametrize("fail", [False, True])
def test_native_direct_public_entry_preserves_junit_and_uses_rust(
    tmp_path: Path, fail: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = --strict-markers --cov=tests --cov-report=term\n",
    )
    project.write(
        "conftest.py",
        "import json, os, sys\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def output(request):\n"
        "    print('fixture setup')\n"
        "    os.write(1, b'fixture setup fd stdout\\n')\n"
        "    os.write(2, b'fixture setup fd stderr\\n')\n"
        "    yield\n"
        "    print('fixture teardown')\n"
        "    os.write(1, b'fixture teardown fd stdout\\n')\n"
        "    os.write(2, b'fixture teardown fd stderr\\n')\n"
        "    if os.environ.get('DIRECT_ENTRY_ORACLE') == '1':\n"
        "        observer = request.config.pluginmanager.getplugin('_vfy_run_observer')\n"
        "        assert observer is not None and observer._native_bridge\n"
        "        assert request.config.pluginmanager.hasplugin('vfy.pytest_plugin')\n"
        "        assert not request.config.pluginmanager.hasplugin('_vfy_test_selection')\n"
        "        assert request.config.getoption('no_cov')\n"
        "        assert not any(name in sys.modules for name in\n"
        "            ('vfy.cli', 'vfy.run.controller'))\n"
        "        Path('direct-modules.json').write_text(json.dumps(sorted(sys.modules)))\n",
    )
    project.write(
        "tests/test_cases.py",
        "import os, sys, warnings\nimport pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_parameter(value, record_property, mocker):\n"
        "    assert mocker.Mock(return_value=value)() == value\n"
        "    record_property('value', value)\n"
        "    print('body stdout', value)\n"
        "    print('body stderr', value, file=sys.stderr)\n"
        "    os.write(1, b'body fd stdout\\n')\n"
        "    os.write(2, b'body fd stderr\\n')\n"
        "    warnings.warn('body warning', UserWarning)\n"
        "    assert value > 0\n"
        "@pytest.mark.skip(reason='oracle skip')\n"
        "def test_skip():\n    pass\n"
        "@pytest.mark.xfail(reason='oracle xfail', strict=True)\n"
        "def test_xfail():\n    assert False\n"
        f"def test_result():\n    assert {not fail!r}, 'oracle failure'\n",
    )
    descriptor = Project(
        "fixture",
        "",
        "",
        ".",
        ("tests",),
        (),
        plugins=("pytest_mock",),
        stock_plugins=("pytest_cov.plugin",),
        outcome_args=("--no-cov",),
    )
    python = Path(sys.executable)
    routes = [
        benchmark.Route("stock", 0),
        benchmark.Route("pytest-native", 1, False),
        benchmark.Route("pytest-native", 1, False, "direct"),
    ]
    inventory = [
        "tests/test_cases.py::test_parameter[1]",
        "tests/test_cases.py::test_parameter[2]",
        "tests/test_cases.py::test_skip",
        "tests/test_cases.py::test_xfail",
        "tests/test_cases.py::test_result",
    ]
    expected = None
    for route in routes:
        destination = tmp_path / route.name
        destination.mkdir()
        environment = benchmark.route_environment(
            descriptor,
            tmp_path,
            python,
            route,
            None,
            output=destination,
        )
        if route.engine == "pytest-native":
            environment["PYTEST_NATIVE_RESULT"] = str(destination / "native.json")
        if route.transport == "direct":
            environment["DIRECT_ENTRY_ORACLE"] = "1"
        arguments = benchmark.command(descriptor, tmp_path, python, destination, None, route)
        result = project.run(*arguments, env=environment)
        assert result.returncode == int(fail), result.stdout + result.stderr
        cases = benchmark.adoption.junit_records(
            destination / "junit.xml", tmp_path, tmp_path / "base"
        )
        assert len(cases) == len(inventory)
        if expected is None:
            expected = cases
        else:
            assert cases == expected
        if route.engine == "pytest-native":
            proof = benchmark.native_proof(
                destination, tmp_path, python, tmp_path / "base", route, cases, inventory
            )
            assert [record["engine"] for record in proof] == ["pytest-runner-native-v1"]
        if route.transport == "direct":
            assert arguments[:4] == [str(python), "-P", "-m", "pytest"]
            plugins = [
                arguments[index + 1] for index, value in enumerate(arguments) if value == "-p"
            ]
            assert plugins[:4] == [
                "no:cacheprovider",
                "no:asyncio",
                "vfy.pytest_plugin",
                "vfy._pytest.native_pytest",
            ]
            assert "pytest_mock" in plugins
            assert not any(argument.startswith("--workers") for argument in arguments)
            assert "--no-cov" in arguments
            assert "--history" not in arguments
            assert not (destination / "history.json").exists()
            modules = json.loads((tmp_path / "direct-modules.json").read_text())
            assert "vfy._native" in modules
            assert "vfy._pytest.native_pytest" in modules
            assert "vfy.pytest_plugin" in modules
            direct_results = json.loads((destination / "direct-results.json").read_text())
            assert direct_results["infrastructure_failed"] is False
            assert sorted(row["nodeid"] for row in direct_results["tests"]) == sorted(inventory)
            assert next(
                row["outcome"]
                for row in direct_results["tests"]
                if row["nodeid"].endswith("::test_result")
            ) == ("failed" if fail else "passed")
            (destination / "native.json").unlink()
            with pytest.raises(
                ValueError, match="missing or unexpected native worker result files"
            ):
                benchmark.native_proof(
                    destination, tmp_path, python, tmp_path / "base", route, cases, inventory
                )
        elif route.engine == "pytest-native":
            assert (destination / "history.json").is_file()
    assert not (tmp_path / ".coverage").exists()


@pytest.mark.parametrize(
    "route",
    [benchmark.Route("stock", 0), benchmark.Route("pytest-native", 1, False, "direct")],
    ids=["stock", "native-direct"],
)
def test_native_direct_does_not_override_required_plugins(
    tmp_path: Path, route: benchmark.Route
) -> None:
    project = VfyProject(tmp_path)
    config = project.write(
        "pytest.ini",
        "[pytest]\nrequired_plugins = unavailable-direct-oracle-plugin>=1\n",
    )
    original = config.read_bytes()
    project.write(
        "tests/test_case.py",
        "from pathlib import Path\ndef test_case():\n    Path('ran').touch()\n",
    )
    descriptor = Project("fixture", "", "", ".", ("tests",), ())
    python = Path(sys.executable)
    destination = tmp_path / "run"
    destination.mkdir()
    environment = benchmark.route_environment(
        descriptor, tmp_path, python, route, None, output=destination
    )
    environment["PYTEST_NATIVE_RESULT"] = str(destination / "native.json")
    result = project.run(
        *benchmark.command(descriptor, tmp_path, python, destination, None, route),
        env=environment,
    )
    result.expect(4)
    assert "Missing required plugins" in result.stderr
    assert config.read_bytes() == original
    assert not (tmp_path / "ran").exists()
    assert not (destination / "native.json").exists()
    assert not (destination / "direct-results.json").exists()


_CACHE_SOURCE = """\
from pathlib import Path
import sys
import pytest

retained_sys = sys

@pytest.fixture
def resource(request, cache):
    assert request.getfixturevalue('cache') is cache
    assert request.config.cache is cache
    request.addfinalizer(lambda: record('finalizer'))
    return cache

def record(name):
    with Path('order.txt').open('a') as stream:
        stream.write(name + '\\n')

def test_passing(cache, request, resource, capsys):
    assert request.config.cache is cache
    assert request.getfixturevalue('resource') is resource is cache
    assert retained_sys is sys
    retained_sys.stdout.write('stdout alias sentinel\\n')
    retained_sys.stderr.write('stderr alias sentinel\\n')
    captured = capsys.readouterr()
    assert captured.out == 'stdout alias sentinel\\n'
    assert captured.err == 'stderr alias sentinel\\n'
    assert 'vfy.cli' not in sys.modules
    assert 'vfy.run.controller' not in sys.modules
    prior = cache.get('entry/value', 0)
    cache.set('entry/value', prior + 1)
    record('pass:' + str(prior + 1))

def test_failure(cache):
    assert cache.get('entry/value', 0) >= 1
    record('fail')
    assert 1 == 2
"""


def test_native_direct_preserves_cache_and_failure_selection(tmp_path: Path) -> None:
    observations = []
    for route in (
        benchmark.Route("stock", 0),
        benchmark.Route("pytest-native", 1, False, "direct"),
    ):
        project = VfyProject(tmp_path / route.name)
        project.write(
            "pytest.ini",
            "[pytest]\ncache_dir = .cache\naddopts = --cov=. --cov-report=term\n",
        )
        project.write("test_cache.py", _CACHE_SOURCE)
        observed_runs = []
        for index, (flags, expected_order) in enumerate(
            (
                ((), ["pass:1", "finalizer", "fail"]),
                (("--lf",), ["fail"]),
                (("--ff",), ["fail", "pass:2", "finalizer"]),
            )
        ):
            descriptor = Project(
                "fixture",
                "",
                "",
                ".",
                ("test_cache.py",),
                (),
                stock_plugins=("pytest_cov.plugin",),
                outcome_args=("--no-cov",),
                extra_args=flags,
            )
            destination = project.root / f"run-{index}"
            destination.mkdir()
            order = project.root / "order.txt"
            order.unlink(missing_ok=True)
            python = Path(sys.executable)
            environment = benchmark.route_environment(
                descriptor,
                project.root,
                python,
                route,
                None,
                output=destination,
            )
            completed = project.run(
                *benchmark.command(descriptor, project.root, python, destination, None, route),
                env=environment,
            )
            transcript = completed.stdout + completed.stderr
            assert completed.returncode == 1, transcript
            assert order.read_text().splitlines() == expected_order, transcript
            assert not (project.root / ".coverage").exists(), transcript
            cached = json.loads((project.root / ".cache/v/entry/value").read_text())
            lastfailed = json.loads((project.root / ".cache/v/cache/lastfailed").read_text())
            assert lastfailed == {"test_cache.py::test_failure": True}, lastfailed
            cases = benchmark.adoption.junit_records(
                destination / "junit.xml",
                project.root,
                project.root / "base",
            )
            observed_runs.append((cached, lastfailed, cases))
        observations.append(observed_runs)
    assert observations[0] == observations[1]
