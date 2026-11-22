"""Line data persists as Coverage.py's packed numbits and merges by union."""

import random
import sqlite3
from pathlib import Path

from coverage import CoverageData as OracleData

from tests.support.coverage_data import DataFileFactory
from vfy import CoverageData as VfyData


def _line_bits(path: Path) -> list[tuple[str, str, bytes]]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT file.path, context.context, line_bits.numbits FROM line_bits "
            "JOIN file ON file.id = line_bits.file_id "
            "JOIN context ON context.id = line_bits.context_id "
            "ORDER BY file.path, context.context"
        ).fetchall()
    connection.close()
    return [(str(path), str(context), bytes(numbits)) for path, context, numbits in rows]


def _write(cls: DataFileFactory, path: Path, context: str, lines: dict[str, set[int]]) -> None:
    data = cls(str(path))
    data.set_context(context)
    data.add_lines({name: set(values) for name, values in lines.items()})
    data.write()


def test_numbits_encoding_matches_coverage_py(tmp_path: Path) -> None:
    randomizer = random.Random(1729)
    for round_index in range(20):
        lines = {
            f"file_{index}.py": {
                randomizer.randrange(0, 20_000) for _ in range(randomizer.randrange(1, 500))
            }
            for index in range(5)
        }
        paths = {
            "oracle": tmp_path / f"oracle-{round_index}.db",
            "vfy": tmp_path / f"vfy-{round_index}.db",
        }
        _write(OracleData, paths["oracle"], "test", lines)
        _write(VfyData, paths["vfy"], "test", lines)
        assert _line_bits(paths["vfy"]) == _line_bits(paths["oracle"])


def _union(cls: DataFileFactory, directory: Path) -> list[tuple[str, str, bytes]]:
    groups = [{1, 7, 8}, {7, 42, 1000}, {0, 2000}]
    target = cls(str(directory / "merged.db"))
    for index, group in enumerate(groups):
        shard = directory / f"shard-{index}.db"
        _write(cls, shard, "test", {"a.py": group})
        source = cls(str(shard))
        source.read()
        target.update(source)
    target.write()
    return _line_bits(directory / "merged.db")


def test_numbits_union_matches_coverage_py(tmp_path: Path) -> None:
    (tmp_path / "oracle").mkdir()
    (tmp_path / "vfy").mkdir()
    assert _union(VfyData, tmp_path / "vfy") == _union(OracleData, tmp_path / "oracle")
