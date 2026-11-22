"""Startup exclusions precede capture declarations and positive activation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_ORDERS = [
    (),
    ("no:capture",),
    ("no:capture", "capture"),
    ("capture", "no:capture"),
    ("no:capture", "capture", "no:capture"),
    ("capture", "capture"),
]
_NAMES = ["capfd", "capfdbinary", "capsys", "capsysbinary", "capteesys"]


def _write_capture_order_project(project: VfyProject) -> None:
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """providers = []
def pytest_plugin_registered(plugin, plugin_name):
    if plugin_name == 'capture':
        providers.append(plugin)
""",
    )
    project.write(
        "test_capture_order.py",
        f"""import json, pytest
from pathlib import Path
from conftest import providers
NAMES = {_NAMES!r}
results = []

@pytest.mark.parametrize('name', NAMES)
def test_capture_order(request, name):
    try:
        capture = request.getfixturevalue(name)
    except pytest.FixtureLookupError as error:
        results.append([name, 'lookup', error.argname, error.msg])
    except AttributeError as error:
        results.append([name, 'attribute', str(error), error.name, error.obj is None])
    else:
        definition = capture.request._get_fixturestack()[-1]
        assert definition.func is getattr(providers[-1], name)._get_wrapped_function()
        print('captured', end='')
        assert capture.readouterr().out in ('captured', b'captured')
        results.append([name, 'available', capture.request.scope, capture.request.fixturename])
    if len(results) == len(NAMES):
        Path('observations.json').write_text(json.dumps({{
            'providers': len(providers),
            'same_objects': all(provider is providers[0] for provider in providers),
            'names': [sorted(name for name in NAMES if hasattr(provider, name))
                      for provider in providers],
            'capturemanager': request.config.pluginmanager.getplugin('capturemanager') is not None,
            'results': results,
        }}))
""",
    )


@pytest.mark.parametrize("engine", ["stock", "native"])
@pytest.mark.parametrize("plugins", _ORDERS)
def test_capture_startup_plugin_order_uses_actual_declaration_history(
    tmp_path: Path, engine: str, plugins: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    _write_capture_order_project(project)
    arguments = [argument for name in plugins for argument in ("-p", name)]
    result = (
        project.run_pytest("-q", *arguments, load_xdist=False)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q", *arguments)
    )
    project.write("runner.stdout", result.stdout)
    project.write("runner.stderr", result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    history = not plugins or "capture" in plugins
    initialized = not plugins or plugins[-1] == "capture"
    if initialized:
        values = [[name, "available", "function", name] for name in _NAMES]
    elif history:
        values = [
            [
                name,
                "attribute",
                "'NoneType' object has no attribute 'set_fixture'",
                "set_fixture",
                True,
            ]
            for name in _NAMES
        ]
    else:
        values = [[name, "lookup", name, None] for name in _NAMES]
    assert json.loads((tmp_path / "observations.json").read_text()) == {
        "providers": int(history),
        "same_objects": True,
        "names": [_NAMES] if history else [],
        "capturemanager": initialized,
        "results": values,
    }
