"""Runtest hooks surround native fixture and test operations through the registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_EVENTS = """from pathlib import Path
import pytest

def event(text):
    with Path('events').open('a') as output:
        output.write(text + '\\n')

"""


def _compare(project: VfyProject, *, returncode: int = 0) -> tuple[str, str]:
    result = project.run_python(
        "-m",
        "vfy.independent.entry",
        "--native-result-json=results.json",
        env={"PYTHONPATH": _SOURCE},
    )
    assert result.returncode == returncode, (result.stdout, result.stderr)
    events = (project.root / "events").read_text()
    (project.root / "events").unlink()
    oracle = project.run_python("-m", "pytest", "-q")
    assert oracle.returncode == returncode, (oracle.stdout, oracle.stderr)
    assert events == (project.root / "events").read_text()
    return events, (project.root / "results.json").read_text()


def test_phase_wrappers_and_priorities_enclose_actual_fixture_and_body_work(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        _EVENTS
        + """active = False

@pytest.fixture
def resource():
    global active
    active = True
    event('fixture setup')
    yield 5
    event('fixture teardown')
    active = False

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    event('setup wrapper before')
    result = yield
    event('setup wrapper after')
    return result

@pytest.hookimpl(tryfirst=True, specname='pytest_runtest_setup')
def pytest_setup_first(item):
    assert not active
    event('setup first')

@pytest.hookimpl(specname='pytest_runtest_setup')
def pytest_setup_plain(item):
    event('setup plain')

@pytest.hookimpl(trylast=True, specname='pytest_runtest_setup')
def pytest_setup_last(item):
    assert active
    assert item.funcargs['resource'] == 5
    event('setup last')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    event('call wrapper before')
    result = yield
    event('call wrapper after')
    return result

@pytest.hookimpl(tryfirst=True, specname='pytest_runtest_call')
def pytest_call_first(item):
    item.funcargs['resource'] += 1
    event('call first')

@pytest.hookimpl(specname='pytest_runtest_call')
def pytest_call_plain(item):
    event('call plain')

@pytest.hookimpl(trylast=True, specname='pytest_runtest_call')
def pytest_call_last(item):
    event('call last')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    event('teardown wrapper before')
    result = yield
    event('teardown wrapper after')
    return result

@pytest.hookimpl(tryfirst=True, specname='pytest_runtest_teardown')
def pytest_teardown_first(item, nextitem):
    assert active
    name = 'none' if nextitem is None else nextitem.name
    event('teardown first next=' + name)

@pytest.hookimpl(specname='pytest_runtest_teardown')
def pytest_teardown_plain(item):
    event('teardown plain')

@pytest.hookimpl(trylast=True, specname='pytest_runtest_teardown')
def pytest_teardown_last(item):
    assert not active
    event('teardown last')
""",
    )
    project.write(
        "test_phases.py",
        _EVENTS
        + """def test_first(resource, request):
    assert resource == 6
    assert request is request.node.funcargs['request']
    event('first body')

def test_second(resource, request):
    assert resource == 6
    assert request is request.node.funcargs['request']
    event('second body')
""",
    )
    events, _ = _compare(project)
    assert "setup plain\nfixture setup\nsetup last\n" in events
    assert "call plain\nfirst body\ncall last\n" in events
    assert "teardown plain\nfixture teardown\nteardown last\n" in events
    assert "teardown first next=test_second\n" in events
    assert "teardown first next=none\n" in events


@pytest.mark.parametrize("style", ["legacy", "modern"])
@pytest.mark.parametrize("operation", ["mask", "replace"])
def test_call_wrappers_can_mask_or_replace_the_actual_body_error(
    tmp_path: Path, style: str, operation: str
) -> None:
    project = VfyProject(tmp_path)
    wrapper = (
        """@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    event('wrapper before')
    result = yield
    assert result.excinfo[0] is ValueError
    event('wrapper saw ValueError')
    if OPERATION == 'mask':
        result.force_result(None)
    else:
        result.force_exception(RuntimeError('replacement error'))
    event('wrapper after')
"""
        if style == "legacy"
        else """@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    event('wrapper before')
    try:
        result = yield
    except ValueError:
        event('wrapper saw ValueError')
        event('wrapper after')
        if OPERATION == 'mask':
            return []
        raise RuntimeError('replacement error')
    return result
"""
    )
    project.write(
        "conftest.py",
        _EVENTS
        + f"OPERATION = {operation!r}\n"
        + wrapper
        + """
@pytest.fixture
def resource():
    event('fixture setup')
    yield
    event('fixture teardown')

@pytest.hookimpl(trylast=True, specname='pytest_runtest_call')
def pytest_call_last(item):
    event('unreachable trylast callback')
""",
    )
    project.write(
        "test_failure.py",
        _EVENTS
        + """def test_failure(resource):
    event('actual body')
    raise ValueError('body error')
""",
    )
    events, record_json = _compare(project, returncode=0 if operation == "mask" else 1)
    record = json.loads(record_json)
    assert events == (
        "fixture setup\nwrapper before\nactual body\nwrapper saw ValueError\n"
        "wrapper after\nfixture teardown\n"
    )
    phases = record["cases"][0]["phases"]
    assert phases[1]["outcome"] == ("passed" if operation == "mask" else "failed")
    if operation == "replace":
        assert phases[1]["exception"] == "RuntimeError: replacement error"


def test_teardown_wrapper_can_mask_an_actual_fixture_teardown_error(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        _EVENTS
        + """@pytest.fixture
def resource():
    event('fixture setup')
    yield
    event('actual fixture teardown')
    raise ValueError('teardown failure')

@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    event('teardown wrapper before')
    try:
        result = yield
    except ValueError:
        event('teardown wrapper recovered')
        return []
    return result
""",
    )
    project.write(
        "test_teardown.py",
        _EVENTS + "def test_value(resource):\n    event('actual body')\n",
    )
    events, _ = _compare(project)
    assert events.endswith(
        "teardown wrapper before\nactual fixture teardown\nteardown wrapper recovered\n"
    )
