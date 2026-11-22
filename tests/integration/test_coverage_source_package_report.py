"""Package-source reports follow the source actually imported by pytest."""

from __future__ import annotations

import json
import os
import sysconfig
import venv
import zipfile
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_AUDIT = """import importlib.util
import json
import os
from pathlib import Path
import pytest
import scope_pkg

@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    plugin = session.config.pluginmanager.getplugin('_cov')
    coverage = getattr(plugin, 'coverage', None)
    if coverage is None:
        coverage = plugin.cov_controller.cov
    else:
        assert plugin._finished
    # Both real coverage runtest-loop wrappers have finished before this hook.
    measured = sorted(coverage.get_data().measured_files())
    Path('source-audit.json').write_text(json.dumps({
        'cwd': os.getcwd(),
        'origin': scope_pkg.__file__,
        'spec_origin': importlib.util.find_spec('scope_pkg').origin,
        'measured_files': measured,
        'existing_files': {path: Path(path).is_file() for path in measured},
        'source': coverage.get_option('run:source'),
        'relative_files': coverage.get_option('run:relative_files'),
        'exitstatus': int(exitstatus),
    }, sort_keys=True))
"""


def _installed_package_wheel(directory: Path) -> Path:
    """Build a local semantic fixture wheel, without building or changing vfy."""
    wheel = directory / "scope_pkg-0.0.1-py3-none-any.whl"
    members = {
        "scope_pkg/__init__.py": "def value():\n    return 99\n",
        "scope_pkg-0.0.1.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: scope-pkg\nVersion: 0.0.1\n"
        ),
        "scope_pkg-0.0.1.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    members["scope_pkg-0.0.1.dist-info/RECORD"] = "".join(
        f"{name},,\n" for name in [*members, "scope_pkg-0.0.1.dist-info/RECORD"]
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return wheel


def _project(directory: Path, wheel: Path, source_path: str) -> VfyProject:
    directory.mkdir()
    environment = directory / "environment"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    project = VfyProject(directory, interpreter=interpreter)
    purelib = project.run_python("-c", "import sysconfig; print(sysconfig.get_path('purelib'))")
    purelib.expect(0)
    # Process editable .pth files as well as forwarding the paired installation.
    (Path(purelib.stdout.strip()) / "parent.pth").write_text(
        f"import site; site.addsitedir({sysconfig.get_path('purelib')!r})\n"
    )
    installed = project.run(
        "uv",
        "pip",
        "install",
        "--no-cache",
        "--link-mode=copy",
        "--python",
        str(interpreter),
        str(wheel),
    )
    installed.expect(0)
    duplicate = project.run_python(
        "-c", "import scope_pkg; print(scope_pkg.value()); print(scope_pkg.__file__)"
    )
    duplicate.expect(0)
    assert duplicate.stdout.splitlines() == [
        "99",
        str(Path(purelib.stdout.strip()) / "scope_pkg/__init__.py"),
    ]
    configured = 'pythonpath = ["src"]\n' if source_path == "configuration" else ""
    project.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
        + configured
        + '\n[tool.coverage.run]\nrelative_files = true\nsource = ["scope_pkg"]\n'
        + '\n[tool.coverage.paths]\nsource = ["src/scope_pkg", '
        + '".tox/*/lib/python*/site-packages/scope_pkg", '
        + '".tox/pypy*/site-packages/scope_pkg"]\n'
        + "\n[tool.coverage.json]\nshow_contexts = true\n",
    )
    project.write("src/scope_pkg/__init__.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_source.py",
        "from scope_pkg import value\n\ndef test_value():\n    assert value() == 1\n",
    )
    project.write("conftest.py", _AUDIT)
    return project


@requires_native_pytest
@pytest.mark.parametrize("source_path", ["environment", "configuration"])
def test_source_package_report_uses_imported_source_with_installed_duplicate(
    tmp_path: Path, source_path: str
) -> None:
    wheel = _installed_package_wheel(tmp_path)
    reports = {}
    for engine in ("stock", "native"):
        directory = tmp_path / engine
        project = _project(directory, wheel, source_path)
        options = [
            "-q",
            "--cov=scope_pkg",
            "--cov-context=test",
            "--cov-report=json:coverage.json",
            "tests",
        ]
        environment = {
            "PYTHONPATH": str(directory / "src") if source_path == "environment" else None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        result = (
            project.run_pytest(
                "-p", "pytest_cov.plugin", *options, env=environment, load_xdist=False
            )
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--workers=1",
                "--all",
                *options,
                env=environment,
            )
        )
        result.expect(0)
        audit = json.loads((directory / "source-audit.json").read_text())
        assert Path(audit["origin"]).resolve() == directory / "src/scope_pkg/__init__.py"
        assert audit["spec_origin"] == audit["origin"]
        assert Path(audit["cwd"]) == directory
        assert audit["relative_files"] is True
        assert audit["source"] == ["scope_pkg"]
        assert "src/scope_pkg/__init__.py" in audit["measured_files"]
        assert all(audit["existing_files"].values())
        report_path = directory / "coverage.json"
        assert report_path.is_file(), (audit, result.stdout, result.stderr)
        reports[engine] = json.loads(report_path.read_text())["files"]
    assert reports["native"] == reports["stock"]
