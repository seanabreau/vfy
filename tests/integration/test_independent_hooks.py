"""Independent hook dispatch follows the pluggy call protocol that pytest plug-ins observe."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROBE = """\
import json
from pathlib import Path

import pytest

mode = MODE
events = []

class Spec:
    @pytest.hookspec(firstresult=mode == "first")
    def pytest_case(value): pass

def pytest_addhooks(pluginmanager):
    pluginmanager.add_hookspecs(Spec)

class Plain:
    def __init__(self, label): self.label = label
    @pytest.hookimpl()
    def pytest_case(self, value):
        events.append(self.label)
        if mode == "error": raise ValueError("inner")
        return value + self.label
class Outer:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_case(self):
        events.append("outer enter")
        try: result = yield
        except RuntimeError as error:
            events.append(str(error))
            return "recovered"
        events.append(["outer exit", result])
        return result
class Legacy:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_case(self):
        events.append("legacy enter")
        result = yield
        if result.exception:
            events.append(["legacy error", str(result.exception)])
            result.force_exception(RuntimeError("replacement"))
        else:
            events.append(["legacy exit", result.get_result()])
class First:
    def __init__(self, label): self.label = label
    @pytest.hookimpl(tryfirst=True)
    def pytest_case(self, value="default"): events.append([self.label, value])
class Last:
    def __init__(self, label): self.label = label
    @pytest.hookimpl(trylast=True)
    def pytest_case(self): events.append(self.label)

def pytest_configure(config):
    manager = config.pluginmanager
    plugins = [Plain("a"), Plain("b"), First("first a"), First("first b"),
               Last("last a"), Last("last b"), Outer(), Legacy()]
    for index, plugin in enumerate(plugins):
        manager.register(plugin, str(index))
    value = config.hook.pytest_case(value="v")
    assert manager.get_plugin("0") is not None
    manager.unregister(name="0")
    assert manager.get_plugin("0") is None
    Path("probe.json").write_text(json.dumps([value, events]))
"""

_FIRST_RESULT_PROBE = """\
import gc, json, weakref
from pathlib import Path

import pytest

mode = MODE
events = []
reference = None

class Spec:
    @pytest.hookspec(firstresult=True)
    def pytest_case(): pass

def pytest_addhooks(pluginmanager):
    pluginmanager.add_hookspecs(Spec)

class Value:
    def __del__(self): events.append('released')
class WitnessError(Exception): pass
class NoneProvider:
    @pytest.hookimpl(tryfirst=True)
    def pytest_case(self): events.append('none')
class Body:
    @pytest.hookimpl()
    def pytest_case(self):
        global reference
        value = Value()
        reference = weakref.ref(value)
        events.append('body')
        return value
class Uncalled:
    @pytest.hookimpl(trylast=True)
    def pytest_case(self): raise AssertionError('first-result dispatch did not stop')
class Inner:
    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_case(self):
        value = yield
        events.append(['inner', reference() is value])
        del value
        if mode == 'raise': raise WitnessError('replacement')
        return 'replacement'
class Legacy:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_case(self):
        outcome = yield
        events.append(['legacy', reference() is not None,
                       type(outcome.exception).__name__])
        if outcome.exception is not None: outcome.force_result('recovered')
class Outer:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_case(self):
        value = yield
        events.append(['outer', reference() is not None, value])
        return value

def pytest_configure(config):
    for plugin in [NoneProvider(), Body(), Uncalled(), Inner(), Legacy(), Outer()]:
        config.pluginmanager.register(plugin)
    value = config.hook.pytest_case()
    events.append(['returned', value])
    gc.collect()
    events.append(['alive', reference() is not None])
    Path('probe.json').write_text(json.dumps(events))
"""


def _observe(tmp_path: Path, probe: str, mode: str) -> list[str]:
    """Run one probe under stock pytest and vfy and return both recorded results."""

    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", probe.replace("MODE", repr(mode), 1))
        project.write("test_body.py", "def test_body():\n    pass\n")
        result = project.run_engine(engine, "-q")
        result.expect(0)
        observations.append((project.root / "probe.json").read_text())
    return observations


@pytest.mark.parametrize("mode", ["all", "first", "error"])
def test_independent_hooks_match_stock_pluggy(tmp_path: Path, mode: str) -> None:
    stock, independent = _observe(tmp_path, _PROBE, mode)

    assert independent == stock


@pytest.mark.parametrize("mode", ["replace", "raise"])
def test_first_result_survives_replacement_until_all_wrappers_finish(
    tmp_path: Path, mode: str
) -> None:
    stock, independent = _observe(tmp_path, _FIRST_RESULT_PROBE, mode)

    assert independent == stock
