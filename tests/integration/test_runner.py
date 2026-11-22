from __future__ import annotations

from pathlib import Path

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@requires_native_pytest
def test_runner_respects_warnings_as_errors(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nfilterwarnings = error\n")
    project.write("test_example.py", "def test_example():\n    assert True\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "-j",
        "1",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    assert "1 passed" in result.stdout


@requires_native_pytest
def test_console_entry_point_preserves_project_imports_in_importlib_mode(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "VALUE = 1\n")
    project.write(
        "tests/test_app.py",
        "from app import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
    )
    result = project.run_vfy_console(
        "test",
        "--engine=native",
        "--all",
        "-j1",
        "-q",
        "--import-mode=importlib",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    assert "1 passed" in result.stdout
