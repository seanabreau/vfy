"""Verify vfy wheel installation leaves the pinned stock pytest intact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE = """
import hashlib, importlib.metadata, importlib.util, json
from pathlib import Path
import pytest
from _pytest import fixtures
from _pytest.python import PyCollector
payload = {
    'pytest_version': importlib.metadata.version('pytest'),
    'pytest_files': {
        str(name): hashlib.sha256(
            Path(importlib.metadata.distribution('pytest').locate_file(name)).read_bytes()
        ).hexdigest()
        for name in importlib.metadata.files('pytest')
        if str(name).endswith('.py')
    },
    'vfy_installed': importlib.util.find_spec('vfy') is not None,
}
if payload['vfy_installed']:
    previous = (fixtures.reorder_items, PyCollector._genfunctions, pytest.Cache)
    from vfy._pytest import native_pytest
    assert previous == (fixtures.reorder_items, PyCollector._genfunctions, pytest.Cache)
    payload['vfy_version'] = importlib.metadata.version('vfy')
    payload['native_origin'] = native_pytest.__file__
print(json.dumps(payload, sort_keys=True))
"""


def run(command: list[str], cwd: Path, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {command}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout


def verify(vfy_wheel: Path, pytest_wheel: Path, python: Path, output: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    python = python.absolute()
    vfy_wheel = vfy_wheel.resolve(strict=True)
    pytest_wheel = pytest_wheel.resolve(strict=True)
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "VIRTUAL_ENV", "CONDA_PREFIX"):
        environment.pop(name, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    stages: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="vfy-wheel-lifecycle-") as directory:
        root = Path(directory)
        requirements = root / "requirements.txt"
        run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--group",
                "pytest-corpus",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            repository,
            environment,
        )
        run(["uv", "venv", "--python", str(python), str(root / "venv")], root, environment)
        executable = root / "venv" / "bin" / "python"
        run(
            ["uv", "pip", "sync", "--python", str(executable), str(requirements)], root, environment
        )
        run(
            [
                "uv",
                "pip",
                "install",
                "--no-deps",
                "--reinstall-package",
                "pytest",
                "--python",
                str(executable),
                str(pytest_wheel),
            ],
            root,
            environment,
        )
        project = root / "project"
        project.mkdir()
        (project / "test_example.py").write_text(
            "import pytest\n@pytest.mark.parametrize('value', range(3))\n"
            "def test_example(value):\n    assert value >= 0\n",
            encoding="utf-8",
        )
        baseline: object = None
        for stage in ("stock", "install", "upgrade", "uninstall", "reinstall"):
            if stage in {"install", "upgrade", "reinstall"}:
                run(
                    [
                        "uv",
                        "pip",
                        "install",
                        "--no-deps",
                        "--reinstall-package",
                        "vfy",
                        "--python",
                        str(executable),
                        str(vfy_wheel),
                    ],
                    root,
                    environment,
                )
            elif stage == "uninstall":
                run(
                    ["uv", "pip", "uninstall", "--python", str(executable), "vfy"],
                    root,
                    environment,
                )
            run(["uv", "pip", "check", "--python", str(executable)], root, environment)
            observed = json.loads(run([str(executable), "-c", PROBE], project, environment))
            if stage == "stock":
                baseline = observed["pytest_files"]
            if observed["pytest_version"] != "9.1.1" or observed["pytest_files"] != baseline:
                raise RuntimeError(f"{stage}: vfy changed the stock pytest installation")
            expected_install = stage not in {"stock", "uninstall"}
            if observed["vfy_installed"] is not expected_install:
                raise RuntimeError(f"{stage}: unexpected vfy import state")
            if expected_install and not Path(observed["native_origin"]).resolve().is_relative_to(
                (root / "venv").resolve()
            ):
                raise RuntimeError(f"{stage}: native adapter is outside the installed environment")
            commands = [[str(executable), "-m", "pytest", "-q"]]
            if expected_install:
                commands.append(
                    [
                        str(executable),
                        "-m",
                        "vfy",
                        "test",
                        "--engine=pytest-native",
                        "--workers=1",
                        "--no-cov",
                        "-q",
                    ]
                )
            observed["smoke"] = [run(command, project, environment) for command in commands]
            if any("3 passed" not in text for text in observed["smoke"]):
                raise RuntimeError(f"{stage}: smoke did not execute the expected cases")
            stages[stage] = observed
        record = {
            "stages": stages,
            "artifacts": {
                wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()
                for wheel in (vfy_wheel, pytest_wheel)
            },
            "dependency_lock_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vfy-wheel", type=Path, required=True)
    parser.add_argument("--stock-pytest-wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    verify(arguments.vfy_wheel, arguments.stock_pytest_wheel, arguments.python, arguments.output)


if __name__ == "__main__":
    main()
