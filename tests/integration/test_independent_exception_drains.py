"""Exception providers preserve foreign queue callbacks and metadata lifetimes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_TEST = r"""import importlib
import json
from pathlib import Path
import warnings
import weakref
import pytest
from selection import KIND

def test_foreign_queue(pytestconfig):
    provider = importlib.import_module('_pytest.' + KIND + 'exception')
    collect = getattr(provider, 'collect_' + ('thread_exception' if KIND == 'thread' else KIND))
    key = getattr(provider, KIND + '_exceptions')
    category = (pytest.PytestUnhandledThreadExceptionWarning if KIND == 'thread'
                else pytest.PytestUnraisableExceptionWarning)
    assert pytestconfig.pluginmanager.get_plugin(KIND + 'exception') is not None
    original = pytestconfig.stash[key]
    observed = {}
    try:
        for mode in ('empty', 'warnings', 'first-error', 'later-error'):
            events = []
            references = []
            class Metadata:
                exc_value = None
                def __init__(self, number):
                    self.number = number
                    references.append(weakref.ref(self))
                @property
                def msg(self):
                    events.append(['message', self.number])
                    if mode == 'warnings' and self.number == 1:
                        queue.pending.append(Metadata(2))
                    return 'warning-' + str(self.number)
                def __del__(self):
                    events.append(['released', self.number])
            class Queue:
                def __init__(self):
                    self.pending = [Metadata(1)] if mode in ('warnings', 'later-error') else []
                    self.calls = 0
                @property
                def pop(self):
                    events.append('bind')
                    return self.next
                def next(self):
                    self.calls += 1
                    events.append(['pop', self.calls, [ref() is not None for ref in references]])
                    if self.pending:
                        return self.pending.pop()
                    if mode.endswith('error'):
                        raise LookupError('foreign pop failure')
                    raise IndexError
            queue = Queue()
            pytestconfig.stash[key] = queue
            with warnings.catch_warnings():
                warnings.simplefilter('error', category)
                try:
                    collect(pytestconfig)
                except ExceptionGroup as error:
                    events.append(['group', [str(child) for child in error.exceptions]])
                except LookupError as error:
                    events.append(['error', str(error)])
            events.append(['alive', [ref() is not None for ref in references]])
            observed[mode] = events
    finally:
        pytestconfig.stash[key] = original
    Path('observed.json').write_text(json.dumps(observed))
"""


@pytest.mark.parametrize("kind", ["thread", "unraisable"])
def test_exception_drain_preserves_foreign_pop_and_metadata_lifetimes(
    tmp_path: Path, kind: str
) -> None:
    observed = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("selection.py", f"KIND = {kind!r}\n")
        project.write("test_foreign.py", _TEST)
        result = (
            project.run_python("-m", "pytest", "-q", "-p", "no:cacheprovider")
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "observed.json").read_text())
    assert observed["stock"]["empty"] == ["bind", ["pop", 1, []], ["alive", []]]
    assert observed["stock"]["first-error"] == [
        "bind",
        ["pop", 1, []],
        ["error", "foreign pop failure"],
        ["alive", []],
    ]
    assert observed["stock"]["warnings"][-2:] == [
        ["group", ["warning-1", "warning-2"]],
        ["alive", [False, False]],
    ]
    assert observed["stock"]["later-error"][-2:] == [
        ["error", "foreign pop failure"],
        ["alive", [False]],
    ]
    assert observed["native"] == observed["stock"]
