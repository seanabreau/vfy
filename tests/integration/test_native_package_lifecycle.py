"""Compare package xunit lifecycle and failed-owner reuse with pytest."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

type CaseReports = list[tuple[str | None, list[tuple[str, str | None, str | None]]]]


@pytest.mark.parametrize(
    ("mode", "stop_on_failure"),
    [
        ("normal", False),
        ("setup-error", False),
        ("setup-error", True),
        ("setup-skip", False),
        ("teardown-error", False),
        ("teardown-error", True),
    ],
)
def test_nested_package_hooks_and_failed_setup_match_pytest(
    tmp_path: Path, mode: str, stop_on_failure: bool
) -> None:
    setup = {
        "setup-error": "raise RuntimeError('package setup failed')",
        "setup-skip": "pytest.skip('package unavailable')",
    }.get(mode, "pass")
    teardown = (
        "raise RuntimeError('package teardown failed')" if mode == "teardown-error" else "pass"
    )
    observed: dict[str, tuple[list[str], CaseReports]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "outer/__init__.py",
            """from pathlib import Path

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

def setup_module(module):
    assert module.__name__ == 'outer'
    record('outer setup')

def teardown_module(module):
    assert module.__name__ == 'outer'
    record('outer teardown')
""",
        )
        project.write(
            "outer/inner/__init__.py",
            f"""import pytest
from outer import record

def setup_module():
    record('inner setup')
    {setup}

def teardown_module():
    record('inner teardown')
    {teardown}
""",
        )
        for module in ("a", "b"):
            skipped = (
                "@pytest.mark.skip(reason='first item skipped')\n"
                "def test_skipped():\n    record('skipped call')\n"
                if module == "a"
                else ""
            )
            project.write(
                f"outer/inner/test_{module}.py",
                f"""import pytest
from outer import record

def setup_module():
    record('{module} setup')

def teardown_module():
    record('{module} teardown')

{skipped}
def test_{module}():
    record('{module} call')
""",
            )
        project.write(
            "outer/test_z.py",
            "from outer import record\ndef test_outer():\n    record('outer call')\n",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            *(("-x",) if stop_on_failure else ()),
            "outer",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == (1 if mode.endswith("error") else 0), transcript
        events = (project.root / "events.txt").read_text().splitlines()
        reports = [
            (
                case.get("name"),
                [(child.tag, child.get("type"), child.get("message")) for child in case],
            )
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
        observed[engine] = (events, reports)

    assert observed["pytest-native"] == observed["pytest"]
    expected_events = ["outer setup", "inner setup"]
    if not mode.startswith("setup-"):
        expected_events.extend(
            [
                "a setup",
                "a call",
                "a teardown",
                "b setup",
                "b call",
                "b teardown",
                "inner teardown",
            ]
        )
    if not stop_on_failure:
        expected_events.append("outer call")
    expected_events.append("outer teardown")
    assert observed["pytest-native"][0] == expected_events


def test_failed_package_setup_exception_survives_owner_finalizers(tmp_path: Path) -> None:
    observed: dict[str, list[str]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """package = None

def pytest_collection_modifyitems(items):
    global package
    package = next(node for node in items[0].listchain() if node.nodeid == 'pkg')
""",
        )
        project.write(
            "pkg/__init__.py",
            """from pathlib import Path
import conftest

state = {'alive': True}

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

class SetupError(Exception):
    def __del__(self):
        state['alive'] = False
        record('exception released')

def finalize():
    record('package finalizer alive' if state['alive'] else 'package finalizer dead')
    assert state['alive'], 'setup exception was released before package teardown'

def setup_module():
    record('package setup')
    conftest.package.addfinalizer(finalize)
    raise SetupError('package setup failed')
""",
        )
        project.write("pkg/test_a.py", "def test_one(): pass\ndef test_two(): pass\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "pkg",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        transcript = result.stdout + result.stderr
        assert result.returncode == 1, transcript
        observed[engine] = (project.root / "events.txt").read_text().splitlines()

    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"] == [
        "package setup",
        "package finalizer alive",
        "exception released",
    ]
