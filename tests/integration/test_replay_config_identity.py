"""Passing evidence follows pytest configuration and interpreter state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


def test_local_and_portable_identity_distinguish_interpreter_optimization_levels(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    program = (
        "import json\n"
        "from vfy.run.record import execution_fingerprint\n"
        "print(json.dumps({identity: execution_fingerprint({}, [], [], "
        "dependency_fingerprint='fixed', identity=identity) "
        "for identity in ('local', 'portable')}))\n"
    )
    fingerprints: dict[str, set[str]] = {"local": set(), "portable": set()}
    for options in ([], ["-O"], ["-OO"]):
        result = project.run_python(*options, "-c", program, env={"PYTHONOPTIMIZE": None})
        result.expect(0)
        for identity, fingerprint in json.loads(result.stdout).items():
            fingerprints[identity].add(fingerprint)
    assert {identity: len(values) for identity, values in fingerprints.items()} == {
        "local": 3,
        "portable": 3,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize("optimization", ["-O", "-OO"])
@pytest.mark.parametrize("worker_mode", ["serial", "exec", "exec-collection"])
def test_optimized_interpreter_executes_unchanged_tests_instead_of_replaying_passes(
    tmp_path: Path, optimization: str, worker_mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".vfy/\n.coverage*\n.pytest_cache/\n__pycache__/\n")
    project.write("app.py", "def debug_enabled():\n    return __debug__\n")
    project.write(
        "test_app.py",
        "from pathlib import Path\n"
        "from app import debug_enabled\n"
        "def test_debug_enabled():\n"
        "    marker = Path('.vfy/executions')\n"
        "    marker.parent.mkdir(exist_ok=True)\n"
        "    with marker.open('a') as stream:\n"
        "        stream.write(str(debug_enabled()) + '\\n')\n"
        "    if not debug_enabled():\n"
        "        raise RuntimeError('optimized execution detected')\n",
    )
    project.git_init()
    workers = "1" if worker_mode == "serial" else "2"
    arguments = [
        "test",
        "--engine=pytest-native",
        f"--workers={workers}",
        "--cov=app.py",
        "--cov-report=",
        "-q",
    ]
    environment = {
        "PYTHONOPTIMIZE": None,
        "VFY_WORKER_START": None if worker_mode == "serial" else "exec",
        "VFY_WORKER_COLLECTION": "1" if worker_mode == "exec-collection" else None,
    }
    marker = project.root / ".vfy/executions"

    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--all", *arguments[1:], env=environment
    )
    baseline.expect(0)
    reused = project.run_vfy(*arguments, env=environment)
    reused.expect(0)
    assert "Reused 1 tests from passing baseline" in reused.stderr
    assert marker.read_text(encoding="utf-8").splitlines() == ["True"]
    assert project.run("git", "status", "--porcelain").stdout == ""

    optimized = project.run_python(optimization, "-m", "vfy", *arguments, env=environment)
    optimized.expect(1)
    assert "Reused 1 tests from passing baseline" not in optimized.stderr
    assert "1 failed" in optimized.stdout
    assert "RuntimeError: optimized execution detected" in optimized.stdout
    assert marker.read_text(encoding="utf-8").splitlines() == ["True", "False"]

    oracle = project.run_python(optimization, "-m", "pytest", "-q", env=environment)
    assert oracle.returncode == optimized.returncode, oracle.stdout + oracle.stderr
    assert "1 failed" in oracle.stdout
    assert "RuntimeError: optimized execution detected" in oracle.stdout
    assert marker.read_text(encoding="utf-8").splitlines() == ["True", "False", "False"]
    assert project.run("git", "status", "--porcelain").stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize("selection", ["explicit", "discovered"])
@pytest.mark.parametrize("rootdir", ["checkout", "external"])
def test_external_pytest_config_reuses_unchanged_evidence_and_executes_changed_tests(
    tmp_path: Path, selection: str, rootdir: str
) -> None:
    project = VfyProject(tmp_path / "project")
    project.write(".gitignore", ".vfy/\n.coverage*\n.pytest_cache/\n__pycache__/\n")
    project.write("app.py", "def value(): return 1\n")
    for directory, expected in (("tests_ok", 1), ("tests_bad", 2)):
        project.write(
            f"{directory}/test_{directory}.py",
            "from pathlib import Path\n"
            "from app import value\n"
            "def test_value():\n"
            "    marker = Path('.vfy/executions')\n"
            "    marker.parent.mkdir(exist_ok=True)\n"
            "    with marker.open('a') as stream:\n"
            f"        stream.write('{directory}\\n')\n"
            f"    assert value() == {expected}\n",
        )
    project.git_init()
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\npython_files = test_tests_ok.py\n", encoding="utf-8")
    configured_root = project.root if rootdir == "checkout" else tmp_path
    arguments = ["test", "--engine=pytest-native", "--workers=1", f"--rootdir={configured_root}"]
    if selection == "explicit":
        arguments.extend(("-c", str(config)))
    arguments.extend(("--cov=app.py", "--cov-report=", "-q"))
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    marker = project.root / ".vfy/executions"

    for reused in (False, True):
        result = project.run_vfy(*arguments, env=environment)
        result.expect(0)
        assert ("Reused 1 tests from passing baseline" in result.stderr) is reused
        assert marker.read_text(encoding="utf-8").splitlines() == ["tests_ok"]
        assert project.run("git", "status", "--porcelain").stdout == ""

    config.write_text("[pytest]\npython_files = test_tests_bad.py\n", encoding="utf-8")
    result = project.run_vfy(*arguments, env=environment)

    result.expect(1)
    assert "Reused 1 tests from passing baseline" not in result.stderr
    assert "1 failed" in result.stdout
    assert marker.read_text(encoding="utf-8").splitlines() == ["tests_ok", "tests_bad"]
    assert project.run("git", "status", "--porcelain").stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
def test_repeated_cli_invocations_refresh_dependency_identity_and_recover_errors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    project = VfyProject(root)
    dependencies = tmp_path / "dependencies"
    metadata = dependencies / "example-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: example\nVersion: 1.0\n", encoding="utf-8")
    project.write(".gitignore", ".vfy/\n.coverage*\n.pytest_cache/\n__pycache__/\n")
    project.write("app.py", "def value(): return 1\n")
    project.write(
        "test_app.py",
        "from pathlib import Path\n"
        "from app import value\n"
        "def test_value():\n"
        "    marker = Path('.vfy/executions')\n"
        "    marker.parent.mkdir(exist_ok=True)\n"
        "    with marker.open('a') as stream: stream.write('ran\\n')\n"
        "    assert value() == 1\n",
    )
    project.write(
        "same_process.py",
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import vfy.dependency_fingerprint as dependencies\n"
        "from vfy.entry import main\n"
        f"metadata = Path({str(metadata)!r})\n"
        "arguments = ['vfy', 'test', '--engine=native', '--workers=1', "
        "'--cov=app.py', '--cov-report=', '-q']\n"
        "def run():\n"
        "    sys.argv = arguments[:]\n"
        "    return main()\n"
        "first_status = run()\n"
        "first = dependencies.installed_dependency_fingerprint()\n"
        "metadata.write_text('Name: example\\nVersion: 2.0\\n')\n"
        "second_status = run()\n"
        "second = dependencies.installed_dependency_fingerprint()\n"
        "original = dependencies._compute\n"
        "def fail(): raise ValueError('temporary dependency scan failure')\n"
        "dependencies._compute = fail\n"
        "failed_status = run()\n"
        "dependencies._compute = original\n"
        "recovered_status = run()\n"
        "Path('result.json').write_text(json.dumps({\n"
        "    'statuses': [first_status, second_status, failed_status, recovered_status],\n"
        "    'identity_changed': first != second,\n"
        "}))\n",
    )
    project.git_init()
    result = project.run_python(
        "same_process.py",
        env={"PYTHONPATH": str(dependencies), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    diagnostic = result.stdout + result.stderr
    assert result.returncode == 0, diagnostic
    observed = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert observed == {"statuses": [0, 0, 1, 0], "identity_changed": True}, diagnostic
    assert (root / ".vfy/executions").read_text(encoding="utf-8").splitlines()[:2] == [
        "ran",
        "ran",
    ], diagnostic
