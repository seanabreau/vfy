"""Independent public-boundary proofs against pytest-asyncio 1.4.0."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


def test_skipped_parameter_does_not_add_a_runner_to_other_parameters(tmp_path: Path) -> None:
    results = []
    for plugin in ("pytest_asyncio.plugin", "vfy.asyncio_plugin"):
        project = VfyProject(tmp_path / plugin)
        project.write(
            "conftest.py",
            dedent("""\
                import asyncio
                import json
                from pathlib import Path
                import pytest

                loops = []

                class Policy(asyncio.DefaultEventLoopPolicy):
                    def new_event_loop(self):
                        loop = super().new_event_loop()
                        loops.append(loop)
                        return loop

                @pytest.fixture(scope='session')
                def event_loop_policy():
                    return Policy()

                def pytest_sessionfinish(session):
                    Path('loops.json').write_text(json.dumps(
                        [loop.is_closed() for loop in loops]
                    ))
                """),
        )
        project.write(
            "test_parameters.py",
            dedent("""\
                import asyncio
                import pytest

                @pytest.mark.parametrize('value', [
                    pytest.param(1, marks=pytest.mark.asyncio(loop_scope='function')),
                    pytest.param(2, marks=[
                        pytest.mark.asyncio(loop_scope='session'),
                        pytest.mark.skip(reason='unused scope'),
                    ]),
                ])
                async def test_parameter(value):
                    assert asyncio.get_running_loop().is_running()
                """),
        )
        result = project.run_python(
            "-m",
            "pytest",
            "-p",
            plugin,
            "-q",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": None},
        )
        result.expect(0)
        assert "1 passed, 1 skipped" in result.stdout
        results.append(json.loads((project.root / "loops.json").read_text()))
    assert results == [[True], [True]]


@pytest.mark.parametrize("scope", ["function", "class", "module", "package", "session"])
def test_fixture_scope_and_factory_lifecycle_matches_upstream(tmp_path: Path, scope: str) -> None:
    results = []
    for directory, plugin, decorator in (
        ("upstream", "pytest_asyncio.plugin", "from pytest_asyncio import fixture"),
        ("vfy", "vfy.asyncio_plugin", "from vfy import async_fixture as fixture"),
    ):
        project = VfyProject(tmp_path / directory)
        project.write("suite/__init__.py", "")
        project.write(
            "conftest.py",
            dedent("""\
                import asyncio
                import json
                from pathlib import Path

                import pytest

                events = []
                loops = []

                def factory(name):
                    loop = asyncio.new_event_loop()
                    loop.variant = name
                    loop.ordinal = len(loops)
                    loops.append(loop)
                    events.append(['create', loop.ordinal, name])
                    return loop

                def pytest_configure(config):
                    config.lifecycle_events = events

                def pytest_asyncio_loop_factories(config, item):
                    return {'first': lambda: factory('first'),
                            'second': lambda: factory('second')}

                def pytest_runtest_logreport(report):
                    events.append([report.nodeid, report.when, report.outcome])

                @pytest.hookimpl(wrapper=True)
                def pytest_sessionfinish(session):
                    yield
                    assert loops and all(loop.is_closed() for loop in loops)
                    Path('events.json').write_text(json.dumps(events))
                """),
        )
        project.write(
            "suite/test_scopes.py",
            decorator
            + "\n"
            + dedent(f"""\
                import asyncio
                import contextvars
                import pytest

                current = contextvars.ContextVar('current', default=None)

                class TestScope:
                    @fixture(scope={scope!r}, loop_scope={scope!r}, params=[1, 2])
                    async def resource(self, request):
                        loop = asyncio.get_running_loop()
                        events = request.config.lifecycle_events
                        events.append(['setup', loop.ordinal, request.param])
                        current.set(request.param)
                        yield loop, request.param
                        assert asyncio.get_running_loop() is loop
                        assert not loop.is_closed()
                        assert current.get() == request.param
                        events.append(['teardown', loop.ordinal, request.param])

                    @pytest.mark.asyncio(loop_scope={scope!r})
                    @pytest.mark.parametrize('value', [0, 1])
                    async def test_resource(self, resource, value):
                        loop, parameter = resource
                        assert loop is asyncio.get_running_loop()
                        assert loop.variant in ('first', 'second')
                        assert current.get() == parameter
                """),
        )
        result = project.run_python(
            "-m",
            "pytest",
            "-p",
            plugin,
            "-q",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": None},
        )
        result.expect(0)
        results.append(json.loads((project.root / "events.json").read_text()))
    assert results[0] == results[1]


def test_interrupt_finishes_async_fixtures_and_closes_loops_like_upstream(tmp_path: Path) -> None:
    for directory, plugin, decorator in (
        ("upstream", "pytest_asyncio.plugin", "from pytest_asyncio import fixture"),
        ("vfy", "vfy.asyncio_plugin", "from vfy import async_fixture as fixture"),
    ):
        project = VfyProject(tmp_path / directory)
        project.write(
            "conftest.py",
            dedent("""\
                import asyncio
                from pathlib import Path

                import pytest

                loops = []
                def make_loop():
                    loop = asyncio.new_event_loop()
                    loops.append(loop)
                    return loop

                def pytest_asyncio_loop_factories(config, item):
                    return {'custom': make_loop}

                @pytest.hookimpl(wrapper=True)
                def pytest_sessionfinish(session):
                    yield
                    assert loops and all(loop.is_closed() for loop in loops)
                    assert Path('finalized').is_file()
                    Path('closed').touch()
                """),
        )
        project.write(
            "test_interrupt.py",
            decorator
            + "\n"
            + dedent("""\
                import asyncio
                from pathlib import Path
                import pytest

                @fixture
                async def resource():
                    yield
                    await asyncio.sleep(0)
                    Path('finalized').touch()

                @pytest.mark.asyncio
                async def test_interrupt(resource):
                    raise KeyboardInterrupt
                """),
        )
        result = project.run_python(
            "-m",
            "pytest",
            "-p",
            plugin,
            "-q",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": None},
        )
        result.expect(2)
        assert (project.root / "closed").is_file()


def test_strict_mode_coexists_with_anyio_trio_like_upstream(tmp_path: Path) -> None:
    for directory, plugin, decorator in (
        ("upstream", "pytest_asyncio.plugin", "from pytest_asyncio import fixture"),
        ("vfy", "vfy.asyncio_plugin", "from vfy import async_fixture as fixture"),
    ):
        project = VfyProject(tmp_path / directory)
        project.write(
            "test_backends.py",
            decorator
            + "\n"
            + dedent("""\
                import asyncio
                import pytest
                import trio

                @pytest.fixture
                def anyio_backend():
                    return 'trio'

                @pytest.fixture
                async def trio_resource(anyio_backend):
                    await trio.sleep(0)
                    yield trio.lowlevel.current_task()
                    await trio.sleep(0)

                @fixture
                async def asyncio_resource():
                    await asyncio.sleep(0)
                    yield asyncio.get_running_loop()
                    await asyncio.sleep(0)

                @pytest.mark.anyio
                async def test_trio(trio_resource):
                    assert trio_resource is trio.lowlevel.current_task()

                @pytest.mark.asyncio
                async def test_asyncio(asyncio_resource):
                    assert asyncio_resource is asyncio.get_running_loop()
                """),
        )
        result = project.run_python(
            "-m",
            "pytest",
            "-p",
            plugin,
            "-p",
            "anyio.pytest_plugin",
            "-q",
            "--asyncio-mode=strict",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": None},
        )
        result.expect(0)
        assert "2 passed" in result.stdout


def test_runner_waits_for_default_executor_shutdown_like_upstream(tmp_path: Path) -> None:
    for directory, plugin in (
        ("upstream", "pytest_asyncio.plugin"),
        ("vfy", "vfy.asyncio_plugin"),
    ):
        project = VfyProject(tmp_path / directory)
        project.write(
            "conftest.py",
            dedent("""\
                from pathlib import Path
                import pytest

                @pytest.hookimpl(wrapper=True)
                def pytest_sessionfinish(session):
                    yield
                    assert Path('executor-finished').is_file()
                """),
        )
        project.write(
            "test_executor.py",
            dedent("""\
                import asyncio
                import time
                from pathlib import Path
                import pytest

                def finish_work():
                    time.sleep(0.05)
                    Path('executor-finished').touch()

                @pytest.mark.asyncio
                async def test_background_executor_work():
                    asyncio.get_running_loop().run_in_executor(None, finish_work)
                """),
        )
        result = project.run_python(
            "-m",
            "pytest",
            "-p",
            plugin,
            "-q",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": None},
        )
        result.expect(0)


@requires_native_pytest
def test_documented_async_fixture_has_identical_serial_and_xdist_coverage(tmp_path: Path) -> None:
    evidence = []
    for directory, workers in (("serial", "1"), ("xdist", "2")):
        project = VfyProject(tmp_path / directory)
        source = project.write(
            "test_request.py",
            dedent("""\
                import asyncio
                import vfy
                import pytest

                @vfy.async_fixture
                async def client():
                    await asyncio.sleep(0)
                    yield object()

                async def request(value):
                    await asyncio.sleep(0)
                    return 'yes' if value else 'no'

                @pytest.mark.parametrize('value', [False, True])
                @pytest.mark.asyncio
                async def test_request(client, value):
                    assert client is not None
                    assert await request(value) == ('yes' if value else 'no')
                """),
        )
        engine = ("--engine=pytest",) if workers == "2" else ()
        result = project.run_vfy(
            "test",
            *engine,
            "--all",
            "-j",
            workers,
            "--cov-branch",
            "--cov-context=test",
            "--cov-report=",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        data = project.data()
        contexts = tuple(
            sorted(
                (line, tuple(sorted(values)))
                for line, values in data.contexts_by_lineno(str(source)).items()
            )
        )
        evidence.append(
            (tuple(data.lines(str(source)) or ()), tuple(data.arcs(str(source)) or ()), contexts)
        )
    assert evidence[0] == evidence[1]


@pytest.mark.parametrize(
    "engine", ["pytest", pytest.param("pytest-native", marks=requires_native_pytest)]
)
def test_async_execution_does_not_import_the_comparison_oracle(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        dedent("""\
            import sys
            from importlib.abc import MetaPathFinder

            class RejectOracle(MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] == 'pytest_asyncio':
                        raise AssertionError('runtime imported the development oracle')

            assert not any(name.split('.')[0] == 'pytest_asyncio' for name in sys.modules)
            sys.meta_path.insert(0, RejectOracle())
            """),
    )
    project.write(
        "test_independent.py",
        dedent("""\
            import asyncio
            import contextvars
            import vfy
            import pytest

            state = contextvars.ContextVar('state', default='outside')
            finished = []

            @vfy.async_fixture
            async def resource():
                state.set('fixture')
                yield asyncio.get_running_loop()
                assert state.get() == 'fixture'
                finished.append(True)

            @pytest.mark.asyncio
            async def test_async(resource, unused_tcp_port_factory):
                assert resource is asyncio.get_running_loop()
                assert state.get() == 'fixture'
                assert unused_tcp_port_factory() != unused_tcp_port_factory()
                state.set('test')

            def test_cleanup():
                assert finished == [True]
                assert state.get() == 'outside'
            """),
    )
    result = project.run_vfy(
        "test",
        f"--engine={engine}",
        "--all",
        "-j1",
        "--no-cov",
        "-q",
        "-o",
        "asyncio_default_fixture_loop_scope=function",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    result.expect(0)
    assert "2 passed" in result.stdout


def test_closed_loop_cleanup_preserves_upstream_warning_category(tmp_path: Path) -> None:
    warnings_by_plugin = []
    for directory, plugin, decorator in (
        ("upstream", "pytest_asyncio.plugin", "from pytest_asyncio import fixture"),
        ("vfy", "vfy.asyncio_plugin", "from vfy import async_fixture as fixture"),
    ):
        project = VfyProject(tmp_path / directory)
        project.write(
            "conftest.py",
            dedent("""\
                import json
                from pathlib import Path

                categories = []

                def pytest_warning_recorded(warning_message):
                    categories.append(warning_message.category.__name__)

                def pytest_sessionfinish():
                    Path('warnings.json').write_text(json.dumps(categories))
                """),
        )
        project.write(
            "test_closed.py",
            decorator
            + "\n"
            + dedent("""\
                import asyncio

                @fixture
                async def loop():
                    return asyncio.get_running_loop()

                def test_closed_loop(loop):
                    loop.close()
                """),
        )
        result = project.run_pytest(
            "-p",
            plugin,
            "-q",
            "-o",
            "asyncio_default_fixture_loop_scope=function",
            load_xdist=False,
        )
        result.expect(0)
        warnings_by_plugin.append(json.loads((project.root / "warnings.json").read_text()))
    assert warnings_by_plugin == [["RuntimeWarning"], ["RuntimeWarning"]]
