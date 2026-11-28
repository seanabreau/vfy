"""The installed CodSpeed fixture's unmeasured behavior under both engines."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


def _has_reference() -> bool:
    try:
        version = importlib.metadata.version("pytest-codspeed")
    except importlib.metadata.PackageNotFoundError:
        return False
    return SpecifierSet(">=4.3").contains(version, prereleases=True)


pytestmark = [
    requires_native_pytest,
    pytest.mark.skipif(
        not _has_reference(),
        reason="stock comparison requires the mode options added in pytest-codspeed 4.3",
    ),
]


SOURCE = """\
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import pytest
import pytest_codspeed
from pytest_codspeed import BenchmarkFixture


def event(value):
    with Path("events.jsonl").open("a") as output:
        output.write(json.dumps(value) + "\\n")


@pytest.mark.codspeed_benchmark(group="fixture")
def test_marked():
    event("marked")


def test_benchmark(benchmark):
    assert importlib.util.find_spec("pytest_codspeed") is not None
    assert pytest_codspeed.plugin.BenchmarkFixture is BenchmarkFixture
    assert importlib.metadata.version("pytest-codspeed") == pytest_codspeed.__version__
    assert isinstance(benchmark, BenchmarkFixture)
    event(["result", benchmark(lambda x: x + 1, 2)])
    with pytest.raises(RuntimeError, match="only be used once"):
        benchmark(lambda: None)


def test_pedantic(codspeed_benchmark):
    def setup():
        event("setup")
        return ((4,), {"y": 5})
    def target(x, y):
        event(["target", x, y])
        return x + y
    def teardown(x, y):
        event(["teardown", x, y])
    event(["result", codspeed_benchmark.pedantic(
        target, setup=setup, teardown=teardown, rounds=0
    )])


def test_pedantic_failure(codspeed_benchmark):
    def fail():
        event("failure")
        raise ValueError("target")
    with pytest.raises(ValueError, match="target"):
        codspeed_benchmark.pedantic(fail, teardown=lambda: event("unexpected teardown"))


def test_pedantic_invalid(codspeed_benchmark):
    with pytest.raises(ValueError, match="iterations must be positive"):
        codspeed_benchmark.pedantic(lambda: None, iterations=0)
    with pytest.raises(RuntimeError, match="only be used once"):
        codspeed_benchmark(lambda: None)


@pytest.mark.parametrize("kwargs, message", [
    ({"rounds": -1}, "rounds must be positive"),
    ({"warmup_rounds": -1}, "warmup_rounds must be non-negative"),
    ({"iterations": 0}, "iterations must be positive"),
    ({"iterations": 2, "setup": lambda: None}, "setup cannot be used with multiple iterations"),
])
def test_pedantic_validation(codspeed_benchmark, kwargs, message):
    with pytest.raises(ValueError, match=message):
        codspeed_benchmark.pedantic(lambda: None, **kwargs)


def test_setup_with_arguments(codspeed_benchmark):
    def setup(x, y): event(["setup-arguments", x, y])
    def target(x, y): event(["target-arguments", x, y]); return x + y
    def teardown(x, y): event(["teardown-arguments", x, y])
    event(["result", codspeed_benchmark.pedantic(
        target, args=(2,), kwargs={"y": 3}, setup=setup, teardown=teardown
    )])


def test_setup_cannot_return_arguments(codspeed_benchmark):
    with pytest.raises(ValueError, match="setup cannot return a value when args are provided"):
        codspeed_benchmark.pedantic(lambda x: x, args=(1,), setup=lambda x: ((x,), {}))


def test_options(request):
    event([request.config.getoption(name) for name in (
        "--codspeed-mode", "--codspeed-warmup-time", "--codspeed-max-time", "--codspeed-max-rounds"
    )])


def test_skip():
    pytest.skip("expected skip")
"""


@pytest.mark.skipif(
    importlib.util.find_spec("pytest_benchmark") is not None,
    reason="requires CodSpeed's benchmark alias without pytest-benchmark",
)
@pytest.mark.parametrize("mode", ["simulation", "memory", "walltime"])
def test_inactive_codspeed_matches_installed_plugin(tmp_path: Path, mode: str) -> None:
    projects = [VfyProject(tmp_path / name) for name in ("pytest", "vfy")]
    for project in projects:
        project.write("test_sample.py", SOURCE)
    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None, "CODSPEED_ENV": None}
    options = (
        f"--codspeed-mode={mode}",
        "--codspeed-warmup-time=1.25",
        "--codspeed-max-time=2.5",
        "--codspeed-max-rounds=3",
    )
    stock = projects[0].run_pytest("-q", *options, load_xdist=False, env=env)
    actual = projects[1].run_vfy("test", "--engine=native", "--no-cov", "-q", *options, env=env)
    for result in (stock, actual):
        result.expect(0)
        assert "12 passed, 1 skipped" in result.stdout
    observed = [
        [json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()]
        for project in projects
    ]
    assert observed[0] == observed[1]


@pytest.mark.skipif(
    importlib.util.find_spec("pytest_benchmark") is None,
    reason="requires pytest-benchmark alongside CodSpeed",
)
def test_inactive_codspeed_preserves_other_benchmark_fixture(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_sample.py",
            """import json
from pathlib import Path
from pytest_codspeed import BenchmarkFixture
from pytest_benchmark.fixture import BenchmarkFixture as OtherBenchmarkFixture

def test_benchmarks(benchmark, codspeed_benchmark):
    assert isinstance(benchmark, OtherBenchmarkFixture)
    assert not isinstance(benchmark, BenchmarkFixture)
    assert isinstance(codspeed_benchmark, BenchmarkFixture)
    assert codspeed_benchmark.__class__ is OtherBenchmarkFixture
    codspeed_benchmark.extra_info['owner'] = 'codspeed'
    events = []
    result = benchmark(lambda: events.append('benchmark') or 3)
    def setup():
        events.append('setup')
        return ((4,), {})
    def target(value):
        events.append('target')
        return value + 1
    def teardown(value):
        events.append(['teardown', value])
    other = codspeed_benchmark.pedantic(target, setup=setup, teardown=teardown)
    Path('events.json').write_text(json.dumps([
        events, result, other, codspeed_benchmark.extra_info]))
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None, "CODSPEED_ENV": None}
        arguments = ("-q", "--benchmark-disable")
        result = (
            project.run_pytest(*arguments, load_xdist=False, env=env)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", *arguments, env=env)
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0] == [
        ["benchmark", "setup", "target", ["teardown", 4]],
        3,
        5,
        {"owner": "codspeed"},
    ]
    assert observations[1] == observations[0]


@pytest.mark.parametrize("activation", ["flag", "environment", "empty_environment"])
def test_measurement_is_rejected_before_running_tests(tmp_path: Path, activation: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_sample.py", "from pathlib import Path\ndef test_run(): Path('ran').touch()\n"
    )
    args = ("--codspeed",) if activation == "flag" else ()
    env = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
        "CODSPEED_ENV": None
        if activation == "flag"
        else ("" if activation == "empty_environment" else "1"),
    }
    result = project.run_vfy("test", "--engine=native", "--no-cov", *args, env=env)
    assert result.returncode != 0
    assert "--engine=pytest" in result.stdout + result.stderr
    assert not (project.root / "ran").exists()


@pytest.mark.parametrize("remove_submodule", [False, True])
def test_preloaded_package_is_rejected_without_rebinding(
    tmp_path: Path, remove_submodule: bool
) -> None:
    project = VfyProject(tmp_path)
    startup = project.write(
        "startup/sitecustomize.py",
        f"""\
import atexit
from pathlib import Path
import sys
import pytest_codspeed

sentinel = object()
pytest_codspeed.BenchmarkFixture = sentinel
if {remove_submodule!r}:
    sys.modules.pop("pytest_codspeed.plugin", None)
plugin = sys.modules.get("pytest_codspeed.plugin")

@atexit.register
def check_binding():
    preserved = (pytest_codspeed.BenchmarkFixture is sentinel
                 and sys.modules.get("pytest_codspeed.plugin") is plugin)
    if preserved:
        Path("preloaded-preserved").touch()
""",
    )
    project.write(
        "test_sample.py", "from pathlib import Path\ndef test_run(): Path('ran').touch()\n"
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        env={
            "PYTHONPATH": str(startup.parent),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None,
            "CODSPEED_ENV": None,
        },
    )
    assert result.returncode != 0
    assert "pytest_codspeed is already imported" in result.stdout + result.stderr
    assert (tmp_path / "preloaded-preserved").exists()
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize("feature", ["simple", "tuning", "pedantic"])
def test_missing_upstream_config_is_checked_when_used(tmp_path: Path, feature: str) -> None:
    project = VfyProject(tmp_path)
    startup = project.write(
        "startup/sitecustomize.py",
        """import sys

class MissingConfig:
    def find_spec(self, fullname, *args):
        if fullname == "pytest_codspeed.config":
            raise ModuleNotFoundError(fullname, name=fullname)

sys.meta_path.insert(0, MissingConfig())
""",
    )
    call = ".pedantic" if feature == "pedantic" else ""
    project.write(
        "test_sample.py",
        "from pathlib import Path\n"
        "from pytest_codspeed import BenchmarkFixture\n\n"
        "def test_body(codspeed_benchmark):\n"
        "    Path('entered').touch()\n"
        "    assert isinstance(codspeed_benchmark, BenchmarkFixture)\n"
        f"    assert codspeed_benchmark{call}(lambda: 123) == 123\n",
    )
    args = ("--codspeed-max-rounds=3",) if feature == "tuning" else ()
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-p",
        "codspeed",
        *args,
        env={"PYTHONPATH": str(startup.parent), "CODSPEED_ENV": None},
    )
    if feature == "simple":
        result.expect(0)
    else:
        output = result.stdout + result.stderr
        assert result.returncode != 0, output
        assert "--engine=pytest" in output
        expected = (
            "--codspeed-max-rounds requires the installed pytest-codspeed"
            if feature == "tuning"
            else "CodSpeed BenchmarkFixture.pedantic requires the installed pytest-codspeed"
        )
        assert expected in output
    assert (tmp_path / "entered").exists() is (feature != "tuning")


def test_repeated_sessions_restore_public_config_namespace(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "import importlib\n"
        "import sys\n"
        "def test_package():\n"
        "    package = importlib.import_module('pytest_codspeed')\n"
        "    config = importlib.import_module('pytest_codspeed.config')\n"
        "    assert package.config is config\n"
        "    assert package.plugin is sys.modules['pytest_codspeed.plugin']\n",
    )
    project.write(
        "sessions.py",
        "import sys\n"
        "from vfy.independent.entry import main\n"
        "for _ in range(2):\n"
        "    assert main(['-q', '-p', 'codspeed', 'test_body.py']) == 0\n"
        "    assert 'pytest_codspeed' not in sys.modules\n"
        "    assert 'pytest_codspeed.plugin' not in sys.modules\n",
    )
    result = project.run_python("sessions.py")
    result.expect(0)
    assert result.stdout.count("1 passed") == 2
