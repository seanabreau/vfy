"""Report keywords consume the item attribute once, as pytest does."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PLUGIN = r"""import json
from pathlib import Path
import pytest
from selection import MODE

events = []

class Keywords:
    def __init__(self, phase, read):
        self.phase, self.read = phase, read
    def __iter__(self):
        events.append(['iterate', self.phase, self.read])
        yield 'first-' + str(self.read)
        if MODE == 'iterator-error':
            raise ValueError('actual keyword iterator failure')
        yield 'last-' + str(self.read)
    def __del__(self):
        events.append(['release', self.phase, self.read])

class CaseFile(pytest.File):
    def collect(self):
        yield CaseItem.from_parent(self, name='actual')

class CaseItem(pytest.Item):
    def runtest(self):
        pass
    def __getattribute__(self, name):
        state = object.__getattribute__(self, '__dict__')
        phase = state.get('_observed_phase')
        if name == 'keywords' and phase is not None:
            read = state['_reads'] = state.get('_reads', 0) + 1
            events.append(['read', phase, read])
            if MODE == 'missing':
                raise AttributeError('actual missing keywords')
            if MODE == 'getter-error':
                raise LookupError('actual keyword getter failure')
            return Keywords(phase, read)
        return super().__getattribute__(name)

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.case':
        return CaseFile.from_parent(parent, path=file_path)

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    item._observed_phase = call.when
    item._reads = 0
    try:
        report = yield
        events.append(['report', call.when, list(report.keywords.items())])
        return report
    except (LookupError, ValueError) as error:
        events.append(['error', call.when, type(error).__name__, str(error)])
        raise
    finally:
        item._observed_phase = None
        Path('observed.json').write_text(json.dumps(events))
"""


def _observe(root: Path, engine: str, mode: str) -> tuple[int, list[list[object]]]:
    project = VfyProject(root)
    project.write("pytest.ini", "[pytest]\n")
    project.write("selection.py", f"MODE = {mode!r}\n")
    project.write("conftest.py", _PLUGIN)
    project.write("test_value.case", "")
    result = (
        project.run_python("-m", "pytest", "-q")
        if engine == "stock"
        else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    )
    observed = project.root / "observed.json"
    assert observed.is_file(), result.stdout + result.stderr
    return result.returncode, cast("list[list[object]]", json.loads(observed.read_text()))


@pytest.mark.parametrize("mode", ["changing", "getter-error", "iterator-error"])
def test_report_keywords_read_actual_resource_once_and_preserve_errors(
    tmp_path: Path, mode: str
) -> None:
    expected = _observe(tmp_path / "stock", "stock", mode)
    if mode == "changing":
        assert expected == (
            0,
            [
                row
                for phase in ("setup", "call", "teardown")
                for row in (
                    ["read", phase, 1],
                    ["iterate", phase, 1],
                    ["release", phase, 1],
                    ["report", phase, [["first-1", 1], ["last-1", 1]]],
                )
            ],
        )
    else:
        assert expected[0] != 0
        assert any(row[0] == "error" and "actual keyword" in str(row[-1]) for row in expected[1])
    assert _observe(tmp_path / "independent", "independent", mode) == expected


def test_missing_report_keywords_keep_the_independent_item_fallback(tmp_path: Path) -> None:
    status, observed = _observe(tmp_path, "independent", "missing")
    assert status == 0
    assert observed == [
        row
        for phase in ("setup", "call", "teardown")
        for row in (["read", phase, 1], ["report", phase, []])
    ]


def test_public_keyword_replacement_deletion_and_retained_iteration_match_stock(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_value.case", "")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []

class CaseFile(pytest.File):
    def collect(self):
        yield CaseItem.from_parent(self, name='actual')

class CaseItem(pytest.Item):
    def __setattr__(self, name, value):
        if name == 'keywords':
            events.append(['assign'])
        super().__setattr__(name, value)
    def runtest(self):
        pass

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.case':
        return CaseFile.from_parent(parent, path=file_path)

def pytest_collection_modifyitems(items):
    item, = items
    original = item.keywords
    item.keywords = {'replacement': True}
    events.append(['replacement', list(item.keywords)])
    del item.keywords
    events.append(['deleted', hasattr(item, 'keywords')])
    original['own'] = True
    iterator = iter(original)
    events.append(['first', next(iterator)])
    parent_keywords = item.parent.keywords
    try:
        item.parent.keywords = {'inherited': True, 'own': True}
        events.append(['remaining', list(iterator)])
    finally:
        item.parent.keywords = parent_keywords
    item.keywords = original

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    item.keywords = {'first-' + call.when: True, 'last-' + call.when: True}
    report = yield
    events.append(['report', call.when, list(report.keywords.items())])
    return report

def pytest_sessionfinish(session):
    Path('observed.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_pytest("-q", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "observed.json").read_text())
    assert expected == [
        ["assign"],
        ["assign"],
        ["replacement", ["replacement"]],
        ["deleted", False],
        ["first", "actual"],
        ["remaining", ["own", "inherited"]],
        ["assign"],
        *[
            row
            for phase in ("setup", "call", "teardown")
            for row in (
                ["assign"],
                ["report", phase, [["first-" + phase, 1], ["last-" + phase, 1]]],
            )
        ],
    ]
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "observed.json").read_text()) == expected


def test_report_keywords_snapshot_live_ancestor_changes_for_each_phase(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

events = []
reports = []

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    item.keywords['own-' + call.when] = True
    item.parent.keywords['parent-' + call.when] = True
    report = yield
    reports.append(report)
    events.append([call.when, [[key, value] for key, value in report.keywords.items()
                              if key.startswith(('own-', 'parent-'))]])
    return report

def pytest_sessionfinish():
    events.append(['retained', [list(report.keywords.items()) == list(copy)
                               for report, copy in zip(reports, snapshots)]])
    Path('observed.json').write_text(json.dumps(events))

snapshots = []

def pytest_runtest_logreport(report):
    snapshots.append(list(report.keywords.items()))
""",
    )
    stock = project.run_python("-m", "pytest", "-q")
    stock.expect(0)
    expected = json.loads((tmp_path / "observed.json").read_text())
    assert expected == [
        [
            phase,
            [["own-" + prior, 1] for prior in ("setup", "call", "teardown")[: index + 1]]
            + [["parent-" + prior, 1] for prior in ("setup", "call", "teardown")[: index + 1]],
        ]
        for index, phase in enumerate(("setup", "call", "teardown"))
    ] + [["retained", [True, True, True]]]
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    independent.expect(0)
    assert json.loads((tmp_path / "observed.json").read_text()) == expected


@pytest.mark.parametrize("source", ["dict", "set", "frozenset", "mapping", "iterator", "reentrant"])
def test_report_keyword_key_callbacks_and_iterator_ownership_match_stock(
    tmp_path: Path, source: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("selection.py", f"SOURCE = {source!r}\n")
    project.write("test_value.py", "def test_value():\n    pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest
from selection import SOURCE

events = []
active = False
mutated = False
current = None

class Key(str):
    def __hash__(self):
        global mutated
        if active:
            events.append(['hash', str(self)])
            if SOURCE == 'reentrant' and not mutated:
                mutated = True
                current['inserted-during-hash'] = True
        return 7
    def __eq__(self, other):
        if active:
            events.append(['equal', str(self), str(other)])
        return str(self) == str(other)

class Keywords:
    def __init__(self, keys):
        self.keys = keys
    def __iter__(self):
        events.append(['iterate'])
        yield from self.keys
    def __del__(self):
        events.append(['released'])

class Iterator:
    def __init__(self, keys):
        self.keys = iter(keys)
    def __iter__(self):
        # CPython 3.13 can reacquire self-iterators; keep this operation idempotent.
        return self
    def __next__(self):
        return next(self.keys)
    def __del__(self):
        events.append(['released'])

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    global active, current, mutated
    keys = [Key('same'), Key('other'), Key('same')]
    source = (dict.fromkeys(keys) if SOURCE in ('dict', 'reentrant') else
              set(keys) if SOURCE == 'set' else
              frozenset(keys) if SOURCE == 'frozenset' else
              Keywords(keys) if SOURCE == 'mapping' else Iterator(keys))
    previous = item.keywords
    item.keywords = source
    current = source
    mutated = False
    active = True
    try:
        report = yield
        active = False
        events.append(['report', call.when, list(report.keywords.items())])
        return report
    except RuntimeError as error:
        active = False
        events.append(['error', call.when, str(error)])
        raise
    finally:
        active = False
        current = None
        item.keywords = previous
        del source
        Path('observed.json').write_text(json.dumps(events))
""",
    )
    stock = project.run_python("-m", "pytest", "-q")
    expected = json.loads((tmp_path / "observed.json").read_text())
    assert any(event[0] == "hash" for event in expected), expected
    if source == "reentrant":
        assert stock.returncode != 0
        assert [event for event in expected if event[0] == "error"] == [
            ["error", "setup", "dictionary changed size during iteration"]
        ]
    else:
        stock.expect(0)
        assert any(event[0] == "equal" for event in expected), expected
        assert len([event for event in expected if event[0] == "report"]) == 3
        if source == "mapping":
            assert len([event for event in expected if event[0] == "iterate"]) == 3
        if source in {"mapping", "iterator"}:
            assert len([event for event in expected if event[0] == "released"]) == 3
    independent = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    assert json.loads((tmp_path / "observed.json").read_text()) == expected
