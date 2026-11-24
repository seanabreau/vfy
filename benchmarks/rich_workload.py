"""Prepare the deterministic workload shared by the pinned Rich benchmarks."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


def install_rich_clock_fixture(root: Path) -> dict[str, str]:
    """Apply the same scoped logging clock to all three public test routes."""

    conftest = root / "tests/conftest.py"
    original = conftest.read_text(encoding="utf-8")
    if "freeze_rich_log_clock" in original:
        raise RuntimeError("Rich already defines the benchmark clock fixture")
    source = Path(__file__).resolve().parent / "rich_clock_plugin.py"
    fixture_source = source.read_text(encoding="utf-8")
    marker = "import logging\n"
    if fixture_source.count(marker) != 1:
        raise RuntimeError("Rich clock fixture source has changed unexpectedly")
    fixture = marker + fixture_source.split(marker, 1)[1]
    conftest.write_text(original.rstrip() + "\n\n" + fixture, encoding="utf-8")
    return {
        "path": "tests/conftest.py",
        "fixture_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "prepared_conftest_sha256": hashlib.sha256(conftest.read_bytes()).hexdigest(),
    }


def install_rich_deterministic_tests(root: Path) -> dict[str, object]:
    """Apply the reviewed upstream test stabilization to every route."""

    source = (
        Path(__file__).resolve().parents[1]
        / "validation/pytest_runner/patches/rich-deterministic-workload.patch"
    )
    patch = source.read_bytes()
    changed = ("tests/test_console.py", "tests/test_live.py", "tests/test_traceback.py")
    before = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in changed}
    subprocess.run(["git", "apply", "--check", str(source)], cwd=root, check=True)
    subprocess.run(["git", "apply", str(source)], cwd=root, check=True)
    after = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in changed}
    return {
        "source": str(source),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "before_sha256": before,
        "after_sha256": after,
    }


def commit_rich_test_adaptations(root: Path) -> str:
    """Give the shared benchmark fixtures a clean Git baseline for public selection."""

    paths = (
        "tests/conftest.py",
        "tests/test_console.py",
        "tests/test_live.py",
        "tests/test_traceback.py",
    )
    subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if staged != sorted(paths):
        raise RuntimeError(f"Rich benchmark fixture commit staged unexpected files: {staged!r}")
    environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-10-06T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-10-06T00:00:00+00:00",
    }
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=vfy Benchmark",
            "-c",
            "user.email=benchmark@vfy.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Apply deterministic Rich benchmark test fixtures",
        ],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    if status:
        raise RuntimeError(f"Rich benchmark fixture baseline is not clean: {status}")
    return head


def prepare_rich_workload(root: Path) -> tuple[dict[str, object], dict[str, str]]:
    """Apply and identify the test fixtures before collecting a baseline."""

    stabilization = install_rich_deterministic_tests(root)
    clock = install_rich_clock_fixture(root)
    stabilization["derived_commit"] = commit_rich_test_adaptations(root)
    return stabilization, clock
