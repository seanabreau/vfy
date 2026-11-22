"""Reporting hooks observe completed phases before the next phase starts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_REPORTER = """import json, pytest
from pathlib import Path
session = None
events = []
reports = []
seen = set()

def pytest_sessionstart(session):
    globals()['session'] = session

def pytest_runtest_logstart(nodeid, location):
    events.append(['start', nodeid, list(location)])

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_logreport(report):
    events.append(['before', report.when, session.testsfailed])
    result = yield
    events.append(['after', report.when, session.testsfailed])
    seen.add(report.when)
    report.extra = {'phase': report.when}
    report.__dict__['mapping'] = report.when
    report.sections.append(('custom', report.when))
    print('HOOK:' + report.when)
    reports.append(report)
    return result

def pytest_runtest_logfinish(nodeid, location):
    events.append(['finish', nodeid, list(location)])

def pytest_sessionfinish():
    data = []
    for report in reports:
        assert report.duration >= 0 and report.stop >= report.start > 0
        assert report.extra == {'phase': report.when}
        assert report.__dict__['mapping'] == report.when
        data.append({
            'nodeid': report.nodeid,
            'location': list(report.location),
            'when': report.when,
            'outcome': report.outcome,
            'passed': report.passed,
            'failed': report.failed,
            'skipped': report.skipped,
            'sections': report.sections,
            'caplog': report.caplog,
            'capstdout': report.capstdout,
            'capstderr': report.capstderr,
            'log_sections': list(report.get_sections('Captured log')),
            'has_detail': 'report sentinel' in report.longreprtext,
            'no_longrepr': report.longrepr is None,
            'properties': report.user_properties,
        })
    Path('reports.json').write_text(json.dumps({'events': events, 'reports': data}))
"""


@requires_native_pytest
def test_report_locations_keep_symlink_spelling_after_retarget_and_removal(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for directory in ("real", "other"):
        project.write(f"{directory}/test_body.py", "def test_body():\n    pass\n")
        project.write(f"{directory}/custom.py", "")
        project.write(f"{directory}/test_custom.case", "")
    try:
        (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

observed = {'queries': [], 'items': [], 'reports': []}

class CaseFile(pytest.File):
    def collect(self):
        yield CaseItem.from_parent(self, name='custom')

class CaseItem(pytest.Item):
    def runtest(self):
        pass
    def reportinfo(self):
        return self.config.rootpath / 'alias/custom.py', 11, 'custom sentinel'

def pytest_collect_file(file_path, parent):
    if file_path.suffix == '.case':
        return CaseFile.from_parent(parent, path=file_path)

def pytest_collection_modifyitems(session, items):
    root = session.config.rootpath
    alias = root / 'alias'
    alias.unlink()
    alias.symlink_to(root / 'real', target_is_directory=True)
    paths = [root, root / 'real/test_body.py', Path('relative.py'),
             root.parent / 'outside.py', alias / 'test_body.py']
    def record():
        observed['queries'].append([session._node_location_to_relpath(path) for path in paths])
        observed['items'].append([list(item.location) for item in items])
        for item in items:
            cached = item.location
            assert item.location is cached
    record()
    alias.unlink()
    alias.symlink_to(root / 'other', target_is_directory=True)
    record()
    alias.unlink()
    record()
    alias.symlink_to(root / 'other', target_is_directory=True)
    items[-1].location = ('owned location.py', 17, 'caller domain')

def pytest_runtest_logreport(report):
    observed['reports'].append([report.when, list(report.location)])

def pytest_sessionfinish():
    Path('locations.json').write_text(json.dumps(observed))
""",
    )
    arguments = ["-q", "alias"]
    stock = project.run_engine("stock", *arguments)
    project.write("stock.stdout", stock.stdout)
    project.write("stock.stderr", stock.stderr)
    stock.expect(0)
    expected = json.loads((tmp_path / "locations.json").read_text())
    queries = [".", "real/test_body.py", "relative.py", "../outside.py", "alias/test_body.py"]
    locations = [
        ["alias/test_body.py", 0, "test_body"],
        ["alias/custom.py", 11, "custom sentinel"],
    ]
    assert expected == {
        "queries": [queries] * 3,
        "items": [locations] * 3,
        "reports": [
            [phase, location]
            for location in (locations[0], ["owned location.py", 17, "caller domain"])
            for phase in ("setup", "call", "teardown")
        ],
    }
    (tmp_path / "locations.json").unlink()
    independent = project.run_engine("native", *arguments)
    project.write("independent.stdout", independent.stdout)
    project.write("independent.stderr", independent.stderr)
    independent.expect(0)
    assert json.loads((tmp_path / "locations.json").read_text()) == expected


@requires_native_pytest
@pytest.mark.parametrize(("kind", "status"), [("pass", 0), ("fail", 1), ("skip", 0), ("error", 1)])
def test_public_report_hooks_match_phase_order_capture_location_and_retained_reports(
    tmp_path: Path, kind: str, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nlog_format = %(message)s\n")
    project.write("report_plugin.py", _REPORTER)
    project.write(
        "test_reports.py",
        f"""import logging, sys, pytest
import report_plugin
kind = {kind!r}

@pytest.fixture(autouse=True)
def phases(caplog, request):
    request.node.user_properties.append(('fixture', 'live'))
    print('setup output')
    print('setup error output', file=sys.stderr)
    logging.warning('setup log')
    if kind == 'error':
        raise RuntimeError('report sentinel')
    yield
    assert 'call' in report_plugin.seen
    print('teardown output')
    logging.warning('teardown log')

class TestReports:
    @pytest.mark.parametrize('value', ['literal::parameter'])
    def test_body(self, caplog, value):
        assert 'setup' in report_plugin.seen
        print('call output')
        logging.warning('retained before clear')
        caplog.clear()
        assert caplog.records == []
        logging.warning('after clear')
        if kind == 'fail':
            assert False, 'report sentinel'
        if kind == 'skip':
            pytest.skip('report sentinel')
""",
    )
    oracle = project.run_engine("stock", "-q", "-p", "report_plugin")
    assert oracle.returncode == status, oracle.stdout + oracle.stderr
    expected = json.loads((tmp_path / "reports.json").read_text())
    (tmp_path / "reports.json").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "report_plugin",
        "--native-result-json=results.json",
    )
    assert candidate.returncode == status, candidate.stdout + candidate.stderr
    actual = json.loads((tmp_path / "reports.json").read_text())
    assert actual == expected
    phases = [report["when"] for report in expected["reports"]]
    assert phases == (["setup", "teardown"] if kind == "error" else ["setup", "call", "teardown"])
    for phase in phases:
        assert f"HOOK:{phase}" in candidate.stdout
    record = json.loads((tmp_path / "results.json").read_text())
    for phase in record["cases"][0]["phases"]:
        assert "HOOK:" not in phase["stdout"] + phase["stderr"] + phase["log"]
    if kind != "error":
        call = record["cases"][0]["phases"][1]
        assert call["log"] == "retained before clear\nafter clear"


@requires_native_pytest
@pytest.mark.parametrize(
    ("registration", "status"), [("normal", 1), ("early_first", 1), ("late_first", 0)]
)
def test_report_mutation_preserves_hook_priority_and_updates_the_native_phase(
    tmp_path: Path, registration: str, status: int
) -> None:
    project = VfyProject(tmp_path)
    hook = (
        """class LateReporter:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logreport(self, report):
        mutate(report)

def pytest_configure(config):
    config.pluginmanager.register(LateReporter(), 'late-reporter')
"""
        if registration == "late_first"
        else f"""@pytest.hookimpl(tryfirst={registration == "early_first"!r})
def pytest_runtest_logreport(report):
    mutate(report)
"""
    )
    project.write(
        "report_plugin.py",
        f"""import json, pytest
from pathlib import Path
reports = []

{hook}

def mutate(report):
    if report.when == 'call':
        report.outcome = 'passed'
        report.longrepr = None
        report.sections.append(('Captured log custom', 'plugin retained'))
        report.custom = ['live']
        reports.append(report)

def pytest_sessionfinish(session):
    report = reports[0]
    report.sections.append(('Captured stdout late', 'late retained'))
    report.custom.append('later')
    Path('mutation.json').write_text(json.dumps({{
        'outcome': report.outcome,
        'passed': report.passed,
        'failed': report.failed,
        'longrepr': report.longrepr,
        'longreprtext': report.longreprtext,
        'caplog': report.caplog,
        'capstdout': report.capstdout,
        'custom': report.custom,
        'testsfailed': session.testsfailed,
    }}))
""",
    )
    project.write("test_failure.py", "def test_failure():\n    assert False, 'policy sentinel'\n")
    oracle = project.run_engine("stock", "-q", "-p", "report_plugin")
    assert oracle.returncode == status, oracle.stdout + oracle.stderr
    expected = json.loads((tmp_path / "mutation.json").read_text())
    (tmp_path / "mutation.json").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "report_plugin",
        "--native-result-json=results.json",
    )
    assert candidate.returncode == status, candidate.stdout + candidate.stderr
    assert json.loads((tmp_path / "mutation.json").read_text()) == expected
    record = json.loads((tmp_path / "results.json").read_text())
    assert record["cases"][0]["phases"][1]["outcome"] == "passed"
    assert record["counts"]["passed"] == 1
    assert record["exitstatus"] == status


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_exception_interact_receives_each_failed_phase(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "def pytest_exception_interact(node, call, report):\n"
        "    with Path('interactions').open('a') as output:\n"
        "        output.write(f'{node.name} {call.when} {report.outcome}\\n')\n",
    )
    project.write(
        "test_body.py",
        "import pytest\n\ndef test_body():\n    assert False\n\n"
        "def test_skip():\n    pytest.skip('skipped')\n",
    )
    if engine == "stock":
        result = project.run_pytest("-q", "-p", "no:cacheprovider", load_xdist=False)
    else:
        result = project.run_engine("native", "-q")
    result.expect(1)
    assert (tmp_path / "interactions").read_text() == "test_body call failed\n"


@requires_native_pytest
def test_exception_interact_preserves_scope_before_eligibility_callbacks(tmp_path: Path) -> None:
    observed = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import pytest

class MovingException:
    def __init__(self, info, item):
        self.info = info
        self.item = item

    @property
    def value(self):
        self.item.path = self.item.config.rootpath / 'other/test_other.py'
        return self.info.value

    def __getattr__(self, name):
        return getattr(self.info, name)

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if item.name == 'test_failure' and call.when == 'call':
        call.excinfo = MovingException(call.excinfo, item)
    return report
""",
        )
        for directory in ("initial", "other"):
            project.write(
                f"{directory}/conftest.py",
                "from pathlib import Path\n"
                "def pytest_exception_interact(node, call, report):\n"
                "    with Path('interactions').open('a') as stream:\n"
                f"        stream.write('{directory} ' + report.when + '\\n')\n",
            )
        project.write("initial/test_body.py", "def test_failure():\n    assert False\n")
        project.write("other/test_other.py", "def test_success():\n    pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_engine("native", "-q")
        )
        result.expect(1)
        observed[engine] = (project.root / "interactions").read_text().splitlines()
    assert observed["native"] == observed["stock"] == ["initial call"]


@requires_native_pytest
@pytest.mark.parametrize("filename", ["test_names.py", "test_[names].py"])
def test_item_names_preserve_node_separators_inside_parameter_ids(
    tmp_path: Path, filename: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        filename,
        """import json, pytest
from pathlib import Path

class TestNames:
    @pytest.mark.parametrize('value', ['literal::parameter', '[nested::id]'])
    def test_name(self, value, request):
        with Path('names.jsonl').open('a') as output:
            output.write(json.dumps([request.node.nodeid, request.node.name,
                                     request.node.originalname]) + '\\n')
""",
    )
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    expected = (tmp_path / "names.jsonl").read_text()
    (tmp_path / "names.jsonl").unlink()
    candidate = project.run_engine("native")
    candidate.expect(0)
    assert (tmp_path / "names.jsonl").read_text() == expected


@requires_native_pytest
def test_phase_report_hooks_run_in_each_native_worker(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, os
from pathlib import Path
reports = []

def pytest_runtest_logreport(report):
    reports.append(report)

def pytest_sessionfinish(session):
    rows = [[report.nodeid, report.when, report.outcome, report.caplog]
            for report in reports]
    Path(f'reports-{os.getpid()}.json').write_text(json.dumps(rows))
""",
    )
    for name in ("first", "second"):
        project.write(
            f"test_{name}.py",
            f"import logging\ndef test_{name}():\n    logging.warning({name!r})\n",
        )
    project.write("pytest.ini", "[pytest]\nlog_format = %(message)s\n")
    oracle = project.run_engine("stock", "-q")
    oracle.expect(0)
    oracle_paths = list(tmp_path.glob("reports-*.json"))
    assert len(oracle_paths) == 1
    expected = sorted(json.loads(oracle_paths[0].read_text()))
    oracle_paths[0].unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--native-result-json=results.json",
    )
    candidate.expect(0)
    paths = list(tmp_path.glob("reports-*.json"))
    record = json.loads((tmp_path / "results.json").read_text())
    pids = set(record["execution_topology"]["worker_pids"])
    assert len(pids) == 2
    writers = {int(path.stem.removeprefix("reports-")): path for path in paths}
    (controller,) = writers.keys() - pids
    assert set(writers) == pids | {controller}
    assert json.loads(writers[controller].read_text()) == []
    actual = sorted(row for path in paths for row in json.loads(path.read_text()))
    assert actual == expected


@requires_native_pytest
@pytest.mark.parametrize("when", ["setup", "call"])
def test_report_hook_error_finishes_session_before_active_fixture_cleanup(
    tmp_path: Path, when: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "report_plugin.py",
        f"""import pytest
from pathlib import Path

def record(event):
    with Path('events').open('a') as output:
        output.write(event + '\\n')

def pytest_runtest_logreport(report):
    record('report-' + report.when)
    if report.when == {when!r}:
        raise RuntimeError('report hook sentinel')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish():
    record('sessionfinish-before')
    yield
    record('sessionfinish-after')

def pytest_unconfigure():
    record('unconfigure')

class Normal:
    def pytest_sessionfinish(self, session, exitstatus):
        record('sessionfinish-' + str(exitstatus))

class Last:
    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self):
        record('sessionfinish-last')

def pytest_configure(config):
    config.pluginmanager.register(Normal(), 'normal-finish')
    config.pluginmanager.register(Last(), 'last-finish')
    config.add_cleanup(lambda: record('cleanup'))
""",
    )
    project.write(
        "test_failure.py",
        """import pytest
from report_plugin import record
@pytest.fixture
def value():
    yield 1
    record('fixture-finalized')
def test_failure(value):
    pass
""",
    )
    oracle = project.run_engine("stock", "-q", "-p", "report_plugin")
    oracle.expect(3)
    expected = (tmp_path / "events").read_text()
    (tmp_path / "events").unlink()
    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "report_plugin",
        "--native-result-json=results.json",
    )
    candidate.expect(3)
    assert (tmp_path / "events").read_text() == expected
    record = json.loads((tmp_path / "results.json").read_text())
    assert "report hook sentinel" in record["internal_errors"][0]["detail"]


@requires_native_pytest
def test_report_hook_interrupt_closes_active_fixtures(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest
from pathlib import Path

def record(event):
    with Path('events').open('a') as output:
        output.write(event + '\\n')
@pytest.fixture
def value():
    yield 1
    record('fixture-finalized')
def pytest_runtest_logreport(report):
    if report.when == 'setup':
        raise KeyboardInterrupt()
def pytest_unconfigure():
    record('unconfigure')
def pytest_configure(config):
    config.add_cleanup(lambda: record('cleanup'))
""",
    )
    project.write("test_interrupt.py", "def test_interrupt(value):\n    pass\n")
    oracle = project.run_engine("stock", "-q")
    oracle.expect(2)
    expected = (tmp_path / "events").read_text()
    (tmp_path / "events").unlink()
    candidate = project.run_engine("native")
    candidate.expect(2)
    assert (tmp_path / "events").read_text() == expected
