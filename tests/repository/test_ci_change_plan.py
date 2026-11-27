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


@pytest.mark.parametrize(
    ("paths", "scope", "check_docs"),
    [
        (("docs/about/testing.md", "changelogs/0.0.x.md", "README.md"), "documentation", "true"),
        (
            ("python/vfy/pytest_reports.py", "tests/integration/test_pytest_cov_compat.py"),
            "code",
            "false",
        ),
        (("python/vfy/independent/bootstrap.py", "python/vfy/run/ci_run.py"), "code", "false"),
        (("python/vfy/independent/bootstrap.py", "changelogs/0.0.x.md"), "code", "false"),
        (("python/vfy/run/selection.py", "docs/guides/test-selection.md"), "code", "true"),
        (("crates/vfy-core/src/watch/tests.rs", "python/vfy/run/plan.py"), "rust", "false"),
        (("crates/vfy-pytest/src/parallel/collection.rs",), "rust", "false"),
        (("crates\\vfy\\src\\collector.rs", "docs/about/testing.md"), "rust", "true"),
        (("scripts/cargo-test-env.sh",), "rust", "false"),
        (("Cargo.lock",), "rust", "false"),
        (("pyproject.toml",), "rust", "true"),
        (("uv.lock",), "rust", "true"),
        ((".github/workflows/docs.yml",), "code", "true"),
        ((".github/workflows/ci.yml", "scripts/check_ci_results.sh"), "code", "false"),
        (("examples/coverage/pyproject.toml", "python/vfy/cli.py"), "code", "true"),
        (("scripts/check_tutorial_install.py",), "code", "true"),
        (("tests/fixtures/semantic/boolean_expression.md",), "code", "false"),
        (("README.pypi.md",), "code", "false"),
        (("README.md.backup",), "unknown", "true"),
        (("docs/index.md", "new-top-level-tool.toml"), "unknown", "true"),
        ((), "unknown", "true"),
    ],
)
def test_changed_paths_select_required_checks(
    paths: tuple[str, ...], scope: str, check_docs: str
) -> None:
    assert plan(*paths) == {
        "scope": scope,
        "check_docs": check_docs,
        "check_release": "false",
        "check_corpus": "false",
    }


@pytest.mark.parametrize(
    ("path", "release", "corpus"),
    [
        (".github/workflows/release.yml", "true", "false"),
        (".github/workflows/release-verify.yml", "true", "false"),
        ("scripts/check_native_pytest_versions.sh", "true", "false"),
        ("validation/pytest_runner/check_native_adoption_matrix.py", "true", "false"),
        ("validation/pytest_runner/real_packages/corpus.py", "true", "false"),
        (".github/workflows/native-pytest.yml", "false", "true"),
        ("scripts/pytest_corpus_execution.py", "false", "true"),
        ("scripts/process_tree.py", "true", "true"),
        ("docs/about/pytest-test-policy.json", "false", "true"),
        ("validation/pytest_runner/check_native_project.py", "false", "true"),
        ("benchmarks/benchmark_provenance.py", "true", "true"),
        ("benchmarks/locks/rich.txt", "false", "true"),
    ],
)
def test_validation_tooling_selects_its_workflow(path: str, release: str, corpus: str) -> None:
    result = plan(path)
    assert result["scope"] == "code"
    assert result["check_release"] == release
    assert result["check_corpus"] == corpus


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
        "check_docs=true",
        "check_release=false",
        "check_corpus=false",
    ]


@pytest.mark.parametrize("base", ["", "not-a-ci-base"])
def test_unavailable_diff_cannot_skip_targeted_checks(base: str, tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--github-output", str(output)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize(("version", "expected"), [("0.1.0", "false"), ("0.1.1", "true")])
def test_workspace_version_change_selects_release_verification(
    version: str, expected: str, tmp_path: Path
) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.name", "CI test")
    git("config", "user.email", "ci@example.invalid")
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text('[workspace.package]\nversion = "0.1.0"\n', encoding="utf-8")
    git("add", "Cargo.toml")
    git("commit", "-m", "base")
    manifest.write_text(
        f'[workspace.package]\nversion = "{version}"\nedition = "2024"\n', encoding="utf-8"
    )
    git("commit", "-am", "update workspace")

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "HEAD~1"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = dict(line.split("=", 1) for line in completed.stdout.splitlines())
    assert result["scope"] == "rust"
    assert result["check_release"] == expected


def test_renaming_runtime_code_into_docs_keeps_the_wheel_check(tmp_path: Path) -> None:
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
        "scope": "code",
        "check_docs": "true",
        "check_release": "false",
        "check_corpus": "false",
    }
