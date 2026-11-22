"""Independent configuration diagnostics preserve pytest's ordering and precedence."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_STRICT_OBSERVATION = """import json
from pathlib import Path

def pytest_sessionstart(session):
    config = session.config
    raw = [config.getoption('strict_config'), config.getoption('strict_markers')]
    Path('strict-observed.json').write_text(json.dumps({
        'raw_options': raw,
        'flags': [bool(value) for value in raw],
        'ini': [config.getini('strict'), config.getini('strict_config'),
                config.getini('strict_markers')],
    }))
"""


def _runs(
    project: VfyProject,
    arguments: list[str],
    *,
    env: Mapping[str, str | None] | None = None,
    native_env: Mapping[str, str | None] | None = None,
) -> Iterator[tuple[str, CommandResult]]:
    yield "pytest", project.run_pytest("-q", *arguments, env=env, load_xdist=False)
    yield (
        "vfy",
        project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "--native-result-json=results.json",
            *arguments,
            env={**(env or {}), **(native_env or {})},
        ),
    )


def test_strict_observations_report_raw_flags_while_ini_enforces_unknown_marks(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", "[tool.pytest]\nstrict = true\n")
    project.write("conftest.py", _STRICT_OBSERVATION)
    project.write(
        "test_config.py",
        "import pytest\n@pytest.mark.unregistered\ndef test_body(): pass\n",
    )
    for engine, result in _runs(project, []):
        assert result.returncode == 2, result.stdout + result.stderr
        assert "'unregistered' not found in `markers`" in result.stdout + result.stderr
        assert json.loads((tmp_path / "strict-observed.json").read_text()) == {
            "raw_options": [None, None],
            "flags": [False, False],
            "ini": [True, None, None],
        }
        if engine == "vfy":
            observed = json.loads((tmp_path / "results.json").read_text())["configuration"]
            assert observed["strict_config"] is False
            assert observed["strict_markers"] is False


@pytest.mark.parametrize(
    ("settings", "arguments", "addopts", "raw", "flags"),
    [
        (
            "strict = true\nstrict_config = false\nstrict_markers = false\n",
            [],
            None,
            [True, False, False],
            [False, False],
        ),
        (
            "",
            ["--strict-markers", "-o", "strict_markers=false"],
            None,
            [False, None, False],
            [False, True],
        ),
        (
            "",
            ["-o", "strict_markers=false", "--strict-markers"],
            None,
            [False, None, True],
            [False, True],
        ),
        (
            "addopts = --strict-markers\n",
            ["-o", "strict_markers=false"],
            "--strict",
            [True, None, False],
            [False, True],
        ),
        (
            "addopts = -o strict_markers=false\n",
            ["--strict-markers"],
            None,
            [False, None, True],
            [False, True],
        ),
    ],
)
def test_strict_flags_and_ini_overrides_preserve_argument_source_order(
    tmp_path: Path,
    settings: str,
    arguments: list[str],
    addopts: str | None,
    raw: list[bool | None],
    flags: list[bool],
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\n{settings}")
    project.write(
        "test_config.py",
        """import json
from pathlib import Path
def test_settings(request):
    config = request.config
    Path('observed.json').write_text(json.dumps({
        'ini': [config.getini('strict'), config.getini('strict_config'),
                config.getini('strict_markers')],
        'flags': [bool(config.getoption('strict_config')),
                  bool(config.getoption('strict_markers'))],
    }))
""",
    )
    for engine, result in _runs(project, arguments, env={"PYTEST_ADDOPTS": addopts}):
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((tmp_path / "observed.json").read_text()) == {
            "ini": raw,
            "flags": flags,
        }
        (tmp_path / "observed.json").unlink()
        if engine == "vfy":
            configuration = json.loads((tmp_path / "results.json").read_text())["configuration"]
            assert [configuration["strict_config"], configuration["strict_markers"]] == flags


@pytest.mark.parametrize(
    ("settings", "late_declaration", "status", "diagnostic"),
    [
        ("", False, 0, True),
        ("strict_config = true\n", False, 4, True),
        ("strict = true\nstrict_config = false\n", False, 0, True),
        ("strict_config = true\n", True, 0, False),
    ],
)
def test_unknown_ini_validation_follows_late_declarations_and_collection_finish(
    tmp_path: Path, settings: str, late_declaration: bool, status: int, diagnostic: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\ncustom_key = value\n{settings}")
    project.write(
        "conftest.py",
        """from pathlib import Path
def pytest_collection_finish(session):
    Path('finish_seen').write_text(str(len(session.items)))
"""
        + (
            "\ndef pytest_addoption(parser):\n    parser.addini('custom_key', 'late declaration')\n"
            if late_declaration
            else ""
        ),
    )
    project.write(
        "test_config.py",
        """from pathlib import Path
Path('imported').touch()
def test_body(): Path('body').touch()
""",
    )
    for _, result in _runs(project, []):
        assert result.returncode == status, result.stdout + result.stderr
        assert ("Unknown config option: custom_key" in result.stdout + result.stderr) is diagnostic
        assert (tmp_path / "imported").exists()
        assert (tmp_path / "finish_seen").read_text() == "1"
        assert (tmp_path / "body").exists() is (status == 0)
        for name in ("imported", "finish_seen", "body"):
            (tmp_path / name).unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("name", "arguments", "registered", "status", "diagnostic"),
    [
        ("custom", [], False, 0, "Unknown pytest.mark.custom"),
        ("custom", ["--strict-markers"], False, 2, "'custom' not found in `markers`"),
        ("custom", ["--strict-markers"], True, 0, ""),
        ("parameterize", [], False, 2, "did you mean 'parametrize'?"),
    ],
)
def test_marker_diagnostics_use_current_registered_names(
    tmp_path: Path, name: str, arguments: list[str], registered: bool, status: int, diagnostic: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    if registered:
        project.write(
            "conftest.py",
            "def pytest_configure(config):\n"
            "    config.addinivalue_line('markers', 'custom(reason): late registration')\n",
        )
    project.write(
        "test_config.py",
        f"""import pytest
from pathlib import Path
Path('imported').touch()
@pytest.mark.{name}
def test_body(): Path('body').touch()
""",
    )
    for _, result in _runs(project, arguments):
        assert result.returncode == status, result.stdout + result.stderr
        if diagnostic:
            assert diagnostic in result.stdout + result.stderr
        else:
            assert "Unknown pytest.mark.custom" not in result.stdout + result.stderr
        assert (tmp_path / "imported").exists()
        assert (tmp_path / "body").exists() is (status == 0)
        for filename in ("imported", "body"):
            (tmp_path / filename).unlink(missing_ok=True)


def _plugin(project: VfyProject) -> None:
    project.write(
        "vfy_demo_plugin.py",
        """from pathlib import Path
Path('plugin_imported').touch()
def pytest_addoption(parser):
    parser.addoption('--custom-count', type=int, default=1)
""",
    )
    project.write(
        "vfy_demo_plugin-1.5rc1.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: vfy-demo-plugin\nVersion: 1.5rc1\n",
    )
    project.write(
        "vfy_demo_plugin-1.5rc1.dist-info/entry_points.txt",
        "[pytest11]\nvfy_demo_plugin = vfy_demo_plugin\n",
    )


@pytest.mark.parametrize(
    ("arguments", "addopts", "expected"),
    [([], None, 2), ([], "--custom-count=3", 3), (["--custom-count", "4"], "--custom-count=3", 4)],
)
def test_plugin_options_parse_after_registration_with_rust_resolved_precedence(
    tmp_path: Path, arguments: list[str], addopts: str | None, expected: int
) -> None:
    project = VfyProject(tmp_path)
    _plugin(project)
    project.write("pytest.ini", "[pytest]\naddopts = -p vfy_demo_plugin --custom-count=2\n")
    project.write(
        "test_config.py",
        """from pathlib import Path
def test_option(request):
    Path('observed').write_text(str(request.config.getoption('custom_count')))
""",
    )
    for _, result in _runs(project, arguments, env={"PYTEST_ADDOPTS": addopts}):
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "observed").read_text() == str(expected)
        (tmp_path / "observed").unlink()


@pytest.mark.parametrize(
    "arguments", [["--unknown-option"], ["-k"], ["-p", "vfy_demo_plugin", "--custom-count=invalid"]]
)
def test_option_parse_errors_return_usage_status_before_test_import(
    tmp_path: Path, arguments: list[str]
) -> None:
    project = VfyProject(tmp_path)
    _plugin(project)
    project.write(
        "test_config.py",
        "from pathlib import Path\nPath('imported').touch()\ndef test_body(): pass\n",
    )
    for _, result in _runs(project, arguments):
        assert result.returncode == 4, result.stdout + result.stderr
        assert "error" in (result.stdout + result.stderr).lower()
        assert not (tmp_path / "imported").exists()


@pytest.mark.parametrize(
    ("required", "minimum", "arguments", "status", "plugin_imported", "diagnostic"),
    [
        (["vfy-demo-plugin>=1.4"], "9.0", ["-p", "vfy_demo_plugin"], 0, True, ""),
        (
            ["vfy-demo-plugin[extra]>=1.4; python_version < '0'"],
            "6.2",
            ["-p", "vfy_demo_plugin"],
            0,
            True,
            "",
        ),
        (
            ["vfy-demo-plugin>=2"],
            "",
            ["-p", "vfy_demo_plugin"],
            4,
            True,
            "Missing required plugins: vfy-demo-plugin>=2",
        ),
        (
            ["vfy-demo-plugin @ arbitrary-url"],
            "",
            ["-p", "vfy_demo_plugin"],
            0,
            True,
            "",
        ),
        (
            ["Vfy-demo-plugin"],
            "",
            ["-p", "vfy_demo_plugin"],
            4,
            True,
            "Missing required plugins: Vfy-demo-plugin",
        ),
        (
            ["not a requirement"],
            "",
            ["-p", "vfy_demo_plugin"],
            4,
            True,
            "Missing required plugins: not a requirement",
        ),
        (["vfy-demo-plugin"], "", [], 4, False, "Missing required plugins: vfy-demo-plugin"),
        ([], "99.0", [], 4, False, "'minversion' requires pytest-99.0"),
    ],
)
def test_version_requirements_use_loaded_distributions_without_packaging_imports(
    tmp_path: Path,
    required: list[str],
    minimum: str,
    arguments: list[str],
    status: int,
    plugin_imported: bool,
    diagnostic: str,
) -> None:
    project = VfyProject(tmp_path)
    _plugin(project)
    project.write(
        "pytest.toml",
        f"[pytest]\nrequired_plugins = {json.dumps(required)}\n"
        f"minversion = {json.dumps(minimum)}\n",
    )
    project.write("conftest.py", "from pathlib import Path\nPath('conftest_imported').touch()\n")
    project.write(
        "test_config.py", "from pathlib import Path\ndef test_body(): Path('body').touch()\n"
    )
    project.write(
        "sitecustomize.py",
        """import os
import sys
if os.environ.get('VFY_BLOCK_PACKAGING'):
    class NoPackaging:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'packaging' or fullname.startswith('packaging.'):
                raise RuntimeError('independent configuration imported Packaging')
    sys.meta_path.insert(0, NoPackaging())
""",
    )
    for _, result in _runs(
        project,
        arguments,
        env={"PYTHONPATH": str(tmp_path)},
        native_env={"VFY_BLOCK_PACKAGING": "1"},
    ):
        assert result.returncode == status, result.stdout + result.stderr
        if diagnostic:
            assert diagnostic in result.stdout + result.stderr
        assert (tmp_path / "plugin_imported").exists() is plugin_imported
        assert (tmp_path / "conftest_imported").exists() is (status == 0)
        assert (tmp_path / "body").exists() is (status == 0)
        for name in ("plugin_imported", "conftest_imported", "body"):
            (tmp_path / name).unlink(missing_ok=True)


@pytest.mark.parametrize("required", [False, True])
def test_required_plugins_only_reads_distributions_when_requested(
    tmp_path: Path, required: bool
) -> None:
    project = VfyProject(tmp_path)
    requirement = "required_plugins = vfy-missing-distribution\n" if required else ""
    project.write("pytest.ini", "[pytest]\n" + requirement)
    project.write("test_body.py", "def test_body(): pass\n")
    project.write(
        "probeplugin.py",
        "from pathlib import Path\n"
        "def pytest_addoption(parser, pluginmanager):\n"
        "    def observe():\n"
        "        with Path('distribution-calls').open('a') as calls:\n"
        "            calls.write('called\\n')\n"
        "        return []\n"
        "    pluginmanager.list_plugin_distinfo = observe\n",
    )
    observations: list[tuple[int, list[str], str]] = []
    for run in (
        lambda: project.run_python("-m", "pytest", "-q", "-p", "probeplugin"),
        lambda: project.run_vfy("test", "--engine=native", "--no-cov", "-q", "-p", "probeplugin"),
    ):
        result = run()
        marker = tmp_path / "distribution-calls"
        calls = marker.read_text().splitlines() if marker.exists() else []
        marker.unlink(missing_ok=True)
        observations.append((result.returncode, calls, result.stdout + result.stderr))

    expected_status = 4 if required else 0
    expected_calls = ["called"] if required else []
    for status, calls, diagnostic in observations:
        assert status == expected_status, diagnostic
        assert calls == expected_calls, diagnostic
        if required:
            assert "Missing required plugins: vfy-missing-distribution" in diagnostic
