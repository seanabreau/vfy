"""Report wires round-trip through in-process and real transports without changing results."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_COPY_HOOK = r"""import execnet, json, os, pickle, threading, pytest
from pathlib import Path
calls = []

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    data = report._to_json()
    assert type(data) is dict
    data['duration'] = 1.25
    data['sections'] = [*data['sections'], ('Captured stdout call', 'injected output')]
    expected = json.loads(json.dumps(data))
    for transported in (pickle.loads(pickle.dumps(data)), execnet.loads(execnet.dumps(data))):
        assert type(transported) is dict
        assert json.loads(json.dumps(transported)) == expected
        offline = type(report)._from_json(transported)
        assert offline.nodeid == report.nodeid and offline.when == report.when
    first = type(report)._from_json(data)
    second = type(report)._from_json(data)
    assert first is not second and first.nodeid == second.nodeid == report.nodeid
    threaded = []
    errors = []
    def copy_in_thread():
        try:
            issued = report._to_json()
            issued['duration'] = 1.25
            issued['sections'] = [*issued['sections'], ('Captured stdout call', 'injected output')]
            threaded.append(type(report)._from_json(issued))
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=copy_in_thread)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and not errors and len(threaded) == 1
    result = threaded[0]
    label = item.path.stem
    result.user_properties = [('edited', label), ('duplicate', 'one'), ('duplicate', 'two')]
    result.extra = 'actual edited extra'
    return result

def pytest_runtest_logreport(report):
    if report.when == 'call':
        calls.append({'nodeid':report.nodeid, 'duration':report.duration,
                      'properties':list(report.user_properties), 'stdout':report.capstdout,
                      'extra':report.extra})

def pytest_sessionfinish():
    Path('calls-' + str(os.getpid()) + '.json').write_text(json.dumps(calls))
"""

_COPY_BODY = """import json, os
from pathlib import Path

def test_body(record_property, record_xml_attribute):
    record_property('original', 'must be replaced')
    record_xml_attribute('owner', Path(__file__).stem)
    print('body output ' + Path(__file__).stem)
    Path(Path(__file__).stem + '.body').write_text(str(os.getpid()))
"""


def _xml_cases(path: Path) -> list[tuple[str, str, list[tuple[str, str]], str]]:
    return [
        (
            case.attrib["name"],
            case.attrib["owner"],
            [
                (row.attrib["name"], row.attrib["value"])
                for row in case.findall("./properties/property")
            ],
            case.attrib["time"],
        )
        for case in ET.parse(path).findall("./testsuite/testcase")
    ]


@pytest.mark.parametrize("workers", [1, 2])
def test_exact_report_replays_preserve_mutations_and_real_transports(
    tmp_path: Path, workers: int
) -> None:
    observations = []
    xml = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini",
            "[pytest]\njunit_family=legacy\njunit_logging=all\njunit_duration_report=call\n",
        )
        project.write("conftest.py", _COPY_HOOK)
        for name in ("a", "b"):
            project.write(f"test_{name}.py", _COPY_BODY)
        result = (
            project.run_pytest("-q", "--junitxml=result.xml", load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                f"--workers={workers}",
                "-q",
                "--junitxml=result.xml",
                "--native-result-json=record.json",
            )
        )
        result.expect(0)
        rows = [
            row
            for path in sorted(project.root.glob("calls-*.json"))
            for row in json.loads(path.read_text())
        ]
        rows.sort(key=lambda row: row["nodeid"])
        assert [row["nodeid"] for row in rows] == ["test_a.py::test_body", "test_b.py::test_body"]
        for row, label in zip(rows, ("test_a", "test_b"), strict=True):
            assert row["duration"] == 1.25
            assert row["properties"] == [
                ["edited", label],
                ["duplicate", "one"],
                ["duplicate", "two"],
            ]
            assert row["extra"] == "actual edited extra"
            assert row["stdout"] == f"body output {label}\ninjected output"
        observations.append(rows)
        xml.append(_xml_cases(project.root / "result.xml"))
        assert len(xml[-1]) == 2
        for case, label in zip(xml[-1], ("test_a", "test_b"), strict=True):
            assert case == (
                "test_body",
                label,
                [("edited", label), ("duplicate", "one"), ("duplicate", "two")],
                "1.250",
            )
        for node in ET.parse(project.root / "result.xml").findall("./testsuite/testcase"):
            captured = node.findtext("system-out", "")
            assert f"body output {node.attrib['owner']}\ninjected output" in captured
        if engine == "native":
            record = json.loads((project.root / "record.json").read_text())
            assert record["counts"]["passed"] == 2
            assert [case["nodeid"] for case in record["cases"]] == [row["nodeid"] for row in rows]
            topology = record["execution_topology"]
            assert topology["active_workers"] == topology["requested_workers"] == workers
            pids = topology["worker_pids"]
            assert len(pids) == len(set(pids)) == workers
            assert {int(path.read_text()) for path in project.root.glob("*.body")} == set(pids)
    assert observations[0] == observations[1]
    assert xml[0] == xml[1]


def test_failed_configuration_preserves_prior_result_and_payload_cleanup_allows_rerun(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "bridge.py",
        "import os\nfail = os.environ.get('CONFIGURE_FAIL') == '1'\nsource = None\n"
        "class Sentinel(Exception): pass\n"
        "sentinel = Sentinel('actual configure failure')\n",
    )
    project.write(
        "conftest.py",
        """import atexit, gc, json, weakref, pytest
from pathlib import Path
import bridge

def pytest_configure(config):
    if bridge.fail: raise bridge.sentinel

def pytest_internalerror(excrepr, excinfo):
    assert excinfo.value is bridge.sentinel
    Path('original-error-identity').touch()

class Payload:
    def __init__(self, source): self.source = source
    def __del__(self):
        self.source.user_properties = self.source.user_properties
        assert type(self.source._to_json()) is dict
        with Path('released.jsonl').open('a') as stream:
            stream.write('released\\n')

def pytest_runtest_logreport(report):
    if report.when == 'call':
        bridge.source = weakref.ref(report)
        data = report._to_json()
        data['cycle'] = Payload(report)

def collected():
    gc.collect()
    if bridge.source is not None:
        Path('source-collected').write_text(str(bridge.source() is None))

atexit.register(collected)
""",
    )
    project.write(
        "test_body.py", "from pathlib import Path\ndef test_body(): Path('body-ran').touch()\n"
    )
    project.write("record.json", "prior completed result")
    failed = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-q",
        "--native-result-json=record.json",
        env={"CONFIGURE_FAIL": "1"},
    )
    failed.expect(3)
    assert "actual configure failure" in failed.stdout + failed.stderr
    assert (project.root / "original-error-identity").is_file()
    assert not (project.root / "body-ran").exists()
    assert (project.root / "record.json").read_text() == "prior completed result"
    for run in (1, 2):
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "-q",
            "--native-result-json=record.json",
            env={"CONFIGURE_FAIL": "0"},
        )
        result.expect(0)
        released = (project.root / "released.jsonl").read_text().splitlines()
        assert released == ["released"] * run
        assert (project.root / "source-collected").read_text() == "True"
        assert json.loads((project.root / "record.json").read_text())["counts"]["passed"] == 1


def test_exact_makereport_replays_preserve_real_subtest_reports(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import json, pytest
from pathlib import Path
deliveries = []
copies = []
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    data = report._to_json()
    assert type(data) is dict
    copied = type(report)._from_json(data)
    copied.extra = 'actual replayed field'
    copies.append([copied.when, copied.outcome])
    return copied
def pytest_runtest_logreport(report):
    context = getattr(report, 'context', None)
    deliveries.append([report.when, report.outcome,
                       None if context is None else context.msg,
                       report.capstdout, report.extra])
def pytest_sessionfinish():
    Path('deliveries.json').write_text(json.dumps({'copies':copies, 'reports':deliveries}))
""",
        )
        project.write(
            "test_children.py",
            """def test_parent(subtests):
    with subtests.test('first'):
        print('first child bytes')
    with subtests.test('second'):
        print('second child bytes')
        assert False, 'actual child failure'
""",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "-q",
                "--native-result-json=record.json",
            )
        )
        result.expect(1)
        observed = json.loads((project.root / "deliveries.json").read_text())
        assert observed["copies"] == [
            ["setup", "passed"],
            ["call", "passed"],
            ["call", "failed"],
            ["call", "passed"],
            ["teardown", "passed"],
        ]
        assert [row[:3] for row in observed["reports"]] == [
            ["setup", "passed", None],
            ["call", "passed", "first"],
            ["call", "failed", "second"],
            ["call", "failed", None],
            ["teardown", "passed", None],
        ]
        assert observed["reports"][1][3] == "first child bytes\n"
        assert observed["reports"][2][3] == "second child bytes\n"
        assert all(row[4] == "actual replayed field" for row in observed["reports"])
        observations.append(observed)
        if engine == "native":
            record = json.loads((project.root / "record.json").read_text())
            assert len(record["cases"]) == 1
            assert [child["outcome"] for child in record["cases"][0]["subtests"]] == [
                "passed",
                "failed",
            ]
    assert observations[0] == observations[1]
