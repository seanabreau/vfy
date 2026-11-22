"""Compare project phase callbacks and fixture boundaries with pytest."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize(
    "scenario",
    [
        "pass",
        "skip_first",
        "skip_default",
        "error_setup",
        "error_teardown",
        "error_finalizer",
        "marker",
        "no_skipping",
    ],
)
def test_plain_conftest_phase_order_and_failure_cleanup(tmp_path: Path, scenario: str) -> None:
    conftest = r"""
from pathlib import Path
import os
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        phase = os.environ.get('PYTEST_CURRENT_TEST', 'none').rsplit(' ', 1)[-1]
        stream.write(event + ' env=' + phase + '\n')

SCENARIO = SCENARIO_VALUE

def pytest_collection():
    record('collection')

def pytest_collection_finish(session):
    record('collected ' + str(len(session.items)))

@pytest.fixture(scope='session')
def shared():
    record('shared setup')
    yield
    record('shared teardown')

@pytest.fixture
def value(shared):
    record('value setup')
    yield 3
    record('value teardown')
    if SCENARIO == 'error_finalizer':
        raise ValueError('fixture finalizer failed')

@pytest.hookimpl(specname='pytest_runtest_setup', tryfirst=True)
def pytest_setup_first(item):
    record(item.name + ' setup first')
    if item.name == 'test_a' and SCENARIO == 'skip_first':
        pytest.skip('first callback skipped')
    if item.name == 'test_a' and SCENARIO == 'marker':
        item.add_marker(pytest.mark.skip(reason='live marker'))

@pytest.hookimpl(specname='pytest_runtest_setup')
def pytest_setup_default(item):
    record(item.name + ' setup default')
    print('setup callback output')
    if item.name == 'test_a' and SCENARIO == 'skip_default':
        pytest.skip('default callback skipped')
    if item.name == 'test_a' and SCENARIO == 'error_setup':
        raise ValueError('setup callback failed')

@pytest.hookimpl(specname='pytest_runtest_setup', trylast=True)
def pytest_setup_last(item):
    record(item.name + ' setup last')

@pytest.hookimpl(specname='pytest_runtest_teardown', tryfirst=True)
def pytest_teardown_first(nextitem, item):
    record(item.name + ' teardown first ' + (nextitem.name if nextitem else 'none'))

@pytest.hookimpl(specname='pytest_runtest_teardown')
def pytest_teardown_default(item):
    record(item.name + ' teardown default')
    print('teardown callback output')
    if item.name == 'test_a' and SCENARIO == 'error_teardown':
        raise ValueError('teardown callback failed')

@pytest.hookimpl(specname='pytest_runtest_teardown', trylast=True)
def pytest_teardown_last(item):
    record(item.name + ' teardown last')
""".replace("SCENARIO_VALUE", repr(scenario))
    source = """from conftest import record

def test_a(value):
    record('a body')
    assert value == 3

def test_b(value):
    record('b body')
    assert value == 3
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\njunit_logging=all\n")
        project.write("conftest.py", conftest)
        project.write("test_case.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            *(["-p", "no:skipping"] if scenario == "no_skipping" else []),
        )
        transcript = result.stdout + result.stderr
        assert result.returncode in {0, 1}, transcript
        xml = ET.parse(project.root / "result.xml")
        observed[engine] = (
            result.returncode,
            (project.root / "events.txt").read_text().splitlines(),
            [
                (
                    case.get("name"),
                    tuple(
                        child.tag for child in case if child.tag in {"failure", "error", "skipped"}
                    ),
                    tuple(
                        (child.text or "").count("callback output")
                        for child in case
                        if child.tag == "system-out"
                    ),
                )
                for case in xml.iter("testcase")
            ],
        )
    assert observed["pytest-native"] == observed["pytest"]


def test_conftest_phase_hooks_are_path_scoped(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "left/conftest.py",
            """from pathlib import Path

def pytest_runtest_setup(item):
    with Path('events.txt').open('a') as stream:
        stream.write(item.nodeid + '\\n')
""",
        )
        for directory in ("left", "right"):
            project.write(f"{directory}/test_{directory}.py", "def test_case():\n    pass\n")
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"] == ["left/test_left.py::test_case"]


def test_conftest_callable_globals_that_reject_attribute_access_are_not_hooks(
    tmp_path: Path,
) -> None:
    # Werkzeug context-local proxies, such as Flask's ``g`` and ``request``,
    # are callable but raise outside an application context.
    conftest = """
class Unbound:
    def __call__(self):
        raise RuntimeError("outside context")

    def __getattr__(self, name):
        raise RuntimeError("outside context")

request = Unbound()
pytest_shaped = Unbound()
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write("test_case.py", "def test_case():\n    pass\n")
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "1 passed" in result.stdout


def test_plain_setup_hooks_observe_live_arguments_and_share_native_request(tmp_path: Path) -> None:
    conftest = r"""
from dataclasses import replace
from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\n')

@pytest.fixture
def value(request):
    record('value setup')
    request.node.fixturenames.append('late_added')
    yield 3
    record('value teardown')

@pytest.fixture
def late_added():
    record('late added setup')
    yield
    record('late added teardown')

@pytest.fixture
def removed():
    raise AssertionError('removed fixture must not run')

@pytest.fixture
def added():
    record('added setup')
    yield
    record('added teardown')

@pytest.fixture
def dynamic():
    record('dynamic setup')
    yield 11
    record('dynamic teardown')

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if item.name == 'test_live':
        item.fixturenames.remove('removed')
        item.fixturenames.append('added')
        item._fixtureinfo = replace(item._fixtureinfo, argnames=('value', 'request'))
        def replacement(value, request):
            assert value == 17
            assert request is item._request
            assert request.getfixturevalue('dynamic') == 11
            record('replacement body')
        item.obj = replacement
    if hasattr(item, 'callspec'):
        item.callspec.params['number'] = 9

@pytest.hookimpl(specname='pytest_runtest_setup', trylast=True)
def pytest_setup_last(item):
    if item.name == 'test_live':
        assert item._request.getfixturevalue('value') == 3
        assert item._request.getfixturevalue('dynamic') == 11
        assert item._request.getfixturevalue('dynamic') == 11
        item.funcargs['value'] = 17
"""
    source = """import pytest

def test_live(value, removed, request):
    raise AssertionError('original callable must not run')

@pytest.mark.parametrize('number', [1, 2])
def test_parameter(number):
    assert number == 9
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", conftest)
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "3 passed" in result.stdout + result.stderr
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"].count("dynamic setup") == 1
    assert observed["pytest-native"].count("value setup") == 1


@pytest.mark.parametrize("wrapper_option", ["wrapper", "hookwrapper"])
@pytest.mark.parametrize("hook_name", ["pytest_runtest_setup", "phase_alias"])
def test_conftest_phase_wrappers_are_rejected(
    tmp_path: Path, wrapper_option: str, hook_name: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        f"""import pytest
@pytest.hookimpl(specname="pytest_runtest_setup", {wrapper_option}=True)
def {hook_name}():
    yield
""",
    )
    project.write("test_case.py", "def test_case():\n    pass\n")
    result = project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
    assert result.returncode != 0
    assert "conftest phase hook wrappers" in result.stdout + result.stderr


@pytest.mark.parametrize("body", ["unittest", "doctest"])
def test_plain_phase_hooks_share_fixture_cache_with_nonfunction_items(
    tmp_path: Path, body: str
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(autouse=True)
def value():
    record('setup')
    yield 7
    record('teardown')

@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item):
    assert item._request.getfixturevalue('value') == 7
    record('hook')
""",
        )
        if body == "unittest":
            project.write(
                "test_case.py",
                """import unittest
class TestCase(unittest.TestCase):
    def test_value(self):
        self.assertEqual(1, 1)
""",
            )
            args = []
        else:
            project.write("test_case.py", '"""\n>>> getfixture("value")\n7\n"""\n')
            args = ["--doctest-modules"]
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", *args
        )
        result.expect(0)
        observed[engine] = (project.root / "events.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"] == ["setup", "hook", "teardown"]


def test_conftest_phase_hooks_must_register_before_execution(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_case.py",
        """import types

def test_late_hook(request):
    plugin = types.ModuleType('late_conftest')
    plugin.__file__ = 'late/conftest.py'
    def pytest_runtest_setup():
        raise AssertionError('late callback must not be installed')
    plugin.pytest_runtest_setup = pytest_runtest_setup
    request.config.pluginmanager.register(plugin)
""",
    )
    result = project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
    assert result.returncode != 0
    assert "phase hooks to register before execution" in result.stdout + result.stderr


def test_collection_finish_hook_observes_full_parameter_inventory(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from pathlib import Path

def pytest_collection_finish(session):
    Path('inventory.txt').write_text('\\n'.join(item.nodeid for item in session.items))
""",
        )
        project.write(
            "test_case.py",
            """import pytest
@pytest.mark.parametrize('value', range(30))
def test_case(value):
    assert value >= 0
""",
        )
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        observed[engine] = (project.root / "inventory.txt").read_text().splitlines()
    assert observed["pytest-native"] == observed["pytest"]
    assert len(observed["pytest-native"]) == 30


@pytest.mark.parametrize("stop", ["maxfail", "shouldstop", "shouldfail"])
def test_conftest_phase_hook_stop_preserves_nextitem_and_finalizers(
    tmp_path: Path, stop: str
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope='session')
def value():
    yield
    record('finalizer')

def pytest_runtest_teardown(item, nextitem):
    record('next=' + (nextitem.name if nextitem else 'none'))
""",
        )
        action = (
            "assert False, 'stop now'"
            if stop == "maxfail"
            else f"request.session.{stop} = 'stop now'"
        )
        project.write(
            "test_case.py",
            f"""from conftest import record

def test_a(value, request):
    record('a')
    {action}

def test_b(value):
    record('b')
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            *(["-x"] if stop == "maxfail" else []),
        )
        assert result.returncode != 0, result.stdout + result.stderr
        observed[engine] = (
            result.returncode,
            (project.root / "events.txt").read_text().splitlines(),
        )
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"][1] == ["a", "next=none", "finalizer"]


@pytest.mark.parametrize("priority", ["tryfirst", "default"])
@pytest.mark.parametrize("lookup", ["fresh_function", "fresh_session", "cached_session"])
def test_setup_hook_fixture_lookup_before_runner_owner_setup(
    tmp_path: Path, priority: str, lookup: str
) -> None:
    scope = "function" if lookup == "fresh_function" else "session"
    target = "test_b" if lookup == "cached_session" else "test_a"
    decorator = "@pytest.hookimpl(tryfirst=True)" if priority == "tryfirst" else ""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            f"""from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.fixture(scope={scope!r})
def value():
    record('fixture setup')
    yield 7
    record('fixture teardown')

{decorator}
def pytest_runtest_setup(item):
    record(item.name + ' hook')
    if item.name == {target!r}:
        try:
            item._request.getfixturevalue('value')
        except BaseException as error:
            record('lookup ' + type(error).__name__)
            raise
        record('lookup succeeded')
""",
        )
        project.write(
            "test_case.py",
            """from conftest import record

def test_a(value):
    record('a body')

def test_b(value):
    record('b body')
""",
        )
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", "--junitxml=result.xml"
        )
        assert result.returncode in {0, 1}, result.stdout + result.stderr
        xml = ET.parse(project.root / "result.xml")
        observed[engine] = (
            result.returncode,
            (project.root / "events.txt").read_text().splitlines(),
            [
                (
                    case.get("name"),
                    tuple(
                        child.tag for child in case if child.tag in {"failure", "error", "skipped"}
                    ),
                )
                for case in xml.iter("testcase")
            ],
        )
    assert observed["pytest-native"] == observed["pytest"]


@pytest.mark.parametrize("project_hooks", [False, True])
def test_disabled_skipping_plugin_does_not_evaluate_markers(
    tmp_path: Path, project_hooks: bool
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        if project_hooks:
            project.write("conftest.py", "def pytest_runtest_setup():\n    pass\n")
        project.write(
            "test_case.py",
            """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\\n')

@pytest.mark.skip(reason='disabled')
def test_skip_marker():
    record('skip marker ran')

@pytest.mark.skipif('1 / 0', reason='must not evaluate')
def test_skipif_marker():
    record('skipif marker ran')

@pytest.mark.xfail(run=False, reason='disabled')
def test_notrun_marker():
    record('notrun marker ran')

@pytest.mark.xfail(strict=True, reason='disabled')
def test_strict_marker():
    record('strict marker ran')

def test_direct_skip():
    pytest.skip('direct skip remains active')

def test_direct_xfail():
    pytest.xfail('direct xfail is an ordinary failure without the plugin')
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "-p",
            "no:skipping",
            "--junitxml=result.xml",
        )
        result.expect(1)
        xml = ET.parse(project.root / "result.xml")
        observed[engine] = (
            (project.root / "events.txt").read_text().splitlines(),
            [
                (
                    case.get("name"),
                    tuple(
                        child.tag for child in case if child.tag in {"failure", "error", "skipped"}
                    ),
                )
                for case in xml.iter("testcase")
            ],
        )
    assert observed["pytest-native"] == observed["pytest"]
    assert len(observed["pytest-native"][0]) == 4


_COLLECTION_HOOKS = r"""
from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a') as stream:
        stream.write(event + '\n')

def pytest_sessionstart(session):
    record('sessionstart')

class Skipped(pytest.File):
    def collect(self):
        return []

def pytest_pycollect_makemodule(module_path, parent):
    if module_path.name.endswith('_future.py'):
        record('makemodule skipped ' + module_path.name)
        return Skipped.from_parent(parent, path=module_path)
    return None

def pytest_pycollect_makeitem(collector, name, obj):
    if name == 'helper_test':
        record('makeitem ' + name)
        return []
    return None

def pytest_generate_tests(metafunc):
    if 'number' in metafunc.fixturenames:
        metafunc.parametrize('number', [1, 2])

def pytest_make_parametrize_id(config, val, argname):
    return argname + '-' + str(val)

def pytest_itemcollected(item):
    record('itemcollected ' + item.nodeid)

def pytest_deselected(items):
    record('deselected ' + ' '.join(item.nodeid for item in items))
"""


def test_collection_hooks_in_conftest_match_pytest(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\npython_functions = test_* helper_*\n")
        project.write("conftest.py", _COLLECTION_HOOKS)
        project.write(
            "test_numbers.py",
            "def test_positive(number):\n    assert number > 0\n\n"
            "def helper_test():\n    raise AssertionError\n\n"
            "def test_other():\n    pass\n",
        )
        project.write("test_new_future.py", "raise SyntaxError('future syntax')\n")
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--deselect",
            "test_numbers.py::test_other",
            "-rA",
        )
        result.expect(0)
        passed = sorted(line for line in result.stdout.splitlines() if line.startswith("PASSED"))
        observed[engine] = (passed, (project.root / "events.txt").read_text().splitlines())
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"][0] == [
        "PASSED test_numbers.py::test_positive[number-1]",
        "PASSED test_numbers.py::test_positive[number-2]",
    ]


def test_deselected_hook_observes_each_parameter_case(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nmarkers = hidden: excluded cases\n")
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

def pytest_deselected(items):
    Path('deselected.json').write_text(json.dumps([
        [item.nodeid, item.callspec.params['value'],
         [mark.name for mark in item.iter_markers()]]
        for item in items
    ]))
""",
        )
        project.write(
            "test_cases.py",
            """import pytest

@pytest.mark.hidden
@pytest.mark.parametrize('value', [1, 2])
def test_hidden(value):
    raise AssertionError('deselected test ran')

@pytest.mark.parametrize('value', [3, 4])
def test_visible(value):
    assert value > 0
""",
        )
        result = project.run_vfy(
            "test", f"--engine={engine}", "--workers=1", "--no-cov", "-q", "-m", "not hidden"
        )
        result.expect(0)
        assert "2 passed, 2 deselected" in result.stdout
        observed[engine] = (project.root / "deselected.json").read_text()
    assert observed["pytest-native"] == observed["pytest"]


@pytest.mark.parametrize("engine", ["stock", "native"])
def test_nested_conftest_fixture_hooks_are_directory_scoped(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        "import pytest\n\n\n@pytest.fixture\ndef probe():\n    return 'probe'\n",
    )
    project.write(
        "outer/inner/conftest.py",
        """from pathlib import Path


def _record(event, request):
    with Path('events.txt').open('a') as stream:
        stream.write(f'{event} {request.node.nodeid}\\n')


def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname == 'probe':
        _record('setup', request)


def pytest_fixture_post_finalizer(fixturedef, request):
    if fixturedef.argname == 'probe':
        _record('teardown', request)
""",
    )
    project.write(
        "outer/inner/test_inner.py",
        "def test_inner(probe):\n    assert probe == 'probe'\n",
    )
    project.write(
        "outer/other/test_other.py",
        "def test_other(probe):\n    assert probe == 'probe'\n",
    )
    result = (
        project.run_pytest("-q", load_xdist=False)
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "events.txt").read_text().splitlines() == [
        "setup outer/inner/test_inner.py::test_inner",
        "teardown outer/inner/test_inner.py::test_inner",
    ]
