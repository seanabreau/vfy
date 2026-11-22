"""Hook error protocols preserve exception identity, conversion failures, and release."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.hook_probe import run_manager_probe
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROBE = r"""import gc
import json
import weakref

import pytest

mode = case
events = []
refs = {}
marker = pytest.hookimpl
manager = registry
class Spec:
    @pytest.hookspec
    def pytest_case(): pass
manager.add_hookspecs(Spec)
def call(): return manager.hook.pytest_case()

class ConversionError(Exception):
    def __init__(self, message):
        super().__init__(message)
        refs['conversion'] = weakref.ref(self)

class InnerError(Exception):
    def __init__(self, message):
        super().__init__(message)
        refs['inner'] = weakref.ref(self)

class CauseError(RuntimeError):
    def __init__(self, message):
        super().__init__(message)
        refs['cause'] = weakref.ref(self)
    def __getattribute__(self, name):
        if name == '__cause__':
            raise ConversionError('cause conversion')
        return super().__getattribute__(name)

class OriginalError(Exception):
    def __init__(self, message):
        super().__init__(message)
        refs['original'] = weakref.ref(self)
    def __format__(self, spec):
        if mode in ('format-raises', 'format-context'):
            raise ConversionError('format conversion')
        return super().__format__(spec)
    def __str__(self):
        if mode == 'str-raises':
            raise ConversionError('str conversion')
        return 'original error'

class Body:
    @marker()
    def pytest_case(self):
        events.append('body')
        if mode == 'cause-raises':
            raise StopIteration('body stop')
        if mode == 'traceback-pass':
            raise OriginalError('body')
        return 'body-value'

class Legacy:
    @marker(hookwrapper=True)
    def pytest_case(self):
        events.append('legacy.enter')
        outcome = yield
        events.append(['legacy.resume', type(outcome.exception).__name__])
        if mode == 'traceback-pass':
            outcome.force_result('forced')
        elif mode == 'format-context':
            try:
                raise InnerError('inner teardown error')
            except InnerError:
                raise OriginalError('teardown')
        else:
            raise OriginalError('teardown')

class CauseWrapper:
    @marker(wrapper=True)
    def pytest_case(self):
        try:
            yield
        except StopIteration as original:
            raise CauseError('cause wrapper') from original

class Outer:
    @marker(wrapper=True, tryfirst=True)
    def pytest_case(self):
        try:
            value = yield
        except BaseException as error:
            kind = 'conversion'
            context = refs['cause' if mode == 'cause-raises' else 'original']()
            events.append(['outer.error', type(error).__name__, error is refs[kind](),
                           error.__context__ is context])
            if mode == 'format-context':
                events.append(['original.context', context.__context__ is refs['inner']()])
            return 'recovered'
        events.append(['outer.value', value])
        return value

middle = CauseWrapper() if mode == 'cause-raises' else Legacy()
for name, plugin in [('body', Body()), ('middle', middle), ('outer', Outer())]:
    manager.register(plugin, name)

if mode == 'format-context':
    try:
        raise RuntimeError('ambient caller error')
    except RuntimeError:
        value = call()
else:
    value = call()
events.append(['returned', value])
gc.collect()
events.append(['released', {name: ref() is None for name, ref in refs.items()}])
print(json.dumps(events))
"""

_MODES = ["str-raises", "format-raises", "traceback-pass", "cause-raises", "format-context"]


@pytest.mark.parametrize("mode", _MODES)
def test_hook_error_protocols_match_stock_pytest(tmp_path: Path, mode: str) -> None:
    stock, independent = run_manager_probe(tmp_path, _PROBE, mode)
    snapshots = [json.loads(output.strip().splitlines()[-1]) for output in (stock, independent)]
    assert snapshots[0] == snapshots[1]
    events = snapshots[1]
    if mode == "traceback-pass":
        assert ["legacy.resume", "OriginalError"] in events
        assert ["outer.value", "forced"] in events
    else:
        assert ["outer.error", "ConversionError", True, True] in events
        if mode == "format-context":
            assert ["original.context", True] in events
    assert events[-1][0] == "released"
    assert all(events[-1][1].values())
