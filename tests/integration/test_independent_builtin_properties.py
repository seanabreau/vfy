"""Built-in fixture values retain their public owners and reporting effects."""

from __future__ import annotations

import ast
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()

_NAMESPACE_TEST = """import pytest

saved = []

@pytest.fixture(scope='session')
def namespace_owner(doctest_namespace):
    saved.append(doctest_namespace)
    doctest_namespace['mutable'] = []
    yield doctest_namespace
    assert doctest_namespace['mutable'] == ['first', 'second']

@pytest.fixture(scope='session', autouse=True)
def namespace_alias(namespace_owner, request):
    assert request.getfixturevalue('doctest_namespace') is namespace_owner

def test_first(doctest_namespace, request):
    assert request.getfixturevalue('doctest_namespace') is doctest_namespace
    assert doctest_namespace is saved[0]
    doctest_namespace['mutable'].append('first')

def test_second(doctest_namespace):
    assert doctest_namespace is saved[0]
    doctest_namespace['mutable'].append('second')
"""

_PROPERTY_TEST = """import pytest

@pytest.fixture
def property_owner(record_property, request):
    request.node.user_properties = [('before', 'replaced')]
    record_property(name='setup', value=['original'])
    assert request.node.user_properties[-1][1] == ['original']
    yield record_property
    record_property('teardown', '<&>')

def test_property(property_owner, record_property, request):
    assert property_owner is record_property
    request.node.user_properties[-1][1].append('mutated')
    record_property('call', 3)
"""

_PROPERTY_OBSERVER = """from pathlib import Path
import json

def pytest_runtest_logreport(report):
    with Path('reports.jsonl').open('a') as stream:
        stream.write(json.dumps([report.when, report.outcome, report.user_properties]) + '\\n')

def pytest_warning_recorded(warning_message, when, nodeid):
    with Path('warnings.jsonl').open('a') as stream:
        row = [type(warning_message.message).__name__,
               str(warning_message.message), when, nodeid]
        stream.write(json.dumps(row) + '\\n')
"""


def test_doctest_namespace_uses_the_existing_session_fixture_lifetime(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_namespace.py", _NAMESPACE_TEST)
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python("-m", module, "-q", env={"PYTHONPATH": _SOURCE})
        result.expect(0)


@pytest.mark.parametrize("family", ["legacy", "xunit2"])
def test_record_property_uses_actual_item_reports_and_junit(tmp_path: Path, family: str) -> None:
    project = VfyProject(tmp_path)
    project.write("test_property.py", _PROPERTY_TEST)
    project.write("conftest.py", _PROPERTY_OBSERVER)
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--junitxml=results.xml",
            "-o",
            f"junit_family={family}",
            env={"PYTHONPATH": _SOURCE},
        )
        result.expect(0)
        reports = [
            json.loads(row) for row in (project.root / "reports.jsonl").read_text().splitlines()
        ]
        properties = [
            element.attrib
            for element in ET.parse(project.root / "results.xml").findall(
                ".//testcase/properties/property"
            )
        ]
        warning_path = project.root / "warnings.jsonl"
        warnings = (
            [json.loads(row) for row in warning_path.read_text().splitlines()]
            if warning_path.exists()
            else []
        )
        snapshots.append((reports, properties, warnings))
        (project.root / "reports.jsonl").unlink()
        if warning_path.exists():
            warning_path.unlink()
    assert snapshots[0] == snapshots[1]
    assert [row[0] for row in snapshots[0][0]] == ["setup", "call", "teardown"]
    assert [row["name"] for row in snapshots[0][1]] == ["before", "setup", "call", "teardown"]
    assert len(snapshots[0][2]) == (1 if family == "xunit2" else 0)


@pytest.mark.parametrize(
    ("plugin", "fixture"), [("doctest", "doctest_namespace"), ("junitxml", "record_property")]
)
def test_disabled_builtin_plugins_do_not_advertise_their_fixture(
    tmp_path: Path,
    plugin: str,
    fixture: str,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_disabled.py",
        f"""import pytest

def test_missing(request):
    with pytest.raises(pytest.FixtureLookupError):
        request.getfixturevalue({fixture!r})
""",
    )
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m", module, "-q", "-p", f"no:{plugin}", env={"PYTHONPATH": _SOURCE}
        )
        result.expect(0)


@pytest.mark.parametrize(
    ("plugin", "fixture"), [("doctest", "doctest_namespace"), ("junitxml", "record_property")]
)
@pytest.mark.parametrize(
    ("hook", "priority"),
    [
        ("pytest_configure", ""),
        ("pytest_sessionstart", "tryfirst=True"),
        ("pytest_sessionstart", ""),
        ("pytest_sessionstart", "trylast=True"),
        ("pytest_collection_finish", ""),
    ],
)
def test_registered_builtin_fixture_survives_later_plugin_block(
    tmp_path: Path,
    plugin: str,
    fixture: str,
    hook: str,
    priority: str,
) -> None:
    project = VfyProject(tmp_path)
    argument = "config" if hook == "pytest_configure" else "session"
    manager = "config.pluginmanager" if argument == "config" else "session.config.pluginmanager"
    project.write(
        "conftest.py",
        f"import pytest\n\n@pytest.hookimpl({priority})\n"
        f"def {hook}({argument}):\n    {manager}.set_blocked({plugin!r})\n",
    )
    project.write(
        "test_registered.py",
        f"""def test_retained(request):
    first = request.getfixturevalue({fixture!r})
    assert request.getfixturevalue({fixture!r}) is first
""",
    )
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python("-m", module, "-q", env={"PYTHONPATH": _SOURCE})
        result.expect(0)


_PROPERTY_FINALIZATION = """from pathlib import Path
import pytest

class Value:
    def __init__(self, label):
        self.label = label

    def __str__(self):
        with Path('conversions.txt').open('a') as stream:
            stream.write(self.label + '\\n')
        return self.label + '<&>\\x00\\ud800'

@pytest.fixture
def recorded(record_property):
    value = Value('call')
    record_property('call', value)
    yield
    value.label = 'changed'
    record_property('teardown', Value('teardown'))
    raise RuntimeError('teardown failed')

def test_failure(recorded):
    assert False, 'call failed'
"""


def test_record_property_finalizes_each_double_failure_report(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_finalization.py", _PROPERTY_FINALIZATION)
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--junitxml=results.xml",
            "-o",
            "junit_family=legacy",
            env={"PYTHONPATH": _SOURCE},
        )
        result.expect(1)
        cases = ET.parse(project.root / "results.xml").findall(".//testcase")
        snapshots.append(
            (
                [
                    [element.attrib for element in case.findall("./properties/property")]
                    for case in cases
                ],
                (project.root / "conversions.txt").read_text().splitlines(),
            )
        )
        (project.root / "conversions.txt").unlink()
    assert snapshots[0] == snapshots[1]
    assert [len(properties) for properties in snapshots[0][0]] == [1, 2]
    assert snapshots[0][1] == ["changed", "changed", "teardown"]


_PROPERTY_HOOKS = """from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'teardown':
        report.user_properties = [('replacement', 'makereport')]
    return report

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    if report.when == 'teardown':
        report.user_properties.append(('before-writer', 'included'))

class Late:
    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        if report.when == 'teardown':
            report.user_properties.append(('after-writer', 'excluded'))

def pytest_configure(config):
    config.pluginmanager.register(Late(), 'late-property-observer')
"""


def test_record_property_uses_writer_hook_position_after_report_replacement(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _PROPERTY_HOOKS)
    project.write(
        "test_hook.py", "def test_hook(record_property):\n    record_property('old', 1)\n"
    )
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--junitxml=results.xml",
            "-o",
            "junit_family=legacy",
            env={"PYTHONPATH": _SOURCE},
        )
        result.expect(0)
        snapshots.append(
            [
                element.attrib
                for element in ET.parse(project.root / "results.xml").findall(
                    ".//testcase/properties/property"
                )
            ]
        )
    assert (
        snapshots[0]
        == snapshots[1]
        == [
            {"name": "replacement", "value": "makereport"},
            {"name": "before-writer", "value": "included"},
        ]
    )


@pytest.mark.parametrize(
    ("name_literal", "expected_exit"), [("'control\\x01'", 0), ("'surrogate\\udfff'", 1)]
)
def test_record_property_preserves_name_publication_semantics(
    tmp_path: Path, name_literal: str, expected_exit: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_name.py",
        f"def test_name(record_property):\n    record_property({name_literal}, 1)\n",
    )
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--junitxml=results.xml",
            "-o",
            "junit_family=legacy",
            env={"PYTHONPATH": _SOURCE},
        )
        assert result.returncode == expected_exit, (result.stdout, result.stderr)
        if expected_exit:
            assert "UnicodeEncodeError" in result.stdout + result.stderr
        else:
            snapshots.append(
                b'name="control\x01" value="1"' in (project.root / "results.xml").read_bytes()
            )
    if not expected_exit:
        assert snapshots == [True, True]


def test_record_property_preserves_duplicate_case_identity(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_duplicate.py",
        """count = 0

def test_duplicate(record_property):
    global count
    count += 1
    record_property('count', count)
""",
    )
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--keep-duplicates",
            "test_duplicate.py",
            "test_duplicate.py",
            "--junitxml=results.xml",
            "-o",
            "junit_family=legacy",
            env={"PYTHONPATH": _SOURCE},
        )
        result.expect(0)
        snapshots.append(
            [
                [element.attrib for element in case.findall("./properties/property")]
                for case in ET.parse(project.root / "results.xml").findall(".//testcase")
            ]
        )
    assert (
        snapshots[0]
        == snapshots[1]
        == [
            [{"name": "count", "value": "1"}],
            [{"name": "count", "value": "2"}],
        ]
    )


_PROPERTY_ITERABLE = """from pathlib import Path
import pytest

def event(message):
    with Path('iterable-events.txt').open('a') as stream:
        stream.write(message + '\\n')

class Name:
    def __str__(self):
        event('name')
        return 'iterable'

class Value:
    def __str__(self):
        event('value')
        return 'retained'

class Pair:
    def __iter__(self):
        event('pair')
        yield Name()
        yield Value()
        event('pair-done')

class Properties:
    def __iter__(self):
        event('properties')
        yield Pair()
        event('properties-done')

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'teardown':
        report.user_properties = Properties()
    return report
"""


def test_record_property_report_replacement_accepts_iterables(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _PROPERTY_ITERABLE)
    project.write("test_iterable.py", "def test_iterable(record_property):\n    pass\n")
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--junitxml=results.xml",
            "-o",
            "junit_family=legacy",
            env={"PYTHONPATH": _SOURCE},
        )
        result.expect(0)
        snapshots.append(
            (
                (project.root / "iterable-events.txt").read_text().splitlines(),
                [
                    element.attrib
                    for element in ET.parse(project.root / "results.xml").findall(
                        ".//testcase/properties/property"
                    )
                ],
            )
        )
        (project.root / "iterable-events.txt").unlink()
    assert (
        snapshots[0]
        == snapshots[1]
        == (
            ["properties", "pair", "pair-done", "value", "name", "properties-done"],
            [{"name": "iterable", "value": "retained"}],
        )
    )


@pytest.mark.parametrize(
    "pair",
    ["()", "('one',)", "('one', 'two', 'three')"],
)
def test_record_property_preserves_report_pair_unpack_errors(tmp_path: Path, pair: str) -> None:
    with pytest.raises(ValueError, match="values to unpack") as reference:
        _name, _value = ast.literal_eval(pair)
    message = str(reference.value)
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'teardown':
        report.user_properties = iter([{pair}])
    return report
""",
    )
    project.write("test_pair.py", "def test_pair(record_property):\n    pass\n")
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--junitxml=results.xml",
            "-o",
            "junit_family=legacy",
            env={"PYTHONPATH": _SOURCE},
        )
        result.expect(3)
        assert f"ValueError: {message}" in result.stdout + result.stderr


_PROPERTY_LIFETIME = r"""from pathlib import Path
import json
import weakref
import pytest

refs = {}
saved = {}

def event(label):
    row = [label, {key: ref() is not None for key,ref in refs.items()}]
    with Path('events.jsonl').open('a') as stream:
        stream.write(json.dumps(row) + '\n')

class Text(str):
    def __new__(cls, text, label):
        value = super().__new__(cls, text)
        value.label = label
        refs[label] = weakref.ref(value)
        return value
    def __str__(self):
        event('text.str:' + self.label)
        return self
    def __del__(self):
        event('text.del:' + self.label)

class Name:
    def __init__(self, label):
        self.label = label
        refs['name:' + label] = weakref.ref(self)
    def __str__(self):
        event('name.str:' + self.label)
        return Text(self.label, 'name-text:' + self.label)
    def __del__(self):
        report = saved.get('report')
        if report is not None:
            report.user_properties = report.user_properties
        event('name.del:' + self.label)

class Value:
    def __init__(self, label, raises=False):
        self.label = label
        self.raises = raises
        refs['value:' + label] = weakref.ref(self)
    def __str__(self):
        event('value.str:' + self.label)
        if self.raises:
            raise RuntimeError('later conversion failed')
        return Text('visible:' + self.label, 'value-text:' + self.label)
    def __del__(self):
        report = saved.get('report')
        if report is not None:
            report.user_properties = report.user_properties
        event('value.del:' + self.label)

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == 'teardown':
        report.user_properties = properties()
        saved['report'] = report
    return report

def pytest_internalerror(excrepr, excinfo):
    event('internalerror')

@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    event('sessionfinish:first')

class Last:
    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        event('sessionfinish:last')
        Path('finish.xml').write_bytes(Path('results.xml').read_bytes())
    @pytest.hookimpl(trylast=True)
    def pytest_unconfigure(self, config):
        event('unconfigure:last')

def pytest_configure(config):
    config.pluginmanager.register(Last(), 'lifetime-last')
"""

_PROPERTY_LIFETIME_VARIANTS = {
    "partial-bad": """def properties():
    yield ('first', 'visible')
    event('before-bad-pair')
    yield ('bad',)
""",
    "partial-raise": """def properties():
    yield ('first', 'visible')
    event('before-raising-value')
    yield ('second', Value('bad', raises=True))
""",
    "lifetime-next": """def properties():
    yield (Name('first'), Value('first'))
    event('next:first')
    yield (Name('second'), Value('second'))
    event('next:second')
""",
    "lifetime-bad": """def properties():
    yield (Name('first'), Value('first'))
    event('next:first')
    yield ('bad',)
""",
    "lifetime-raise": """def properties():
    yield (Name('first'), Value('first'))
    event('next:first')
    yield (Name('second'), Value('second', raises=True))
""",
}


@pytest.mark.parametrize("variant", ["partial-bad", "partial-raise"])
def test_record_property_retains_successful_prefix_after_later_error(
    tmp_path: Path, variant: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", _PROPERTY_LIFETIME + "\n" + _PROPERTY_LIFETIME_VARIANTS[variant])
    project.write("test_case.py", "def test_case(record_property):\n    pass\n")
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        project.write("results.xml", "previous artifact")
        result = project.run_python(
            "-m",
            module,
            "-q",
            "--junitxml=results.xml",
            "-o",
            "junit_family=legacy",
            env={"PYTHONPATH": _SOURCE},
        )
        result.expect(3)
        assert (project.root / "finish.xml").read_bytes() == (
            project.root / "results.xml"
        ).read_bytes()
        suite = ET.parse(project.root / "results.xml").find(".//testsuite")
        assert suite is not None
        cases = suite.findall("./testcase")
        snapshots.append(
            (
                {key: suite.attrib[key] for key in ("errors", "failures", "skipped", "tests")},
                [
                    (
                        {key: value for key, value in case.attrib.items() if key != "time"},
                        [element.attrib for element in case.findall("./properties/property")],
                        [
                            (element.tag, element.attrib)
                            for element in case
                            if element.tag != "properties"
                        ],
                    )
                    for case in cases
                ],
            )
        )
        assert "previous artifact" not in (project.root / "results.xml").read_text()
        (project.root / "events.jsonl").unlink()
    assert snapshots[0] == snapshots[1]
    assert snapshots[0][0] == {"errors": "1", "failures": "0", "skipped": "0", "tests": "2"}
    assert snapshots[0][1][0][1] == [{"name": "first", "value": "visible"}]
    assert snapshots[0][1][1][0] == {"classname": "pytest", "name": "internal"}


_ORDINARY_LOGREPORT_FAILURE = r"""from pathlib import Path
import json
import pytest
made = []
logged = []
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    made.append([report.when,report.outcome])
    return report
@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    logged.append([report.when,report.outcome])
    if report.when == PHASE:
        raise RuntimeError('ordinary report hook failed')
def pytest_sessionfinish(session, exitstatus):
    Path('ordinary.json').write_text(json.dumps([made,logged,int(exitstatus),session.testsfailed]))
"""


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_real_report_prefix_survives_ordinary_logreport_error(tmp_path: Path, phase: str) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", f"PHASE = {phase!r}\n" + _ORDINARY_LOGREPORT_FAILURE)
    project.write("test_case.py", "def test_case():\n    pass\n")
    snapshots = []
    for module in ("pytest", "vfy.independent.entry"):
        result = project.run_python("-m", module, "-q", env={"PYTHONPATH": _SOURCE})
        result.expect(3)
        assert "RuntimeError: ordinary report hook failed" in result.stdout + result.stderr
        snapshots.append(json.loads((project.root / "ordinary.json").read_text()))
    assert snapshots[0] == snapshots[1]
    expected = [[which, "passed"] for which in ("setup", "call", "teardown")]
    expected = expected[: ("setup", "call", "teardown").index(phase) + 1]
    assert snapshots[0] == [expected, expected, 3, 0]
