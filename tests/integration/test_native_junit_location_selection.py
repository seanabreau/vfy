"""Public JUnit locations and captures with controller-only unused getters."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")

_OBSERVER = """\
import json
import os
from pathlib import Path
import pytest

def pytest_collection_modifyitems(items):
    for item in items:
        domain = item.reportinfo()[2]
        item.reportinfo = lambda domain=domain: ('virtual-source.py', 73, domain)

def pytest_collection_finish(session):
    if os.environ['JUNIT_LOCATION_ROUTE'] == 'native':
        role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')
        Path('collection-' + role + '.json').write_text(
            json.dumps([item.nodeid for item in session.items]))

@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session):
    yield
    if (os.environ['JUNIT_LOCATION_ROUTE'] == 'native'
            and 'VFY_NATIVE_PARALLEL_INDEX' not in os.environ):
        Path('controller-finish.json').write_text(json.dumps({
            'items': [item.nodeid for item in session.items],
            'collected': session.testscollected,
        }))
        if session.config.getini('junit_family') != 'xunit2':
            return
        def unused_location():
            raise RuntimeError('unused controller location was evaluated')
        for item in session.items:
            vars(item).pop('location', None)
            item.reportinfo = unused_location
        Path('location-guard.json').write_text(json.dumps([item.nodeid for item in session.items]))
"""

_CASES = """\
import logging
import sys
import pytest

def emit(token):
    print(token + '-out')
    print(token + '-err', file=sys.stderr)
    logging.warning(token + '-log')

def test_pass(record_property):
    emit('passing')
    record_property('owner', 'location-proof')

def test_skip():
    emit('skipping')
    pytest.skip('skip location retained')

@pytest.mark.xfail(reason='expected location proof')
def test_xfail():
    emit('xfailing')
    assert False
"""


def _case_shapes(path: Path, root: Path) -> list[tuple[dict[str, str], str]]:
    shapes = []
    for case in ET.parse(path).iter("testcase"):
        attributes = {key: value for key, value in case.attrib.items() if key != "time"}
        children = "".join(ET.tostring(child, encoding="unicode") for child in case)
        shapes.append((attributes, children.replace(str(root), "<ROOT>")))
    return sorted(shapes, key=lambda shape: (shape[0]["classname"], shape[0]["name"]))


@pytest.mark.parametrize("family", ["legacy", "xunit1", "xunit2"])
def test_native_junit_resolves_only_required_controller_locations(
    tmp_path: Path, family: str
) -> None:
    observed = {}
    inventory = [
        "test_locations.py::test_pass",
        "test_locations.py::test_skip",
        "test_locations.py::test_xfail",
    ]
    for route in ("pytest", "native_controller", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _OBSERVER)
        project.write("test_locations.py", _CASES)
        options = (
            "-q",
            "--junitxml=result.xml",
            "-o",
            f"junit_family={family}",
            "-o",
            "junit_logging=all",
        )
        environment = {
            "VFY_WORKER_START": "exec",
            "JUNIT_LOCATION_ROUTE": "pytest" if route == "pytest" else "native",
            "VFY_WORKER_COLLECTION": "0" if route == "native_controller" else None,
        }
        if route == "pytest":
            result = project.run_pytest(*options, load_xdist=False, env=environment)
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                *options,
                env=environment,
            )
        result.expect(0)
        assert "unused controller location was evaluated" not in result.stdout + result.stderr
        observed[route] = _case_shapes(project.root / "result.xml", project.root)
        if route != "pytest":
            controller_items = inventory if route == "native_controller" else []
            assert json.loads((project.root / "controller-finish.json").read_text()) == {
                "items": controller_items,
                "collected": 3,
            }
            controller_collection = project.root / "collection-controller.json"
            assert controller_collection.exists() is (route == "native_controller")
            if controller_collection.exists():
                assert json.loads(controller_collection.read_text()) == inventory
            worker_collections = list(project.root.glob("collection-[0-9]*.json"))
            assert worker_collections
            assert all(json.loads(path.read_text()) == inventory for path in worker_collections)
            if family == "xunit2":
                guarded = json.loads((project.root / "location-guard.json").read_text())
                assert guarded == controller_items
    assert observed["native"] == observed["native_controller"] == observed["pytest"]
    for attributes, _children in observed["native"]:
        if family in {"legacy", "xunit1"}:
            assert attributes["file"] == "virtual-source.py"
            assert attributes["line"] == "73"
        else:
            assert "file" not in attributes
            assert "line" not in attributes
    assert any("skip location retained" in children for _attrs, children in observed["native"])
    assert any("location-proof" in children for _attrs, children in observed["native"])
