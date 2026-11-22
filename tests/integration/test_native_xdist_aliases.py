"""Compare explicit xdist activation and blocking at the public command boundary."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from textwrap import dedent
from typing import Literal

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")) or importlib.util.find_spec("xdist") is None,
    reason="native xdist execution requires Linux or macOS and pytest-xdist",
)

Route = Literal["upstream", "native"]

WORKER_TESTS = dedent("""\
    import json
    import os
    from pathlib import Path
    import pytest

    @pytest.mark.parametrize('case', range(8))
    def test_worker(case, worker_id):
        assert worker_id in {'gw0', 'gw1'}
        assert worker_id == os.environ['PYTEST_XDIST_WORKER']
        Path(f'case-{case}.json').write_text(json.dumps([case, worker_id]))
    """)


def _run(
    project: VfyProject,
    route: Route,
    *arguments: str,
    env: Mapping[str, str | None] | None = None,
) -> CommandResult:
    environment = dict(env or {})
    if route == "upstream":
        return project.run_pytest(*arguments, env=environment, load_xdist=False)
    return project.run_vfy(
        "test", "--engine=pytest-native", "--no-replay", "--no-cov", *arguments, env=environment
    )


@pytest.mark.parametrize("source", ["argument", "configuration", "environment", "conftest"])
def test_native_xdist_accepts_entrypoint_alias_and_root_module_declaration(
    tmp_path: Path, source: str
) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_workers.py", WORKER_TESTS)
        conftest = ""
        arguments = ("-p", "xdist", "--numprocesses=2", "-q")
        environment: dict[str, str | None] = {}
        if source == "configuration":
            project.write("pytest.ini", "[pytest]\naddopts = -p xdist -n2\n")
            arguments = ("-q",)
        elif source == "environment":
            environment["PYTEST_ADDOPTS"] = "-p xdist -n2"
            arguments = ("-q",)
        elif source == "conftest":
            conftest = "pytest_plugins = ['xdist.plugin']\n" + conftest
            arguments = ("--numprocesses=2", "-q")
        project.write("conftest.py", conftest)

        result = _run(project, route, *arguments, env=environment)

        result.expect(0)
        observed = [
            json.loads(path.read_text()) for path in sorted(project.root.glob("case-*.json"))
        ]
        assert [row[0] for row in observed] == list(range(8))
        assert {row[1] for row in observed} == {"gw0", "gw1"}


@pytest.mark.parametrize("workers", [None, "0", "2"])
def test_native_xdist_declared_plugin_accepts_report_hooks(
    tmp_path: Path, workers: str | None
) -> None:
    observations = []
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            "pytest_plugins = ['xdist.plugin']\n"
            "from pathlib import Path\n"
            "def pytest_runtest_logreport(report):\n"
            "    if report.when == 'call':\n"
            "        Path('reported').write_text(report.nodeid + ':' + report.outcome)\n",
        )
        project.write("test_body.py", "def test_body(): pass\n")
        arguments = ("-q",) if workers is None else ("-q", "--numprocesses", workers)

        result = _run(project, route, *arguments)

        result.expect(0)
        observations.append((project.root / "reported").read_text())
    assert observations == ["test_body.py::test_body:passed"] * 2


@pytest.mark.parametrize(
    "engine", [pytest.param("pytest-native", marks=requires_native_pytest), "pytest"]
)
@pytest.mark.parametrize("arguments", [("-n0",), ("-n=0",), ("-n", "0")])
def test_serial_worker_alias_overrides_inherited_xdist_workers(
    tmp_path: Path, engine: str, arguments: tuple[str, ...]
) -> None:
    observations = []
    for route in ("upstream", engine):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\naddopts = -n auto\n")
        project.write(
            "conftest.py",
            "import json\n"
            "from pathlib import Path\n"
            "def pytest_configure(config):\n"
            "    Path('configured.json').write_text(json.dumps([\n"
            "        config.option.numprocesses, config.option.dist,\n"
            "        hasattr(config, 'workerinput'),\n"
            "    ]))\n",
        )
        project.write(
            "test_serial.py",
            "import json\n"
            "from pathlib import Path\n"
            "def test_serial(pytestconfig, worker_id):\n"
            "    assert worker_id == 'master'\n"
            "    assert json.loads(Path('configured.json').read_text()) == [0, 'no', False]\n"
            "    assert pytestconfig.option.numprocesses == 0\n"
            "    assert pytestconfig.option.dist == 'no'\n",
        )
        if route == "upstream":
            result = project.run_pytest("-p", "xdist.plugin", *arguments, "-q", load_xdist=False)
        else:
            result = project.run_vfy(
                "test", f"--engine={engine}", "--no-replay", "--no-cov", *arguments, "-q"
            )
        result.expect(0)
        observations.append(json.loads((project.root / "configured.json").read_text()))
    assert observations == [[0, "no", False]] * 2


@pytest.mark.parametrize("forged_metadata", [False, True])
def test_native_xdist_rejects_project_shadow_before_running_hooks(
    tmp_path: Path, forged_metadata: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("xdist/__init__.py", "")
    project.write(
        "xdist/plugin.py",
        "from pathlib import Path\ndef pytest_configure(config): Path('shadow-hook-ran').touch()\n",
    )
    if forged_metadata:
        project.write(
            "pytest_xdist-3.8.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: pytest-xdist\nVersion: 3.8.0\n",
        )
    project.write("test_body.py", "def test_body(): pass\n")

    result = _run(project, "native", "-p", "xdist.plugin", "-q")

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "requires the installed upstream xdist plugin" in result.stderr
    assert not (project.root / "shadow-hook-ran").exists()


@pytest.mark.parametrize("blocked", ["xdist", "xdist.plugin"])
def test_native_xdist_honors_explicit_plugin_block_before_running_cases(
    tmp_path: Path, blocked: str
) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_body.py", "from pathlib import Path\ndef test_body(): Path('ran').touch()\n"
        )

        result = _run(project, route, "-p", f"no:{blocked}", "--numprocesses=2", "-q")

        assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
        assert "unrecognized arguments: --numprocesses=2" in result.stdout + result.stderr
        assert not (project.root / "ran").exists()


def test_native_xdist_rejects_combining_vfy_workers_with_auto_workers(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py", "from pathlib import Path\ndef test_body(): Path('ran').touch()\n"
    )

    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--no-replay",
        "-j2",
        "--no-cov",
        "--numprocesses=auto",
        "-q",
    )

    assert result.returncode != 0
    assert "cannot combine vfy's --workers/-j/-n with pytest-xdist" in result.stderr
    assert not (project.root / "ran").exists()


@pytest.mark.parametrize("workers", ["2", "auto"])
def test_native_xdist_declared_plugin_rejects_vfy_workers(tmp_path: Path, workers: str) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "pytest_plugins = ['xdist.plugin']\n")
    project.write(
        "test_body.py", "from pathlib import Path\ndef test_body(): Path('ran').touch()\n"
    )

    result = project.run_vfy(
        "test", "--engine=pytest-native", "--no-replay", "--workers", workers, "--no-cov", "-q"
    )

    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "cannot combine vfy's --workers/-j/-n with pytest-xdist" in result.stderr
    assert not (project.root / "ran").exists()


def test_native_xdist_retains_late_supported_report_hooks(tmp_path: Path) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\nnorecursedirs = support\n")
        project.write(
            "support/conftest.py",
            "from pathlib import Path\n"
            "def pytest_runtest_logreport(report):\n"
            "    if report.when == 'call':\n"
            "        with Path('late-reports').open('a') as stream:\n"
            "            stream.write(report.nodeid + '\\n')\n",
        )
        project.write(
            "test_body.py",
            "def test_register(request):\n"
            "    import support.conftest\n"
            "    request.config.pluginmanager.register(support.conftest, 'late-report-observer')\n"
            "def test_after_registration(): pass\n",
        )

        result = _run(project, route, "-p", "xdist.plugin", "--numprocesses=0", "-q")

        result.expect(0)
        assert (project.root / "late-reports").read_text().splitlines() == [
            "test_body.py::test_register",
            "test_body.py::test_after_registration",
        ]


@pytest.mark.parametrize("alias", ["xdist", "xdist.plugin"])
def test_native_xdist_rejects_late_forged_alias_after_native_dispatch(
    tmp_path: Path, alias: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        f"ALIAS = {alias!r}\n"
        + dedent("""\
            from pathlib import Path
            from types import ModuleType
            import pytest

            def test_first(): pass

            def test_forged_registration(request):
                root = Path.cwd()
                metadata = root / 'pytest_xdist-3.8.0.dist-info'
                metadata.mkdir()
                (metadata / 'METADATA').write_text(
                    'Metadata-Version: 2.1\\nName: pytest-xdist\\nVersion: 3.8.0\\n'
                )
                fake = ModuleType('xdist.plugin')
                fake.__file__ = str(root / 'xdist' / 'plugin.py')
                def pytest_configure(config):
                    Path('forged-hook-ran').touch()
                fake.pytest_configure = pytest_configure
                with pytest.raises(pytest.UsageError, match='installed upstream xdist plugin'):
                    request.config.pluginmanager.register(fake, ALIAS)

            def test_after_rejection(): pass
            """),
    )
    active_alias = "xdist.plugin" if alias == "xdist" else "xdist"

    result = _run(project, "native", "-p", active_alias, "--numprocesses=0", "-q")

    result.expect(0)
    assert "3 passed" in result.stdout
    assert not (project.root / "forged-hook-ran").exists()


def test_native_xdist_run_continues_after_xdist_plugin_is_unregistered(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_body.py",
        "def test_unregister(request):\n"
        "    request.config.pluginmanager.unregister(name='xdist.plugin')\n"
        "def test_after_unregister(): pass\n",
    )

    result = _run(project, "native", "-p", "xdist.plugin", "--numprocesses=0", "-q")

    result.expect(0)
    assert "2 passed" in result.stdout
