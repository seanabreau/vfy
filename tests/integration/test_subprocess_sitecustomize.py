"""Application interpreter startup remains intact under subprocess coverage."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject


@pytest.mark.parametrize("symlink", [False, True])
def test_explicit_pytest_worker_coverage_replaces_startup_measurements(
    tmp_path: Path, symlink: bool
) -> None:
    project = VfyProject(tmp_path / "project")
    startup = tmp_path / "startup"
    startup.mkdir()
    hook = startup / "sitecustomize.py"
    source = tmp_path / "startup_hook.py" if symlink else hook
    source.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['APP_STARTUP_MARKERS'], str(os.getpid())).touch()\n"
        "startup_only = 11\n"
        "def invoked_later():\n"
        "    return 17\n",
        encoding="utf-8",
    )
    if symlink:
        try:
            hook.symlink_to(source)
        except OSError as error:
            pytest.skip(f"symlinks unavailable: {error}")
    project.write(".coveragerc", "[run]\npatch = subprocess\n")
    project.write(
        "test_body.py",
        "import sitecustomize\n"
        "def test_body():\n"
        "    assert sitecustomize.invoked_later() == 17\n"
        "def test_other():\n"
        "    assert sitecustomize.startup_only == 11\n",
    )
    project.git_init()
    observations = []
    for engine in ("reference", "vfy"):
        markers = tmp_path / f"markers-{engine}"
        markers.mkdir()
        environment = {"PYTHONPATH": str(startup), "APP_STARTUP_MARKERS": str(markers)}
        arguments = ("--cov-report=json:coverage.json", "-q")
        result = (
            project.run_python(
                "-m",
                "pytest",
                "-p",
                "pytest_cov.plugin",
                "-p",
                "xdist.plugin",
                "--cov",
                "-n2",
                *arguments,
                env=environment,
            )
            if engine == "reference"
            else project.run_vfy("test", "--engine=pytest", "-j2", *arguments, env=environment)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(list(markers.iterdir())) >= 3
        payload = json.loads((project.root / "coverage.json").read_text())
        observations.append(
            {
                str((project.root / filename).resolve()): report["executed_lines"]
                for filename, report in payload["files"].items()
            }
        )
    assert observations[0][str(source.resolve())] == [6]
    assert observations[1] == observations[0]


@pytest.mark.parametrize(
    "failure",
    [
        "",
        "RuntimeError('application startup failed')",
        "ModuleNotFoundError('application startup failed', name='application_dependency')",
    ],
)
def test_subprocess_preserves_application_sitecustomize(tmp_path: Path, failure: str) -> None:
    results: list[CommandResult] = []
    measured: list[dict[str, set[int]]] = []
    for kind in ("reference", "vfy"):
        project = VfyProject(tmp_path / kind)
        startup = project.write(
            "startup/sitecustomize.py",
            "import os\nimport site\n"
            "if os.environ.get('CHILD_STARTUP') == '1':\n"
            "    site.ENABLE_USER_SITE = True\n"
            "    TOKEN = 'application'\n"
            "    import sitecustomize\n"
            "    assert sitecustomize.TOKEN == TOKEN\n"
            "    from pkg import startup\n"
            "    startup.configure()\n"
            "    os.environ['APP_STARTUP_ORDER'] = 'site'\n"
            "    os.environ['APP_STARTUP_COUNT'] = str(\n"
            "        int(os.environ.get('APP_STARTUP_COUNT', '0')) + 1)\n"
            + (f"    raise {failure}\n" if failure else ""),
        ).parent
        project.write(
            "startup/usercustomize.py",
            "import os\n"
            "if os.environ.get('CHILD_STARTUP') == '1':\n"
            "    os.environ['APP_STARTUP_ORDER'] += ':user'\n",
        )
        project.write("pkg/__init__.py", "")
        project.write(
            "pkg/startup.py",
            "import os\ndef configure():\n    os.environ['APP_BOOTSTRAPPED'] = 'yes'\n",
        )
        project.write(
            "pkg/child.py",
            "import os\n"
            "assert os.environ.get('APP_BOOTSTRAPPED') == 'yes'\n"
            "assert os.environ.get('APP_STARTUP_ORDER') == 'site:user'\n"
            "assert os.environ.get('APP_STARTUP_COUNT') == '1'\n"
            + (
                "import sitecustomize\n"
                "assert sitecustomize.TOKEN == 'application'\n"
                "assert os.environ['APP_STARTUP_COUNT'] == '1'\n"
                if not failure
                else ""
            )
            + "print('application startup preserved')\n",
        )
        project.write(
            ".coveragerc",
            "[run]\npatch = subprocess\nsource = pkg\ndisable_warnings = no-data-collected\n",
        )
        project.write(
            "program.py",
            "import os\nimport subprocess\nimport sys\n"
            "for name in ('APP_BOOTSTRAPPED', 'APP_STARTUP_ORDER', 'APP_STARTUP_COUNT'):\n"
            "    os.environ.pop(name, None)\n"
            "os.environ['CHILD_STARTUP'] = '1'\n"
            "subprocess.run([sys.executable, '-m', 'pkg.child'], check=True)\n",
        )
        run = project.run_coverage_py if kind == "reference" else project.run_vfy
        arguments = (
            ("run", "program.py")
            if kind == "reference"
            else (
                "coverage",
                "run",
                "program.py",
            )
        )
        result = run(
            *arguments,
            env={"PYTHONPATH": os.pathsep.join((str(startup), str(project.root)))},
        )
        assert result.returncode == 0, (kind, result.stdout, result.stderr)
        assert result.stdout == "application startup preserved\n"
        if failure:
            assert f"{failure.split('(')[0]}: application startup failed" in result.stderr
        else:
            assert result.stderr == ""
        results.append(result)
        lines: dict[str, set[int]] = {"pkg/child.py": set(), "pkg/startup.py": set()}
        for shard in project.root.glob(".coverage.*"):
            data = project.data(shard.name)
            try:
                for filename, measured_lines in lines.items():
                    measured_lines.update(data.lines(str(project.root / filename)) or [])
            finally:
                data.close(force=True)
        assert lines["pkg/child.py"], kind
        assert lines["pkg/startup.py"] == {1, 2, 3}, (kind, lines)
        measured.append(lines)
    assert results[1] == results[0]
    assert measured[1] == measured[0]


def test_subprocess_sitecustomize_system_exit_prevents_program_execution(tmp_path: Path) -> None:
    for kind in ("reference", "vfy"):
        project = VfyProject(tmp_path / kind)
        startup = project.write(
            "startup/sitecustomize.py",
            "import os\n"
            "if os.environ.get('CHILD_STARTUP') == '1':\n"
            "    raise SystemExit('application startup stopped')\n",
        ).parent
        project.write("pkg/__init__.py", "")
        project.write("pkg/child.py", "from pathlib import Path\nPath('child-ran').touch()\n")
        project.write(
            ".coveragerc",
            "[run]\npatch = subprocess\nsource = pkg\ndisable_warnings = no-data-collected\n",
        )
        project.write(
            "program.py",
            "import os\nimport subprocess\nimport sys\n"
            "os.environ['CHILD_STARTUP'] = '1'\n"
            "sys.exit(subprocess.call([sys.executable, '-m', 'pkg.child']))\n",
        )
        env = {"PYTHONPATH": str(startup)}
        result = (
            project.run_coverage_py("run", "program.py", env=env)
            if kind == "reference"
            else project.run_vfy("coverage", "run", "program.py", env=env)
        )
        assert result.returncode == 1, (kind, result.stdout, result.stderr)
        assert result.stdout == ""
        assert "Fatal Python error: init_import_site" in result.stderr
        assert "SystemExit: application startup stopped" in result.stderr
        assert not (project.root / "child-ran").exists()


@pytest.mark.skipif(os.name == "nt", reason="requires an unresolvable POSIX symlink")
def test_subprocess_sitecustomize_ignores_unresolvable_search_entry(tmp_path: Path) -> None:
    for kind in ("reference", "vfy"):
        project = VfyProject(tmp_path / kind)
        startup = project.write(
            "startup/sitecustomize.py",
            "import os\nif os.environ.get('CHILD_STARTUP') == '1':\n    TOKEN = 'application'\n",
        ).parent
        loop = project.root / "loop"
        loop.symlink_to("loop")
        project.write("pkg/__init__.py", "")
        project.write(
            "pkg/child.py",
            "import sitecustomize\nprint(sitecustomize.TOKEN)\n",
        )
        project.write(
            ".coveragerc",
            "[run]\npatch = subprocess\nsource = pkg\ndisable_warnings = no-data-collected\n",
        )
        project.write(
            "program.py",
            "import os\nimport subprocess\nimport sys\n"
            "os.environ['CHILD_STARTUP'] = '1'\n"
            "subprocess.run([sys.executable, '-m', 'pkg.child'], check=True)\n",
        )
        env = {"PYTHONPATH": os.pathsep.join((str(loop), str(startup), str(project.root)))}
        result = (
            project.run_coverage_py("run", "program.py", env=env)
            if kind == "reference"
            else project.run_vfy("coverage", "run", "program.py", env=env)
        )
        assert result == CommandResult(0, "application\n", ""), (kind, result)


def test_subprocess_without_application_sitecustomize_keeps_module_absent(tmp_path: Path) -> None:
    for kind in ("reference", "vfy"):
        project = VfyProject(tmp_path / kind)
        project.write("pkg/__init__.py", "")
        if kind == "reference":
            probe = project.run_python(
                "-c",
                "import importlib.util; "
                "print(importlib.util.find_spec('sitecustomize') is not None)",
            )
            probe.expect(0)
            if probe.stdout.strip().splitlines()[-1] == "True":
                pytest.skip("interpreter supplies a startup hook")
        project.write(
            "pkg/child.py",
            "import importlib\nimport sys\n"
            "assert 'sitecustomize' not in sys.modules\n"
            "try:\n"
            "    importlib.import_module('sitecustomize')\n"
            "except ModuleNotFoundError as error:\n"
            "    assert error.name == 'sitecustomize'\n"
            "else:\n"
            "    raise AssertionError('unexpected startup module')\n"
            "print('no application hook')\n",
        )
        project.write(
            ".coveragerc",
            "[run]\npatch = subprocess\nsource = pkg\ndisable_warnings = no-data-collected\n",
        )
        project.write(
            "program.py",
            "import subprocess\nimport sys\n"
            "subprocess.run([sys.executable, '-m', 'pkg.child'], check=True)\n",
        )
        result = (
            project.run_coverage_py("run", "program.py")
            if kind == "reference"
            else project.run_vfy("coverage", "run", "program.py")
        )
        assert result == CommandResult(0, "no application hook\n", ""), (kind, result)
