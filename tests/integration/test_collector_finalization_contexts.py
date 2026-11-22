"""Reference release and cyclic collection preserve complete coverage contexts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_collector_generator_close import _Snapshot, _snapshot
from tests.support import VfyProject

_SUBJECT = """from functools import partial
import sys
from types import ModuleType
import weakref

def leaf():
    yield 'first'
    yield 'second'

def unmatched(log):
    try:
        yield from leaf()
    except Exception:
        log.append('caught')

class BufferedCycle:
    def __init__(self, log):
        self._gen = unmatched(log)
        self._next = partial(next, self._buffered())

    def _buffered(self):
        while True:
            yield next(self._gen)

    def __next__(self):
        return self._next()

class Owner:
    pass

def make_module_owner(name, cyclic):
    owner = Owner()
    module = ModuleType(name)
    sys.modules[name] = weakref.proxy(module, lambda unused: sys.modules.pop(name, None))
    owner.module = module
    if cyclic:
        module.owner = owner
    return owner
"""

_DRIVER = """import gc
import hashlib
from importlib import import_module
from importlib.machinery import EXTENSION_SUFFIXES
import json
from pathlib import Path
import sys
import weakref

provider, destination, branch, scenario = sys.argv[1:]
coverage = import_module(provider).Coverage(
    data_file=destination, branch=branch == '1', source=['subject'], config_file=False)
coverage.set_option('run:core', 'ctrace')
coverage.start()
try:
    trace = sys.gettrace()
    identity = {
        'trace_module': None if trace is None else type(trace).__module__,
        'trace_name': None if trace is None else type(trace).__name__,
        'requested_core': None,
        'extension_origin': None,
        'extension_sha256': None,
    }
    if provider == 'coverage':
        tracer = import_module('coverage.tracer')
        origin = Path(tracer.__file__).resolve()
        assert type(trace) is tracer.CTracer
        assert any(str(origin).endswith(suffix) for suffix in EXTENSION_SUFFIXES)
        identity.update({
            'requested_core': coverage.get_option('run:core'),
            'extension_origin': str(origin),
            'extension_sha256': hashlib.sha256(origin.read_bytes()).hexdigest(),
        })
        assert identity['requested_core'] == 'ctrace'
        assert identity['trace_name'] == 'CTracer'
        assert identity['trace_module'].split('.')[0] == 'coverage'
    import subject
    coverage.switch_context('create-and-suspend')
    log = []
    cyclic = scenario.endswith('-cycle')
    is_module = scenario.startswith('module-')
    module_name = 'lifetime_subject_module'
    assert module_name not in sys.modules
    if is_module:
        value = subject.make_module_owner(module_name, cyclic)
        references = [weakref.ref(value.module)]
        if cyclic:
            references.append(weakref.ref(value))
        assert sys.modules[module_name].__name__ == module_name
    elif cyclic:
        value = subject.BufferedCycle(log)
        references = [weakref.ref(value), weakref.ref(value._gen)]
        assert next(value) == 'first'
    else:
        value = subject.unmatched(log)
        references = [weakref.ref(value)]
        assert next(value) == 'first'
    before_release = [reference() is not None for reference in references]
    assert all(before_release)
    coverage.switch_context('reference-release')
    if is_module and not cyclic:
        value.module = None
    else:
        del value
    after_release = [reference() is not None for reference in references]
    if cyclic:
        # Natural GC may run earlier; both release operations use this same context.
        gc.collect()
    else:
        assert not any(after_release)
    after_collection = [reference() is not None for reference in references]
    assert not any(after_collection)
    assert module_name not in sys.modules
    assert log == []
    observations = {
        'before_release': before_release,
        'after_release': after_release,
        'explicit_collection': cyclic,
        'after_collection': after_collection,
        'module_registered': module_name in sys.modules,
        'log': log,
    }
finally:
    coverage.stop()
coverage.save()
Path(destination + '.producer.json').write_text(json.dumps(identity))
Path(destination + '.lifetime.json').write_text(json.dumps(observations))
"""


@pytest.mark.parametrize("branch", [False, True], ids=["line", "branch"])
@pytest.mark.parametrize(
    "scenario", ["generator-release", "generator-cycle", "module-release", "module-cycle"]
)
def test_finalization_preserves_lines_arcs_and_manual_contexts(
    tmp_path: Path, branch: bool, scenario: str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("subject.py", _SUBJECT)
    project.write("driver.py", _DRIVER)
    snapshots: dict[str, _Snapshot] = {}
    observations = {}
    for provider, destination in (
        ("coverage", "reference.coverage"),
        ("vfy.api", "actual.coverage"),
    ):
        result = project.run_python("driver.py", provider, destination, str(int(branch)), scenario)
        result.expect(0)
        snapshots[provider] = _snapshot(source, tmp_path / destination)
        observations[provider] = json.loads(
            (tmp_path / (destination + ".lifetime.json")).read_text()
        )
    reference = json.loads((tmp_path / "reference.coverage.producer.json").read_text())
    assert reference["requested_core"] == "ctrace"
    assert reference["trace_name"] == "CTracer"
    assert reference["extension_origin"]
    assert reference["extension_sha256"]
    expected = snapshots["coverage"]
    assert expected["lines"]
    assert expected["contexts_by_lineno"]
    assert "create-and-suspend" in expected["context_arcs"]
    if branch:
        assert expected["arcs"]
        assert expected["context_arcs"]["create-and-suspend"]
    assert snapshots["vfy.api"] == expected, observations
