"""Verify that a corpus recipe controls child file permissions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from validation.pytest_runner.real_packages import benchmark, corpus


@pytest.mark.skipif(os.name != "posix", reason="POSIX subprocess umask")
def test_corpus_and_benchmark_apply_the_same_child_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tmp_path / "src"
    project = sources / "package"
    project.mkdir(parents=True)
    parent_before = tmp_path / "parent-before"
    fd = os.open(parent_before, os.O_CREAT | os.O_WRONLY, 0o666)
    os.close(fd)
    (project / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
    (project / "test_mode.py").write_text(
        "import os\n"
        "def test_mode():\n"
        "    fd = os.open('created', os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o666)\n"
        "    os.close(fd)\n"
        "    assert os.stat('created').st_mode & 0o777 == 0o644\n"
    )
    monkeypatch.setattr(corpus, "SOURCES", sources)
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    entry = {"name": "package", "umask": "0022"}
    output = tmp_path / "corpus"
    output.mkdir()
    for route in ("pytest", "vfy"):
        (project / "created").unlink(missing_ok=True)
        result = corpus._execute(entry, route, output, bare=False, vfy_source=None)
        assert result["exit"] == 0, result
        assert result["counts"] == {"passed": 1}
        provenance = result["provenance"]
        assert isinstance(provenance, dict)
        assert provenance["umask"] == "0022"
        assert (project / "created").stat().st_mode & 0o777 == 0o644

    command = [
        sys.executable,
        "-c",
        "import os; fd=os.open('timed',os.O_CREAT|os.O_WRONLY,0o666); "
        "os.close(fd); print('1 passed in 0.1s')",
    ]
    benchmark._sample(
        "package",
        entry,
        project,
        project,
        tmp_path / "timings",
        "pytest",
        {"command": command, "env": dict(os.environ), "benchmark_table_guard": False},
        {"passed": 1},
        "sample",
        0,
    )
    assert (project / "timed").stat().st_mode & 0o777 == 0o644
    receipt = tmp_path / "timings/package/sample-00-pytest/command.json"
    assert json.loads(receipt.read_text())["umask"] == "0022"
    parent_after = tmp_path / "parent-after"
    fd = os.open(parent_after, os.O_CREAT | os.O_WRONLY, 0o666)
    os.close(fd)
    assert parent_after.stat().st_mode & 0o777 == parent_before.stat().st_mode & 0o777


@pytest.mark.parametrize("value", ["22", "0999", "-1", "00222", 0o22, None])
def test_invalid_umask_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="four-digit POSIX octal"):
        corpus._subprocess_umask({"umask": value})
