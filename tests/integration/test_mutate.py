"""End-to-end source mutation tests."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from tests.support.platforms import requires_independent_mutation
from tests.support.vfy_project import CommandResult, VfyProject

requires_native_execution = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="native mutation execution requires Linux or macOS",
)


@requires_independent_mutation
@pytest.mark.parametrize("namespace", [False, True])
def test_dotted_mutation_source_resolves_without_importing_parent(
    tmp_path: Path, *, namespace: bool
) -> None:
    project = VfyProject(tmp_path)
    if not namespace:
        project.write(
            "pkg/__init__.py", "from pathlib import Path\nassert Path('conftest-loaded').exists()\n"
        )
    project.write("pkg/child.py", "def value():\n    return 1\n")
    project.write("conftest.py", "from pathlib import Path\nPath('conftest-loaded').touch()\n")
    project.write(
        "tests/test_child.py",
        "from pkg.child import value\n\ndef test_value():\n    assert value() == 1\n",
    )

    completed = project.run_vfy(
        "mutate",
        "--engine=native",
        "--all",
        "--source",
        "pkg.child",
        "--operator",
        "integer_plus_one",
        "--max-mutations",
        "1",
        "--format",
        "json",
        "-q",
    )

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert Path(report["mutations"][0]["path"]).relative_to(project.root).as_posix() == (
        "pkg/child.py"
    )


@requires_native_execution
@pytest.mark.parametrize(
    "selection",
    [
        "file",
        "node",
        "attached-keyword",
        "grouped-keyword",
        "attached-marker",
        "grouped-marker",
    ],
)
def test_scoped_mutation_collects_a_passing_baseline_without_publishing_a_full_run(
    tmp_path: Path, selection: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    marker = "import pytest\n@pytest.mark.focus\n" if selection.endswith("marker") else ""
    project.write(
        "tests/test_app.py",
        f"from app import value\n\n{marker}def test_value():\n    assert value() == 1\n",
    )
    project.write("tests/test_other.py", "def test_other():\n    assert False\n")
    if selection == "node":
        targets = ("tests/test_app.py::test_value",)
    elif selection.endswith("keyword"):
        targets = ("-qktest_value" if selection.startswith("grouped") else "-ktest_value",)
    elif selection.endswith("marker"):
        project.write("pytest.ini", "[pytest]\nmarkers = focus\n")
        targets = ("-qmfocus" if selection.startswith("grouped") else "-mfocus",)
    else:
        targets = ("tests/test_app.py",)

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "-q",
        *targets,
        env=_environment(),
    )

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["mutations"][0]["killer"] == "tests/test_app.py::test_value"
    assert (tmp_path / ".vfy/mutation-baseline.json").is_file()
    assert not (tmp_path / ".vfy/test-run.json").exists()


@requires_native_execution
def test_configured_selectors_define_the_mutation_baseline_suite(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    project.write("tests/test_other.py", "def test_other():\n    assert False\n")
    project.write("pytest.ini", "[pytest]\naddopts = -k test_value\n")

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "-q",
        env=_environment(),
    )

    completed.expect(0)
    assert json.loads(completed.stdout)["summary"] == {"protected": 1}
    # Selectors in configured addopts define the suite, so its passing run is
    # the passing baseline that `vfy test` selects from.
    assert (tmp_path / ".vfy/test-run.json").is_file()


@requires_native_execution
def test_changing_mutation_test_scope_refreshes_baseline_and_cached_results(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py",
        "from app import value\n\n"
        "def test_exact():\n    assert value() == 1\n\n"
        "def test_range():\n    assert value() in (1, 2)\n",
    )
    arguments = (
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--fail-on-survivor",
        "--format",
        "json",
        "-q",
    )
    exact = project.run_vfy(*arguments, "tests/test_app.py::test_exact", env=_environment())
    exact.expect(0)
    assert json.loads(exact.stdout)["summary"] == {"protected": 1}

    ranged = project.run_vfy(*arguments, "tests/test_app.py::test_range", env=_environment())
    ranged.expect(2)
    report = json.loads(ranged.stdout)
    assert report["summary"] == {"survived": 1}
    assert report["metrics"]["cache_hits"] == 0
    assert report["mutations"][0]["tests"] == ["tests/test_app.py::test_range"]


@requires_native_execution
@pytest.mark.skipif(
    version("pytest") != "9.1.1", reason="argument-file guard uses pinned pytest 9.1.1"
)
@pytest.mark.parametrize(
    ("arguments", "diagnostic"),
    [
        ("-n\n2\n", "controls mutation workers"),
        ("--continue-on-collection-errors\n", "requires successful collection"),
    ],
)
def test_argument_file_mutation_options_are_rejected_before_evidence_or_collection(
    tmp_path: Path, arguments: str, diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value(): return 1\n")
    project.write("tests/test_app.py", "raise AssertionError('must not collect')\n")
    project.write("args.txt", arguments)
    prior_coverage = "previous coverage evidence\n"
    project.write(".coverage", prior_coverage)
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "-q",
        "@args.txt",
        env=_environment(),
    )
    result.expect(1)
    assert diagnostic in result.stderr
    assert "must not collect" not in result.stdout + result.stderr
    assert (tmp_path / ".coverage").read_text(encoding="utf-8") == prior_coverage
    assert not (tmp_path / ".vfy").exists()


def test_mutation_module_imports_without_native_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "probe.py",
        "import vfy.native\n"
        "if hasattr(vfy.native, '_pytest'):\n"
        "    del vfy.native._pytest\n"
        "import vfy.mutate\n"
        "assert callable(vfy.mutate.run_mutation)\n",
    )
    completed = project.run_python("probe.py")
    completed.expect(0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native execution rejection")
def test_windows_mutation_rejects_execution_without_replacing_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write("tests/test_app.py", "raise AssertionError('must not collect')\n")
    project.write(".coverage", "previous coverage")
    completed = project.run_vfy("mutate", "--engine=pytest-native", "--all", "--source", "app.py")
    assert completed.returncode == 1
    assert "requires native execution" in completed.stderr
    assert "must not collect" not in completed.stdout + completed.stderr
    assert (tmp_path / ".coverage").read_text() == "previous coverage"


@requires_native_execution
def test_configured_continue_on_collection_errors_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write("pytest.ini", "[pytest]\naddopts = --continue-on-collection-errors\n")
    project.write(
        "tests/test_app.py",
        "from pathlib import Path\nimport pytest\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def record_fixture_execution():\n"
        "    Path('fixture-executed').touch()\n\n"
        "def test_value():\n"
        "    Path('test-executed').touch()\n"
        "    assert True\n",
    )
    prior_coverage = "previous coverage evidence\n"
    project.write(".coverage", prior_coverage)

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--format",
        "json",
        env=_environment(),
    )

    completed.expect(1)
    assert "--continue-on-collection-errors" in completed.stderr
    assert "requires successful collection" in completed.stderr
    assert not (tmp_path / "fixture-executed").exists()
    assert not (tmp_path / "test-executed").exists()
    assert (tmp_path / ".coverage").read_text(encoding="utf-8") == prior_coverage


@requires_native_execution
def test_parallel_collected_and_portable_mutations_preserve_outcomes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import _pytest.runner\n\n"
        "def pytest_configure(config):\n"
        "    def forbidden(*args, **kwargs):\n"
        "        raise AssertionError('stock pytest execution was used')\n"
        "    _pytest.runner.runtestprotocol = forbidden\n",
    )
    source = "\n".join(f"def value_{index}():\n    return {index + 1}\n" for index in range(3))
    project.write("app.py", source)
    project.write(
        "tests/test_app.py",
        "import app\n"
        + "\n".join(
            f"def test_value_{index}():\n    assert app.value_{index}() == {index + 1}\n"
            for index in range(3)
        ),
    )
    options = (
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--workers",
        "2",
        "--format",
        "json",
    )
    reports = []
    for engine in ("auto", "portable"):
        completed = project.run_vfy(*options, env={**_environment(), "VFY_MUTATION_ENGINE": engine})
        completed.expect(0)
        reports.append(json.loads(completed.stdout))
    for engine, report in zip(("auto", "portable"), reports, strict=True):
        for mutation in report["mutations"]:
            assert mutation["tests"] == ([mutation["killer"]] if engine == "auto" else [])
    fields = ("id", "status", "killer")
    assert [{key: item[key] for key in fields} for item in reports[0]["mutations"]] == [
        {key: item[key] for key in fields} for item in reports[1]["mutations"]
    ]
    assert reports[0]["summary"] == {"protected": 3}
    if os.name == "posix":
        assert reports[0]["metrics"]["fast_results"] == 3


@pytest.mark.skipif(os.name != "posix", reason="mutation rendezvous requires POSIX FIFOs")
def test_portable_and_collected_mutations_share_worker_slots(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value(argument=1):\n    return argument + 1\n")
    os.mkfifo(tmp_path / "mutation-handshake")
    project.write(
        "tests/test_app.py",
        "from pathlib import Path\nimport app\n\n"
        "def test_value():\n"
        "    handshake = Path('mutation-handshake')\n"
        "    if app.value.__defaults__ == (2,):\n"
        "        with handshake.open('wb', buffering=0) as stream:\n"
        "            stream.write(b'ready')\n"
        "    elif app.value() == 3:\n"
        "        with handshake.open('rb', buffering=0) as stream:\n"
        "            assert stream.read(5) == b'ready'\n"
        "    assert app.value() == 2\n",
    )
    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--workers",
        "2",
        "--timeout",
        "5",
        "--format",
        "json",
        env=_environment(),
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 2}
    assert report["metrics"]["portable_fallbacks"] == 1
    assert report["metrics"]["fast_results"] == 1


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup requires POSIX")
@pytest.mark.parametrize("test_isolation", ["serial", "file"])
def test_portable_mutation_timeout_releases_descendant_processes(
    tmp_path: Path, test_isolation: str
) -> None:
    import fcntl  # noqa: PLC0415 - the skipped Windows test cannot import this POSIX module

    project = VfyProject(tmp_path, timeout=30)
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    project.write(
        "descendant.py",
        "import fcntl, os, signal\n"
        "with open('descendant.lock', 'w') as stream:\n"
        "    fcntl.flock(stream, fcntl.LOCK_EX)\n"
        "    stream.write(str(os.getpid()))\n"
        "    stream.flush()\n"
        "    print('ready', flush=True)\n"
        "    signal.pause()\n",
    )
    project.write(
        "tests/test_app.py",
        "import signal, subprocess, sys\nimport app\n\n"
        "def test_value():\n"
        "    if app.value() == 2:\n"
        "        child = subprocess.Popen([sys.executable, 'descendant.py'], "
        "stdout=subprocess.PIPE, text=True)\n"
        "        assert child.stdout.readline().strip() == 'ready'\n"
        "        signal.pause()\n"
        "    assert app.value() == 1\n",
    )
    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--workers",
        "2",
        "--timeout",
        "2",
        "--test-isolation",
        test_isolation,
        "--format",
        "json",
        env=_environment(),
    )
    lock_path = tmp_path / "descendant.lock"
    try:
        completed.expect(1)
        report = json.loads(completed.stdout)
        assert report["summary"] == {"timeout": 1}
        assert report["mutations"][0]["killer"] is None
        with lock_path.open() as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if lock_path.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(lock_path.read_text()), signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="collected mutation children require fork")
def test_failed_mutation_fork_closes_pipes_and_reports_a_retryable_error(tmp_path: Path) -> None:
    source, _ = _project(
        tmp_path,
        "def value():\n    return 1\n",
        "from app import value\ndef test_value():\n    assert value() == 1\n",
    )
    baseline = _collect_baseline(tmp_path, source)
    baseline.expect(0)
    project = VfyProject(tmp_path)
    project.write(
        "run.py",
        "import os\nfrom vfy.cli import main\n"
        "original_pipe, original_fork = os.pipe, os.fork\npipes = []\n"
        "def pipe():\n    pair = original_pipe()\n    pipes.extend(pair)\n    return pair\n"
        "def fork():\n    raise OSError('fork failed')\n"
        "arguments = ['mutate', '--engine=pytest-native', '--all', '--source', 'app.py', "
        "'--operator', 'integer_plus_one', '--no-cache', '--format', 'json']\n"
        "os.pipe, os.fork = pipe, fork\n"
        "try:\n    status = main(arguments)\n"
        "finally:\n    os.pipe, os.fork = original_pipe, original_fork\n"
        "assert status == 1\nassert pipes\n"
        "for fd in pipes:\n"
        "    try:\n        os.fstat(fd)\n"
        "    except OSError:\n        pass\n"
        "    else:\n        raise AssertionError('pipe leaked after failed fork')\n"
        "assert main(arguments) == 0\n",
    )
    completed = project.run_python("run.py", env=_environment())
    completed.expect(0)


@pytest.mark.skipif(os.name != "posix", reason="collected mutation children require fork")
def test_collected_mutations_drain_large_child_results(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_app.py",
        "import pytest\nfrom app import value\n"
        "@pytest.mark.parametrize('argument', [1], ids=['x' * 262144])\n"
        "def test_value(argument):\n    assert value() == argument\n",
    )
    project.write(
        "run.py",
        "import signal\nfrom vfy.cli import main\n"
        "def interrupt(signum, frame):\n    raise KeyboardInterrupt\n"
        "signal.signal(signal.SIGALRM, interrupt)\nsignal.alarm(15)\n"
        "try:\n"
        "    status = main(['mutate', '--engine=pytest-native', '--all', '--source', 'app.py', "
        "'--operator', 'integer_plus_one', '--no-cache', '--format', 'json'])\n"
        "finally:\n    signal.alarm(0)\n"
        "raise SystemExit(status)\n",
    )
    completed = project.run_python("run.py", env=_environment())
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["fast_results"] == 1
    assert report["mutations"][0]["killer"] == "tests/test_app.py::test_value[" + "x" * 262144 + "]"


def _environment() -> dict[str, str]:
    # Disabling third-party pytest plug-ins keeps the child result about the
    # mutation, not an installed coverage plug-in. VfyProject adds the
    # inherited environment around this overlay.
    return {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def _project(tmp_path: Path, source: str, tests: str) -> tuple[Path, Path]:
    project = VfyProject(tmp_path)
    source_path = tmp_path / "app.py"
    tests_path = tmp_path / "tests" / "test_app.py"
    project.write(source_path.name, source)
    project.write(tests_path.relative_to(tmp_path), tests)
    return source_path, tests_path


def _collect_baseline(tmp_path: Path, source_path: Path) -> CommandResult:
    return VfyProject(tmp_path).run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--source",
        source_path.name,
        "-q",
        env=_environment(),
    )


def _mutation(
    tmp_path: Path,
    *options: str,
    env: dict[str, str | None] | None = None,
) -> CommandResult:
    return VfyProject(tmp_path).run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--format",
        "json",
        "--timeout",
        "10",
        *options,
        env=_environment() if env is None else env,
    )


def _project_with_baseline_counter(tmp_path: Path) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 5\n")
    project.write(
        "tests/test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 5\n"
    )
    project.write(
        "conftest.py",
        "import os\nfrom pathlib import Path\n\n"
        "def pytest_sessionfinish(session):\n"
        "    if not hasattr(session.config, 'workerinput') and "
        "os.environ.get('VFY_TEST_RESULTS'):\n"
        "        path = Path('.vfy/baseline-runs')\n"
        "        path.parent.mkdir(exist_ok=True)\n"
        "        with path.open('a') as stream:\n"
        "            stream.write('run\\n')\n",
    )
    project.git_init()
    return project


@requires_native_execution
def test_reported_mutation_spans_are_byte_accurate_and_stable(tmp_path: Path) -> None:
    text = (
        "def choose(value):\n"
        "    return '✓' if value else None\n\n"
        "def invert(value):\n"
        "    return not (value)\n"
    )
    source_path, _ = _project(
        tmp_path,
        text,
        "from app import choose, invert\n\n"
        "def test_choose():\n    assert choose(1) == '✓'\n    assert choose(0) is None\n\n"
        "def test_invert():\n    assert invert(0) is True\n    assert invert(1) is False\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0

    first = _mutation(tmp_path, "--no-cache")
    second = _mutation(tmp_path, "--no-cache")

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    mutations = json.loads(first.stdout)["mutations"]
    assert mutations
    assert [m["id"] for m in mutations] == [m["id"] for m in json.loads(second.stdout)["mutations"]]
    assert len({m["id"] for m in mutations}) == len(mutations)
    source_bytes = source_path.read_bytes()
    for mutation in mutations:
        assert source_bytes[mutation["start"] : mutation["end"]] == mutation["original"].encode()
        assert mutation["line"] >= 1
        assert mutation["column"] >= 0
        # A replacement that does not compile is reported as an error.
        assert mutation["status"] != "error", mutation["detail"]


@requires_native_execution
@pytest.mark.parametrize("original", ["✓", "e\u0301", "\u00a0", "\u200b", "\ue000", "\u0378"])
def test_survivor_output_keeps_non_ascii_source_text(tmp_path: Path, original: str) -> None:
    source_path, _ = _project(
        tmp_path,
        f"def mark():\n    return {original!r}\n",
        "from app import mark\n\ndef test_mark():\n    mark()\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    arguments = ("--operator", "return_none", "--no-cache", "--fail-on-survivor")

    structured = _mutation(tmp_path, *arguments)
    pretty = _mutation(tmp_path, *arguments, "--pretty-print")
    text = VfyProject(tmp_path).run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--timeout",
        "10",
        *arguments,
        env=_environment(),
    )

    assert structured.returncode == pretty.returncode == text.returncode == 2
    report = json.loads(structured.stdout)
    assert report["summary"] == {"survived": 1}
    assert report["mutations"][0]["original"] == repr(original)
    assert [m["original"] for m in json.loads(pretty.stdout)["mutations"]] == [
        m["original"] for m in report["mutations"]
    ]
    assert pretty.stdout.startswith("{\n")
    assert f"diff={repr(original)!r} -> 'None'" in text.stdout


@requires_native_execution
def test_protected_mutation_reports_the_failing_test_and_preserves_source(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def classify(value):\n    if value >= 2:\n        return 'high'\n    return 'low'\n",
        "from app import classify\n\n"
        "def test_high():\n    assert classify(2) == 'high'\n\n"
        "def test_low():\n    assert classify(1) == 'low'\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    baseline.expect(0)
    original = source_path.read_text(encoding="utf-8")

    completed = _mutation(tmp_path, "--operator", "comparison_boundary")

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["cache_hits"] == 0
    result = report["mutations"][0]
    assert result["status"] == "protected"
    assert result["killer"] == "tests/test_app.py::test_high"
    assert result["phase"] == "call"
    assert source_path.read_text(encoding="utf-8") == original
    cache = json.loads((tmp_path / ".vfy" / "mutations.json").read_text())
    assert cache["schema"] == "vfy.mutation"
    assert "version" not in report
    assert "version" not in cache

    second = _mutation(tmp_path, "--operator", "comparison_boundary")
    second.expect(0)
    second_report = json.loads(second.stdout)
    assert second_report["summary"] == {"protected": 1}
    assert second_report["metrics"]["cache_hits"] == 1
    assert second_report["mutations"][0]["phase"] == "call"


@requires_native_execution
@pytest.mark.parametrize(
    ("owner", "engine"), [("plain", "auto"), ("plain", "portable"), ("decorated", "auto")]
)
def test_changing_parametrize_ids_run_current_tests_and_invalidate_mutation_cache(
    tmp_path: Path, owner: str, engine: str
) -> None:
    # A process-specific ID changes between the context baseline, collected
    # runner, and fresh mutation process.
    decorator = "def passthrough(function):\n    return lambda value: function(value)\n\n"
    source_path, _ = _project(
        tmp_path,
        (decorator + "@passthrough\n" if owner == "decorated" else "")
        + "def classify(value):\n    return value >= 2\n",
        "import os\nimport pytest\nfrom app import classify\n\n"
        "@pytest.mark.parametrize(\n"
        "    'value', [int(os.environ.get('VFY_CASE', '2'))], ids=[str(os.getpid())]\n"
        ")\n"
        "def test_high(value):\n    assert classify(value) is True\n",
    )
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import os\n"
        "if (os.getenv('VFY_MUTATION_ENGINE') == 'portable'\n"
        "    and os.getenv('VFY_TEST_MUTATION_CONTROLLER_PID') == str(os.getpid())):\n"
        "    raise AssertionError('portable mutation collected in controller')\n",
    )
    arguments = (
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--format",
        "json",
        "--timeout",
        "10",
        "--operator",
        "comparison_boundary",
    )
    project.write(
        "driver.py",
        "import os\nfrom vfy.cli import main\n"
        "os.environ['VFY_TEST_MUTATION_CONTROLLER_PID'] = str(os.getpid())\n"
        f"raise SystemExit(main({list(arguments)!r}))\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0

    completed = project.run_python(
        "driver.py",
        env={**_environment(), "VFY_CASE": "2", "VFY_MUTATION_ENGINE": engine},
    )

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    mutation = report["mutations"][0]
    assert mutation["killer"].startswith("tests/test_app.py::test_high[")
    assert mutation["tests"] == (
        [mutation["killer"]] if owner == "plain" and engine == "auto" else []
    )
    if engine == "auto":
        assert "pytest did not collect 1 test IDs" in completed.stderr
    else:
        assert "pytest did not collect" not in completed.stderr

    # The generated ID also changes when a later collection uses a different
    # parameter value. That run must execute the mutation instead of reusing a
    # protected cache entry for the old parameter.
    changed = project.run_python(
        "driver.py",
        env={**_environment(), "VFY_CASE": "3", "VFY_MUTATION_ENGINE": engine},
    )
    changed.expect(0)
    changed_report = json.loads(changed.stdout)
    assert changed_report["summary"] == {"survived": 1}
    assert changed_report["metrics"]["cache_hits"] == 0
    if engine == "auto":
        assert "pytest did not collect 1 test IDs" in changed.stderr
    else:
        assert "pytest did not collect" not in changed.stderr


@requires_native_execution
def test_cache_is_invalidated_when_test_source_changes(tmp_path: Path) -> None:
    source_path, tests_path = _project(
        tmp_path,
        "def classify(value):\n    if value >= 2:\n        return 'high'\n    return 'low'\n",
        "from app import classify\n\ndef test_high():\n    assert classify(2) == 'high'\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    baseline.expect(0)
    first = _mutation(tmp_path, "--operator", "comparison_boundary")
    assert json.loads(first.stdout)["summary"] == {"protected": 1}

    tests_path.write_text(
        "from app import classify\n\ndef test_high():\n    classify(2)\n",
        encoding="utf-8",
    )
    second = _mutation(tmp_path, "--operator", "comparison_boundary")
    second.expect(0)
    assert json.loads(second.stdout)["summary"] == {"survived": 1}


@requires_native_execution
def test_mutant_collection_error_is_not_reported_as_protection(tmp_path: Path) -> None:
    _project(
        tmp_path,
        "VALUE = 1\nif VALUE != 1:\n    raise RuntimeError('mutant cannot import')\n"
        "def value():\n    return VALUE\n",
        "import app\n\ndef test_value():\n    assert app.value() == 1\n",
    )
    completed = _mutation(
        tmp_path, "--operator", "integer_plus_one", "--max-mutations", "1", "--no-cache"
    )
    completed.expect(1)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"error": 1}
    assert report["mutations"][0]["killer"] is None
    assert "mutant cannot import" in report["mutations"][0]["detail"]


@requires_native_execution
def test_existing_failure_cannot_be_reported_as_mutation_protection(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def classify(value):\n    if value >= 2:\n        return 'high'\n    return 'low'\n",
        "from app import classify\n\ndef test_high():\n    assert classify(2) == 'wrong'\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    assert baseline.returncode != 0
    assert not (tmp_path / ".vfy/replay.coverage").exists()
    assert not (tmp_path / ".vfy/test-run.json").exists()
    assert not (tmp_path / ".vfy/mutation-baseline.json").exists()

    completed = _mutation(
        tmp_path,
        "--operator",
        "comparison_boundary",
        "--no-cache",
        "--fail-on-survivor",
    )
    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["summary"] == {"unstable": 1}
    assert "without the mutation" in report["mutations"][0]["detail"]


@requires_native_execution
def test_failure_in_a_test_without_source_context_invalidates_baseline(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def value():\n    return 5\n",
        "def test_unrelated_failure():\n    assert False\n\n"
        "def test_value_is_called():\n"
        "    from app import value\n"
        "    assert value() == 5\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    assert baseline.returncode != 0

    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--no-cache",
    )
    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["summary"] == {"unstable": 1}
    assert "without the mutation" in report["mutations"][0]["detail"]


@requires_native_execution
def test_escalated_existing_failure_cannot_be_reported_as_protection(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def value():\n    return 5\n\ndef unrelated():\n    return 'ok'\n",
        "from app import value\n\n"
        "def test_value_is_called():\n    value()\n\n"
        "def test_existing_failure():\n    from app import unrelated\n\n"
        "    unrelated()\n    assert False\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    assert baseline.returncode != 0

    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--fail-on-survivor",
    )
    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    assert report["summary"] == {"unstable": 1}


@requires_native_execution
def test_timeout_is_an_incomplete_mutation_result(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def value():\n    return 5\n",
        "import time\n\n"
        "from app import value\n\n"
        "def test_value_is_called():\n    time.sleep(1)\n    value()\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    baseline.expect(0)

    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--timeout",
        "0.05",
        "--no-cache",
        "--fail-on-survivor",
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["summary"] == {"timeout": 1}


@pytest.mark.skipif(os.name != "posix", reason="collected mutation children require fork")
def test_collected_mutation_preserves_large_parameterized_killer(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def value():\n    return 5\n",
        "import pytest\nfrom app import value\n"
        "@pytest.mark.parametrize('unused', [1], ids=['x' * (1 << 19)])\n"
        "def test_value(unused):\n    assert value() == 5\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    baseline.expect(0)
    command = VfyProject(tmp_path).start_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--format",
        "json",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "-j",
        "1",
        env={**_environment(), "VFY_MUTATION_ENGINE": "auto"},
    )
    try:
        completed = command.wait(timeout=20)
    finally:
        if command.poll() is None:
            command.send_signal(signal.SIGINT)
            try:
                command.wait(timeout=5)
            except subprocess.TimeoutExpired:
                command.kill()
                command.wait(timeout=5)
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert "x" * (1 << 19) in report["mutations"][0]["killer"]
    assert report["metrics"]["fast_results"] == 1


@requires_native_execution
def test_survivor_is_a_reviewable_failure_when_requested(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def value():\n    return 5\n",
        "from app import value\n\ndef test_value_is_called():\n    value()\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    baseline.expect(0)

    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--fail-on-survivor",
    )

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report["summary"] == {"survived": 1}
    assert report["mutations"][0]["status"] == "survived"


@requires_native_execution
def test_changed_scope_without_git_uses_measured_source(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def classify(value):\n    if value >= 2:\n        return 'high'\n    return 'low'\n",
        "from app import classify\n\ndef test_high():\n    assert classify(2) == 'high'\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    baseline.expect(0)

    completed = VfyProject(tmp_path).run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--operator",
        "comparison_boundary",
        "--format",
        "json",
        "--no-cache",
        env=_environment(),
    )

    completed.expect(0)
    assert json.loads(completed.stdout)["summary"] == {"protected": 1}
    assert "Git state unavailable" in completed.stderr


@requires_native_execution
def test_collected_runner_updates_imported_aliases_nested_functions_and_methods(
    tmp_path: Path,
) -> None:
    source_path, _ = _project(
        tmp_path,
        "def outer(value):\n"
        "    def inner(item):\n"
        "        return item >= 2\n"
        "    return inner(value)\n\n"
        "class Box:\n"
        "    def value(self, item):\n"
        "        return item >= 2\n",
        "from app import Box, outer\n\n"
        "def test_outer():\n    assert outer(2) is True\n\n"
        "def test_method():\n    assert Box().value(2) is True\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "comparison_boundary",
        "--no-cache",
        "-j",
        "2",
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 2}
    assert {result["owner"]["qualname"] for result in report["mutations"]} == {
        "outer",
        "Box.value",
    }


@requires_native_execution
def test_duplicate_module_objects_use_portable_mutation_fallback(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def classify(value):\n    return value >= 2\n",
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n"
        "from app import classify\n\n"
        "spec = importlib.util.spec_from_file_location(\n"
        "    'app_alias', Path(__file__).parents[1] / 'app.py'\n"
        ")\n"
        "assert spec is not None and spec.loader is not None\n"
        "alias = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = alias\n"
        "spec.loader.exec_module(alias)\n\n"
        "def test_classify():\n    assert classify(2) is True\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "comparison_boundary",
        "--no-cache",
    )

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["portable_fallbacks"] == 1


@requires_native_execution
@pytest.mark.parametrize("warnings_as_errors", [False, True])
def test_decorated_owner_uses_portable_fallback(
    tmp_path: Path, *, warnings_as_errors: bool
) -> None:
    # toolz runs with `filterwarnings = error`; the fresh pytest process loads
    # vfy.mutation after the bootstrap imported it.
    if warnings_as_errors:
        VfyProject(tmp_path).write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
    source_path, _ = _project(
        tmp_path,
        "def passthrough(function):\n"
        "    def wrapper(*args, **kwargs):\n"
        "        return function(*args, **kwargs)\n"
        "    return wrapper\n\n"
        "@passthrough\n"
        "def classify(value):\n"
        "    return value >= 2\n",
        "from app import classify\n\ndef test_classify():\n    assert classify(2) is True\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "comparison_boundary",
        "--no-cache",
    )
    completed.expect(0)
    result = json.loads(completed.stdout)["mutations"][0]
    assert result["status"] == "protected"
    assert result["owner"]["fast_eligible"] is False


@requires_native_execution
def test_post_definition_wrapper_falls_back_after_runtime_validation(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def passthrough(function):\n"
        "    def wrapper(*args, **kwargs):\n"
        "        return function(*args, **kwargs)\n"
        "    return wrapper\n\n"
        "def classify(value):\n"
        "    return value >= 2\n\n"
        "classify = passthrough(classify)\n",
        "from app import classify\n\ndef test_classify():\n    assert classify(2) is True\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "comparison_boundary",
        "--no-cache",
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["metrics"]["portable_fallbacks"] == 1


@requires_native_execution
def test_mutation_collects_a_missing_baseline_automatically(tmp_path: Path) -> None:
    _source_path, _ = _project(
        tmp_path,
        "def classify(value):\n    return value >= 2\n",
        "from app import classify\n\ndef test_classify():\n    assert classify(2) is True\n",
    )
    completed = _mutation(
        tmp_path,
        "--operator",
        "comparison_boundary",
        "--no-cache",
    )
    completed.expect(0)
    assert (tmp_path / ".coverage").is_file()
    assert (tmp_path / ".vfy" / "mutation-baseline.json").is_file()


@requires_native_execution
def test_partial_test_run_does_not_publish_reusable_green_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 5\n")
    project.write(
        "tests/test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 5\n"
    )

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--source",
        "app.py",
        "tests/test_app.py::test_value",
        env=_environment(),
    )

    completed.expect(0)
    assert not (tmp_path / ".vfy/replay.coverage").exists()
    assert not (tmp_path / ".vfy/test-run.json").exists()
    assert not (tmp_path / ".vfy/mutation-baseline.json").exists()


@requires_native_execution
def test_mutation_collects_a_missing_baseline_once(tmp_path: Path) -> None:
    _project_with_baseline_counter(tmp_path)

    completed = _mutation(tmp_path, "--operator", "integer_plus_one", "--no-cache")

    completed.expect(0)
    assert (tmp_path / ".vfy/mutation-baseline.json").is_file()
    assert (tmp_path / ".vfy/baseline-runs").read_text().splitlines() == ["run"]


@requires_native_execution
def test_serial_mutation_reuses_a_green_serial_test_run(tmp_path: Path) -> None:
    project = _project_with_baseline_counter(tmp_path)

    baseline = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "-j",
        "1",
        "--tb=native",
        env=_environment(),
    )
    baseline.expect(0)
    assert not (tmp_path / ".vfy/mutation-baseline.json").exists()
    baseline_runs = (tmp_path / ".vfy/baseline-runs").read_text().splitlines()
    assert baseline_runs

    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--test-isolation",
        "serial",
    )

    completed.expect(0)
    assert (tmp_path / ".vfy/mutation-baseline.json").is_file()
    assert (tmp_path / ".vfy/baseline-runs").read_text().splitlines() == baseline_runs


@pytest.mark.parametrize(
    "changed_input",
    [
        "source",
        "test",
        "config",
        "environment",
        "history",
        "coverage",
        "engine",
        "native_scheduling",
    ],
)
@requires_native_execution
def test_stale_green_test_evidence_refreshes_the_mutation_baseline(
    tmp_path: Path, changed_input: str
) -> None:
    project = _project_with_baseline_counter(tmp_path)
    baseline_engine = "pytest" if changed_input == "engine" else "pytest-native"
    # The documented JSON history layout lets the test edit recorded durations.
    history = ["--history", ".vfy/test-history.json"] if changed_input == "history" else []
    baseline = project.run_vfy(
        "test",
        f"--engine={baseline_engine}",
        "--all",
        "--source",
        "app.py",
        "-j",
        "1",
        *history,
        env=_environment(),
    )
    baseline.expect(0)
    baseline_runs = len((tmp_path / ".vfy/baseline-runs").read_text().splitlines())

    environment = _environment()
    if changed_input == "source":
        project.write("app.py", "def value():\n    return 5\n# changed\n")
    elif changed_input == "test":
        project.write(
            "tests/test_app.py",
            "from app import value\n\ndef test_value():\n    assert value() == 5\n# changed\n",
        )
    elif changed_input == "config":
        project.write("pytest.ini", "[pytest]\naddopts =\n")
    elif changed_input == "environment":
        environment["PYTEST_ADDOPTS"] = "-ra"
    elif changed_input == "native_scheduling":
        environment["VFY_NATIVE_SCHEDULING"] = "cases"
    elif changed_input == "history":
        path = tmp_path / ".vfy/test-history.json"
        recorded = json.loads(path.read_text(encoding="utf-8"))
        recorded["tests"]["tests/test_app.py::test_value"]["duration_ms"] = 9999.0
        path.write_text(json.dumps(recorded), encoding="utf-8")
    elif changed_input == "coverage":
        (tmp_path / ".vfy/replay.coverage").write_bytes((tmp_path / ".coverage").read_bytes())

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "--no-cache",
        *history,
        env=environment,
    )

    completed.expect(0)
    assert len((tmp_path / ".vfy/baseline-runs").read_text().splitlines()) > baseline_runs


@requires_native_execution
@pytest.mark.skipif(
    version("pytest") != "9.1.1", reason="argument-file identity uses pinned pytest 9.1.1"
)
@pytest.mark.parametrize("evidence", ["green-test", "mutation-baseline"])
def test_external_argument_file_changes_refresh_mutation_test_contexts(
    tmp_path: Path, evidence: str
) -> None:
    project = _project_with_baseline_counter(tmp_path)
    project.write(
        "tests/alternate.py",
        "from app import value\n\ndef test_alternate():\n    assert value() == 5\n",
    )
    project.git_commit("Add alternate discovery scope")
    argument_file = tmp_path.with_name(f"{tmp_path.name}-pytest-args.txt")
    argument_file.write_text("--override-ini=python_files=test_*.py\n", encoding="utf-8")
    argument = f"@{argument_file}"
    if evidence == "green-test":
        initial = project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--all",
            "--source",
            "app.py",
            "-j",
            "1",
            "-q",
            argument,
            env=_environment(),
        )
    else:
        initial = _mutation(tmp_path, "--operator", "integer_plus_one", "-q", argument)
    initial.expect(0)
    baseline_runs = len((tmp_path / ".vfy/baseline-runs").read_text().splitlines())

    argument_file.write_text("--override-ini=python_files=alternate.py\n", encoding="utf-8")
    completed = _mutation(tmp_path, "--operator", "integer_plus_one", "-q", argument)

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}
    assert report["mutations"][0]["tests"] == ["tests/alternate.py::test_alternate"]
    assert report["mutations"][0]["killer"] == "tests/alternate.py::test_alternate"
    assert len((tmp_path / ".vfy/baseline-runs").read_text().splitlines()) > baseline_runs


@requires_native_execution
@pytest.mark.parametrize("cache_enabled", [False, True])
def test_external_config_changes_refresh_mutation_baseline_and_cache(
    tmp_path: Path, cache_enabled: bool
) -> None:
    project = _project_with_baseline_counter(tmp_path / "project")
    project.write(".gitignore", ".vfy/\n.coverage*\n.pytest_cache/\n__pycache__/\n")
    project.write(
        "tests/alternate.py",
        "from app import value\n\ndef test_alternate():\n    assert value() == 5\n",
    )
    project.git_commit("Add alternate discovery scope and ignore runtime evidence")
    config = tmp_path / "configs/external.ini"
    config.parent.mkdir()
    config.write_text("[pytest]\npython_files = test_*.py\n", encoding="utf-8")
    arguments = ["--operator", "integer_plus_one"]
    if not cache_enabled:
        arguments.append("--no-cache")
    arguments.extend(("-q", f"-c{config}"))
    marker = project.root / ".vfy/baseline-runs"

    for expected_hits in (0, int(cache_enabled)):
        completed = _mutation(project.root, *arguments)
        completed.expect(0)
        report = json.loads(completed.stdout)
        assert report["summary"] == {"protected": 1}
        assert report["metrics"]["cache_hits"] == expected_hits
        assert marker.read_text(encoding="utf-8").splitlines() == ["run"]
        assert project.run("git", "status", "--porcelain").stdout == ""

    config.write_text("[pytest]\npython_files = alternate.py\n", encoding="utf-8")
    for expected_hits in (0, int(cache_enabled)):
        completed = _mutation(project.root, *arguments)
        completed.expect(0)
        report = json.loads(completed.stdout)
        assert report["summary"] == {"protected": 1}
        assert report["metrics"]["cache_hits"] == expected_hits
        assert report["mutations"][0]["tests"] == ["tests/alternate.py::test_alternate"]
        assert report["mutations"][0]["killer"] == "tests/alternate.py::test_alternate"
        assert marker.read_text(encoding="utf-8").splitlines() == ["run", "run"]
        assert project.run("git", "status", "--porcelain").stdout == ""
    if cache_enabled:
        cache = json.loads((project.root / ".vfy/mutations.json").read_text(encoding="utf-8"))
        assert cache["mutations"][0]["tests"] == ["tests/alternate.py::test_alternate"]
        assert cache["mutations"][0]["killer"] == "tests/alternate.py::test_alternate"


@requires_native_execution
def test_relevant_tests_are_ordered_by_recorded_duration(tmp_path: Path) -> None:
    _project(
        tmp_path,
        "def classify(value):\n    return value >= 2\n",
        "import time\n\nfrom app import classify\n\n"
        "def test_a_slow():\n    time.sleep(0.05)\n    assert classify(2) is True\n\n"
        "def test_b_fast():\n    assert classify(2) is True\n",
    )
    history = tmp_path / ".vfy" / "test-history.json"
    baseline = VfyProject(tmp_path).run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "-q",
        "--history",
        str(history),
        env=_environment(),
    )
    baseline.expect(0)

    completed = _mutation(
        tmp_path, "--operator", "comparison_boundary", "--no-cache", "--history", str(history)
    )

    completed.expect(0)
    mutation = json.loads(completed.stdout)["mutations"][0]
    assert mutation["tests"] == [
        "tests/test_app.py::test_b_fast",
        "tests/test_app.py::test_a_slow",
    ]


@requires_native_execution
def test_definition_time_mutations_run_tests_that_inspect_defaults(
    tmp_path: Path,
) -> None:
    source_path, _tests_path = _project(
        tmp_path,
        "def value(argument=1):\n    return argument\n\ndef unrelated():\n    return 3\n",
        "import app\n\n"
        "def test_unrelated():\n    assert app.unrelated() == 3\n\n"
        "def test_default_metadata():\n    assert app.value.__defaults__ == (1,)\n",
    )
    baseline = _collect_baseline(tmp_path, source_path)
    baseline.expect(0)

    reports = []
    for engine in ("auto", "portable"):
        completed = _mutation(
            tmp_path,
            "--operator",
            "integer_plus_one",
            "--max-mutations",
            "1",
            "--no-cache",
            env={**_environment(), "VFY_MUTATION_ENGINE": engine},
        )
        completed.expect(0)
        reports.append(json.loads(completed.stdout))

    fields = ("id", "status", "tests", "killer")
    assert [{key: item[key] for key in fields} for item in reports[0]["mutations"]] == [
        {key: item[key] for key in fields} for item in reports[1]["mutations"]
    ]
    result = reports[0]["mutations"][0]
    assert result["owner"]["phase"] == "definition"
    assert result["status"] == "protected"
    assert result["killer"] == "tests/test_app.py::test_default_metadata"


@requires_native_execution
def test_expected_xfail_is_not_mistaken_for_mutant_protection(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def value():\n    return 5\n",
        "import pytest\n\n"
        "from app import value\n\n"
        "@pytest.mark.xfail(reason='known contract gap')\n"
        "def test_value():\n    assert value() == 6\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--no-cache",
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"survived": 1}


@requires_native_execution
def test_strict_xpass_remains_a_real_mutant_killer(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "def value():\n    return 5\n",
        "import pytest\n\n"
        "from app import value\n\n"
        "@pytest.mark.xfail(reason='known contract gap', strict=True)\n"
        "def test_value():\n    assert value() == 6\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--no-cache",
    )
    completed.expect(0)
    result = json.loads(completed.stdout)["mutations"][0]
    assert result["status"] == "protected"
    assert result["killer"] == "tests/test_app.py::test_value"


@requires_native_execution
def test_global_writes_force_isolated_fast_execution(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "counter = 0\n\n"
        "def value():\n"
        "    global counter\n"
        "    counter += 1\n"
        "    return counter == 1\n",
        "from app import value\n\ndef test_value():\n    assert value() is True\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--operator",
        "comparison_negation",
        "--no-cache",
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 4}
    module_assignment = next(mutation for mutation in report["mutations"] if mutation["line"] == 1)
    assert module_assignment["owner"]["phase"] == "definition"
    assert module_assignment["status"] == "protected"
    assert module_assignment["killer"] == "tests/test_app.py::test_value"


@requires_native_execution
@pytest.mark.parametrize("engine", ["auto", "portable"])
def test_fixture_setup_failure_is_reported_as_the_killing_phase(
    tmp_path: Path, engine: str
) -> None:
    source_path, _ = _project(
        tmp_path,
        "def enabled():\n    return True\n",
        "import pytest\n\n"
        "from app import enabled\n\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    if not enabled():\n"
        "        raise RuntimeError('disabled')\n"
        "    yield\n\n"
        "def test_resource(resource):\n    assert resource is None\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "boolean_flip",
        "--no-cache",
        env={**_environment(), "VFY_MUTATION_ENGINE": engine},
    )
    completed.expect(0)
    result = json.loads(completed.stdout)["mutations"][0]
    assert result["status"] == "protected"
    assert result["phase"] == "setup"


@requires_native_execution
@pytest.mark.parametrize("engine", ["auto", "portable"])
def test_fixture_teardown_failure_is_reported_as_the_killing_phase(
    tmp_path: Path, engine: str
) -> None:
    source_path, _ = _project(
        tmp_path,
        "def enabled():\n    return True\n",
        "import pytest\n\n"
        "from app import enabled\n\n"
        "state = []\n\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    yield\n"
        "    assert state == [True]\n\n"
        "def test_resource(resource):\n    state.append(enabled())\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "boolean_flip",
        "--no-cache",
        env={**_environment(), "VFY_MUTATION_ENGINE": engine},
    )
    completed.expect(0)
    result = json.loads(completed.stdout)["mutations"][0]
    assert result["status"] == "protected"
    assert result["phase"] == "teardown"


@requires_native_execution
def test_async_and_generator_code_objects_use_the_fast_path(tmp_path: Path) -> None:
    source_path, _ = _project(
        tmp_path,
        "async def async_value():\n    return 5\n\ndef generated():\n    yield 5\n",
        "import asyncio\n\n"
        "from app import async_value, generated\n\n"
        "def test_async_value():\n"
        "    assert asyncio.run(async_value()) == 5\n\n"
        "def test_generated():\n"
        "    assert next(generated()) == 5\n",
    )
    assert _collect_baseline(tmp_path, source_path).returncode == 0
    completed = _mutation(
        tmp_path,
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "-j",
        "2",
    )
    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 2}
    assert all(item["owner"]["fast_eligible"] for item in report["mutations"])


@pytest.mark.parametrize("engine", ["auto", "portable"])
@pytest.mark.parametrize(
    ("tests", "expected"),
    [
        (
            "from app import first, second\n\n"
            "def test_values():\n    assert first() == 5\n    second()\n",
            {"first": "protected", "second": "survived"},
        ),
        (
            "from app import first, second\n"
            "state = ([],)\n\n"
            "def test_values():\n"
            "    state[0].append(None)\n"
            "    assert len(state[0]) == 1\n"
            "    first()\n    second()\n",
            {"first": "survived", "second": "survived"},
        ),
    ],
    ids=["independent-code", "nested-mutable-state"],
)
@requires_native_execution
def test_mutations_do_not_inherit_previous_code_or_test_state(
    tmp_path: Path, engine: str, tests: str, expected: dict[str, str]
) -> None:
    _project(tmp_path, "def first():\n    return 5\n\ndef second():\n    return 8\n", tests)
    result = VfyProject(tmp_path).run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "--no-cache",
        "-j",
        "1",
        env={**_environment(), "VFY_MUTATION_ENGINE": engine},
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert {item["owner"]["qualname"]: item["status"] for item in report["mutations"]} == expected


@pytest.mark.parametrize("engine", ["auto", "portable"])
@pytest.mark.parametrize("checks_result", [False, True], ids=["survivor", "protected"])
@requires_native_execution
def test_mutations_use_the_baselines_asyncio_support(
    tmp_path: Path, engine: str, checks_result: bool
) -> None:
    statement = "assert await value() == 5" if checks_result else "await value()"
    _project(
        tmp_path,
        "async def value():\n    return 5\n",
        "import pytest\nfrom app import value\n\n"
        f"@pytest.mark.asyncio\nasync def test_value():\n    {statement}\n",
    )
    result = VfyProject(tmp_path).run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "--no-cache",
        "-j",
        "1",
        env={**_environment(), "VFY_MUTATION_ENGINE": engine},
    )
    result.expect(0)
    assert json.loads(result.stdout)["summary"] == {"protected" if checks_result else "survived": 1}


@requires_independent_mutation
def test_base_mutates_committed_pr_files_and_reports_git_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("app.py", "def value():\n    return 1\n")
    project.write("sibling.py", "def other():\n    return 9\n")
    project.write(
        "tests/test_app.py",
        "from app import value\ndef test_value():\n    assert value() > 0\n",
    )
    project.git_init()
    base = project.run("git", "rev-parse", "HEAD").stdout.strip()
    project.write("app.py", "def value():\n    return 2\n")
    project.git_commit("PR edit")
    assert project.run("git", "status", "--porcelain").stdout == ""
    result = project.run_vfy(
        "mutate",
        "--engine=native",
        "--base",
        base,
        "--changed",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "--fail-on-survivor",
        env=_environment(),
    )
    assert result.returncode == 2, result
    report = json.loads(result.stdout)
    assert report["scope"] == {
        "requested_base": base,
        "base_commit": base,
        "merge_base_commit": base,
        "selected_files": [str((tmp_path / "app.py").resolve())],
    }
    assert report["summary"] == {"survived": 1}
    assert [(Path(item["path"]).name, item["original"]) for item in report["mutations"]] == [
        ("app.py", "2")
    ]
    text_result = project.run_vfy(
        "mutate",
        "--engine=native",
        "--base",
        base,
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        env=_environment(),
    )
    assert text_result.returncode == 0, text_result
    assert f"Scope: {base} (merge base {base}); 1 changed Python file(s)" in text_result.stdout
    assert "cache hits=1" in text_result.stdout
    assert (tmp_path / "app.py").read_text() == "def value():\n    return 2\n"


@requires_native_execution
@pytest.mark.parametrize(
    ("options", "message"),
    [
        (("--base", "missing-ref"), "cannot resolve mutation base"),
        (("--base", "HEAD", "--all"), "--base cannot be combined"),
        (("--base", "HEAD", "app.py:2"), "--base cannot be combined"),
        (("--base", "HEAD"), "no supported mutations"),
    ],
)
def test_base_rejections_preserve_evidence_before_collection(
    tmp_path: Path, options: tuple[str, ...], message: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write("tests/test_app.py", "raise AssertionError('must not collect')\n")
    project.git_init()
    artifacts = [
        ".coverage",
        ".vfy/mutations.json",
        ".vfy/mutation-baseline.json",
        "report.json",
    ]
    for name in artifacts:
        project.write(name, "previous evidence\n")
    result = project.run_vfy(
        "mutate",
        "--engine=native" if sys.platform == "linux" else "--engine=pytest-native",
        *options,
        "--source",
        "app.py",
        "--output",
        "report.json",
        env=_environment(),
    )
    assert result.returncode == 1, result
    assert message in result.stderr
    assert "must not collect" not in result.stdout + result.stderr
    assert {name: (tmp_path / name).read_text() for name in artifacts} == dict.fromkeys(
        artifacts, "previous evidence\n"
    )


@requires_independent_mutation
def test_shallow_base_history_fails_without_collecting(tmp_path: Path) -> None:
    origin = VfyProject(tmp_path / "origin")
    origin.write("app.py", "def value():\n    return 1\n")
    origin.write("tests/test_app.py", "raise AssertionError('must not collect')\n")
    origin.git_init()
    origin.run("git", "branch", "comparison")
    origin.write("app.py", "def value():\n    return 2\n")
    origin.git_commit("PR change")
    checkout = tmp_path / "checkout"
    clone = origin.run("git", "clone", "--depth=1", origin.root.as_uri(), str(checkout))
    assert clone.returncode == 0, clone
    project = VfyProject(checkout)
    fetch = project.run("git", "fetch", "--depth=1", "origin", "comparison:comparison")
    assert fetch.returncode == 0, fetch
    result = project.run_vfy(
        "mutate",
        "--engine=native",
        "--base",
        "comparison",
        "--source",
        "app.py",
        env=_environment(),
    )
    assert result.returncode == 1, result
    assert "fetch complete base and HEAD history" in result.stderr
    assert "must not collect" not in result.stdout + result.stderr
    assert not (checkout / ".coverage").exists()
    assert not (checkout / ".vfy").exists()


@requires_native_execution
def test_mutation_children_load_the_adapter_plugins_the_baseline_used(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 5\n")
    project.write(
        "tests/test_app.py",
        "import app\n\n"
        "def test_value(mocker):\n"
        "    spy = mocker.spy(app, 'value')\n"
        "    assert app.value() == 5\n"
        "    assert spy.call_count == 1\n",
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "--source", "app.py", "-q", env=environment
    )
    baseline.expect(0)

    completed = _mutation(tmp_path, env=environment)

    completed.expect(0)
    report = json.loads(completed.stdout)
    assert report["summary"] == {"protected": 1}, report["mutations"]
    assert report["mutations"][0]["phase"] == "call", report["mutations"][0]["detail"]


@requires_native_execution
def test_definitions_in_a_module_no_test_imports_are_unreached(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "")
    project.write("pkg/app.py", "def value():\n    return 5\n")
    project.write(
        "pkg/__main__.py", "LIMIT = 3\nif __name__ == '__main__':\n    print(LIMIT == 3)\n"
    )
    project.write(
        "tests/test_app.py",
        "from pkg.app import value\n\ndef test_value():\n    assert value() == 5\n",
    )
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "--source", "pkg", "-q", env=_environment()
    )
    baseline.expect(0)

    completed = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "pkg",
        "--format",
        "json",
        "--timeout",
        "10",
        env=_environment(),
    )

    completed.expect(0)
    report = json.loads(completed.stdout)
    statuses = {
        (Path(mutation["path"]).name, mutation["status"]) for mutation in report["mutations"]
    }
    assert statuses == {("app.py", "protected"), ("__main__.py", "unreached")}, report["mutations"]


@requires_native_execution
@pytest.mark.parametrize(
    "engine", ["pytest-native", pytest.param("native", marks=requires_independent_mutation)]
)
def test_omitted_mutation_source_runs_full_suite(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path, timeout=60)
    project.write("pyproject.toml", '[tool.coverage.run]\nomit = ["pkg/__init__.py"]\n')
    source = (
        "from .helper import identity\n"
        "def parse(lines):\n"
        "    def generator():\n"
        "        return next(\n"
        "            line.split('=', 1)[1]\n"
        "            for line in lines\n"
        "        )\n"
        "    return identity(generator())\n"
    )
    project.write("pkg/__init__.py", source)
    project.write("pkg/helper.py", "def identity(value):\n    return value\n")
    project.write(
        "tests/test_app.py",
        "from pkg import parse\n\ndef test_parse():\n    assert parse(['a=b']) == 'b'\n",
    )
    environment = _environment()
    stock = project.run_pytest("-q", load_xdist=False, env=environment)
    stock.expect(0)

    command = (
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source",
        "pkg",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "--timeout",
        "10",
        "pkg/__init__.py:5",
    )
    result = project.run_vfy(*command, env=environment)
    result.expect(0)
    mutations = json.loads(result.stdout)["mutations"]
    index = next(item for item in mutations if item["original"] == "1" and item["column"] > 30)
    assert index["status"] == "protected", index
    warm = project.run_vfy(*command, env=environment)
    warm.expect(0)
    warm_report = json.loads(warm.stdout)
    assert warm_report["metrics"]["cache_hits"] > 0
    assert (
        next(item for item in warm_report["mutations"] if item["id"] == index["id"])["status"]
        == "protected"
    )

    project.write("pkg/__init__.py", source.replace("[1]", "[2]"))
    shutil.rmtree(tmp_path / "pkg" / "__pycache__", ignore_errors=True)
    stock_mutant = project.run_pytest("-q", load_xdist=False, env=environment)
    assert stock_mutant.returncode == 1
    assert "test_parse" in stock_mutant.stdout
    assert "IndexError" in stock_mutant.stdout


@requires_native_execution
def test_measured_untaken_branch_remains_unreached(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=60)
    project.write("app.py", "def value(flag):\n    if flag:\n        return 8\n    return 5\n")
    project.write(
        "tests/test_app.py",
        "from app import value\n\ndef test_value():\n    assert value(False) == 5\n",
    )
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--format",
        "json",
        "--timeout",
        "10",
        "app.py:3",
        env=_environment(),
    )
    result.expect(0)
    assert [item["status"] for item in json.loads(result.stdout)["mutations"]] == ["unreached"]
