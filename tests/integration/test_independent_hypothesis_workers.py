"""Real Hypothesis reports retain examples and publication across native workers."""

from __future__ import annotations

import ast
import base64
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROOF = """import json, os
from pathlib import Path

def emit(kind, nodeid=None, **values):
    directory = Path(os.environ['HYPOTHESIS_PROOF'])
    directory.mkdir(exist_ok=True)
    with (directory / (str(os.getpid()) + '.jsonl')).open('a') as output:
        output.write(json.dumps(dict(kind=kind, nodeid=nodeid, pid=os.getpid(), **values)) + '\\n')
"""

_CONFIG = """import atexit, os, sys
from pathlib import Path
import pytest
from hypothesis import settings
from proof import emit

settings.register_profile('workers-proof', max_examples=3, database=None, deadline=None)

def shutdown():
    emit('shutdown')
atexit.register(shutdown)

def observe_patch(name, original):
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        emit('patch', operation=name, result=(str(result) if result is not None else None))
        return result
    return observed

def pytest_configure(config):
    plugin = config.pluginmanager.getplugin('hypothesispytest')
    emit('configure', active=plugin is not None)
    config.add_cleanup(lambda: emit('cleanup'))
    import hypothesis.statistics
    describe = hypothesis.statistics.describe_statistics
    def observed_statistics(statistics):
        result = describe(statistics)
        emit('statistics', statistics['nodeid'], raw=statistics, text=result)
        return result
    hypothesis.statistics.describe_statistics = observed_statistics
    config.add_cleanup(lambda: setattr(hypothesis.statistics, 'describe_statistics', describe))
    if os.environ['HYPOTHESIS_PATCH_PROOF'] == '1':
        from hypothesis.extra import _patching
        originals = {}
        for name in ('make_patch', 'gc_patches', 'save_patch'):
            originals[name] = getattr(_patching, name)
            setattr(_patching, name, observe_patch(name, originals[name]))
        def restore():
            for name, original in originals.items():
                setattr(_patching, name, original)
        config.add_cleanup(restore)

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items):
    for item in items:
        emit('mark', item.nodeid, hypothesis=item.get_closest_marker('hypothesis') is not None)

@pytest.fixture
def resource(request):
    values = []
    emit('fixture-setup', request.node.nodeid)
    yield values
    emit('fixture-teardown', request.node.nodeid, values=values)

@pytest.fixture(autouse=True)
def observe_examples(request, resource, record_property, record_testsuite_property):
    def observe(value):
        resource.append(value)
        emit('example', request.node.nodeid, value=value, size=len(resource),
             label=Path(request.module.__file__).stem)
        record_property('example', value)
        record_testsuite_property('example:' + request.node.nodeid, value)
    request.module.observe = observe
    yield
    del request.module.observe

def report_values(report):
    return dict(when=report.when, outcome=report.outcome,
                hypothesis='hypothesis' in report.keywords,
                statistics=report.__dict__.get('_hypothesis_stats'),
                examples=report.__dict__.get('_hypothesis_failing_examples'),
                sections=report.sections,
                diagnostic=str(report.longrepr) if report.longrepr is not None else None)

def pytest_runtest_logreport(report):
    emit('report', report.nodeid, **report_values(report))

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_terminal_summary(terminalreporter):
    seen = set()
    for reports in terminalreporter.stats.values():
        for report in reports:
            if hasattr(report, 'when') and id(report) not in seen:
                seen.add(id(report))
                emit('summary-report', report.nodeid, **report_values(report))
    return (yield)
"""

_CASE = """from hypothesis import event, example, given, note, strategies as st
from product import allowed

@example(value=0)
@given(value=st.integers(min_value=0, max_value=10))
def test_property(value):
    observe(value)
    event('observed example')
    note('observed value=' + str(value))
    assert allowed(value)
"""


def _other_plugins() -> list[str]:
    # Both commands retain their required coverage option service alongside Hypothesis.
    names = {
        entry.name
        for distribution in distributions()
        for entry in distribution.entry_points
        if entry.group == "pytest11" and entry.name not in {"hypothesispytest", "pytest_cov"}
    }
    return [argument for name in sorted(names) for argument in ("-p", f"no:{name}")]


def _events(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def _by_node(events: list[dict[str, Any]], kind: str) -> dict[str, list[dict[str, Any]]]:
    values: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event["kind"] == kind:
            values[event["nodeid"]].append(event)
    return dict(values)


def _properties(path: Path) -> list[tuple[str, str]]:
    return [
        (element.attrib["name"], element.attrib["value"])
        for element in ET.parse(path).findall("./testsuite/properties/property")
    ]


def _assert_reports(events: list[dict[str, Any]], stdout: str) -> None:
    reports = _by_node(events, "report")
    observations = _by_node(events, "statistics")
    summaries = _by_node(events, "summary-report")
    assert reports.keys() == summaries.keys()
    for nodeid, phases in reports.items():
        assert [phase["when"] for phase in phases] == ["setup", "call", "teardown"]
        statistics = phases[-1]["statistics"]
        assert isinstance(statistics, str)
        assert nodeid in statistics
        assert statistics in stdout
        assert [item["text"] for item in observations[nodeid]] == [statistics]
        assert all(phase["hypothesis"] for phase in phases)
        projected = [phase for phase in summaries[nodeid] if phase["when"] == "teardown"]
        assert projected
        for projection in projected:
            assert projection["statistics"] == statistics
            assert projection["examples"] == phases[-1]["examples"]
            assert projection["sections"] == phases[-1]["sections"]
            assert projection["hypothesis"]


def _assert_cleanup(events: list[dict[str, Any]]) -> None:
    configured = {event["pid"] for event in events if event["kind"] == "configure"}
    assert configured
    assert all(event["active"] for event in events if event["kind"] == "configure")
    for kind in ("cleanup", "shutdown"):
        assert {event["pid"] for event in events if event["kind"] == kind} == configured
    setup = _by_node(events, "fixture-setup")
    teardown = _by_node(events, "fixture-teardown")
    examples = _by_node(events, "example")
    assert setup.keys() == teardown.keys() == examples.keys()
    for nodeid in setup:
        assert len(setup[nodeid]) == len(teardown[nodeid]) == 1
        assert teardown[nodeid][0]["values"] == [event["value"] for event in examples[nodeid]]
    assert all(event["hypothesis"] for event in events if event["kind"] == "mark")


def _assert_junit(events: list[dict[str, Any]], path: Path) -> None:
    properties = _properties(path)
    reports = _by_node(events, "report")
    for nodeid, phases in reports.items():
        statistics = [
            value for name, value in properties if name == "hypothesis-statistics-" + nodeid
        ]
        assert len(statistics) == 1
        assert base64.b64decode(statistics[0], validate=True).decode() == phases[-1]["statistics"]
        assert [value for name, value in properties if name == "example:" + nodeid] == [
            str(event["value"]) for event in _by_node(events, "example")[nodeid]
        ]


def _assert_patch(events: list[dict[str, Any]], root: Path, *, broken: bool) -> None:
    calls = [event for event in events if event["kind"] == "patch"]
    if not broken:
        assert calls == []
        return
    assert [event["operation"] for event in calls] == ["make_patch", "gc_patches", "save_patch"]
    assert len({event["pid"] for event in calls}) == 1
    patch = root / calls[-1]["result"]
    assert patch.read_text() == calls[0]["result"]
    assert "discovered failure" in patch.read_text()
    for phases in _by_node(events, "report").values():
        triple = json.loads(phases[-1]["examples"])
        assert len(triple) == 3
        assert all(isinstance(part, str) for part in triple)
        function = ast.parse(triple[2]).body[0]
        assert isinstance(function, ast.FunctionDef)
        decorators = [
            decorator.func.value
            for decorator in function.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "via"
        ]
        assert any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "example"
            and [(item.arg, ast.literal_eval(item.value)) for item in decorator.keywords]
            == [("value", 3)]
            for decorator in decorators
        )


def _exercise(tmp_path: Path, workers: int, *, patches: bool) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\njunit_family=xunit1\n")
    project.write("proof.py", _PROOF)
    project.write("conftest.py", _CONFIG)
    for name in ("alpha", "beta"):
        project.write(f"test_{name}.py", _CASE)
    options = (
        *_other_plugins(),
        "-p",
        "pytest_cov",
        "--no-cov",
        "--cov-context=test",
        "-p",
        "hypothesispytest",
        "--hypothesis-profile=workers-proof",
        "--hypothesis-seed=17",
        "--hypothesis-show-statistics",
        "-q",
    )
    for stage, broken in (("passing", False), ("failing", True), ("repaired", False)):
        project.write(
            "product.py", f"def allowed(value): return {'value < 3' if broken else 'True'}\n"
        )
        expected: list[dict[str, Any]] = []
        for engine in ("stock", "native"):
            directory = tmp_path / f"{stage}-{engine}"
            environment = {
                "HYPOTHESIS_PROOF": str(directory),
                "HYPOTHESIS_PATCH_PROOF": str(int(patches)),
                "HYPOTHESIS_STORAGE_DIRECTORY": str(directory / ".hypothesis"),
            }
            arguments = (*options, f"--junitxml={stage}-{engine}.xml")
            result = (
                project.run_python("-m", "pytest", *arguments, env=environment)
                if engine == "stock"
                else project.run_vfy(
                    "test",
                    "--engine=native",
                    f"--workers={workers}",
                    "--native-result-json=native.json",
                    *arguments,
                    env=environment,
                )
            )
            project.write(f"{stage}-{engine}.stdout", result.stdout)
            project.write(f"{stage}-{engine}.stderr", result.stderr)
            assert result.returncode == int(broken), result.stdout + result.stderr
            events = _events(directory)
            _assert_reports(events, result.stdout)
            _assert_cleanup(events)
            _assert_junit(events, tmp_path / f"{stage}-{engine}.xml")
            if patches:
                _assert_patch(events, tmp_path, broken=broken)
            if engine == "stock":
                expected = events
            else:
                _assert_native(project, events, expected, workers)


def _assert_native(
    project: VfyProject, events: list[dict[str, Any]], expected: list[dict[str, Any]], workers: int
) -> None:
    for kind in ("example", "report"):
        actual_nodes = _by_node(events, kind)
        expected_nodes = _by_node(expected, kind)
        assert actual_nodes.keys() == expected_nodes.keys()
        for nodeid, values in actual_nodes.items():
            fields = (
                ("value", "size", "label")
                if kind == "example"
                else ("when", "outcome", "sections", "hypothesis")
            )
            assert [[item[field] for field in fields] for item in values] == [
                [item[field] for field in fields] for item in expected_nodes[nodeid]
            ]
    statistics = _by_node(events, "statistics")
    expected_statistics = _by_node(expected, "statistics")
    assert statistics.keys() == expected_statistics.keys()
    for nodeid, values in statistics.items():
        assert [_statistics_semantics(item["raw"]) for item in values] == [
            _statistics_semantics(item["raw"]) for item in expected_statistics[nodeid]
        ]
    native = json.loads((project.root / "native.json").read_text())
    topology = native["execution_topology"]
    assert topology["requested_workers"] == topology["active_workers"] == workers
    pids = topology["worker_pids"]
    assert len(pids) == len(set(pids)) == workers
    assert {event["pid"] for event in events if event["kind"] == "example"} == set(pids)
    reports = _by_node(events, "report")
    for case in native["cases"]:
        summary = case["phases"][-1]["report_summary"]
        assert summary["hypothesis_statistics"] == reports[case["nodeid"]][-1]["statistics"]
        examples = reports[case["nodeid"]][-1]["examples"]
        assert summary.get("hypothesis_failing_examples") == examples


def _statistics_semantics(statistics: dict[str, Any]) -> dict[str, Any]:
    # Compare engine decisions; complete measured timings and rendered text remain in the raw proof.
    result = {name: value for name, value in statistics.items() if not name.endswith("-phase")}
    for name, phase in statistics.items():
        if name.endswith("-phase"):
            result[name] = {
                "distinct-failures": phase["distinct-failures"],
                "shrinks-successful": phase["shrinks-successful"],
                "test-cases": [
                    {"status": case["status"], "events": case["events"]}
                    for case in phase["test-cases"]
                ],
            }
    return result


@pytest.mark.parametrize("workers", [1, 2])
def test_hypothesis_worker_statistics_examples_and_cleanup(tmp_path: Path, workers: int) -> None:
    _exercise(tmp_path, workers, patches=False)


@pytest.mark.parametrize("workers", [1, 2])
def test_hypothesis_worker_patches_publish_once(tmp_path: Path, workers: int) -> None:
    # Requires the real Hypothesis codemods dependencies; a missing dependency is a failed proof.
    _exercise(tmp_path, workers, patches=True)
