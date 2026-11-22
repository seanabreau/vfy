from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from coverage import CoverageData as ReferenceData

from tests.support import VfyProject
from vfy import Coverage as VfyCoverage, CoverageData
from vfy.errors import DataError, NoDataError


def _line_shard(path: Path, source: str | Path, lines: set[int]) -> None:
    data = ReferenceData(str(path))
    data.add_lines({str(source): set(lines)})
    data.write()


def _arc_shard(path: Path, source: str | Path, arcs: set[tuple[int, int]]) -> None:
    data = ReferenceData(str(path))
    data.add_arcs({str(source): set(arcs)})
    data.write()


def _malformed_semantic_shard(path: Path, source: str | Path) -> None:
    _line_shard(path, source, {1})
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE vfy_decision (key text primary key, path text, line integer,
                end_line integer, kind text, source_hash text, expression text, logic text,
                condition_lines text);
            CREATE TABLE vfy_observation (context text, decision text, vector text,
                outcome integer);
            INSERT INTO vfy_decision VALUES
                ('huge', 'example.py', 1, 1, 'if', 'hash', 'x', 'c999999999999999999999', '1');
            """
        )


def test_read_rejects_resource_exhausting_semantic_logic(tmp_path: Path) -> None:
    malformed = tmp_path / ".coverage"
    _malformed_semantic_shard(malformed, "example.py")

    with pytest.raises(DataError, match=r"semantic|corrupt|Invalid"):
        CoverageData(str(malformed)).read()


def test_combine_rejects_malformed_semantics_without_replacing_destination(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".coverage"
    original = b"destination remains byte-for-byte unchanged"
    output.write_bytes(original)
    shard = tmp_path / ".coverage.worker"
    _malformed_semantic_shard(shard, "example.py")

    with pytest.raises(DataError):
        CoverageData(str(output)).combine([str(tmp_path)])

    assert output.read_bytes() == original
    assert shard.exists()


def test_combine_rejects_dangling_tracer_without_replacing_destination(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".coverage"
    original = b"destination remains byte-for-byte unchanged"
    output.write_bytes(original)
    shard = tmp_path / ".coverage.worker"
    _line_shard(shard, "example.py", {1})
    with closing(sqlite3.connect(shard)) as connection:
        connection.execute("INSERT INTO tracer(file_id, tracer) VALUES (999, 'plugin.name')")
        connection.commit()

    with pytest.raises(DataError, match=r"tracer references absent file id 999"):
        CoverageData(str(output)).combine([str(tmp_path)])

    assert output.read_bytes() == original
    assert shard.exists()


def test_combine_discovers_merges_and_cleans_shards_in_one_call(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    output = tmp_path / ".coverage"
    first = tmp_path / ".coverage.first"
    second = tmp_path / ".coverage.second"
    _line_shard(first, source, {1, 2})
    _line_shard(second, source, {2, 3})

    data = CoverageData(str(output))
    data.combine([str(tmp_path)])

    assert data.lines(str(source)) == [1, 2, 3]
    assert output.exists()
    assert not first.exists()
    assert not second.exists()
    reference = ReferenceData(str(output))
    reference.read()
    assert reference.lines(str(source)) == [1, 2, 3]


def test_combine_validates_every_shard_before_mutating_output_or_cleanup(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    output = tmp_path / ".coverage"
    output.write_bytes(b"existing-output")
    valid = tmp_path / ".coverage.a"
    invalid = tmp_path / ".coverage.b"
    _line_shard(valid, source, {1})
    invalid.write_bytes(b"not sqlite")

    data = CoverageData(str(output))
    with pytest.raises(DataError):
        data.combine([str(tmp_path)])

    assert output.read_bytes() == b"existing-output"
    assert valid.exists()
    assert invalid.exists()


def test_combine_rejects_mixed_modes_before_mutating_any_file(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    output = tmp_path / ".coverage"
    output.write_bytes(b"existing-output")
    lines = tmp_path / ".coverage.lines"
    arcs = tmp_path / ".coverage.arcs"
    _line_shard(lines, source, {1})
    _arc_shard(arcs, source, {(-1, 1), (1, -1)})

    data = CoverageData(str(output))
    with pytest.raises(DataError, match="line and branch"):
        data.combine([str(tmp_path)])

    assert output.read_bytes() == b"existing-output"
    assert lines.exists()
    assert arcs.exists()


def test_combine_preserves_unsaved_in_memory_data_and_keep(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    output = tmp_path / ".coverage"
    shard = tmp_path / ".coverage.worker"
    _line_shard(shard, source, {2})
    data = CoverageData(str(output))
    data.add_lines({str(source): {1}})

    data.combine([str(tmp_path)], keep=True)

    assert data.lines(str(source)) == [1, 2]
    assert shard.exists()
    reference = ReferenceData(str(output))
    reference.read()
    assert reference.lines(str(source)) == [1, 2]


def test_combine_path_aliases_match_coverage_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    remote = "/workers/linux/checkout/src/example.py"
    config = tmp_path / ".coveragerc"
    config.write_text(
        "[paths]\nsource =\n    src\n    /workers/*/checkout/src\n",
        encoding="utf-8",
    )

    vfy_output = tmp_path / ".coverage-vfy"
    _line_shard(tmp_path / ".coverage-vfy.worker", remote, {1})
    actual = VfyCoverage(data_file=str(vfy_output), config_file=str(config))
    actual.combine([str(tmp_path)])

    reference_output = tmp_path / ".coverage-reference"
    _line_shard(tmp_path / ".coverage-reference.worker", remote, {1})
    reference = __import__("coverage").Coverage(
        data_file=str(reference_output), config_file=str(config)
    )
    reference.combine([str(tmp_path)])

    assert actual.get_data().measured_files() == {source.as_posix()}
    assert actual.get_data().lines(source.as_posix()) == [1]
    assert reference.get_data().measured_files() == {str(source)}


def test_combine_rejects_invalid_alias_before_mutation(tmp_path: Path) -> None:
    output = tmp_path / ".coverage"
    output.write_bytes(b"existing-output")
    shard = tmp_path / ".coverage.worker"
    source = str(tmp_path / "example.py")
    _line_shard(shard, source, {1})
    original_shard = shard.read_bytes()
    config = tmp_path / ".coveragerc"
    config.write_text("[paths]\nsource =\n    src\n    [invalid\n", encoding="utf-8")
    coverage = VfyCoverage(data_file=str(output), config_file=str(config))
    coverage.get_data().add_lines({source: {2}})

    with pytest.raises(DataError, match="invalid path alias pattern"):
        coverage.combine([str(tmp_path)])

    assert output.read_bytes() == b"existing-output"
    assert shard.read_bytes() == original_shard
    assert coverage.get_data().lines(source) == [2]


def test_combine_reports_an_explicit_missing_path_as_no_data(tmp_path: Path) -> None:
    data = CoverageData(str(tmp_path / ".coverage"))

    with pytest.raises(NoDataError, match="non-existent path"):
        data.combine([str(tmp_path / "missing")])


def test_report_output_creates_parents_and_replaces_atomically(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("example.py", "VALUE = 1\n")
    _line_shard(tmp_path / ".coverage", tmp_path / "example.py", {1})
    destination = tmp_path / "reports" / "coverage.json"

    first = project.run_vfy("coverage", "json", "-o", str(destination))
    destination.write_text("stale", encoding="utf-8")
    second = project.run_vfy("coverage", "json", "-o", str(destination))

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert json.loads(destination.read_text(encoding="utf-8"))["files"]
    assert list(destination.parent.iterdir()) == [destination]


def test_report_output_does_not_replace_target_when_staging_fails(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("example.py", "VALUE = 1\n")
    _line_shard(tmp_path / ".coverage", tmp_path / "example.py", {1})
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("unchanged", encoding="utf-8")

    result = project.run_vfy("coverage", "json", "-o", str(parent_file / "coverage.json"))

    assert result.returncode == 1
    assert "failed to write report output" in result.stderr
    assert parent_file.read_text(encoding="utf-8") == "unchanged"
