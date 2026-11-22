"""Public mutation results across process-exit notification capabilities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@requires_native_pytest
@pytest.mark.parametrize("pidfd", ["available", "missing", "denied"])
def test_file_mutations_preserve_results_without_pidfd(tmp_path: Path, pidfd: str) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import os\n"
        f"capability = {pidfd!r}\n"
        "if capability == 'missing':\n"
        "    if hasattr(os, 'pidfd_open'):\n"
        "        del os.pidfd_open\n"
        "elif capability == 'denied':\n"
        "    def deny_pidfd(*args, **kwargs):\n"
        "        raise PermissionError('pidfd is unavailable in this sandbox')\n"
        "    os.pidfd_open = deny_pidfd\n",
    )
    for name in ("first", "second"):
        project.write(
            f"tests/test_{name}.py",
            "from app import value\n\n"
            "def test_value():\n"
            + ("    assert value() >= 1\n" if name == "first" else "    assert value() == 1\n"),
        )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    clean = project.run_python("-m", "pytest", "-q", "tests", env=environment)
    clean.expect(0)
    result = project.run_vfy(
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--test-isolation=file",
        "--workers=2",
        "--no-cache",
        "--format=json",
        "-q",
        "tests",
        env=environment,
    )
    result.expect(0)
    report = json.loads(result.stdout)
    assert report["summary"] == {"protected": 1}
    mutation = report["mutations"][0]
    assert mutation["killer"] == "tests/test_second.py::test_value"
    assert mutation["phase"] == "call"
    assert (tmp_path / "app.py").read_text() == "def value():\n    return 1\n"
