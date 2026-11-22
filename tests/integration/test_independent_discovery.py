"""Configured independent discovery preserves pytest's selected execution order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CASE_SOURCE = """from pathlib import Path

def test_first(request):
    with Path('events').open('a') as output:
        output.write(request.node.nodeid + '\\n')

def test_second(request):
    with Path('events').open('a') as output:
        output.write(request.node.nodeid + '\\n')
"""


def _assert_execution_matches_pytest(
    project: VfyProject,
    arguments: list[str],
    expected: list[str],
    *,
    deselected: list[str] | None = None,
) -> None:
    oracle = project.run_pytest("-q", *arguments, load_xdist=False)
    oracle.expect(0)
    events = project.root / "events"
    assert events.read_text().splitlines() == expected
    events.unlink()

    independent = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--native-result-json=results.json",
        *arguments,
    )
    independent.expect(0)
    assert events.read_text().splitlines() == expected
    record = json.loads((project.root / "results.json").read_text())
    assert [case["nodeid"] for case in record["cases"]] == expected
    if deselected is not None:
        assert record["deselected_nodeids"] == deselected


@pytest.mark.parametrize(
    ("filename", "configuration"),
    [
        (
            "pytest.ini",
            "[pytest]\ntestpaths = suite/unit*\npython_files = check_*.py\n"
            "python_functions = check\npython_classes = Spec*\nnorecursedirs = excluded\n",
        ),
        (
            "pytest.toml",
            "[pytest]\ntestpaths = ['suite/unit*']\npython_files = ['check_*.py']\n"
            "python_functions = ['check']\npython_classes = ['Spec*']\n"
            "norecursedirs = ['excluded']\n",
        ),
    ],
)
def test_configured_names_testpaths_and_recursion_match_pytest(
    tmp_path: Path, filename: str, configuration: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(filename, configuration)
    source = _CASE_SOURCE.replace("test_first", "check_first").replace(
        "test_second", "check_second"
    )
    source += """
class Special:
    def check_class(self, request):
        with Path('events').open('a') as output:
            output.write(request.node.nodeid + '\\n')
"""
    for name in (
        "suite/unit_a/check_a.py",
        "suite/unit_a/.hidden/check_hidden.py",
        "suite/unit_a/excluded/check_excluded.py",
        "suite/unit_b/check_b.py",
        "check_outside.py",
    ):
        project.write(name, source)
    project.write(
        "suite/unit_a/test_unselected.py", "raise AssertionError('wrong naming pattern')\n"
    )

    _assert_execution_matches_pytest(
        project,
        [],
        [
            f"{path}::{name}"
            for path in (
                "suite/unit_a/.hidden/check_hidden.py",
                "suite/unit_a/check_a.py",
                "suite/unit_b/check_b.py",
            )
            for name in ("check_first", "check_second", "Special::check_class")
        ],
    )


@pytest.mark.parametrize(
    ("testpaths", "expected_files"),
    [
        ("suite/**", ["suite/nested/test_nested.py", "suite/test_top.py"]),
        (
            "suite/*",
            ["suite/helper.py", "suite/nested/test_nested.py", "suite/test_top.py"],
        ),
    ],
)
def test_testpaths_globs_preserve_directory_precedence_over_explicit_files(
    tmp_path: Path, testpaths: str, expected_files: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\ntestpaths = {testpaths}\n")
    for name in (
        "suite/helper.py",
        "suite/test_top.py",
        "suite/nested/extra.py",
        "suite/nested/test_nested.py",
    ):
        project.write(name, _CASE_SOURCE)

    _assert_execution_matches_pytest(
        project,
        [],
        [f"{path}::{name}" for path in expected_files for name in ("test_first", "test_second")],
    )


def test_explicit_targets_keep_order_and_bypass_file_name_and_ignore_filters(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_z.py", _CASE_SOURCE)
    project.write("helper.py", _CASE_SOURCE)

    _assert_execution_matches_pytest(
        project,
        [
            "test_z.py",
            "helper.py::test_second",
            "helper.py::test_first",
            "--ignore=helper.py",
        ],
        [
            "test_z.py::test_first",
            "test_z.py::test_second",
            "helper.py::test_second",
            "helper.py::test_first",
        ],
    )


@pytest.mark.parametrize(
    ("last_target", "expected"),
    [
        (
            "test_a.py::test_first",
            [
                "test_a.py::test_second",
                "test_b.py::test_first",
                "test_b.py::test_second",
                "test_a.py::test_first",
            ],
        ),
        (
            "test_a.py",
            [
                "test_b.py::test_first",
                "test_b.py::test_second",
                "test_a.py::test_first",
                "test_a.py::test_second",
            ],
        ),
    ],
)
def test_interleaved_selectors_and_later_bare_file_precedence_match_pytest(
    tmp_path: Path, last_target: str, expected: list[str]
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_a.py", _CASE_SOURCE)
    project.write("test_b.py", _CASE_SOURCE)

    _assert_execution_matches_pytest(
        project, ["test_a.py::test_second", "test_b.py", last_target], expected
    )


def test_missing_explicit_selector_reports_usage_error_without_running_tests(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_cases.py", _CASE_SOURCE)
    selector = "test_cases.py::test_missing"

    oracle = project.run_pytest("-q", selector, load_xdist=False)
    oracle.expect(4)
    assert "not found" in oracle.stderr
    assert not (tmp_path / "events").exists()

    independent = project.run_vfy("test", "--engine=native", "--no-cov", selector)
    independent.expect(4)
    assert "not found" in independent.stderr
    assert not (tmp_path / "events").exists()


def test_ignored_paths_globs_and_deselection_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_keep.py", _CASE_SOURCE)
    project.write(
        "ignored/test_unselected.py", "raise AssertionError('ignored directory imported')\n"
    )
    project.write("nested/test_drop.py", "raise AssertionError('ignored file imported')\n")

    _assert_execution_matches_pytest(
        project,
        [
            "--ignore=ignored",
            "--ignore-glob=*/test_drop.py",
            "--deselect=test_keep.py::test_first",
        ],
        ["test_keep.py::test_second"],
        deselected=["test_keep.py::test_first"],
    )


def test_invocation_subdirectory_uses_local_suite_and_root_relative_nodeids(tmp_path: Path) -> None:
    root_project = VfyProject(tmp_path)
    root_project.write("pytest.ini", "[pytest]\ntestpaths = unit\n")
    root_project.write(
        "unit/test_outside.py", "raise AssertionError('testpaths used below root')\n"
    )
    root_project.write("other/test_local.py", _CASE_SOURCE)
    project = VfyProject(tmp_path / "other")

    _assert_execution_matches_pytest(
        project,
        [],
        ["other/test_local.py::test_first", "other/test_local.py::test_second"],
    )


def test_symlink_directory_spelling_and_default_directory_filters_match_pytest(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("real/test_cases.py", _CASE_SOURCE)
    project.write("target/test_target.py", _CASE_SOURCE)
    project.write(".hidden/test_hidden.py", "raise AssertionError('hidden directory imported')\n")
    try:
        (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    _assert_execution_matches_pytest(
        project,
        [],
        [
            f"{path}::{name}"
            for path in ("alias/test_cases.py", "real/test_cases.py", "target/test_target.py")
            for name in ("test_first", "test_second")
        ],
    )


def test_explicit_hidden_directory_bypasses_default_and_ignore_filters(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".hidden/test_cases.py", _CASE_SOURCE)

    _assert_execution_matches_pytest(
        project,
        [".hidden", "--ignore=.hidden"],
        [".hidden/test_cases.py::test_first", ".hidden/test_cases.py::test_second"],
    )
