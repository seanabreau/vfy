"""Rewritten test modules reuse a vfy-tagged bytecode cache keyed by the source stat."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_TIMING = re.compile(r"in \d+\.\d+s")


def _module(expected: int) -> str:
    # The expected value changes width only through the digit, keeping the byte size fixed.
    return f"""def compute(item):
    return item * 2

def test_value():
    assert compute(2) == {expected}
"""


def _run(project: VfyProject, *arguments: str, env: dict[str, str] | None = None) -> CommandResult:
    return project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "-p",
        "no:cacheprovider",
        *arguments,
        env=env,
    )


def _output(result: CommandResult) -> str:
    return result.stdout + result.stderr


def test_cache_entry_is_written_once_and_reused_until_the_source_changes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_cache.py", _module(5))
    cache_dir = project.root / "__pycache__"

    first = _run(project, "test_cache.py")
    assert first.returncode == 1, _output(first)
    assert "assert 4 == 5" in _output(first)
    (cache,) = cache_dir.glob("test_cache.*-vfy-*.pyc")
    entry = cache.stat().st_ino

    second = _run(project, "test_cache.py")
    assert second.returncode == 1, _output(second)
    # Every write replaces the entry with a new inode, so an unchanged inode means a hit.
    assert cache.stat().st_ino == entry

    project.write("test_cache.py", _module(6))
    third = _run(project, "test_cache.py")
    assert third.returncode == 1, _output(third)
    assert "assert 4 == 6" in _output(third)
    assert cache.stat().st_ino != entry


def test_plain_assertion_mode_does_not_reuse_rewritten_entries(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_plain.py", _module(5))
    rewritten = _run(project, "test_plain.py")
    assert "assert 4 == 5" in _output(rewritten)

    plain = _run(project, "test_plain.py", "--assert=plain")
    assert plain.returncode == 1, _output(plain)
    assert "assert 4 == 5" not in _output(plain)
    assert "where 4 = compute(2)" not in _output(plain)
    assert "AssertionError" in _output(plain)


def test_failure_explanations_match_between_uncached_and_cached_runs(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_explanations.py",
        """class Owner:
    value = 3

def compute(item):
    return item * 2

def test_call():
    assert compute(2) == 5

def test_membership():
    items = [1, 2, 3]
    assert items[0] in [4, 5]

def test_attribute():
    assert not Owner.value

def test_sequence():
    assert sorted([1, 2, 3]) == [3, 2, 1]
""",
    )
    cache_dir = project.root / "__pycache__"

    uncached = _run(project, "test_explanations.py", env={"PYTHONDONTWRITEBYTECODE": "1"})
    assert not list(cache_dir.glob("test_explanations.*-vfy-*.pyc"))
    writing = _run(project, "test_explanations.py")
    assert list(cache_dir.glob("test_explanations.*-vfy-*.pyc"))
    cached = _run(project, "test_explanations.py")

    expected = _TIMING.sub("in <time>", _output(uncached))
    assert uncached.returncode == 1, _output(uncached)
    assert "assert 4 == 5" in expected
    assert "assert 1 in [4, 5]" in expected
    assert "assert not 3" in expected
    assert "assert [1, 2, 3] == [3, 2, 1]" in expected
    for result in (writing, cached):
        assert result.returncode == 1, _output(result)
        assert _TIMING.sub("in <time>", _output(result)) == expected


@pytest.mark.parametrize("blocked", ["read_only_directory", "occupied_entry"])
def test_unwritable_bytecode_cache_does_not_break_the_run(tmp_path: Path, blocked: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_blocked.py", _module(5))
    cache_dir = project.root / "__pycache__"
    assert _run(project, "test_blocked.py").returncode == 1
    (cache,) = cache_dir.glob("test_blocked.*-vfy-*.pyc")
    # The new source makes the entry stale, so the run must attempt a write.
    project.write("test_blocked.py", _module(6))
    if blocked == "read_only_directory":
        cache_dir.chmod(0o555)
    else:
        # A directory at the entry path fails both the read and the replace for every uid.
        cache.unlink()
        cache.mkdir()
    try:
        result = _run(project, "test_blocked.py")
    finally:
        if blocked == "read_only_directory":
            cache_dir.chmod(0o755)

    assert result.returncode == 1, _output(result)
    assert "assert 4 == 6" in _output(result)
    if blocked == "occupied_entry":
        assert cache.is_dir()
