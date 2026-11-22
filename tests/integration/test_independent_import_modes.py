"""Collected package identity and execution counts match the pytest import modes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


@pytest.mark.parametrize("mode", ["prepend", "append", "importlib"])
def test_package_initializer_execution_matches_each_import_mode(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    project.write("suite_pkg/__init__.py", "from . import test_case\n")
    project.write(
        "suite_pkg/test_case.py",
        """import builtins
import importlib
import json
from pathlib import Path

builtins.eager_target_import_count = getattr(builtins, 'eager_target_import_count', 0) + 1

def check():
    assert 1 == 2

def test_observe():
    from suite_pkg import test_case
    try:
        check()
    except AssertionError as error:
        explanation = str(error)
    observed = {
        'import_count': builtins.eager_target_import_count,
        'name': __name__,
        'package': __package__,
        'import_identity': importlib.import_module(__name__).__dict__ is globals(),
        'parent_identity': test_case.__dict__ is globals(),
        'assertion': explanation,
    }
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    oracle = project.run_pytest(
        "-q", f"--import-mode={mode}", "suite_pkg/test_case.py", load_xdist=False
    )
    oracle.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    (project.root / "observed.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--import-mode={mode}",
        "suite_pkg/test_case.py",
        env={"PYTHONPATH": _SOURCE},
    )
    actual.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected
    assert expected == {
        "import_count": 2 if mode == "importlib" else 1,
        "name": "suite_pkg.test_case",
        "package": "suite_pkg",
        "import_identity": True,
        "parent_identity": True,
        "assertion": "assert 1 == 2",
    }


@pytest.mark.parametrize("mode", ["prepend", "append"])
def test_import_mode_preserves_search_precedence_and_reports_file_conflicts(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    local = project.write(
        "suite/test_clash.py",
        """from pathlib import Path
Path('selected.txt').write_text('local import')
def test_selected():
    Path('selected.txt').write_text('local execution')
""",
    )
    installed = project.write(
        "installed/test_clash.py",
        """from pathlib import Path
Path('selected.txt').write_text('installed import')
def test_selected():
    Path('selected.txt').write_text('installed execution')
""",
    )
    oracle = project.run_pytest(
        "-q",
        f"--import-mode={mode}",
        "suite/test_clash.py",
        load_xdist=False,
        env={"PYTHONPATH": os.pathsep.join([str(installed.parent), str(local.parent)])},
    )
    expected = (oracle.returncode, (project.root / "selected.txt").read_text())
    (project.root / "selected.txt").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--import-mode={mode}",
        "suite/test_clash.py",
        env={"PYTHONPATH": os.pathsep.join([_SOURCE, str(installed.parent), str(local.parent)])},
    )
    selected = project.root / "selected.txt"
    observed = (actual.returncode, selected.read_text() if selected.exists() else None)
    assert observed == expected, actual.stdout + actual.stderr
    if mode == "append":
        assert expected == (2, "installed import")
        for result in [oracle, actual]:
            output = result.stdout + result.stderr
            assert "import file mismatch" in output
            assert str(local) in output
            assert str(installed) in output
    else:
        assert expected == (0, "local execution")


@pytest.mark.parametrize("mode", ["prepend", "append", "importlib"])
@pytest.mark.parametrize("origin", ["cli", "ini", "environment"])
def test_configured_import_mode_is_active_before_initial_conftest_imports(
    tmp_path: Path, mode: str, origin: str
) -> None:
    project = VfyProject(tmp_path)
    arguments = [f"--import-mode={mode}"] if origin == "cli" else []
    ini = f"addopts = --import-mode={mode}\n" if origin == "ini" else ""
    project.write("pytest.ini", f"[pytest]\n{ini}")
    environment = {"PYTEST_ADDOPTS": f"--import-mode={mode}"} if origin == "environment" else {}
    project.write(
        "branch/deep/conftest.py",
        """import json
from pathlib import Path
Path('initial.json').write_text(json.dumps({
    'name': __name__, 'package': __package__, 'file': __file__,
}))
""",
    )
    project.write("branch/deep/test_case.py", "def test_case(): assert 1 == 1\n")
    stock = project.run_pytest(
        *arguments, "branch/deep/test_case.py", load_xdist=False, env=environment
    )
    stock.expect(0)
    expected = json.loads((tmp_path / "initial.json").read_text())
    assert expected["name"] == ("branch.deep.conftest" if mode == "importlib" else "conftest")
    (tmp_path / "initial.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        *arguments,
        "branch/deep/test_case.py",
        env=environment,
    )
    actual.expect(0)
    assert json.loads((tmp_path / "initial.json").read_text()) == expected
