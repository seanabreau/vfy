"""Independent runs preserve controller and child responsibilities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.history import read_history
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _controller_probe(project: VfyProject, *, block: bool) -> dict[str, str]:
    startup = project.write(
        "startup/sitecustomize.py",
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "parent = int(os.environ.setdefault('VFY_CONTROLLER_PROBE_PID', str(os.getpid())))\n"
        "if os.getpid() == parent:\n"
        "    Path('parent-guard-started').touch()\n"
        "class ControllerGuard:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        general = {'vfy.run.controller', 'vfy.run.selected_run'}\n"
        "        if fullname in general and os.getpid() == parent:\n"
        "            Path('parent-controller-import').touch()\n"
        f"            if {block!r}:\n"
        "                raise RuntimeError('parent controller import was blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, ControllerGuard())\n",
    )
    return {"PYTHONPATH": str(startup.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def _passing_test(project: VfyProject) -> None:
    project.write(
        "test_example.py",
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import app\n"
        "def test_example():\n"
        "    assert app.answer() == 42\n"
        "    Path('child.json').write_text(json.dumps({\n"
        "        'pid': os.getpid(), 'ppid': os.getppid(),\n"
        "        'parent': int(os.environ['VFY_CONTROLLER_PROBE_PID'])\n"
        "    }))\n",
    )
    project.write("app.py", "def answer():\n    return 42\n")


def _assert_child_and_history(
    root: Path, *, nodeid: str = "test_example.py::test_example", outcome: str = "passed"
) -> None:
    child = json.loads((root / "child.json").read_text())
    assert child["pid"] != child["parent"]
    assert child["ppid"] == child["parent"]
    history = read_history(root / ".vfy/test-history.sqlite3")
    assert history["tests"][nodeid]["observations"][-1]["outcome"] == outcome


@pytest.mark.parametrize("configured", [False, True], ids=["command", "pytest-ini"])
def test_execution_only_parent_does_not_import_legacy_controller(
    tmp_path: Path, configured: bool
) -> None:
    project = VfyProject(tmp_path)
    _passing_test(project)
    if configured:
        project.write("pytest.ini", "[pytest]\naddopts = --no-cov\n")
    environment = _controller_probe(project, block=True)

    result = project.run_vfy(
        "test",
        "--engine=native",
        *(("--no-cov",) if not configured else ()),
        "-q",
        env=environment,
    )

    assert (tmp_path / "parent-guard-started").is_file()
    result.expect(0)
    assert "1 passed" in result.stdout
    assert not (tmp_path / "parent-controller-import").exists()
    _assert_child_and_history(tmp_path)


def test_configured_coverage_uses_general_controller_and_records_coverage(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    _passing_test(project)
    project.write("pytest.ini", "[pytest]\naddopts = --cov=app --cov-report=\n")
    environment = _controller_probe(project, block=False)

    result = project.run_vfy("test", "--engine=native", "-q", env=environment)

    assert (tmp_path / "parent-guard-started").is_file()
    result.expect(0)
    assert "1 passed" in result.stdout
    assert (tmp_path / "parent-controller-import").is_file()
    assert project.data().lines(str(tmp_path / "app.py")) == [1, 2]
    _assert_child_and_history(tmp_path)


def test_execution_only_failure_records_failed_history(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_failure.py",
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "def test_failure():\n"
        "    Path('child.json').write_text(json.dumps({\n"
        "        'pid': os.getpid(), 'ppid': os.getppid(),\n"
        "        'parent': int(os.environ['VFY_CONTROLLER_PROBE_PID'])\n"
        "    }))\n"
        "    assert False, 'execution witness'\n",
    )
    environment = _controller_probe(project, block=True)

    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=environment)

    assert (tmp_path / "parent-guard-started").is_file()
    result.expect(1)
    assert "execution witness" in result.stdout + result.stderr
    assert not (tmp_path / "parent-controller-import").exists()
    _assert_child_and_history(tmp_path, nodeid="test_failure.py::test_failure", outcome="failed")


@pytest.mark.parametrize("query", ["--collect-only", "--version"])
def test_execution_only_query_does_not_record_history(tmp_path: Path, query: str) -> None:
    project = VfyProject(tmp_path)
    _passing_test(project)
    environment = _controller_probe(project, block=True)

    result = project.run_vfy("test", "--engine=native", "--no-cov", query, "-q", env=environment)

    assert (tmp_path / "parent-guard-started").is_file()
    result.expect(0)
    assert not (tmp_path / "parent-controller-import").exists()
    assert not (tmp_path / "child.json").exists()
    assert not (tmp_path / ".vfy/test-history.sqlite3").exists()
    if query == "--collect-only":
        assert "test_example.py::test_example" in result.stdout
    else:
        assert result.stdout.startswith("vfy ")


def test_execution_only_explicit_workers_run_in_distinct_processes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            "import os\nfrom pathlib import Path\n"
            f"def test_{name}():\n    Path('{name}.pid').write_text(str(os.getpid()))\n",
        )
    environment = _controller_probe(project, block=True)

    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "--workers=2", "-q", env=environment
    )

    assert (tmp_path / "parent-guard-started").is_file()
    result.expect(0)
    assert "2 passed" in result.stdout
    assert not (tmp_path / "parent-controller-import").exists()
    assert len({(tmp_path / f"{name}.pid").read_text() for name in ("a", "b")}) == 2
    history = read_history(tmp_path / ".vfy/test-history.sqlite3")
    for name in ("a", "b"):
        assert (
            history["tests"][f"test_{name}.py::test_{name}"]["observations"][-1]["outcome"]
            == "passed"
        )
