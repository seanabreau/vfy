"""Reporting and invocation contracts for the real-package corpus harness."""

from __future__ import annotations

import importlib.util
import json
import os
import select
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import ModuleType

import pytest

CORPUS = Path(__file__).parents[2] / "validation/pytest_runner/real_packages/corpus.py"


@pytest.fixture
def corpus() -> ModuleType:
    spec = importlib.util.spec_from_file_location("corpus_gate", CORPUS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_finds_final_outcomes_before_server_shutdown_logs(corpus: ModuleType) -> None:
    log = (
        "2 passed in 0.10s\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_requests.py::TestTimeout::test_connect_timeout[timeout0]\n"
        "4 failed, 615 passed, 15 skipped, 1 xpassed, 18 warnings in 82.80s (0:01:22)\n"
        + '127.0.0.1 - - [28/Oct/2026 20:52:38] "GET /get HTTP/1.1" 200 306\n' * 20
        + "server: 999 failed requests\n"
    )
    assert corpus.summary(log) == {"failed": 4, "passed": 615, "skipped": 15, "xpassed": 1}


@pytest.mark.parametrize(
    ("line", "counts"),
    [
        ("1 xpassed in 0.10s", {"xpassed": 1}),
        (
            "\x1b[32m\x1b[32m\x1b[1m1751 passed\x1b[0m, \x1b[33m11 skipped\x1b[0m"
            "\x1b[32m in 4.51s\x1b[0m\x1b[0m",
            {"passed": 1751, "skipped": 11},
        ),
        (
            "=== 1,234 passed, 2 subtests passed, 1 subtests failed, 3 subtests skipped, "
            "1 error, 5 warnings in 82.80s (0:01:22) ===",
            {
                "passed": 1234,
                "subtests passed": 2,
                "subtests failed": 1,
                "subtests skipped": 3,
                "errors": 1,
            },
        ),
    ],
)
def test_summary_preserves_pytest_outcome_counts(
    corpus: ModuleType, line: str, counts: dict[str, int]
) -> None:
    assert corpus.summary(line) == counts


@pytest.mark.parametrize(
    "last_summary",
    [
        "1 failed, 2 passed",
        "1 failed, 2 passed in 0.",
        "no tests ran in",
        "2 passed, 1 failures in 0.1s",
        "1 failures, 2 passed in 0.1s",
        "2 passed, 1 passed in 0.1s",
        "1 error, 0 errors, 2 passed in 0.1s",
        "2 passed, 1 warning, 2 warnings in 0.1s",
    ],
)
def test_summary_rejects_invalid_final_outcomes(corpus: ModuleType, last_summary: str) -> None:
    assert corpus.summary(f"2 passed in 0.10s\n{last_summary}\n") == {}


@pytest.fixture
def recorded_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: ModuleType) -> Path:
    """Use an actual pinned pytest report as the gate's positive evidence."""
    project = tmp_path / "src/example"
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (project / "test_example.py").write_text("def test_first(): pass\ndef test_second(): pass\n")
    output = tmp_path / "results"
    output.mkdir()
    monkeypatch.setattr(corpus, "SOURCES", project.parent)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    vfy_spec = importlib.util.find_spec("vfy")
    assert vfy_spec is not None
    assert vfy_spec.origin is not None
    source = Path(vfy_spec.origin).resolve().parent.parent
    entry = {
        "name": "example",
        "env": {"PYTHONPATH": os.pathsep.join((str(source), sysconfig.get_path("purelib")))},
    }
    stock = corpus._execute(entry, "pytest", output, bare=False, vfy_source=None)
    assert stock["exit"] == 0, (output / "example.pytest.log").read_text()
    assert "evidence_error" not in stock, stock
    vfy = corpus._execute(entry, "vfy", output, bare=False, vfy_source=None)
    assert vfy["exit"] == 0, (output / "example.vfy.log").read_text()
    (output / corpus.INVENTORY).write_text(
        json.dumps(
            {
                "run_id": "current",
                "names": ["example"],
                "routes": ["pytest", "vfy"],
                "artifact": corpus._artifact(None, source),
            }
        )
    )
    (output / "example.json").write_text(
        json.dumps(
            {
                "name": "example",
                "run_id": "current",
                "pytest": stock,
                "vfy": vfy,
            }
        )
    )
    return output


@pytest.mark.skipif(os.name == "nt", reason="corpus environments use POSIX bin/python paths")
def test_distributed_report_requires_observed_workers(
    recorded_pair: Path, corpus: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    result_path = recorded_pair / "example.json"
    result = json.loads(result_path.read_text())
    vfy_spec = importlib.util.find_spec("vfy")
    assert vfy_spec is not None
    assert vfy_spec.origin is not None
    source = Path(vfy_spec.origin).resolve().parent.parent
    entry = {
        "name": "example",
        "env": {"PYTHONPATH": os.pathsep.join((str(source), sysconfig.get_path("purelib")))},
    }
    result["vfy"] = corpus._execute(
        entry,
        "vfy",
        recorded_pair,
        bare=False,
        vfy_source=None,
        vfy_workers=2,
        vfy_scheduling="cases",
    )
    assert result["vfy"]["exit"] == 0, (recorded_pair / "example.vfy.log").read_text()
    topology = result["vfy"]["execution_topology"]
    assert topology["requested_workers"] == topology["active_workers"] == 2
    assert len(set(topology["worker_pids"])) == 2
    inventory_path = recorded_pair / "corpus-run.json"
    inventory = json.loads(inventory_path.read_text())
    inventory["vfy_workers"] = 2
    inventory["vfy_scheduling"] = "cases"
    inventory_path.write_text(json.dumps(inventory))
    result_path.write_text(json.dumps(result))
    assert corpus.report(recorded_pair)
    for bad_topology in (
        None,
        {**topology, "active_workers": 1},
        {**topology, "worker_pids": [topology["worker_pids"][0]] * 2},
    ):
        result["vfy"]["execution_topology"] = bad_topology
        result_path.write_text(json.dumps(result))
        assert not corpus.report(recorded_pair)
        assert "unverified (requested workers=2, observed topology=" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="corpus environments use POSIX bin/python paths")
@pytest.mark.parametrize(
    ("damage", "verdict"),
    [
        ("none", "match"),
        ("identity", "differ"),
        ("outcome", "differ"),
        ("engine", "unverified"),
        ("artifact", "unverified"),
        ("origin", "unverified"),
        ("adapter", "unverified"),
        ("missing_artifact", "unverified"),
        ("recipe", "incompatible provenance"),
        ("dependencies", "incompatible provenance"),
        ("stale", "incomplete (missing or stale route)"),
        ("missing", "incomplete (missing or stale route)"),
        ("baseline_failure", "baseline_not_green (match)"),
        ("baseline_failure_stderr", "baseline_not_green (match)"),
    ],
)
def test_report_requires_current_complete_matching_cases(
    recorded_pair: Path,
    damage: str,
    verdict: str,
) -> None:
    path = recorded_pair / "example.json"
    result = json.loads(path.read_text())
    if damage == "identity":
        result["vfy"]["cases"][0][1] = "test_other"
    elif damage == "outcome":
        result["vfy"]["cases"][0][2] = [["skipped", "changed"]]
    elif damage == "engine":
        result["vfy"]["engine"] = "pytest-runner-native-v1"
    elif damage == "artifact":
        result["vfy"]["provenance"]["vfy_origins"]["vfy._native"]["sha256"] = "other artifact"
    elif damage == "origin":
        result["vfy"]["provenance"]["vfy_origins"]["vfy"]["path"] = "/other/vfy/__init__.py"
    elif damage == "adapter":
        result["vfy"]["provenance"]["vfy_files"]["vfy/independent/entry.py"] = "other adapter"
    elif damage == "missing_artifact":
        inventory = recorded_pair / "corpus-run.json"
        value = json.loads(inventory.read_text())
        value.pop("artifact")
        inventory.write_text(json.dumps(value))
    elif damage == "recipe":
        result["vfy"]["provenance"]["recipe"] = "other recipe"
    elif damage == "dependencies":
        result["vfy"]["provenance"]["distributions"]["pytest"] = "0"
    elif damage == "stale":
        result["run_id"] = "previous"
    elif damage == "missing":
        inventory = recorded_pair / "corpus-run.json"
        value = json.loads(inventory.read_text())
        value["names"].append("missing")
        inventory.write_text(json.dumps(value))
    elif damage in {"baseline_failure", "baseline_failure_stderr"}:
        for route in ("pytest", "vfy"):
            result[route]["exit"] = 1
            log = "1 failed, 1 passed in 0.1s\n"
            if damage == "baseline_failure_stderr" and route == "pytest":
                log += '127.0.0.1 - - "GET /get HTTP/1.1" 200 306\n' * 20
            (recorded_pair / f"example.{route}.log").write_text(log)
    path.write_text(json.dumps(result))
    report = subprocess.run(
        [sys.executable, str(CORPUS), "report", str(recorded_pair)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert report.returncode == (0 if damage == "none" else 1), report.stdout + report.stderr
    name = "missing" if damage == "missing" else "example"
    assert f"{name} | {verdict}\n" in report.stdout


@pytest.mark.parametrize(
    "footer",
    ["2 passed in", "2 passed, 1 failures in 0.1s", "1 error, 0 errors, 2 passed in 0.1s"],
)
def test_report_rejects_invalid_summary_after_an_earlier_complete_summary(
    recorded_pair: Path,
    footer: str,
) -> None:
    log = recorded_pair / "example.pytest.log"
    log.write_text(log.read_text() + footer + "\n")
    result = subprocess.run(
        [sys.executable, str(CORPUS), "report", str(recorded_pair)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "example | incomplete\n" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="corpus environments use POSIX bin/python paths")
def test_bare_preflight_rejects_a_pytest_import_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corpus: ModuleType,
) -> None:
    project = tmp_path / "src/example"
    python = project / ".venv-bare/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (project / ".venv-bare/pruned.txt").write_text("")
    monkeypatch.setattr(corpus, "SOURCES", project.parent)
    entry = {"name": "example", "env": {"PYTHONPATH": sysconfig.get_path("purelib")}}
    with pytest.raises(RuntimeError, match="bare environment can still import pytest or _pytest"):
        corpus._execute(entry, "vfy", tmp_path, bare=True, vfy_source=None)


@pytest.mark.skipif(sys.platform != "linux", reason="checks descendant exit through a Linux pidfd")
def test_timed_out_execution_stops_test_children_and_retains_both_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corpus: ModuleType
) -> None:
    if sys.platform != "linux":
        pytest.skip("process-exit observation requires Linux pidfds")
    project = tmp_path / "src/example"
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    (project / "test_child.py").write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "def test_child():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "    Path('child.pid').write_text(str(child.pid))\n"
        "    print('child started', flush=True)\n"
        "    print('child diagnostic', file=sys.stderr, flush=True)\n"
        "    time.sleep(60)\n"
    )
    output = tmp_path / "results"
    output.mkdir()
    monkeypatch.setattr(corpus, "SOURCES", project.parent)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    result = corpus._execute(
        {
            "name": "example",
            "timeout": 3,
            "pytest_args": ["-s"],
            "env": {"PYTHONPATH": sysconfig.get_path("purelib")},
        },
        "pytest",
        output,
        bare=False,
        vfy_source=None,
    )
    assert result["exit"] == "timeout"
    pid = int((project / "child.pid").read_text())
    try:
        child = os.pidfd_open(pid)
    except ProcessLookupError:
        pass
    else:
        try:
            assert select.select([child], [], [], 5)[0], f"timed-out child {pid} did not exit"
        finally:
            os.close(child)
    log = (output / "example.pytest.log").read_text()
    assert "child started" in log
    assert "child diagnostic" in log


def test_failed_preparation_fails_the_run_and_retains_requested_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corpus: ModuleType,
) -> None:
    monkeypatch.setattr(corpus, "manifest", lambda: {"example": {"name": "example"}})
    monkeypatch.setattr(corpus, "prepare", lambda *_args, **_kwargs: "install failed")
    monkeypatch.setattr(corpus, "_validate_work_root", lambda: None)
    monkeypatch.setattr(corpus, "_ensure_default_work", lambda: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "corpus.py",
            "run",
            "--only",
            "pytest",
            "--out",
            str(tmp_path),
            "example",
        ],
    )
    assert corpus.main() == 1
    assert json.loads((tmp_path / "corpus-run.json").read_text())["names"] == ["example"]
    assert json.loads((tmp_path / "example.json").read_text())["prepare_error"] == "install failed"


def test_report_rejects_historical_counts_without_execution_evidence(
    tmp_path: Path,
) -> None:
    samples = {
        "timeout": ("timeout", "timed out\n"),
        "crash": (2, "startup error\n"),
        "empty": (0, "no tests ran\n"),
        "zero": (0, "0 passed in 0.1s\n"),
        "deselected": (0, "100 deselected in 0.1s\n"),
        "skipped": (0, "3 skipped in 0.1s\n"),
        "collection": (2, "1 error in 0.1s\n"),
        "failing": (1, "1 failed, 2 passed in 0.1s\n"),
        "fixture_error": (1, "1 error, 2 passed in 0.1s\n"),
        "clean": (0, "2 passed in 0.1s\n"),
    }
    for name, (status, log) in samples.items():
        (tmp_path / f"{name}.json").write_text(
            json.dumps({"name": name, "pytest": {"exit": status}, "vfy": {"exit": status}})
        )
        for route in ("pytest", "vfy"):
            (tmp_path / f"{name}.{route}.log").write_text(log)
    result = subprocess.run(
        [sys.executable, str(CORPUS), "report", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.splitlines()
    assert result.returncode == 1, result.stdout + result.stderr
    for name in ("timeout", "crash", "empty", "zero", "deselected", "skipped", "collection"):
        assert f"{name} | incomplete" in lines, result.stdout
    for name in ("failing", "fixture_error", "clean"):
        assert f"{name} | unverified" in lines
    assert "0 of 10 compared packages have a clean match" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="corpus environments use POSIX bin/python paths")
def test_relative_source_is_anchored_to_invocation_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "python/vfy"
    source.parent.mkdir()
    vfy_spec = importlib.util.find_spec("vfy")
    assert vfy_spec is not None
    assert vfy_spec.origin is not None
    source.symlink_to(Path(vfy_spec.origin).resolve().parent, target_is_directory=True)
    project = tmp_path / "work/src/example"
    bin_dir = project / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    (project / "test_source.py").write_text("def test_source(): pass\n")
    output = tmp_path / "results"
    monkeypatch.setenv("CORPUS_WORK", str(tmp_path / "work"))
    spec = importlib.util.spec_from_file_location("real_package_corpus", CORPUS)
    assert spec is not None
    assert spec.loader is not None
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    monkeypatch.setattr(corpus, "manifest", lambda: {"example": {"name": "example"}})
    monkeypatch.setattr(corpus, "prepare", lambda *_args, **_kwargs: "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "corpus.py",
            "run",
            "--vfy-source",
            "python",
            "--only",
            "vfy",
            "--out",
            str(output),
            "example",
        ],
    )
    assert corpus.main() == 0
    result = json.loads((output / "example.json").read_text())
    log = (output / "example.vfy.log").read_text()
    assert result["vfy"]["exit"] == 0, log
    assert "1 passed" in log
    assert result["vfy"]["engine"] == "vfy-independent-v1"


def test_preparation_rebuilds_existing_and_failed_environments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("real_package_corpus_prepare", CORPUS)
    assert spec is not None
    assert spec.loader is not None
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    monkeypatch.setattr(corpus, "_checkout", lambda _entry: tmp_path)
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    commands: list[list[str]] = []
    fail_install = True

    def run(
        command: list[str], *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal fail_install
        commands.append(command)
        if command[1:3] == ["pip", "install"] and fail_install:
            fail_install = False
            return subprocess.CompletedProcess(command, 1, "")
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(corpus, "_run", run)
    entry = {"name": "example", "install": ["example==1"]}
    assert "exit status 1" in corpus.prepare(entry)
    entry["install"] = ["example==2"]
    assert corpus.prepare(entry) == ""
    assert sum(command[:2] == ["uv", "venv"] and "--clear" in command for command in commands) == 2
    installs = [command for command in commands if command[1:3] == ["pip", "install"]]
    assert "example==1" in installs[0]
    assert "example==2" in installs[1]


def test_preparation_removes_prior_run_state_before_git_versioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corpus: ModuleType,
) -> None:
    project = tmp_path / "src/example"
    project.mkdir(parents=True)
    monkeypatch.setattr(corpus, "SOURCES", project.parent)

    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=project, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Corpus test")
    git("config", "user.email", "corpus@example.invalid")
    (project / ".gitignore").write_text(".venv/\n")
    (project / "source.py").write_text("value = 1\n")
    git("add", ".")
    git("commit", "-qm", "clean package")
    revision = git("rev-parse", "HEAD")
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    unrelated = project / ".venv/keep"
    unrelated.write_text("preserve")
    states_at_install: list[str] = []

    def run(
        command: list[str],
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] != "uv":
            return subprocess.run(command, cwd=project, text=True, capture_output=True, check=False)
        if command[1:3] == ["pip", "install"]:
            # Git-based version providers inspect this state during the build.
            states_at_install.append(git("status", "--porcelain"))
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(corpus, "_run", run)
    for _ in range(2):
        for cache in (".vfy", ".pytest_cache"):
            (project / cache).mkdir()
            (project / cache / "previous-run").write_text("stale")
        assert ".vfy/" in git("status", "--porcelain")
        assert corpus.prepare({"name": "example", "commit": revision, "install": ["."]}) == ""
    assert states_at_install == ["", ""]
    untracked = project / "fixture-input.txt"
    untracked.write_text("preserve untracked input")
    (project / ".vfy").mkdir()
    assert corpus.prepare({"name": "example", "commit": revision, "install": ["."]}) == ""
    assert states_at_install[-1] == "?? fixture-input.txt"
    assert untracked.read_text() == "preserve untracked input"
    assert unrelated.read_text() == "preserve"
    assert (project / "source.py").read_text() == "value = 1\n"


def test_prepare_validates_all_names_before_preparing_any_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corpus: ModuleType,
) -> None:
    prepared: list[str] = []
    monkeypatch.setattr(corpus, "manifest", lambda: {"example": {"name": "example"}})
    monkeypatch.setattr(corpus, "prepare", lambda entry: prepared.append(entry["name"]) or "")
    monkeypatch.setattr(sys, "argv", ["corpus.py", "prepare", "example", "unknown-package"])
    with pytest.raises(SystemExit) as error:
        corpus.main()
    assert error.value.code == 2
    assert prepared == []

    work = tmp_path / "work"
    result = subprocess.run(
        [sys.executable, str(CORPUS), "prepare", "iniconfig", "unknown-package"],
        env={**os.environ, "CORPUS_WORK": str(work)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "package names must be unique entries from manifest.json" in result.stderr
    assert "Traceback" not in result.stderr
    assert not work.exists()


def test_wheel_install_uses_uncached_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("real_package_corpus_wheel", CORPUS)
    assert spec is not None
    assert spec.loader is not None
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    commands: list[list[str]] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(corpus, "_run", run)
    monkeypatch.setattr(corpus, "SOURCES", tmp_path)
    assert corpus._install_vfy({"name": "example"}, "vfy.whl", bare=True) == ""
    assert "--no-cache" in commands[0]
    assert "--link-mode=copy" in commands[0]


@pytest.mark.skipif(os.name == "nt", reason="corpus environments use POSIX bin/python paths")
def test_corpus_rejects_ancestor_config_and_preserves_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nfilterwarnings = ["error"]\n'
    )
    unsafe_work = parent / "work"
    project = unsafe_work / "src/example"
    project.mkdir(parents=True)
    (project / "test_warning.py").write_text(
        "import warnings\ndef test_warning():\n"
        "    warnings.warn('parent contamination', DeprecationWarning)\n"
    )
    environment = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    baseline = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert baseline.returncode == 1, baseline.stdout + baseline.stderr
    assert "DeprecationWarning: parent contamination" in baseline.stdout

    marker = project / ".pytest_cache/keep"
    marker.parent.mkdir()
    marker.write_text("keep")
    environment["CORPUS_WORK"] = str(unsafe_work)
    for arguments in (["prepare", "iniconfig"], ["run", "--only", "pytest", "iniconfig"]):
        blocked = subprocess.run(
            [sys.executable, str(CORPUS), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert blocked.returncode == 2
        assert str(parent / "pyproject.toml") in blocked.stderr
        assert marker.read_text() == "keep"

    safe_sources = tmp_path / "safe/src"
    safe_project = safe_sources / "example"
    bin_dir = safe_project / ".venv/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    (safe_project / "pytest.ini").write_text("[pytest]\naddopts = -k selected\n")
    (safe_project / "test_project.py").write_text(
        "def test_selected():\n    pass\ndef test_other():\n    assert False\n"
    )
    output = tmp_path / "output"
    output.mkdir()
    spec = importlib.util.spec_from_file_location("real_package_corpus_isolation", CORPUS)
    assert spec is not None
    assert spec.loader is not None
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    monkeypatch.setattr(corpus, "SOURCES", safe_sources)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", sysconfig.get_path("purelib"))
    result = corpus._execute(
        {"name": "example", "env": {"PYTHONPATH": sysconfig.get_path("purelib")}},
        "pytest",
        output,
        bare=False,
        vfy_source=None,
    )
    log = (output / "example.pytest.log").read_text()
    assert result["exit"] == 0, log
    assert result["counts"] == {"passed": 1, "deselected": 1}, log


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and permission contract")
def test_default_work_directory_is_private(tmp_path: Path) -> None:
    environment = {**os.environ, "TMPDIR": str(tmp_path)}
    environment.pop("CORPUS_WORK", None)
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('corpus', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "module._validate_work_root()\n"
        "module._ensure_default_work()\n"
        "print(module.WORK)\n"
    )
    command = [sys.executable, "-c", code, str(CORPUS)]
    created = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
    root = Path(created.stdout.strip())
    assert root.parent == tmp_path
    assert root.stat().st_mode & 0o777 == 0o700
    root.chmod(0o755)
    rejected = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "not private to this user" in rejected.stderr


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires privileges on Windows")
def test_checkout_symlink_cannot_escape_managed_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tmp_path / "safe/src"
    sources.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / ".pytest_cache/keep"
    marker.parent.mkdir()
    marker.write_text("keep")
    (sources / "example").symlink_to(outside, target_is_directory=True)
    spec = importlib.util.spec_from_file_location("real_package_corpus_symlink", CORPUS)
    assert spec is not None
    assert spec.loader is not None
    corpus = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(corpus)
    monkeypatch.setattr(corpus, "SOURCES", sources)
    with pytest.raises(ValueError, match="checkout resolves outside"):
        corpus._execute({"name": "example"}, "pytest", tmp_path, bare=False, vfy_source=None)
    assert marker.read_text() == "keep"
