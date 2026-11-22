"""Legacy path predicates through the public independent runner."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_legacy_isdir_follows_filesystem_types_and_symlinks(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_paths.py",
        """import os
def test_predicate(tmpdir):
    assert tmpdir.isdir() is True
    file = tmpdir.join('file')
    file.write('contents')
    assert file.isdir() is False
    missing = tmpdir.join('missing')
    assert missing.isdir() is False
    directory_link = tmpdir.join('directory-link')
    os.symlink(tmpdir, directory_link, target_is_directory=True)
    assert directory_link.isdir() is True
    broken_link = tmpdir.join('broken-link')
    os.symlink(missing, broken_link)
    assert broken_link.isdir() is False
""",
    )
    stock = project.run_python("-m", "pytest", "-q")
    stock.expect(0)
    candidate = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-p", "vfy.independent.outcomes"
    )
    candidate.expect(0)
