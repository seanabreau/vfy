"""The invocation owns configuration parsing before selecting real worker processes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_GUARD = """import os, sys
from pathlib import Path
if os.environ.get('BLOCK_RUNNER_IMPORTS'):
    Path('attempts.txt').write_text('')
    class Guard:
        def find_spec(self, name, path=None, target=None):
            if name.split('.')[0] in {'pytest', '_pytest', 'pluggy', 'packaging'}:
                with Path('attempts.txt').open('a') as stream:
                    stream.write(name + '\\n')
                raise ImportError(name)
    sys.meta_path.insert(0, Guard())
"""


def _configuration_project(tmp_path: Path, failure: str) -> tuple[VfyProject, list[str]]:
    project = VfyProject(tmp_path)
    project.write("sitecustomize.py", _GUARD)
    project.write("test_body.py", "raise AssertionError('test module was imported')\n")
    arguments: list[str]
    if failure == "missing":
        arguments = ["-c", "missing.ini"]
    elif failure == "unreadable":
        if not hasattr(os, "geteuid") or os.geteuid() == 0:
            pytest.skip("requires an unprivileged POSIX reader")
        private = project.write("private.ini", "[pytest]\n")
        private.chmod(0)
        arguments = ["-c", "private.ini"]
    elif failure == "directory":
        (tmp_path / "directory.ini").mkdir()
        arguments = ["-c", "directory.ini"]
    elif failure == "encoding":
        (tmp_path / "pytest.ini").write_bytes(b"[pytest]\naddopts = \xff\n")
        arguments = ["-c", "pytest.ini"]
    elif failure == "toml":
        project.write("pyproject.toml", "[tool.pytest.ini_options\n")
        arguments = ["-c", "pyproject.toml"]
    else:
        project.write("pytest.ini", "[pytest]\naddopts = -k 'unterminated\n")
        arguments = []
    return project, arguments


@pytest.mark.parametrize(
    "failure", ["missing", "unreadable", "directory", "encoding", "toml", "quoted-addopts"]
)
def test_public_configuration_failure_status_matches_stock(tmp_path: Path, failure: str) -> None:
    project, arguments = _configuration_project(tmp_path, failure)
    oracle = project.run_engine("stock", *arguments, env={"PYTEST_ADDOPTS": ""})
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        *arguments,
        env={"PYTHONPATH": str(tmp_path), "PYTEST_ADDOPTS": "", "BLOCK_RUNNER_IMPORTS": "1"},
    )
    assert actual.returncode == oracle.returncode, actual.stdout + actual.stderr
    assert (tmp_path / "attempts.txt").read_text() == ""


@requires_native_pytest
def test_public_report_write_failure_cannot_reuse_successful_invocation_status(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_body(): pass\n")
    (tmp_path / "report-directory").mkdir()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--native-result-json=report-directory",
    )
    assert actual.returncode != 0, actual.stdout + actual.stderr
    assert "directory" in actual.stderr.lower(), actual.stdout + actual.stderr


@requires_native_pytest
def test_public_workers_use_separate_directories_under_configured_basetemp(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    base = tmp_path / "temporary-base"
    base.mkdir()
    sentinel = base / "parent-owned.txt"
    sentinel.write_text("preserve the shared parent")
    project.write("pytest.ini", f"[pytest]\naddopts = -n2 --basetemp={base}\n")
    for name in ["a", "b"]:
        project.write(
            f"test_{name}.py",
            """import json, os
from pathlib import Path
def test_body(tmp_path):
    tmp_path.joinpath('value.txt').write_text('owned')
    Path(f'path-{os.getpid()}.json').write_text(json.dumps(str(tmp_path)))
""",
        )
    actual = project.run_engine("native")
    actual.expect(0)
    directories = [Path(json.loads(path.read_text())) for path in tmp_path.glob("path-*.json")]
    assert len(directories) == 2
    assert {directory.parent.name for directory in directories} == {"worker-0", "worker-1"}
    assert all(directory.parent.parent == base for directory in directories)
    assert all(directory.joinpath("value.txt").read_text() == "owned" for directory in directories)
    assert sentinel.read_text() == "preserve the shared parent"


@requires_native_pytest
@pytest.mark.parametrize("workers", [1, 2])
def test_public_invalid_basetemp_is_rejected_before_configure(tmp_path: Path, workers: int) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "raise AssertionError('test module was imported')\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\ndef pytest_configure(config):\n"
        "    Path('configured.txt').touch()\n",
    )
    oracle = project.run_engine("stock", "--basetemp=.")
    oracle.expect(4)
    assert not (tmp_path / "configured.txt").exists()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--basetemp=.",
    )
    assert actual.returncode == oracle.returncode, actual.stdout + actual.stderr
    assert not (tmp_path / "configured.txt").exists()
    assert "basetemp" in actual.stderr


def _selection_project(
    tmp_path: Path, selection: str
) -> tuple[VfyProject, list[str], str, int, str]:
    project = VfyProject(tmp_path)
    project.write("sitecustomize.py", _GUARD)
    project.write(
        "selection_plugin.py",
        """def pytest_addoption(parser):
    parser.addoption('--probe-token', default='absent')

def pytest_configure(config):
    if config.getoption('probe_token') == 'hook':
        config.option.workers = 2
""",
    )
    body = """import hashlib, json, os
from pathlib import Path

def test_body():
    path = Path(__file__).resolve()
    with Path('bodies-' + os.environ['ROUTE'] + '.jsonl').open('a') as stream:
        stream.write(json.dumps({'path': str(path), 'pid': os.getpid(),
                                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}) + '\\n')
"""
    for name in ["a", "b"]:
        project.write(f"tests/test_{name}.py", body)
    arguments = ["-q", "-p", "selection_plugin", "tests"]
    environment = ""
    expected_token = "absent"
    workers = 2
    if selection == "ini":
        expected_token = "ini"
        project.write("pytest.ini", "[pytest]\naddopts = -n2 --probe-token=ini\n")
    elif selection in {"environment", "override"}:
        expected_token = "environment"
        environment = "-n2 --probe-token=environment"
        if selection == "override":
            workers = 1
            arguments.append("-n1")
    elif selection == "vfyconfig":
        workers = 1
        expected_token = "environment"
        environment = "-n2 --probe-token=environment"
        project.write("pyproject.toml", "[tool.vfy.test]\nworkers = 1\n")
    elif selection == "serial":
        workers = 1
        arguments.append("-n1")
    elif selection == "hook":
        expected_token = "hook"
        arguments += ["-n1", "--probe-token=hook"]
    else:
        arguments.append("-n2")
    return project, arguments, environment, workers, expected_token


@requires_native_pytest
@pytest.mark.parametrize(
    "selection", ["cli", "ini", "environment", "serial", "override", "hook", "vfyconfig"]
)
def test_public_frontend_uses_final_configuration_for_actual_worker_selection(
    tmp_path: Path, selection: str
) -> None:
    project, arguments, environment, workers, expected_token = _selection_project(
        tmp_path, selection
    )
    project.write(
        "selection_plugin.py",
        """import os
from pathlib import Path
def pytest_addoption(parser):
    parser.addoption('--probe-token', default='absent')
def pytest_configure(config):
    assert config.option.probe_token == os.environ['TOKEN']
    if config.option.probe_token == 'hook':
        config.option.workers = 2
    if 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ:
        Path('controller.pid').write_text(str(os.getpid()))
""",
    )
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--native-result-json=result.json",
        *arguments,
        env={
            "PYTHONPATH": str(tmp_path),
            "PYTEST_ADDOPTS": environment,
            "BLOCK_RUNNER_IMPORTS": "1",
            "ROUTE": "public",
            "TOKEN": expected_token,
        },
    )
    actual.expect(0)
    assert (tmp_path / "attempts.txt").read_text() == ""
    bodies = [
        json.loads(line) for line in (tmp_path / "bodies-public.jsonl").read_text().splitlines()
    ]
    assert len(bodies) == 2
    assert {Path(body["path"]).name for body in bodies} == {"test_a.py", "test_b.py"}
    raw = json.loads((tmp_path / "result.json").read_text())
    pids = {body["pid"] for body in bodies}
    assert set(raw["execution_topology"]["worker_pids"]) == pids
    assert len(pids) == workers
    assert raw["execution_topology"]["requested_workers"] == workers
    assert (int((tmp_path / "controller.pid").read_text()) in pids) is (workers == 1)


@requires_native_pytest
@pytest.mark.parametrize(
    "arguments",
    [
        ["--", "--collect-only"],
        ["--", "--collect-only", "-k", "wanted"],
        ["--", "--collect-only", "--ignore=test_other.py"],
        ["-k", "wanted", "--", "--collect-only"],
        ["test_body.py", "--", "--collect-only"],
    ],
)
def test_final_separator_matches_stock_pytest(tmp_path: Path, arguments: list[str]) -> None:
    project = VfyProject(tmp_path)
    project.write("test_body.py", "def test_wanted(): pass\ndef test_other(): pass\n")
    project.write("test_other.py", "def test_other_file(): pass\n")
    environment = {"PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    project.write("arguments.txt", "\n".join(["-q", *arguments]) + "\n")
    oracle = project.run_engine("stock", "@arguments.txt", env=environment)
    actual = project.run_vfy(
        "test", "--engine=native", "--no-cov", "@arguments.txt", env=environment
    )
    assert actual.returncode == oracle.returncode, (
        actual.stdout + actual.stderr + oracle.stdout + oracle.stderr
    )
    for name in (
        "test_body.py::test_wanted",
        "test_body.py::test_other",
        "test_other.py::test_other_file",
    ):
        assert (name in actual.stdout) == (name in oracle.stdout), actual.stdout + oracle.stdout
