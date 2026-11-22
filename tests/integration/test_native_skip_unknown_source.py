"""Marked skips preserve pytest's unknown-source location sentinel."""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


@pytest.mark.parametrize("family", ["xunit1", "xunit2"])
def test_marked_skip_of_c_callable_matches_stock_unknown_source(
    tmp_path: Path, family: str
) -> None:
    roles = ("stock", "serial", "fork") if sys.platform == "linux" else ("stock", "serial")
    projects = {name: VfyProject(tmp_path / name) for name in roles}
    conftest = """
import functools, os
from pathlib import Path
import pytest

def pytest_collection_modifyitems(items):
    for item in items:
        if item.name == 'test_unknown_source':
            item.obj = functools.partial(int, 'skipped C body must not run')
    Path('c-callable-installed').touch()

def pytest_collection_finish(session):
    role = os.getenv('VFY_NATIVE_PARALLEL_INDEX', 'controller')
    Path(f'collected-{role}').touch()
"""
    source = """
import pytest

@pytest.mark.skip(reason='unknown-source-skip-anchor')
def test_unknown_source():
    raise AssertionError('original body must not run')

def test_pass():
    print('passing-sibling')
"""
    for project in projects.values():
        project.write("conftest.py", conftest)
        project.write("test_report.py", source)
    arguments = (
        "-q",
        "--capture=sys",
        "--junitxml=result.xml",
        "-o",
        f"junit_family={family}",
        "-o",
        "junit_logging=all",
        "-o",
        "junit_log_passing_tests=true",
    )
    environment = {"PYTEST_NATIVE_RESULT": None, "VFY_WORKER_COLLECTION": None}
    results = {
        "stock": projects["stock"].run_pytest(*arguments, load_xdist=False, env=environment),
        "serial": projects["serial"].run_vfy(
            "test",
            "--engine=native",
            "--workers=1",
            "--no-replay",
            "--no-cov",
            *arguments,
            env={**environment, "VFY_WORKER_START": None},
        ),
    }
    if "fork" in projects:
        results["fork"] = projects["fork"].run_vfy(
            "test",
            # This route asserts installed-pytest controller-only collection below.
            "--engine=pytest-native",
            "--workers=2",
            "--no-replay",
            "--no-cov",
            *arguments,
            env={
                **environment,
                "VFY_WORKER_START": "fork",
                "VFY_NATIVE_SCHEDULING": "cases",
            },
        )
    records = {}
    for name, result in results.items():
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "1 passed, 1 skipped" in result.stdout
        assert (projects[name].root / "c-callable-installed").exists()
        normalized = []
        for case in ET.parse(projects[name].root / "result.xml").iter("testcase"):
            case.attrib.pop("time", None)
            if case.attrib["name"] == "test_unknown_source":
                skipped = case.find("skipped")
                assert skipped is not None
                assert skipped.text == ":0: unknown-source-skip-anchor"
                if family == "xunit1":
                    assert case.attrib["file"] == "."
                    assert case.attrib["line"] == "-1"
                else:
                    assert "file" not in case.attrib
                    assert "line" not in case.attrib
            normalized.append(ET.tostring(case, encoding="unicode"))
        records[name] = sorted(normalized)
    assert records["serial"] == records["stock"]
    if "fork" in projects:
        assert records["fork"] == records["stock"]
        assert sorted(path.name for path in projects["fork"].root.glob("collected-*")) == [
            "collected-controller"
        ]
