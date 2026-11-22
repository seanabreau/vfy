"""Compare native worker temporary-directory policy with pytest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_native_parallel import _native_parallel_available
from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    not _native_parallel_available(),
    reason="native parallel tests require Linux or macOS and a supported pytest version",
)


@pytest.mark.parametrize(
    "policy", ["cli", "ini", "pyproject", "environment", "enabled", "disabled-basetemp"]
)
def test_native_workers_preserve_tmpdir_policy(tmp_path: Path, policy: str) -> None:
    disabled = policy != "enabled"
    supplied_basetemp = not disabled or policy == "disabled-basetemp"
    blocked = "-p no:tmpdir -p no:legacypath"
    arguments = blocked.split() if policy in {"cli", "disabled-basetemp"} else []
    environment = {"PYTEST_ADDOPTS": blocked} if policy == "environment" else {}
    outcomes = []
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        if policy == "ini":
            project.write("pytest.ini", f"[pytest]\naddopts = {blocked}\n")
        if policy == "pyproject":
            project.write(
                "pyproject.toml",
                "[tool.pytest.ini_options]\n"
                'addopts = ["--strict-config", "--capture=sys", "-p", "no:logging", '
                '"-p", "no:threadexception", "-p", "no:tmpdir", "-p", "no:legacypath"]\n',
            )
        project.write(
            "conftest.py",
            "import project_plugin\n"
            "def pytest_addoption(parser):\n"
            "    parser.addoption('--project-token')\n"
            "def pytest_configure(config):\n"
            "    config.pluginmanager.register(project_plugin, 'project_plugin')\n",
        )
        project.write(
            "project_plugin.py",
            "import pytest\n"
            "@pytest.fixture\n"
            "def project_token(request):\n"
            "    return request.config.getoption('--project-token')\n",
        )
        for name in ("alpha", "beta"):
            project.write(
                f"test_{name}.py",
                "import json\n"
                "import os\n"
                "from pathlib import Path\n"
                f"def test_policy(project_token, request{'' if disabled else ', tmp_path'}):\n"
                "    assert project_token == 'preserved'\n"
                "    assert ('VFY_NATIVE_PARALLEL_INDEX' in os.environ) "
                f"is {engine == 'pytest-native'}\n"
                f"    assert request.config.pluginmanager.hasplugin('tmpdir') is {not disabled}\n"
                + (
                    "    assert request.config.getoption('basetemp', default=None) "
                    f"== {'controlled-temp' if supplied_basetemp else None!r}\n"
                    if disabled
                    else "    (tmp_path / 'sentinel').write_text('alive')\n"
                )
                + f"    Path('{name}.json').write_text(json.dumps("
                + ("{'passed': True}" if disabled else "{'base': str(tmp_path.parent)}")
                + "))\n",
            )
        pytest_args = [*arguments, "--project-token=preserved", "-q"]
        if supplied_basetemp:
            pytest_args.append("--basetemp=controlled-temp")
        result = (
            project.run_pytest(*pytest_args, env=environment, load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                # This parameter asserts the installed-pytest worker directory names.
                *(("--engine=pytest-native",) if policy == "enabled" else ()),
                "--workers=2",
                "--no-replay",
                "--no-cov",
                *pytest_args,
                env={**environment, "VFY_WORKER_START": "exec"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout
        records = [
            json.loads((project.root / f"{name}.json").read_text()) for name in ("alpha", "beta")
        ]
        if disabled:
            outcomes.append(records)
        elif engine == "pytest-native":
            assert {Path(record["base"]).name for record in records} == {
                "native-worker-0",
                "native-worker-1",
            }
            for record in records:
                assert Path(record["base"]).parent == project.root / "controlled-temp"
                assert list(Path(record["base"]).glob("*/sentinel"))
    if disabled:
        assert outcomes[0] == outcomes[1] == [{"passed": True}, {"passed": True}]


def test_native_workers_preserve_disabled_tmpdir_parse_errors(tmp_path: Path) -> None:
    for engine in ("stock", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_example.py",
            "from pathlib import Path\ndef test_example():\n    Path('ran').touch()\n",
        )
        arguments = ("-p", "no:tmpdir", "-p", "no:legacypath", "--basetemp=.")
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                *arguments,
            )
        )
        assert result.returncode == 4, result.stdout + result.stderr
        assert "basetemp must not be empty, the current working directory" in result.stderr
        assert not (project.root / "ran").exists()
