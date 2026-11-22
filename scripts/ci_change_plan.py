#!/usr/bin/env python3
"""Classify changes into conservative CI tiers.

The classifier intentionally treats unknown files as platform-sensitive. It is
used by CI only; local development can pass explicit paths to inspect a plan.
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
FULL_MATRIX_PATHS = (
    ".github/",
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain",
    "rust-toolchain.toml",
    "crates/vfy/Cargo.toml",
    "crates/vfy-core/Cargo.toml",
    "crates/vfy/src/",
    "pyproject.toml",
    "uv.lock",
)
FULL_MATRIX_PYTHON_PATHS = (
    "python/vfy/bootstrap/",
    "python/vfy/collector.py",
    "python/vfy/native.py",
    "python/vfy/process_startup.py",
    "python/vfy/pytest_plugin.py",
    "python/vfy/pytest_reporting.py",
    "python/vfy/run/",
)
FULL_MATRIX_TEST_PATHS = (
    "tests/integration/test_api.py",
    "tests/integration/test_rust_transactions.py",
    "tests/integration/test_plugin.py",
    "tests/integration/test_pytest_cov_compat.py",
    "tests/integration/test_native_pytest_support.py",
)
KNOWN_CODE_PATHS = (
    "benchmarks/",
    "crates/vfy-core/src/",
    "python/vfy/",
    "tests/",
)


@dataclass(frozen=True)
class ChangePlan:
    """The CI jobs required for one set of repository paths."""

    scope: str
    run_quality: bool
    full_matrix: bool


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        path.startswith(pattern) if pattern.endswith("/") else path == pattern
        for pattern in patterns
    )


def _is_documentation(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return _matches(normalized, DOC_PATHS)


def _is_platform_sensitive(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return _matches(
        normalized, (*FULL_MATRIX_PATHS, *FULL_MATRIX_PYTHON_PATHS, *FULL_MATRIX_TEST_PATHS)
    )


def _is_known_code(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return _matches(normalized, KNOWN_CODE_PATHS)


def classify(paths: list[str]) -> ChangePlan:
    """Return a conservative tier for changed repository paths."""

    normalized = [path.replace("\\", "/") for path in paths if path]
    if not normalized:
        return ChangePlan("unknown", True, True)
    non_documentation = [path for path in normalized if not _is_documentation(path)]
    if not non_documentation:
        return ChangePlan("documentation", False, False)
    if any(
        not _is_platform_sensitive(path) and not _is_known_code(path) for path in non_documentation
    ):
        return ChangePlan("unknown", True, True)
    if any(_is_platform_sensitive(path) for path in non_documentation):
        return ChangePlan("platform", True, True)
    if all(path.startswith(("tests/", "benchmarks/")) for path in non_documentation):
        return ChangePlan("tests", True, False)
    return ChangePlan("code", True, False)


def changed_paths(base: str | None) -> list[str]:
    """Read changed paths from Git; an empty result requests the full CI matrix."""

    if base:
        completed = subprocess.run(
            # Classify both ends of a rename, including code moved into docs.
            ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return [path for path in completed.stdout.split("\0") if path]
    return []


def _write_outputs(plan: ChangePlan, output: TextIO) -> None:
    print(f"scope={plan.scope}", file=output)
    print(f"run_quality={str(plan.run_quality).lower()}", file=output)
    print(f"full_matrix={str(plan.full_matrix).lower()}", file=output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Git base revision to diff against")
    parser.add_argument("--path", action="append", default=[], help="Changed path (repeatable)")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    paths = args.path or changed_paths(args.base)
    plan = classify(paths)
    if args.github_output is None:
        _write_outputs(plan, sys.stdout)
    else:
        with args.github_output.open("a", encoding="utf-8") as output:
            _write_outputs(plan, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
