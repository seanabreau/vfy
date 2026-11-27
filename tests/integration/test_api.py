from __future__ import annotations

import inspect
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from types import FrameType
from typing import override

import coverage as coverage_py
import pytest
from coverage.config import CoverageConfig

from tests.support import VfyProject
from tests.support.reference import reference_coverage
from vfy import Coverage, CoverageData
from vfy.errors import NoDataError, NoSource, NotPython
from vfy.plugin import CoveragePlugin, FileReporter, FileTracer


def _register(registration: object, method: str, plugin: CoveragePlugin) -> None:
    """Register ``plugin`` on Coverage.py's or vfy's plug-in registry, which share method names."""

    getattr(registration, method)(plugin)


@pytest.mark.parametrize("branch", [False, True])
def test_collection_tracks_preimported_imported_and_compiled_code(
    tmp_path: Path, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    source = "def choose(value):\n    if value:\n        return 1\n    return 2\n"
    for name in ("preimported.py", "imported.py", "compiled.py"):
        project.write(name, source)
    project.write(
        "run.py",
        "import importlib\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import preimported\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "cov = Coverage(data_file=sys.argv[2], source=['.'], "
        f"branch={branch!r}, config_file=False)\n"
        "with cov.collect():\n"
        "    import imported\n"
        "    namespace = {}\n"
        "    path = Path('compiled.py').resolve()\n"
        "    exec(compile(path.read_text(), str(path), 'exec'), namespace)\n"
        "    preimported.choose(True)\n"
        "    imported.choose(True)\n"
        "    namespace['choose'](True)\n"
        "with cov.collect():\n"
        "    preimported.choose(False)\n"
        "    imported.choose(False)\n"
        "    namespace['choose'](False)\n"
        "cov.save()\n",
    )
    for tool in ("vfy", "coverage"):
        result = project.run_python("run.py", tool, f".{tool}")
        result.expect(0)
    for name in ("preimported.py", "imported.py", "compiled.py"):
        assert project.lines(name, ".vfy") == project.lines(name, ".coverage")
        measured_lines = project.lines(name, ".vfy")
        assert measured_lines is not None
        assert {2, 3, 4} <= set(measured_lines)
        if branch:
            assert project.arcs(name, ".vfy") == project.arcs(name, ".coverage")


@pytest.mark.parametrize("active_snapshot", [False, True])
def test_multiline_branch_arcs_match_coverage_in_each_context(
    tmp_path: Path, active_snapshot: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "choices.py",
        "def choose(values):\n"
        "    if not values:\n"
        "        return []\n"
        "    return [\n"
        "        value * 2\n"
        "        for value in values\n"
        "        if value > 0\n"
        "    ]\n",
    )
    project.write(
        "run.py",
        "import importlib, sys\n"
        "from choices import choose\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    data_file='.' + sys.argv[1], source=['.'], branch=True, config_file=False)\n"
        "for context, values in [('populated', [1, -1, 2]), ('empty', [])]:\n"
        "    cov.start()\n"
        "    cov.switch_context(context)\n"
        "    choose(values)\n"
        + ("    cov.json_report(outfile='active.json')\n" if active_snapshot else "")
        + "    choose([])\n    cov.stop()\ncov.save()\n",
    )
    for tool in ("coverage", "vfy"):
        result = project.run_python("run.py", tool)
        result.expect(0)
    actual, expected = project.data(".vfy"), project.data(".coverage")
    path = str(tmp_path / "choices.py")
    assert sorted(actual.arcs(path) or ()) == sorted(expected.arcs(path) or ())
    for context in ("populated", "empty"):
        actual.set_query_context(context)
        expected.set_query_context(context)
        assert sorted(actual.arcs(path) or ()) == sorted(expected.arcs(path) or ()), context


@pytest.mark.parametrize("branch", [True, False])
def test_repeated_loop_evidence_returns_in_every_context(tmp_path: Path, branch: bool) -> None:
    """Retired loop jumps and self-arcs run again after each context switch."""
    project = VfyProject(tmp_path)
    project.write(
        "loops.py",
        "def total(values):\n"
        "    result = 0\n"
        "    for value in values:\n"
        "        if value % 2:\n"
        "            result += value\n"
        "    return result\n"
        "def countdown(n):\n"
        "    while n: n -= 1\n"
        "    return n\n"
        "def grid(rows):\n"
        "    return [[cell for cell in range(row)] for row in range(rows)]\n",
    )
    project.write(
        "run.py",
        "import importlib, sys\n"
        "import loops\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        f"    data_file='.' + sys.argv[1], source=['.'], branch={branch}, config_file=False)\n"
        "cov.start()\n"
        "for context in ('first', 'second', 'third'):\n"
        "    cov.switch_context(context)\n"
        "    loops.total([1, 2, 3])\n"
        "    loops.countdown(3)\n"
        "    loops.grid(3)\n"
        "cov.stop()\ncov.save()\n",
    )
    for tool in ("coverage", "vfy"):
        result = project.run_python("run.py", tool)
        result.expect(0)
    actual, expected = project.data(".vfy"), project.data(".coverage")
    path = str(tmp_path / "loops.py")
    # Coverage.py's sysmon core does not re-enable already observed events on
    # a context switch, so only its first context holds the complete run.
    expected.set_query_context("first")
    expected_lines = sorted(expected.lines(path) or ())
    expected_arcs = sorted(expected.arcs(path) or ())
    assert expected_lines
    for context in ("first", "second", "third"):
        actual.set_query_context(context)
        assert sorted(actual.lines(path) or ()) == expected_lines, context
        assert sorted(actual.arcs(path) or ()) == expected_arcs, context


@pytest.mark.parametrize("operation", ["erase", "load"])
def test_reset_discards_branch_edges_before_the_first_snapshot(
    tmp_path: Path, operation: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def choose(flag):\n    if flag:\n        return 1\n    return 2\n")
    project.write(
        "run.py",
        "import importlib, sys\n"
        "from subject import choose\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "cov = tool.Coverage(data_file='.' + sys.argv[1], source=['.'],\n"
        "                    branch=True, config_file=False)\n"
        "cov.start()\n"
        "assert choose(True) == 1\n"
        f"cov.{operation}()\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    for tool in ("vfy", "coverage"):
        result = project.run_python("run.py", tool)
        result.expect(0)
        assert not project.arcs("subject.py", "." + tool)


def test_evaluating_function_annotations_preserves_coverage_branch_data(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "annotated.py",
        "def choose(value: int) -> int:\n    return value\n"
        "assert choose.__annotations__ == {'value': int, 'return': int}\n"
        "assert choose(1) == 1\n",
    )
    expected = project.run_coverage_py("run", "--branch", "annotated.py")
    actual = project.run_vfy(
        "coverage", "run", "--branch", "annotated.py", env={"COVERAGE_FILE": ".vfy"}
    )
    assert expected.returncode == actual.returncode == 0, actual.stderr + expected.stderr
    assert project.arcs("annotated.py", ".vfy") == project.arcs("annotated.py", ".coverage")


@pytest.mark.parametrize("class_name", ["Coverage", "CoverageData"])
def test_public_method_surface_covers_coverage_py(class_name: str) -> None:
    reference = getattr(coverage_py, class_name)
    implementation = globals()[class_name]
    reference_methods = {name for name in dir(reference) if not name.startswith("_")}
    implementation_methods = {name for name in dir(implementation) if not name.startswith("_")}
    assert reference_methods <= implementation_methods


def test_coverage_constructor_and_public_method_parameter_names_match() -> None:
    assert list(inspect.signature(Coverage).parameters) == list(
        inspect.signature(coverage_py.Coverage).parameters
    )
    for name in sorted(
        method
        for method in dir(coverage_py.Coverage)
        if not method.startswith("_") and callable(getattr(coverage_py.Coverage, method))
    ):
        expected = list(inspect.signature(getattr(coverage_py.Coverage, name)).parameters)
        actual = list(inspect.signature(getattr(Coverage, name)).parameters)
        assert actual == expected, name


def test_report_observes_active_collection_without_stopping(tmp_path: Path) -> None:
    source = tmp_path / "active.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    coverage = Coverage(data_file=None, source=[str(tmp_path)])

    coverage.start()
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), {})
    output = StringIO()
    assert coverage.report(file=output) == 100.0
    assert coverage._collector is not None
    assert coverage.get_data().lines(str(source)) == [1]
    coverage.stop()
    assert coverage.get_data().lines(str(source)) == [1]


def test_active_data_checkpoints_preserve_subsequent_contexts(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "first = 1\nsecond = 2\n")
    project.write(
        "run.py",
        """import importlib, json, sys
from pathlib import Path
tool = importlib.import_module(sys.argv[1])
path = str(Path('subject.py').resolve())
cov = tool.Coverage(data_file='.' + sys.argv[1], source=['.'], config_file=False)
cov.start()
exec(compile('first = 1\\n', path, 'exec'), {})
first = cov.get_data().lines(path)
cov.save()
saved = tool.CoverageData('.' + sys.argv[1]); saved.read()
checkpoint = saved.lines(path)
cov.switch_context('second')
exec(compile('\\nsecond = 2\\n', path, 'exec'), {})
cov.save()
cov.stop()
print(json.dumps([first, checkpoint, cov.get_data().contexts_by_lineno(path)]))
""",
    )
    results = []
    for tool in ("vfy", "coverage"):
        result = project.run_python("run.py", tool)
        result.expect(0)
        results.append(json.loads(result.stdout))
    assert results[0] == results[1] == [[1], [1], {"1": [""], "2": ["second"]}]


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("operation", ["purge", "erase", "load"])
def test_active_checkpoint_does_not_restore_cleared_measurements(
    tmp_path: Path, branch: bool, operation: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "def hit():\n    return 1\n")
    project.write(
        "run.py",
        """import importlib, json, sys
from pathlib import Path
tool = importlib.import_module(sys.argv[1])
path = str(Path('subject.py').resolve())
cov = tool.Coverage(data_file='.' + sys.argv[1], config_file=False,
                    source=['.'], branch=sys.argv[3] == 'True')
if sys.argv[2] == 'load':
    baseline = tool.CoverageData('.' + sys.argv[1])
    if sys.argv[3] == 'True':
        baseline.add_arcs({'baseline.py': [(-1, 1), (1, -1)]})
    else:
        baseline.add_lines({'baseline.py': [1]})
    baseline.write()
cov.start()
import subject
subject.hit()
if sys.argv[2] == 'purge':
    cov.get_data().purge_files([path])
elif sys.argv[2] == 'erase':
    cov.get_data()
    cov.erase()
else:
    cov.load()
cleared = cov.get_data().lines(path)
if sys.argv[2] != 'purge':
    cov.stop()
    print(json.dumps([cleared]))
    raise SystemExit(0)
subject.hit()
first = cov.get_data().lines(path)
cov.switch_context('second')
subject.hit()
second = cov.get_data().contexts_by_lineno(path)
cov.stop()
normalized = {str(line): sorted(contexts) for line, contexts in second.items()}
print(json.dumps([cleared, first, normalized]))
""",
    )
    results = []
    for tool in ("vfy", "coverage"):
        result = project.run_python("run.py", tool, operation, str(branch))
        result.expect(0)
        results.append(json.loads(result.stdout))
    assert results[0][0] == results[1][0]
    if operation == "purge":
        # Coverage.py's sysmon core leaves previously disabled events disabled
        # after purge. vfy must still observe subsequent hits and contexts.
        assert results[0] == [[], [2], {"2": ["", "second"]}]
    else:
        assert results[0] == [None]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork requires POSIX")
def test_memory_only_collector_remains_memory_only_after_fork(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "run.py",
        """import importlib, os, sys
from pathlib import Path
tool = importlib.import_module(sys.argv[1])
cov = tool.Coverage(data_file=None, config_file=False, source=['.'])
cov.start()
pid = os.fork()
if pid == 0:
    cov.stop()
    cov.save()
    os._exit(0)
_, status = os.waitpid(pid, 0)
assert os.waitstatus_to_exitcode(status) == 0
cov.stop()
cov.save()
assert list(Path('.').glob('.coverage*')) == []
""",
    )
    for tool in ("vfy", "coverage"):
        result = project.run_python("run.py", tool)
        result.expect(0)


def test_live_checkpoints_preserve_incomplete_semantic_evaluations(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def choose(left, checkpoint, right):\n"
        "    if left and checkpoint() and right:\n"
        "        return 1\n"
        "    return 2\n",
    )
    project.write(
        "run.py",
        """from pathlib import Path
from vfy import Coverage
path = str(Path('subject.py').resolve())
cov = Coverage(data_file=None, config_file=False, source=[path], decisions=True)
def checkpoint():
    cov.get_data()
    return True
cov.start()
import subject
subject.choose(True, checkpoint, False)
assert {row.vector for row in cov.get_decision_data().observations} == {'110'}
cov.get_data().purge_files([path])
assert not cov.get_decision_data().definitions
assert not cov.get_decision_data().observations
subject.choose(True, checkpoint, True)
assert {row.vector for row in cov.get_decision_data().observations} == {'111'}
cov.stop()
""",
    )
    result = project.run_python("run.py")
    result.expect(0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork requires POSIX")
def test_forked_process_analyzes_while_another_thread_analyzes(tmp_path: Path) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write("subject.py", "value = 1\n")
    project.write(
        "fork_cache.py",
        "import os\n"
        "import signal\n"
        "import threading\n"
        "from pathlib import Path\n"
        "from vfy import Coverage\n"
        "path = str(Path('subject.py').resolve())\n"
        "def analyze():\n"
        "    return Coverage(data_file=None, config_file=False).analysis2(path)[1]\n"
        "assert analyze() == [1]\n"
        "stop = threading.Event()\n"
        "def analyze_repeatedly():\n"
        "    while not stop.is_set():\n"
        "        analyze()\n"
        "worker = threading.Thread(target=analyze_repeatedly)\n"
        "worker.start()\n"
        "try:\n"
        "    for _ in range(8):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            signal.alarm(5)\n"
        "            assert analyze() == [1]\n"
        "            os._exit(0)\n"
        "        _, status = os.waitpid(pid, 0)\n"
        "        assert os.waitstatus_to_exitcode(status) == 0\n"
        "finally:\n"
        "    stop.set()\n"
        "    worker.join()\n",
    )

    result = project.run_python("fork_cache.py")

    result.expect(0)


def test_failed_live_checkpoint_retains_pending_evidence_for_retry(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "first = 1\nsecond = 2\n")
    project.write(
        "run.py",
        """from vfy import Coverage
from pathlib import Path
path = str(Path('subject.py').resolve())
cov = Coverage(data_file=None, config_file=False, source=[path], branch=True)
data = cov.get_data()
data.add_lines({'seed.py': [1]})
cov.start()
exec(compile(Path(path).read_text(), path, 'exec'), {})
try:
    cov.get_data()
except Exception as error:
    assert type(error).__name__ == 'DataError', error
else:
    raise AssertionError('mixed modes must fail')
data.erase()
recovered = cov.get_data().lines(path)
cov.stop()
assert recovered == [1, 2], recovered
""",
    )
    result = project.run_python("run.py")
    result.expect(0)


@pytest.mark.parametrize("operation", ["load", "erase"])
def test_failed_data_lifecycle_operation_preserves_pending_measurements(
    tmp_path: Path, operation: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", "first = 1\nsecond = 2\n")
    project.write(
        "run.py",
        """import sys
from pathlib import Path
from vfy import Coverage
from vfy.errors import DataError
operation = sys.argv[1]
path = str(Path('subject.py').resolve())
database = Path('.vfy')
if operation == 'load':
    database.write_bytes(b'not a SQLite database')
    expected_error = DataError
else:
    database.mkdir()
    expected_error = OSError
cov = Coverage(data_file=str(database), config_file=False, source=[path])
original = cov.get_data()
original.add_lines({'seed.py': [9]})
cov.start()
exec(compile(Path(path).read_text(), path, 'exec'), {})
try:
    getattr(cov, operation)()
except expected_error:
    pass
else:
    raise AssertionError('invalid data destination must fail')
data = cov.get_data()
assert data is original
assert data.lines('seed.py') == [9]
assert data.lines(path) == [1, 2], data.lines(path)
cov.stop()
if operation == 'load':
    assert database.read_bytes() == b'not a SQLite database'
else:
    assert database.is_dir()
""",
    )
    result = project.run_python("run.py", operation)
    result.expect(0)


def test_explicit_memory_only_coverage_preserves_existing_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ExitStack() as resources:
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / ".coverage"
        existing.write_bytes(b"original database")
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(data_file=None, config_file=False)
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            coverage.get_data().add_lines({"subject.py": {1}})
            coverage.save()
            assert existing.read_bytes() == b"original database"


def test_serialized_worker_data_preserves_semantic_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        """def choose(value):
    if value:
        return 1
    return 2
def named(value):
    match value:
        case 1:
            return True
        case _:
            return False
""",
    )
    project.write(
        "run.py",
        """from pathlib import Path
from vfy import Coverage, CoverageData
cov = Coverage(data_file='.original', source=['.'], config_file=False, decisions=True)
with cov.collect():
    import subject
    cov.switch_context('first')
    subject.choose(True); subject.named(1)
    cov.switch_context('second')
    subject.choose(False); subject.named(2)
before = cov.get_decision_data()
assert before.definitions and before.observations and before.path_definitions
copy = CoverageData('.restored')
copy.loads(cov.get_data().dumps())
copy.write()
restored = Coverage(data_file='.restored', config_file=False, decisions=True)
restored.load()
after = restored.get_decision_data()
assert (before.definitions, before.observations, before.path_definitions,
        before.path_observations, before.evidence_records()) == (
        after.definitions, after.observations, after.path_definitions,
        after.path_observations, after.evidence_records())
""",
    )
    result = project.run_python("run.py")
    result.expect(0)


def test_api_reports_use_configured_defaults_and_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ExitStack() as resources:
        monkeypatch.chdir(tmp_path)
        source = tmp_path / "subject.py"
        source.write_text("first = 1\nsecond = 2\nthird = 3\n")
        config = tmp_path / ".coveragerc"
        config.write_text(
            "[report]\nprecision = 2\nshow_missing = true\nignore_errors = true\n"
            "skip_covered = true\nskip_empty = true\n"
            "[json]\noutput = result.json\npretty_print = true\nshow_contexts = true\n"
            "[xml]\noutput = result.xml\n[lcov]\noutput = result.lcov\n"
            "[html]\ndirectory = result-html\ntitle = Configured title\n"
        )
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(data_file=None, config_file=str(config))
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            assert [
                coverage.get_option("report:" + option)
                for option in (
                    "precision",
                    "show_missing",
                    "ignore_errors",
                    "skip_covered",
                    "skip_empty",
                )
            ] == [2, True, True, True, True]
            coverage.get_data().set_context("first test")
            coverage.get_data().add_lines({str(source): {1}})
            coverage.json_report()
            report = json.loads((tmp_path / "result.json").read_text())
            assert report["meta"]["show_contexts"] is True
            assert report["totals"]["percent_covered_display"] == "33.33"
            coverage.xml_report()
            coverage.lcov_report()
            coverage.html_report()
            assert (tmp_path / "result.xml").is_file()
            assert (tmp_path / "result.lcov").is_file()
            assert "Configured title" in (tmp_path / "result-html" / "index.html").read_text()
            coverage.json_report(outfile="override.json", show_contexts=False)
            assert not json.loads((tmp_path / "override.json").read_text())["meta"]["show_contexts"]


@pytest.mark.parametrize("html_skip_covered", [False, True])
def test_html_skip_covered_config_matches_coverage_py(
    tmp_path: Path, html_skip_covered: bool
) -> None:
    with ExitStack() as resources:
        root = tmp_path.resolve()
        full = root / "full.py"
        partial = root / "partial.py"
        empty = root / "empty.py"
        full.write_text("value = 1\n", encoding="utf-8")
        partial.write_text("value = 2\n", encoding="utf-8")
        empty.write_text("# no statements\n", encoding="utf-8")
        config = root / ".coveragerc"
        config.write_text(
            "[report]\nskip_covered = true\n"
            f"[html]\nskip_covered = {str(html_skip_covered).lower()}\nskip_empty = true\n",
            encoding="utf-8",
        )

        results = []
        for implementation in (Coverage, coverage_py.Coverage):
            reporter = implementation(data_file=None, config_file=str(config))
            if isinstance(reporter, coverage_py.Coverage):
                resources.callback(reporter.get_data().close, force=True)
            reporter.get_data().add_lines({str(full): {1}, str(partial): set(), str(empty): set()})
            directory = root / implementation.__module__
            percent = reporter.html_report(directory=str(directory))
            index = (directory / "index.html").read_text(encoding="utf-8")
            results.append(
                (percent, "full.py" in index, "partial.py" in index, "empty.py" in index)
            )

        assert results == [(50.0, not html_skip_covered, True, False)] * 2


@pytest.mark.parametrize(
    ("setting", "pattern"),
    [
        ("exclude_lines", "custom"),
        ("exclude_lines", ""),
        ("exclude_also", "custom"),
    ],
)
def test_configured_exclusions_replace_or_extend_defaults(
    tmp_path: Path, setting: str, pattern: str
) -> None:
    with ExitStack() as resources:
        source = tmp_path / "subject.py"
        source.write_text("first = 1\nsecond = 2  # pragma: no cover\nthird = 3 # custom\n")
        config = tmp_path / ".coveragerc"
        config.write_text(f"[report]\n{setting} = {pattern}\n")
        results = []
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(data_file=None, config_file=str(config))
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            coverage.get_data().add_lines({str(source): {1}})
            results.append(coverage.analysis2(str(source))[1:])
        assert results[0] == results[1]


@pytest.mark.parametrize(
    "configuration",
    [
        "[report]\n",
        "[report]\nexclude_lines =\n    pragma: no cover\n    @overload\n",
        "[report]\nexclude_lines =\n",
        "[report]\nexclude_also =\n    @overload\n",
    ],
    ids=["default", "replacement", "empty", "additive"],
)
def test_configured_ellipsis_gap_exclusions_match_coverage_py(
    tmp_path: Path, configuration: str
) -> None:
    source = tmp_path / "contracts.py"
    source.write_text(
        "from typing import overload\n\n"
        "class Contract:\n"
        "    def pragma(\n"
        "        self,\n"
        "    ) -> str:  # pragma: no cover\n"
        "        ...\n\n"
        "# after pragma\n"
        "value = 1\n\n"
        "@overload\n"
        "def overloaded(\n"
        "    value: str,\n"
        ") -> str:\n"
        "    ...\n\n"
        "# after overload\n"
        "def overloaded(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    config = tmp_path / ".coveragerc"
    config.write_text(configuration, encoding="utf-8")

    results = []
    with ExitStack() as resources:
        for implementation in (Coverage, coverage_py.Coverage):
            instance = implementation(data_file=None, config_file=str(config))
            if isinstance(instance, coverage_py.Coverage):
                resources.callback(instance.get_data().close, force=True)
            instance.get_data().add_lines({str(source): set(range(1, 21))})
            results.append(instance.analysis2(str(source))[1:])

    assert results[0] == results[1]


@pytest.mark.parametrize("which", ["exclude", "partial"])
def test_exclusion_mutators_and_options_share_the_effective_list(which: str) -> None:
    with ExitStack() as resources:
        option = "report:exclude_lines" if which == "exclude" else "report:partial_branches"
        results = []
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(data_file=None, config_file=False)
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            coverage.clear_exclude(which)
            cleared = coverage.get_option(option)
            assert isinstance(cleared, list)
            cleared = list(cleared)
            coverage.exclude("custom", which)
            added = coverage.get_option(option)
            assert isinstance(added, list)
            added = list(added)
            coverage.set_option(option, ["replacement"])
            replaced = coverage.get_exclude_list(which)
            results.append((cleared, added, replaced))
        assert results[0] == results[1] == ([], ["custom"], ["replacement"])


@pytest.mark.parametrize("which", ["exclude", "partial"])
def test_configurers_replace_additive_exclusions_in_effective_config(
    tmp_path: Path, which: str
) -> None:
    with ExitStack() as resources:
        option = "exclude_lines" if which == "exclude" else "partial_branches"
        additive = "exclude_also" if which == "exclude" else "partial_also"
        config = tmp_path / ".coveragerc"
        config.write_text(f"[report]\n{additive} = extra\n")

        class Configurer(CoveragePlugin):
            @override
            def configure(self, config: object) -> None:
                assert isinstance(config, (Coverage, coverage_py.Coverage, CoverageConfig))
                config.set_option("report:" + option, ["custom"])

        def initialize(plugins: object) -> None:
            _register(plugins, "add_configurer", Configurer())

        results = []
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(data_file=None, config_file=str(config), plugins=[initialize])
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            results.append(coverage.get_exclude_list(which))
        assert results[0] == results[1] == ["custom"]


@pytest.mark.parametrize("patterns", [list, tuple, set, frozenset])
@pytest.mark.parametrize("configured", [False, True])
def test_report_filter_setters_change_rendered_files_and_contexts(
    tmp_path: Path, configured: bool, patterns: Callable[[Iterable[str]], Iterable[str]]
) -> None:
    with ExitStack() as resources:
        first = tmp_path / "first.py"
        second = tmp_path / "second.py"
        first.write_text("first = 1\nsecond = 2\n")
        second.write_text("value = 1\n")
        config = tmp_path / ".coveragerc"
        config.write_text("[report]\ninclude = */*.py\nomit = */unused.py\ncontexts = ignored\n")
        results = []
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(
                data_file=None, config_file=str(config) if configured else False
            )
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            data = coverage.get_data()
            data.set_context("selected")
            data.add_lines({str(first): [1], str(second): [1]})
            data.set_context("other")
            data.add_lines({str(first): [2]})
            coverage.set_option("report:include", patterns(["*/first.py", "*/second.py"]))
            coverage.set_option("report:omit", patterns(["*/second.py"]))
            coverage.set_option("report:contexts", ["selected"])
            output = tmp_path / f"{implementation.__module__}.json"
            coverage.json_report(outfile=str(output))
            report = json.loads(output.read_text())
            results.append(
                {Path(path).name: file["executed_lines"] for path, file in report["files"].items()}
            )
        assert results[0] == results[1] == {"first.py": [1]}


@pytest.mark.parametrize("setting", ["partial_branches", "partial_also", "clear"])
def test_configured_partial_branches_replace_or_extend_defaults(
    tmp_path: Path, setting: str
) -> None:
    with ExitStack() as resources:
        source = tmp_path / "subject.py"
        source.write_text(
            "def choose(value):\n    if value: # pragma: no branch\n"
            "        return 1\n    return 2\n"
        )
        config = tmp_path / ".coveragerc"
        config.write_text("[report]\n" if setting == "clear" else f"[report]\n{setting} = custom\n")
        results = []
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(data_file=None, config_file=str(config), branch=True)
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            if setting == "clear":
                coverage.clear_exclude("partial")
            coverage.get_data().add_arcs(
                {str(source): {(-1, 1), (1, -1), (-1, 2), (2, 3), (3, -1)}}
            )
            results.append(coverage.report(file=StringIO()))
        assert results[0] == results[1]


def test_html_options_render_precision_contexts_and_css_before_writing(tmp_path: Path) -> None:
    with ExitStack() as resources:
        source = tmp_path / "subject.py"
        source.write_text("first = 1\nsecond = 2\nthird = 3\n")
        css = tmp_path / "custom.css"
        css.write_text("body { color: red; }")
        for implementation in (Coverage, coverage_py.Coverage):
            coverage = implementation(data_file=None, config_file=False)
            if isinstance(coverage, coverage_py.Coverage):
                resources.callback(coverage.get_data().close, force=True)
            coverage.get_data().set_context("test <context>")
            coverage.get_data().add_lines({str(source): {1}})
            directory = tmp_path / implementation.__module__
            coverage.html_report(
                directory=str(directory), precision=2, show_contexts=True, extra_css=str(css)
            )
            assert "33.33%" in (directory / "index.html").read_text()
            assert any(
                "body { color: red; }" in path.read_text() for path in directory.glob("*.css")
            )
            assert any(
                "test" in path.read_text() and "context" in path.read_text()
                for path in directory.glob("*.html")
                if path.name != "index.html"
            )
        index = tmp_path / Coverage.__module__ / "index.html"
        original = index.read_bytes()
        coverage = Coverage(data_file=None, config_file=False)
        coverage.get_data().add_lines({str(source): {1}})
        with pytest.raises(OSError, match="CSS"):
            coverage.html_report(
                directory=str(index.parent), extra_css=str(tmp_path / "missing.css")
            )
        assert index.read_bytes() == original


def test_report_uses_unsaved_in_memory_data(tmp_path: Path) -> None:
    source = tmp_path / "unsaved.py"
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    coverage = Coverage(data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)])

    coverage.start()
    exec(compile("first = 1\n", str(source), "exec"), {})
    coverage.stop()
    output = StringIO()
    assert coverage.report(file=output, show_missing=True) == 50.0
    assert "2" in output.getvalue()


def test_public_report_does_not_turn_missing_source_into_full_coverage(tmp_path: Path) -> None:
    missing = tmp_path / "deleted.py"
    vfy = Coverage(data_file=None, config_file=False)
    vfy.data.add_lines({str(missing): {1}})
    with reference_coverage(config_file=False) as reference:
        reference.get_data().add_lines({str(missing): {1}})

        with pytest.raises(NoSource) as actual:
            vfy.report(file=StringIO())
        with pytest.raises(coverage_py.exceptions.NoSource) as oracle:
            reference.report(file=StringIO())
        assert str(actual.value) == str(oracle.value)

        vfy_output = StringIO()
        with pytest.raises(NoDataError, match="No data to report"):
            vfy.report(file=vfy_output, ignore_errors=True)
        reference_output = StringIO()
        with (
            pytest.warns(coverage_py.exceptions.CoverageWarning),
            pytest.raises(coverage_py.exceptions.NoDataError, match="No data to report"),
        ):
            reference.report(file=reference_output, ignore_errors=True)
        assert vfy_output.getvalue() == reference_output.getvalue() == ""

        present = tmp_path / "present.py"
        present.write_text("value = 1\n", encoding="utf-8")
        vfy.data.add_lines({str(present): {1}})
        reference.get_data().add_lines({str(present): {1}})
        vfy_output = StringIO()
        reference_output = StringIO()
        assert vfy.report(file=vfy_output, ignore_errors=True) == 100.0
        with pytest.warns(coverage_py.exceptions.CoverageWarning):
            assert reference.report(file=reference_output, ignore_errors=True) == 100.0
        for output in (vfy_output.getvalue(), reference_output.getvalue()):
            assert str(present) in output
            assert str(missing) not in output


@pytest.mark.parametrize("method_name", ["analysis2", "branch_stats"])
def test_public_analysis_rejects_missing_and_malformed_sources(
    tmp_path: Path, method_name: str
) -> None:
    missing = tmp_path / "deleted.py"
    vfy = Coverage(data_file=None, config_file=False)
    with reference_coverage(config_file=False) as reference:
        with pytest.raises(NoSource) as actual:
            getattr(vfy, method_name)(str(missing))
        with pytest.raises(coverage_py.exceptions.NoSource) as oracle:
            getattr(reference, method_name)(str(missing))
        assert str(actual.value) == str(oracle.value)

        unreadable = tmp_path / "directory.py"
        unreadable.mkdir()
        with pytest.raises(NoSource):
            getattr(vfy, method_name)(str(unreadable))
        with pytest.raises(coverage_py.exceptions.NoSource):
            getattr(reference, method_name)(str(unreadable))

        malformed = tmp_path / "broken.py"
        malformed.write_text("def broken(:\n", encoding="utf-8")
        with pytest.raises(NotPython, match="as Python source"):
            getattr(vfy, method_name)(str(malformed))
        with pytest.raises(coverage_py.exceptions.NotPython):
            getattr(reference, method_name)(str(malformed))


def test_switch_context_reenables_line_events(tmp_path: Path) -> None:
    source = tmp_path / "contexts.py"
    source.write_text("value = 1\n", encoding="utf-8")
    code = compile(source.read_text(encoding="utf-8"), str(source), "exec")
    coverage = Coverage(data_file=None, source=[str(tmp_path)])

    coverage.start()
    coverage.switch_context("first")
    exec(code, {})
    coverage.switch_context("second")
    exec(code, {})
    coverage.stop()

    assert coverage.get_data().contexts_by_lineno(str(source)) == {1: ["first", "second"]}


@pytest.mark.skipif(
    not hasattr(sys, "monitoring")
    or not hasattr(sys.monitoring.events, "BRANCH_LEFT")
    or not hasattr(sys.monitoring.events, "BRANCH_RIGHT"),
    reason="independently disableable branch events require CPython 3.14+",
)
def test_switch_context_reenables_branch_events(tmp_path: Path) -> None:
    source = tmp_path / "branch_contexts.py"
    source.write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 2\n",
        encoding="utf-8",
    )
    code = compile(source.read_text(encoding="utf-8"), str(source), "exec")
    coverage = Coverage(data_file=None, source=[str(tmp_path)], branch=True)

    coverage.start()
    namespace = {}
    coverage.switch_context("first")
    exec(code, namespace)
    namespace["choose"](True)
    coverage.switch_context("second")
    namespace["choose"](False)
    coverage.stop()

    data = coverage.get_data()
    assert data.contexts_by_lineno(str(source)) == {
        1: ["first"],
        2: ["first", "second"],
        3: ["first"],
        4: ["second"],
    }

    data.set_query_context("first")
    assert set(data.arcs(str(source)) or ()) == {
        (1, -1),
        (1, 1),
        (2, 2),
        (2, 3),
        (3, -1),
        (3, 3),
    }
    data.set_query_context("second")
    assert set(data.arcs(str(source)) or ()) == {
        (2, 2),
        (2, 4),
        (4, -1),
        (4, 4),
    }


@pytest.mark.skipif(
    not hasattr(sys, "monitoring")
    or not hasattr(sys.monitoring.events, "BRANCH_LEFT")
    or not hasattr(sys.monitoring.events, "BRANCH_RIGHT"),
    reason="independently disableable branch events require CPython 3.14+",
)
def test_restarting_branch_collector_reenables_events(tmp_path: Path) -> None:
    source = tmp_path / "restart.py"
    source.write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 2\n",
        encoding="utf-8",
    )
    path = str(source.resolve())
    code = compile(source.read_text(encoding="utf-8"), path, "exec")
    namespace = {}
    coverage = Coverage(data_file=None, source=[str(tmp_path)], branch=True)

    coverage.start()
    exec(code, namespace)
    namespace["choose"](True)
    coverage.stop()

    coverage.start()
    namespace["choose"](False)
    coverage.stop()

    assert set(coverage.get_data().arcs(path) or ()) >= {
        (2, 3),
        (2, 4),
    }


@pytest.mark.skipif(
    sys.version_info < (3, 14)
    or os.getenv("COVERAGE_CORE") not in (None, "", "sysmon")
    or not hasattr(sys, "monitoring")
    or not hasattr(sys.monitoring.events, "BRANCH_LEFT")
    or not hasattr(sys.monitoring.events, "BRANCH_RIGHT"),
    reason="direct branch monitoring with independently disableable events requires CPython 3.14+",
)
def test_branch_monitoring_disables_observed_locations(tmp_path: Path) -> None:
    source = tmp_path / "repeated.py"
    source.write_text(
        "def run(count):\n"
        "    total = 0\n"
        "    for value in range(count):\n"
        "        if value % 2:\n"
        "            total += value\n"
        "        else:\n"
        "            total -= value\n"
        "    return total\n",
        encoding="utf-8",
    )
    coverage = Coverage(data_file=None, source=[str(tmp_path)], branch=True)
    coverage.start()

    monitoring = sys.monitoring
    tool_id = monitoring.COVERAGE_ID
    callbacks = {}
    counts = Counter()
    for event_name in ("LINE", "BRANCH_LEFT", "BRANCH_RIGHT", "PY_RETURN"):
        event = getattr(monitoring.events, event_name)

        def counting_callback(*args: object, event_name: str = event_name) -> object:
            if args and getattr(args[0], "co_filename", None) == str(source):
                counts[event_name] += 1
            previous = callbacks[event_name]
            return previous(*args) if previous is not None else None

        callbacks[event_name] = monitoring.register_callback(tool_id, event, counting_callback)

    namespace = {}
    code = compile(source.read_text(encoding="utf-8"), str(source), "exec")
    try:
        exec(code, namespace)
        namespace["run"](8)
        first_run_counts = counts.copy()
        namespace["run"](100_000)
    finally:
        coverage.stop()

    assert counts == first_run_counts


def test_relative_files_records_paths_from_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "product.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    config = tmp_path / "pyproject.toml"
    config.write_text(
        '[tool.coverage.run]\nsource = ["."]\nrelative_files = true\n',
        encoding="utf-8",
    )
    measured = Coverage(data_file=None, config_file=str(config))

    measured.start()
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), {})
    measured.stop()

    assert measured.get_data().measured_files() == {"product.py"}
    assert measured.get_data().lines("product.py") == [1]


@pytest.mark.parametrize(
    ("parallel", "data_suffix", "collected"),
    [
        (False, None, False),
        (True, None, False),
        (False, True, False),
        (False, "worker", False),
        (True, False, False),
        (False, True, True),
    ],
)
def test_erase_matches_coverage_py_for_parallel_settings_and_shards(
    tmp_path: Path, parallel: bool, data_suffix: str | bool | None, *, collected: bool
) -> None:
    def erase(factory: type[coverage_py.Coverage | Coverage], name: str) -> list[str]:
        root = tmp_path / name
        root.mkdir()
        base = root / ".coverage"
        config = root / "coverage.ini"
        config.write_text(f"[run]\nparallel = {parallel}\n", encoding="utf-8")
        for data_name in (base.name, ".coverage.shard"):
            data = coverage_py.CoverageData(str(root / data_name))
            data.add_lines({"a.py": {1}})
            data.write()
            data.close(force=True)
        coverage = factory(
            data_file=str(base),
            data_suffix=data_suffix,
            config_file=str(config),
            source=[str(root)],
        )
        if collected:
            source = root / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            with coverage.collect():
                exec(compile(source.read_bytes(), str(source), "exec"), {})
            coverage.save()
            assert len(list(root.glob(".coverage*"))) == 3
        coverage.erase()
        return sorted(path.name for path in root.glob(".coverage*"))

    assert erase(Coverage, "vfy") == erase(coverage_py.Coverage, "reference")


@pytest.mark.parametrize(
    ("setting", "data_suffix", "expected_shard"),
    [
        ("parallel = true", None, True),
        ('concurrency = ["multiprocessing"]', None, True),
        ("parallel = true", False, False),
    ],
)
def test_parallel_settings_choose_the_collection_file_like_coverage_py(
    tmp_path: Path, setting: str, data_suffix: bool | None, expected_shard: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pyproject.toml", f"[tool.coverage.run]\n{setting}\n")
    project.write(
        "collect.py",
        "import importlib\n"
        "import sys\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "suffix = {'none': None, 'false': False}[sys.argv[3]]\n"
        "coverage = Coverage(data_file=sys.argv[2], data_suffix=suffix)\n"
        "coverage.start()\n"
        "value = 1\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )

    suffix = "none" if data_suffix is None else "false"
    for tool in ("coverage", "vfy"):
        completed = project.run_python("collect.py", tool, f".{tool}-data", suffix)
        completed.expect(0)
        assert (tmp_path / f".{tool}-data").exists() is not expected_shard
        assert len(list(tmp_path.glob(f".{tool}-data.*"))) == int(expected_shard)


def test_loaded_api_report_keeps_rust_data_handle(tmp_path: Path) -> None:
    source = tmp_path / "loaded.py"
    source.write_text("value = 1\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    data = CoverageData(str(data_file))
    data.add_lines({str(source): {1}})
    data.write()
    coverage = Coverage(data_file=str(data_file), config_file=False)
    coverage.load()

    assert coverage.data._store.rust_data is not None
    assert coverage.data._store.native_data is coverage.data._store.rust_data
    assert coverage.report(file=StringIO()) == 100.0
    assert coverage.data._store.rust_data is not None


def test_source_directory_reports_unimported_python_files(tmp_path: Path) -> None:
    executed = tmp_path / "executed.py"
    unimported = tmp_path / "unimported.py"
    executed.write_text("executed = 1\n", encoding="utf-8")
    unimported.write_text("unimported = 1\n", encoding="utf-8")
    coverage = Coverage(data_file=None, source=[str(tmp_path)])
    with coverage.collect():
        exec(compile("executed = 1\n", str(executed), "exec"), {})

    output = StringIO()
    assert coverage.report(file=output) == 50.0
    assert str(unimported) in output.getvalue()


def test_unsupported_api_arguments_fail_instead_of_being_ignored() -> None:
    with pytest.raises(TypeError):
        Coverage(unsupported_option=True)  # ty: ignore[unknown-argument] - exercise runtime rejection
    with pytest.raises(TypeError):
        CoverageData(unsupported_option=True)  # ty: ignore[unknown-argument] - exercise runtime rejection


def test_rust_collection_composes_source_and_omit(tmp_path: Path) -> None:
    keep = tmp_path / "keep.py"
    omit = tmp_path / "omit.py"
    outside = tmp_path.parent / "outside.py"
    for path in (keep, omit, outside):
        path.write_text("answer = 42\n", encoding="utf-8")

    coverage = Coverage(
        data_file=None,
        source=[str(tmp_path)],
        omit=["*/omit.py"],
    )
    coverage.start()
    for path in (keep, omit, outside):
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), {})
    coverage.stop()

    assert coverage.get_data().measured_files() == {str(keep)}


def test_analysis_exclusions_options_and_collection_context_manager(tmp_path: Path) -> None:
    source = tmp_path / "surface.py"
    source.write_text("first = 1\nsecond = 2  # project: ignore\n", encoding="utf-8")
    coverage = Coverage(data_file=None, source=[str(tmp_path)])
    coverage.exclude(r"# project: ignore")

    with coverage.collect():
        assert Coverage.current() is coverage
        exec(compile("first = 1\n", str(source), "exec"), {})

    assert Coverage.current() is None
    filename, statements, excluded, missing, formatted = coverage.analysis2(source)
    assert filename == str(source)
    assert statements == [1]
    assert excluded == [2]
    assert missing == []
    assert formatted == ""
    assert coverage.analysis(source) == (str(source), [1], [], "")
    coverage.set_option("report:precision", 2)
    assert coverage.get_option("report:precision") == 2
    coverage.clear_exclude()
    assert coverage.get_exclude_list() == []


def test_static_file_tracer_plugin_is_called_from_rust_collector(tmp_path: Path) -> None:
    reported = tmp_path / "generated.py"
    reported.write_text("answer = 42\n", encoding="utf-8")
    executed = tmp_path / "template.tpl"

    class Tracer(FileTracer):
        @override
        def source_filename(self) -> str:
            return str(reported)

    class Plugin(CoveragePlugin):
        @override
        def file_tracer(self, filename: str) -> FileTracer | None:
            return Tracer() if filename == str(executed) else None

        @override
        def file_reporter(self, filename: str) -> FileReporter:
            class Reporter(FileReporter):
                @override
                def source(self) -> str:
                    return "answer = 42\nmissing = 0\n"

                @override
                def lines(self) -> set[int]:
                    return {1, 2}

            return Reporter(filename)

    def initialize(registration: object) -> None:
        _register(registration, "add_file_tracer", Plugin())

    coverage = Coverage(data_file=None, source=[str(tmp_path)], plugins=[initialize])
    coverage.start()
    exec(compile("answer = 42\n", str(executed), "exec"), {})
    coverage.stop()

    assert coverage.get_data().lines(str(reported)) == [1]
    tracer = coverage.get_data().file_tracer(str(reported))
    assert tracer is not None
    assert tracer.endswith(".Plugin")
    assert coverage.report(file=StringIO(), show_missing=True) == 50.0


def test_file_tracer_executable_file_discovery_adds_uncovered_virtual_source(
    tmp_path: Path,
) -> None:
    virtual = tmp_path / "template.tpl"
    virtual.write_text("hello\n", encoding="utf-8")

    class Reporter(FileReporter):
        @override
        def lines(self) -> set[int]:
            return {1}

        @override
        def source(self) -> str:
            return "hello\n"

    class Plugin(CoveragePlugin):
        @override
        def find_executable_files(self, src_dir: str) -> list[str]:
            return [str(virtual)] if src_dir == str(tmp_path) else []

        @override
        def file_reporter(self, filename: str) -> FileReporter:
            return Reporter(filename)

    def initialize(registration: object) -> None:
        _register(registration, "add_file_tracer", Plugin())

    coverage = Coverage(
        data_file=str(tmp_path / ".coverage"), source=[str(tmp_path)], plugins=[initialize]
    )
    output = StringIO()
    assert coverage.report(file=output, show_missing=True) == 0.0
    assert "template.tpl" in output.getvalue()


def test_dynamic_file_tracer_uses_current_frame_and_maps_line_range(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("\n" * 39 + "first = 1\n", encoding="utf-8")
    second.write_text("\n" * 49 + "second = 2\n", encoding="utf-8")
    executed = tmp_path / "template.tpl"

    class Tracer(FileTracer):
        @override
        def has_dynamic_source_filename(self) -> bool:
            return True

        @override
        def dynamic_source_filename(self, filename: str, frame: FrameType) -> str | None:
            reported_filename = frame.f_globals["reported_filename"]
            assert isinstance(reported_filename, str)
            return reported_filename

        @override
        def line_number_range(self, frame: FrameType) -> tuple[int, int]:
            line = frame.f_globals["reported_line"]
            assert isinstance(line, int)
            return line, line

    class Plugin(CoveragePlugin):
        @override
        def file_tracer(self, filename: str) -> FileTracer | None:
            return Tracer() if filename == str(executed) else None

    def initialize(registration: object) -> None:
        _register(registration, "add_file_tracer", Plugin())

    coverage = Coverage(data_file=None, source=[str(tmp_path)], plugins=[initialize])
    coverage.start()
    exec(
        compile("value = 1\n", str(executed), "exec"),
        {"reported_filename": str(first), "reported_line": 40},
    )
    exec(
        compile("value = 2\n", str(executed), "exec"),
        {"reported_filename": str(second), "reported_line": 50},
    )
    coverage.stop()

    assert coverage.get_data().lines(str(first)) == [40]
    assert coverage.get_data().lines(str(second)) == [50]


def test_dynamic_file_tracer_can_decline_a_frame(tmp_path: Path) -> None:
    reported = tmp_path / "reported.py"
    reported.write_text("value = 1\n", encoding="utf-8")
    executed = tmp_path / "template.tpl"

    class Tracer(FileTracer):
        @override
        def has_dynamic_source_filename(self) -> bool:
            return True

        @override
        def dynamic_source_filename(self, filename: str, frame: FrameType) -> str | None:
            return str(reported) if frame.f_code.co_name == "render" else None

        @override
        def line_number_range(self, frame: FrameType) -> tuple[int, int]:
            return 1, 1

    class Plugin(CoveragePlugin):
        @override
        def file_tracer(self, filename: str) -> FileTracer | None:
            return Tracer() if filename == str(executed) else None

    def initialize(registration: object) -> None:
        _register(registration, "add_file_tracer", Plugin())

    coverage = Coverage(data_file=None, source=[str(tmp_path)], plugins=[initialize])
    coverage.start()
    exec(compile("def render():\n    value = 1\nrender()\n", str(executed), "exec"), {})
    coverage.stop()

    assert coverage.get_data().lines(str(reported)) == [1]


def test_dynamic_context_plugin_runs_only_at_rust_frame_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "context.py"
    source.write_text("value = 1\n", encoding="utf-8")

    class Plugin(CoveragePlugin):
        @override
        def dynamic_context(self, frame: FrameType) -> str | None:
            if frame.f_code.co_filename == str(source):
                return "plugin-context"
            return None

    def initialize(registration: object) -> None:
        _register(registration, "add_dynamic_context", Plugin())

    coverage = Coverage(
        data_file=None,
        source=[str(tmp_path)],
        context="static",
        plugins=[initialize],
    )
    coverage.start()
    exec(compile("value = 1\n", str(source), "exec"), {})
    coverage.stop()

    assert coverage.get_data().contexts_by_lineno(str(source)) == {1: ["static|plugin-context"]}


@pytest.mark.skipif(
    not hasattr(sys, "monitoring")
    or not hasattr(sys.monitoring.events, "BRANCH_LEFT")
    or not hasattr(sys.monitoring.events, "BRANCH_RIGHT"),
    reason="independently disableable branch events require CPython 3.14+",
)
def test_dynamic_context_plugin_keeps_repeated_branch_events_enabled(tmp_path: Path) -> None:
    source = tmp_path / "dynamic_branch_context.py"
    source.write_text(
        "def choose(context, value):\n    if value:\n        return 1\n    return 2\n",
        encoding="utf-8",
    )

    class Plugin(CoveragePlugin):
        @override
        def dynamic_context(self, frame: FrameType) -> str | None:
            if frame.f_code.co_filename == str(source):
                context = frame.f_locals.get("context")
                assert context is None or isinstance(context, str)
                return context
            return None

    def initialize(registration: object) -> None:
        _register(registration, "add_dynamic_context", Plugin())

    coverage = Coverage(
        data_file=None,
        source=[str(tmp_path)],
        context="static",
        branch=True,
        plugins=[initialize],
    )
    namespace = {}
    coverage.start()
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
    namespace["choose"]("first", True)
    namespace["choose"]("second", False)
    namespace["choose"]("third", True)
    coverage.stop()

    data = coverage.get_data()
    assert data.contexts_by_lineno(str(source)) == {
        1: ["static"],
        2: ["static|first", "static|second", "static|third"],
        3: ["static|first", "static|third"],
        4: ["static|second"],
    }

    data.set_query_context("static|first")
    assert set(data.arcs(str(source)) or ()) == {
        (-1, 2),
        (2, 3),
        (3, -1),
    }
    data.set_query_context("static|second")
    assert set(data.arcs(str(source)) or ()) == {
        (-1, 2),
        (2, 4),
        (4, -1),
    }
    data.set_query_context("static|third")
    assert set(data.arcs(str(source)) or ()) == {
        (-1, 2),
        (2, 3),
        (3, -1),
    }


def test_plugin_module_and_options_load_from_coverage_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "sample_plugin.py"
    module.write_text(
        "OPTIONS = None\n"
        "class Plugin: pass\n"
        "def coverage_init(registration, options):\n"
        "    global OPTIONS\n"
        "    OPTIONS = options\n"
        "    registration.add_noop(Plugin())\n",
        encoding="utf-8",
    )
    config = tmp_path / ".coveragerc"
    config.write_text(
        "[coverage:run]\nplugins = sample_plugin\n[plugin:sample_plugin]\nanswer = 42\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    coverage = Coverage(data_file=None, config_file=str(config))

    loaded = __import__("sample_plugin")
    assert loaded.OPTIONS == {"answer": "42"}
    assert len(list(coverage._plugins)) == 1


def test_configurer_reads_plugin_options_and_changes_coverage_options(tmp_path: Path) -> None:
    module = tmp_path / "configured_plugin.py"
    module.write_text(
        "seen = None\n"
        "class Plugin:\n"
        "    def configure(self, config):\n"
        "        global seen\n"
        "        seen = config.get_option('configured_plugin:answer')\n"
        "        config.set_option('run:omit', ['ignored.py'])\n"
        "def coverage_init(registration, options):\n"
        "    registration.add_configurer(Plugin())\n",
        encoding="utf-8",
    )
    config = tmp_path / ".coveragerc"
    config.write_text(
        "[coverage:run]\nplugins = configured_plugin\n[coverage:configured_plugin]\nanswer = 42\n",
        encoding="utf-8",
    )

    sys.path.insert(0, str(tmp_path))
    try:
        coverage = Coverage(data_file=None, config_file=str(config))
        loaded = __import__("configured_plugin")
    finally:
        sys.path.remove(str(tmp_path))

    assert loaded.seen == "42"
    assert coverage.omit == ["ignored.py"]


def test_collection_retries_after_tool_id_collision(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", "value = 1\n")
    project.write(
        "run.py",
        """
import sys
from vfy import Coverage
from vfy.errors import DataError
monitoring = sys.monitoring
monitoring.use_tool_id(monitoring.COVERAGE_ID, "occupied")
cov = Coverage(config_file=False, source=["."])
try:
    cov.start()
except DataError:
    pass
else:
    raise AssertionError("tool conflict must fail")
monitoring.free_tool_id(monitoring.COVERAGE_ID)
with cov.collect():
    import sample
cov.save()
assert monitoring.get_tool(monitoring.COVERAGE_ID) is None
""",
    )
    result = project.run_python("run.py")
    result.expect(0)
    assert project.lines("sample.py") == [1]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires a forked collector")
def test_forked_collection_can_retry_after_callback_initialization_fails(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", "answer = 42\n")
    project.write(
        "run.py",
        "import os\nimport sys\nfrom vfy import Coverage\n"
        "cov = Coverage(source=['.'], config_file=False)\ncov.start()\n"
        "original = sys.monitoring.register_callback\nfailures = []\n"
        "def failing(*args):\n"
        "    if args[-1] is None:\n        return original(*args)\n"
        "    sys.monitoring.register_callback = original\n"
        "    raise RuntimeError('child registration failed')\n"
        "sys.monitoring.register_callback = failing\n"
        "sys.unraisablehook = lambda error: failures.append(error.exc_value)\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    assert len(failures) == 1\n"
        "    assert str(failures[0]) == 'child registration failed'\n"
        "    assert Coverage.current() is None\n"
        "    with cov.collect():\n        import sample\n"
        "    assert cov.get_data().lines(os.path.abspath('sample.py')) == [1]\n"
        "    assert Coverage.current() is None\n"
        "    os._exit(0)\n"
        "sys.monitoring.register_callback = original\n"
        "_, status = os.waitpid(pid, 0)\ncov.stop()\n"
        "assert os.waitstatus_to_exitcode(status) == 0\n",
    )
    completed = project.run_python("run.py")
    completed.expect(0)


@pytest.mark.parametrize("stage", ["callback", "fork"])
def test_partial_startup_releases_resources_before_retry(tmp_path: Path, stage: str) -> None:
    if stage == "fork" and not hasattr(os, "register_at_fork"):
        pytest.skip("fork hooks are unavailable on this platform")
    project = VfyProject(tmp_path)
    project.write("sample.py", "answer = 42\n")
    project.write(
        "run.py",
        "import os\nimport sys\nfrom pathlib import Path\nfrom vfy import Coverage\n"
        f"stage = {stage!r}\n"
        "owner = sys.monitoring if stage == 'callback' else os\n"
        "name = 'register_callback' if stage == 'callback' else 'register_at_fork'\n"
        "original = getattr(owner, name)\ncalls = 0\n"
        "def failing(*args, **kwargs):\n"
        "    global calls\n    calls += 1\n"
        "    if stage == 'fork' or calls == 2:\n"
        "        setattr(owner, name, original)\n"
        "        raise RuntimeError('startup failed')\n"
        "    return original(*args, **kwargs)\n"
        "cov = Coverage(source=['.'], config_file=False)\n"
        "setattr(owner, name, failing)\n"
        "try:\n    cov.start()\nexcept RuntimeError as error:\n"
        "    assert str(error) == 'startup failed'\n"
        "else:\n    raise AssertionError('expected failure')\n"
        "assert sys.monitoring.get_tool(sys.monitoring.COVERAGE_ID) is None\n"
        "with cov.collect():\n"
        "    path = Path('sample.py').resolve()\n"
        "    exec(compile(path.read_text(), str(path), 'exec'), {})\n"
        "cov.save()\n",
    )
    completed = project.run_python("run.py")
    completed.expect(0)
    assert project.lines("sample.py") == [1]


@pytest.mark.parametrize("config_mode", ["null", "explicit", "discovered"])
def test_subprocess_coverage_preserves_parent_config(tmp_path: Path, config_mode: str) -> None:
    project = VfyProject(tmp_path)
    patched = "patch = subprocess\n"
    conflicting = "[run]\nplugins = unavailable_vfy_subprocess_plugin\n"
    discovered = f"[run]\ncontext = discovered\n{patched}"
    project.write(".coveragerc", discovered if config_mode == "discovered" else conflicting)
    project.write("selected.coveragerc", f"[run]\ncontext = selected\n{patched}")
    project.write("parent/.coveragerc", conflicting)
    project.write("child/.coveragerc", discovered if config_mode == "discovered" else conflicting)
    source = project.write(
        "subject.py",
        "def choose(flag):\n    if flag:\n        return 'yes'\n    return 'no'\n",
    )
    project.write(
        "child/child.py",
        "import subject\nassert subject.choose(True) == 'yes'\n"
        "assert subject.choose(False) == 'no'\nprint('yes no')\n",
    )
    project.write(
        "parent_program.py",
        "import os\nimport subprocess\nimport sys\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parent\n"
        "os.environ['PYTHONPATH'] = str(root) + os.pathsep + os.environ.get('PYTHONPATH', '')\n"
        "sys.path.insert(0, str(root))\n"
        "import subject\n"
        "assert subject.choose(True) == 'yes'\n"
        "os.chdir(root / 'parent')\n"
        "child = subprocess.run([sys.executable, str(root / 'child' / 'child.py')],\n"
        "                       cwd=root / 'child', capture_output=True, text=True)\n"
        "sys.stdout.write(child.stdout)\nsys.stderr.write(child.stderr)\n"
        "raise SystemExit(child.returncode)\n",
    )
    rcfile = {"null": os.devnull, "explicit": "selected.coveragerc", "discovered": None}[
        config_mode
    ]
    runs = {}
    for tool in ("coverage", "vfy"):
        data_file = f".coverage.{tool}"
        arguments = ["run", "--branch", f"--include={source}", f"--data-file={data_file}"]
        if rcfile is not None:
            arguments.append(f"--rcfile={rcfile}")
        if config_mode == "null":
            arguments.append("--parallel-mode")
        arguments.append("parent_program.py")
        runner = (
            project.run_coverage_py
            if tool == "coverage"
            else (lambda *a: project.run_vfy("coverage", *a))
        )
        runs[tool] = runner(*arguments)
        if config_mode != "null":
            combined = runner(
                "combine", f"--data-file={data_file}", *([f"--rcfile={rcfile}"] if rcfile else [])
            )
            combined.expect(0)
    for run in runs.values():
        assert (run.returncode, run.stdout, run.stderr) == (0, "yes no\n", "")
    if config_mode == "null":
        # Without the subprocess patch only the parent process is measured.
        return
    actual = project.data(".coverage.vfy")
    expected = project.data(".coverage.coverage")
    try:
        filename = str(source)
        assert actual.measured_files() == expected.measured_files() == {filename}
        assert actual.lines(filename) == expected.lines(filename) == [1, 2, 3, 4]
        assert sorted(actual.arcs(filename) or []) == sorted(expected.arcs(filename) or [])
        context = {"explicit": "selected", "discovered": "discovered"}[config_mode]
        assert (
            actual.contexts_by_lineno(filename)
            == expected.contexts_by_lineno(filename)
            == {line: [context] for line in [1, 2, 3, 4]}
        )
    finally:
        actual.close(force=True)
        expected.close(force=True)


@pytest.mark.parametrize("phase", ["before", "get_data", "load", "started"])
@pytest.mark.parametrize("option", ["data_file", "parallel", "sigterm"])
def test_run_option_setters_follow_initialization_boundaries(
    tmp_path: Path, phase: str, option: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "options.py",
        "import importlib, json, pathlib, signal, sys\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(config_file=False)\n"
        "phase, option = sys.argv[2:]\n"
        "if phase == 'get_data': cov.get_data()\n"
        "if phase == 'load': cov.load()\n"
        "if phase == 'started': cov.start()\n"
        "before = signal.getsignal(signal.SIGTERM)\n"
        "cov.set_option('run:' + option, 'alternate.data' if option == 'data_file' else True)\n"
        "if phase != 'started': cov.start()\n"
        "name = cov.get_data().data_filename()\n"
        "base = cov.get_data().base_filename()\n"
        "print(json.dumps([pathlib.Path(base).name, name != base, "
        "signal.getsignal(signal.SIGTERM) != before]))\n"
        "cov.stop()\n"
        "signal.signal(signal.SIGTERM, before)\n",
    )
    results = []
    for module in ("coverage", "vfy"):
        result = project.run_python("options.py", module, phase, option)
        result.expect(0)
        results.append(json.loads(result.stdout))
    assert results[1] == results[0]


def test_source_iterators_survive_related_option_change(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("first/sample.py", "value = 1\n")
    project.write("second/other.py", "value = 2\n")
    project.write(
        "sources.py",
        "import importlib, json, pathlib, sys\n"
        "first = pathlib.Path('first').resolve()\n"
        "second = pathlib.Path('second').resolve()\n"
        "sample = first / 'sample.py'\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    config_file=False, data_file=None, source=iter([str(first)]),\n"
        "    source_dirs=iter([str(second)]))\n"
        "cov.set_option('run:source_dirs', [str(second)])\n"
        "cov.start()\n"
        "exec(compile(sample.read_text(), str(sample), 'exec'), {})\n"
        "cov.stop()\n"
        "print(json.dumps(str(sample) in cov.get_data().measured_files()))\n",
    )
    results = []
    for module in ("coverage", "vfy"):
        result = project.run_python("sources.py", module)
        result.expect(0)
        results.append(json.loads(result.stdout))
    assert results == [True, True]


@pytest.mark.parametrize("initialized", [False, True])
def test_relative_files_setter_respects_initialization(tmp_path: Path, initialized: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", "value = 1\n")
    project.write(
        "relative.py",
        "import importlib, json, pathlib, sys\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(config_file=False, data_file=None)\n"
        "if sys.argv[2] == 'true': cov.get_data()\n"
        "cov.set_option('run:relative_files', True)\n"
        "sample = pathlib.Path('sample.py').resolve()\n"
        "cov.start()\n"
        "exec(compile(sample.read_text(), str(sample), 'exec'), {})\n"
        "cov.stop()\n"
        "print(json.dumps(sorted(cov.get_data().measured_files())))\n",
    )
    results = []
    for module in ("coverage", "vfy"):
        result = project.run_python("relative.py", module, str(initialized).lower())
        result.expect(0)
        results.append(json.loads(result.stdout))
    assert results[1] == results[0]
    assert results[0] == [str(tmp_path / "sample.py") if initialized else "sample.py"]


@pytest.mark.parametrize("initialized", [False, True])
def test_concurrency_setter_controls_parallel_data_at_initialization(
    tmp_path: Path, initialized: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("coverage.ini", "[run]\n")
    project.write(
        "concurrency.py",
        "import importlib, json, sys\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(config_file='coverage.ini')\n"
        "if sys.argv[2] == 'true': cov.get_data()\n"
        "cov.set_option('run:concurrency', ['multiprocessing'])\n"
        "cov.start()\n"
        "data = cov.get_data()\n"
        "print(json.dumps(data.data_filename() != data.base_filename()))\n"
        "cov.stop()\n",
    )
    results = []
    for module in ("coverage", "vfy"):
        result = project.run_python("concurrency.py", module, str(initialized).lower())
        result.expect(0)
        results.append(json.loads(result.stdout))
    assert results == [not initialized, not initialized]


def test_concurrency_setter_collects_spawned_child(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for module in ("coverage", "vfy"):
        project.write(f"{module}.ini", f"[run]\ndata_file = {module}-data\n")
    project.write("target.py", "def run():\n    return 123\n")
    project.write(
        "children.py",
        "import importlib, json, multiprocessing, pathlib, sys\n"
        "def work():\n"
        "    import target\n"
        "    target.run()\n"
        "if __name__ == '__main__':\n"
        "    module = importlib.import_module(sys.argv[1])\n"
        "    base = sys.argv[1] + '-data'\n"
        "    cov = module.Coverage(config_file=sys.argv[1] + '.ini')\n"
        "    cov.set_option('run:concurrency', ['multiprocessing'])\n"
        "    cov.start()\n"
        "    process = multiprocessing.get_context('spawn').Process(target=work)\n"
        "    process.start()\n"
        "    process.join(15)\n"
        "    if process.is_alive():\n"
        "        process.terminate()\n"
        "        process.join()\n"
        "        raise RuntimeError('child did not finish')\n"
        "    assert process.exitcode == 0\n"
        "    cov.stop()\n"
        "    cov.save()\n"
        "    seen = False\n"
        "    for path in pathlib.Path('.').glob(base + '.*'):\n"
        "        data = module.CoverageData(basename=str(path))\n"
        "        data.read()\n"
        "        seen |= any(name.endswith('target.py') for name in data.measured_files())\n"
        "    print(json.dumps(seen))\n",
    )
    for module in ("coverage", "vfy"):
        result = project.run_python("children.py", module)
        result.expect(0)
        assert json.loads(result.stdout) is True


def _outcome(action: Callable[[], object]) -> tuple[str, str]:
    """Return the exception class name and message of ``action``, or ``("", "")``."""
    try:
        action()
    except Exception as error:  # noqa: BLE001 - the differential compares every outcome
        return type(error).__name__, str(error)
    return "", ""


def test_switch_context_requires_started_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Coverage.py's sysmon core, its 3.14 default, warns that dynamic contexts
    # are unsupported; the C tracer is the reference for context switching.
    monkeypatch.setenv("COVERAGE_CORE", "ctrace")
    outcomes = {}
    for engine, coverage_class in (("coverage", coverage_py.Coverage), ("vfy", Coverage)):
        cov = coverage_class(data_file=str(tmp_path / engine), config_file=False)
        before_start = _outcome(lambda cov=cov: cov.switch_context("early"))
        cov.start()
        started = _outcome(lambda cov=cov: cov.switch_context("running"))
        cov.stop()
        after_stop = _outcome(lambda cov=cov: cov.switch_context("late"))
        outcomes[engine] = (before_start, started, after_stop)
    assert (
        outcomes["vfy"][0][1]
        == outcomes["coverage"][0][1]
        == ("Cannot switch context, coverage is not started")
    )
    assert [kind for kind, _message in outcomes["vfy"]] == ["VfyError", "", "VfyError"]
    assert [kind for kind, _message in outcomes["coverage"]] == [
        "CoverageException",
        "",
        "CoverageException",
    ]
    assert [message for _kind, message in outcomes["vfy"]] == [
        message for _kind, message in outcomes["coverage"]
    ]


@pytest.mark.parametrize("concurrency", ["fiber", ["thread", "fiber", "abacus"]])
def test_unknown_concurrency_is_a_configuration_error(
    tmp_path: Path, concurrency: str | list[str]
) -> None:
    outcomes = {}
    for engine, coverage_class in (("coverage", coverage_py.Coverage), ("vfy", Coverage)):
        outcomes[engine] = _outcome(
            lambda cls=coverage_class, path=str(tmp_path / engine): cls(
                data_file=path, config_file=False, concurrency=concurrency
            )
        )
    assert outcomes["vfy"] == outcomes["coverage"]
    assert outcomes["vfy"][0] == "ConfigError"
    assert outcomes["vfy"][1].startswith("Unknown concurrency choices: ")


def test_run_patterns_do_not_filter_reports_unless_passed_to_the_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructor include/omit replace [report] patterns; [run] patterns never reach reports."""
    monkeypatch.chdir(tmp_path)
    for name in "abc":
        (tmp_path / f"{name}.py").write_text("x = 1\n")
    cases: dict[str, tuple[str, list[str] | None, list[str] | None]] = {
        "run-omit": ("[run]\nomit = a.py\n", None, None),
        "run-include": ("[run]\ninclude = a.py\n", None, None),
        "run-and-report-omit": ("[run]\nomit = a.py\n[report]\nomit = b.py\n", None, None),
        "constructor-omit": ("", ["a.py"], None),
        "constructor-omit-over-report": ("[report]\nomit = b.py\n", ["a.py"], None),
        "constructor-include-over-report": ("[report]\ninclude = b.py\n", None, ["a.py"]),
    }
    for name, (config, omit, include) in cases.items():
        reported = {}
        for engine, data_class, coverage_class in (
            ("coverage", coverage_py.CoverageData, coverage_py.Coverage),
            ("vfy", CoverageData, Coverage),
        ):
            (tmp_path / ".coveragerc").write_text(config)
            data_file = str(tmp_path / f".coverage.{engine}")
            data = data_class(data_file)
            data.add_lines({str(tmp_path / f"{file}.py"): [1] for file in "abc"})
            data.write()
            cov = coverage_class(
                data_file=data_file, config_file=".coveragerc", omit=omit, include=include
            )
            cov.load()
            output = StringIO()
            cov.report(file=output)
            rows = output.getvalue().splitlines()[2:-2]
            reported[engine] = (
                [row.split()[0] for row in rows],
                cov.get_option("report:omit"),
                cov.get_option("report:include"),
            )
        assert reported["vfy"] == reported["coverage"], name


@pytest.mark.parametrize("option", ["include", "omit"])
def test_constructor_pattern_iterators_filter_collection_like_coverage_py(
    tmp_path: Path, option: str
) -> None:
    project = VfyProject(tmp_path)
    for name in ("chosen", "other"):
        project.write(f"{name}.py", "value = 1\n")
    project.write(
        "collect.py",
        "import json, runpy, sys\n"
        "from importlib import import_module\n"
        "from pathlib import Path\n"
        "Coverage = import_module(sys.argv[1]).Coverage\n"
        "cov = Coverage(config_file=False, data_file=None, "
        "**{sys.argv[2]: iter(['chosen.py'])})\n"
        "with cov.collect():\n"
        "    for name in ('chosen.py', 'other.py'):\n"
        "        runpy.run_path(str(Path(name).resolve()))\n"
        "measured = {Path(name).name for name in cov.get_data().measured_files()}\n"
        "print(json.dumps(sorted(measured & {'chosen.py', 'other.py'})))\n",
    )

    reference = project.run_python("collect.py", "coverage", option).expect(0)
    actual = project.run_python("collect.py", "vfy", option).expect(0)

    assert (
        json.loads(actual.stdout)
        == json.loads(reference.stdout)
        == ["chosen.py" if option == "include" else "other.py"]
    )
