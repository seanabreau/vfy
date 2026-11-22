"""Public update and alias mapping preserve mixed live and persisted semantic evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize("disk_first", [False, True], ids=["live-first", "disk-first"])
@pytest.mark.parametrize("aliases", [False, True], ids=["same-path", "aliased-paths"])
@pytest.mark.parametrize(
    "missing_conditions", [False, True], ids=["stored-ranges", "unknown-ranges"]
)
def test_semantic_update_merges_live_and_disk_evidence(
    tmp_path: Path, disk_first: bool, aliases: bool, missing_conditions: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "run.py",
        """\
import json
import sqlite3
import sys
from contextlib import ExitStack, closing
from pathlib import Path

from vfy import Coverage, CoverageData

disk_first, aliases, missing_conditions = (value == "True" for value in sys.argv[1:])
source = "value = left or right\\nif left and right:\\n    pass\\n"
first_path = Path("first.py").resolve()
second_path = Path("second.py").resolve() if aliases else first_path
target_path = Path("target.py").resolve()
for path in (first_path, second_path, target_path):
    path.write_text(source, encoding="utf-8")

def collect(path, data_file, flag):
    coverage = Coverage(config_file=False, data_file=data_file, decisions=True)
    coverage.start()
    try:
        exec(compile(source, str(path), "exec"), {"left": flag, "right": flag})
    finally:
        coverage.stop()
    return coverage

with ExitStack() as resources:
    first = collect(first_path, ".first", False)
    second = collect(second_path, ".second", True)
    disk, live = (first, second) if disk_first else (second, first)
    disk.save()
    if missing_conditions:
        with closing(sqlite3.connect(disk.get_data().data_filename())) as connection, connection:
            connection.execute("DROP TABLE vfy_condition")
    loaded = resources.enter_context(closing(CoverageData(disk.get_data().data_filename())))
    loaded.read()
    combined, incoming = (loaded, live.get_data()) if disk_first else (live.get_data(), loaded)
    combined.update(incoming)
    target = resources.enter_context(closing(CoverageData(".combined")))
    target.update(combined, map_path=lambda path: str(target_path))

    def snapshot(data_file):
        reader = Coverage(config_file=False, data_file=data_file, decisions=True)
        reader.load()
        semantic = reader.get_decision_data()
        report = reader.decision_report()
        data = reader.get_data()
        return {
            "paths": sorted(data.measured_files()),
            "boolean_definitions": len(semantic.definitions),
            "condition_ranges": [
                [item.start_line, item.start_column, item.end_line, item.end_column]
                for item in semantic.definitions[0].condition_ranges
            ],
            "path_definitions": len(semantic.path_definitions),
            "boolean_observations": sorted(
                (item.vector, item.outcome) for item in semantic.observations
            ),
            "path_observations": sorted(item.outcome for item in semantic.path_observations),
            "decision_coverage": [report.decisions_covered, report.decisions_total],
        }

    target.write()
    print(json.dumps(snapshot(".combined")))
""",
    )

    result = project.run_python("run.py", str(disk_first), str(aliases), str(missing_conditions))

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert json.loads(result.stdout) == {
        "paths": [str(tmp_path / "target.py")],
        "boolean_definitions": 1,
        "condition_ranges": [[2, 3, 2, 7], [2, 12, 2, 17]],
        "path_definitions": 1,
        "boolean_observations": [["0-", False], ["11", True]],
        "path_observations": [0, 1],
        "decision_coverage": [4, 4],
    }


def test_semantic_alias_range_conflict_preserves_saved_destination(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conflict.py",
        """\
import json
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

from vfy import Coverage, CoverageData
from vfy.errors import DataError

source = "if left and right:\\n    pass\\n"
paths = [Path(name).resolve() for name in ("first.py", "second.py", "target.py")]
for path in paths:
    path.write_text(source, encoding="utf-8")

def collect(path, data_file, flag):
    coverage = Coverage(config_file=False, data_file=data_file, decisions=True)
    coverage.start()
    try:
        exec(compile(source, str(path), "exec"), {"left": flag, "right": flag})
    finally:
        coverage.stop()
    coverage.save()
    return coverage.get_data()

with ExitStack() as resources:
    first = collect(paths[0], ".first", False)
    collect(paths[1], ".second", True)
    with closing(sqlite3.connect(".second")) as connection, connection:
        connection.execute(
            "UPDATE vfy_condition SET start_col = start_col + 1 WHERE index_no = 0"
        )
    second = resources.enter_context(closing(CoverageData(".second")))
    second.read()
    first.update(second)
    destination = collect(paths[2], ".destination", False)
    before_memory = destination.dumps()
    before_file = Path(".destination").read_bytes()
    reader = Coverage(config_file=False, data_file=".destination", decisions=True)
    reader.load()
    before_semantic = reader.get_decision_data()
    try:
        destination.update(first, map_path=lambda path: str(paths[2]))
    except DataError as error:
        assert str(error) == "conflicting semantic definitions", error
    else:
        raise AssertionError("accepted conflicting aliased condition ranges")
    assert destination.dumps() == before_memory
    assert Path(".destination").read_bytes() == before_file
    destination.write()
    restored = resources.enter_context(closing(CoverageData(".destination")))
    restored.read()
    reloaded = Coverage(config_file=False, data_file=".destination", decisions=True)
    reloaded.load()
    restored_semantic = reloaded.get_decision_data()
    assert restored_semantic.definitions == before_semantic.definitions
    assert restored_semantic.observations == before_semantic.observations
    assert restored.lines(str(paths[2])) == destination.lines(str(paths[2]))
    print(json.dumps({"rejected": True, "unchanged": True}))
""",
    )
    result = project.run_python("conflict.py")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert json.loads(result.stdout) == {"rejected": True, "unchanged": True}
