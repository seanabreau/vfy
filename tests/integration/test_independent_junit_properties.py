"""Suite properties keep stock conversion, publication order, and XML values."""

from __future__ import annotations

import errno
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import ENGINES, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, engine: str, *, xml: bool = True) -> tuple[int, str]:
    arguments = ["--junitxml=result.xml"] if xml else []
    result = project.run_engine(engine, "-q", *arguments)
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    project.write(f"{engine}.status.json", json.dumps({"exit_status": result.returncode}))
    return result.returncode, result.stdout + result.stderr


def _properties(project: VfyProject) -> list[tuple[str, str]]:
    tree = ET.parse(project.root / "result.xml")
    return [
        (element.attrib["name"], element.attrib["value"])
        for element in tree.findall("./testsuite/properties/property")
    ]


@ENGINES
@pytest.mark.parametrize("broken", [False, True])
def test_suite_property_values_escape_and_keep_duplicate_order(
    tmp_path: Path, engine: str, broken: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("product.py", f"def answer(): return {0 if broken else 42}\n")
    xml_value = "\x00\x04\x7f\ud800\udfff\ufffe\t\n\r<&\"'Ω😀"
    project.write(
        "test_value.py",
        f"""from product import answer

def test_value(record_testsuite_property):
    record_testsuite_property('build <&"', 'first')
    record_testsuite_property('build <&"', {xml_value!r})
    record_testsuite_property('count', 42)
    assert answer() == 42
""",
    )
    code, output = _run(project, engine)
    assert code == int(broken), output
    assert _properties(project) == [
        ('build <&"', "first"),
        ('build <&"', "#x00#x04#x7F#xD800#xDFFF#xFFFE\t\n\r<&\"'Ω😀"),
        ("count", "42"),
    ]


@ENGINES
def test_suite_property_publication_precedes_late_finish_and_cleanup(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
record = None

@pytest.fixture(scope='session', autouse=True)
def properties(record_testsuite_property, pytestconfig):
    global record
    record = record_testsuite_property
    record('setup', 'one')
    pytestconfig.add_cleanup(lambda: record('cleanup', 'late'))
    yield
    record('fixture-finalizer', 'two')

@pytest.hookimpl(tryfirst=True, specname='pytest_sessionfinish')
def pytest_first_finish(session, exitstatus):
    record('first-finish', 'three')

@pytest.hookimpl(specname='pytest_sessionfinish')
def pytest_normal_finish(session, exitstatus):
    properties = ET.parse('result.xml').findall('./testsuite/properties/property')
    Path('visible-at-finish.json').write_text(json.dumps([
        [element.attrib['name'], element.attrib['value']] for element in properties
    ]))
    record('normal-finish', 'late')

@pytest.hookimpl(trylast=True, specname='pytest_sessionfinish')
def pytest_last_finish(session, exitstatus):
    record('last-finish', 'late')

def pytest_unconfigure(config):
    record('unconfigure', 'late')
    Path('cleanup-ran').touch()
""",
    )
    project.write("test_value.py", "def test_value(record_testsuite_property): pass\n")
    code, output = _run(project, engine)
    assert code == 0, output
    assert (tmp_path / "cleanup-ran").exists()
    assert json.loads((tmp_path / "visible-at-finish.json").read_text()) == [
        ["setup", "one"],
        ["fixture-finalizer", "two"],
        ["first-finish", "three"],
    ]
    assert _properties(project) == [
        ("setup", "one"),
        ("fixture-finalizer", "two"),
        ("first-finish", "three"),
    ]


@ENGINES
def test_junit_file_error_stops_later_finish_and_still_unconfigures(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

def event(value):
    with Path('events').open('a') as output: output.write(value + '\\n')

def pytest_configure(config):
    config.add_cleanup(lambda: event('cleanup'))

@pytest.hookimpl(tryfirst=True, specname='pytest_sessionfinish')
def pytest_first_finish(session, exitstatus): event('first finish')

@pytest.hookimpl(specname='pytest_sessionfinish')
def pytest_normal_finish(session, exitstatus): event('normal finish')

def pytest_unconfigure(config): event('unconfigure')

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    try:
        yield
    except BaseException as error:
        import json
        Path('io-error-type').write_text(type(error).__name__)
        Path('io-error-attributes.json').write_text(json.dumps({
            'errno': error.errno, 'filename': error.filename, 'args': error.args,
        }))
        raise
""",
    )
    project.write(
        "test_value.py",
        "from pathlib import Path\ndef test_value(): Path('result.xml').mkdir()\n",
    )
    code, output = _run(project, engine)
    assert code == 1, output
    assert "directory" in output.lower(), output
    assert (tmp_path / "events").read_text().splitlines() == [
        "first finish",
        "unconfigure",
        "cleanup",
    ]
    assert (tmp_path / "result.xml").is_dir()
    assert (tmp_path / "io-error-type").read_text() == "IsADirectoryError"
    assert json.loads((tmp_path / "io-error-attributes.json").read_text()) == {
        "errno": errno.EISDIR,
        "filename": str(tmp_path / "result.xml"),
        "args": [errno.EISDIR, os.strerror(errno.EISDIR)],
    }


@pytest.mark.parametrize("broken", [False, True])
def test_worker_suite_properties_reach_controller_in_assignment_order(
    tmp_path: Path, broken: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    for label in ("a", "b"):
        project.write(
            f"test_{label}.py",
            f"""def test_value(record_testsuite_property):
    record_testsuite_property('worker', {label!r})
    assert {not (broken and label == "b")!r}
""",
        )
    code, output = _run(project, "stock")
    assert code == int(broken), output
    expected = _properties(project)
    assert expected == [("worker", "a"), ("worker", "b")]
    (tmp_path / "result.xml").unlink()
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-j", "2", "--junitxml=result.xml"
    )
    project.write("independent.stdout", result.stdout)
    project.write("independent.stderr", result.stderr)
    assert result.returncode == int(broken), result.stdout + result.stderr
    assert _properties(project) == expected
    cases = ET.parse(tmp_path / "result.xml").findall("./testsuite/testcase")
    assert len(cases) == 2
    assert sum(case.find("failure") is not None for case in cases) == int(broken)


@ENGINES
@pytest.mark.parametrize("parent", ["blocked", "blocked/nested"])
@pytest.mark.parametrize("surrogate", [False, True])
def test_junit_parent_creation_failure_keeps_operation_metadata_and_precedes_encoding(
    tmp_path: Path, engine: str, parent: str, surrogate: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path
import pytest

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    assert session.testsfailed == 0
    try:
        yield
    except OSError as error:
        Path('io-error.json').write_text(json.dumps({
            'type': type(error).__name__, 'errno': error.errno,
            'filename': error.filename, 'args': error.args,
        }))
        raise
""",
    )
    name = "name\ud800" if surrogate else "name"
    project.write(
        "test_value.py",
        f"""from pathlib import Path
def test_value(record_testsuite_property):
    record_testsuite_property({name!r}, 'value')
    Path('blocked').write_text('preserved parent file')
    Path('body-completed').touch()
""",
    )
    arguments = ("-q", f"--junitxml={parent}/result.xml")
    result = project.run_engine(engine, *arguments)
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    result.expect(1)
    code = errno.EEXIST if parent == "blocked" else errno.ENOTDIR
    assert json.loads((tmp_path / "io-error.json").read_text()) == {
        "type": "FileExistsError" if parent == "blocked" else "NotADirectoryError",
        "errno": code,
        "filename": str(tmp_path / parent),
        "args": [code, os.strerror(code)],
    }
    assert (tmp_path / "body-completed").exists()
    assert (tmp_path / "blocked").read_text() == "preserved parent file"


@ENGINES
def test_surrogate_suite_property_name_fails_encoding_at_publication(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """from pathlib import Path
import pytest

def event(value):
    with Path('events').open('a') as output: output.write(value + '\\n')

def pytest_configure(config):
    config.add_cleanup(lambda: event('cleanup'))

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    assert exitstatus == 0 and session.testsfailed == 0
    assert not Path('result.xml').exists()
    event('finish')
    try:
        yield
    except UnicodeEncodeError:
        event('encoding error')
        raise

@pytest.hookimpl(trylast=True, specname='pytest_sessionfinish')
def pytest_late_finish(session): event('late finish')

def pytest_unconfigure(config): event('unconfigure')
""",
    )
    name = "beforeΩ😀\ud800\udfffafter"
    project.write(
        "test_value.py",
        f"""from pathlib import Path
def test_value(record_testsuite_property):
    record_testsuite_property({name!r}, 'value')
    Path('body-completed').touch()
""",
    )
    code, output = _run(project, engine)
    assert code == 1, output
    assert (tmp_path / "body-completed").exists()
    assert (tmp_path / "events").read_text().splitlines() == [
        "finish",
        "encoding error",
        "unconfigure",
        "cleanup",
    ]
