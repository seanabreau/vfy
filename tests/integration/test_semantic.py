"""Truth-table and persistence tests for opt-in semantic coverage."""

from __future__ import annotations

import hashlib
import json
import runpy
import shlex
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from coverage import CoverageData as ReferenceData

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult
from vfy import Coverage, CoverageData
from vfy.errors import DataError, NoDataError

pytestmark = pytest.mark.skipif(
    not hasattr(sys, "monitoring"),
    reason="semantic coverage requires CPython 3.12+ monitoring",
)


def _write_text(path: Path, text: str) -> None:
    """Write fixture source with stable LF bytes on every platform."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _read(data_file: Path, *, source: Sequence[str] | None = None) -> Coverage:
    """Open a saved data file through the public reporting API."""
    coverage = Coverage(data_file=str(data_file), decisions=True, config_file=False, source=source)
    coverage.load()
    return coverage


def _semantic_json(
    coverage: Coverage, directory: Path, *, source: Sequence[str] | None = None
) -> dict[str, Any]:
    destination = directory / "semantic.json"
    coverage.decision_report(format="json", output=str(destination), source=source)
    return json.loads(destination.read_text(encoding="utf-8"))


def _semantic_text(coverage: Coverage, directory: Path) -> str:
    destination = directory / "semantic.txt"
    coverage.decision_report(format="text", output=str(destination))
    return destination.read_text(encoding="utf-8")


def collect(tmp_path: Path, source: str) -> Coverage:
    filename = tmp_path / "sample.py"
    _write_text(filename, source)
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True
    )
    coverage.start()
    try:
        runpy.run_path(str(filename), run_name="__main__")
    finally:
        coverage.stop()
        coverage.save()
    return _read(tmp_path / ".coverage")


def collect_shard(
    tmp_path: Path,
    data_file: Path,
    source: str,
    *,
    run_globals: dict[str, object] | None = None,
) -> None:
    filename = tmp_path / "sample.py"
    _write_text(filename, source)
    coverage = Coverage(data_file=str(data_file), source=[str(tmp_path)], decisions=True)
    coverage.start()
    try:
        runpy.run_path(str(filename), run_name="__main__", init_globals=run_globals)
    finally:
        coverage.stop()
        coverage.save()


def test_identical_files_keep_independent_mcdc_evidence(tmp_path: Path) -> None:
    source = """\
def choose(first, second):
    if first and second:
        return True
    return False
"""
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _write_text(first_path, source)
    _write_text(second_path, source)
    data_file = tmp_path / ".coverage"
    coverage = Coverage(data_file=str(data_file), source=[str(tmp_path)], decisions=True)
    coverage.start()
    try:
        first = runpy.run_path(str(first_path))["choose"]
        second = runpy.run_path(str(second_path))["choose"]
        first(False, False)
        first(True, True)
        second(True, False)
        second(True, True)
    finally:
        coverage.stop()
        coverage.save()

    store = _read(data_file)
    report = store.decision_report()
    assert {(Path(row.path).name, row.mcdc_covered, row.mcdc_total) for row in report.rows} == {
        ("first.py", 1, 2),
        ("second.py", 1, 2),
    }
    assert report.mcdc_percent == 50.0


def test_decision_and_condition_coverage_can_hide_incomplete_mcdc(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """
def choose(a, b):
    if a and b:
        return 1
    return 0

choose(False, False)
choose(True, True)
""",
    )
    report = store.decision_report()
    assert report.decision_percent == 100.0
    assert report.condition_percent < 100.0
    assert report.mcdc_percent < 100.0
    assert any("independent effect" in item for item in report.rows[0].missing)


def test_named_exception_and_value_outcomes_are_persisted_and_reported(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
class CustomValueError(ValueError):
    pass

def choose(first, second, mode):
    value = first or second
    try:
        if mode == "value":
            raise CustomValueError()
        if mode == "key":
            raise KeyError()
    except ValueError:
        return value
    return value

choose(1, 2, "normal")
choose(0, 2, "value")
try:
    choose(1, 2, "key")
except KeyError:
    pass
""",
    )
    data = store.get_decision_data()
    definitions = {definition.kind: definition for definition in data.path_definitions}
    assert [outcome.label for outcome in definitions["value_or"].outcomes] == [
        "operand 1 selected",
        "operand 2 selected",
    ]
    assert [outcome.label for outcome in definitions["try"].outcomes] == [
        "normal completion",
        "handler 1 ValueError",
        "unhandled exception",
    ]
    observed = {
        (
            next(item for item in data.path_definitions if item.key == observation.decision).kind,
            observation.outcome,
        )
        for observation in data.path_observations
    }
    assert {("value_or", 0), ("value_or", 1), ("try", 0), ("try", 1), ("try", 2)} <= observed

    rows = store.decision_report().rows
    value_row = next(row for row in rows if row.kind == "value_or")
    try_row = next(row for row in rows if row.kind == "try" and row.line == 6)
    assert value_row.decisions_covered == value_row.decisions_total == 2
    assert try_row.decisions_covered == try_row.decisions_total == 3
    payload = _semantic_json(store, tmp_path)
    assert payload["schema"] == "vfy.semantic"
    assert "version" not in payload
    assert any(row["outcomes"] for row in payload["files"])


def test_cancellation_aware_regions_record_normal_and_cancelled(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
import asyncio

async def worker(started, block):
    try:
        started.set()
        if block:
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(0)
    finally:
        pass

async def main():
    await worker(asyncio.Event(), False)
    blocking = asyncio.Event()
    task = asyncio.create_task(worker(blocking, True))
    await blocking.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

asyncio.run(main())
""",
    )
    data = store.get_decision_data()
    worker = next(
        definition
        for definition in data.path_definitions
        if definition.kind == "async_cancellation" and definition.line == 4
    )
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == worker.key
    } == {0, 1}


def test_except_star_records_each_matching_handler_for_one_group(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
try:
    raise ExceptionGroup("mixed", [ValueError(), TypeError()])
except* ValueError:
    value = 1
except* TypeError:
    value = 2
""",
    )
    data = store.get_decision_data()
    definition = next(item for item in data.path_definitions if item.kind == "try_star")
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == definition.key
    } == {1, 2}


def test_nested_value_expressions_are_independent_decisions(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
def choose(first, second, third):
    return (first and second) or third

choose(0, 2, 3)
choose(1, 2, 3)
""",
    )
    data = store.get_decision_data()
    definitions = {
        definition.kind: definition
        for definition in data.path_definitions
        if definition.kind.startswith("value_")
    }
    assert set(definitions) == {"value_and", "value_or"}
    for definition in definitions.values():
        assert {
            observation.outcome
            for observation in data.path_observations
            if observation.decision == definition.key
        } == {0, 1}


def test_match_does_not_fabricate_no_match_when_subject_pattern_or_guard_raises(
    tmp_path: Path,
) -> None:
    store = collect(
        tmp_path,
        """
def bad_subject():
    raise RuntimeError()

class Bad:
    __match_args__ = (42,)

    def __init__(self):
        self.present = True

def bad_guard():
    raise ValueError()

try:
    match bad_subject():
        case 1:
            pass
except RuntimeError:
    pass

try:
    match Bad():
        case Bad(value):
            pass
except TypeError:
    pass

try:
    match 1:
        case 1 if bad_guard():
            pass
except ValueError:
    pass
""",
    )
    data = store.get_decision_data()
    matches = [definition for definition in data.path_definitions if definition.kind == "match"]
    assert len(matches) == 3
    assert all(
        not any(observation.decision == definition.key for observation in data.path_observations)
        for definition in matches
    )


def test_review_nested_or_does_not_select_inner_truthy_prefix(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
def choose(first, second, third):
    return (first and second) or third

choose(1, 0, 3)
""",
    )
    data = store.get_decision_data()
    definition = next(item for item in data.path_definitions if item.kind == "value_or")
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == definition.key
    } == {1}


def test_nested_or_propagates_truthy_result_to_parent_and(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
def choose(first, second, third):
    return (first or second) and third

choose(1, 0, 3)
choose(0, 2, 3)
choose(0, 0, 3)
""",
    )
    data = store.get_decision_data()
    definitions = {
        definition.kind: definition
        for definition in data.path_definitions
        if definition.kind.startswith("value_")
    }
    assert set(definitions) == {"value_and", "value_or"}
    for definition in definitions.values():
        assert {
            observation.outcome
            for observation in data.path_observations
            if observation.decision == definition.key
        } == {0, 1}


def test_value_expression_attributes_each_operand(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
def choose(first, second, third):
    return first or second or third

choose(1, 2, 3)
choose(0, 2, 3)
choose(0, 0, 3)
""",
    )
    data = store.get_decision_data()
    definition = next(item for item in data.path_definitions if item.kind == "value_or")
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == definition.key
    } == {0, 1, 2}


def test_async_with_is_cancellation_aware(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
import asyncio

class Manager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, kind, value, traceback):
        return False

async def worker(started, block):
    async with Manager():
        started.set()
        if block:
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(0)

async def main():
    await worker(asyncio.Event(), False)
    started = asyncio.Event()
    task = asyncio.create_task(worker(started, True))
    await started.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

asyncio.run(main())
""",
    )
    data = store.get_decision_data()
    definition = next(
        item
        for item in data.path_definitions
        if item.kind == "async_cancellation" and item.line == 11
    )
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == definition.key
    } == {0, 1}


def test_prove_gates_named_decision_outcomes_by_default(
    tmp_path: Path,
) -> None:
    collect(
        tmp_path,
        """\
def choose(first, second):
    return first or second

choose(1, 2)
""",
    )
    result = VfyProject(tmp_path).run_vfy(
        "coverage", "check", "--strict", "--data-file", ".coverage"
    )
    result.expect(2)
    assert "required decision coverage of 100%" in result.stderr.lower()


def test_prove_gates_unmatched_match_outcome(
    tmp_path: Path,
) -> None:
    collect(
        tmp_path,
        """
value = 1
match value:
    case 1:
        result = \"one\"
    case 2:
        result = \"two\"
""",
    )
    result = VfyProject(tmp_path).run_vfy(
        "coverage", "check", "--strict", "--data-file", ".coverage"
    )
    result.expect(2)
    assert "required decision coverage of 100%" in result.stderr.lower()


def test_multiline_match_subject_records_selected_outcomes(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """
for value in (1, 2):
    match (
        value
    ):
        case 1:
            result = "one"
        case _:
            result = "other"
""",
    )
    definition = next(
        item for item in store.get_decision_data().path_definitions if item.kind == "match"
    )
    assert {
        observation.outcome
        for observation in store.get_decision_data().path_observations
        if observation.decision == definition.key
    } == {0, 1}


def test_match_loop_records_no_match_before_next_iteration(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """
for value in (0, 1):
    match value:
        case 1:
            result = "one"
""",
    )
    definition = next(
        item for item in store.get_decision_data().path_definitions if item.kind == "match"
    )
    assert {
        observation.outcome
        for observation in store.get_decision_data().path_observations
        if observation.decision == definition.key
    } == {0, 1}


def test_prove_is_the_strict_mcdc_gate(
    tmp_path: Path,
) -> None:
    collect(
        tmp_path,
        """\
def choose(first, second):
    if first and second:
        return True
    return False

choose(False, False)
choose(True, True)
""",
    )
    result = VfyProject(tmp_path).run_vfy(
        "coverage", "check", "--strict", "--data-file", ".coverage"
    )
    result.expect(2)
    assert "required MC/DC coverage of 100%" in result.stderr


def test_evidence_lists_the_rust_independent_effect_pairs(
    tmp_path: Path,
) -> None:
    source = """\
def choose(first, second):
    if first and second:
        return True
    return False
"""
    filename = tmp_path / "sample.py"
    _write_text(filename, source)
    data_file = tmp_path / ".coverage"
    coverage = Coverage(data_file=str(data_file), source=[str(tmp_path)], decisions=True)
    coverage.start()
    try:
        choose = runpy.run_path(str(filename), run_name="__main__")["choose"]
        for context, first, second in [
            ("a_false", False, False),
            ("b_middle", True, False),
            ("c_true", True, True),
            ("d_false", False, False),
            ("e_true", True, True),
        ]:
            coverage.switch_context(context)
            choose(first, second)
    finally:
        coverage.stop()
        coverage.save()

    decision_data = _read(data_file).get_decision_data()
    definition = decision_data.definitions[0]
    expected = [
        (0, "a_false", "0-", False, "c_true", "11", True),
        (0, "a_false", "0-", False, "e_true", "11", True),
        (0, "c_true", "11", True, "d_false", "0-", False),
        (0, "d_false", "0-", False, "e_true", "11", True),
        (1, "b_middle", "10", False, "c_true", "11", True),
        (1, "b_middle", "10", False, "e_true", "11", True),
    ]
    assert [
        (
            pair.decision,
            pair.condition,
            pair.left_context,
            pair.left_vector,
            pair.left_outcome,
            pair.right_context,
            pair.right_vector,
            pair.right_outcome,
        )
        for pair in decision_data.evidence_records()
    ] == [(definition.key, *witness) for witness in expected]
    explained = VfyProject(tmp_path).run_vfy(
        "explain", f"{filename}:2", "--data-file", ".coverage", "--json"
    )
    explained.expect(0)
    output = json.loads(explained.stdout)
    assert output["semantic_evidence"] == [
        {
            "condition": condition + 1,
            "left_context": left_context,
            "left_vector": left_vector,
            "right_context": right_context,
            "right_vector": right_vector,
        }
        for condition, left_context, left_vector, _, right_context, right_vector, _ in expected
    ]


def test_condition_ranges_are_rust_and_persisted(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """\
def choose(first, second):
    if first and second:
        return True
    return False

choose(False, False)
""",
    )
    definition = store.get_decision_data().definitions[0]
    assert [
        (item.index, item.start_line, item.start_column, item.end_line, item.end_column)
        for item in definition.condition_ranges
    ] == [
        (0, 2, 7, 2, 12),
        (1, 2, 17, 2, 23),
    ]
    report = store.decision_report()
    assert report.rows[0].condition_ranges == definition.condition_ranges
    payload = _semantic_json(store, tmp_path)
    assert payload["files"][0]["conditions_definition"] == [
        {"index": 0, "line": 2, "column": 7, "end_line": 2, "end_column": 12},
        {"index": 1, "line": 2, "column": 17, "end_line": 2, "end_column": 23},
    ]


def test_structurally_invalid_observation_is_rejected_on_read(tmp_path: Path) -> None:
    collect(
        tmp_path,
        """\
def choose(first, second):
    if first and second:
        return True
    return False

choose(False, False)
""",
    )
    with closing(sqlite3.connect(tmp_path / ".coverage")) as connection, connection:
        connection.execute("UPDATE vfy_observation SET vector = '-0'")

    with pytest.raises(DataError):
        _read(tmp_path / ".coverage")


@pytest.mark.parametrize("kind", ["unknown", ""])
def test_unsupported_boolean_kind_is_rejected_on_read(tmp_path: Path, kind: str) -> None:
    collect(tmp_path, "flag = False\nif flag:\n    pass\n")
    with closing(sqlite3.connect(tmp_path / ".coverage")) as connection, connection:
        connection.execute("UPDATE vfy_decision SET kind = ?", (kind,))

    with pytest.raises(DataError, match="unsupported boolean decision kind"):
        _read(tmp_path / ".coverage")


@pytest.mark.parametrize("pragma", ["pragma: no cover", "pragma: no branch", "pragma: no decision"])
def test_pragma_literals_preserve_boolean_and_path_observations(
    tmp_path: Path, pragma: str
) -> None:
    store = collect(
        tmp_path,
        f"value = 'other'\nif value == {pragma!r}:\n    pass\nresult = value or {pragma!r}\n",
    )
    data = store.get_decision_data()
    assert [(definition.kind, definition.line) for definition in data.definitions] == [("if", 2)]
    assert [(observation.vector, observation.outcome) for observation in data.observations] == [
        ("0", False)
    ]
    assert [(definition.kind, definition.line) for definition in data.path_definitions] == [
        ("value_or", 4)
    ]
    assert [observation.outcome for observation in data.path_observations] == [0]


def test_invalid_named_outcome_reference_is_rejected_on_read(tmp_path: Path) -> None:
    collect(
        tmp_path,
        """\
def choose(first, second):
    return first or second

choose(1, 2)
""",
    )
    with closing(sqlite3.connect(tmp_path / ".coverage")) as connection, connection:
        connection.execute("UPDATE vfy_path_observation SET outcome = 99")

    with pytest.raises(DataError):
        _read(tmp_path / ".coverage")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE vfy_path_decision SET kind = 'unknown'",
        "UPDATE vfy_path_outcome SET role = 'unknown' WHERE index_no = 0",
        "UPDATE vfy_path_outcome SET role = 'handler' WHERE index_no = 0",
        "UPDATE vfy_path_outcome SET label = '' WHERE index_no = 0",
        "UPDATE vfy_path_outcome SET index_no = 4 WHERE index_no = 1",
    ],
    ids=["kind", "role", "handler-body", "label", "index"],
)
def test_malformed_named_path_definition_is_rejected_on_read(
    tmp_path: Path, statement: str
) -> None:
    collect(
        tmp_path,
        """\
def choose(first, second):
    return first or second

choose(1, 2)
""",
    )
    with closing(sqlite3.connect(tmp_path / ".coverage")) as connection, connection:
        connection.execute(statement)

    with pytest.raises(DataError):
        _read(tmp_path / ".coverage")


def test_disabled_decision_mode_does_not_persist_named_paths(tmp_path: Path) -> None:
    filename = tmp_path / "sample.py"
    filename.write_text(
        "value = False or True\ntry:\n    result = value\nexcept RuntimeError:\n    pass\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    coverage = Coverage(data_file=str(data_file), source=[str(tmp_path)], decisions=False)
    coverage.start()
    try:
        runpy.run_path(str(filename), run_name="__main__")
    finally:
        coverage.stop()
        coverage.save()

    store = _read(data_file)
    assert store.get_data().lines(str(filename))
    assert not store.get_decision_data().has_data()
    with closing(sqlite3.connect(data_file)) as connection, connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not tables & {
        "vfy_path_decision",
        "vfy_path_outcome",
        "vfy_path_observation",
    }


def test_terminal_report_names_missing_conditions_and_the_fastest_reaching_test(
    tmp_path: Path,
) -> None:
    filename = tmp_path / "sample.py"
    filename.write_text(
        "def choose(café, second):\n"
        "    if café and second:\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True
    )
    coverage.start()
    try:
        coverage.switch_context("tests/test_choose.py::test_false|run")
        runpy.run_path(
            str(filename),
            run_name="__main__",
            init_globals={},
        )["choose"](True, False)
    finally:
        coverage.stop()
        coverage.save()
    history = tmp_path / ".vfy" / "test-history.json"
    history.parent.mkdir()
    history.write_text(
        json.dumps(
            {
                "tests": {
                    "tests/test_choose.py::test_false": {"duration_ms": 2.8},
                },
            }
        ),
        encoding="utf-8",
    )

    checked = VfyProject(tmp_path).run_vfy(
        "coverage", "check", "--data-file", ".coverage", "--history", str(history)
    )
    rendered = checked.stdout + checked.stderr

    assert "'café' must be false (2:8)" in rendered
    assert "'second' must be true (2:18)" in rendered
    assert "try: tests/test_choose.py::test_false (2.8 ms)" in rendered
    assert "vfy test tests/test_choose.py::test_false\n" in rendered


@requires_native_pytest
def test_semantic_suggestion_runs_the_reported_parameter(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "sample.py", "def choose(flag):\n    if flag:\n        return 1\n    return 0\n"
    )
    target = "-tests/test_choose.py::test_choice[it's here]"
    project.write(
        "-tests/test_choose.py",
        "import pytest\nfrom sample import choose\n\n"
        "@pytest.mark.parametrize('flag', [False, True], ids=[\"it's here\", 'other'])\n"
        "def test_choice(flag):\n    assert choose(flag) == int(flag)\n",
    )
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True
    )
    with coverage.collect():
        coverage.switch_context(f"{target}|run")
        runpy.run_path(str(source))["choose"](False)
    coverage.save()

    report = project.run_vfy("coverage", "check")
    suggestion = next(line.strip() for line in report.stdout.splitlines() if "vfy test " in line)
    command, *arguments = shlex.split(suggestion)
    assert command == "vfy"
    assert arguments == ["test", f"./{target}"]
    result = project.run_vfy(*arguments, "--engine=native", "--no-cov", "-v").expect(0)
    assert "1 passed" in result.stdout
    assert "test_choice[other] PASSED" not in result.stdout


@pytest.mark.parametrize("root_kind", ["file", "directory"])
def test_unreadable_discovered_semantic_source_is_rejected(tmp_path: Path, root_kind: str) -> None:
    store = collect(tmp_path, "flag = False\nif flag:\n    pass\n")
    invalid = tmp_path / "invalid.py"
    invalid.write_bytes(b"if '\xff':\n    pass\n")
    root = invalid if root_kind == "file" else tmp_path

    with pytest.raises(OSError, match=r"read semantic source.*invalid\.py"):
        store.decision_report(source=[str(root)])


def test_static_source_and_stale_data_are_safe(tmp_path: Path) -> None:
    filename = tmp_path / "uncovered.py"
    filename.write_text("value = left or right\nif flag:\n    value = 1\n", encoding="utf-8")
    data = Coverage(data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True)
    data.start()
    data.stop()
    data.save()

    store = _read(tmp_path / ".coverage")
    with pytest.raises(NoDataError):
        store.decision_report()
    report = store.decision_report(source=[str(filename)])
    assert report.decisions_total == 4
    assert report.decisions_covered == 0

    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    collect(stale_dir, "flag = False\nif flag:\n    value = 1\n")
    stale_file = stale_dir / "sample.py"
    stale_file.write_text("other = False\nif other:\n    value = 1\n", encoding="utf-8")
    stale_store = _read(stale_dir / ".coverage")
    with pytest.raises(DataError, match="stale"):
        stale_store.decision_report()

    project = VfyProject(stale_dir)
    output = project.write("report.json", "previous report\n")
    result = project.run_vfy("coverage", "check", "--format", "json", "--output", str(output))
    assert result.returncode == 1
    assert "is stale; rerun collection with --decisions" in result.stderr
    assert result.stdout == ""
    assert output.read_text() == "previous report\n"


def test_terminal_report_decodes_python_source_condition_evidence(tmp_path: Path) -> None:
    filename = tmp_path / "sample.py"
    filename.write_bytes(
        b"# coding: latin-1\n"
        b"def choose(caf\xe9, second):\n"
        b"    if caf\xe9 and second:\n"
        b"        return True\n"
        b"    return False\n\n"
        b"choose(True, False)\n"
    )
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True
    )
    coverage.start()
    try:
        runpy.run_path(str(filename), run_name="__main__")
    finally:
        coverage.stop()
        coverage.save()

    rendered = _semantic_text(_read(tmp_path / ".coverage"), tmp_path)

    assert "'café' must be false (3:8)" in rendered
    assert "'second' must be true (3:18)" in rendered


def test_coverage_py_reads_the_schema_seven_extension(tmp_path: Path) -> None:
    source = "flag = True\nif flag:\n    value = 1\n"
    store = collect(tmp_path, source)
    assert store.get_decision_data().definitions

    reference = ReferenceData(str(tmp_path / ".coverage"))
    reference.read()
    assert any(path.endswith("sample.py") for path in reference.measured_files())


@pytest.mark.parametrize("kind", ["boolean", "path"])
def test_semantic_check_rejects_a_later_conflicting_source_digest(
    tmp_path: Path, kind: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "sample.py",
        "flag = True\nif flag:\n    value = 1\nif value:\n    value = 2\n"
        "match value:\n    case 2:\n        value = 3\n",
    )
    collected = project.run_vfy("coverage", "run", "--decisions", "sample.py")
    collected.expect(0)
    with closing(sqlite3.connect(tmp_path / ".coverage")) as connection, connection:
        if kind == "boolean":
            connection.execute(
                "UPDATE vfy_decision SET source_hash = ? "
                "WHERE key = (SELECT MAX(key) FROM vfy_decision)",
                ("0" * 64,),
            )
        else:
            connection.execute("UPDATE vfy_path_decision SET source_hash = ?", ("0" * 64,))
    output = project.write("report.json", "previous report\n")
    result = project.run_vfy("coverage", "check", "--format", "json", "--output", str(output))
    assert result.returncode == 1
    assert "different source revisions" in result.stderr
    assert result.stdout == ""
    assert output.read_text() == "previous report\n"


def test_semantic_observations_remain_partitioned_by_context(tmp_path: Path) -> None:
    filename = tmp_path / "sample.py"
    filename.write_text("if flag:\n    value = 1\n", encoding="utf-8")
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True
    )
    coverage.start()
    try:
        coverage.switch_context("test_false")
        runpy.run_path(str(filename), run_name="__main__", init_globals={"flag": False})
        coverage.switch_context("test_true")
        runpy.run_path(str(filename), run_name="__main__", init_globals={"flag": True})
    finally:
        coverage.stop()
        coverage.save()

    store = _read(tmp_path / ".coverage")
    observations = store.get_decision_data().observations
    assert {(item.context, item.vector, item.outcome) for item in observations} == {
        ("test_false", "0", False),
        ("test_true", "1", True),
    }
    filtered = store.decision_report(contexts=["test_true"])
    assert filtered.decision_percent == 50.0

    unmatched = store.decision_report(contexts=["does-not-exist"])
    assert unmatched.decision_percent == 0.0
    assert unmatched.rows[0].vectors == ()


def test_named_path_observations_remain_partitioned_by_context(tmp_path: Path) -> None:
    filename = tmp_path / "sample.py"
    filename.write_text(
        "def choose(first, second):\n    return first or second\n",
        encoding="utf-8",
    )
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True
    )
    coverage.start()
    try:
        coverage.switch_context("test_first")
        choose = runpy.run_path(str(filename), run_name="__main__")["choose"]
        choose(1, 2)
        coverage.switch_context("test_second")
        choose(0, 2)
    finally:
        coverage.stop()
        coverage.save()

    store = _read(tmp_path / ".coverage")
    data = store.get_decision_data()
    definition = next(item for item in data.path_definitions if item.kind == "value_or")
    assert {
        (observation.context, observation.outcome)
        for observation in data.path_observations
        if observation.decision == definition.key
    } == {("test_first", 0), ("test_second", 1)}

    filtered = store.decision_report(contexts=["test_second"])
    row = next(item for item in filtered.rows if item.key == definition.key)
    assert row.decisions_covered == 1
    assert row.decisions_total == 2
    assert row.outcomes == (
        ("operand 1 selected", False, ()),
        ("operand 2 selected", True, ("test_second",)),
    )


def test_match_outcomes_remain_partitioned_by_context(tmp_path: Path) -> None:
    filename = tmp_path / "sample.py"
    filename.write_text(
        """\
match value:
    case 1:
        result = "one"
    case _:
        result = "other"
""",
        encoding="utf-8",
    )
    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True
    )
    coverage.start()
    try:
        coverage.switch_context("test_one")
        runpy.run_path(str(filename), run_name="__main__", init_globals={"value": 1})
        coverage.switch_context("test_other")
        runpy.run_path(str(filename), run_name="__main__", init_globals={"value": 2})
    finally:
        coverage.stop()
        coverage.save()

    store = _read(tmp_path / ".coverage")
    data = store.get_decision_data()
    definition = next(item for item in data.path_definitions if item.kind == "match")
    assert {
        (observation.context, observation.outcome)
        for observation in data.path_observations
        if observation.decision == definition.key
    } == {("test_one", 0), ("test_other", 1)}
    filtered = store.decision_report(contexts=["test_other"])
    row = next(item for item in filtered.rows if item.key == definition.key)
    assert row.decisions_covered == 1
    assert row.decisions_total == 2
    assert row.conditions_total == row.mcdc_total == 0


def test_match_outcomes_handle_repeated_nested_and_early_exit_paths(tmp_path: Path) -> None:
    store = collect(
        tmp_path,
        """
def classify(value):
    match value:
        case 1:
            match value:
                case 1:
                    return "nested"
        case _:
            raise LookupError()
    return "other"

for value in (1, 2, 1, 2):
    try:
        classify(value)
    except LookupError:
        pass
""",
    )
    data = store.get_decision_data()
    matches = [definition for definition in data.path_definitions if definition.kind == "match"]
    assert len(matches) == 2
    outer = next(definition for definition in matches if definition.line == 3)
    inner = next(definition for definition in matches if definition.line == 5)
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == outer.key
    } == {0, 1}
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == inner.key
    } == {0}


def _combine(
    project: VfyProject, output: Path, *inputs: Path, keep: bool = True, rcfile: Path | None = None
) -> CommandResult:
    arguments = ["coverage", "combine", "--data-file", str(output)]
    if keep:
        arguments.append("--keep")
    if rcfile is not None:
        arguments += ["--rcfile", str(rcfile)]
    return project.run_vfy(*arguments, *(str(path) for path in inputs))


def test_semantic_shards_union_vectors_in_one_transaction(tmp_path: Path) -> None:
    source = """\
def choose(first, second):
    if first and second:
        return True
    return False

choose(first, second)
"""
    first = tmp_path / ".coverage.first"
    second = tmp_path / ".coverage.second"
    output = tmp_path / ".coverage"
    collect_shard(tmp_path, first, source, run_globals={"first": False, "second": False})
    collect_shard(tmp_path, second, source, run_globals={"first": True, "second": True})
    # The source digest is intentionally the same: only the executed test row
    # differs, so combine should union evidence rather than reject the shards.
    combined = _combine(VfyProject(tmp_path), output, first, second)
    combined.expect(0)
    vectors = {observation.vector for observation in _read(output).get_decision_data().observations}
    assert vectors == {"0-", "11"}
    assert first.exists()
    assert second.exists()


def test_semantic_shards_union_named_outcomes(tmp_path: Path) -> None:
    source = "result = first or second\n"
    first = tmp_path / ".coverage.first"
    second = tmp_path / ".coverage.second"
    output = tmp_path / ".coverage"
    collect_shard(tmp_path, first, source, run_globals={"first": 1, "second": 2})
    collect_shard(tmp_path, second, source, run_globals={"first": 0, "second": 2})

    combined = _combine(VfyProject(tmp_path), output, first, second)
    combined.expect(0)
    data = _read(output).get_decision_data()
    decision = next(item for item in data.path_definitions if item.kind == "value_or")
    assert {
        observation.outcome
        for observation in data.path_observations
        if observation.decision == decision.key
    } == {0, 1}


def _use_unscoped_semantic_keys(database: Path) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        for table, column in (
            ("vfy_decision", "key"),
            ("vfy_condition", "decision"),
            ("vfy_observation", "decision"),
            ("vfy_path_decision", "key"),
            ("vfy_path_outcome", "decision"),
            ("vfy_path_observation", "decision"),
        ):
            keys = connection.execute(f"SELECT DISTINCT {column} FROM {table}").fetchall()
            for (key,) in keys:
                unscoped = "vfy:" + key.rsplit(":", 1)[-1]
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE {column}=?", (unscoped, key)
                )


@pytest.mark.parametrize("operation", ["update", "combine", "append"])
def test_unscoped_semantic_keys_merge_with_fresh_collection(tmp_path: Path, operation: str) -> None:
    source = "selected = flag or False\nif flag:\n    value = 1\n"
    output = tmp_path / ".coverage"
    shard = tmp_path / ".coverage.shard"
    collect_shard(tmp_path, output, source, run_globals={"flag": False})
    _use_unscoped_semantic_keys(output)
    collect_shard(tmp_path, shard, source, run_globals={"flag": True})

    if operation == "update":
        old = CoverageData(str(output))
        old.read()
        fresh = CoverageData(str(shard))
        fresh.read()
        old.update(fresh)
        old.write()
    elif operation == "combine":
        base = tmp_path / ".coverage.base"
        shutil.move(output, base)
        combined = _combine(VfyProject(tmp_path), output, base, shard)
        combined.expect(0)
    else:
        coverage = Coverage(data_file=str(output), source=[str(tmp_path)], decisions=True)
        coverage.load()
        coverage.start()
        try:
            runpy.run_path(str(tmp_path / "sample.py"), init_globals={"flag": True})
        finally:
            coverage.stop()
            coverage.save()

    restored = _read(output)
    data = restored.get_decision_data()
    assert len(data.definitions) == len(data.path_definitions) == 1
    assert {item.decision for item in data.observations} == {data.definitions[0].key}
    assert {item.decision for item in data.path_observations} == {data.path_definitions[0].key}
    report = restored.decision_report()
    assert (report.decisions_covered, report.decisions_total) == (4, 4)


def test_semantic_path_aliases_merge_checkout_roots_without_duplicate_decisions(
    tmp_path: Path,
) -> None:
    source = "value = left or right\nif left and right:\n    value = 1\n"
    first_root = tmp_path / "worker-a"
    second_root = tmp_path / "worker-b"
    canonical_root = tmp_path / "src"
    for root in (first_root, second_root, canonical_root):
        root.mkdir()
    canonical_path = canonical_root / "sample.py"
    _write_text(canonical_path, source)
    first = tmp_path / ".coverage.first"
    second = tmp_path / ".coverage.second"
    output = tmp_path / ".coverage"
    collect_shard(first_root, first, source, run_globals={"left": False, "right": False})
    collect_shard(second_root, second, source, run_globals={"left": True, "right": True})
    rcfile = tmp_path / ".coveragerc"
    rcfile.write_text(
        f"[paths]\nsource =\n    {canonical_root}\n    {first_root}\n    {second_root}\n",
        encoding="utf-8",
    )

    combined = _combine(VfyProject(tmp_path), output, first, second, rcfile=rcfile)
    combined.expect(0)
    restored = _read(output)
    data = restored.get_decision_data()
    assert len(data.definitions) == len(data.path_definitions) == 1
    assert {definition.path for definition in data.definitions} == {str(canonical_path)}
    assert {definition.path for definition in data.path_definitions} == {str(canonical_path)}
    report = restored.decision_report()
    assert {row.kind for row in report.rows} == {"if", "value_or"}
    assert (report.decisions_covered, report.decisions_total) == (4, 4)
    assert (report.mcdc_covered, report.mcdc_total) == (1, 2)


def test_semantic_shards_reject_conflicting_source_digests_atomically(tmp_path: Path) -> None:
    first = tmp_path / ".coverage.first"
    second = tmp_path / ".coverage.second"
    output = tmp_path / ".coverage"
    collect_shard(tmp_path, first, "flag = True\nif flag:\n    value = 1\n")
    collect_shard(tmp_path, second, "other = False\nif other:\n    value = 2\n")
    output.write_bytes(b"sentinel")

    combined = _combine(VfyProject(tmp_path), output, first, second, keep=False)
    combined.expect(1)
    assert "different source revisions" in combined.stderr
    assert output.read_bytes() == b"sentinel"
    assert first.exists()
    assert second.exists()


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_family_semantic_shards_preserve_destination_and_inputs_on_conflict(
    tmp_path: Path, reverse: bool
) -> None:
    first = tmp_path / ".coverage.first"
    second = tmp_path / ".coverage.second"
    output = tmp_path / ".coverage"
    collect_shard(tmp_path, first, "left = True\nif left:\n    pass\n")
    collect_shard(tmp_path, second, "left = True\nvalue = left or False\n")
    shutil.copyfile(first, output)
    before = {path: path.read_bytes() for path in (output, first, second)}
    inputs = [second, first] if reverse else [first, second]

    combined = _combine(VfyProject(tmp_path), output, *inputs, keep=False)
    combined.expect(1)
    assert "different source revisions" in combined.stderr
    assert {path: path.read_bytes() for path in before} == before
    restored = _read(output).get_decision_data()
    assert len(restored.definitions) == 1
    assert len(restored.path_definitions) == 0


@pytest.mark.parametrize("family", ["boolean", "path"])
def test_internally_mixed_semantic_shard_rejects_before_publication(
    tmp_path: Path, family: str
) -> None:
    shard = tmp_path / ".coverage.mixed"
    output = tmp_path / ".coverage"
    collect_shard(tmp_path, shard, "left = True\nvalue = left or False\nif left:\n    pass\n")
    shutil.copyfile(shard, output)
    table = "vfy_decision" if family == "boolean" else "vfy_path_decision"
    with closing(sqlite3.connect(shard)) as connection, connection:
        connection.execute(f"UPDATE {table} SET source_hash = ?", ("0" * 64,))
    before = {path: path.read_bytes() for path in (output, shard)}

    with pytest.raises(DataError, match="different source revisions"):
        _read(shard)
    combined = _combine(VfyProject(tmp_path), output, shard, keep=False)
    combined.expect(1)
    assert "different source revisions" in combined.stderr
    assert {path: path.read_bytes() for path in before} == before
    restored = _read(output).get_decision_data()
    assert len(restored.definitions) == len(restored.path_definitions) == 1


def test_append_replaces_semantic_evidence_for_changed_source(tmp_path: Path) -> None:
    filename = tmp_path / "sample.py"
    filename.write_text("flag = True\nif flag:\n    value = 1\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"

    first = Coverage(data_file=str(data_file), source=[str(tmp_path)], decisions=True)
    first.start()
    runpy.run_path(str(filename), run_name="__main__")
    first.stop()
    first.save()
    original_hash = _read(data_file).get_decision_data().definitions[0].source_hash

    filename.write_text("other = False\nif other:\n    value = 2\n", encoding="utf-8")
    second = Coverage(data_file=str(data_file), source=[str(tmp_path)], decisions=True)
    second.load()
    second.start()
    runpy.run_path(str(filename), run_name="__main__")
    second.stop()
    second.save()
    definitions = _read(data_file).get_decision_data().definitions
    assert len(definitions) == 1
    assert definitions[0].source_hash != original_hash


def test_static_source_model_preserves_definitions_and_ranges(tmp_path: Path) -> None:
    source = "if left and right:\n    pass\ntry:\n    work()\nexcept ValueError:\n    pass\n"
    path = tmp_path / "source.py"
    path.write_text(source, encoding="utf-8", newline="\n")
    empty = Coverage(data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], decisions=True)
    empty.start()
    empty.stop()
    empty.save()

    payload = _semantic_json(_read(tmp_path / ".coverage"), tmp_path, source=[str(path)])

    digest = hashlib.sha256(source.encode()).hexdigest()
    decision, named = payload["files"]
    assert {decision["source_digest"], named["source_digest"]} == {digest}
    assert (decision["path"], decision["line"], decision["kind"]) == (str(path), 1, "if")
    assert (decision["expression"], decision["logic"]) == ("left and right", "and(c0,c1)")
    assert decision["conditions_definition"] == [
        {"index": 0, "line": 1, "column": 3, "end_line": 1, "end_column": 7},
        {"index": 1, "line": 1, "column": 12, "end_line": 1, "end_column": 17},
    ]
    assert (named["path"], named["line"], named["kind"]) == (str(path), 3, "try")
    assert named["expression"] == "try:\n    work()\nexcept ValueError:\n    pass"
    # The try can complete, select its handler, or propagate another exception.
    assert [outcome["label"] for outcome in named["outcomes"]] == [
        "normal completion",
        "handler 1 ValueError",
        "unhandled exception",
    ]
    assert decision["decision"] != named["decision"]
