from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from coverage import Coverage as ReferenceCoverage

from vfy import CoverageData
from vfy.errors import DataError


@pytest.mark.parametrize(
    ("branch", "damage"),
    [
        (False, "INSERT INTO arc VALUES (1, 1, 1, 2)"),
        (True, "INSERT INTO line_bits VALUES (1, 1, X'02')"),
        (False, "UPDATE meta SET value = 'bad' WHERE key = 'has_arcs'"),
        (False, "DELETE FROM meta WHERE key = 'has_arcs'"),
    ],
)
def test_invalid_shard_preserves_output_and_every_input(
    tmp_path: Path, branch: bool, damage: str
) -> None:
    output = tmp_path / ".coverage"
    shard = tmp_path / ".coverage.worker"
    other = tmp_path / ".coverage.other"
    for path in (output, shard, other):
        data = CoverageData(str(path))
        if branch:
            data.add_arcs({"example.py": {(1, 2)}})
        else:
            data.add_lines({"example.py": {1}})
        data.write()
    with closing(sqlite3.connect(shard)) as connection, connection:
        connection.execute(damage)
    before = {path: path.read_bytes() for path in (output, shard, other)}
    data = CoverageData(str(output))
    with pytest.raises(DataError, match="has_arcs"):
        data.combine([str(tmp_path)])
    assert {path: path.read_bytes() for path in before} == before
    with pytest.raises(DataError, match="has_arcs"):
        CoverageData(str(shard)).read()
    with pytest.raises(DataError, match="has_arcs"):
        CoverageData(no_disk=True).loads(b"vfy-data\0" + shard.read_bytes())


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks may require privileges on Windows")
@pytest.mark.parametrize("keep", [False, True])
def test_directory_symlink_shard_matches_coverage_and_preserves_target(
    tmp_path: Path, keep: bool
) -> None:
    target = tmp_path / "source-data"
    source = CoverageData(str(target))
    source.add_lines({"example.py": {7}})
    source.write()
    original = target.read_bytes()
    snapshots = []
    for engine in ("vfy", "coverage"):
        directory = tmp_path / engine
        directory.mkdir()
        link = directory / ".coverage.worker"
        link.symlink_to(target)
        output = directory / ".coverage"
        if engine == "vfy":
            data = CoverageData(str(output))
            data.combine([str(directory)], keep=keep)
        else:
            coverage = ReferenceCoverage(data_file=str(output))
            coverage.combine([str(directory)], keep=keep)
            data = coverage.get_data()
        snapshots.append((data.measured_files(), data.lines("example.py"), link.is_symlink()))
        assert target.read_bytes() == original
    assert snapshots == [({"example.py"}, [7], keep)] * 2


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks may require privileges on Windows")
def test_directory_discovery_does_not_cleanup_a_link_to_output(tmp_path: Path) -> None:
    output = tmp_path / ".coverage"
    data = CoverageData(str(output))
    data.add_lines({"example.py": {1}})
    data.write()
    link = tmp_path / ".coverage.alias"
    link.symlink_to(output)
    data.combine([str(tmp_path)])
    assert link.is_symlink()
    assert data.lines("example.py") == [1]
