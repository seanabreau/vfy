"""Authenticated mutation startup failures remain incomplete results."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
def test_mutated_conftest_import_reports_error_and_preserves_other_results(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = "def index():\n    return 0\n\ndef answer():\n    return 41\n"
    project.write("app.py", source)
    starts = project.write(".vfy/startup-values", "")
    project.write(
        "conftest.py",
        "from pathlib import Path\nimport app\n\n"
        "with Path('.vfy/startup-values').open('a') as stream:\n"
        "    stream.write(str(app.index()) + '\\n')\n"
        "configured = ['entry'][app.index()]\n",
    )
    project.write(
        "test_app.py", "import app\n\ndef test_answer():\n    assert app.answer() == 41\n"
    )
    clean = project.run_python("-B", "-m", "pytest", "-q")
    clean.expect(0)
    project.write("app.py", source.replace("return 0", "return 1"))
    changed = project.run_python("-B", "-m", "pytest", "-q")
    changed.expect(4)
    assert "IndexError" in changed.stderr
    project.write("app.py", source)
    starts.write_text("")

    for invocation in range(2):
        completed = project.run_vfy(
            "mutate",
            "--engine=native",
            "--all",
            "--source=app.py",
            "--operator=integer_plus_one",
            "--format=json",
        )
        completed.expect(1)
        assert "Traceback (most recent call last)" not in completed.stderr
        report = json.loads(completed.stdout)
        assert report["summary"] == {"error": 1, "protected": 1}, report
        startup = next(row for row in report["mutations"] if row["original"] == "0")
        assert startup["status"] == "error", report
        assert (startup["killer"], startup.get("phase")) == (None, None), report
        assert startup["returncode"] == 4, report
        assert "conftest_loading" in startup["detail"], report
        assert "IndexError" in startup["detail"], report
        detected = next(row for row in report["mutations"] if row["original"] == "41")
        assert (detected["status"], detected["killer"], detected["phase"]) == (
            "protected",
            "test_app.py::test_answer",
            "call",
        ), report
        assert starts.read_text().splitlines().count("1") == invocation + 1, report


@pytest.mark.skipif(sys.platform != "linux", reason="independent mutations require Linux")
def test_mutated_test_collection_reports_error_and_preserves_other_results(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = "IMPORT_VALUE = 1\n\ndef value():\n    return 3\n"
    project.write("app.py", source)
    starts = project.write(".vfy/collection-values", "")
    project.write(
        "test_app.py",
        "from pathlib import Path\nimport app\n\n"
        "with Path('.vfy/collection-values').open('a') as stream:\n"
        "    stream.write(str(app.IMPORT_VALUE) + '\\n')\n"
        "assert app.IMPORT_VALUE == 1\n\n"
        "def test_value():\n    assert app.value() == 3\n",
    )
    clean = project.run_python("-B", "-m", "pytest", "-q")
    clean.expect(0)
    project.write("app.py", source.replace("IMPORT_VALUE = 1", "IMPORT_VALUE = 2"))
    changed = project.run_python("-B", "-m", "pytest", "-q")
    changed.expect(2)
    assert "AssertionError" in changed.stdout
    project.write("app.py", source)
    starts.write_text("")

    for invocation in range(2):
        completed = project.run_vfy(
            "mutate",
            "--engine=native",
            "--all",
            "--source=app.py",
            "--operator=integer_plus_one",
            "--format=json",
        )
        completed.expect(1)
        assert "Traceback (most recent call last)" not in completed.stderr
        report = json.loads(completed.stdout)
        assert report["summary"] == {"error": 1, "protected": 1}, report
        collection = next(row for row in report["mutations"] if row["original"] == "1")
        assert collection["status"] == "error", report
        assert (collection["killer"], collection.get("phase")) == (None, None), report
        assert collection["returncode"] == 2, report
        assert "AssertionError" in collection["detail"], report
        assert "test_app.py" in collection["detail"], report
        detected = next(row for row in report["mutations"] if row["original"] == "3")
        assert (detected["status"], detected["killer"], detected["phase"]) == (
            "protected",
            "test_app.py::test_value",
            "call",
        ), report
        assert starts.read_text().splitlines().count("2") == invocation + 1, report
