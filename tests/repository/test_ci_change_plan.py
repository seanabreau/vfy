from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "ci_change_plan.py"


def plan(*paths: str) -> dict[str, str]:
    arguments = [argument for path in paths for argument in ("--path", path)]
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return dict(line.split("=", 1) for line in completed.stdout.splitlines())


def test_documentation_changes_use_the_lightest_tier() -> None:
    result = plan("docs/about/testing.md", "changelogs/0.0.x.md", "README.md", "overrides/404.html")
    assert result["scope"] == "documentation"
    assert result["run_quality"] == "false"
    assert result["full_matrix"] == "false"


def test_github_output_preserves_existing_step_outputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.write_text("cache_key=candidate\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--path", "README.md", "--github-output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    assert output.read_text(encoding="utf-8").splitlines() == [
        "cache_key=candidate",
        "scope=documentation",
        "run_quality=false",
        "full_matrix=false",
    ]


def test_similarly_named_files_are_not_mistaken_for_documentation() -> None:
    result = plan("README.md.backup")
    assert result["scope"] == "unknown"
    assert result["full_matrix"] == "true"


def test_runtime_boundary_changes_require_the_full_platform_matrix() -> None:
    result = plan("crates/vfy/src/collector.rs", "python/vfy/process_startup.py")
    assert result["scope"] == "platform"
    assert result["full_matrix"] == "true"


@pytest.mark.parametrize(
    "path",
    [
        "python/vfy/native.py",
        "python/vfy/pytest_plugin.py",
        "python/vfy/pytest_reporting.py",
        "python/vfy/run/selection.py",
        "python/vfy/run/watch_scope.py",
        "python/vfy/run/pytest_controller.py",
    ],
)
def test_rust_and_lifecycle_adapters_require_the_full_platform_matrix(path: str) -> None:
    result = plan(path)
    assert result["scope"] == "platform"
    assert result["full_matrix"] == "true"


def test_process_and_pyo3_boundary_tests_require_the_full_platform_matrix() -> None:
    result = plan(
        "tests/integration/test_pytest_cov_compat.py",
        "tests/integration/test_rust_transactions.py",
    )
    assert result["scope"] == "platform"
    assert result["full_matrix"] == "true"


def test_ordinary_core_and_python_changes_use_the_fast_code_tier() -> None:
    result = plan("crates/vfy-core/src/semantic/logic.rs", "python/vfy/cli.py")
    assert result["scope"] == "code"
    assert result["run_quality"] == "true"
    assert result["full_matrix"] == "false"


def test_unlisted_vfy_adapter_changes_are_known_code() -> None:
    result = plan("python/vfy/new_adapter.py")
    assert result["scope"] == "code"
    assert result["full_matrix"] == "false"


@pytest.mark.parametrize("documentation", ["README.md", "changelogs/0.0.x.md"])
@pytest.mark.parametrize(
    ("path", "scope", "full_matrix"),
    [
        ("python/vfy/cli.py", "code", "false"),
        ("tests/integration/test_cli.py", "tests", "false"),
        ("python/vfy/process_startup.py", "platform", "true"),
    ],
)
def test_documentation_preserves_the_required_tier_for_known_changes(
    documentation: str, path: str, scope: str, full_matrix: str
) -> None:
    assert plan(path, documentation) == {
        "scope": scope,
        "run_quality": "true",
        "full_matrix": full_matrix,
    }


@pytest.mark.parametrize("documentation", ["docs/index.md", "README.md"])
def test_unknown_changes_with_documentation_require_the_full_matrix(documentation: str) -> None:
    result = plan(documentation, "new-top-level-tool.toml")
    assert result["scope"] == "unknown"
    assert result["run_quality"] == "true"
    assert result["full_matrix"] == "true"


def test_non_documentation_assets_do_not_skip_the_test_matrix() -> None:
    result = plan("tests/fixtures/screenshot.svg")
    assert result["scope"] == "tests"
    assert result["full_matrix"] == "false"


def test_markdown_test_fixtures_are_code_not_documentation() -> None:
    result = plan("tests/fixtures/semantic/boolean_expression.md")
    assert result["scope"] == "tests"
    assert result["run_quality"] == "true"


def test_test_only_changes_keep_feedback_fast() -> None:
    result = plan("tests/integration/test_cli.py", "benchmarks/benchmark_report.py")
    assert result["scope"] == "tests"
    assert result["run_quality"] == "true"
    assert result["full_matrix"] == "false"


def test_empty_change_set_is_unknown_and_full_matrix() -> None:
    result = plan()
    assert result["scope"] == "unknown"
    assert result["full_matrix"] == "true"


def test_renaming_runtime_code_into_docs_keeps_the_platform_matrix(tmp_path: Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.name", "CI test")
    git("config", "user.email", "ci@example.invalid")
    git("config", "diff.renames", "true")
    source = tmp_path / "python/vfy/collector.py"
    source.parent.mkdir(parents=True)
    source.write_text("runtime code\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    (tmp_path / "docs").mkdir()
    git("mv", "python/vfy/collector.py", "docs/collector.md")
    git("commit", "-m", "move runtime code into docs")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert dict(line.split("=", 1) for line in completed.stdout.splitlines()) == {
        "scope": "platform",
        "run_quality": "true",
        "full_matrix": "true",
    }
