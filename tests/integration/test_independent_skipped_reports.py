"""Skipped report locations and diagnostics follow the pytest boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_OBSERVER = """import json
from pathlib import Path

def pytest_runtest_logreport(report):
    filename = 'reports-' + Path(report.nodeid.split('::', 1)[0]).stem + '.jsonl'
    value = report.longrepr
    with Path(filename).open('a') as output:
        output.write(json.dumps({
            'nodeid': report.nodeid, 'when': report.when, 'outcome': report.outcome,
            'longrepr': list(value) if isinstance(value, tuple) else value,
            'longrepr_type': type(value).__name__, 'diagnostic': report.longreprtext,
        }) + '\\n')
"""

_HELPERS = """import json
from pathlib import Path
import pytest

def note(value):
    with Path(Path(__file__).stem + '.events.jsonl').open('a') as output:
        output.write(json.dumps(value) + '\\n')

class GlobalHide:
    def __bool__(self):
        note(['global bool'])
        return True
    def __call__(self, info):
        note(['global hide', info.type.__name__, str(info.value)])
        return True

__tracebackhide__ = GlobalHide()

class FalseHide:
    def __bool__(self):
        note(['false bool'])
        return False
    def __call__(self, info):
        raise AssertionError('false callable was called')

class UnreachedHide:
    def __bool__(self):
        raise AssertionError('hide lookup continued beyond first visible frame')

class UseItem:
    def __bool__(self):
        note(['use item'])
        return True

def global_hidden():
    pytest.skip('global hidden')

def local_visible():
    __tracebackhide__ = False
    pytest.skip('local overrides global')

def false_callable():
    __tracebackhide__ = FalseHide()
    pytest.skip('false callable')

def visible_inner():
    __tracebackhide__ = False
    pytest.skip('first visible')

def outer_unreached():
    __tracebackhide__ = UnreachedHide()
    visible_inner()

def item_location():
    error = pytest.skip.Exception('item location')
    error._use_item_location = UseItem()
    raise error
"""

_CASES = """import pytest
from HELPERS import global_hidden, local_visible, false_callable, outer_unreached, item_location

@pytest.mark.skip(reason='marker "quote" \\n\\u00e9')
def test_marker():
    raise AssertionError('marker skip ran')

@pytest.mark.skipif(True, reason='conditional reason')
def test_skipif():
    raise AssertionError('conditional skip ran')

@pytest.fixture
def setup_skip():
    pytest.skip('setup reason')

def test_setup(setup_skip):
    raise AssertionError('setup skip ran')

def test_call():
    pytest.skip('call reason')

@pytest.fixture
def teardown_skip():
    yield
    pytest.skip('teardown reason')

def test_teardown(teardown_skip):
    pass

def test_empty_reason():
    pytest.skip('')

def test_global_hidden():
    global_hidden()

def test_local_visible():
    local_visible()

def test_false_callable():
    false_callable()

def test_early_stop():
    outer_unreached()

def test_use_item_location():
    item_location()
"""


def _read_observations(root: Path) -> dict[str, str]:
    return {
        path.name: path.read_text()
        for pattern in ("reports-*.jsonl", "skip_helpers_*.events.jsonl")
        for path in root.glob(pattern)
    }


def _compare(project: VfyProject, workers: int) -> None:
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    expected = _read_observations(project.root)
    assert len(expected) >= 2
    for name in expected:
        (project.root / name).unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "--native-scheduling=file",
        "--native-result-json=native.json",
        env={"VFY_WORKER_START": "exec"},
    )
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert _read_observations(project.root) == expected
    raw = json.loads((project.root / "native.json").read_text())
    assert raw["execution_topology"]["active_workers"] == workers
    reports = {
        (record["nodeid"], record["when"]): record
        for name, text in expected.items()
        if name.startswith("reports-")
        for line in text.splitlines()
        for record in [json.loads(line)]
    }
    for case in raw["cases"]:
        for phase in case["phases"]:
            record = reports[case["nodeid"], phase["phase"]]
            assert phase["detail"] == record["diagnostic"]
            if record["longrepr_type"] == "tuple":
                path, line, message = record["longrepr"]
                assert phase["skip_location"] == {"path": path, "line": line}
                assert phase["exception"] == message


@pytest.mark.parametrize("workers", [1, 2])
def test_skipped_reports_preserve_crash_lookup_before_item_location(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _OBSERVER)
    for name in ("first", "second"):
        project.write(f"skip_helpers_{name}.py", _HELPERS)
        project.write(f"test_{name}.py", _CASES.replace("HELPERS", f"skip_helpers_{name}"))
    _compare(project, workers)


@pytest.mark.parametrize("workers", [1, 2])
def test_skip_item_location_uses_live_reportinfo_after_hide_callbacks(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _OBSERVER)
    for name in ("first", "second"):
        project.write(
            f"skip_helpers_{name}.py",
            """import json
from pathlib import Path
import pytest

def note(value):
    with Path(Path(__file__).stem + '.events.jsonl').open('a') as output:
        output.write(json.dumps(value) + '\\n')

def imported():
    error = pytest.skip.Exception('imported item')
    error._use_item_location = True
    raise error

class Flag:
    def __bool__(self):
        note('flag')
        return True

class ReportPath:
    def __fspath__(self):
        note('fspath')
        return 'plugin reportinfo path'

class Line(int):
    def __add__(self, other):
        note(['line add', other])
        return super().__add__(other)

class ReportInfo:
    def __getitem__(self, key):
        note(['slice', key.start, key.stop, key.step])
        assert (key.start, key.stop, key.step) == (None, 2, None)
        return [ReportPath(), Line(76)]

def changed(request):
    error = pytest.skip.Exception('changed reportinfo')
    error._use_item_location = Flag()
    def reportinfo():
        note('reportinfo')
        return ReportInfo()
    def hide(info):
        assert info.value is error
        note('hide')
        request.node.reportinfo = reportinfo
        return False
    __tracebackhide__ = hide
    raise error
""",
        )
        project.write(
            f"test_{name}.py",
            f"from skip_helpers_{name} import imported as test_imported, changed as test_changed\n",
        )
    _compare(project, workers)
    for name in ("first", "second"):
        records = [
            json.loads(line)
            for line in (tmp_path / f"reports-test_{name}.jsonl").read_text().splitlines()
        ]
        imported = next(
            record
            for record in records
            if record["nodeid"].endswith("::test_imported") and record["when"] == "call"
        )
        assert imported["longrepr"] == [
            str(tmp_path / f"skip_helpers_{name}.py"),
            9,
            "Skipped: imported item",
        ]
        events = [
            json.loads(line)
            for line in (tmp_path / f"skip_helpers_{name}.events.jsonl").read_text().splitlines()
        ]
        assert events == [
            "hide",
            "flag",
            "reportinfo",
            ["slice", None, 2, None],
            "fspath",
            ["line add", 1],
        ]


def test_item_reportinfo_resolves_current_names_and_imported_sources(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json
import os
from pathlib import Path

def pytest_collection_modifyitems(items):
    records = []
    for item in items:
        before = item.reportinfo()
        item.name = 'changed_' + item.name
        after = item.reportinfo()
        records.append([
            item.nodeid,
            [os.fspath(before[0]), before[1], before[2]],
            [os.fspath(after[0]), after[1], after[2]],
        ])
    Path('reportinfo.json').write_text(json.dumps(records))
""",
    )
    project.write("imported_helper.py", "def imported():\n    pass\n")
    project.write(
        "test_locations.py",
        """from imported_helper import imported as test_imported

class TestClass:
    def test_method(self):
        pass
""",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    output = tmp_path / "reportinfo.json"
    expected = output.read_text()
    output.unlink()
    candidate = project.run_engine("native")
    assert candidate.returncode == oracle.returncode, candidate.stdout + candidate.stderr
    assert output.read_text() == expected


@pytest.mark.parametrize("workers", [1, 2])
def test_fixture_skip_selects_requesting_item_before_cache_and_report_hooks(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        _OBSERVER
        + """
import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    if call.excinfo is not None and isinstance(call.excinfo.value, pytest.skip.Exception):
        module = Path(item.nodeid.split('::', 1)[0]).stem
        filename = 'skip_helpers_flags_' + module + '.events.jsonl'
        with Path(filename).open('a') as output:
            output.write(json.dumps([
                item.nodeid, call.when, call.excinfo.value._use_item_location,
            ]) + '\\n')
    return (yield)
""",
    )
    for name in ("first", "second"):
        project.write(
            f"test_{name}.py",
            """from pathlib import Path
import pytest

@pytest.fixture(scope='module')
def cached_skip():
    Path('skip_helpers_body_' + Path(__file__).stem + '.events.jsonl').write_text('"body"\\n')
    pytest.skip('cached fixture setup')

def test_first(cached_skip):
    raise AssertionError('cached skip ran')

def test_second(cached_skip):
    raise AssertionError('cached skip ran')

def test_dynamic(request):
    request.getfixturevalue('cached_skip')

@pytest.fixture
def generator_setup_skip():
    pytest.skip('generator first next')
    yield

def test_generator(generator_setup_skip):
    raise AssertionError('generator setup skip ran')

@pytest.fixture
def teardown_skip():
    yield
    pytest.skip('fixture teardown')

def test_teardown(teardown_skip):
    pass
""",
        )
    _compare(project, workers)
    for name in ("first", "second"):
        flags = [
            json.loads(line)
            for line in (tmp_path / f"skip_helpers_flags_test_{name}.events.jsonl")
            .read_text()
            .splitlines()
        ]
        assert flags == [
            [f"test_{name}.py::test_first", "setup", True],
            [f"test_{name}.py::test_second", "setup", True],
            [f"test_{name}.py::test_dynamic", "call", True],
            [f"test_{name}.py::test_generator", "setup", True],
            [f"test_{name}.py::test_teardown", "teardown", False],
        ]
