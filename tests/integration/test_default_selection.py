"""The explicit native engine selects affected tests and preserves safe fallbacks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")), reason="native pytest requires Linux or macOS"
)

_ARGUMENTS = ("--cov=pkg", "--cov-branch", "--cov-report=", "-q")
_FULL = "Running the full suite"
_SKIPPED = "`vfy test --all` runs everything"


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    for name, value in (("a", 1), ("b", 2)):
        project.write(f"pkg/{name}.py", f"def value():\n    return {value}\n")
        project.write(
            f"tests/test_{name}.py",
            f"from pkg.{name} import value\n\ndef test_{name}():\n    assert value() == {value}\n",
        )
    project.write("data.txt", "input\n")
    project.write("conftest.py", "# shared fixtures\n")
    project.git_init()
    return project


def _run_native_selection(
    project: VfyProject,
    *arguments: str,
    workers: str = "1",
    env: dict[str, str | None] | None = None,
) -> CommandResult:
    return project.run_vfy(
        "test", "--engine=pytest-native", f"--workers={workers}", *arguments, *_ARGUMENTS, env=env
    )


def _recorded(project: VfyProject, relative: str) -> dict[str, object]:
    """Return per-file measurements that must match between selected and full runs."""

    data = project.data(relative)
    return {
        str(Path(path).relative_to(project.root)): (
            data.lines(path),
            data.arcs(path),
            {line: sorted(c) for line, c in sorted(data.contexts_by_lineno(path).items())},
        )
        for path in sorted(data.measured_files())
        if Path(path).is_relative_to(project.root / "pkg")
    }


def _baselined(root: Path) -> VfyProject:
    project = _project(root)
    first = _run_native_selection(project)
    first.expect(0)
    return project


def test_first_run_executes_every_test_and_records_a_baseline(tmp_path: Path) -> None:
    project = _project(tmp_path)

    first = _run_native_selection(project)

    first.expect(0)
    assert "2 passed" in first.stdout
    assert _FULL in first.stderr
    assert "no passing baseline exists yet" in first.stderr
    assert (tmp_path / ".vfy/replay.coverage").is_file()
    assert (tmp_path / ".coverage").is_file()


@pytest.mark.parametrize("workers", ["1", "2"])
@pytest.mark.parametrize("edit", ["passing", "failing"])
def test_edit_runs_only_affected_tests_and_matches_the_full_run(
    tmp_path: Path, workers: str, edit: str
) -> None:
    project = _project(tmp_path)
    assert _run_native_selection(project, workers=workers).returncode == 0
    value = "1 + 0" if edit == "passing" else "10"
    project.write("pkg/a.py", f"def value():\n    return {value}\n")

    selected = _run_native_selection(project, workers=workers)

    expected_status = 0 if edit == "passing" else 1
    assert selected.returncode == expected_status, selected.stdout + selected.stderr
    assert "Selected 1 / 2 tests" in selected.stderr
    assert _SKIPPED in selected.stderr
    assert ("1 passed" if edit == "passing" else "1 failed") in selected.stdout
    assert "2 passed" not in selected.stdout
    if edit == "failing":
        assert "Passing baseline preserved" in selected.stderr
        full = _run_native_selection(project, "--all", workers=workers)
        assert full.returncode == selected.returncode
        assert "1 failed, 1 passed" in full.stdout
        return
    reconstructed = _recorded(project, ".coverage")
    full = _run_native_selection(project, "--all", workers=workers)
    assert full.returncode == selected.returncode == 0, full.stdout + full.stderr
    assert "2 passed" in full.stdout
    assert _recorded(project, ".coverage") == reconstructed


def test_selected_inventory_and_reuse_follow_completed_pytest_output(tmp_path: Path) -> None:
    project = _project(tmp_path)
    # Merge streams as a terminal does, so buffered progress cannot hide interleaving.
    command = (
        "import subprocess, sys; "
        "raise SystemExit(subprocess.call([sys.executable, "
        "'-m', 'vfy', 'test', '--engine=pytest-native', "
        "'--workers=1', '--cov=pkg', '--cov-branch', '--cov-report='], "
        "stderr=subprocess.STDOUT))"
    )
    baseline = project.run_python("-c", command)
    baseline.expect(0)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")

    selected = project.run_python("-c", command)

    selected.expect(0)
    output = selected.stdout
    assert "Selected 1 / 2 tests (from the saved baseline; collection follows)" in output
    assert "collected 1 item" in output
    assert output.index("1 passed") < output.index("Reused 1 tests")
    assert any(line.startswith("Reused 1 tests;") for line in output.splitlines())


def test_unchanged_tree_restores_coverage_and_names_the_way_to_run_everything(
    tmp_path: Path,
) -> None:
    project = _baselined(tmp_path)

    repeated = _run_native_selection(project)

    repeated.expect(0)
    assert "passed" not in repeated.stdout
    assert "No code changed" in repeated.stderr
    assert _SKIPPED in repeated.stderr
    assert "2 passed" in _run_native_selection(project, "--all").stdout


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("untracked", "untracked files"),
        ("conftest", "test discovery"),
        ("unmapped", "not a Python source file"),
        ("deleted", "deleted or renamed files"),
        ("configuration", "invocation changed"),
    ],
)
def test_safety_conditions_run_the_complete_scope(tmp_path: Path, change: str, reason: str) -> None:
    project = _baselined(tmp_path)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    if change == "untracked":
        project.write("pkg/extra.py", "VALUE = 1\n")
    elif change == "conftest":
        project.write("conftest.py", "# changed fixtures\n")
    elif change == "unmapped":
        project.write("data.txt", "changed input\n")
    elif change == "deleted":
        project.run("git", "rm", "-q", "tests/test_b.py")
    else:
        project.write("pytest.ini", "[pytest]\naddopts = -p no:randomly\n")

    completed = _run_native_selection(project)

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert _FULL in completed.stderr
    assert reason in completed.stderr, output
    assert _SKIPPED not in completed.stderr
    assert ("1 passed" if change == "deleted" else "2 passed") in completed.stdout


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        (("--no-cov",), "coverage is disabled"),
        (("-k", "test_a or test_b"), "pytest selection options"),
        (("-o", "addopts=-k 'test_a or test_b'"), "pytest selection options"),
        (("tests/test_a.py", "tests/test_b.py"), "pytest selection options"),
    ],
)
def test_requests_that_change_pytest_scope_run_the_requested_scope(
    tmp_path: Path, arguments: tuple[str, ...], reason: str
) -> None:
    project = _baselined(tmp_path)
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")

    completed = project.run_vfy("test", "--engine=pytest-native", "--workers=1", *arguments, "-q")

    completed.expect(0)
    assert reason in completed.stderr
    assert "2 passed" in completed.stdout


@pytest.mark.parametrize(
    "addopts",
    [
        "-m 'not slow'",
        "--deselect tests/test_slow.py::test_slow",
        "-k 'not slow'",
        "tests/test_a.py tests/test_b.py",
        "--ignore-glob tests/test_slow.py",
    ],
)
def test_static_selectors_in_config_addopts_define_the_selectable_suite(
    tmp_path: Path, addopts: str
) -> None:
    project = _project(tmp_path)
    project.write(
        "pytest.ini", f"[pytest]\nmarkers =\n    slow: excluded by default\naddopts = {addopts}\n"
    )
    project.write(
        "tests/test_slow.py",
        "import pytest\n\n@pytest.mark.slow\ndef test_slow():\n    raise AssertionError\n",
    )
    project.git_commit("Exclude slow tests in pytest configuration")
    baseline = _run_native_selection(project, "--all")
    baseline.expect(0)
    assert "2 passed" in baseline.stdout
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")

    selected = _run_native_selection(project)
    narrowed = _run_native_selection(project, "-k", "test_a or test_b")

    selected.expect(0)
    assert "Selected 1 / 2 tests" in selected.stderr
    assert "1 passed" in selected.stdout
    narrowed.expect(0)
    assert "pytest selection options" in narrowed.stderr
    assert "2 passed" in narrowed.stdout


def test_addopts_override_selector_cannot_publish_all_baseline(tmp_path: Path) -> None:
    project = _project(tmp_path)

    completed = _run_native_selection(project, "--all", "-o", "addopts=-k test_a")

    assert completed.returncode != 0
    assert "--all cannot be combined with pytest test-selection options" in completed.stderr
    assert not (tmp_path / ".vfy/test-run.json").exists()


def test_coverage_source_package_names_select_affected_tests(tmp_path: Path) -> None:
    # Coverage.py treats a source entry that is not a path as an importable
    # package, as in Pallets projects' `source = ["jinja2", "tests"]`.
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write(
        "pyproject.toml",
        '[tool.coverage.run]\nsource = ["pkg", "tests"]\n',
    )
    project.write("src/pkg/__init__.py", "")
    for name, value in (("a", 1), ("b", 2)):
        project.write(f"src/pkg/{name}.py", f"def value():\n    return {value}\n")
        project.write(
            f"tests/test_{name}.py",
            f"from pkg.{name} import value\n\ndef test_{name}():\n    assert value() == {value}\n",
        )
    project.git_init()
    environment: dict[str, str | None] = {"PYTHONPATH": "src"}
    arguments = ("test", "--engine=pytest-native", "--workers=1", "--cov", "--cov-report=", "-q")
    baseline = project.run_vfy(*arguments, env=environment)
    baseline.expect(0)
    project.write("src/pkg/a.py", "def value():\n    return 1 + 0\n")

    selected = project.run_vfy(*arguments, env=environment)

    selected.expect(0)
    assert "Selected 1 / 2 tests" in selected.stderr
    assert "source root does not exist" not in selected.stderr


def test_package_source_import_graph_selects_transitive_dependent(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pyproject.toml", '[tool.coverage.run]\nsource = ["pkg"]\n')
    project.write("src/pkg/__init__.py", "")
    project.write("src/pkg/base.py", "VALUE = 1\n")
    project.write("src/pkg/dependent.py", "from pkg.base import VALUE\n")
    project.write(
        "tests/test_dependent.py",
        "from pkg.dependent import VALUE\n\ndef test_dependent():\n    assert VALUE == 1\n",
    )
    project.write("tests/test_other.py", "def test_other():\n    assert True\n")
    project.git_init()
    environment: dict[str, str | None] = {"PYTHONPATH": "src"}
    arguments = ("test", "--engine=pytest-native", "--workers=1", "--cov", "--cov-report=", "-q")
    baseline = project.run_vfy(*arguments, env=environment)
    baseline.expect(0)
    project.write("src/pkg/base.py", "VALUE = 1 + 0\n")

    selected = project.run_vfy(*arguments, env=environment)

    selected.expect(0)
    assert "Selected 1 / 2 tests" in selected.stderr


def test_dotted_source_root_does_not_import_parent_before_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pkg/__init__.py", "from pathlib import Path\nassert Path('conftest-loaded').exists()\n"
    )
    project.write("pkg/child.py", "value = 1\n")
    project.write("conftest.py", "from pathlib import Path\nPath('conftest-loaded').touch()\n")
    project.write(
        "tests/test_child.py",
        "from pkg.child import value\n\ndef test_child():\n    assert value == 1\n",
    )

    result = project.run_vfy(
        "test", "--engine=native", "--workers=1", "--cov=pkg.child", "--cov-report=", "-q"
    )

    result.expect(0)
    assert "1 passed" in result.stdout


@pytest.mark.parametrize("policy", ["balanced", "aggressive"])
def test_configured_narrow_policy_selects_and_all_runs_everything(
    tmp_path: Path, policy: str
) -> None:
    project = _project(tmp_path)
    project.write("pyproject.toml", f'[tool.vfy.test]\nselection = "{policy}"\n')
    project.git_commit("Configure the selection policy")
    assert _run_native_selection(project).returncode == 0
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")

    selected = _run_native_selection(project)
    everything = _run_native_selection(project, "--all")

    selected.expect(0)
    assert "Selected 1 / 2 tests" in selected.stderr
    assert "2 passed" in everything.stdout


_SHARED_MODULE = """\
def used_by_a():
    return 1


def at_import():
    return 2


def for_fixture():
    return 3


VALUE = at_import()
"""


@pytest.mark.parametrize(
    ("function", "selected", "failed"),
    [
        ("used_by_a", "Selected 1 / 4 tests", ("test_a",)),
        ("at_import", "Selected 4 / 4 tests", ("test_import",)),
        ("for_fixture", "Selected 4 / 4 tests", ("test_fixture_first", "test_fixture_second")),
    ],
)
def test_function_body_edit_selects_callers_unless_its_result_is_shared(
    tmp_path: Path, function: str, selected: str, failed: tuple[str, ...]
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    project.write("pkg/shared.py", _SHARED_MODULE)
    project.write(
        "conftest.py",
        "import pytest\nfrom pkg.shared import for_fixture\n\n\n"
        '@pytest.fixture(scope="session")\ndef shared():\n    return for_fixture()\n',
    )
    project.write(
        "tests/test_shared.py",
        "from pkg.shared import VALUE, used_by_a\n\n\n"
        "def test_a():\n    assert used_by_a() == 1\n\n\n"
        "def test_import():\n    assert VALUE == 2\n\n\n"
        "def test_fixture_first(shared):\n    assert shared == 3\n\n\n"
        "def test_fixture_second(shared):\n    assert shared == 3\n",
    )
    project.git_init()
    baseline = _run_native_selection(project)
    baseline.expect(0)
    original = f"def {function}():\n    return "
    project.write("pkg/shared.py", _SHARED_MODULE.replace(original, f"{original}10 + "))

    rerun = _run_native_selection(project)

    rerun.expect(1)
    assert selected in rerun.stderr
    assert f"{len(failed)} failed" in rerun.stdout
    for name in failed:
        assert f"FAILED tests/test_shared.py::{name}" in rerun.stdout


@pytest.mark.parametrize("workers", ["1", "2"])
def test_function_body_edit_reconstructs_the_full_run_coverage(
    tmp_path: Path, workers: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".gitignore", ".coverage*\n.vfy/\n__pycache__/\n.pytest_cache/\n")
    project.write("pkg/__init__.py", "")
    project.write(
        "pkg/shapes.py",
        "def area(width, height):\n    return width * height\n\n\n"
        "def perimeter(width, height):\n    total = width + height\n    return 2 * total\n",
    )
    project.write(
        "tests/test_shapes.py",
        "from pkg.shapes import area, perimeter\n\n\n"
        "def test_area():\n    assert area(2, 3) == 6\n\n\n"
        "def test_perimeter():\n    assert perimeter(2, 3) == 10\n",
    )
    project.git_init()
    assert _run_native_selection(project, workers=workers).returncode == 0
    project.write(
        "pkg/shapes.py",
        "def area(width, height):\n    product = width * height\n    return product\n\n\n"
        "def perimeter(width, height):\n    total = width + height\n    return 2 * total\n",
    )

    selected = _run_native_selection(project, workers=workers)

    selected.expect(0)
    assert "Selected 1 / 2 tests" in selected.stderr
    reconstructed = _recorded(project, ".coverage")
    full = _run_native_selection(project, "--all", workers=workers)
    full.expect(0)
    assert _recorded(project, ".coverage") == reconstructed


def test_selected_run_imports_only_modules_with_selected_tests(tmp_path: Path) -> None:
    project = _project(tmp_path / "project")
    imports = tmp_path / "imports.txt"
    project.write(
        "tests/test_b.py",
        "import os\n\nwith open(os.environ['IMPORT_LOG'], 'a') as log:\n    log.write('b\\n')\n\n"
        "from pkg.b import value\n\ndef test_b():\n    assert value() == 2\n",
    )
    project.git_commit("Record test module imports")
    environment: dict[str, str | None] = {"IMPORT_LOG": str(imports)}
    assert _run_native_selection(project, "tests", env=environment).returncode == 0
    assert imports.read_text(encoding="utf-8") == "b\n"
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")

    selected = _run_native_selection(project, "tests", env=environment)

    selected.expect(0)
    assert "Selected 1 / 2 tests" in selected.stderr
    assert imports.read_text(encoding="utf-8") == "b\n"


@pytest.mark.parametrize("repository", ["absent", "without-commits"])
def test_independent_coverage_run_without_git_head_runs_the_full_suite(
    tmp_path: Path, repository: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "")
    project.write("pkg/a.py", "def value():\n    return 1\n")
    project.write(
        "tests/test_a.py", "from pkg.a import value\n\ndef test_a():\n    assert value() == 1\n"
    )
    if repository == "without-commits":
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    result = project.run_vfy("test", "--engine=native", *_ARGUMENTS)

    result.expect(0)
    assert "1 passed" in result.stdout
    assert _FULL in result.stderr
    assert "Git state is unavailable" in result.stderr
    assert (tmp_path / ".coverage").is_file()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("modified", "Git submodule data has uncommitted changes"),
        ("checkout", "Git submodule data is not checked out at its recorded commit"),
        ("deinit", "Git submodule data is not initialized"),
    ],
)
def test_submodule_selects_by_recorded_commit_and_runs_everything_when_it_differs(
    tmp_path: Path, change: str, reason: str
) -> None:
    origin = VfyProject(tmp_path / "origin")
    origin.write("value.txt", "1\n")
    origin.git_init()
    project = _project(tmp_path / "project")
    project.run(
        "git",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(origin.root),
        "data",
    ).expect(0)
    project.git_commit("Add a data submodule")

    def run(*arguments: str) -> CommandResult:
        return project.run_vfy("test", "--engine=native", *arguments, *_ARGUMENTS)

    project.run("git", "config", "submodule.data.ignore", "all").expect(0)
    complete = run("--all")
    complete.expect(0)
    assert "2 passed" in complete.stdout
    project.write("pkg/a.py", "def value():\n    return 1 + 0\n")
    selected = run()
    selected.expect(0)
    assert "Selected 1 / 2 tests" in selected.stderr
    if change == "modified":
        project.write("data/value.txt", "2\n")
    elif change == "checkout":
        identity = ("-c", "user.name=vfy Tests", "-c", "user.email=vfy-tests@example.invalid")
        project.run(
            "git", "-C", "data", *identity, "commit", "-q", "--allow-empty", "-m", "Bump"
        ).expect(0)
    else:
        project.run("git", "submodule", "deinit", "-q", "-f", "data").expect(0)

    changed = run()

    changed.expect(0)
    assert "2 passed" in changed.stdout
    assert _FULL in changed.stderr
    assert reason in changed.stderr


def test_uninitialized_submodule_cannot_seed_replay(tmp_path: Path) -> None:
    origin = VfyProject(tmp_path / "origin")
    origin.write("value.txt", "2\n")
    origin.git_init()
    project = _project(tmp_path / "project")
    project.write(
        "tests/test_b.py",
        "from pathlib import Path\n\n"
        "def test_b():\n"
        "    path = Path('data/value.txt')\n"
        "    assert not path.exists() or path.read_text() == '1\\n'\n",
    )
    project.run(
        "git",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(origin.root),
        "data",
    ).expect(0)
    project.git_commit("Add a data submodule")
    project.run("git", "submodule", "deinit", "-q", "-f", "data").expect(0)
    project.run_vfy("test", "--engine=pytest-native", "--all", *_ARGUMENTS).expect(0)
    project.run(
        "git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "-q"
    ).expect(0)

    result = project.run_vfy("test", "--engine=pytest-native", *_ARGUMENTS)

    result.expect(1)
    assert "test_b" in result.stdout
