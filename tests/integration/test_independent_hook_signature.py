"""Hook admission follows live Python function signatures."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest


@requires_native_pytest
def test_hook_signature_sources_match_pytest(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("test_body.py", "def test_body(): pass\n")
        project.write(
            "conftest.py",
            """import functools, inspect, json, sys, types
from pathlib import Path
import pytest

events = []

class Spec:
    @pytest.hookspec
    def pytest_probe(alpha, beta, gamma): pass

class Bound:
    @pytest.hookimpl
    def pytest_probe(self, alpha, /, beta=4, *, gamma=9):
        return ['bound', alpha, beta, gamma]

@ pytest.hookimpl
def basic(alpha, /, beta=4, *, gamma=9):
    return ['basic', alpha, beta, gamma]

@ pytest.hookimpl
def changed(alpha):
    return ['changed', alpha]

def replacement(alpha, beta=4):
    return ['changed', alpha, beta]
changed.__code__ = replacement.__code__
changed.__defaults__ = (7,)

@pytest.hookimpl
def sentinel(alpha=inspect.Parameter.empty):
    return ['sentinel', alpha]

@ pytest.hookimpl
def original(alpha):
    return ['wrapped', alpha]
@functools.wraps(original)
def wrapped(*args, **kwargs):
    return original(*args, **kwargs)

@ pytest.hookimpl
def overridden(alpha, beta=8):
    return ['override', alpha, beta]
overridden.__signature__ = inspect.Signature([
    inspect.Parameter('alpha', inspect.Parameter.POSITIONAL_OR_KEYWORD),
    inspect.Parameter('beta', inspect.Parameter.POSITIONAL_OR_KEYWORD),
])

@pytest.hookimpl
def annotated(alpha):
    return ['annotated', alpha]

if sys.version_info >= (3, 14):
    def annotate(format):
        events.append(['annotate', int(format)])
        return {'alpha': int}
    annotated.__annotate__ = annotate

def pytest_addhooks(pluginmanager):
    pluginmanager.add_hookspecs(Spec)

def pytest_configure(config):
    manager = config.pluginmanager
    providers = [('basic', basic), ('changed', changed), ('wrapped', wrapped),
                 ('override', overridden), ('sentinel', sentinel)]
    for name, function in providers:
        manager.register(types.SimpleNamespace(pytest_probe=function), name=name)
    if sys.version_info >= (3, 14):
        manager.register(types.SimpleNamespace(pytest_probe=annotated), name='annotated')
    manager.register(Bound(), name='bound')
    def unusual(alpha, beta): pass
    unusual = pytest.hookimpl(unusual)
    rejected = []
    unusual.__code__ = unusual.__code__.replace(co_varnames=('for', 'beta'))
    try:
        manager.register(types.SimpleNamespace(pytest_probe=unusual), name='keyword')
    except Exception as error:
        rejected.append([type(error).__name__, str(error)])
    def duplicate(alpha, beta): pass
    duplicate = pytest.hookimpl(duplicate)
    duplicate.__code__ = duplicate.__code__.replace(co_varnames=('alpha', 'alpha'))
    manager.register(types.SimpleNamespace(pytest_probe=duplicate), name='duplicate')
    hook = manager.hook.pytest_probe
    rows = [[impl.plugin_name, list(impl.argnames), list(impl.kwargnames)]
            for impl in hook.get_hookimpls()]
    manager.unregister(name='duplicate')
    Path('signatures.json').write_text(
        json.dumps([rows, rejected, hook(alpha=1, beta=2, gamma=3), events]))
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observations.append(json.loads((project.root / "signatures.json").read_text()))
    assert observations[0] == observations[1]
    rows, rejected, _, _ = observations[0]
    assert ["duplicate", ["alpha"], []] in rows
    assert rejected == [["ValueError", "'for' is not a valid parameter name"]]
