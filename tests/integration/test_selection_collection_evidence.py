"""Replay keeps coverage recorded only while collecting a skipped test module."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject


@dataclass(frozen=True)
class _FileEvidence:
    lines: tuple[int, ...]
    arcs: tuple[tuple[int, int], ...]
    contexts: dict[int, tuple[str, ...]]


def _read_evidence(project: VfyProject, filename: str) -> dict[str, _FileEvidence]:
    data = project.data(filename)
    try:
        return {
            path: _FileEvidence(
                lines=tuple(sorted(data.lines(path) or [])),
                arcs=tuple(sorted(data.arcs(path) or [])),
                contexts={
                    line: tuple(sorted(contexts))
                    for line, contexts in sorted(data.contexts_by_lineno(path).items())
                },
            )
            for path in sorted(data.measured_files())
        }
    finally:
        data.close(force=True)


@pytest.mark.skipif(sys.platform == "win32", reason="native execution requires Linux or macOS")
@pytest.mark.parametrize(
    ("before_body", "after_body", "baseline_helper_line", "edited_helper_line"),
    [
        ("    return 1\n", "    return int('1')\n", 6, 6),
        ("    return 1\n", "    value = 1\n    return value\n", 6, 7),
        ("    value = 1\n    return value\n", "    return 1\n", 7, 6),
    ],
    ids=["body-edit", "insert", "delete"],
)
def test_replay_preserves_collection_only_hits_from_a_skipped_module(
    tmp_path: Path,
    before_body: str,
    after_body: str,
    baseline_helper_line: int,
    edited_helper_line: int,
) -> None:
    """Editing one body preserves global hits from an unchanged, uncollected body."""

    project = VfyProject(tmp_path)
    project.write(
        ".gitignore",
        ".coverage*\n.vfy/\n.collected\n.ran_*\n.pytest_cache/\n__pycache__/\n",
    )
    project.write("pytest.ini", "[pytest]\ntestpaths = tests\n")
    project.write("pkg/__init__.py", "")
    source = project.write(
        "pkg/model.py",
        f"def changed():\n{before_body}\n\ndef collection_only():\n    return 7\n",
    )
    project.write(
        "tests/test_collection.py",
        "from pathlib import Path\n"
        "from pkg.model import collection_only\n\n"
        "assert collection_only() == 7\n"
        "Path('.collected').write_text('collected\\n')\n\n"
        "def test_unrelated():\n"
        "    Path('.ran_unrelated').write_text('ran\\n')\n",
    )
    project.write(
        "tests/test_changed.py",
        "from pathlib import Path\n"
        "from pkg.model import changed\n\n"
        "def test_changed():\n"
        "    Path('.ran_changed').write_text('ran\\n')\n"
        "    assert changed() == 1\n",
    )
    project.git_init()
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": None}
    coverage_arguments = (
        "--cov=pkg",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=json:.vfy/report.json",
    )
    vfy_arguments = (
        "test",
        "--engine=pytest-native",
        "-j",
        "1",
        "--source",
        "pkg",
        "-q",
        *coverage_arguments,
    )

    baseline_run = project.run_vfy("test", "--all", *vfy_arguments[1:], env=environment)
    baseline_run.expect(0)
    baseline = _read_evidence(project, ".coverage")
    assert baseline[str(source)].contexts[baseline_helper_line] == ("",)
    assert (tmp_path / ".collected").is_file()
    assert (tmp_path / ".ran_changed").is_file()
    assert (tmp_path / ".ran_unrelated").is_file()

    (tmp_path / ".collected").unlink()
    (tmp_path / ".ran_changed").unlink()
    (tmp_path / ".ran_unrelated").unlink()
    project.write(
        "pkg/model.py", source.read_text(encoding="utf-8").replace(before_body, after_body)
    )

    selected_run = project.run_vfy(*vfy_arguments, env=environment)
    selected_run.expect(0)
    assert "Selected 1 / 2 tests" in selected_run.stderr
    assert (tmp_path / ".ran_changed").is_file()
    assert not (tmp_path / ".ran_unrelated").exists()
    assert not (tmp_path / ".collected").exists()
    replayed = _read_evidence(project, ".coverage")

    stock_arguments = ("-m", "pytest", "-q", "-p", "pytest_cov.plugin", *coverage_arguments)
    full_run = project.run_python(
        *stock_arguments,
        env={**environment, "COVERAGE_FILE": str(tmp_path / ".vfy/oracle-full.coverage")},
    )
    full_run.expect(0)
    full = _read_evidence(project, ".vfy/oracle-full.coverage")
    assert full[str(source)].contexts[edited_helper_line] == ("",)

    nodeid = "tests/test_changed.py::test_changed"
    selected_oracle_run = project.run_python(
        *stock_arguments,
        nodeid,
        env={**environment, "COVERAGE_FILE": str(tmp_path / ".vfy/oracle-selected.coverage")},
    )
    selected_oracle_run.expect(0)
    selected_oracle = _read_evidence(project, ".vfy/oracle-selected.coverage")
    fresh_run = project.run_vfy(
        "test",
        "--no-replay",
        "--data-file",
        ".vfy/fresh-selected.coverage",
        *vfy_arguments[1:],
        nodeid,
        env=environment,
    )
    fresh_run.expect(0)
    fresh = _read_evidence(project, ".vfy/fresh-selected.coverage")
    assert fresh == selected_oracle
    assert edited_helper_line not in fresh[str(source)].lines

    assert replayed == full
