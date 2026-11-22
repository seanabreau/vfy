"""Compare live next-item routes and protocol cleanup with pinned pytest."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="phase dispatch requires Unix and pinned pytest",
)

_NEXT_CONFTEST = r"""
from pathlib import Path
import pytest

saved = []

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\n')

def pytest_runtest_setup(item):
    if saved:
        assert saved.pop() is item
        record('next identity ' + item.name)

def pytest_runtest_teardown():
    record('plain teardown')

def both(item, nextitem):
    record('both ' + item.name + ' next=' + (nextitem.name if nextitem else 'none'))
    if nextitem is not None:
        saved.append(nextitem)

def next_only(nextitem):
    record('next-only ' + (nextitem.name if nextitem else 'none'))
    if nextitem is not None:
        saved.append(nextitem)

def reverse(nextitem, item):
    record('reverse ' + item.name + ' next=' + (nextitem.name if nextitem else 'none'))
    if nextitem is not None:
        saved.append(nextitem)

@pytest.fixture(scope='module')
def shared():
    record('module start')
    yield
    record('module finish')

@pytest.fixture
def value(request, shared):
    record('fixture start ' + request.node.name)
    yield
    record('fixture finish ' + request.node.name)
"""

_NEXT_TEST = r"""
import pytest
import conftest

@pytest.mark.parametrize('index', range(5))
def test_case(index, value, request):
    impl = next(impl for impl in request.config.hook.pytest_runtest_teardown.get_hookimpls()
                if impl.plugin is conftest)
    callback, names = (
        (conftest.pytest_runtest_teardown, ()),
        (conftest.both, ('item', 'nextitem')),
        (conftest.next_only, ['nextitem']),
        (conftest.reverse, ['nextitem', 'item']),
        (conftest.both, ('item', 'nextitem')),
    )[index]
    impl.function = callback
    impl.argnames = names
    conftest.record('body ' + str(index))
    assert index != 2, 'call failure still unwinds nextitem hooks and shared fixtures'
"""


@pytest.mark.parametrize("stop", [False, True])
def test_phase_context_uses_live_nextitem_routes_and_preserves_scope_cleanup(
    tmp_path: Path, stop: bool
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _NEXT_CONFTEST)
        project.write("test_case.py", _NEXT_TEST)
        arguments = ("-q", "--junitxml=result.xml", *(["-x"] if stop else []))
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                *arguments,
            )
        )
        assert result.returncode == 1, result.stdout + result.stderr
        cases = [
            (
                case.get("name"),
                tuple(child.tag for child in case if child.tag in {"failure", "error"}),
            )
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
        events = (project.root / "events.txt").read_text().splitlines()
        assert events[-1] == "module finish"
        assert ("next-only none" if stop else "next-only test_case[3]") in events
        observations[route] = events, cases
    assert observations["native"] == observations["pytest"]


_PROTOCOL_CONFTEST = r"""
from pathlib import Path
import os
import pytest
from _pytest.assertion import util

original_compare = util._reprcompare

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\n')

def wrap(original, label):
    def protocol(item):
        record('enter ' + label + ' ' + item.name)
        try:
            return (yield from original(item))
        finally:
            record('exit ' + label + ' ' + item.name)
    return protocol

def pytest_configure(config):
    hook = config.hook.pytest_runtest_protocol
    for label in ('assertion', 'faulthandler'):
        plugin = config.pluginmanager.getplugin(label)
        original = plugin.pytest_runtest_protocol
        replacement = wrap(original, label)
        plugin.pytest_runtest_protocol = replacement
        for implementation in hook.get_hookimpls():
            if implementation.plugin is plugin and implementation.function is original:
                implementation.function = replacement

@pytest.fixture
def resource():
    record('fixture start')
    yield
    record('fixture finish')
    if os.environ['PROTOCOL_FAILURE'] == 'teardown':
        raise ValueError('teardown failure')

def pytest_sessionfinish(session):
    assert util._reprcompare is original_compare
    record('assertion state restored')
"""

_PROTOCOL_TEST = r"""
import os
from conftest import record

def test_case(resource):
    record('body')
    if os.environ['PROTOCOL_FAILURE'] == 'call':
        assert [1] == [2]
"""


@pytest.mark.parametrize("failure", ["none", "call", "teardown"])
def test_protocol_generators_close_in_order_and_restore_assertion_state(
    tmp_path: Path, failure: str
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _PROTOCOL_CONFTEST)
        project.write("test_case.py", _PROTOCOL_TEST)
        environment = {"PROTOCOL_FAILURE": failure}
        result = (
            project.run_pytest("-q", load_xdist=False, env=environment)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-replay",
                "--no-cov",
                "-q",
                env=environment,
            )
        )
        assert result.returncode == (0 if failure == "none" else 1), result.stdout + result.stderr
        observations[route] = (project.root / "events.txt").read_text().splitlines()
        assert observations[route][-1] == "assertion state restored"
    assert observations["native"] == observations["pytest"]
