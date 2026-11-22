"""Fixture control exceptions leave the actual binding uncached at teardown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PLUGIN = """import json,pytest
from pathlib import Path
events=[]
bindings={}
failure=None
class CustomBase(BaseException):pass
class MaskedBase(BaseException):
    @property
    def __class__(self):
        events.append('unexpected class descriptor')
        return pytest.fail.Exception
class CustomFailure(pytest.fail.Exception):pass
def stop():
    if MODE=='keyboard':raise KeyboardInterrupt('uncached')
    if MODE=='system_exit':raise SystemExit(23)
    if MODE=='generator_exit':raise GeneratorExit('uncached')
    if MODE=='custom_base':raise CustomBase('uncached')
    if MODE=='masked_base':raise MaskedBase('uncached')
    if MODE=='exit':pytest.exit('cached',returncode=42)
    if MODE=='failed':pytest.fail('cached')
    if MODE=='failed_subclass':raise CustomFailure('cached')
    if MODE=='skipped':pytest.skip('cached')
    if MODE=='xfailed':pytest.xfail('cached')
    raise RuntimeError('cached')
@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef,request):
    if fixturedef.argname!='resource':return (yield)
    bindings['resource']=fixturedef
    try:return (yield)
    except BaseException as error:
        global failure
        failure=error
        cache=fixturedef.cached_result
        events.append(['setup error',type(error).__name__,cache is None,
                       cache is not None and cache[2][0] is error])
        raise
def pytest_fixture_post_finalizer(fixturedef,request):
    if fixturedef.argname in {'resource','dependency'}:
        events.append(['post',fixturedef.argname,fixturedef.cached_result is None])
@pytest.fixture(scope='module')
def dependency(request):
    events.append('dependency setup')
    def cleanup():
        events.append('dependency finalizer')
        request.addfinalizer(lambda:events.append('dependency appended'))
    request.addfinalizer(cleanup)
    return 17
@pytest.fixture
def resource(request,dependency):
    events.append(['resource setup',dependency])
    def cleanup():
        events.append('resource finalizer')
        request.addfinalizer(lambda:events.append('resource appended'))
    request.addfinalizer(cleanup)
    stop()
def pytest_sessionfinish(session,exitstatus):
    binding=bindings['resource']
    events.append(['finish',int(exitstatus),binding.cached_result is None,
                   len(binding._finalizers)])
    Path('events.json').write_text(json.dumps(events))
"""

_BODY = """from conftest import events,bindings
def test_body(request):
    request.node.addfinalizer(lambda:events.append('node finalizer'))
    try:request.getfixturevalue('resource')
    except BaseException as error:
        events.append(['caught',type(error).__name__,len(bindings['resource']._finalizers)])
    else:raise AssertionError('fixture did not raise')
    assert request.getfixturevalue('dependency')==17
    events.append('body complete')
"""

_ERRORS = {
    "keyboard": "KeyboardInterrupt",
    "system_exit": "SystemExit",
    "generator_exit": "GeneratorExit",
    "custom_base": "CustomBase",
    "masked_base": "MaskedBase",
    "exit": "Exit",
    "failed": "Failed",
    "failed_subclass": "CustomFailure",
    "skipped": "Skipped",
    "xfailed": "XFailed",
    "ordinary": "RuntimeError",
}
_UNCACHED = {"keyboard", "system_exit", "generator_exit", "custom_base", "masked_base"}


def _run(project: VfyProject, engine: str, mode: str) -> list[Any]:
    expected = None
    if engine == "native":
        expected = _run(project, "stock", mode)
        (project.root / "events.json").unlink()
    result = (
        project.run_python("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider")
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (project.root / "events.json").is_file(), result.stdout + result.stderr
    observed = json.loads((project.root / "events.json").read_text())
    project.write(f"{engine}-events.json", json.dumps(observed))
    uncached = mode in _UNCACHED
    cleanup = (
        [] if uncached else ["resource finalizer", "resource appended", ["post", "resource", False]]
    )
    assert observed == [
        "dependency setup",
        ["resource setup", 17],
        ["setup error", _ERRORS[mode], uncached, not uncached],
        ["caught", _ERRORS[mode], 2],
        "body complete",
        *cleanup,
        "node finalizer",
        "dependency finalizer",
        "dependency appended",
        ["post", "dependency", False],
        ["finish", 0, True, 2 if uncached else 0],
    ]
    if engine == "native":
        assert observed == expected
    return observed


@pytest.mark.parametrize("engine", ["stock", "native"])
@pytest.mark.parametrize("mode", _ERRORS)
def test_uncached_fixture_outcomes_preserve_binding_and_dependency_cleanup(
    tmp_path: Path, engine: str, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", f"MODE={mode!r}\n" + _PLUGIN)
    project.write("test_fixture.py", _BODY)
    _run(project, engine, mode)
