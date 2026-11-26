#!/usr/bin/env python3
"""Select the additional checks required by a pull request.

Pass explicit paths to inspect a plan locally. Missing or unrecognized paths
require the Linux wheel, Rust, and documentation checks.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

DOC_PATHS = (
    "docs/",
    "overrides/",
    "AGENTS.md",
    "changelogs/",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "mkdocs.yml",
)
RUST_PATHS = (
    "crates/",
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain",
    "rust-toolchain.toml",
    "pyproject.toml",
    "uv.lock",
    "scripts/cargo-insta-test.sh",
    "scripts/cargo-nextest.sh",
    "scripts/cargo-test-env.sh",
    "scripts/cargo-test.sh",
)
KNOWN_CODE_PATHS = (
    ".github/",
    "benchmarks/",
    "examples/coverage/",
    "python/vfy/",
    "scripts/",
    "tests/",
    "validation/",
    "README.pypi.md",
)
DOC_BUILD_PATHS = (
    "docs/",
    "overrides/",
    ".github/workflows/docs.yml",
    "mkdocs.yml",
    "pyproject.toml",
    "uv.lock",
    "examples/coverage/",
    "scripts/check_tutorial_install.py",
)
RELEASE_PATHS = (
    ".github/workflows/release.yml",
    ".github/workflows/release-verify.yml",
    "benchmarks/benchmark_harness.py",
    "benchmarks/benchmark_provenance.py",
    "benchmarks/project_manifest.py",
    "scripts/changelog_section.py",
    "scripts/check_release_version.py",
    "scripts/normalize_release_sboms.py",
    "scripts/check_native_pytest_versions.sh",
    "scripts/check_native_pytest_mock_versions.sh",
    "scripts/process_tree.py",
    "validation/pytest_runner/adoption_observations.py",
    "validation/pytest_runner/check_native_adoption_matrix.py",
    "validation/pytest_runner/check_native_xdist_adoption.py",
    "validation/pytest_runner/native-adoption-requirements.in",
    "validation/pytest_runner/native-adoption-requirements.txt",
    "validation/pytest_runner/patches/",
    "validation/pytest_runner/real_packages/",
)
CORPUS_PATHS = (
    ".github/workflows/native-pytest.yml",
    "benchmarks/benchmark_harness.py",
    "benchmarks/benchmark_provenance.py",
    "benchmarks/locks/rich.txt",
    "benchmarks/prepare_projects.py",
    "benchmarks/project_manifest.py",
    "docs/about/pytest-test-identity.json",
    "docs/about/pytest-test-policy.json",
    "scripts/process_tree.py",
    "scripts/pytest_environment_lock.py",
)


@dataclass(frozen=True)
class ChangePlan:
    """The CI jobs required for one set of repository paths."""

    scope: str
    check_docs: bool
    check_release: bool = False
    check_corpus: bool = False


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        path.startswith(pattern) if pattern.endswith("/") else path == pattern
        for pattern in patterns
    )


def classify(paths: list[str]) -> ChangePlan:
    """Return the required pull request checks for changed repository paths."""

    normalized = [path.replace("\\", "/") for path in paths if path]
    if not normalized:
        return ChangePlan("unknown", True)
    check_release = any(_matches(path, RELEASE_PATHS) for path in normalized)
    check_corpus = any(
        _matches(path, CORPUS_PATHS)
        or path.startswith(("scripts/pytest_corpus_", "scripts/pytest_execution_"))
        or (path.startswith("validation/pytest_runner/") and not _matches(path, RELEASE_PATHS))
        for path in normalized
    )
    non_documentation = [
        path for path in normalized if not _matches(path, DOC_PATHS) or _matches(path, CORPUS_PATHS)
    ]
    if not non_documentation:
        return ChangePlan("documentation", True)
    if any(
        not _matches(path, (*RUST_PATHS, *KNOWN_CODE_PATHS, *CORPUS_PATHS))
        for path in non_documentation
    ):
        return ChangePlan("unknown", True, check_release, check_corpus)
    check_docs = any(_matches(path, DOC_BUILD_PATHS) for path in normalized)
    scope = "rust" if any(_matches(path, RUST_PATHS) for path in non_documentation) else "code"
    return ChangePlan(scope, check_docs, check_release, check_corpus)


def changed_paths(base: str | None) -> list[str]:
    """Read the diff from Git and reject an unavailable pull request base."""

    if base is None:
        return []
    if not base:
        raise ValueError("--base must name a Git revision")
    completed = subprocess.run(
        # Classify both ends of a rename, including code moved into docs.
        ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.strip() or "Git could not read the pull request diff")
    return [path for path in completed.stdout.split("\0") if path]


def _write_outputs(plan: ChangePlan, output: TextIO) -> None:
    print(f"scope={plan.scope}", file=output)
    print(f"check_docs={str(plan.check_docs).lower()}", file=output)
    print(f"check_release={str(plan.check_release).lower()}", file=output)
    print(f"check_corpus={str(plan.check_corpus).lower()}", file=output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Git base revision to diff against")
    parser.add_argument("--path", action="append", default=[], help="Changed path (repeatable)")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    try:
        paths = args.path or changed_paths(args.base)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    plan = classify(paths)
    if args.github_output is None:
        _write_outputs(plan, sys.stdout)
    else:
        with args.github_output.open("a", encoding="utf-8") as output:
            _write_outputs(plan, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
