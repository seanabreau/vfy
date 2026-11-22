"""Configured import paths select project code and preserve pytest cleanup semantics."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()
_BLOCKER = """import json
import os
import sys
from pathlib import Path
if os.environ.get('VFY_BLOCK_UPSTREAM'):
    Path('upstream-attempts.jsonl').touch()
    class NoUpstream:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.')[0] in {'_pytest', 'pluggy', 'packaging', 'coverage'}:
                with Path('upstream-attempts.jsonl').open('a') as output:
                    output.write(json.dumps(fullname) + '\\n')
                raise RuntimeError('independent bootstrap imported upstream: ' + fullname)
    sys.meta_path.insert(0, NoUpstream())
"""


@pytest.mark.parametrize(
    ("mode", "activation", "override"),
    [("prepend", "explicit", False), ("append", "autoload", True), ("importlib", "explicit", True)],
)
def test_configured_paths_precede_ambient_code_and_load_plugins_before_conftests(
    tmp_path: Path, mode: str, activation: str, override: bool
) -> None:
    project = VfyProject(tmp_path / "invoke")
    project.write("sitecustomize.py", _BLOCKER)
    settings = "ignored" if override else "'preferred source' secondary"
    project.write("../pytest.ini", f"[pytest]\npythonpath = {settings}\n")
    preferred = tmp_path / "preferred source"
    secondary = tmp_path / "secondary"
    installed = tmp_path / "installed"
    project.write("../secondary/selected_application.py", "VALUE = 999\n")
    project.write("../installed/selected_application.py", "VALUE = 42\n")
    plugin = """import json
from pathlib import Path
Path('plugin-origin.json').write_text(json.dumps(__file__))
def pytest_configure(config):
    config.configured_plugin_origin = __file__
"""
    project.write("../preferred source/configured_plugin.py", plugin)
    project.write("../installed/configured_plugin.py", plugin)
    project.write(
        "../preferred source/configured_plugin-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: configured-plugin\nVersion: 1.0\n",
    )
    project.write(
        "../preferred source/configured_plugin-1.0.dist-info/entry_points.txt",
        "[pytest11]\nconfigured_plugin = configured_plugin\n",
    )
    project.write(
        "conftest.py",
        "import sys\nassert 'configured_plugin' in sys.modules, 'plugin loaded after conftest'\n",
    )
    project.write(
        "tests/test_selected.py",
        """import json
from pathlib import Path
import selected_application

def test_configured_application(request):
    paths = request.config.getini('pythonpath')
    assert all(isinstance(path, Path) for path in paths)
    Path('selected.json').write_text(json.dumps({
        'module': selected_application.__file__,
        'value': selected_application.VALUE,
        'plugin': request.config.configured_plugin_origin,
        'paths': [str(path) for path in paths],
        'path_types': [[type(path).__module__, type(path).__name__] for path in paths],
    }))
    assert selected_application.VALUE == 42
""",
    )
    arguments = [
        "-p",
        "pytest_cov",
        "-c",
        "../pytest.ini",
        f"--import-mode={mode}",
        "tests/test_selected.py",
    ]
    if override:
        arguments.extend(["-o", "pythonpath='preferred source' secondary"])
    if activation == "explicit":
        arguments.extend(["-p", "configured_plugin"])
    else:
        for name in sorted(
            {
                entry.name
                for entry in importlib.metadata.entry_points(group="pytest11")
                if entry.name != "pytest_cov"
            }
        ):
            arguments.extend(["-p", f"no:{name}"])
    environment = {
        "PYTHONPATH": os.pathsep.join([_SOURCE, str(installed), str(project.root)]),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": None if activation == "autoload" else "1",
    }
    for value, status in [(0, 1), (42, 0)]:
        project.write("../preferred source/selected_application.py", f"VALUE = {value}\n")
        oracle = project.run_python("-m", "pytest", "-q", *arguments, env=environment)
        assert oracle.returncode == status, (oracle.stdout, oracle.stderr)
        expected = json.loads((project.root / "selected.json").read_text())
        (project.root / "selected.json").unlink()
        (project.root / "plugin-origin.json").unlink()
        actual = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            *arguments,
            env={**environment, "VFY_BLOCK_UPSTREAM": "1"},
        )
        assert (project.root / "upstream-attempts.jsonl").read_text() == ""
        selected = project.root / "selected.json"
        assert actual.returncode == status, (
            actual.stdout,
            actual.stderr,
            selected.read_text() if selected.exists() else "no body witness",
        )
        assert selected.exists(), (actual.stdout, actual.stderr)
        assert json.loads(selected.read_text()) == expected
        assert json.loads((project.root / "plugin-origin.json").read_text()) == str(
            preferred / "configured_plugin.py"
        )
        assert expected == {
            "module": str(preferred / "selected_application.py"),
            "value": value,
            "plugin": str(preferred / "configured_plugin.py"),
            "paths": [str(preferred), str(secondary)],
            "path_types": [[type(Path()).__module__, type(Path()).__name__]] * 2,
        }
        (project.root / "selected.json").unlink()
        (project.root / "plugin-origin.json").unlink()


@pytest.mark.parametrize("mutate_declared_paths", [False, True])
def test_pythonpath_cleanup_uses_live_values_and_preserves_other_path_mutations(
    tmp_path: Path, mutate_declared_paths: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("sitecustomize.py", _BLOCKER)
    project.write("pytest.ini", "[pytest]\npythonpath = preferred preferred secondary\n")
    project.write("preferred/selected_application.py", "VALUE = 42\n")
    project.write("secondary/selected_application.py", "VALUE = 999\n")
    project.write(
        "preferred/configured_plugin.py",
        """import sys
from pathlib import Path
sys.path.append(str(Path.cwd() / 'plugin-added'))
""",
    )
    project.write(
        "conftest.py",
        """import os
def pytest_sessionfinish(session, exitstatus):
    if os.environ.get('MUTATE_DECLARED_PATHS'):
        session.config.getini('pythonpath').pop(0)
""",
    )
    project.write(
        "test_selected.py",
        """import sys
from pathlib import Path
import selected_application

def test_selected():
    sys.path.append(str(Path.cwd() / 'body-added'))
    assert selected_application.VALUE == 42
""",
    )
    root = project.root
    project.write(
        "sitecustomize.py",
        _BLOCKER
        + f"""
import atexit

_root = Path({str(root)!r})
_names = ['preferred', 'secondary', 'plugin-added', 'body-added']

def _record():
    if 'configured_plugin' not in sys.modules and 'selected_application' not in sys.modules:
        return
    counts = [sys.path.count(str(_root / name)) for name in _names]
    Path(os.environ['COUNTS_FILE']).write_text(json.dumps(counts))

atexit.register(_record)
""",
    )
    expected: dict[str, list[int]] = {}
    for failing in (False, True):
        stage = "after_failure" if failing else "after_success"
        arguments = ["-q", "--import-mode=importlib", "-p", "configured_plugin"]
        if failing:
            arguments.extend(["-o", "required_plugins=not-installed-vfy-plugin"])
        environment = {
            "PYTHONPATH": os.pathsep.join([str(root / "preferred"), str(root), _SOURCE]),
            "MUTATE_DECLARED_PATHS": "1" if mutate_declared_paths else None,
        }
        oracle = project.run_python(
            "-m", "pytest", *arguments, env={**environment, "COUNTS_FILE": "oracle.json"}
        )
        assert oracle.returncode == (4 if failing else 0), (oracle.stdout, oracle.stderr)
        actual = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            *arguments,
            env={**environment, "COUNTS_FILE": "candidate.json", "VFY_BLOCK_UPSTREAM": "1"},
        )
        assert (root / "upstream-attempts.jsonl").read_text() == ""
        assert actual.returncode == (4 if failing else 0), (actual.stdout, actual.stderr)
        expected[stage] = json.loads((root / "oracle.json").read_text())
        assert json.loads((root / "candidate.json").read_text()) == expected[stage]
    remaining = 2 if mutate_declared_paths else 1
    assert expected["after_success"] == [remaining, 0, 1, 1]
    assert expected["after_failure"] == [1, 0, 1, 0]
