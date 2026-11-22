from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack, closing
from functools import partial
from pathlib import Path
from random import Random

import pytest
from coverage import CoverageData as ReferenceData
from coverage.exceptions import DataError as ReferenceDataError
from coverage.numbits import nums_to_numbits

from tests.support import VfyProject
from vfy import CoverageData
from vfy.errors import DataError

_MIXED_SHARD_SUBJECT = """def choose(flag):
    if flag:
        return 'parent'
    return 'child'
"""

_MIXED_SHARD_PROGRAM = """import importlib
import json
import runpy
import subprocess
import sys
from pathlib import Path

engine, role, root_text = sys.argv[1:]
root = Path(root_text)
Coverage = importlib.import_module(engine).Coverage
options = dict(config_file=str(root / 'pyproject.toml'),
               source=[str(root / 'src/pkg'), str(root / 'other')],
               data_file=str(root / ('.coverage.' + engine)), branch=True)

def snapshot(data):
    return {path: {'lines': sorted(data.lines(path) or []),
                   'arcs': sorted(data.arcs(path) or []),
                   'contexts': {str(line): sorted(values) for line, values in
                                sorted(data.contexts_by_lineno(path).items())}}
            for path in sorted(data.measured_files())}

cov = Coverage(data_suffix=True, context=role, **options)
cov.start()
try:
    subject = runpy.run_path(str(root / 'src/pkg/subject.py'))
    assert subject['choose'](role == 'parent') == role
    if role == 'parent':
        other = runpy.run_path(str(root / 'other/subject.py'))
        assert other['distinct']() == 'different file'
finally:
    cov.stop()
    cov.save()
data = cov.get_data()
(root / (engine + '-' + role + '.json')).write_text(json.dumps(snapshot(data)))
data.close(force=True)
if role == 'child':
    raise SystemExit(0)

away = root.parent / ('away-' + root.name)
away.mkdir(exist_ok=True)
child = subprocess.run([sys.executable, '-B', str(root / 'collect.py'),
                        engine, 'child', str(root)], cwd=away,
                       capture_output=True, text=True)
assert child.returncode == 0, child.stdout + child.stderr
combined = Coverage(**options)
combined.combine(strict=True)
combined.save()
data = combined.get_data()
before = snapshot(data)
data.close(force=True)
combined.json_report(outfile=str(root / (engine + '-report.json')))
reloaded = Coverage(**options)
reloaded.load()
data = reloaded.get_data()
assert snapshot(data) == before
data.close(force=True)
report = json.loads((root / (engine + '-report.json')).read_text())
(root / (engine + '-combined.json')).write_text(json.dumps({
    'data': before, 'files': report['files'], 'totals': report['totals']}))
"""


@pytest.mark.parametrize("relative_files", [False, True])
def test_configured_combine_maps_actual_parent_and_child_shards(
    tmp_path: Path, relative_files: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pyproject.toml",
        "[tool.coverage.run]\n"
        f"relative_files = {str(relative_files).lower()}\n"
        '[tool.coverage.paths]\npackage = ["src/pkg", "*/worker/src/pkg"]\n',
    )
    subject = project.write("src/pkg/subject.py", _MIXED_SHARD_SUBJECT)
    other = project.write("other/subject.py", "def distinct():\n    return 'different file'\n")
    assert subject.resolve() != other.resolve()
    project.write("collect.py", _MIXED_SHARD_PROGRAM)
    for engine in ("coverage", "vfy"):
        result = project.run_python("collect.py", engine, "parent", str(tmp_path))
        result.expect(0)
    expected = json.loads((tmp_path / "coverage-combined.json").read_text())
    actual = json.loads((tmp_path / "vfy-combined.json").read_text())
    assert actual == expected
    expected_paths = (
        {"src/pkg/subject.py", "other/subject.py"}
        if relative_files
        else {str(subject.resolve()), str(other.resolve())}
    )
    assert set(expected["data"]) == expected_paths
    parent = json.loads((tmp_path / "coverage-parent.json").read_text())
    child = json.loads((tmp_path / "coverage-child.json").read_text())
    assert json.loads((tmp_path / "vfy-parent.json").read_text()) == parent
    assert json.loads((tmp_path / "vfy-child.json").read_text()) == child
    assert set(parent) == expected_paths
    assert set(child) == {str(subject.resolve()), str(other.resolve())}


def _update_same_implementation(
    data: CoverageData | ReferenceData,
    other: CoverageData | ReferenceData,
    *,
    map_path: Callable[[str], str] | None = None,
) -> None:
    if isinstance(data, CoverageData):
        assert isinstance(other, CoverageData)
        data.update(other, map_path=map_path)
    else:
        assert isinstance(other, ReferenceData)
        data.update(other, map_path=map_path)


@pytest.mark.parametrize("branch", [False, True])
def test_purged_loaded_records_stay_empty_after_save(tmp_path: Path, branch: bool) -> None:
    results = []
    for implementation in (CoverageData, ReferenceData):
        data = implementation(str(tmp_path / implementation.__module__))
        data.set_context("test")
        if branch:
            data.add_arcs({"gone.py": {(1, 2)}, "keep.py": {(3, 4)}})
        else:
            data.add_lines({"gone.py": {1, 2}, "keep.py": {3, 4}})
        data.write()
        data.read()
        data.purge_files(["gone.py"])
        data.write()
        data.read()
        results.append((data.measured_files(), data.lines("gone.py"), data.lines("keep.py")))
    assert results[0] == results[1]


def test_conflicting_update_leaves_all_destination_records_unchanged() -> None:
    with ExitStack() as resources:
        for implementation in (CoverageData, ReferenceData):
            data = implementation(no_disk=True)
            if isinstance(data, ReferenceData):
                resources.callback(data.close, force=True)
            data.add_lines({"same.py": {1}})
            data.add_file_tracers({"same.py": "first"})
            other = implementation(no_disk=True)
            if isinstance(other, ReferenceData):
                resources.callback(other.close, force=True)
            other.add_lines({"earlier.py": {10}, "same.py": {2}})
            other.add_file_tracers({"same.py": "second"})
            with pytest.raises(
                (DataError, ReferenceDataError), match=r"[Cc]onflicting|conflicting"
            ):
                _update_same_implementation(data, other)
            assert data.measured_files() == {"same.py"}
            assert data.lines("same.py") == [1]


@pytest.mark.parametrize("branch", [False, True])
def test_update_unions_colliding_mapped_paths_and_contexts(branch: bool) -> None:
    with ExitStack() as resources:
        results = []
        for implementation in (CoverageData, ReferenceData):
            data = implementation(no_disk=True)
            if isinstance(data, ReferenceData):
                resources.callback(data.close, force=True)
            other = implementation(no_disk=True)
            if isinstance(other, ReferenceData):
                resources.callback(other.close, force=True)
            for path, context, line in (("first.py", "first", 1), ("second.py", "second", 2)):
                other.set_context(context)
                if branch:
                    other.add_arcs({path: {(line, line + 1)}})
                else:
                    other.add_lines({path: {line}})
            _update_same_implementation(data, other, map_path=lambda path: "merged.py")
            results.append(
                (
                    data.lines("merged.py"),
                    data.arcs("merged.py"),
                    _context_sets(data.contexts_by_lineno("merged.py")),
                )
            )
        assert results[0] == results[1]


def _context_sets(values: dict[int, list[str]]) -> dict[int, set[str]]:
    return {line: set(contexts) for line, contexts in values.items()}


@pytest.mark.parametrize("branch", [False, True])
def test_update_with_path_mapping_keeps_tracers_and_contexts(branch: bool) -> None:
    with ExitStack() as resources:
        results = []
        for implementation in (CoverageData, ReferenceData):
            data = implementation(no_disk=True)
            other = implementation(no_disk=True)
            for item in (data, other):
                if isinstance(item, ReferenceData):
                    resources.callback(item.close, force=True)
            other.set_context("test_one")
            if branch:
                other.add_arcs({"worker/example.py": {(2, 3), (3, 5)}})
            else:
                other.add_lines({"worker/example.py": {2, 5}})
            other.add_file_tracers({"worker/example.py": "plugin.name"})
            _update_same_implementation(
                data, other, map_path=lambda path: path.replace("worker/", "canonical/")
            )
            results.append(
                (
                    data.measured_files(),
                    data.lines("canonical/example.py"),
                    data.arcs("canonical/example.py"),
                    data.file_tracer("canonical/example.py"),
                    _context_sets(data.contexts_by_lineno("canonical/example.py")),
                )
            )
        assert results[0] == results[1]
        assert results[0][3] == "plugin.name"


def test_reads_coverage_py_line_data_with_contexts(tmp_path: Path) -> None:
    filename = str(tmp_path / ".coverage")
    source = str(tmp_path / "example.py")
    reference = ReferenceData(filename)
    reference.set_context("test_one")
    reference.add_lines({source: {1, 2}})
    reference.set_context("test_two")
    reference.add_lines({source: {2, 4}})
    reference.add_file_tracers({source: "plugin.name"})
    reference.write()

    data = CoverageData(filename)
    data.read()
    assert data.measured_files() == {source}
    assert data.measured_contexts() == {"test_one", "test_two"}
    assert data.lines(source) == [1, 2, 4]
    assert data.contexts_by_lineno(source) == {
        1: ["test_one"],
        2: ["test_one", "test_two"],
        4: ["test_two"],
    }
    assert data.file_tracer(source) == "plugin.name"

    data.set_query_context("test_two")
    assert data.lines(source) == [2, 4]


def test_identical_coverage_py_line_partitions_survive_vfy_round_trip(tmp_path: Path) -> None:
    original = str(tmp_path / "reference.coverage")
    rewritten = str(tmp_path / "vfy.coverage")
    source = str(tmp_path / "example.py")
    contexts = {
        "test_one": {1, 65, 1_025},
        "test_two": {1, 65, 1_025},
        "test_unique": {7, 65_535},
    }
    with ExitStack() as resources:
        reference = ReferenceData(original)
        resources.callback(reference.close, force=True)
        for context, lines in contexts.items():
            reference.set_context(context)
            reference.add_lines({source: lines})
        reference.write()

        data = CoverageData(original)
        data.read()
        expected = _context_sets(reference.contexts_by_lineno(source))
        assert _context_sets(data.contexts_by_lineno(source)) == expected

        output = CoverageData(rewritten)
        output.update(data)
        output.write()
        round_trip = ReferenceData(rewritten)
        resources.callback(round_trip.close, force=True)
        round_trip.read()
        assert round_trip.measured_contexts() == set(contexts)
        assert set(round_trip.lines(source) or []) == set().union(*contexts.values())
        assert _context_sets(round_trip.contexts_by_lineno(source)) == expected


def test_vfy_line_data_is_readable_by_coverage_py(tmp_path: Path) -> None:
    filename = str(tmp_path / ".coverage")
    source = str(tmp_path / "example.py")
    data = CoverageData(filename)
    data.set_context("test_one")
    data.add_lines({source: {1, 2}})
    data.set_context("test_two")
    data.add_lines({source: {2, 4}})
    data.add_file_tracers({source: "plugin.name"})
    data.write()

    reference = ReferenceData(filename)
    reference.read()
    assert reference.measured_files() == {source}
    assert reference.measured_contexts() == {"test_one", "test_two"}
    assert reference.lines(source) == [1, 2, 4]
    assert _context_sets(reference.contexts_by_lineno(source)) == {
        1: {"test_one"},
        2: {"test_one", "test_two"},
        4: {"test_two"},
    }
    assert reference.file_tracer(source) == "plugin.name"


def test_vfy_writes_canonical_coverage_py_line_bitmaps(tmp_path: Path) -> None:
    filename = str(tmp_path / ".coverage")
    source = str(tmp_path / "bitmap_boundaries.py")
    lines_by_context = {
        "dense": set(range(1, 257)),
        "sparse": {1, 7, 8, 63, 64, 65, 255, 256, 1_025, 65_535},
    }
    data = CoverageData(filename)
    for context, lines in lines_by_context.items():
        data.set_context(context)
        data.add_lines({source: lines})
    data.write()

    with closing(sqlite3.connect(filename)) as connection:
        rows = connection.execute(
            "SELECT context.context, line_bits.numbits "
            "FROM line_bits "
            "JOIN context ON context.id = line_bits.context_id "
            "JOIN file ON file.id = line_bits.file_id "
            "WHERE file.path = ? ORDER BY context.context",
            (source,),
        ).fetchall()
    assert rows == [
        (context, nums_to_numbits(lines)) for context, lines in sorted(lines_by_context.items())
    ]

    reference = ReferenceData(filename)
    try:
        reference.read()
        assert reference.lines(source) == sorted(set().union(*lines_by_context.values()))
    finally:
        reference.close(force=True)


def test_branch_contexts_survive_coverage_py_round_trip(tmp_path: Path) -> None:
    original = str(tmp_path / "original.coverage")
    rewritten = str(tmp_path / "rewritten.coverage")
    source = str(tmp_path / "branchy.py")
    reference = ReferenceData(original)
    reference.set_context("test_one")
    reference.add_arcs({source: {(-1, 1), (1, 2), (2, -1)}})
    reference.set_context("test_two")
    reference.add_arcs({source: {(-1, 1), (1, 3), (3, -1)}})
    reference.write()

    data = CoverageData(original)
    data.read()
    assert data.has_arcs()
    assert data.contexts_by_lineno(source) == {
        1: ["test_one", "test_two"],
        2: ["test_one"],
        3: ["test_two"],
    }
    data._filename = rewritten
    data.write()

    round_trip = ReferenceData(rewritten)
    round_trip.read()
    assert set(round_trip.arcs(source) or []) == {
        (-1, 1),
        (1, 2),
        (1, 3),
        (2, -1),
        (3, -1),
    }
    assert _context_sets(round_trip.contexts_by_lineno(source)) == _context_sets(
        data.contexts_by_lineno(source)
    )


def test_programmatic_api_query_serialization_and_purge() -> None:
    data = CoverageData(no_disk=True)
    assert not data
    data.add_lines({})
    data.touch_file("empty.py")
    data.set_context("unit")
    data.add_lines({"covered.py": {1, 2, 5}})
    assert data
    assert data.measured_files() == {"empty.py", "covered.py"}

    restored = CoverageData(no_disk=True)
    restored.loads(data.dumps())
    assert restored
    assert restored.lines("covered.py") == [1, 2, 5]
    assert restored.contexts_by_lineno("covered.py") == {
        1: ["unit"],
        2: ["unit"],
        5: ["unit"],
    }
    restored.purge_files({"covered.py"})
    assert restored
    assert restored.measured_files() == {"empty.py", "covered.py"}
    assert restored.lines("covered.py") == []
    restored._store = restored._store.copy()
    restored._store.files.clear()
    assert not restored
    restored.touch_file("new.py")
    assert restored


@pytest.mark.parametrize("contexts", [None, [], ["^one$"], ["o", "^t"], ["absent"]])
def test_query_contexts_match_coverage_py(contexts: list[str] | None) -> None:
    results = []
    for factory in (ReferenceData, CoverageData):
        data = factory(no_disk=True)
        data.set_context("one")
        data.add_lines({"a.py": [1, 2]})
        data.set_context("two")
        data.add_lines({"a.py": [2, 5]})
        data.set_query_contexts(contexts)
        results.append(sorted(data.lines("a.py") or []))
        data.close(force=True)
    assert results[1] == results[0]


def test_rejects_mixed_line_and_arc_data() -> None:
    data = CoverageData(no_disk=True)
    data.add_lines({"example.py": {1}})
    with pytest.raises(DataError, match="cannot combine line and branch coverage data"):
        data.add_arcs({"example.py": {(1, 2)}})


@pytest.mark.parametrize("initialization", ["uninitialized", "empty", "purged"])
@pytest.mark.parametrize("materialize", [False, True])
def test_arc_addition_preserves_established_empty_line_mode(
    initialization: str, materialize: bool
) -> None:
    with ExitStack() as resources:
        states = []
        for implementation in (ReferenceData, CoverageData):
            data = implementation(no_disk=True)
            if isinstance(data, ReferenceData):
                resources.callback(data.close, force=True)
            data.set_context("original")
            if initialization == "empty":
                data.add_lines({})
            elif initialization == "purged":
                data.add_lines({"f.py": {1}})
                data.purge_files(["f.py"])
            if materialize:
                data.lines("f.py")
            data.set_context("incoming")
            if initialization == "uninitialized":
                data.add_arcs({"f.py": {(-1, 1), (1, -1)}})
            else:
                with pytest.raises((ReferenceDataError, DataError)):
                    data.add_arcs({"f.py": {(-1, 1), (1, -1)}})
            states.append(
                (
                    data.has_arcs(),
                    data.measured_files(),
                    data.measured_contexts(),
                    data.lines("f.py"),
                    data.arcs("f.py"),
                    data.contexts_by_lineno("f.py"),
                )
            )
        assert states[0] == states[1]


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("materialize", [False, True])
def test_touch_rejects_tracer_conflict_and_preserves_context_records(
    multiple: bool, materialize: bool
) -> None:
    with ExitStack() as resources:
        states = []
        for implementation in (ReferenceData, CoverageData):
            data = implementation(no_disk=True)
            if isinstance(data, ReferenceData):
                resources.callback(data.close, force=True)
            data.set_context("original")
            data.add_lines({"f.py": {1}})
            data.add_file_tracers({"f.py": "first"})
            if materialize:
                data.lines("f.py")
            data.set_context("incoming")
            if multiple:
                with pytest.raises((ReferenceDataError, DataError), match=r"[Cc]onflict"):
                    data.touch_files(["f.py"], plugin_name="second")
            else:
                with pytest.raises((ReferenceDataError, DataError), match=r"[Cc]onflict"):
                    data.touch_file("f.py", plugin_name="second")
            data.touch_file("f.py", plugin_name="first")
            data.touch_file("f.py")
            states.append(
                (
                    data.has_arcs(),
                    data.measured_files(),
                    data.measured_contexts(),
                    data.lines("f.py"),
                    data.file_tracer("f.py"),
                    data.contexts_by_lineno("f.py"),
                )
            )
        assert states[0] == states[1]


@pytest.mark.parametrize("initialization", ["uninitialized", "lines", "arcs"])
@pytest.mark.parametrize("plugin", ["", "example.plugin"])
@pytest.mark.parametrize("multiple", [False, True])
def test_touch_requires_an_established_collection_mode(
    initialization: str, plugin: str, multiple: bool
) -> None:
    with ExitStack() as resources:
        states = []
        for implementation in (ReferenceData, CoverageData):
            data = implementation(no_disk=True)
            if isinstance(data, ReferenceData):
                resources.callback(data.close, force=True)
            if initialization == "lines":
                data.add_lines({})
            elif initialization == "arcs":
                data.add_arcs({})
            touch = (
                partial(data.touch_files, ["f.py"], plugin)
                if multiple
                else partial(data.touch_file, "f.py", plugin)
            )
            if initialization == "uninitialized":
                with pytest.raises((ReferenceDataError, DataError), match="empty CoverageData"):
                    touch()
            else:
                touch()
            states.append((data.has_arcs(), data.measured_files(), data.file_tracer("f.py")))
        assert states[0] == states[1]


def test_touch_batch_rejects_tracer_conflicts_before_adding_files() -> None:
    data = CoverageData(no_disk=True)
    data.add_lines({"measured.py": [1]})
    data.add_file_tracers({"measured.py": "original"})
    before = data.dumps()
    with pytest.raises(DataError, match="conflicting file tracers"):
        data.touch_files(["new.py", "measured.py"], plugin_name="other")
    assert data.dumps() == before


@pytest.mark.parametrize("materialize", [False, True])
def test_tracer_batch_conflict_preserves_existing_data(materialize: bool) -> None:
    data = CoverageData(no_disk=True)
    data.add_lines({"measured.py": [1]})
    data.add_file_tracers({"measured.py": "original"})
    if materialize:
        data.lines("measured.py")
    before = data.dumps()
    with pytest.raises(DataError, match="conflicting file tracers"):
        data.add_file_tracers({"new.py": "new", "measured.py": "other"})
    assert data.dumps() == before
    assert data.measured_files() == {"measured.py"}

    # Coverage.py commits earlier entries; vfy promises an atomic batch.
    reference = ReferenceData(no_disk=True)
    try:
        reference.add_lines({"measured.py": [1]})
        reference.add_file_tracers({"measured.py": "original"})
        with pytest.raises(ReferenceDataError, match="Conflicting file tracer"):
            reference.add_file_tracers({"new.py": "new", "measured.py": "other"})
        assert reference.measured_files() == {"measured.py", "new.py"}
    finally:
        reference.close(force=True)


def test_empty_tracer_can_be_replaced_with_a_plugin() -> None:
    with ExitStack() as resources:
        states = []
        for implementation in (ReferenceData, CoverageData):
            data = implementation(no_disk=True)
            if isinstance(data, ReferenceData):
                resources.callback(data.close, force=True)
            data.add_file_tracers({"source.py": ""})
            data.add_file_tracers({"source.py": "plugin"})
            states.append((data.measured_files(), data.file_tracer("source.py")))
        assert states[0] == states[1] == ({"source.py"}, "plugin")


@pytest.mark.parametrize(
    "payload",
    [
        b"not-vfy",
        b"vfy-data\0not-zlib",
        b"vfy-data-v1\0not-zlib",
        b"vfy-data-v1\0x\x9c\xab\xae\x05\x00\x01u\x00\xf9",
    ],
)
def test_loads_rejects_foreign_or_corrupt_payloads(payload: bytes) -> None:
    data = CoverageData(no_disk=True)
    data.add_lines({"original.py": {7}})
    with pytest.raises(DataError, match="serialized"):
        data.loads(payload)
    assert data.lines("original.py") == [7]


@pytest.mark.parametrize("branch", [False, True])
def test_randomized_rust_schema_round_trip_is_coverage_py_readable(
    tmp_path: Path, branch: bool
) -> None:
    random = Random(20260821)
    filename = str(tmp_path / ("branch.coverage" if branch else "line.coverage"))
    data = CoverageData(filename)
    expected: dict[str, set[int | tuple[int, int]]] = {}
    for file_index in range(25):
        source = str(tmp_path / f"module_{file_index:03d}.py")
        for context_index in range(4):
            data.set_context(f"test_{context_index}")
            if branch:
                values = {(random.randint(-20, 80), random.randint(-20, 80)) for _ in range(20)}
                data.add_arcs({source: values})
            else:
                values = {random.randint(0, 500) for _ in range(30)}
                data.add_lines({source: values})
            expected.setdefault(source, set()).update(values)
        if file_index % 3 == 0:
            data.add_file_tracers({source: "example.plugin"})
    data.write()

    reference = ReferenceData(filename)
    reference.read()
    assert reference.measured_files() == set(expected)
    for source, values in expected.items():
        observed = reference.arcs(source) if branch else reference.lines(source)
        assert set(observed or []) == values
        if int(source.rsplit("_", 1)[1].split(".", 1)[0]) % 3 == 0:
            assert reference.file_tracer(source) == "example.plugin"


@pytest.mark.parametrize("branch", [False, True])
def test_update_tracer_conflict_preserves_existing_data(branch: bool) -> None:
    with ExitStack() as resources:
        reference_left = ReferenceData(no_disk=True)
        resources.callback(reference_left.close, force=True)
        reference_right = ReferenceData(no_disk=True)
        resources.callback(reference_right.close, force=True)
        vfy_left = CoverageData(no_disk=True)
        vfy_right = CoverageData(no_disk=True)
        states = []
        for left, right, update in (
            (reference_left, reference_right, lambda: reference_left.update(reference_right)),
            (vfy_left, vfy_right, lambda: vfy_left.update(vfy_right)),
        ):
            if branch:
                left.add_arcs({"example.py": {(1, 2)}})
                right.add_arcs({"example.py": {(2, 3)}})
            else:
                left.add_lines({"example.py": {1}})
                right.add_lines({"example.py": {2}})
            left.add_file_tracers({"example.py": "one"})
            right.add_file_tracers({"example.py": "two"})
            with pytest.raises((ReferenceDataError, DataError), match=r"[Cc]onflict|conflicting"):
                update()
            states.append(
                (left.lines("example.py"), left.arcs("example.py"), left.file_tracer("example.py"))
            )
        assert states[0] == states[1]


@pytest.mark.parametrize("branch", [False, True])
def test_purge_preserves_measured_context_inventory_across_transport(
    tmp_path: Path, branch: bool
) -> None:
    with ExitStack() as resources:
        results = []
        for implementation in (CoverageData, ReferenceData):
            data = implementation(str(tmp_path / implementation.__module__))
            data.set_context("removed observation")
            if branch:
                data.add_arcs({"gone.py": {(1, 2)}})
            else:
                data.add_lines({"gone.py": {1, 2}})
            data.purge_files(["gone.py"])
            before = data.measured_contexts()
            data.write()
            data.read()
            persisted = data.measured_contexts()
            restored = implementation(no_disk=True)
            if isinstance(restored, ReferenceData):
                resources.callback(restored.close, force=True)
            restored.loads(data.dumps())
            # Querying lines materializes the compatibility view; context names
            # must survive subsequent Rust serialization as well.
            assert restored.lines("gone.py") == []
            second_restore = implementation(no_disk=True)
            if isinstance(second_restore, ReferenceData):
                resources.callback(second_restore.close, force=True)
            second_restore.loads(restored.dumps())
            results.append((before, persisted, second_restore.measured_contexts()))
        assert results[0] == results[1] == ({"removed observation"},) * 3


@pytest.mark.parametrize("initialize_lines", [False, True])
def test_branch_update_distinguishes_uninitialized_and_empty_line_data(
    initialize_lines: bool,
) -> None:
    with ExitStack() as resources:
        for implementation in (CoverageData, ReferenceData):
            target = implementation(no_disk=True)
            if isinstance(target, ReferenceData):
                resources.callback(target.close, force=True)
            target.add_arcs({"keep.py": {(1, 2)}})
            incoming = implementation(no_disk=True)
            if isinstance(incoming, ReferenceData):
                resources.callback(incoming.close, force=True)
            if initialize_lines:
                incoming.add_lines({})
                with pytest.raises((DataError, ReferenceDataError)):
                    _update_same_implementation(target, incoming)
            else:
                _update_same_implementation(target, incoming)
            assert target.has_arcs()
            assert target.arcs("keep.py") == [(1, 2)]


def test_branch_writer_round_trips_through_coverage_py_without_metadata(
    tmp_path: Path,
) -> None:
    filename = str(tmp_path / "vfy.coverage")
    rewritten = str(tmp_path / "reference.coverage")
    data = CoverageData(filename)
    data.set_context("test")
    data.add_arcs({"module.py": {(-1, 1), (1, 3), (3, -1)}})
    data.write()

    with ExitStack() as resources:
        reference = ReferenceData(filename)
        resources.callback(reference.close, force=True)
        reference.read()
        assert (
            reference.has_arcs(),
            reference.arcs("module.py"),
            reference.lines("module.py"),
        ) == (
            True,
            [(-1, 1), (1, 3), (3, -1)],
            [1, 3],
        )
        output = ReferenceData(rewritten)
        resources.callback(output.close, force=True)
        output.update(reference)
        output.write()

    restored = CoverageData(rewritten)
    restored.read()
    assert restored.has_arcs()
    assert restored.arcs("module.py") == data.arcs("module.py")
    assert restored.lines("module.py") == [1, 3]
    loaded = CoverageData(no_disk=True)
    loaded.loads(data.dumps())
    assert loaded.arcs("module.py") == data.arcs("module.py")


def _outcome(action: Callable[[], object]) -> tuple[str, str]:
    """Return the exception class name and message of ``action``, or ``("", "")``."""
    try:
        action()
    except Exception as error:  # noqa: BLE001 - the differential compares every outcome
        return type(error).__name__, str(error)
    return "", ""


def test_write_of_never_populated_data_creates_no_file(tmp_path: Path) -> None:
    """Coverage.py creates its data file when lines or arcs are first chosen."""
    files: dict[str, list[str]] = {}
    for engine, data_class in (("coverage", ReferenceData), ("vfy", CoverageData)):
        directory = tmp_path / engine
        directory.mkdir()
        data = data_class(str(directory / "data"))
        data.write()
        data.set_context("unit")
        data.write()
        files[engine] = sorted(path.name for path in directory.iterdir())
        populated = data_class(str(directory / "populated"))
        populated.add_lines({})
        populated.write()
        files[engine + "-populated"] = sorted(path.name for path in directory.iterdir())
    assert files["vfy"] == files["coverage"] == []
    assert files["vfy-populated"] == files["coverage-populated"] == ["populated"]


@pytest.mark.parametrize("populate", ["none", "lines", "arcs"])
def test_purge_files_matches_coverage_py_for_each_collection_mode(
    tmp_path: Path, populate: str
) -> None:
    outcomes = {}
    for engine, data_class in (("coverage", ReferenceData), ("vfy", CoverageData)):
        data = data_class(str(tmp_path / engine))
        if populate == "lines":
            data.add_lines({})
        elif populate == "arcs":
            data.add_arcs({})
        outcomes[engine] = _outcome(lambda data=data: data.purge_files(["a.py"]))
    assert outcomes["vfy"] == outcomes["coverage"]
    assert (outcomes["vfy"][0] == "DataError") is (populate == "none")
    assert outcomes["vfy"][1] in {"", "Can't purge files in an empty CoverageData"}


def test_empty_arc_set_registers_no_file_but_chooses_arc_mode(tmp_path: Path) -> None:
    state = {}
    for engine, data_class in (("coverage", ReferenceData), ("vfy", CoverageData)):
        data = data_class(str(tmp_path / engine))
        data.add_arcs({"empty.py": set(), "covered.py": {(1, 2)}})
        data.add_arcs({"also_empty.py": set()})
        state[engine] = (data.measured_files(), data.has_arcs(), bool(data))
    assert state["vfy"] == state["coverage"]
    assert state["vfy"][0] == {"covered.py"}


@pytest.mark.parametrize(
    ("paths", "measured", "source"),
    [
        pytest.param(
            '["src/missing/../pkg", "/remote/pkg"]',
            "/remote/pkg/mod.py",
            "src/pkg/mod.py",
            id="parent-through-missing-directory",
        ),
        pytest.param('["src", "/"]', "/mod.py", "src/mod.py", id="root-pattern"),
        pytest.param(
            '["", "/other"]',
            "/other{target}",
            "src/mod.py",
            id="empty-result",
            marks=pytest.mark.skipif(os.name == "nt", reason="uses a POSIX absolute path"),
        ),
        pytest.param(
            '["src/has space ", "/remote/pkg"]',
            "/remote/pkg/mod.py",
            "src/has space /mod.py",
            id="literal-spaces",
            marks=pytest.mark.skipif(os.name == "nt", reason="trailing spaces in directory names"),
        ),
        pytest.param(
            '["src/file.py/../pkg", "/remote/pkg"]',
            "/remote/pkg/mod.py",
            "src/pkg/mod.py",
            id="parent-through-file",
        ),
    ],
)
def test_configured_combine_path_alias_edges_match_coverage_py(
    tmp_path: Path, paths: str, measured: str, source: str
) -> None:
    combined: dict[str, list[str]] = {}
    for engine in ("coverage", "vfy"):
        project = VfyProject(tmp_path / engine)
        target = project.write(source, "x = 1\n")
        project.write("src/file.py", "pass\n")
        project.write("pyproject.toml", f"[tool.coverage.paths]\npkg = {paths}\n")
        data = ReferenceData(basename=str(project.root / ".coverage.shard"))
        try:
            data.add_lines({measured.format(target=target.as_posix()): [1]})
            data.write()
        finally:
            data.close(force=True)
        command = ("-m", "coverage") if engine == "coverage" else ("-m", "vfy", "coverage")
        project.run_python(*command, "combine").expect(0)
        result = ReferenceData(basename=str(project.root / ".coverage"))
        try:
            result.read()
            root = project.root.resolve().as_posix()
            combined[engine] = sorted(
                Path(name).as_posix().replace(root, "<root>") for name in result.measured_files()
            )
        finally:
            result.close(force=True)
    assert combined["vfy"] == combined["coverage"] == [f"<root>/{source}"]
