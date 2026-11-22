"""Assertion exception names follow the captured Python representation protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PREFIX_CASES = {
    "rewritten_integer": "def test_value():\n    assert 1 == 2\n",
    "rewritten_quoted_string": (
        "def test_value():\n"
        "    left = str({'foo': 0}) * 30\n"
        "    right = str({'FOO': 0}) * 30\n"
        "    assert left == right\n"
    ),
    "rewritten_single_quoted_string": (
        "def test_value():\n"
        "    left = 'prefix' * 30 + 'LEFT' + 'suffix' * 30\n"
        "    right = 'prefix' * 30 + 'RIGHT' + 'suffix' * 30\n"
        "    assert left == right\n"
    ),
    "literal_assert_message": (
        "def test_value():\n    raise AssertionError('assert literal message')\n"
    ),
    "literal_other_message": ("def test_value():\n    raise AssertionError('ordinary message')\n"),
    "assertion_subclass": (
        "class Child(AssertionError):\n    pass\n"
        "def test_value():\n    raise Child('assert subclass message')\n"
    ),
}

_PROTOCOL_HEADER = """import builtins

class AssertionError(builtins.AssertionError):
    __module__ = 'builtins'

    def __repr__(self):
        return self.representation

    def __str__(self):
        return 'assert actual message'
"""

_PROTOCOL_CASES = {
    "representation": "error.representation = \"AssertionError('assert captured')\"",
    "long_representation": (
        "error.representation = \"AssertionError('assert \" + 'x' * 500 + \"')\""
    ),
    "double_quoted_representation": (
        "error.representation = 'AssertionError(\"assert captured\")'"
    ),
    "message_precedes_representation": (
        "error.msg = \"AssertionError('assert captured')\"\n"
        "    error.representation = 'unused representation'"
    ),
    "literal_message_attribute": (
        "error.msg = 'assert captured'\n"
        "    error.representation = \"AssertionError('assert unused')\""
    ),
    "false_message": (
        "error.msg = Message(False)\n    error.representation = \"AssertionError('assert unused')\""
    ),
    "message_protocol": (
        "error.msg = Message(True)\n    error.representation = 'unused representation'"
    ),
    "representation_string_subclass": (
        "error.representation = ReprText(\"AssertionError('assert captured')\")"
    ),
    "representation_mutating_length": (
        "error.representation = ReprText(\"AssertionError('assert captured')\", mutate=True)"
    ),
    "representation_first_length_failure": (
        "error.representation = ReprText(\"AssertionError('assert captured')\", fail_at=1)"
    ),
}

_LIFETIME_CASES = {
    "stateful_text": "",
    "hidden_text": "    __tracebackhide__ = hide_always\n",
    "changing_hide": "    __tracebackhide__ = hide_after_display\n",
    "global_hide": "    globals()['__tracebackhide__'] = hide_always\n",
    "false_callable_hide": "    __tracebackhide__ = FalseHide()\n",
    "stateful_chain": "",
    "missing_traceback_cause": "",
    "failing_representation": "",
    "failing_failure_representation": "",
    "message_mutated_by_hook": "",
}

_MESSAGE_PROTOCOL = """
class LengthError(Exception):
    def __repr__(self):
        return 'length failed'

class ReprText(str):
    def __new__(cls, value, mutate=False, fail_at=0):
        result = super().__new__(cls, value)
        result.mutate = mutate
        result.fail_at = fail_at
        result.length_calls = 0
        return result

    def __len__(self):
        self.length_calls += 1
        if self.length_calls == self.fail_at:
            raise LengthError('length failure')
        if self.mutate and self.length_calls == 2:
            return 241
        return super().__len__()

    def __getitem__(self, index):
        if self.mutate:
            return 'changed representation'
        return super().__getitem__(index)

    def startswith(self, prefix):
        return super().startswith(prefix)

class Decision:
    def __bool__(self):
        return True

class Message:
    def __init__(self, enabled):
        self.enabled = enabled

    def __bool__(self):
        return self.enabled

    def startswith(self, prefix):
        return Decision()
"""


def _source(case: str) -> str:
    if case in _PREFIX_CASES:
        return _PREFIX_CASES[case]
    if case in _PROTOCOL_CASES:
        return (
            _PROTOCOL_HEADER
            + _MESSAGE_PROTOCOL
            + "\ndef test_value():\n    error = AssertionError()\n    "
            + _PROTOCOL_CASES[case]
            + "\n    error.add_note('retained note')\n    raise error\n"
        )
    setup = _LIFETIME_CASES[case]
    if case in {"failing_representation", "failing_failure_representation"}:
        setup += "    error.broken_repr = True\n"
    if case == "failing_failure_representation":
        setup += "    RepresentationError.broken_repr = True\n"
    if case == "message_mutated_by_hook":
        setup += "    error.msg = Message(True)\n"
    tail = "    raise error\n"
    if case == "stateful_chain":
        tail = (
            "    try:\n        raise error\n"
            "    except AssertionError as cause:\n"
            "        raise AssertionError('outer') from cause\n"
        )
    elif case == "missing_traceback_cause":
        tail = "    raise AssertionError('outer') from error\n"
    return (
        _PROTOCOL_HEADER
        + _MESSAGE_PROTOCOL
        + """
class RepresentationError(Exception):
    broken_repr = False

    def __repr__(self):
        if self.broken_repr:
            raise ValueError('nested representation failure')
        return 'representation failed'

    def __str__(self):
        return 'representation failure message'

class AssertionError(AssertionError):
    __module__ = 'builtins'

    def __init__(self, label):
        self.label = label
        self.calls = 0
        self.broken_repr = False

    def __repr__(self):
        if self.broken_repr:
            raise RepresentationError()
        return "AssertionError('assert captured')"

    def __str__(self):
        self.calls += 1
        return f'assert {self.label} text {self.calls}'

def hide_always(info):
    return True

class FalseHide:
    def __bool__(self):
        return False

    def __call__(self, info):
        return True

hide_calls = 0
def hide_after_display(info):
    global hide_calls
    hide_calls += 1
    return hide_calls > 1

def test_value():
    error = AssertionError('inner')
    error.add_note('retained note')
"""
        + setup
        + tail
    )


def _observe(project: VfyProject, *, candidate: bool) -> dict[str, object]:
    result = (
        project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "-q",
            "--native-result-json=" + str(project.root / "native.json"),
        )
        if candidate
        else project.run_python("-m", "pytest", "-q")
    )
    label = "candidate" if candidate else "stock"
    project.write(f"{label}.stdout", result.stdout)
    project.write(f"{label}.stderr", result.stderr)
    result.expect(1)
    path = project.root / "diagnostic.json"
    observed = cast("dict[str, object]", json.loads(path.read_text()))
    path.unlink()
    if candidate:
        native = json.loads((project.root / "native.json").read_text())
        observed["exception"] = next(
            phase["exception"]
            for case in native["cases"]
            for phase in case["phases"]
            if phase["outcome"] == "failed"
        )
    project.write(f"{label}-observed.json", json.dumps(observed, indent=2))
    return observed


@pytest.mark.parametrize("case", [*_PREFIX_CASES, *_PROTOCOL_CASES, *_LIFETIME_CASES])
def test_assertion_exception_names_match_stock(tmp_path: Path, case: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_value.py", _source(case))
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    if report.failed:
        crash = getattr(report.longrepr, 'reprcrash', None)
        exception = getattr(crash, 'message', None)
        Path('diagnostic.json').write_text(json.dumps({
            'nodeid': report.nodeid, 'when': report.when,
            'diagnostic': report.longreprtext,
            'exception': exception if isinstance(exception, str) else str(report.longrepr),
        }))

def pytest_runtest_makereport(item, call):
    if item.name == 'test_value' and call.excinfo is not None:
        error = call.excinfo.value
        if hasattr(error, 'msg'):
            error.msg = 'mutated after capture'
""",
    )
    expected = _observe(project, candidate=False)
    project.write("stock-diagnostic.json", json.dumps(expected, indent=2))
    assert _observe(project, candidate=True) == expected


def test_second_representation_length_failure_propagates_from_capture(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_value.py",
        _source("representation_first_length_failure").replace("fail_at=1", "fail_at=2"),
    )
    stock = project.run_python("-m", "pytest", "-q")
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    stock.expect(3)
    assert "LengthError: length failure" in stock.stdout + stock.stderr
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    project.write("candidate.stdout", candidate.stdout)
    project.write("candidate.stderr", candidate.stderr)
    assert candidate.returncode == stock.returncode, candidate.stdout + candidate.stderr
    assert "LengthError: length failure" in candidate.stdout + candidate.stderr
