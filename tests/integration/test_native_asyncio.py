"""Differential coverage of the native runner's vfy asyncio boundary."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support.history import read_history
from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native asyncio differential requires Linux or macOS",
)


@pytest.mark.parametrize("initial_mark", [False, True], ids=["add", "change-scope"])
def test_setup_asyncio_marks_control_test_execution(tmp_path: Path, initial_mark: bool) -> None:
    decorator = "@pytest.mark.asyncio(loop_scope='function')\n" if initial_mark else ""
    source = f"""import asyncio
from pathlib import Path
import pytest
loops = []
@pytest.fixture
def mark_async(request):
    request.node.add_marker(pytest.mark.asyncio(loop_scope='module'), append=False)
{decorator}async def test_first(mark_async):
    loops.append(asyncio.get_running_loop())
    Path('first-ran.txt').write_text('ran')
{decorator}async def test_second(mark_async):
    assert asyncio.get_running_loop() is loops[0]
    Path('second-ran.txt').write_text('ran')
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert "2 passed" in transcript, transcript
        assert (project.root / "first-ran.txt").read_text() == "ran"
        assert (project.root / "second-ran.txt").read_text() == "ran"
        assert "was never awaited" not in transcript, transcript


def _async_project_source(mode: str, fixture_module: str, fixture_import: str) -> str:
    decorator = (
        f"from {fixture_module} import {fixture_import} as async_fixture\n"
        if fixture_module == "pytest_asyncio"
        else f"from vfy import {fixture_import} as async_fixture\n"
    )
    fixture_decorator = (
        "@pytest.fixture(scope='module')\n"
        if mode == "auto"
        else "@async_fixture(scope='module', loop_scope='module')\n"
    )
    marker = "" if mode == "auto" else "@pytest.mark.asyncio(loop_scope='module')\n"
    return (
        "import asyncio\n"
        "import contextvars\n"
        "import pytest\n"
        + decorator
        + "\n"
        + "current = contextvars.ContextVar('current', default='outside')\n"
        + "events = []\n"
        + "\n"
        + fixture_decorator
        + "async def resource():\n"
        + "    loop = asyncio.get_running_loop()\n"
        + "    current.set('fixture')\n"
        + "    events.append(('setup', id(loop)))\n"
        + "    yield loop\n"
        + "    assert asyncio.get_running_loop() is loop\n"
        + "    assert current.get() == 'fixture'\n"
        + "    events.append(('teardown', id(loop)))\n"
        + "\n"
        + marker
        + "async def test_resource(resource):\n"
        + "    loop = asyncio.get_running_loop()\n"
        + "    assert resource is loop\n"
        + "    assert current.get() == 'fixture'\n"
        + "    events.append(('test', id(loop)))\n"
        + "\n"
        + marker
        + "async def test_same_scoped_loop():\n"
        + "    loop = asyncio.get_running_loop()\n"
        + "    assert current.get() == 'fixture'\n"
        + "    events.append(('test', id(loop)))\n"
    )


def _context_project_source() -> str:
    return (
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "from test_a_async import current, events\n"
        "\n"
        "def test_asyncgen_finalizer_restored_context_and_shared_loop():\n"
        "    assert current.get() == 'outside'\n"
        "    assert [event[0] for event in events] == ['setup', 'test', 'test', 'teardown']\n"
        "    assert len({event[1] for event in events}) == 1\n"
        "    Path('trace.json').write_text(json.dumps(events), encoding='utf-8')\n"
    )


def _normalized_trace(path: Path) -> list[tuple[str, int]]:
    events_raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(events_raw, list)
    loop_ids: dict[int, int] = {}
    normalized = []
    for event in events_raw:
        assert isinstance(event, list)
        assert len(event) == 2
        name, loop_id = event
        assert isinstance(name, str)
        assert isinstance(loop_id, int)
        normalized.append((name, loop_ids.setdefault(loop_id, len(loop_ids))))
    return normalized


def _normalized_coverage(project: VfyProject, source: Path) -> tuple[object, ...]:
    data = project.data()
    contexts = tuple(
        sorted(
            (line, tuple(sorted(values)))
            for line, values in data.contexts_by_lineno(str(source)).items()
        )
    )
    return tuple(data.lines(str(source)) or ()), tuple(data.arcs(str(source)) or ()), contexts


def _normalized_history(project: VfyProject) -> dict[str, tuple[tuple[str, str | None], ...]]:
    history = read_history(project.root / ".vfy" / "test-history.sqlite3")
    return {
        nodeid: tuple(
            (observation["outcome"], observation.get("failed_phase"))
            for observation in record["observations"]
        )
        for nodeid, record in history["tests"].items()
    }


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_native_async_lifecycle_matches_stock_vfy_and_pytest_asyncio(
    tmp_path: Path, mode: str
) -> None:
    pytest.importorskip("pytest_asyncio")

    projects: dict[str, VfyProject] = {}
    for name in ("pytest-native", "vfy", "upstream"):
        project = VfyProject(tmp_path / name)
        fixture_module, fixture_import = (
            ("pytest_asyncio", "fixture") if name == "upstream" else ("vfy", "async_fixture")
        )
        project.write(
            "test_a_async.py",
            _async_project_source(mode, fixture_module, fixture_import),
        )
        project.write("test_z_context.py", _context_project_source())
        projects[name] = project

    common = [
        "-q",
        "-o",
        f"asyncio_mode={mode}",
        "-o",
        "asyncio_default_fixture_loop_scope=module",
        "-o",
        "asyncio_default_test_loop_scope=module",
    ]
    native = projects["pytest-native"].run_vfy(
        "test", "--engine=pytest-native", "--workers=1", "--no-cov", *common
    )
    stock = projects["vfy"].run_vfy("test", "--engine=pytest", "--workers=1", "--no-cov", *common)
    upstream = projects["upstream"].run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        *common,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert native.returncode == stock.returncode == upstream.returncode == 0, (
        native.stdout
        + native.stderr
        + stock.stdout
        + stock.stderr
        + upstream.stdout
        + upstream.stderr
    )
    assert all("3 passed" in result.stdout for result in (native, stock, upstream))

    traces = {
        name: _normalized_trace(projects[name].root / "trace.json")
        for name in ("pytest-native", "vfy", "upstream")
    }
    expected = [("setup", 0), ("test", 0), ("test", 0), ("teardown", 0)]
    assert traces == {"pytest-native": expected, "vfy": expected, "upstream": expected}


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_rejected_loop_installation_closes_factory_loop_and_allows_reuse(
    tmp_path: Path, engine: str, cleanup_fails: bool
) -> None:
    project = VfyProject(tmp_path / engine)
    project.write(
        "conftest.py",
        "import asyncio\n"
        "import json\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "import warnings\n"
        "\n"
        "created_loops = []\n"
        "\n"
        "def make_loop():\n"
        "    loop = asyncio.new_event_loop()\n"
        f"    if {cleanup_fails!r} and not created_loops:\n"
        "        real_close = loop.close\n"
        "        def close():\n"
        "            real_close()\n"
        "            raise RuntimeError('cleanup rejected')\n"
        "        loop.close = close\n"
        "    created_loops.append(loop)\n"
        "    return loop\n"
        "\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore', DeprecationWarning)\n"
        "    _PolicyBase = type(asyncio.get_event_loop_policy())\n"
        "\n"
        "class RejectFirstInstallPolicy(_PolicyBase):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.reject_first_install = True\n"
        "\n"
        "    def set_event_loop(self, loop):\n"
        "        if loop is not None and self.reject_first_install:\n"
        "            self.reject_first_install = False\n"
        "            raise RuntimeError('first custom loop installation rejected')\n"
        "        super().set_event_loop(loop)\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def event_loop_policy():\n"
        "    policy = RejectFirstInstallPolicy()\n"
        "    yield policy\n"
        "    Path('loop-states.json').write_text(\n"
        "        json.dumps([loop.is_closed() for loop in created_loops]),\n"
        "        encoding='utf-8',\n"
        "    )\n"
        "\n"
        "def pytest_asyncio_loop_factories(config, item):\n"
        "    return {'custom': make_loop}\n",
    )
    project.write(
        "test_loop_lifecycle.py",
        "import asyncio\n"
        "import conftest\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_reuse_after_rejected_install_one():\n"
        "    assert len(conftest.created_loops) == 2\n"
        "    assert conftest.created_loops[0].is_closed()\n"
        "    assert asyncio.get_running_loop() is conftest.created_loops[1]\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_reuse_after_rejected_install_two():\n"
        "    assert len(conftest.created_loops) == 2\n"
        "    assert conftest.created_loops[0].is_closed()\n"
        "    assert asyncio.get_running_loop() is conftest.created_loops[1]\n",
    )

    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        "-q",
        "-W",
        "error::RuntimeWarning",
        "--junitxml=errors.xml",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "RuntimeError: first custom loop installation rejected" in output
    errors = list(ET.parse(project.root / "errors.xml").iter("error"))
    assert len(errors) == 1
    assert (
        errors[0]
        .attrib["message"]
        .startswith('failed on setup with "RuntimeError: first custom loop installation rejected')
    )
    if cleanup_fails:
        assert "Async runner cleanup also failed: RuntimeError('cleanup rejected')" in output
    assert "1 error" in output
    assert "1 passed" in output
    policy_warning = (
        "The event_loop_policy fixture override is deprecated; configure event loops "
        "through pytest_asyncio_loop_factories"
    )
    assert output.count(policy_warning) == 1
    assert json.loads((project.root / "loop-states.json").read_text(encoding="utf-8")) == [
        True,
        True,
    ]


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
def test_event_loop_policy_override_warning_respects_warnings_as_errors(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path / engine)
    project.write(
        "conftest.py",
        "import asyncio\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def event_loop_policy():\n"
        "    Path('policy-ran').touch()\n"
        "    return asyncio.get_event_loop_policy()\n",
    )
    project.write(
        "test_policy_warning.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "def test_sync_body_does_not_run_after_policy_warning(event_loop_policy):\n"
        "    Path('sync-body-ran').touch()\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_async_body_does_not_run_after_policy_warning(event_loop_policy):\n"
        "    Path('async-body-ran').touch()\n",
    )

    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        "-q",
        "-W",
        "error::pytest.PytestDeprecationWarning",
        "--junitxml=errors.xml",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    output = result.stdout + result.stderr
    policy_warning = (
        "The event_loop_policy fixture override is deprecated; configure event loops "
        "through pytest_asyncio_loop_factories"
    )

    assert result.returncode == 1, output
    errors = list(ET.parse(project.root / "errors.xml").iter("error"))
    assert len(errors) == 2, output
    assert all(
        f"pytest.PytestDeprecationWarning: {policy_warning}" in (error.text or "")
        for error in errors
    ), output
    assert "2 errors" in output
    assert "AssertionError" not in output
    assert not (project.root / "policy-ran").exists()
    assert not (project.root / "sync-body-ran").exists()
    assert not (project.root / "async-body-ran").exists()


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_native_async_coverage_contexts_and_history_match_stock_vfy(
    tmp_path: Path, mode: str
) -> None:
    projects: dict[str, VfyProject] = {}
    sources: dict[str, Path] = {}
    for name in ("pytest-native", "vfy"):
        project = VfyProject(tmp_path / name)
        sources[name] = project.write(
            "test_a_async.py",
            _async_project_source(mode, "vfy", "async_fixture"),
        )
        project.write("test_context.py", _context_project_source())
        projects[name] = project

    arguments = [
        "--workers=1",
        "-q",
        "-o",
        f"asyncio_mode={mode}",
        "-o",
        "asyncio_default_fixture_loop_scope=module",
        "-o",
        "asyncio_default_test_loop_scope=module",
        "--cov=.",
        "--cov-context=test",
        "--cov-report=",
    ]
    native = projects["pytest-native"].run_vfy("test", "--engine=pytest-native", *arguments)
    stock = projects["vfy"].run_vfy("test", "--engine=pytest", *arguments)
    assert native.returncode == stock.returncode == 0, (
        native.stdout + native.stderr + stock.stdout + stock.stderr
    )

    assert _normalized_coverage(
        projects["pytest-native"], sources["pytest-native"]
    ) == _normalized_coverage(projects["vfy"], sources["vfy"])
    assert _normalized_history(projects["pytest-native"]) == _normalized_history(projects["vfy"])


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
def test_async_fixture_teardown_failure_is_recorded_after_finalizer(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path / engine)
    project.write(
        "test_async_failure.py",
        "import asyncio\n"
        "import json\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "import vfy\n\n"
        "events = []\n\n"
        "@vfy.async_fixture(scope='module', loop_scope='module')\n"
        "async def resource():\n"
        "    events.append('setup')\n"
        "    yield object()\n"
        "    assert asyncio.get_running_loop().is_running()\n"
        "    events.append('teardown')\n"
        "    Path('trace.json').write_text(json.dumps(events), encoding='utf-8')\n"
        "    raise RuntimeError('async teardown failed')\n\n"
        "@pytest.mark.asyncio(loop_scope='module')\n"
        "async def test_resource(resource):\n"
        "    events.append('test')\n"
        "    assert resource is not None\n",
    )
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(1)
    assert "async teardown failed" in result.stdout + result.stderr
    assert json.loads((project.root / "trace.json").read_text(encoding="utf-8")) == [
        "setup",
        "test",
        "teardown",
    ]
    history = _normalized_history(project)
    assert history == {"test_async_failure.py::test_resource": (("failed", "teardown"),)}


@pytest.mark.parametrize("engine", ["pytest-native", "pytest"])
def test_async_fixture_finalizer_runs_after_keyboard_interrupt(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path / engine)
    project.write(
        "test_async_interrupt.py",
        "import asyncio\n"
        "import json\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "import vfy\n\n"
        "events = []\n\n"
        "@vfy.async_fixture(scope='module', loop_scope='module')\n"
        "async def resource():\n"
        "    events.append('setup')\n"
        "    yield object()\n"
        "    assert asyncio.get_running_loop().is_running()\n"
        "    events.append('teardown')\n"
        "    Path('trace.json').write_text(json.dumps(events), encoding='utf-8')\n\n"
        "@pytest.mark.asyncio(loop_scope='module')\n"
        "async def test_resource(resource):\n"
        "    events.append('test')\n"
        "    raise KeyboardInterrupt()\n",
    )
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--workers=1",
        "--no-cov",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert json.loads((project.root / "trace.json").read_text(encoding="utf-8")) == [
        "setup",
        "test",
        "teardown",
    ]


@pytest.mark.parametrize("mode", ["strict", "auto"])
def test_native_strict_mode_matches_unhandled_async_fixture_behavior(
    tmp_path: Path, mode: str
) -> None:
    pytest.importorskip(
        "pytest_asyncio",
        reason="the upstream strict-mode oracle requires pytest-asyncio",
    )
    pytest_version = int(pytest.version_tuple[0]), int(pytest.version_tuple[1])
    expects_error = mode == "strict" and pytest_version >= (9, 1)
    expects_warning = mode == "strict" and (8, 4) <= pytest_version < (9, 1)
    projects: dict[str, VfyProject] = {}
    source = (
        "import pytest\n\n"
        "@pytest.fixture\n"
        "async def ordinary():\n"
        "    return 42\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_ordinary(ordinary):\n"
        "    value = await ordinary if hasattr(ordinary, '__await__') else ordinary\n"
        "    assert value == 42\n"
    )
    for name in ("pytest-native", "vfy", "upstream"):
        project = VfyProject(tmp_path / name)
        project.write("test_async_fixture.py", source)
        projects[name] = project

    native = projects["pytest-native"].run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--no-cov",
        "-q",
        "-o",
        f"asyncio_mode={mode}",
    )
    stock = projects["vfy"].run_vfy(
        "test",
        "--engine=pytest",
        "--workers=1",
        "--no-cov",
        "-q",
        "-o",
        f"asyncio_mode={mode}",
    )
    upstream = projects["upstream"].run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-q",
        "-o",
        f"asyncio_mode={mode}",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    expected_status = 1 if expects_error else 0
    assert native.returncode == stock.returncode == upstream.returncode == expected_status, (
        native.stdout
        + native.stderr
        + stock.stdout
        + stock.stderr
        + upstream.stdout
        + upstream.stderr
    )
    warning_text = "requested async @pytest.fixture 'ordinary' in strict mode"
    error_text = (
        "'test_ordinary' requested an async fixture 'ordinary', with no plugin or hook that "
        "handled it. This is an error, as pytest does not natively support it."
    )
    assert (warning_text in native.stdout + native.stderr) is False
    assert (warning_text in stock.stdout + stock.stderr) is False
    if expects_warning:
        warning = (
            "'test_ordinary' requested an async fixture 'ordinary', with no plugin or hook that "
            "handled it. This is usually an error, as pytest does not natively support it."
        )
        for result in (native, stock, upstream):
            assert warning in result.stdout + result.stderr
    elif not expects_error:
        assert "requested an async fixture 'ordinary'" not in (
            native.stdout
            + native.stderr
            + stock.stdout
            + stock.stderr
            + upstream.stdout
            + upstream.stderr
        )
    if expects_error:
        assert error_text in native.stdout + native.stderr
        assert error_text in stock.stdout + stock.stderr
        assert error_text in upstream.stdout + upstream.stderr
        expected_history = {"test_async_fixture.py::test_ordinary": (("failed", "setup"),)}
        assert _normalized_history(projects["pytest-native"]) == expected_history
        assert _normalized_history(projects["vfy"]) == expected_history


def test_parameter_asyncio_marks_preserve_loop_scope_and_marker_precedence(tmp_path: Path) -> None:
    pytest.importorskip("pytest_asyncio")
    source = """import asyncio
import pytest

loops = []
@pytest.mark.parametrize("value", [
    pytest.param(0, marks=pytest.mark.asyncio(loop_scope="module")),
    pytest.param(1, marks=pytest.mark.asyncio(loop_scope="module")),
    pytest.param(2, marks=pytest.mark.asyncio(loop_scope="function")),
])
async def test_parameter_mark(value):
    loop = asyncio.get_running_loop()
    if value == 2:
        assert loop is not loops[0]
    else:
        if loops:
            assert loop is loops[0]
        loops.append(loop)

@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("value", [
    pytest.param(0, marks=pytest.mark.asyncio(loop_scope="module")),
])
async def test_function_mark_wins(value):
    assert asyncio.get_running_loop() is not loops[0]

class TestParentMark:
    pytestmark = pytest.mark.asyncio(loop_scope="class")

    @pytest.mark.parametrize("value", [
        pytest.param(0, marks=pytest.mark.asyncio(loop_scope="module")),
    ])
    async def test_parameter_mark_wins(self, value):
        assert asyncio.get_running_loop() is loops[0]
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_marks.py", source)
        result = (
            project.run_pytest("-q", "-p", "pytest_asyncio.plugin", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "5 passed" in result.stdout


@pytest.mark.parametrize("becomes_async", [True, False], ids=["sync-to-async", "async-to-sync"])
def test_fixture_replacing_test_code_refreshes_asyncio_execution(
    tmp_path: Path, becomes_async: bool
) -> None:
    source = f"""from pathlib import Path
import pytest
import vfy

@pytest.fixture
def rewrite(request):
    {"async " if becomes_async else ""}def replacement(rewrite, request):
        assert vfy.asyncio.is_async_test(request.node) is {becomes_async!r}
        Path("replacement-ran").touch()
    request.function.__code__ = replacement.__code__

@pytest.mark.asyncio
{"" if becomes_async else "async "}def test_case(rewrite, request):
    Path("stale-body-ran").touch()
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=function\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        assert result.returncode == 0, transcript
        assert (project.root / "replacement-ran").exists()
        assert not (project.root / "stale-body-ran").exists()
        assert "was never awaited" not in transcript
        assert ("has an asyncio mark but its callable is synchronous" in transcript) == (
            not becomes_async
        )


def test_auto_mode_preserves_unmarked_code_replacement_routing(tmp_path: Path) -> None:
    source = """from pathlib import Path
import pytest

@pytest.fixture
def rewrite(request):
    async def replacement(rewrite):
        Path("replacement-ran").touch()
    request.function.__code__ = replacement.__code__

def test_case(rewrite): pass
"""
    results = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini",
            "[pytest]\nasyncio_mode=auto\nasyncio_default_fixture_loop_scope=function\n",
        )
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        transcript = result.stdout + result.stderr
        results[engine] = result
        assert "async def functions are not natively supported" in transcript
        assert "was never awaited" not in transcript
        assert not (project.root / "replacement-ran").exists()
    assert results["pytest-native"].returncode == results["pytest"].returncode


@pytest.mark.parametrize("becomes_async", [False, True])
def test_runner_acquisition_refreshes_fixture_callable(tmp_path: Path, becomes_async: bool) -> None:
    initial_kind = "" if becomes_async else "async "
    replacement_kind = "async " if becomes_async else ""
    source = f"""import asyncio
import pytest
import vfy

@pytest.fixture(name='_function_scoped_runner')
def runner():
    {replacement_kind}def replacement():
        return 42
    value.__wrapped__.__code__ = replacement.__code__
    with asyncio.Runner() as active:
        yield active

@vfy.async_fixture
{initial_kind}def value():
    return 1

def test_case(value):
    assert value == 42
"""
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_case.py", source)
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
