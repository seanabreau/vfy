"""Node warnings retain live locations, original warning objects and session status."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import native_engines

_OBSERVER = """import json
from pathlib import Path

reports = []
recorded = []
sent = []

def pytest_runtest_logreport(report):
    reports.append([report.when, report.outcome])

def pytest_warning_recorded(warning_message, when, nodeid):
    recorded.append({
        'category': warning_message.category.__name__,
        'message': str(warning_message.message),
        'filename': warning_message.filename,
        'lineno': warning_message.lineno,
        'when': when,
        'nodeid': nodeid,
        'original': not sent or warning_message.message is sent[-1],
    })

def pytest_sessionfinish(session, exitstatus):
    Path('observations.json').write_text(json.dumps({
        'reports': reports, 'warnings': recorded,
        'testsfailed': session.testsfailed, 'exitstatus': int(exitstatus),
    }))
"""


def _run(project: VfyProject, engine: str, *arguments: str) -> CommandResult:
    result = project.run_engine(engine, "-q", *arguments)
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status", str(result.returncode))
    assert (project.root / "observations.json").is_file(), result.stdout + result.stderr
    return result


@native_engines
def test_node_warning_uses_real_item_source_and_original_message(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _OBSERVER)
    project.write(
        "test_warning.py",
        """import pytest
from conftest import sent

def test_warning(request):
    message = pytest.PytestWarning('actual node warning')
    sent.append(message)
    request.node.warn(warning=message)
    print('body reached')
""",
    )
    result = _run(project, engine)
    result.expect(0)
    data = json.loads((tmp_path / "observations.json").read_text())
    assert data == {
        "reports": [["setup", "passed"], ["call", "passed"], ["teardown", "passed"]],
        "warnings": [
            {
                "category": "PytestWarning",
                "message": "actual node warning",
                "filename": "test_warning.py",
                "lineno": 4,
                "when": "runtest",
                "nodeid": "test_warning.py::test_warning",
                "original": True,
            }
        ],
        "testsfailed": 0,
        "exitstatus": 0,
    }
    assert "actual node warning" in result.stdout
    assert result.stderr == ""


@native_engines
def test_node_warning_validates_before_live_location_and_resolves_emitter_before_arguments(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _OBSERVER)
    project.write(
        "test_warning.py",
        """import inspect
import json
from pathlib import Path
import warnings
import pytest

def test_warning(request):
    node = request.node
    events = []
    original = warnings.warn_explicit
    message = UserWarning('live message')
    class Bad:
        def __repr__(self):
            events.append('invalid repr')
            return 'bad-value'
    class Line:
        def __add__(self, value):
            events.append(['line add', value])
            return 17
    def replacement(*args, **kwargs):
        raise AssertionError('emitter was resolved after path conversion')
    class Filename:
        def __str__(self):
            events.append('path str')
            warnings.warn_explicit = replacement
            return 'caller-owned.py'
    class Fields:
        def __iter__(self):
            events.append('pair iter')
            yield Filename()
            yield Line()
            events.append('pair exhausted')
    class Location:
        def __getitem__(self, key):
            events.append(['location slice', repr(key)])
            return Fields()
    def emit(*args, **kwargs):
        events.append(['emit', args[0] is message, kwargs])
    node.location = Location()
    try:
        with pytest.raises(ValueError) as invalid:
            node.warn(Bad())
        assert str(invalid.value) == (
            'warning must be an instance of Warning or subclass, got bad-value'
        )
        assert events == ['invalid repr']
        warnings.warn_explicit = emit
        node.warn(message)
        node.location = ('none.py', None, 'ignored')
        with pytest.raises(AssertionError) as missing:
            node.warn(message)
        assert missing.value.args == ()
    finally:
        warnings.warn_explicit = original
        node.__dict__.pop('location', None)
    assert list(inspect.signature(node.warn).parameters) == ['warning']
    Path('events.json').write_text(json.dumps(events))
""",
    )
    result = _run(project, engine)
    result.expect(0)
    assert json.loads((tmp_path / "events.json").read_text()) == [
        "invalid repr",
        ["location slice", "slice(None, 2, None)"],
        "pair iter",
        "pair exhausted",
        "path str",
        ["line add", 1],
        ["emit", True, {"category": None, "filename": "caller-owned.py", "lineno": 17}],
    ]
    assert result.stderr == ""


@native_engines
def test_node_warning_location_cache_and_function_and_collector_fallbacks(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _OBSERVER)
    project.write(
        "test_warning.py",
        """import functools
import json
from pathlib import Path
import warnings

def alternate():
    pass

def test_warning(request):
    node = request.node
    old_reportinfo = node.reportinfo
    old_object = node.obj
    old_relpath = node.session._node_location_to_relpath
    calls = []
    def reportinfo():
        calls.append('reportinfo')
        return node.config.rootpath / 'virtual.py', 8, 'virtual'
    def relpath(path):
        calls.append(['relpath', isinstance(path, Path), path.name])
        return old_relpath(path)
    node.__dict__.pop('location', None)
    node.reportinfo = reportinfo
    node.session._node_location_to_relpath = relpath
    try:
        first = node.location
        assert node.location is first
        assert first == ('virtual.py', 8, 'virtual')
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            node.warn(UserWarning('cached'))
        assert calls == ['reportinfo', ['relpath', True, 'virtual.py']]
        assert (records[0].filename, records[0].lineno) == ('virtual.py', 9)
        node.location = None
        node.obj = functools.partial(alternate)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            node.warn(UserWarning('function fallback'))
        assert records[0].filename == str(Path(__file__).resolve())
        assert records[0].lineno == alternate.__code__.co_firstlineno
    finally:
        node.obj = old_object
        node.reportinfo = old_reportinfo
        node.session._node_location_to_relpath = old_relpath
        node.__dict__.pop('location', None)
    parent = node.session
    sentinel = object()
    old_parent_object = getattr(parent, 'obj', sentinel)
    old_parent_location = getattr(parent, 'location', sentinel)
    old_parent_path = parent.path
    parent.obj = None
    parent.location = None
    parent.path = Path('collector-owned.py')
    try:
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            parent.warn(UserWarning('collector fallback'))
        assert (records[0].filename, records[0].lineno) == ('collector-owned.py', 0)
    finally:
        parent.path = old_parent_path
        for name, value in [('obj', old_parent_object), ('location', old_parent_location)]:
            if value is sentinel:
                delattr(parent, name)
            else:
                setattr(parent, name, value)
    Path('callbacks.json').write_text(json.dumps(calls))
""",
    )
    result = _run(project, engine)
    result.expect(0)
    assert json.loads((tmp_path / "callbacks.json").read_text()) == [
        "reportinfo",
        ["relpath", True, "virtual.py"],
    ]
    assert result.stderr == ""


@native_engines
@pytest.mark.parametrize(
    ("fixture", "family", "writer"),
    [
        ("record_property", "xunit2", True),
        ("record_property", "legacy", True),
        ("record_property", "xunit2", False),
        ("record_xml_attribute", "xunit2", True),
        ("record_xml_attribute", "legacy", True),
        ("record_xml_attribute", "xunit2", False),
    ],
)
def test_property_fixture_warning_setup_reaches_actual_body_and_reports(
    tmp_path: Path, engine: str, fixture: str, family: str, writer: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _OBSERVER)
    project.write(
        "test_warning.py",
        f"from pathlib import Path\n\ndef test_warning({fixture}):\n"
        f"    {fixture}('actual', 42)\n"
        "    Path('body-ran').write_text('passed')\n",
    )
    arguments = ["-o", f"junit_family={family}"]
    if writer:
        arguments.append("--junitxml=result.xml")
    result = _run(project, engine, *arguments)
    result.expect(0)
    assert (tmp_path / "body-ran").read_text() == "passed"
    data = json.loads((tmp_path / "observations.json").read_text())
    assert data["reports"] == [["setup", "passed"], ["call", "passed"], ["teardown", "passed"]]
    assert data["testsfailed"] == data["exitstatus"] == 0
    expected = []
    if fixture == "record_xml_attribute":
        expected.append(
            ("PytestExperimentalApiWarning", "record_xml_attribute is an experimental feature")
        )
    if writer and family == "xunit2":
        expected.append(
            (
                "PytestWarning",
                f"{fixture} is incompatible with junit_family 'xunit2' (use 'legacy' or 'xunit1')",
            )
        )
    assert [(row["category"], row["message"]) for row in data["warnings"]] == expected
    assert all(
        row["filename"] == "test_warning.py"
        and row["lineno"] == 3
        and row["when"] == "runtest"
        and row["nodeid"] == "test_warning.py::test_warning"
        for row in data["warnings"]
    )
    if writer:
        cases = ET.parse(tmp_path / "result.xml").findall("./testsuite/testcase")
        assert len(cases) == 1
        assert cases[0].find("error") is None
        if fixture == "record_property":
            assert [
                (p.get("name"), p.get("value")) for p in cases[0].findall("./properties/property")
            ] == [("actual", "42")]
        else:
            assert cases[0].get("actual") == ("42" if family == "legacy" else None)
    assert result.stderr == ""


@native_engines
@pytest.mark.parametrize("failure", ["ordinary", "record_property", "record_xml_attribute"])
def test_setup_failure_keeps_nonzero_status_with_real_reports_and_junit(
    tmp_path: Path, engine: str, failure: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _OBSERVER)
    if failure == "ordinary":
        project.write(
            "conftest.py",
            _OBSERVER + "\nimport pytest\n@pytest.fixture\ndef owner():\n"
            "    raise ValueError('actual setup failure')\n",
        )
        fixture, message, category = "owner", "actual setup failure", "ValueError"
    else:
        fixture = failure
        category = (
            "PytestWarning" if failure == "record_property" else "PytestExperimentalApiWarning"
        )
        message = "incompatible" if failure == "record_property" else "experimental"
    project.write(
        "test_warning.py",
        f"from pathlib import Path\n\ndef test_warning({fixture}):\n"
        "    Path('body-ran').write_text('unreachable')\n",
    )
    arguments = ["--junitxml=result.xml", "-o", "junit_family=xunit2"]
    if failure != "ordinary":
        arguments.extend(["-W", f"error::pytest.{category}"])
    result = _run(project, engine, *arguments)
    result.expect(1)
    assert not (tmp_path / "body-ran").exists()
    data = json.loads((tmp_path / "observations.json").read_text())
    assert data["reports"] == [["setup", "failed"], ["teardown", "passed"]]
    assert data["testsfailed"] == data["exitstatus"] == 1
    assert data["warnings"] == []
    errors = ET.parse(tmp_path / "result.xml").findall("./testsuite/testcase/error")
    assert len(errors) == 1
    assert message in (errors[0].text or "")
    assert category in result.stdout
    assert result.stderr == ""
