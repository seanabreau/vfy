"""The collection hook surrounds the complete native collection lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_RECORDER = """import json
from pathlib import Path

def record(*event):
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(event) + '\\n')

"""
_CASES = {
    "plain": (
        """import gc

def pytest_collection():
    record('collection')
    gc.disable()

def pytest_collection_finish():
    record('finish')
    gc.enable()
""",
        """import gc
record('import', gc.isenabled())

def test_one():
    record('call', gc.isenabled())
""",
        0,
        [["collection"], ["import", False], ["finish"], ["call", True]],
    ),
    "wrappers": (
        """import pytest

class Outer:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_collection(self, session):
        record('outer-before', session.testscollected)
        result = yield
        record('outer-after', result, session.testscollected)
        session.items.reverse()
        return result

class Inner:
    @pytest.hookimpl(hookwrapper=True, trylast=True)
    def pytest_collection(self, session):
        record('inner-before')
        outcome = yield
        record('inner-after', outcome.get_result())

class Last:
    @pytest.hookimpl(trylast=True)
    def pytest_collection(self):
        record('last')

def pytest_configure(config):
    for plugin in (Outer(), Inner(), Last()):
        config.pluginmanager.register(plugin)

def pytest_collection():
    record('plain')

def pytest_collection_modifyitems(session, items):
    record('modify', len(items), session.testscollected)

def pytest_collection_finish(session):
    record('finish', len(session.items), session.testscollected)
""",
        """record('import')

def test_one():
    record('call', 'one')

def test_two():
    record('call', 'two')
""",
        0,
        [
            ["outer-before", 0],
            ["inner-before"],
            ["plain"],
            ["import"],
            ["modify", 2, 0],
            ["finish", 2, 0],
            ["last"],
            ["inner-after", None],
            ["outer-after", None, 2],
            ["call", "two"],
            ["call", "one"],
        ],
    ),
    "firstresult": (
        """import pytest

class Observer:
    @pytest.hookimpl(wrapper=True)
    def pytest_collection(self):
        record('before')
        result = yield
        record('after', result)
        return result

def pytest_configure(config):
    config.pluginmanager.register(Observer())

@pytest.hookimpl(tryfirst=True)
def pytest_collection():
    record('short-circuit')
    return False

def pytest_collection_modifyitems():
    raise AssertionError('collection_modifyitems must be bypassed')

def pytest_collection_finish():
    raise AssertionError('collection_finish must be bypassed')
""",
        "raise AssertionError('default collection must be bypassed')\n",
        5,
        [["before"], ["short-circuit"], ["after", False]],
    ),
    "perform_collect": (
        """def pytest_collection(session):
    record('before')
    items = session.perform_collect()
    record('performed', items is session.items, len(items), session.testscollected)
    return True

def pytest_collection_modifyitems(items):
    record('modify', len(items))

def pytest_collection_finish(session):
    record('finish', session.testscollected)
""",
        """record('import')

def test_one():
    record('call')
""",
        0,
        [
            ["before"],
            ["import"],
            ["modify", 1],
            ["finish", 0],
            ["performed", True, 1, 1],
            ["call"],
        ],
    ),
    "finish_error_suppressed": (
        """import pytest
import sys

failure = ValueError('finish broke')

@pytest.hookimpl(wrapper=True)
def pytest_collection():
    record('before')
    try:
        yield
    except ValueError as error:
        record('handled', error is failure, sys.exception() is failure)
        return None

def pytest_collection_finish(session):
    record('finish', len(session.items), session.testscollected)
    raise failure
""",
        """record('import')

def test_one():
    record('call')
""",
        5,
        [["before"], ["import"], ["finish", 1, 0], ["handled", True, True], ["call"]],
    ),
    "collection_error": (
        """import pytest

@pytest.hookimpl(wrapper=True)
def pytest_collection():
    record('before')
    result = yield
    record('after', result)
    return result

def pytest_collection_modifyitems(items):
    record('modify', len(items))

def pytest_collection_finish(session):
    record('finish', len(session.items), session.testscollected)
""",
        "record('import')\nraise ValueError('collection broke')\n",
        2,
        [["before"], ["import"], ["modify", 0], ["finish", 0, 0], ["after", None]],
    ),
}


@pytest.mark.parametrize("scenario", _CASES)
def test_collection_hook_matches_pytest(tmp_path: Path, scenario: str) -> None:
    conftest, module, status, expected = _CASES[scenario]
    observations = []
    for engine in ("pytest", "vfy.independent.entry"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _RECORDER + conftest)
        project.write("test_hook.py", _RECORDER + module)
        result = project.run_python("-m", engine, "-q", env={"PYTHONPATH": _SOURCE})
        assert result.returncode == status, (engine, result.stdout, result.stderr)
        observations.append(
            [json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()]
        )
    assert observations[0] == expected
    assert observations[1] == observations[0]


@pytest.mark.parametrize(
    ("hook", "call", "diagnostic"),
    [
        (
            "pytest_collection",
            "session.perform_collect([])",
            "requires args=None and genitems=True",
        ),
        (
            "pytest_collection",
            "session.perform_collect(genitems=False)",
            "requires args=None and genitems=True",
        ),
        (
            "pytest_sessionstart",
            "session.perform_collect()",
            "is available during pytest_collection",
        ),
    ],
)
def test_perform_collect_rejects_unsupported_modes(
    tmp_path: Path, hook: str, call: str, diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", f"def {hook}(session):\n    {call}\n")
    project.write("test_unreached.py", "raise AssertionError('must reject before collection')\n")
    result = project.run_python("-m", "vfy.independent.entry", env={"PYTHONPATH": _SOURCE})
    result.expect(4)
    assert diagnostic in result.stderr
    assert "must reject before collection" not in result.stdout + result.stderr
