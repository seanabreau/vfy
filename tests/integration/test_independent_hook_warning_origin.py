"""Legacy hookwrapper teardown warnings match pluggy's category, message and filtering."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_LEGACY = """import pytest

class OriginalError(Exception):
    def __str__(self):
        return 'original error'

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield
    raise OriginalError()
"""

_OUTER = """import json
import os
import warnings
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    if os.environ.get('WARNING_MODE') == 'error':
        warnings.filterwarnings('error', message='A plugin raised an exception')
    try:
        value = yield
    except BaseException as error:
        events.append([type(error).__name__, type(error.__context__).__name__])
        return None
    events.append('value')
    return value

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
"""


@pytest.mark.parametrize("mode", ["record", "filter-error"])
def test_legacy_teardown_warning_matches_pluggy(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    project.write("legacy.py", _LEGACY)
    project.write("outer.py", _OUTER)
    project.write("test_one.py", "def test_one():\n    pass\n")
    arguments = ["-p", "legacy", "-p", "outer", "-q", "-p", "no:cacheprovider"]
    environment = {"WARNING_MODE": "error" if mode == "filter-error" else "record"}
    snapshots = []
    for engine in ("stock", "native"):
        result = project.run_engine(engine, *arguments, env=environment)
        events = json.loads((tmp_path / "events.json").read_text())
        (tmp_path / "events.json").unlink()
        snapshots.append((result.returncode, events, _normalized(result.stdout)))
    assert snapshots[0] == snapshots[1]
    _, events, stdout = snapshots[1]
    if mode == "record":
        assert "PluggyTeardownRaisedWarning: A plugin raised an exception" in stdout
        assert "  OriginalError: original error\n" in stdout
        assert events == [["OriginalError", "NoneType"]]
    else:
        assert events == [["PluggyTeardownRaisedWarning", "OriginalError"]]


def _normalized(text: str) -> str:
    # The warning location and source line name the engine frame that issued it.
    text = re.sub(
        r"\n  \S+:\d+: PluggyTeardownRaisedWarning",
        "\n  <location>: PluggyTeardownRaisedWarning",
        text,
    )
    text = re.sub(r"(pluggy\.PluggyTeardownRaisedWarning\n)    .*\n", r"\1", text)
    return re.sub(r" in [0-9.]+s", " in <time>", text)
