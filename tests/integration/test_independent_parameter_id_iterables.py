"""Parameter IDs consume the iterable once, preserving pytest's timing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NotRequired, TypedDict, TypeGuard

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


class IdObservation(TypedDict):
    status: int
    nodeids: list[str]
    bodies: list[int]
    events: list[list[object]]
    remaining: NotRequired[list[str]]
    len_error_raised: bool
    iter_error_raised: bool


def valid_observation(value: object) -> TypeGuard[IdObservation]:
    if not isinstance(value, dict) or type(value.get("status")) is not int:
        return False
    for name in ["nodeids", "remaining"]:
        if name not in value and name == "remaining":
            continue
        values = value.get(name)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            return False
    bodies = value.get("bodies")
    events = value.get("events")
    return (
        isinstance(bodies, list)
        and all(type(item) is int for item in bodies)
        and isinstance(events, list)
        and all(isinstance(event, list) for event in events)
        and type(value.get("len_error_raised")) is bool
        and type(value.get("iter_error_raised")) is bool
    )


BRIDGE = """import traceback
events = []
nodeids = []
bodies = []
errors = []
trace = ''
generator = None
class IdError(Exception): pass
len_error = IdError('ids length failed')
iter_error = IdError('ids iteration failed')

class Values(list):
    def __iter__(self):
        events.append(['values_iter'])
        return super().__iter__()
    def __len__(self):
        events.append(['values_len'])
        return super().__len__()

class SizedIds:
    def __len__(self):
        events.append(['ids_len'])
        if mode == 'len_error': raise len_error
        if mode == 'unsized': raise TypeError('ids have no size')
        return 1 if mode == 'bad_length' else 2
    def __iter__(self):
        events.append(['ids_iter'])
        if mode == 'iter_error': raise iter_error
        return iter(['alpha', 'beta'])
    def __getitem__(self, key):
        global trace
        events.append(['ids_getitem', key])
        try: return {'alpha': 1, 'beta': 2}[key]
        except KeyError:
            trace = ''.join(traceback.format_stack()) + traceback.format_exc()
            raise

def make_ids():
    global generator
    if mode == 'none': return None
    if mode == 'dict': return {'alpha': 10, 'beta': 20}
    if mode == 'empty': return []
    if mode in ['short_generator', 'long_generator']:
        def labels():
            for label in (['alpha'] if mode == 'short_generator' else ['alpha', 'beta', 'gamma']):
                events.append(['ids_next', label])
                yield label
        generator = labels()
        return generator
    if mode == 'callable':
        def identify(value):
            events.append(['id_callable', value])
            return 'id' + str(value)
        return identify
    return SizedIds()
"""

PLUGIN = """import json
from pathlib import Path
import bridge
def pytest_collection_modifyitems(items):
    bridge.nodeids = [item.nodeid for item in items]
def pytest_collectreport(report):
    if report.failed: bridge.errors.append(str(report.longrepr))
def pytest_unconfigure():
    observed = {'nodeids': bridge.nodeids, 'bodies': bridge.bodies,
                'events': list(bridge.events)}
    if bridge.generator is not None:
        observed['remaining'] = list(bridge.generator)
    observed['len_error_raised'] = bridge.len_error.__traceback__ is not None
    observed['iter_error_raised'] = bridge.iter_error.__traceback__ is not None
    Path('observations.json').write_text(json.dumps(observed, indent=2))
    Path('collection-errors.txt').write_text('\\n'.join(bridge.errors))
    Path('protocol-trace.txt').write_text(bridge.trace)
"""


def project(tmp_path: Path, mode: str, kind: str) -> VfyProject:
    result = VfyProject(tmp_path)
    result.write("pytest.ini", "[pytest]\n")
    result.write("bridge.py", f"mode = {mode!r}\n" + BRIDGE)
    result.write("conftest.py", PLUGIN)
    values = "[pytest.param(1, id='fixed'), 2]" if mode == "callable" else "[1, 2]"
    body = f"import bridge, pytest\nvalues = bridge.Values({values})\nids = bridge.make_ids()\n"
    if kind == "fixture":
        body += (
            "@pytest.fixture(params=values, ids=ids)\n"
            "def value(request): return request.param\n"
            "def test_value(value): bridge.bodies.append(value)\n"
        )
    else:
        body += (
            "@pytest.mark.parametrize('value', values, ids=ids)\n"
            "def test_value(value): bridge.bodies.append(value)\n"
        )
    result.write("test_values.py", body)
    return result


def observe(subject: VfyProject, *, native: bool) -> IdObservation:
    role = "candidate" if native else "stock"
    result = subject.run_engine(
        "native" if native else "stock",
        "-p",
        "no:terminal",
        "test_values.py",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    subject.write(f"{role}.stdout", result.stdout)
    subject.write(f"{role}.stderr", result.stderr)
    for name in ["observations.json", "collection-errors.txt", "protocol-trace.txt"]:
        path = subject.root / name
        assert path.exists(), name + result.stdout + result.stderr
        subject.write(f"{role}-{name}", path.read_text())
        path.unlink()
    observed = json.loads((subject.root / f"{role}-observations.json").read_text())
    observed["status"] = result.returncode
    assert valid_observation(observed), observed
    return observed


def active_probe_project(tmp_path: Path) -> VfyProject:
    subject = project(tmp_path, "unsized", "decorator")
    source = (tmp_path / "bridge.py").read_text()
    source = "import sys\n" + source.replace(
        "events.append(['ids_iter'])",
        "events.append(['ids_iter', type(sys.exception()).__name__ "
        "if sys.exception() is not None else None])",
    )
    source = source.replace("return iter(['alpha', 'beta'])", "return DropIterator()")
    source += """
class DropIterator:
    def __init__(self): self.values = iter(['alpha', 'beta'])
    def __iter__(self): return self
    def __next__(self): return next(self.values)
    def __del__(self):
        events.append(['iterator_drop', type(sys.exception()).__name__
                       if sys.exception() is not None else None])
"""
    subject.write("bridge.py", source)
    return subject


def test_unsized_id_probe_preserves_the_active_length_error(tmp_path: Path) -> None:
    subject = active_probe_project(tmp_path)
    expected = observe(subject, native=False)
    actual = observe(subject, native=True)
    assert actual == expected
    assert [event for event in expected["events"] if event[0] == "ids_iter"] == [
        ["ids_iter", "TypeError"],
        ["ids_iter", None],
    ]
    assert [event for event in expected["events"] if event[0] == "iterator_drop"] == [
        ["iterator_drop", "TypeError"],
        ["iterator_drop", None],
    ]


MODES = (
    "none",
    "dict",
    "sized",
    "unsized",
    "short_generator",
    "long_generator",
    "empty",
    "bad_length",
    "len_error",
    "iter_error",
    "callable",
)


@pytest.mark.parametrize("kind", ["decorator", "fixture"])
@pytest.mark.parametrize("mode", MODES)
def test_parameter_id_iterables_preserve_reference_protocol(
    tmp_path: Path, kind: str, mode: str
) -> None:
    subject = project(tmp_path, mode, kind)
    expected = observe(subject, native=False)
    actual = observe(subject, native=True)
    assert _outcome(actual) == _outcome(expected)
    if expected["status"] == 0:
        assert expected["bodies"] == [1, 2]
        assert len(expected["nodeids"]) == 2
    else:
        assert expected["status"] == 2
        assert expected["bodies"] == []
    if (kind == "decorator" and mode == "bad_length") or (
        kind == "fixture" and mode in {"short_generator", "long_generator"}
    ):
        count = 3 if mode == "long_generator" else 1
        message = (
            "In test_values.py::test_value: 2 parameter sets specified, "
            f"with different number of ids: {count}"
        )
        for role in ["stock", "candidate"]:
            assert message in (tmp_path / f"{role}-collection-errors.txt").read_text()


def _outcome(observation: IdObservation) -> dict[str, object]:
    return {key: value for key, value in observation.items() if key != "events"}
