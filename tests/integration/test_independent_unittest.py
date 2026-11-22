"""Stdlib TestCase discovery, lifecycle and results match stock pytest through VfyProject."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_FIXTURES = Path(__file__).parents[1] / "fixtures/independent_unittest"
_SCENARIOS = sorted(path.stem for path in _FIXTURES.glob("*.txt"))
# Stock pytest prints its own runner and unittest frames inside native exception-group and
# chained tracebacks, and vfy prints its adapter frames there. Those scenarios compare exception
# lines; every other scenario compares the full terminal output.
_NATIVE_FRAME_SCENARIOS = frozenset({"errors"})

_OBSERVER = """import gc
import json
import sys
from pathlib import Path

EVENTS = []
DIAGNOSTICS = []

def trace(event, **fields):
    EVENTS.append({'event': event, **fields})

def pytest_collection_modifyitems(items):
    trace('collection', items=[
        {'nodeid': item.nodeid, 'kind': type(item).__name__} for item in items
    ])

def pytest_runtest_setup(item):
    trace('hook-setup', nodeid=item.nodeid)

def pytest_runtest_call(item):
    trace('hook-call', nodeid=item.nodeid)

def pytest_runtest_teardown(item):
    trace('hook-teardown', nodeid=item.nodeid)

def pytest_runtest_logreport(report):
    context = getattr(report, 'context', None)
    trace('report', nodeid=report.nodeid, when=report.when, outcome=report.outcome,
          wasxfail=getattr(report, 'wasxfail', None),
          context=None if context is None else {'msg': context.msg, 'kwargs': dict(context.kwargs)})
    DIAGNOSTICS.append(str(report.longrepr) if report.longrepr else '')

def _check_unconsumed_exception_lifetime(when):
    module = sys.modules.get('test_lifetime')
    if module is not None:
        gc.collect()
        trace('unconsumed-error-token-lifetime', when=when,
              alive=[reference() is not None for reference in module.references])

def pytest_runtest_logfinish(nodeid, location):
    _check_unconsumed_exception_lifetime('logfinish')

def pytest_sessionfinish(session, exitstatus):
    _check_unconsumed_exception_lifetime('sessionfinish')
    Path('unittest-observations.json').write_text(json.dumps({
        'collected': session.testscollected, 'exit_status': int(exitstatus),
        'events': EVENTS, 'diagnostics': DIAGNOSTICS,
    }, indent=2))
"""


def _exception_lines(diagnostic: str) -> list[str]:
    """Return the exception lines of one report, or its last line when it has none."""

    lines = diagnostic.splitlines()
    return [line for line in lines if line.startswith("E   ")] or lines[-1:]


def _observe(root: Path, scenario: str, engine: str) -> dict[str, Any]:
    project = VfyProject(root)
    configuration = "[pytest]\n"
    if scenario == "collection":
        configuration += "python_classes = NeverMatches\npython_functions = never_matches_*\n"
    project.write("pytest.ini", configuration)
    project.write("conftest.py", _OBSERVER)
    project.write(f"test_{scenario}.py", (_FIXTURES / f"{scenario}.txt").read_text())
    arguments = ("-q", "--tb=short", "-rA", "-p", "no:cacheprovider")
    if engine == "stock":
        result = project.run_python("-m", "pytest", *arguments)
    else:
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            *arguments,
            env={"VFY_WORKER_START": "exec"},
        )
    observed = json.loads((root / "unittest-observations.json").read_text())
    # Each engine runs in its own directory, so the project root is normalized in output.
    outcome: dict[str, Any] = {
        "returncode": result.returncode,
        "collected": observed["collected"],
        "exit_status": observed["exit_status"],
        "events": observed["events"],
    }
    if scenario in _NATIVE_FRAME_SCENARIOS:
        outcome["exception_lines"] = [
            _exception_lines(project.normalize(text)) for text in observed["diagnostics"]
        ]
    else:
        outcome["stdout"] = project.normalize(result.stdout)
    return outcome


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_stdlib_testcase_public_phases_and_callbacks(tmp_path: Path, scenario: str) -> None:
    stock = _observe(tmp_path / "stock", scenario, "stock")
    independent = _observe(tmp_path / "independent", scenario, "independent")
    assert independent == stock


def test_class_collector_kind_survives_live_obj_replacement(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import unittest
import pytest

class Replacement(unittest.TestCase):
    def test_method(self):
        Path('ran-replacement').write_text('yes')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_pycollect_makeitem(collector, name, obj):
    if name == 'test_method' and collector.name == 'TestNormal':
        collector.obj = Replacement
    return (yield)

def pytest_collection_modifyitems(items):
    Path('items.json').write_text(json.dumps([
        [item.nodeid, type(item).__name__, type(item.parent).__name__] for item in items
    ]))
""",
    )
    project.write(
        "test_class.py",
        """class TestNormal:
    def test_method(self):
        raise RuntimeError('the replacement class should supply this method')

def test_other():
    pass
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((project.root / "items.json").read_text())
    assert expected == [
        ["test_class.py::TestNormal::test_method", "Function", "Class"],
        ["test_class.py::test_other", "Function", "Module"],
    ]
    assert (project.root / "ran-replacement").read_text() == "yes"
    (project.root / "ran-replacement").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((project.root / "items.json").read_text()) == expected
    assert (project.root / "ran-replacement").read_text() == "yes"
