from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from benchmarks import datasette_mutation
from benchmarks.datasette_mutation import (
    _function_scope,
    _require_successful_target,
)
from benchmarks.project_manifest import manifest


def _record(path: Path, start: int, end: int) -> tuple[object, ...]:
    values: list[object] = ["key", str(path), "digest", "kind", "operator", "old", "new"]
    values.extend((start, end))
    return tuple(values)


def test_datasette_scope_uses_utf8_byte_ranges_and_excludes_adjacent_functions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "utils.py"
    source.write_text(
        "prefix = 'é'\n\n"
        "def escape_sqlite(value):\n"
        "    return value\n\n"
        "def other(value):\n"
        "    return value\n",
        encoding="utf-8",
    )

    scope = _function_scope(source, "escape_sqlite")

    inside = _record(source, scope.start_byte, scope.end_byte - 1)
    adjacent = _record(source, scope.end_byte, scope.end_byte + 1)
    wrong_file = _record(tmp_path / "other.py", scope.start_byte, scope.end_byte - 1)

    assert scope.contains(inside)
    assert not scope.contains(adjacent)
    assert not scope.contains(wrong_file)


def test_direct_fixture_requires_a_successful_non_skipped_call() -> None:
    target = "tests/test_utils.py::test_escape_sqlite"
    records = [
        ("::test_escape_sqlite[plain-plain]", "setup", "passed", None),
        ("::test_escape_sqlite[plain-plain]", "call", "passed", None),
        ("::test_escape_sqlite[plain-plain]", "teardown", "passed", None),
    ]

    assert _require_successful_target(records, target) == records

    with pytest.raises(RuntimeError, match="skipped outcome"):
        _require_successful_target(
            [
                (target, "setup", "skipped", None),
            ],
            target,
        )


def test_prepare_datasette_fixture_runs_real_upstream_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the public fixture boundary on a disposable Datasette-shaped checkout."""

    prepared = tmp_path / "prepared"
    root = prepared / "project"
    source = root / "datasette" / "utils" / "__init__.py"
    tests = root / "tests" / "test_utils.py"
    source.parent.mkdir(parents=True)
    tests.parent.mkdir(parents=True)
    (root / "datasette" / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "benchmarks").mkdir()
    (root / "benchmarks" / "__init__.py").write_text(
        "raise RuntimeError('project package imported')\n", encoding="utf-8"
    )
    source.write_text(
        """def escape_sqlite(value):
    return '"{}"'.format(value.replace('"', '""'))
""",
        encoding="utf-8",
    )
    expected = repr('"O""Reilly"')
    tests.write_text(
        "from datasette.utils import escape_sqlite\n\n"
        "def test_escape_sqlite():\n"
        f"    assert escape_sqlite('O\"Reilly') == {expected}\n",
        encoding="utf-8",
    )
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts = -p no:cacheprovider\n",
        encoding="utf-8",
    )
    for command in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "benchmark@example.invalid"],
        ["git", "config", "user.name", "Benchmark Fixture"],
        ["git", "add", "."],
        ["git", "commit", "--quiet", "-m", "fixture"],
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()

    project = replace(manifest("datasette"), revision=revision, plugins=(), environment=())
    monkeypatch.setattr(datasette_mutation, "manifest", lambda _name: project)
    monkeypatch.setattr(
        datasette_mutation,
        "validate_prepared_root",
        lambda selected, checkout, selected_python, preparation_manifest, artifacts: {
            "manifest": str(preparation_manifest),
            "project": selected.key,
            "root": str(checkout),
            "python": str(selected_python),
            "artifacts": str(artifacts),
        },
    )
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    artifacts = prepared / "artifacts"
    fixture = datasette_mutation.prepare_datasette_fixture(
        root,
        Path(sys.executable),
        prepared / "manifest.json",
        artifacts,
    )

    assert fixture.project is project
    assert fixture.scope.function == "escape_sqlite"
    assert fixture.scope.test_targets == ("tests/test_utils.py::test_escape_sqlite",)
    assert fixture.source_files == (source.resolve(),)
    evidence = artifacts / "datasette-mutation-fixture.json"
    assert evidence.is_file()
    assert len(fixture.filter_records([])) == 0
