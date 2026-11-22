"""Body-only edits collect only the test files whose tests they select."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="independent execution requires POSIX")

_ARGUMENTS = (
    "test",
    "--engine=native",
    "--cov=app",
    "--cov-branch",
    "--cov-report=",
    "--native-result-json=.vfy/result.json",
    "-q",
)


def _project(tmp_path: Path, *, parametrized_ids: bool = False) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\nids.txt\n")
    project.write(
        "app.py", "LIMIT = 2\ndef first():\n    return 1\ndef second():\n    return LIMIT\n"
    )
    first_cases = (
        "import pytest\nfrom pathlib import Path\n"
        "@pytest.mark.parametrize('name', Path('ids.txt').read_text().split())\n"
        "def test_first(name):\n    assert first() == 1\n"
        if parametrized_ids
        else "def test_first():\n    assert first() == 1\n"
    )
    project.write("test_first.py", "from app import first\n" + first_cases)
    project.write(
        "test_second.py", "from app import second\ndef test_second():\n    assert second() == 2\n"
    )
    project.write(
        "test_skipped.py",
        "import pytest\n@pytest.mark.skipif('True', reason='never here')\n"
        "def test_skipped():\n    pass\n",
    )
    project.write(
        "conftest.py",
        "import json, sys\nfrom pathlib import Path\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    imported = sorted(name for name in sys.modules if name.startswith('test_'))\n"
        "    Path('.vfy/imported.json').write_text(json.dumps(imported))\n",
    )
    if parametrized_ids:
        project.write("ids.txt", "a b\n")
    project.git_init()
    (tmp_path / ".vfy").mkdir(exist_ok=True)
    return project


def _executed(root: Path) -> list[str]:
    record = json.loads((root / ".vfy/result.json").read_text())
    return [case["nodeid"] for case in record["cases"]]


def _imported(root: Path) -> list[str]:
    imported = json.loads((root / ".vfy/imported.json").read_text())
    return [str(name) for name in imported]


def _evidence(project: VfyProject, root: Path) -> tuple[object, ...]:
    data = project.data()
    app = str((root / "app.py").resolve())
    return data.lines(app), data.arcs(app), data.contexts_by_lineno(app)


def test_body_edit_imports_only_selected_files_and_reconstructs_full_coverage(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    project.run_vfy(*_ARGUMENTS, "--all").expect(0)
    project.write(
        "app.py", "LIMIT = 2\ndef first():\n    return int('1')\ndef second():\n    return LIMIT\n"
    )

    selected = project.run_vfy(*_ARGUMENTS).expect(0)

    assert sorted(_executed(tmp_path)) == [
        "test_first.py::test_first",
        "test_skipped.py::test_skipped",
    ]
    assert _imported(tmp_path) == ["test_first", "test_skipped"]
    assert "Selected 2 / 3 tests (after collection)" in selected.stderr
    assert "Reused 1 tests" in selected.stderr
    reconstructed = _evidence(project, tmp_path)
    project.run_vfy(*_ARGUMENTS, "--all").expect(0)
    assert reconstructed == _evidence(project, tmp_path)


def test_module_edit_collects_every_file_and_reruns_contextless_tests(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.run_vfy(*_ARGUMENTS, "--all").expect(0)
    project.write(
        "app.py", "LIMIT = 1 + 1\ndef first():\n    return 1\ndef second():\n    return LIMIT\n"
    )

    project.run_vfy(*_ARGUMENTS).expect(0)

    assert sorted(_executed(tmp_path)) == [
        "test_first.py::test_first",
        "test_second.py::test_second",
        "test_skipped.py::test_skipped",
    ]
    assert _imported(tmp_path) == ["test_first", "test_second", "test_skipped"]


def test_changed_scoped_collection_repeats_with_complete_collection(tmp_path: Path) -> None:
    project = _project(tmp_path, parametrized_ids=True)
    project.run_vfy(*_ARGUMENTS, "--all").expect(0)
    project.write(
        "app.py", "LIMIT = 2\ndef first():\n    return int('1')\ndef second():\n    return LIMIT\n"
    )
    # An ignored input renames the selected file's cases without changing Git state.
    project.write("ids.txt", "a c\n")

    rerun = project.run_vfy(*_ARGUMENTS).expect(0)

    assert "Collecting the complete suite to bind the selection" in rerun.stderr
    assert "collected test inventory changed; running the full suite" in rerun.stderr
    assert sorted(_executed(tmp_path)) == [
        "test_first.py::test_first[a]",
        "test_first.py::test_first[c]",
        "test_second.py::test_second",
        "test_skipped.py::test_skipped",
    ]
    assert _imported(tmp_path) == ["test_first", "test_second", "test_skipped"]


def test_body_edit_reruns_tests_with_unmeasured_subprocesses(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write(
        "test_first.py", "from app import first\ndef test_first():\n    assert first() > 0\n"
    )
    project.write(
        "test_child.py",
        "import subprocess, sys\n"
        "def test_child():\n"
        "    child = subprocess.run([sys.executable, '-S', '-c', "
        "'from app import first; print(first())'], capture_output=True, text=True, check=True)\n"
        "    assert child.stdout.strip() == '1'\n",
    )
    project.git_commit("Add subprocess consumer")
    project.run_vfy(*_ARGUMENTS, "--all").expect(0)
    app = str((tmp_path / "app.py").resolve())
    contexts = project.data().contexts_by_lineno(app)
    assert any(
        "test_first.py::test_first" in context for row in contexts.values() for context in row
    )
    assert all(
        "test_child.py::test_child" not in context for row in contexts.values() for context in row
    )
    project.write(
        "app.py", "LIMIT = 2\ndef first():\n    return 200\ndef second():\n    return LIMIT\n"
    )

    selected = project.run_vfy(*_ARGUMENTS)
    executed = _executed(tmp_path)
    project.run_vfy(*_ARGUMENTS, "--all").expect(1)

    selected.expect(1)
    assert "test_child.py::test_child" in executed
    assert "test_child" in selected.stdout


def test_collection_time_body_edit_collects_every_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("cases.py", "def names():\n    return ['a']\n")
    project.write(
        "test_cases.py",
        "import pytest\nfrom cases import names\n"
        "@pytest.mark.parametrize('name', names())\n"
        "def test_cases(name):\n    pass\n",
    )
    project.git_commit("Add collection-time cases")
    arguments = (*_ARGUMENTS, "--cov=cases")
    project.run_vfy(*arguments, "--all").expect(0)
    project.write("cases.py", "def names():\n    return ['a', 'b']\n")

    project.run_vfy(*arguments).expect(0)

    assert sorted(_executed(tmp_path)) == [
        "test_cases.py::test_cases[a]",
        "test_cases.py::test_cases[b]",
        "test_first.py::test_first",
        "test_second.py::test_second",
        "test_skipped.py::test_skipped",
    ]
    assert _imported(tmp_path) == [
        "test_cases",
        "test_first",
        "test_second",
        "test_skipped",
    ]


def test_fixture_body_edit_collects_every_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.write("fixtures.py", "def make():\n    return 1\n")
    project.write(
        "test_fixture.py",
        "import pytest\nfrom fixtures import make\n"
        "@pytest.fixture\ndef value():\n    return make()\n"
        "def test_fixture(value):\n    assert value > 0\n",
    )
    project.git_commit("Add fixture consumer")
    arguments = (*_ARGUMENTS, "--cov=fixtures")
    project.run_vfy(*arguments, "--all").expect(0)
    project.write("fixtures.py", "def make():\n    return 2\n")

    project.run_vfy(*arguments).expect(0)

    assert "test_fixture" in _imported(tmp_path)
    assert _imported(tmp_path) == [
        "test_first",
        "test_fixture",
        "test_second",
        "test_skipped",
    ]


def test_plan_only_collects_every_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    project.run_vfy(*_ARGUMENTS, "--all").expect(0)
    project.write(
        "app.py", "LIMIT = 2\ndef first():\n    return int('1')\ndef second():\n    return LIMIT\n"
    )

    project.run_vfy(*_ARGUMENTS, "--plan-only").expect(0)

    assert _imported(tmp_path) == ["test_first", "test_second", "test_skipped"]
