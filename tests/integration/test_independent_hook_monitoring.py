"""Monitoring, tracing, and ephemeral core callbacks preserve hook call order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.hook_probe import run_manager_probe
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_empty_hooks_preserve_registration_history_and_monitors(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import pytest

class Specifications:
    @pytest.hookspec
    def pytest_empty(required): pass
    @pytest.hookspec(firstresult=True)
    def pytest_first(required): pass
    @pytest.hookspec(historic=True)
    def pytest_replay(required): pass

def pytest_addhooks(pluginmanager):
    pluginmanager.add_hookspecs(Specifications)
""",
        )
        project.write(
            "test_hooks.py",
            """import json
from pathlib import Path
import warnings
import pytest

def test_protocol(pytestconfig):
    manager = pytestconfig.pluginmanager
    hook = manager.hook
    first = hook.pytest_empty(required=1)
    first.append('caller mutation')
    events = [hook.pytest_empty(required=2), hook.pytest_first(required=2)]
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always')
        events.append(hook.pytest_empty())
    events.append([[warning.category.__name__, str(warning.message)] for warning in captured])
    history = []
    hook.pytest_replay.call_historic(kwargs={'required': 3}, result_callback=history.append)
    class Plugin:
        def pytest_empty(self, required): return required
        def pytest_first(self, required): return required
        def pytest_replay(self, required): return required
    plugin = Plugin()
    manager.register(plugin, 'dynamic')
    events.extend([hook.pytest_empty(required=4), hook.pytest_first(required=4), history])
    events.append(manager.subset_hook_caller('pytest_empty', remove_plugins=[plugin])(required=5))
    assert manager.unregister(plugin) is plugin
    events.append(hook.pytest_empty(required=6))
    monitored = []
    def before(name, methods, kwargs):
        if name == 'pytest_empty':
            monitored.append(['before', len(methods), kwargs.copy()])
    def after(outcome, name, methods, kwargs):
        if name == 'pytest_empty':
            monitored.append(['after', outcome.get_result()])
    undo = manager.add_hookcall_monitoring(before, after)
    events.append(hook.pytest_empty(required=7))
    undo()
    events.append(monitored)
    lines = []
    manager.trace.root.setwriter(lines.append)
    undo = manager.enable_tracing()
    events.append(hook.pytest_empty(required=8))
    undo()
    manager.trace.root.setwriter(None)
    events.append(lines)
    Path('events.json').write_text(json.dumps(events))
""",
        )
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_python("-m", "pytest", "-q", env=environment)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=environment)
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[1] == observations[0]


def test_dispatch_snapshots_survive_registration_and_monitor_changes(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_hooks.py",
            """import json
from pathlib import Path
import pytest

def test_hooks(pytestconfig):
    manager = pytestconfig.pluginmanager
    events = []
    class Specifications:
        @pytest.hookspec
        def pytest_snapshot(): pass
    manager.add_hookspecs(Specifications)
    class Body:
        def __init__(self, label): self.label = label
        def pytest_snapshot(self):
            events.append(self.label)
            return self.label
    def before(name, methods, kwargs):
        if name == 'pytest_snapshot':
            events.append(['before', [method.plugin_name for method in methods]])
    def after(outcome, name, methods, kwargs):
        if name == 'pytest_snapshot':
            events.append(['after', outcome.get_result()])
    class Mutator:
        calls = 0
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_snapshot(self):
            self.calls += 1
            events.append(['enter', self.calls])
            if self.calls == 1:
                assert manager.unregister(name='old') is not None
                manager.register(Body('new'), 'new')
                self.undo = manager.add_hookcall_monitoring(before, after)
            elif self.calls == 2:
                self.undo()
            result = yield
            events.append(['exit', self.calls, result])
            return result
    manager.register(Body('old'), 'old')
    manager.register(Mutator(), 'mutator')
    results = [manager.hook.pytest_snapshot() for _ in range(3)]
    Path('events.json').write_text(json.dumps([results, events]))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0] == [
        [["old"], ["new"], ["new"]],
        [
            ["enter", 1],
            "old",
            ["exit", 1, ["old"]],
            ["before", ["new", "mutator"]],
            ["enter", 2],
            "new",
            ["exit", 2, ["new"]],
            ["after", ["new"]],
            ["enter", 3],
            "new",
            ["exit", 3, ["new"]],
        ],
    ]
    assert observations[1] == observations[0]


_PROBE = """\
import json
import inspect

import pytest

events = []
marker = pytest.hookimpl
def call(): return registry.hook.pytest_case(value="v")
def impls(): return registry.hook.pytest_case.get_hookimpls()

class Plugin:
    def __init__(self, label): self.label = label
    @marker()
    def pytest_case(self, value):
        events.append(["body", self.label, value])
        if case == "body_error": raise ValueError("body")
        return self.label + value
registry.register(Plugin("a"), "a")
registry.register(Plugin("b"), "b")

def monitor(index):
    def before(name, methods, kwargs):
        events.append(["before", index, name, [m.plugin_name for m in methods], kwargs.copy()])
        if case == "before_error" and index == 1: raise LookupError("before")
        if case == "mutation" and index == 2:
            methods.pop()
            kwargs["value"] = "changed"
    def after(outcome, name, methods, kwargs):
        events.append(["after", index, name, [m.plugin_name for m in methods],
                       type(outcome.exception).__name__ if outcome.exception else None,
                       outcome.get_result() if outcome.exception is None else None])
        if case == "after_error" and index == 1: raise LookupError("after")
        if case == "force" and index == 1: outcome.force_result(["forced"])
    return registry.add_hookcall_monitoring(before, after)
one, two = monitor(1), monitor(2)
assert not inspect.signature(one).parameters
def observe():
    try: return ["return", call()]
    except BaseException as error: return ["raise", type(error).__name__, str(error)]
try:
    result = [observe()]
    if case == "undo":
        one()
        result.append(observe())
        two()
        result.append(observe())
        two()
        result.append(observe())
finally:
    # Monitors on the live manager would otherwise see (and fail) pytest's own hook calls.
    # Undo restores the dispatcher captured at registration, so stack order matters.
    if case != "undo":
        two()
        one()
print(json.dumps([result, events]))
"""


def _compare(tmp_path: Path, source: str, case: str) -> None:
    stock, actual = run_manager_probe(tmp_path, source, case)
    assert actual == stock


@pytest.mark.parametrize(
    "case", ["normal", "body_error", "before_error", "after_error", "force", "mutation", "undo"]
)
def test_hook_monitoring_matches_stock_pytest(tmp_path: Path, case: str) -> None:
    _compare(tmp_path, _PROBE, case)


@pytest.mark.parametrize("case", ["normal", "error", "nested", "processor", "writer_error"])
def test_hook_tracing_matches_stock_pytest(tmp_path: Path, case: str) -> None:
    _compare(
        tmp_path,
        """\
import json
import inspect
import pytest
events, lines = [], []
marker = pytest.hookimpl
def call(name): return getattr(registry.hook, name)(value="v")
class Plugin:
    @marker()
    def pytest_case(self, value):
        if case == "error": raise ValueError("body")
        if case == "nested": return call("pytest_nested")
        return value
    @marker()
    def pytest_nested(self): return {"nested": "result"}
registry.register(Plugin(), "plugin")
trace = registry.trace
assert list(inspect.signature(trace).parameters) == ["args"]
assert inspect.signature(trace).parameters["args"].kind is inspect.Parameter.VAR_POSITIONAL
root = trace.root
def processor(tags, args):
    events.append([tags, args])
def writer(line):
    lines.append(line)
    if case == "writer_error": raise LookupError("writer")
    if case == "processor": root.setprocessor("hook", processor)
root.setwriter(writer)
undo = registry.enable_tracing()
try: result = ["return", call("pytest_case")]
except BaseException as error: result = ["raise", type(error).__name__, str(error)]
undo()
if case == "writer_error": root.setwriter(None)
trace.get("extra")("manual", {"value": 3})
print(json.dumps([result, lines, events, root.indent, trace.tags]))
""",
        case,
    )


@pytest.mark.parametrize("firstresult", [False, True])
def test_default_callback_uses_core_registration_order(tmp_path: Path, firstresult: bool) -> None:
    _compare(
        tmp_path,
        """\
import json
import pytest
firstresult = case == "True"
events = []
marker = pytest.hookimpl
class Spec:
    @pytest.hookspec(firstresult=firstresult)
    def pytest_case(value): pass
registry.add_hookspecs(Spec)
def core(value):
    events.append("core")
    return "core result"
class Core:
    @marker()
    def pytest_case(self, value): return core(value)
registry.register(Core(), "core-runner")
class Plain:
    @marker()
    def pytest_case(self): events.append("plain")
class Last:
    @marker(trylast=True)
    def pytest_case(self): events.append("last")
class First:
    @marker(tryfirst=True)
    def pytest_case(self): events.append("first")
class Wrapper:
    @marker(wrapper=True)
    def pytest_case(self):
        events.append("enter")
        result = yield
        events.append(["exit", result])
        return result
for plugin in [Plain(), Last(), First(), Wrapper()]: registry.register(plugin)
result = registry.hook.pytest_case(value="v")
print(json.dumps([result, events]))
""",
        str(firstresult),
    )


@pytest.mark.parametrize("kind", ["default", "extra"])
def test_temporary_callback_options_retain_the_observed_mapping(tmp_path: Path, kind: str) -> None:
    _compare(
        tmp_path,
        """\
import json
import pytest
kind = case
events, retained = [], []
marker = pytest.hookimpl
spec = pytest.hookspec
class Specifications:
    @spec
    def pytest_case(value): pass
registry.add_hookspecs(Specifications)
def callback(value): return value
class Core:
    @marker()
    def pytest_case(self, value): return callback(value)
if kind == 'default':
    registry.register(Core(), 'core-runner')
target = 'core-runner'
if kind == 'extra':
    target = '<temp>'
def before(name, methods, kwargs):
    method, = [method for method in methods if method.plugin_name == target]
    options = method.opts
    assert options is method.opts
    events.append(['before', sorted(options.items())])
    options['observed'] = 'before'
    retained.append((method, options))
def after(outcome, name, methods, kwargs):
    method, options = retained[0]
    assert method in methods and method.opts is options
    assert options['observed'] == 'before'
    options['observed'] = 'after'
undo = registry.add_hookcall_monitoring(before, after)
hook = registry.hook.pytest_case
try:
    result = hook(value=7) if kind == 'default' else hook.call_extra([callback], {'value': 7})
finally:
    undo()
method, options = retained[0]
assert method.opts is options
options['retained'] = True
assert method.opts['retained'] is True
print(json.dumps([result, events, sorted(options.items())]))
""",
        kind,
    )


def test_makereport_monitor_payload_is_released_before_logreport(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

events = []

class Payload:
    def __init__(self, phase):
        self.phase = phase
    def __del__(self):
        events.append(['released', self.phase])

def before(name, methods, kwargs):
    if name == 'pytest_runtest_makereport':
        phase = kwargs['call'].when
        events.append(['monitor', phase])
        kwargs['_monitor_payload'] = Payload(phase)

def pytest_configure(config):
    config.pluginmanager.add_hookcall_monitoring(before, lambda *args: None)

def pytest_runtest_logreport(report):
    events.append(['log', report.when])

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
""",
        )
        project.write("test_body.py", "def test_body(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0] == [
        [event, phase]
        for phase in ("setup", "call", "teardown")
        for event in ("monitor", "released", "log")
    ]
    assert observations[1] == observations[0]


def test_protocol_monitor_payload_is_released_before_report_formatting(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

events = []
finished = False
released = False
formatted = 0

class Payload:
    def __del__(self):
        global released
        released = True
        events.append('released')

class Representation:
    def __str__(self):
        global formatted
        if finished:
            assert released, 'protocol kwargs retained through report formatting'
            formatted += 1
        return ''

def before(name, methods, kwargs):
    if name == 'pytest_runtest_protocol':
        kwargs['_monitor_payload'] = Payload()
        events.append('before')

def after(outcome, name, methods, kwargs):
    global finished
    if name == 'pytest_runtest_protocol':
        finished = True
        events.append('after')

def pytest_configure(config):
    config.pluginmanager.add_hookcall_monitoring(before, after)

def pytest_runtest_logreport(report):
    report.longrepr = Representation()

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps([events, formatted]))
""",
        )
        project.write("test_body.py", "def test_body(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0][0] == observations[1][0] == ["before", "after", "released"]
    assert observations[1][1] > 0


def test_fixture_monitor_payload_can_call_suppressed_default_during_release(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import inspect
import json
from pathlib import Path
import pytest

events = []

class Payload:
    def __init__(self, callback, arguments):
        self.callback = callback
        self.arguments = arguments
    def __del__(self):
        events.append('released')
        try:
            value = self.callback(**self.arguments)
        except BaseException as error:
            events.append(['callback-error', type(error).__name__, str(error)])
        else:
            events.append(['callback-result', value])

def before(name, methods, kwargs):
    if name == 'pytest_fixture_setup' and kwargs['fixturedef'].argname == 'provider':
        callback = next(method.function for method in methods if method.plugin_name == 'fixtures')
        arguments = {name: kwargs[name] for name in inspect.signature(callback).parameters}
        kwargs['_monitor_payload'] = Payload(callback, arguments)
        events.append('monitor')

def after(outcome, name, methods, kwargs):
    if name == 'pytest_fixture_setup' and kwargs['fixturedef'].argname == 'provider':
        events.append('after')

def pytest_configure(config):
    config.pluginmanager.add_hookcall_monitoring(before, after)

@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname == 'provider':
        events.append('override')
        return object()

@pytest.fixture
def provider():
    events.append('body')
    return 11

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
""",
        )
        project.write(
            "test_body.py",
            "from conftest import events\n"
            "def test_body(provider):\n"
            "    events.append(['test', provider])\n"
            "    assert provider == 11\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0] == [
        "monitor",
        "override",
        "after",
        "released",
        "body",
        ["callback-result", 11],
        ["test", 11],
    ]
    assert observations[1] == observations[0]


@pytest.mark.parametrize("arity", [1, 2])
def test_hook_arguments_survive_wrapper_start_after_monitor_mutation(
    tmp_path: Path, arity: int
) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_lifetime.py",
            f"""import json
from pathlib import Path
import weakref
import pytest

ARITY = {arity}
events = []
refs = []
released = []
monitored = None

class Payload:
    def __init__(self, name):
        self.name = name
    def __del__(self):
        released.append(self.name)

class Specifications:
    @pytest.hookspec
    def pytest_one(first): pass
    @pytest.hookspec
    def pytest_two(first, second): pass

class One:
    @pytest.hookimpl(wrapper=True)
    def pytest_one(self, first):
        del first
        monitored.clear()
        events.append(['start', all(ref() is not None for ref in refs)])
        return (yield)

class Two:
    @pytest.hookimpl(wrapper=True)
    def pytest_two(self, first, second):
        del first, second
        monitored.clear()
        events.append(['start', all(ref() is not None for ref in refs)])
        return (yield)

def test_lifetime(pytestconfig):
    global monitored
    manager = pytestconfig.pluginmanager
    manager.add_hookspecs(Specifications)
    manager.register(One() if ARITY == 1 else Two(), 'lifetime')
    target = 'pytest_one' if ARITY == 1 else 'pytest_two'
    def before(name, methods, kwargs):
        global monitored
        if name != target:
            return
        events.append('before')
        monitored = kwargs
        for key in tuple(kwargs):
            kwargs[key] = Payload(key)
            refs.append(weakref.ref(kwargs[key]))
    def after(outcome, name, methods, kwargs):
        if name == target:
            events.append(['after', released.copy(), outcome.get_result()])
    undo = manager.add_hookcall_monitoring(before, after)
    try:
        if ARITY == 1:
            result = manager.hook.pytest_one(first=None)
        else:
            result = manager.hook.pytest_two(first=None, second=None)
    finally:
        undo()
    assert result == []
    Path('events.json').write_text(json.dumps(events))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    expected_release = ["first"] if arity == 1 else ["second", "first"]
    assert observations[0] == ["before", ["start", True], ["after", expected_release, []]]
    assert observations[1] == observations[0]


@pytest.mark.parametrize("monitored", [False, True])
def test_parameterless_builtin_dispatch_retains_hook_order(tmp_path: Path, monitored: bool) -> None:
    """The internal defaults keep normal wrapper ordering with or without a monitor."""
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """import json
from pathlib import Path
import pytest

events = []
MONITORED = MONITOR_FLAG

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    events.append('protocol-enter')
    result = yield
    events.append('protocol-exit')
    return result

@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_call(item):
    events.append('call-enter')
    result = yield
    events.append('call-exit')
    return result

@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    selected = fixturedef.argname == 'subject'
    if selected: events.append('fixture-enter')
    result = yield
    if selected: events.append('fixture-exit')
    return result

def pytest_configure(config):
    if MONITORED:
        def before(name, methods, kwargs):
            if name in {'pytest_runtest_protocol', 'pytest_runtest_call', 'pytest_fixture_setup'}:
                for method in methods:
                    assert callable(method.function)
                    assert isinstance(method.argnames, tuple)
                    assert isinstance(method.opts, dict)
        config.pluginmanager.add_hookcall_monitoring(before, lambda *args: None)

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
""".replace("MONITOR_FLAG", repr(monitored)),
        )
        project.write(
            "test_body.py",
            """import pytest
@pytest.fixture
def subject(): return 1
def test_body(subject): assert subject == 1
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert (
        observations[0]
        == observations[1]
        == [
            "protocol-enter",
            "fixture-enter",
            "fixture-exit",
            "call-enter",
            "call-exit",
            "protocol-exit",
        ]
    )


@pytest.mark.parametrize("warning_field", ["warn_on_impl", "warn_on_impl_args"])
def test_default_verification_observes_live_warning_and_monitor(
    tmp_path: Path, warning_field: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
class WarningProbe:
    def __init__(self, manager): self.manager = manager
    def __bool__(self):
        events.append('warning-check')
        if events.count('warning-check') == 1:
            def before(name, methods, kwargs):
                if name == 'pytest_runtest_call':
                    default = next(method for method in methods if method.plugin_name == 'runner')
                    events.append(['monitor', callable(default.function), list(default.argnames)])
            self.manager.add_hookcall_monitoring(before, lambda *args: None)
        return False

def pytest_configure(config):
    manager = config.pluginmanager
    setattr(manager.hook.pytest_runtest_call.spec, WARNING_FIELD, WarningProbe(manager))

def pytest_unconfigure():
    Path('events.json').write_text(json.dumps(events))
""".replace("WARNING_FIELD", repr(warning_field)),
    )
    project.write("test_body.py", "def test_body(): pass\n")
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)
    assert json.loads((project.root / "events.json").read_text()) == [
        "warning-check",
        "warning-check",
        ["monitor", True, []],
    ]
