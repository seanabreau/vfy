"""The ordinary unraisable provider reports real finalizer errors like stock pytest."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult

pytestmark = requires_native_pytest

_OBSERVER = r"""import atexit
import json
import os
import sys
from pathlib import Path
import pytest

mode = os.environ['OBSERVER_MODE']
events = []

def previous(args):
    events.append(['previous', type(args.exc_value).__name__, str(args.exc_value)])
sys.unraisablehook = previous
atexit.register(lambda: Path('observed.json').write_text(json.dumps(events, indent=2) + '\n'))
serial = 0
class DestructorError(Exception):
    pass
class ReprError(Exception):
    pass
class Finalizer:
    def __get__(self, instance, owner):
        return self
    def __call__(self):
        global serial
        serial += 1
        raise DestructorError('actual-finalizer-' + str(serial))
    def __repr__(self):
        if mode == 'repr-error':
            raise ReprError('actual repr error')
        return 'actual-finalizer'
class Victim:
    __del__ = Finalizer()
def shape(error):
    row = [type(error).__name__, str(error)]
    if isinstance(error, BaseExceptionGroup):
        row.append([shape(child) for child in error.exceptions])
    if error.__cause__ is not None:
        row.append(['cause', shape(error.__cause__)])
    if error.__context__ is not None and error.__context__ is not error.__cause__:
        row.append(['context', shape(error.__context__)])
    return row
def make(cyclic=False):
    victim = Victim()
    if cyclic:
        victim.loop = victim

def pytest_sessionstart(session):
    if mode in ('cleanup-error', 'cleanup-warning'):
        def cleanup():
            events.append('user-cleanup')
            make()
            if mode == 'cleanup-error':
                raise RuntimeError('actual cleanup error')
        session.config.add_cleanup(cleanup)
    if mode == 'unregister':
        plugin = session.config.pluginmanager.get_plugin('unraisableexception')
        session.config.pluginmanager.unregister(plugin)
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    if call.excinfo is not None:
        events.append(['exception', call.when, shape(call.excinfo.value)])
    return (yield)
def pytest_warning_recorded(warning_message, when):
    events.append(['warning', when, warning_message.category.__name__,
                   str(warning_message.message)])
"""

_TEST = r"""import gc
import observer

def test_case():
    if observer.mode == 'cyclic':
        gc.disable()
        observer.make(cyclic=True)
    elif observer.mode in ('multiple',):
        observer.make()
        observer.make()
    elif observer.mode not in ('cleanup-error', 'cleanup-warning'):
        observer.make()
    observer.events.append('test-end')
"""

_MODES = (
    "default",
    "ignore",
    "error",
    "multiple",
    "cyclic",
    "repr-error",
    "disabled",
    "unregister",
    "cleanup-error",
    "cleanup-warning",
)

# CPython 3.14 formats the deallocator in err_msg and passes object=None.
_FORMATTED_DEALLOCATOR = sys.version_info >= (3, 14)
_CAUSE_MESSAGE = (
    "Exception ignored while calling deallocator actual-finalizer: None\n"
    if _FORMATTED_DEALLOCATOR
    else (
        "Exception ignored in: actual-finalizer\n"
        "Enable tracemalloc to get traceback where the object was allocated.\n"
        "See https://docs.pytest.org/en/stable/how-to/capture-warnings.html#resource-warnings "
        "for more info."
    )
)


_RAISING = {"cyclic", "unregister", "cleanup-error", "cleanup-warning"}


def _assert_status(result: CommandResult, mode: str) -> None:
    returns = {"error": 1, "multiple": 1, "repr-error": 0 if _FORMATTED_DEALLOCATOR else 1}
    if mode in _RAISING:
        assert result.returncode != 0, result.stdout + result.stderr
    else:
        assert result.returncode == returns.get(mode, 0), result.stdout + result.stderr


def _assert_rows(rows: list[object], mode: str) -> None:
    errors = [row for row in rows if isinstance(row, list) and row[0] == "exception"]
    if mode == "multiple":
        assert len(errors) == 1
        assert errors[0][1:3] == [
            "call",
            [
                "ExceptionGroup",
                "multiple unraisable exception warnings (2 sub-exceptions)",
                [
                    [
                        "PytestUnraisableExceptionWarning",
                        _CAUSE_MESSAGE,
                        ["cause", ["DestructorError", "actual-finalizer-2"]],
                    ],
                    [
                        "PytestUnraisableExceptionWarning",
                        _CAUSE_MESSAGE,
                        ["cause", ["DestructorError", "actual-finalizer-1"]],
                    ],
                ],
            ],
        ]
    elif mode == "repr-error" and not _FORMATTED_DEALLOCATOR:
        assert errors == [
            [
                "exception",
                "call",
                [
                    "RuntimeError",
                    "Failed to process unraisable exception",
                    ["cause", ["ReprError", "actual repr error"]],
                ],
            ]
        ]
    elif mode == "error":
        assert errors == [
            [
                "exception",
                "call",
                [
                    "PytestUnraisableExceptionWarning",
                    _CAUSE_MESSAGE,
                    ["cause", ["DestructorError", "actual-finalizer-1"]],
                ],
            ]
        ]
    else:
        assert errors == []
    if mode == "disabled":
        assert ["previous", "DestructorError", "actual-finalizer-1"] in rows
    if mode in ("cleanup-error", "cleanup-warning"):
        assert "user-cleanup" in rows


def _assert_warning_count(rows: list[object], mode: str) -> None:
    warnings = [row for row in rows if isinstance(row, list) and row[0] == "warning"]
    expected = 1 if mode == "default" else 0
    if mode == "repr-error" and _FORMATTED_DEALLOCATOR:
        expected = 1
    assert len(warnings) == expected


@pytest.mark.parametrize("mode", _MODES)
def test_unraisable_finalizer_errors_match_stock_reports_and_warnings(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("observer.py", _OBSERVER)
    project.write("test_case.py", _TEST)
    arguments = ["-q", "-p", "observer", "-p", "no:threadexception", "-p", "no:cacheprovider"]
    if mode == "disabled":
        arguments += ["-p", "no:unraisableexception"]
    if mode == "ignore":
        arguments += ["-W", "ignore::pytest.PytestUnraisableExceptionWarning"]
    elif mode in ("error", "multiple", "cyclic", "unregister", "cleanup-error", "cleanup-warning"):
        arguments += ["-W", "error::pytest.PytestUnraisableExceptionWarning"]
    arguments.append("test_case.py")
    snapshots = []
    statuses = []
    for engine in ("stock", "native"):
        result = project.run_engine(
            engine,
            *arguments,
            env={
                "OBSERVER_MODE": mode,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(project.root),
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
        )
        _assert_status(result, mode)
        rows = json.loads((project.root / "observed.json").read_text())
        _assert_rows(rows, mode)
        _assert_warning_count(rows, mode)
        if mode == "repr-error":
            if _FORMATTED_DEALLOCATOR:
                assert (
                    "PytestUnraisableExceptionWarning: Exception ignored in: None" in result.stdout
                )
                assert "observer.DestructorError: actual-finalizer-1" in result.stdout
                assert "actual repr error" not in result.stdout
            else:
                assert "Exception ignored in sys.unraisablehook" in result.stdout
                assert "actual repr error" in result.stdout
            assert result.stderr == ""
        snapshots.append(rows)
        statuses.append(result.returncode)
    assert snapshots[0] == snapshots[1]
    assert statuses[0] == statuses[1]
