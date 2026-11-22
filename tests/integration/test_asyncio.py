"""Public pytest boundary tests for vfy's built-in asyncio support."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
import pytest_asyncio

import vfy
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


def _collection_ids(output: str) -> tuple[str, ...]:
    return tuple(line for line in output.splitlines() if line and " collected in " not in line)


def test_async_fixture_signature_matches_pinned_pytest_asyncio() -> None:
    actual = inspect.signature(vfy.async_fixture)
    oracle = inspect.signature(pytest_asyncio.fixture)
    assert [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in actual.parameters.values()
    ] == [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in oracle.parameters.values()
    ]


def test_public_asyncio_imports_do_not_initialize_coverage(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    script = (
        "import sys\n"
        "import vfy\n"
        "assert 'coverage' not in sys.modules\n"
        "assert 'vfy.api' not in sys.modules\n"
        "assert callable(vfy.async_fixture)\n"
        "assert callable(vfy.asyncio.fixture)\n"
        "assert callable(vfy.asyncio.is_async_test)\n"
        "assert 'asyncio' not in sys.modules\n"
        "assert 'vfy.api' not in sys.modules\n"
    )
    result = project.run_python("-c", script)
    result.expect(0)


def test_direct_pytest_runs_marked_async_tests_and_async_fixtures(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_async.py",
        "import asyncio\n"
        "import contextvars\n"
        "import vfy\n"
        "import pytest\n"
        "\n"
        "value = contextvars.ContextVar('value', default='outer')\n"
        "events = []\n"
        "\n"
        "@vfy.async_fixture\n"
        "async def client():\n"
        "    loop = asyncio.get_running_loop()\n"
        "    value.set('fixture')\n"
        "    events.append(('setup', loop))\n"
        "    yield loop\n"
        "    events.append(('teardown', asyncio.get_running_loop()))\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_request(client):\n"
        "    assert value.get() == 'fixture'\n"
        "    assert client is asyncio.get_running_loop()\n"
        "    events.append(('test', client))\n"
        "\n"
        "def test_teardown_finished_and_context_restored():\n"
        "    assert [name for name, _loop in events] == ['setup', 'test', 'teardown']\n"
        "    assert events[0][1] is events[1][1] is events[2][1]\n"
        "    assert value.get() == 'outer'\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)


def test_auto_mode_handles_ordinary_asyncio_fixtures_and_module_loops(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_auto.py",
        "import asyncio\n"
        "import pytest\n"
        "\n"
        "loops = []\n"
        "\n"
        "@pytest.fixture\n"
        "async def answer():\n"
        "    return 42\n"
        "\n"
        "@pytest.mark.asyncio(loop_scope='module')\n"
        "async def test_one(answer):\n"
        "    loops.append(asyncio.get_running_loop())\n"
        "    assert answer == 42\n"
        "\n"
        "@pytest.mark.asyncio(loop_scope='module')\n"
        "async def test_two():\n"
        "    loops.append(asyncio.get_running_loop())\n"
        "\n"
        "def test_same_module_loop():\n"
        "    assert loops[0] is loops[1]\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_mode=auto",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)


def test_loop_factory_hook_and_port_fixtures_match_public_asyncio_surface(tmp_path: Path) -> None:
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
        "import asyncio\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.asyncio(loop_factories=['custom'])\n"
        "async def test_factory(unused_tcp_port, unused_udp_port):\n"
        "    loop = asyncio.get_running_loop()\n"
        "    assert loop.get_debug() is False\n"
        "    assert 1024 <= unused_tcp_port <= 65535\n"
        "    assert 1024 <= unused_udp_port <= 65535\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)


def test_collection_and_outcome_match_pinned_pytest_asyncio_oracle(tmp_path: Path) -> None:
    actual_project = VfyProject(tmp_path / "vfy")
    oracle_project = VfyProject(tmp_path / "upstream")
    source = (
        "import asyncio\n"
        "import {fixture_module}\n"
        "import pytest\n"
        "\n"
        "events = []\n"
        "\n"
        "@{fixture_module}.fixture\n"
        "async def client():\n"
        "    loop = asyncio.get_running_loop()\n"
        "    events.append(('setup', loop))\n"
        "    yield loop\n"
        "    events.append(('teardown', asyncio.get_running_loop()))\n"
        "\n"
        "@pytest.mark.parametrize('value', [1, 2], ids=['one', 'two'])\n"
        "@pytest.mark.asyncio\n"
        "async def test_request(client, value):\n"
        "    assert client is asyncio.get_running_loop()\n"
        "    events.append(('test', value, client))\n"
        "\n"
        "def test_lifecycle():\n"
        "    assert [event[0] for event in events] == [\n"
        "        'setup', 'test', 'teardown', 'setup', 'test', 'teardown'\n"
        "    ]\n"
        "    assert events[0][1] is events[1][2] is events[2][1]\n"
        "    assert events[3][1] is events[4][2] is events[5][1]\n"
    )
    actual_project.write(
        "test_differential.py",
        source.format(fixture_module="vfy.asyncio"),
    )
    oracle_project.write(
        "test_differential.py",
        source.format(fixture_module="pytest_asyncio"),
    )
    isolated = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    actual_collection = actual_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "--collect-only",
        "-q",
        env=isolated,
    )
    oracle_collection = oracle_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "--collect-only",
        "-q",
        env=isolated,
    )
    assert actual_collection.returncode == oracle_collection.returncode == 0
    assert _collection_ids(actual_collection.stdout) == _collection_ids(oracle_collection.stdout)

    actual = actual_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        env=isolated,
    )
    oracle = oracle_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-q",
        env=isolated,
    )
    assert actual.returncode == oracle.returncode == 0


def test_async_collection_preserves_third_party_item_state(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n"
        "\n"
        "collected = {}\n"
        "\n"
        "class ThirdPartyFunction(pytest.Function):\n"
        "    pass\n"
        "\n"
        "@pytest.hookimpl(wrapper=True, trylast=True)\n"
        "def pytest_pycollect_makeitem(collector, name, obj):\n"
        "    nodes = yield\n"
        "    if not nodes:\n"
        "        return\n"
        "    for node in nodes if isinstance(nodes, list) else [nodes]:\n"
        "        if isinstance(node, pytest.Function):\n"
        "            node.__class__ = ThirdPartyFunction\n"
        "            node.third_party_state = object()\n"
        "            collected[node.nodeid] = (id(node), node.third_party_state)\n"
        "    return nodes\n"
        "\n"
        "def pytest_runtest_setup(item):\n"
        "    identity, state = collected[item.nodeid]\n"
        "    assert id(item) == identity\n"
        "    assert type(item) is ThirdPartyFunction\n"
        "    assert item.third_party_state is state\n",
    )
    project.write(
        "test_async.py",
        "import pytest\n\n@pytest.mark.asyncio\nasync def test_value():\n    assert True\n",
    )

    result = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    result.expect(0)


def test_skipped_async_test_does_not_create_a_runner(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import asyncio\n"
        "from pathlib import Path\n"
        "\n"
        "def make_loop():\n"
        "    Path('runner-created').touch()\n"
        "    return asyncio.new_event_loop()\n"
        "\n"
        "def pytest_asyncio_loop_factories(config, item):\n"
        "    return {'tracked': make_loop}\n",
    )
    project.write(
        "test_async.py",
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason='not needed')\n"
        "@pytest.mark.asyncio\n"
        "async def test_value():\n"
        "    assert True\n",
    )

    result = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    result.expect(0)
    assert not (tmp_path / "runner-created").exists()


def test_callable_replacement_is_resolved_at_execution(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "from pathlib import Path\n\n"
        "async def replacement():\n"
        "    Path('replacement-ran').touch()\n\n"
        "def pytest_collection_modifyitems(items):\n"
        "    items[0].obj = replacement\n",
    )
    project.write(
        "test_async.py",
        "import pytest\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_original():\n"
        "    raise AssertionError('stale callable executed')\n",
    )

    result = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    result.expect(0)
    assert (tmp_path / "replacement-ran").is_file()


@pytest.mark.parametrize("replace_code", [False, True], ids=["function", "code"])
def test_sync_callable_replacement_is_reclassified_at_execution(
    tmp_path: Path, replace_code: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "from pathlib import Path\n\n"
        "def replacement():\n"
        "    Path('replacement-ran').touch()\n\n"
        "def pytest_collection_modifyitems(items):\n"
        + (
            "    items[0].obj.__code__ = replacement.__code__\n"
            if replace_code
            else "    items[0].obj = replacement\n"
        ),
    )
    project.write(
        "test_async.py",
        "from pathlib import Path\n"
        "import pytest\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_original():\n"
        "    raise AssertionError('stale callable executed')\n",
    )

    result = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )

    result.expect(0)
    assert (tmp_path / "replacement-ran").is_file()
    assert "has an asyncio mark but its callable is synchronous" in (result.stdout + result.stderr)


def test_overrides_and_dynamic_fixture_requests_match_pinned_oracle(tmp_path: Path) -> None:
    source = (
        "import asyncio\n"
        "import {fixture_module}\n"
        "import pytest\n"
        "\n"
        "@{fixture_module}.fixture\n"
        "async def resource():\n"
        "    return ('base', asyncio.get_running_loop())\n"
        "\n"
        "@pytest.fixture\n"
        "def dynamic_resource(request):\n"
        "    return request.getfixturevalue('resource')\n"
        "\n"
        "class TestOverride:\n"
        "    @{fixture_module}.fixture\n"
        "    async def resource(self):\n"
        "        return ('override', asyncio.get_running_loop())\n"
        "\n"
        "    @pytest.mark.parametrize('expected', ['override'])\n"
        "    @pytest.mark.asyncio\n"
        "    async def test_dynamic_request(self, dynamic_resource, expected):\n"
        "        value, loop = dynamic_resource\n"
        "        assert value == expected\n"
        "        assert loop is asyncio.get_running_loop()\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_base_fixture(dynamic_resource):\n"
        "    value, loop = dynamic_resource\n"
        "    assert value == 'base'\n"
        "    assert loop is asyncio.get_running_loop()\n"
    )
    actual_project = VfyProject(tmp_path / "vfy")
    oracle_project = VfyProject(tmp_path / "upstream")
    actual_project.write(
        "test_overrides.py",
        source.format(fixture_module="vfy.asyncio"),
    )
    oracle_project.write(
        "test_overrides.py",
        source.format(fixture_module="pytest_asyncio"),
    )
    isolated = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    actual = actual_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env=isolated,
    )
    oracle = oracle_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env=isolated,
    )
    oracle.expect(0)
    actual.expect(0)
    assert "2 passed" in actual.stdout
    assert "2 passed" in oracle.stdout


def test_asyncio_configuration_rejects_invalid_scopes_before_collection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\nasyncio_mode = strict\nasyncio_default_test_loop_scope = invalid\n",
    )
    project.write("test_empty.py", "def test_ok():\n    pass\n")
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 4
    assert "'invalid' is not a valid asyncio_default_test_loop_scope" in result.stderr


def test_vfy_test_loads_asyncio_plugin_with_autoload_disabled(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_async.py",
        "import asyncio\n"
        "import vfy\n"
        "import pytest\n"
        "\n"
        "@vfy.async_fixture\n"
        "async def value():\n"
        "    await asyncio.sleep(0)\n"
        "    return 5\n"
        "\n"
        "async def classify(flag):\n"
        "    await asyncio.sleep(0)\n"
        "    if flag:\n"
        "        return 'yes'\n"
        "    return 'no'\n"
        "\n"
        "@pytest.mark.parametrize('flag', [False, True])\n"
        "@pytest.mark.asyncio\n"
        "async def test_value(value, flag):\n"
        "    assert value == 5\n"
        "    assert await classify(flag) == ('yes' if flag else 'no')\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "-q",
        "-n",
        "2",
        "--cov-branch",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    data = project.data()
    source = str(tmp_path / "test_async.py")
    assert data.lines(source)
    assert data.arcs(source)
    contexts = {
        context
        for line_contexts in data.contexts_by_lineno(source).values()
        for context in line_contexts
    }
    assert any("test_value[False]" in context for context in contexts)
    assert any("test_value[True]" in context for context in contexts)


def test_async_tests_share_vfy_replay_selection_and_failure_safety(tmp_path: Path) -> None:
    # Confirmation still uses pytest's retry workflow; replay, selection, and
    # interrupt preservation are covered by the native test controller.
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage\n.vfy/\n.pytest_cache/\n__pycache__/\n*.pyc\n")
    project.write("src/__init__.py", "")
    project.write(
        "src/service.py",
        "import asyncio\n\n"
        "async def classify(value):\n"
        "    await asyncio.sleep(0)\n"
        "    return 'positive' if value > 0 else 'other'\n",
    )
    project.write(
        "tests/test_service.py",
        "import vfy\n"
        "import pytest\n"
        "from src.service import classify\n\n"
        "@vfy.async_fixture\n"
        "async def value():\n"
        "    return 1\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_classify(value):\n"
        "    assert await classify(value) == 'positive'\n",
    )
    project.git_init()
    isolated = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

    baseline = project.run_vfy(
        "test",
        "--engine=pytest",
        "--all",
        "-j1",
        "--source",
        "src",
        "-q",
        env=isolated,
    )
    baseline.expect(0)
    replay_file = tmp_path / ".vfy/replay.coverage"
    original_baseline = replay_file.read_bytes()

    replay = project.run_vfy(
        "test", "--engine=pytest", "-j1", "--source", "src", "-q", env=isolated
    )
    replay.expect(0)
    assert "Reused" in replay.stderr

    project.write(
        "src/service.py",
        "import asyncio\n\n"
        "async def classify(value):\n"
        "    await asyncio.sleep(0)\n"
        "    return 'positive' if value >= 1 else 'other'\n",
    )
    selected = project.run_vfy(
        "test", "--engine=pytest", "-j1", "--source", "src", "-q", env=isolated
    )
    selected.expect(0)
    assert "1 passed" in selected.stdout

    project.write(
        "tests/test_service.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "from src.service import classify\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_classify():\n"
        "    attempt = Path('.async-attempt')\n"
        "    if not attempt.exists():\n"
        "        attempt.touch()\n"
        "        assert False\n"
        "    assert await classify(1) == 'positive'\n",
    )
    failed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "-j1",
        "--source",
        "src",
        "-q",
        env=isolated,
    )
    assert failed.returncode == 1
    assert "passed on confirmation" in failed.stderr
    assert replay_file.read_bytes() == original_baseline

    project.write(
        "tests/test_service.py",
        "import pytest\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_interrupt():\n"
        "    raise KeyboardInterrupt\n",
    )
    interrupted = project.run_vfy(
        "test",
        "--engine=pytest",
        "-j1",
        "--source",
        "src",
        "-q",
        env=isolated,
    )
    assert interrupted.returncode == 2
    assert replay_file.read_bytes() == original_baseline


@requires_native_pytest
@pytest.mark.parametrize("location", ["default", "command", "configuration", "environment"])
def test_vfy_test_owns_asyncio_from_default_and_explicit_aliases(
    tmp_path: Path, location: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_async.py",
        """import asyncio
import sys
from pathlib import Path
import pytest
import pytest_asyncio.plugin as adapter

@pytest.mark.asyncio
async def test_value(request):
    assert request.config.pluginmanager.getplugin('asyncio') is adapter
    assert '_pytest.runner' not in sys.modules
    await asyncio.sleep(0)
    Path('awaited').touch()
""",
    )
    arguments = ["-p", "pytest_asyncio.plugin"] if location == "command" else []
    if location == "configuration":
        project.write("pytest.ini", "[pytest]\naddopts = -p pytest_asyncio.plugin\n")
    environment = {"PYTEST_PLUGINS": "pytest_asyncio.plugin"} if location == "environment" else None
    result = project.run_vfy("test", "--no-cov", "-q", *arguments, env=environment)
    result.expect(0)
    assert (tmp_path / "awaited").is_file()


def test_direct_pytest_diagnoses_explicit_upstream_asyncio_plugin(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_empty.py", "def test_ok():\n    pass\n")
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "vfy.asyncio_plugin",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 4
    assert "'pytest_asyncio.plugin'" in result.stderr
    assert "remove the explicit pytest-asyncio plug-in load" in result.stderr
    assert "remove pytest-asyncio from the project's test dependencies" in result.stderr


def test_vfy_test_diagnoses_upstream_asyncio_from_pytest_plugins(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "pytest_plugins = ['pytest_asyncio.plugin']\n")
    project.write("test_empty.py", "def test_ok():\n    pass\n")
    result = project.run_vfy("test", "--engine=pytest", "--no-cov", "-q")
    assert result.returncode == 4
    assert "remove the explicit pytest-asyncio plug-in load" in result.stderr


@requires_native_pytest
def test_vfy_test_ignores_inactive_pytest_configuration(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\naddopts = -q\n")
    project.write("setup.cfg", "[tool:pytest]\naddopts = -p pytest_asyncio.plugin\n")
    project.write("test_empty.py", "def test_ok():\n    pass\n")
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    )
    result.expect(0)


def test_direct_pytest_diagnoses_dynamic_upstream_plugin_load(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "pytest_plugins = ['pytest_asyncio' + '.plugin']\n")
    project.write("test_empty.py", "def test_ok():\n    pass\n")
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "vfy.asyncio_plugin",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 4
    assert "remove the explicit pytest-asyncio plug-in load" in result.stderr
    assert "pytest_asyncio_loop_factories" not in result.stderr


def test_async_fixture_context_is_restored_after_teardown_failure(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_context.py",
        "import contextvars\n"
        "import vfy\n"
        "import pytest\n"
        "\n"
        "value = contextvars.ContextVar('value', default='outer')\n"
        "\n"
        "@vfy.async_fixture\n"
        "async def broken():\n"
        "    value.set('fixture')\n"
        "    yield\n"
        "    raise RuntimeError('intentional teardown error')\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_fixture(broken):\n"
        "    assert value.get() == 'fixture'\n"
        "\n"
        "def test_context_after_failure():\n"
        "    assert value.get() == 'outer'\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 1
    assert "2 passed" in result.stdout
    assert "1 error" in result.stdout
    assert "test_context_after_failure" not in result.stdout


def test_runner_restores_ambient_event_loop(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_loop.py",
        "import asyncio\n"
        "import pytest\n"
        "\n"
        "ambient = asyncio.new_event_loop()\n"
        "asyncio.set_event_loop(ambient)\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_async():\n"
        "    assert asyncio.get_running_loop() is not ambient\n"
        "\n"
        "def test_ambient_loop_after_async_test():\n"
        "    try:\n"
        "        assert asyncio.get_event_loop() is ambient\n"
        "    finally:\n"
        "        ambient.close()\n"
        "        asyncio.set_event_loop(None)\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)


def test_runner_does_not_create_an_ambient_event_loop(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_loop.py",
        "import asyncio\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_async():\n"
        "    pass\n"
        "\n"
        "def test_no_ambient_loop_after_async_test():\n"
        "    with pytest.raises(RuntimeError, match='There is no current event loop'):\n"
        "        asyncio.get_event_loop()\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)


def test_runner_cancels_abandoned_tasks_before_teardown(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cleanup.py",
        "import asyncio\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_abandon_task():\n"
        "    async def pending():\n"
        "        try:\n"
        "            await asyncio.Event().wait()\n"
        "        finally:\n"
        "            Path('cancelled').write_text('yes')\n"
        "\n"
        "    asyncio.create_task(pending())\n"
        "    await asyncio.sleep(0)\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    assert (tmp_path / "cancelled").read_text(encoding="utf-8") == "yes"


def test_malformed_async_generator_fixture_fails_during_teardown(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_malformed.py",
        "import vfy\n"
        "import pytest\n"
        "\n"
        "@vfy.async_fixture\n"
        "async def value():\n"
        "    yield 1\n"
        "    yield 2\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_value(value):\n"
        "    assert value == 1\n",
    )
    result = project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == 1
    assert "Async fixture yielded more than once" in result.stdout
    assert "1 passed, 1 error" in result.stdout


def test_repeated_pytest_sessions_restore_runner_state(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_repeated.py",
        "import asyncio\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "async def test_loop():\n"
        "    assert asyncio.get_running_loop().is_running()\n",
    )
    result = project.run_python(
        "-c",
        "import pytest\n"
        "args = ['-p', 'vfy.asyncio_plugin', '-q', '-o', "
        "'asyncio_default_fixture_loop_scope=function']\n"
        "assert pytest.main(args) == 0\n"
        "assert pytest.main(args) == 0\n",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)


def test_hypothesis_decorator_orders_match_pinned_oracle(tmp_path: Path) -> None:
    pytest.importorskip("hypothesis")
    source = (
        "import pytest\n"
        "from hypothesis import given, settings, strategies as st\n"
        "\n"
        "@settings(max_examples=5)\n"
        "@given(st.integers())\n"
        "@pytest.mark.asyncio\n"
        "async def test_inner_mark(value):\n"
        "    assert isinstance(value, int)\n"
        "\n"
        "@pytest.mark.asyncio\n"
        "@settings(max_examples=5)\n"
        "@given(st.integers())\n"
        "async def test_outer_mark(value):\n"
        "    assert isinstance(value, int)\n"
    )
    actual_project = VfyProject(tmp_path / "vfy")
    oracle_project = VfyProject(tmp_path / "upstream")
    actual_project.write("test_hypothesis.py", source)
    oracle_project.write("test_hypothesis.py", source)
    isolated = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    actual = actual_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "vfy.asyncio_plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env=isolated,
    )
    oracle = oracle_project.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env=isolated,
    )
    assert actual.returncode == oracle.returncode == 0
    assert "2 passed" in actual.stdout
    assert "2 passed" in oracle.stdout
