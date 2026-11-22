"""Retained live report edits remain authoritative in native worker transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("workers", [1, 2])
def test_report_sections_preserve_replacement_clearing_duplicates_and_late_edits(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "report_plugin.py",
        """import json, os
from pathlib import Path
reports = []

def pytest_runtest_logreport(report):
    reports.append(report)
    if report.when == 'setup':
        report.sections = [('custom', 'first'), ('custom', 'second')]
    elif report.when == 'call':
        report.sections.clear()
    else:
        report.sections[:] = [('teardown custom', 'retained')]

def pytest_sessionfinish():
    for report in reports:
        if report.when == 'setup':
            report.sections.append(('session finish', 'later'))

def pytest_unconfigure():
    for report in reports:
        if report.when == 'teardown':
            report.sections.append(('unconfigure', 'last'))
    if reports:
        Path('reports-' + str(os.getpid()) + '.json').write_text(json.dumps([
            [report.nodeid, report.when, report.sections] for report in reports
        ]))
""",
    )
    for name in ("a", "b"):
        project.write(
            f"test_{name}.py",
            """import pytest
@pytest.fixture(autouse=True)
def output():
    print('setup captured')
    yield
    print('teardown captured')

def test_body():
    print('call captured')
""",
        )
    oracle = project.run_python("-m", "pytest", "-q", "-p", "report_plugin")
    oracle.expect(0)
    expected = _read_reports(tmp_path)
    assert len(expected) == 6
    for path in tmp_path.glob("reports-*.json"):
        path.unlink()

    candidate = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        f"--workers={workers}",
        "-p",
        "report_plugin",
        "--native-result-json=results.json",
    )
    candidate.expect(0)
    assert _read_reports(tmp_path) == expected
    raw = json.loads((tmp_path / "results.json").read_text())
    sections = {
        (case["nodeid"], phase["phase"]): [
            [section["name"], section["content"]] for section in phase["report_sections"]
        ]
        for case in raw["cases"]
        for phase in case["phases"]
    }
    assert sections == expected
    for case in raw["cases"]:
        assert case["phases"][1]["stdout"] == "call captured\n"
        assert case["phases"][1]["report_sections"] == []
    if workers == 2:
        pids = {int(path.stem.removeprefix("reports-")) for path in tmp_path.glob("reports-*.json")}
        assert pids == set(raw["execution_topology"]["worker_pids"])
        assert len(pids) == 2


def _read_reports(root: Path) -> dict[tuple[str, str], list[list[str]]]:
    return {
        (nodeid, phase): sections
        for path in root.glob("reports-*.json")
        for nodeid, phase, sections in json.loads(path.read_text())
    }


def test_empty_report_sections_keep_identity_and_late_alias_edits(tmp_path: Path) -> None:
    observations = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import gc
import json
from pathlib import Path

aliases = {}
reports = []

class Sections(list):
    pass

def check_gc_reads(report):
    errors, seen = [], []
    def read_sections(phase, info):
        if phase == 'start':
            try:
                seen.append(report.sections)
            except Exception as error:
                errors.append(type(error).__name__)
    gc.callbacks.append(read_sections)
    try:
        location = report.location
        gc.collect()
    finally:
        gc.callbacks.remove(read_sections)
    assert location[2] == 'test_body'
    assert not errors and seen
    assert all(value is report.sections for value in seen)

def pytest_runtest_logreport(report):
    reports.append(report)
    assert report.capstdout == report.capstderr == report.caplog == ''
    assert list(report.get_sections('custom')) == []
    if report.when == 'setup':
        check_gc_reads(report)
    elif report.when == 'call':
        replacement = Sections()
        report.sections = replacement
        aliases['call'] = replacement
        assert report.sections is replacement
        replacement.append(('custom', 'initial'))
        report.sections.clear()
        assert report.sections is replacement
    else:
        wire = report._to_json()
        aliases['teardown'] = wire['sections']
        assert wire['sections'] is report.sections
        assert report.sections is report.sections

def pytest_sessionfinish():
    for values in aliases.values():
        values.append(('session finish', 'later'))
    reports.clear()

def pytest_unconfigure():
    for values in aliases.values():
        values.append(('unconfigure', 'last'))
    Path('aliases.json').write_text(json.dumps(aliases))
""",
        )
        project.write("test_case.py", "def test_body(): pass\n")
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        if engine == "stock":
            result = project.run_python("-m", "pytest", "-q", env=env)
        else:
            result = project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "-q",
                "--native-result-json=result.json",
                env=env,
            )
        result.expect(0)
        observations[engine] = json.loads((project.root / "aliases.json").read_text())
        if engine == "native":
            record = json.loads((project.root / "result.json").read_text())
            sections = {
                phase["phase"]: [[row["name"], row["content"]] for row in phase["report_sections"]]
                for phase in record["cases"][0]["phases"]
            }
            assert sections == {"setup": [], **observations[engine]}
    assert (
        observations["native"]
        == observations["stock"]
        == {
            phase: [["session finish", "later"], ["unconfigure", "last"]]
            for phase in ("call", "teardown")
        }
    )


def test_live_add_report_section_replacement_and_empty_callbacks(tmp_path: Path) -> None:
    observations = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_body.py", "def test_body(): pass\n")
        project.write(
            "conftest.py",
            """import json
from pathlib import Path

events = []

def pytest_collection_modifyitems(items):
    kind = type(items[0])
    original = kind.add_report_section
    def second(self, when, key, content):
        events.append(['second', when, key, bool(content)])
        return original(self, when, key, content)
    def first(self, when, key, content):
        events.append(['first', when, key, bool(content)])
        kind.add_report_section = second
        return original(self, when, key, content)
    kind.add_report_section = first
    def restore():
        kind.add_report_section = original
    items[0].config.add_cleanup(restore)

def pytest_sessionfinish():
    Path('callbacks.json').write_text(json.dumps(events))
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        result.expect(0)
        observations[engine] = json.loads((project.root / "callbacks.json").read_text())
    assert observations["native"] == observations["stock"]


def test_report_section_public_operation_preserves_call_protocol(tmp_path: Path) -> None:
    observations = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_body.py", "def test_body(): pass\n")
        project.write(
            "conftest.py",
            """import inspect, json
from pathlib import Path

def pytest_collection_modifyitems(items):
    item = items[0]
    operation = type(item).add_report_section
    events = []
    class Content:
        def __bool__(self):
            events.append('truth')
            return True
    content = Content()
    item.add_report_section(when='manual', key='one', content=content)
    operation(item, 'manual', 'two', 'text')
    def error(call):
        try:
            call()
        except TypeError as exc:
            return str(exc)
    result = {
        'signatures': [str(inspect.signature(operation)),
                       str(inspect.signature(item.add_report_section)),
                       str(inspect.signature(operation.__get__(item)))],
        'truth': events,
        'same_content': item._report_sections[0][2] is content,
        'second': list(item._report_sections[1]),
        'duplicate': error(lambda: operation(item, 'manual', 'key', 'text', key='other')),
        'missing': error(lambda: operation(item, 'manual')),
        'unexpected': error(lambda: operation(item, 'manual', 'key', 'text', extra=1)),
        'excess': error(lambda: operation(item, 'manual', 'key', 'text', 'extra')),
        'excess_duplicate': error(lambda: operation(
            item, 'manual', 'key', 'text', 'extra', key='other')),
        'excess_unexpected': error(lambda: operation(
            item, 'manual', 'key', 'text', 'extra', bogus=1)),
    }
    item._report_sections.clear()
    Path('section-protocol.json').write_text(json.dumps(result))
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        result.expect(0)
        observations[engine] = json.loads((project.root / "section-protocol.json").read_text())
    assert observations["native"] == observations["stock"]


def test_nonempty_report_sections_keep_identity_during_gc_reentry(tmp_path: Path) -> None:
    observations = {}
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_body.py", "def test_body(): print('body output')\n")
        project.write(
            "conftest.py",
            """import gc, json
from pathlib import Path

observed = []
def pytest_runtest_logreport(report):
    if report.when != 'call':
        return
    seen, errors = [], []
    def callback(phase, info):
        if phase == 'start':
            try:
                seen.append(report.sections)
            except Exception as error:
                errors.append(type(error).__name__)
    old = gc.get_threshold()
    gc.callbacks.append(callback)
    try:
        gc.set_threshold(1, 1, 1)
        sections = report.sections
        if not seen:
            gc.collect()
        assert sections is report.sections
        assert all(value is sections for value in seen)
        assert not errors
        observed.extend([list(entry) for entry in sections])
        sections.append(('plugin section', 'added'))
        assert report.sections is sections
    finally:
        gc.set_threshold(*old)
        gc.callbacks.remove(callback)

def pytest_sessionfinish():
    Path('section-gc.json').write_text(json.dumps(observed))
""",
        )
        env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_pytest("-q", load_xdist=False, env=env)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q", env=env)
        )
        result.expect(0)
        observations[engine] = json.loads((project.root / "section-gc.json").read_text())
    assert (
        observations["native"]
        == observations["stock"]
        == [["Captured stdout call", "body output\n"]]
    )
