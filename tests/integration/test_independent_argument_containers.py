"""Real traceback arguments preserve nested safe-repr bounds and handled scopes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_EXPECTED_NESTED = {
    False: {
        "payload": (
            "{'nested': [[0, 1, 2, 3, 4, 5, ...], {'environment': {'PREFI"
            "X_0': 'value-0-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.."
            ".xxxxx', 'PREFIX_3': 'value-3-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', ...}}]}"
        ),
        "kwargs": (
            "{'cwd': 'working directory', 'text': True, 'env': {'PREFIX_0"
            "': 'value-0-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.."
            ".': 'value-3-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', ...}, 'stdout': -1, ...}"
        ),
    },
    True: {
        "payload": (
            "{'nested': [[0, 1, 2, 3, 4, 5, ...], {'environment': {'PREFI"
            "X_0': 'value-0-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'PREFIX_1': 'value-1-x"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxx', 'PREFIX_2': 'value-2-xxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxx', 'PREFIX_3': 'value-3-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', ...}}]}"
        ),
        "kwargs": (
            "{'cwd': 'working directory', 'text': True, 'env': {'PREFIX_0"
            "': 'value-0-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'PREFIX_1': 'value-1-xxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxx', 'PREFIX_2': 'value-2-xxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "', 'PREFIX_3': 'value-3-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', ...}, 'stdout"
            "': -1, ...}"
        ),
    },
}


@pytest.mark.parametrize("runner", ["stock", "independent"])
@pytest.mark.parametrize("kind", ["nested", "exception_context"])
@pytest.mark.parametrize("verbose", [False, True])
def test_real_traceback_arguments_use_nested_safe_representations(
    tmp_path: Path, runner: str, kind: str, verbose: bool
) -> None:
    project = VfyProject(tmp_path, interpreter=sys.executable)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "control.py",
        """import sys

KIND = CASE_KIND
events = []

class ReprFailure(Exception):
    def __repr__(self):
        events.append(['error.repr.handled', sys.exception() is self])
        return 'controlled argument error'

class Bad:
    def __repr__(self):
        events.append(['bad.repr'])
        raise ReprFailure('controlled representation failure')

bad = Bad()
environment = {f'PREFIX_{index}': f'value-{index}-' + 'x' * 80 for index in range(12)}
environment['VFY_TEST_RESULTS'] = 'legitimate-result-file'
payload = {'nested': [list(range(20)), {'environment': environment}]}
expected_custom = None
if KIND == 'exception_context':
    payload = {'nested': [bad]}
    expected_bad = ('<[controlled argument error raised in repr()] Bad object at '
                    + hex(id(bad)) + '>')
    expected_custom = "{'nested': [" + expected_bad + ']}'
""".replace("CASE_KIND", repr(kind)),
    )
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import control

def pytest_runtest_logreport(report):
    if report.when != 'call':
        return
    entries = report.longrepr.reprtraceback.reprentries
    arguments = {}
    for entry in entries:
        funcargs = entry.reprfuncargs
        if funcargs is not None:
            for name, value in funcargs.args:
                if name in {'payload', 'kwargs'}:
                    arguments[name] = value
    Path('argument-observed.json').write_text(json.dumps({
        'outcome': report.outcome,
        'arguments': arguments,
        'events': control.events,
        'custom_expected': control.expected_custom,
        'environment_keys': list(control.environment),
        'environment_tail_value': control.environment['VFY_TEST_RESULTS'],
        'diagnostic': report.longreprtext,
    }))
""",
    )
    project.write(
        "test_body.py",
        """import control

def sink(payload, **kwargs):
    raise ValueError('actual argument representation failure')

def test_body():
    sink(control.payload, cwd='working directory', text=True,
         env=control.environment, stdout=-1, stderr=-1)
""",
    )
    arguments = ["-vvv" if verbose else "-q", "--tb=long"]
    if runner == "stock":
        result = project.run_python("-m", "pytest", *arguments)
    else:
        result = project.run_vfy("test", "--engine=native", "--no-cov", *arguments)
    project.write("child.stdout", result.stdout)
    project.write("child.stderr", result.stderr)
    result.expect(1)
    observed = json.loads((tmp_path / "argument-observed.json").read_text())
    assert observed["outcome"] == "failed"
    assert set(observed["arguments"]) == {"payload", "kwargs"}
    assert observed["environment_keys"] == [
        *[f"PREFIX_{index}" for index in range(12)],
        "VFY_TEST_RESULTS",
    ]
    assert observed["environment_tail_value"] == "legitimate-result-file"
    if kind == "exception_context":
        assert observed["arguments"]["payload"] == observed["custom_expected"]
        assert observed["events"] == [["bad.repr"], ["error.repr.handled", True]]
    assert observed["arguments"]["kwargs"] == _EXPECTED_NESTED[verbose]["kwargs"]
    if kind == "nested":
        assert observed["arguments"]["payload"] == _EXPECTED_NESTED[verbose]["payload"]
    assert "PREFIX_0" in observed["arguments"]["kwargs"]
    assert "PREFIX_11" not in observed["arguments"]["kwargs"]
    assert "VFY_TEST_RESULTS" not in observed["arguments"]["kwargs"]
    assert observed["arguments"]["kwargs"].endswith("'stdout': -1, ...}")
