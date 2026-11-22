"""Scoped hooks and invocation observations follow the active dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.hook_probe import run_manager_probe
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SELECTION = """\
import json
import pytest
events = []
marker = pytest.hookimpl
spec = pytest.hookspec
class Specs:
    @spec(firstresult=case == "firstresult")
    def pytest_case(self, value): pass
registry.add_hookspecs(Specs)
class Core:
    @marker()
    def pytest_case(self, value):
        events.append(["core", value])
        return "core"
core = Core()
if case == "core": registry.register(core, "core-runner")
class Plugin:
    def __init__(self, label): self.label = label
    @marker()
    def pytest_case(self, value):
        events.append([self.label, value])
        return self.label
class First:
    @marker(tryfirst=True)
    def pytest_case(self, value): events.append(["first", value])
class Last:
    @marker(trylast=True)
    def pytest_case(self, value): events.append(["last", value])
class Wrapper:
    @marker(wrapper=True)
    def pytest_case(self, value):
        events.append(["enter", value])
        result = yield
        events.append(["exit", result])
        return result
class Alias:
    @marker(specname="pytest_case")
    def alternate(self, value):
        events.append(["alias", value])
        return "alias"
a, b, alias = Plugin("a"), Plugin("b"), Alias()
for name, plugin in [("a",a), ("last",Last()), ("b",b), ("first",First()),
                     ("wrapper",Wrapper()), ("alias",alias)]:
    registry.register(plugin, name)
def extra_one(value):
    events.append(["extra-one", value])
    return "extra-one"
def extra_two(value):
    events.append(["extra-two", value])
    return "extra-two"
kwargs = {"value": "v"}
removed = [a, alias]
methods = registry.subset_hook_caller("pytest_case", removed).get_hookimpls()
local = {"a", "last", "b", "first", "wrapper", "alias", "core-runner"}
selected = [method.plugin_name for method in methods
            if method.plugin_name in local - {"core-runner"}]
hook = (registry.hook.pytest_case if case == "extra"
        else registry.subset_hook_caller("pytest_case", removed))
result = (hook.call_extra([extra_one, extra_two], kwargs)
          if case in {"extra", "subset_extra"} else hook(**kwargs))
def known(name):
    return hasattr(registry.hook, name)
known_before = known("pytest_case")
unknown = known("pytest_unknown")
for name in sorted(local):
    if registry.get_plugin(name) is not None: registry.unregister(name=name)
known_after = known("pytest_case")
print(json.dumps([result, events, known_before, unknown, known_after, selected]))
"""


@pytest.mark.parametrize("case", ["normal", "firstresult", "core", "extra", "subset_extra"])
def test_scoped_and_extra_hooks_match_stock_pytest(tmp_path: Path, case: str) -> None:
    stock, actual = run_manager_probe(tmp_path, _SELECTION, case)
    assert actual == stock
