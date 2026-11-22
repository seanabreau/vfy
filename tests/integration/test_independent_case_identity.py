"""Real collected Items with equal display IDs retain separate native origins."""

from __future__ import annotations

import json
import os
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_OBSERVER = """import json
import os
from pathlib import Path
import pytest

def pytest_sessionstart(session):
    pass

def pytest_collection_modifyitems(items):
    sentinel=[item for item in items if item.name=='test_sentinel']
    renamed=[item for item in items if item.name!='test_sentinel']
    assert len(sentinel)==1 and len(renamed)==2
    assert renamed[0] is not renamed[1]
    for item in renamed:
        try:
            item.nodeid='shared.py::test_same'
        except AttributeError:
            item._nodeid='shared.py::test_same'
    items[:]=sentinel+list(reversed(renamed))

@pytest.fixture(autouse=True)
def actual_resource(request):
    name=request.node.name
    path=Path('lifetime-'+name)
    assert not path.exists()
    path.write_text('setup')
    yield
    assert path.read_text()=='setup'
    path.write_text('teardown')
"""


def _project(tmp_path: Path, route: str) -> VfyProject:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _OBSERVER)
    return project


@pytest.mark.parametrize("route", ["stock", "serial", "workers"])
@pytest.mark.parametrize("failure", [False, True])
def test_actual_duplicate_display_ids_preserve_source_order_properties_and_outcomes(
    tmp_path: Path, route: str, failure: bool
) -> None:
    project = _project(tmp_path, route)
    project.write(
        "test_source.py",
        """import json
import os
from pathlib import Path
count=0
def observe(record_property,request):
    global count
    count+=1
    record_property('source',request.node.name)
    record_property('count',count)
    Path('body-'+request.node.name).write_text(json.dumps({'pid':os.getpid(),'value':count}))
def test_alpha(record_property,request):
    observe(record_property,request)
def test_beta(record_property,request):
    observe(record_property,request)
    assert FAILURE is False, 'real beta failure'
""".replace("FAILURE", repr(failure)),
    )
    project.write(
        "test_sentinel.py",
        """import json
import os
from pathlib import Path
def test_sentinel(record_property):
    record_property('source','test_sentinel')
    Path('body-test_sentinel').write_text(json.dumps({'pid':os.getpid(),'value':0}))
""",
    )
    arguments = ["-q", "--junitxml=results.xml", "-o", "junit_family=legacy"]
    if route == "stock":
        result = project.run_python("-m", "pytest", *arguments)
    else:
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "--no-replay",
            "--native-scheduling=file",
            *arguments,
            f"--workers={2 if route == 'workers' else 1}",
            "--native-result-json=record.json",
        )
    project.write("run.stdout", result.stdout)
    project.write("run.stderr", result.stderr)
    project.write("run.status", str(result.returncode))
    assert result.returncode == int(failure), result
    cases = ET.parse(tmp_path / "results.xml").findall(".//testcase")
    assert [[prop.attrib for prop in case.findall("./properties/property")] for case in cases] == [
        [{"name": "source", "value": "test_sentinel"}],
        [{"name": "source", "value": "test_beta"}, {"name": "count", "value": "1"}],
        [{"name": "source", "value": "test_alpha"}, {"name": "count", "value": "2"}],
    ]
    assert [len(case.findall("failure")) for case in cases] == [0, int(failure), 0]
    bodies = {
        name: json.loads((tmp_path / ("body-" + name)).read_text())
        for name in ("test_sentinel", "test_beta", "test_alpha")
    }
    assert bodies["test_beta"]["pid"] == bodies["test_alpha"]["pid"]
    assert [bodies[name]["value"] for name in bodies] == [0, 1, 2]
    assert [(tmp_path / ("lifetime-" + name)).read_text() for name in bodies] == ["teardown"] * 3
    if route != "stock":
        record = json.loads((tmp_path / "record.json").read_text())
        assert [case["test_index"] for case in record["cases"]] == [0, 1, 2]
        assert [case["nodeid"] for case in record["cases"]] == [
            "test_sentinel.py::test_sentinel",
            "shared.py::test_same",
            "shared.py::test_same",
        ]
        assert [
            next(phase["outcome"] for phase in case["phases"] if phase["phase"] == "call")
            for case in record["cases"]
        ] == ["passed", "failed" if failure else "passed", "passed"]
        if route == "workers":
            pids = {body["pid"] for body in bodies.values()}
            assert len(pids) == 2
            assert os.getpid() not in pids
            assert set(record["execution_topology"]["worker_pids"]) == pids
            assert record["execution_topology"]["active_workers"] == 2
