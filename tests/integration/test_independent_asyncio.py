"""Scoped asyncio execution through the explicit independent adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, route: str, *arguments: str) -> tuple[int, str]:
    result = project.run_engine(
        route,
        *arguments,
        stock=("-p", "pytest_asyncio.plugin", "-q"),
        independent=("--no-header",),
    )
    return result.returncode, result.stdout + result.stderr


def _config(project: VfyProject, mode: str = "strict", scope: str = "function") -> None:
    project.write(
        "pytest.ini",
        f"[pytest]\nasyncio_mode = {mode}\nasyncio_default_fixture_loop_scope = {scope}\n",
    )


@pytest.mark.parametrize(
    ("mode", "marked", "status"),
    [("strict", True, 0), ("strict", False, 1), ("auto", False, 0)],
)
def test_asyncio_discovery_matches_stock(
    tmp_path: Path, mode: str, marked: bool, status: int
) -> None:
    project = VfyProject(tmp_path)
    _config(project, mode)
    project.write(
        "test_body.py",
        "import asyncio, pytest\nfrom pathlib import Path\n"
        + ("@pytest.mark.asyncio\n" if marked else "")
        + "async def test_body():\n    await asyncio.sleep(0)\n"
        "    Path('body.txt').write_text('awaited')\n",
    )
    for route in ("stock", "independent"):
        (tmp_path / "body.txt").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == status, output
        assert (tmp_path / "body.txt").exists() == (status == 0), output
        assert "Unknown config option" not in output
        assert "Unknown pytest.mark.asyncio" not in output


@pytest.mark.parametrize("scope", ["function", "module"])
@pytest.mark.parametrize("failure", [False, True])
def test_async_generator_fixture_keeps_context_and_closes_after_cleanup(
    tmp_path: Path, scope: str, failure: bool
) -> None:
    project = VfyProject(tmp_path)
    _config(project, "auto", scope)
    project.write(
        "conftest.py",
        "import json, sys\nfrom pathlib import Path\n"
        "def pytest_sessionfinish(session):\n"
        "    path = str(Path('test_body.py').resolve())\n"
        "    matches = [m for m in sys.modules.values() if getattr(m, '__file__', None) == path]\n"
        "    module = matches[0]\n"
        "    closed = [loop.is_closed() for loop in module.loops]\n"
        "    state = {'loops': closed, 'context': module.value.get()}\n"
        "    Path('closed.json').write_text(json.dumps(state))\n",
    )
    project.write(
        "test_body.py",
        f"""import asyncio, contextvars, json, pytest
from pathlib import Path
value = contextvars.ContextVar('value', default='outside')
loops, events = [], []
@pytest.fixture(scope='{scope}')
async def resource():
    loop = asyncio.get_running_loop()
    loops.append(loop)
    value.set('fixture')
    events.append(['setup', loop.is_closed()])
    yield loop
    events.append(['cleanup', asyncio.get_running_loop() is loop, value.get()])
    Path('events.json').write_text(json.dumps(events))
@pytest.mark.asyncio(loop_scope='{scope}')
async def test_first(resource):
    assert resource is asyncio.get_running_loop()
    assert value.get() == 'fixture'
    events.append(['first'])
    assert not {failure!r}, 'deliberate async body failure'
@pytest.mark.asyncio(loop_scope='{scope}')
async def test_second(resource):
    assert resource is asyncio.get_running_loop()
    assert value.get() == 'fixture'
    events.append(['second'])
""",
    )
    expected = None
    for route in ("stock", "independent"):
        (tmp_path / "events.json").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == int(failure), output
        assert json.loads((tmp_path / "closed.json").read_text()) == {
            "loops": [True] * (2 if scope == "function" else 1),
            "context": "outside",
        }
        actual = json.loads((tmp_path / "events.json").read_text())
        if route == "stock":
            expected = actual
            assert [event[0] for event in actual].count("cleanup") == (
                2 if scope == "function" else 1
            )
        else:
            assert actual == expected


def test_runner_cancels_pending_tasks_before_scope_finishes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project)
    project.write(
        "test_body.py",
        """import asyncio, pytest
from pathlib import Path
@pytest.mark.asyncio
async def test_pending():
    async def pending():
        try:
            await asyncio.Event().wait()
        finally:
            Path('cancelled.txt').write_text('cancelled')
    asyncio.create_task(pending())
    await asyncio.sleep(0)
def test_after():
    assert Path('cancelled.txt').read_text() == 'cancelled'
""",
    )
    for route in ("stock", "independent"):
        (tmp_path / "cancelled.txt").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 0, output
        assert (tmp_path / "cancelled.txt").read_text() == "cancelled"


@pytest.mark.parametrize("explicit", [False, True])
def test_strict_mode_requires_explicit_async_fixture(tmp_path: Path, explicit: bool) -> None:
    project = VfyProject(tmp_path)
    _config(project)
    project.write(
        "test_body.py",
        "import pytest\nfrom vfy.asyncio import fixture\nfrom pathlib import Path\n"
        + ("@fixture\n" if explicit else "@pytest.fixture\n")
        + "async def resource():\n    return 17\n@pytest.mark.asyncio\n"
        "async def test_body(resource):\n    assert resource == 17\n"
        "    Path('body.txt').write_text('awaited')\n",
    )
    for route in ("stock", "independent"):
        (tmp_path / "body.txt").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == (0 if explicit else 1), output
        assert (tmp_path / "body.txt").exists() == explicit


@pytest.mark.parametrize(
    ("setting", "value"),
    [("asyncio_mode", "invalid"), ("asyncio_default_test_loop_scope", "task")],
)
def test_invalid_asyncio_config_matches_reference_timing(
    tmp_path: Path, setting: str, value: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\n{setting} = {value}\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "def pytest_runtest_logreport(report):\n"
        "    with Path('phases.txt').open('a') as stream:\n"
        "        stream.write(report.when + ':' + report.outcome + '\\n')\n",
    )
    project.write(
        "test_body.py",
        "from pathlib import Path\nPath('imported.txt').write_text('imported')\n"
        "def test_body():\n    Path('body.txt').touch()\n",
    )
    for route in ("stock", "independent"):
        code, output = _run(project, route)
        expected_status = 1 if setting == "asyncio_mode" else 4
        assert code == expected_status, output
        assert (tmp_path / "imported.txt").exists() == (expected_status == 1), output
        assert not (tmp_path / "body.txt").exists(), output
        phases = tmp_path / "phases.txt"
        assert (phases.read_text().splitlines() if phases.exists() else []) == (
            ["setup:failed", "teardown:passed"] if expected_status == 1 else []
        ), output
        (tmp_path / "imported.txt").unlink(missing_ok=True)
        phases.unlink(missing_ok=True)


def test_disabled_asyncio_does_not_validate_its_mode(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project, "invalid")
    project.write(
        "test_body.py",
        "from pathlib import Path\ndef test_body():\n    Path('body.txt').touch()\n",
    )
    for route in ("stock", "independent"):
        code, output = _run(project, route, "-p", "no:pytest_asyncio.plugin")
        assert code == 0, output
        assert (tmp_path / "body.txt").is_file(), output
        (tmp_path / "body.txt").unlink()


def test_asyncio_candidate_uses_owned_adapters_and_detects_failure(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project, "auto")
    project.write(
        "test_body.py",
        """import asyncio, sys, pytest, pytest_asyncio
from pathlib import Path
@pytest.fixture
async def resource():
    yield asyncio.get_running_loop()
    Path('cleanup.txt').write_text('closed')
async def test_body(resource):
    assert resource is asyncio.get_running_loop()
    for name in ('pluggy', 'packaging', 'vfy.asyncio_plugin'):
        assert name not in sys.modules, name
    assert sys.modules['pytest_asyncio'].__file__.endswith('/vfy/independent/asyncio_modules.py')
    public_source = Path(sys.modules['pytest'].__file__)
    assert public_source.is_file()
    assert (public_source.name, public_source.parent.name) == ('__init__.py', 'pytest')
    Path('body.txt').write_text('awaited')
    assert False, 'deliberate async body failure'
""",
    )
    code, output = _run(project, "independent")
    assert code == 1, output
    assert "deliberate async body failure" in output
    assert (tmp_path / "body.txt").read_text() == "awaited"
    assert (tmp_path / "cleanup.txt").read_text() == "closed"


def test_parameterized_policy_expands_implicit_runner_dependency(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project)
    project.write(
        "test_body.py",
        """import asyncio, pytest
from pathlib import Path
@pytest.fixture(scope='session', params=[0, 1])
def event_loop_policy(request):
    policy = asyncio.DefaultEventLoopPolicy()
    policy.parameter = request.param
    return policy
@pytest.mark.asyncio
async def test_body(event_loop_policy):
    with Path('parameters.txt').open('a') as stream:
        stream.write(str(event_loop_policy.parameter))
""",
    )
    for route in ("stock", "independent"):
        (tmp_path / "parameters.txt").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 0, output
        assert (tmp_path / "parameters.txt").read_text() == "01"


def test_setup_can_change_live_test_loop_scope(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project, "auto", scope="module")
    project.write(
        "conftest.py",
        "import pytest\ndef pytest_runtest_setup(item):\n"
        "    item.add_marker(pytest.mark.asyncio(loop_scope='module'), append=False)\n",
    )
    project.write(
        "test_body.py",
        """import asyncio, pytest
from pathlib import Path
@pytest.fixture(scope='module')
async def resource():
    return asyncio.get_running_loop()
@pytest.mark.asyncio
async def test_body(resource):
    assert resource is asyncio.get_running_loop()
    Path('body.txt').write_text('same loop')
""",
    )
    for route in ("stock", "independent"):
        (tmp_path / "body.txt").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 0, output
        assert (tmp_path / "body.txt").read_text() == "same loop"


def test_sync_callable_returning_coroutine_is_unhandled(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project)
    project.write(
        "test_body.py",
        """from pathlib import Path
async def body():
    Path('awaited.txt').write_text('awaited')
def test_body():
    return body()
""",
    )
    for route in ("stock", "independent"):
        code, output = _run(project, route)
        assert code == 1, output
        assert "async def functions are not natively supported" in output
        assert not (tmp_path / "awaited.txt").exists()


def test_failed_async_fixture_acquisition_allows_following_reuse(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project, "auto")
    project.write(
        "test_body.py",
        """import asyncio, json, pytest
from pathlib import Path
events = []
@pytest.fixture(params=['bad', 'good'])
async def resource(request):
    events.append(['setup', request.param])
    Path('events.json').write_text(json.dumps(events))
    if request.param == 'bad':
        raise ValueError('deliberate acquisition failure')
    yield asyncio.get_running_loop()
    events.append(['cleanup', request.param])
    Path('events.json').write_text(json.dumps(events))
async def test_body(resource):
    assert resource is asyncio.get_running_loop()
    events.append(['body'])
""",
    )
    expected = [["setup", "bad"], ["setup", "good"], ["body"], ["cleanup", "good"]]
    for route in ("stock", "independent"):
        (tmp_path / "events.json").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 1, output
        assert "deliberate acquisition failure" in output
        assert json.loads((tmp_path / "events.json").read_text()) == expected


def test_cli_mode_overrides_ini_and_debug_reaches_loop(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project)
    project.write(
        "test_body.py",
        """import asyncio
from pathlib import Path
async def test_body():
    assert asyncio.get_running_loop().get_debug()
    Path('body.txt').write_text('debug')
""",
    )
    for route in ("stock", "independent"):
        (tmp_path / "body.txt").unlink(missing_ok=True)
        code, output = _run(project, route, "--asyncio-mode=auto", "--asyncio-debug")
        assert code == 0, output
        assert (tmp_path / "body.txt").read_text() == "debug"


@pytest.mark.parametrize("generator", [False, True])
def test_dependency_can_replace_async_fixture_with_sync_code(
    tmp_path: Path, generator: bool
) -> None:
    project = VfyProject(tmp_path)
    _config(project, "auto")
    project.write(
        "test_body.py",
        "import pytest\nfrom pathlib import Path\n"
        "def synchronous(change):\n    return 17\n"
        "@pytest.fixture\ndef change():\n"
        "    resource._get_wrapped_function().__code__ = synchronous.__code__\n"
        "@pytest.fixture\nasync def resource(change):\n"
        + ("    yield 0\n" if generator else "    return 0\n")
        + "def test_body(resource):\n    assert resource == 17\n"
        "    Path('body.txt').write_text('synchronous fixture')\n",
    )
    for route in ("stock", "independent"):
        (tmp_path / "body.txt").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 0, output
        assert (tmp_path / "body.txt").read_text() == "synchronous fixture"


def test_policy_parameter_change_finishes_async_fixture_before_loop_closes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project, scope="module")
    project.write(
        "test_body.py",
        """import asyncio, json, pytest
from pathlib import Path
from vfy.asyncio import fixture
events = []
@pytest.fixture(scope='session', params=[False, True])
def event_loop_policy(request):
    tag = request.param
    class Policy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            loop = super().new_event_loop()
            loop.tag = tag
            return loop
    return Policy()
@fixture(scope='module', loop_scope='module')
async def resource():
    loop = asyncio.get_running_loop()
    events.append(['setup', loop.tag])
    yield loop
    assert not loop.is_closed()
    assert asyncio.get_running_loop() is loop
    events.append(['cleanup', loop.tag])
    Path('events.json').write_text(json.dumps(events))
@pytest.mark.asyncio(loop_scope='module')
async def test_body(resource):
    assert resource is asyncio.get_running_loop()
    events.append(['body', resource.tag])
""",
    )
    expected = [[event, debug] for debug in (False, True) for event in ("setup", "body", "cleanup")]
    for route in ("stock", "independent"):
        (tmp_path / "events.json").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 0, output
        assert json.loads((tmp_path / "events.json").read_text()) == expected


def test_fixture_rejects_a_runner_with_narrower_scope(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project, "auto")
    project.write(
        "test_body.py",
        """import pytest
from pathlib import Path
@pytest.fixture(scope='module')
async def resource():
    Path('setup.txt').write_text('setup')
    yield None
@pytest.mark.asyncio
async def test_body(resource):
    Path('body.txt').write_text('body')
""",
    )
    for route in ("stock", "independent"):
        code, output = _run(project, route)
        assert code == 1, output
        assert "ScopeMismatch" in output
        assert not (tmp_path / "setup.txt").exists()
        assert not (tmp_path / "body.txt").exists()


def test_parameter_marks_keep_distinct_runner_fixture_closures(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_sessionfinish():
    from test_body import loops
    Path('loops.json').write_text(json.dumps([
        loops[0] is loops[2], loops[0] is loops[1],
        [loop.is_closed() for loop in loops],
    ]))
""",
    )
    project.write(
        "test_body.py",
        """import asyncio
import pytest

loops = []
@pytest.mark.parametrize('value', [
    pytest.param(0, marks=pytest.mark.asyncio(loop_scope='module')),
    pytest.param(1, marks=pytest.mark.asyncio(loop_scope='function')),
    pytest.param(2, marks=pytest.mark.asyncio(loop_scope='module')),
])
async def test_body(value):
    loops.append(asyncio.get_running_loop())
""",
    )
    for route in ("stock", "independent"):
        (tmp_path / "loops.json").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 0, output
        assert json.loads((tmp_path / "loops.json").read_text()) == [
            True,
            False,
            [True, True, True],
        ]


def test_async_classification_preserves_repeated_custom_item_registrations(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _config(project)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest
from pytest_asyncio import is_async_test

class CustomItem(pytest.Item):
    def runtest(self):
        pass

class CustomFile(pytest.File):
    def collect(self):
        item = CustomItem.from_parent(self, name='duplicate')
        return [item, item]

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.case':
        return CustomFile.from_parent(parent, path=file_path)

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    observed = [[item.name, is_async_test(item)] for item in items]
    observed.append(['repeat', is_async_test(items[1]), is_async_test(items[1])])
    observed.append(['custom_identity', items[-1] is items[-2]])
    observed.append(['foreign', is_async_test(object()), is_async_test(items[0].parent)])
    Path('classification.json').write_text(json.dumps(observed))
""",
    )
    project.write(
        "test_body.py",
        """import pytest

def test_sync():
    pass

@pytest.mark.asyncio
@pytest.mark.parametrize('value', [0, 1])
async def test_async(value):
    pass
""",
    )
    project.write("zz.case", "")
    for route in ("stock", "independent"):
        (tmp_path / "classification.json").unlink(missing_ok=True)
        code, output = _run(project, route)
        assert code == 0, output
        assert json.loads((tmp_path / "classification.json").read_text()) == [
            ["test_sync", False],
            ["test_async[0]", True],
            ["test_async[1]", True],
            ["duplicate", False],
            ["duplicate", False],
            ["repeat", True, True],
            ["custom_identity", True],
            ["foreign", False, False],
        ]


def test_none_and_awaitable_results_preserve_live_async_protocol(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

events = []
def pytest_sessionfinish():
    Path('awaitable-probes.json').write_text(json.dumps(events))
""",
    )
    project.write(
        "test_results.py",
        """from conftest import events

class Awaitable:
    def __getattribute__(self, name):
        if name in ('__await__', '__aiter__'):
            events.append(name)
        return object.__getattribute__(self, name)
    def __await__(self):
        yield

def test_none():
    events.append('none')
    return None

def test_awaitable():
    return Awaitable()
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(1)
    expected = json.loads((project.root / "awaitable-probes.json").read_text())
    assert expected == ["none", "__await__"]
    (project.root / "awaitable-probes.json").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    assert json.loads((project.root / "awaitable-probes.json").read_text()) == expected


def test_unrelated_coroutine_markers_leave_sync_functions_callable(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_markers.py",
        """from pathlib import Path
import pytest

def unrelated_marker(function):
    function._is_coroutine_marker = object()
    return function

@pytest.fixture
@unrelated_marker
def value():
    return 42

@unrelated_marker
def test_sync(value):
    Path('ran').write_text(str(value))
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    assert (tmp_path / "ran").read_text() == "42"
    (tmp_path / "ran").unlink()
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert (tmp_path / "ran").read_text() == "42"


def test_loop_factory_hook_is_rejected_with_a_usage_error(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import asyncio\n"
        "\n"
        "def pytest_asyncio_loop_factories(config, item):\n"
        "    return {'custom': asyncio.new_event_loop}\n",
    )
    project.write(
        "test_factory.py",
        "import pytest\n\n@pytest.mark.asyncio\nasync def test_factory():\n    pass\n",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov")
    result.expect(4)
    assert "Traceback" not in result.stderr
    assert "pytest_asyncio_loop_factories is not supported" in result.stderr
    assert "use --engine=pytest-native" in result.stderr


def test_loop_factories_marker_argument_is_a_collection_error(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_factory.py",
        "import pytest\n\n"
        "@pytest.mark.asyncio(loop_factories=['custom'])\n"
        "async def test_factory():\n    pass\n",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov")
    result.expect(2)
    assert "Traceback" not in result.stdout + result.stderr
    assert "ERROR collecting test_factory.py" in result.stdout
    assert "loop_factories is not supported" in result.stdout
    assert "use --engine=pytest-native" in result.stdout
