"""Report property snapshots retain mutable identities and Python payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SNAPSHOT = """import json, pytest
from pathlib import Path

events = []
saved = []
class Source:
    def __init__(self, phase):
        self.phase = phase
        self.values = [('initial', phase)] if MODE == 'values' else []
    def __iter__(self):
        events.append(['iterate', self.phase])
        if MODE == 'error':
            raise ValueError('actual property iteration failure')
        yield from self.values
        events.append(['iterated', self.phase])

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    source = Source(call.when)
    item.user_properties = source
    try:
        report = yield
    except ValueError as error:
        events.append(['caught', call.when, str(error)])
        raise
    source.values.append(('too-late', call.when))
    first = report.user_properties
    assert type(first) is list and report.user_properties is first
    assert first == ([('initial', call.when)] if MODE == 'values' else [])
    events.append(['copied', call.when, list(first)])
    first.append(('mutated', call.when))
    assert report.user_properties is first
    first.clear()
    assert report.user_properties is first and first == []
    first.append(('restored', call.when))
    report.extra_property = call.when
    wire = report._to_json()
    assert wire['user_properties'] is first
    assert wire['extra_property'] == report.__dict__['extra_property'] == call.when
    copied = type(report)._from_json(wire)
    assert copied.user_properties == first and copied.user_properties is not first
    copied.user_properties.append(('copy-only', call.when))
    assert first == [('restored', call.when)]
    saved.append(report)
    return report

def pytest_sessionfinish():
    if MODE != 'error':
        assert len(saved) == 3
        assert len({id(report.user_properties) for report in saved}) == 3
        for report in saved:
            assert report.user_properties == [('restored', report.when)]
    Path('properties.json').write_text(json.dumps(events))
"""

_SETTER_AND_GC = """import gc, json, weakref, pytest
from pathlib import Path

events = []
class Payload:
    pass

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if MODE == 'setter':
        for label, value in [('none', None), ('tuple', ('actual', call.when)),
                             ('object', Payload()), ('list', [])]:
            report.user_properties = value
            assert report.user_properties is value
            wire = report._to_json()
            assert wire['user_properties'] is value
            events.append(['setter', call.when, label])
        first = report.user_properties
        first.append(('actual', call.when))
        first.clear()
        assert report.user_properties is first and first == []
    else:
        copy = type(report)._from_json(report._to_json())
        payload = Payload()
        payload.report = copy
        copy.user_properties.append(('cycle', payload))
        wire = copy._to_json()
        assert wire['user_properties'] is copy.user_properties
        copy_reference, payload_reference = weakref.ref(copy), weakref.ref(payload)
        del wire, copy, payload
        gc.collect()
        assert copy_reference() is None and payload_reference() is None
        events.append(['cycle-released', call.when])
    return report

def pytest_sessionfinish():
    Path('properties.json').write_text(json.dumps(events))
"""


def _observe(root: Path, mode: str, source: str, route: str) -> tuple[int, list[list[object]]]:
    project = VfyProject(root)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_value.py", "def test_value(): pass\n")
    project.write("conftest.py", f"MODE = {mode!r}\n" + source)
    result = (
        project.run_python("-m", "pytest", "-q")
        if route == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    )
    project.write("runner.stdout", result.stdout)
    project.write("runner.stderr", result.stderr)
    assert result.returncode == (3 if mode == "error" else 0), result.stdout + result.stderr
    path = root / "properties.json"
    assert path.exists(), result.stdout + result.stderr
    return result.returncode, cast("list[list[object]]", json.loads(path.read_text()))


@pytest.mark.parametrize("mode", ["empty", "values", "error"])
def test_report_properties_copy_eagerly_and_keep_empty_list_identity_and_wire_values(
    tmp_path: Path, mode: str
) -> None:
    expected = _observe(tmp_path / "stock", mode, _SNAPSHOT, "stock")
    assert expected[0] == (3 if mode == "error" else 0)
    assert expected[1]
    assert _observe(tmp_path / "candidate", mode, _SNAPSHOT, "candidate") == expected


@pytest.mark.parametrize("mode", ["setter", "cycle"])
def test_report_properties_preserve_arbitrary_setter_values_and_collect_cycles(
    tmp_path: Path, mode: str
) -> None:
    expected = _observe(tmp_path / "stock", mode, _SETTER_AND_GC, "stock")
    assert expected[0] == 0
    assert expected[1]
    assert _observe(tmp_path / "candidate", mode, _SETTER_AND_GC, "candidate") == expected
