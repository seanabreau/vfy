"""Disabling pytest's terminal plugin preserves execution and JUnit reports."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("terminal_enabled", [False, True], ids=["disabled", "enabled"])
def test_terminal_plugin_is_optional_for_native_execution(
    tmp_path: Path, terminal_enabled: bool
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_case.py",
            "from pathlib import Path\n"
            "import pytest\n"
            "def test_pass():\n"
            "    Path('body.txt').write_text('executed')\n"
            "def test_skip():\n"
            "    pytest.skip('skip sentinel')\n"
            "def test_failure():\n"
            "    print('capture sentinel')\n"
            "    assert False, 'failure sentinel'\n",
        )
        options = ("-q",) if terminal_enabled else ("-p", "no:terminal")
        options += ("--junitxml=result.xml",)
        result = (
            project.run_pytest(*options, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--workers=1", "--no-cov", *options
            )
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert (project.root / "body.txt").read_text() == "executed"
        transcript = result.stdout + result.stderr
        assert "AttributeError" not in transcript
        if terminal_enabled:
            assert "failure sentinel" in transcript
            assert "capture sentinel" in transcript
        else:
            assert "failure sentinel" not in transcript
            assert "capture sentinel" not in transcript
        report = ET.parse(project.root / "result.xml")
        cases = []
        for case in report.iter("testcase"):
            diagnostics = [
                (
                    child.tag,
                    None
                    if child.get("message") is None
                    else child.attrib["message"].replace(str(project.root), "<ROOT>"),
                    (child.text or "").replace(str(project.root), "<ROOT>"),
                )
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ]
            cases.append((case.get("classname"), case.get("name"), diagnostics))
        suite = next(report.iter("testsuite"))
        observed[engine] = (
            {key: suite.get(key) for key in ("tests", "failures", "errors", "skipped")},
            cases,
        )
    assert observed["pytest-native"] == observed["pytest"]
