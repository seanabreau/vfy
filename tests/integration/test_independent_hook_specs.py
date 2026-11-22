"""Hook specification and historic replay contracts match stock pytest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.hook_probe import run_manager_probe
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROBE = """\
import json
import warnings

import pytest

events = []
marker = pytest.hookimpl
specmarker = pytest.hookspec
register = registry.register
addspecs = registry.add_hookspecs
def call(name, kwargs): return getattr(registry.hook, name)(**kwargs)
def historic(name, kwargs, callback):
    return getattr(registry.hook, name).call_historic(callback, kwargs)
def impls(name): return getattr(registry.hook, name).get_hookimpls()
def has_spec(name): return getattr(registry.hook, name).has_spec()

def observe(function):
    try: return ["return", function()]
    except BaseException as error:
        return ["raise", type(error).__name__, str(error),
                getattr(error, "plugin", None) is plugin]

class Spec:
    @specmarker(firstresult=case == "first", historic=case.startswith("historic"))
    def pytest_case(value): pass
class Plugin:
    def __repr__(self): return "plugin"
    @marker()
    def pytest_case(self, value):
        events.append(["body", value])
        return value
plugin = Plugin()

if case == "arguments":
    class Plugin:
        def __repr__(self): return "plugin"
        @marker()
        def pytest_case(self, absent): pass
    plugin = Plugin()
    addspecs(Spec)
    result = observe(lambda: register(plugin, "plugin"))
elif case == "nongenerator":
    class Plugin:
        def __repr__(self): return "plugin"
        @marker(wrapper=True)
        def pytest_case(self, value): return value
    plugin = Plugin()
    addspecs(Spec)
    result = observe(lambda: register(plugin, "plugin"))
elif case == "pending":
    register(plugin, "plugin")
    result = observe(registry.check_pending)
elif case == "optional":
    class Plugin:
        @marker(optionalhook=True)
        def pytest_unknown(self): pass
    plugin = Plugin()
    register(plugin, "plugin")
    result = observe(registry.check_pending)
elif case == "late_spec":
    class Plugin:
        def __repr__(self): return "plugin"
        @marker()
        def pytest_case(self, absent): pass
    plugin = Plugin()
    register(plugin, "plugin")
    result = observe(lambda: addspecs(Spec))
    events.append(has_spec("pytest_case"))
elif case == "historic_wrapper":
    class Plugin:
        def __repr__(self): return "plugin"
        @marker(wrapper=True)
        def pytest_case(self): yield
    plugin = Plugin()
    addspecs(Spec)
    result = observe(lambda: register(plugin, "plugin"))
elif case == "historic_replay":
    addspecs(Spec)
    values = []
    kwargs = {"value": "first"}
    historic("pytest_case", kwargs, values.append)
    kwargs["value"] = "changed"
    register(plugin, "plugin")
    historic("pytest_case", {"value": "second"}, values.append)
    other = Plugin()
    register(other, "other")
    result = [values, events]
    events = []
elif case == "historic_direct":
    addspecs(Spec)
    result = observe(lambda: call("pytest_case", {"value": "v"}))
elif case == "first":
    addspecs(Spec)
    register(plugin, "first")
    register(Plugin(), "second")
    result = call("pytest_case", {"value": "v"})
elif case == "metadata":
    addspecs(Spec)
    register(plugin, "plugin")
    method = impls("pytest_case")[0]
    result = [method.plugin is plugin, method.function == plugin.pytest_case,
              method.argnames, method.kwargnames, method.wrapper,
              method.hookwrapper, method.tryfirst, method.trylast,
              method.optionalhook, method.plugin_name,
              method is impls("pytest_case")[0]]
elif case == "warn_argument":
    class Spec:
        @specmarker(warn_on_impl=UserWarning("whole"),
                    warn_on_impl_args={"value": DeprecationWarning("argument")})
        def pytest_case(value): pass
    addspecs(Spec)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        register(plugin, "plugin")
    result = [[type(w.message).__name__, str(w.message)] for w in caught]
else: raise AssertionError(case)
# vfy validates before it publishes; pluggy keeps a plug-in whose registration failed.
rejected = case in {"arguments", "nongenerator", "historic_wrapper"}
print(json.dumps([result, events, None if rejected else registry.get_plugin("plugin") is plugin]))
"""


@pytest.mark.parametrize(
    "case",
    [
        "arguments",
        "nongenerator",
        "pending",
        "optional",
        "late_spec",
        "historic_wrapper",
        "historic_replay",
        "historic_direct",
        "first",
        "metadata",
        "warn_argument",
    ],
)
def test_hook_specifications_and_history_match_stock_pytest(tmp_path: Path, case: str) -> None:
    stock, actual = run_manager_probe(tmp_path, _PROBE, case)
    assert actual == stock


def test_failed_registration_leaves_no_plugin_or_hook_implementation(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_body.py", "def test_body():\n    pass\n")
    project.write(
        "conftest.py",
        """import pytest

class Spec:
    @pytest.hookspec
    def pytest_case(value): pass

class Plugin:
    @pytest.hookimpl
    def pytest_case(self, absent): pass

def pytest_configure(config):
    manager = config.pluginmanager
    manager.add_hookspecs(Spec)
    try:
        manager.register(Plugin(), "invalid")
    except Exception as error:
        assert type(error).__name__ == "PluginValidationError", error
    else:
        raise AssertionError("invalid registration succeeded")
    assert manager.get_plugin("invalid") is None
    assert manager.hook.pytest_case.get_hookimpls() == []
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)


_SIGNATURES = """\
import json
from pathlib import Path

def pytest_configure(config):
    specs = {}
    for name in sorted(vars(config.pluginmanager.hook)):
        if not name.startswith("pytest_"):
            continue
        spec = getattr(config.pluginmanager.hook, name).spec
        if spec is not None:
            specs[name] = [list(spec.argnames), spec.opts["firstresult"], spec.opts["historic"]]
    Path("specs.json").write_text(json.dumps(specs))
"""


def test_driver_hook_signatures_match_stock_pytest(tmp_path: Path) -> None:
    recorded = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _SIGNATURES)
        project.write("test_body.py", "def test_body():\n    pass\n")
        result = project.run_engine(engine, "-q")
        result.expect(0)
        recorded[engine] = json.loads((project.root / "specs.json").read_text())
    assert recorded["native"]
    for name, specification in recorded["native"].items():
        assert recorded["stock"][name] == specification, name
