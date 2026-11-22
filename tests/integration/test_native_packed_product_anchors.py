"""Packed stacked calls keep stock IDs, live projections, and public detachment."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="packed product anchors require Unix and pinned pytest 9.1.1",
)

_CASES = """\
import json
from pathlib import Path
import pytest

@pytest.mark.source_marker
@pytest.mark.parametrize('a', [0, 1], ids=['a-left', 'a-right'])
@pytest.mark.parametrize('b', [0, 1], ids=['b-left', 'b-right'])
@pytest.mark.parametrize('c', [0, 1], ids=['c-left', 'c-right'])
def test_product(a, b, c, request):
    spec = request.node.callspec
    assert request.node.keywords['proof_attribute'] == 'source-value'
    assert request.node.get_closest_marker('source_marker') is not None
    result = [
        request.node.nodeid, a, b, c, list(spec._idlist), spec.id,
        dict(spec.indices), {key: scope.value for key, scope in spec._arg2scope.items()},
        [mark.name for mark in spec.marks], request.node.user_properties,
    ]
    Path(f'case-{a}-{b}-{c}.json').write_text(json.dumps(result))
test_product.proof_attribute = 'source-value'
"""

_OBSERVER = """\
import gc
import json
import os
import weakref
from pathlib import Path
import pytest
from _pytest.scope import Scope

events = []

class Payload:
    def __del__(self):
        events.append(sibling.id)

def pytest_collection():
    if os.environ["PRODUCT_GC"] == "disabled":
        gc.disable()

def pytest_collection_modifyitems(items):
    assert len(items) == 8
    observed = []
    for ordinal, item in enumerate(items):
        spec = item.callspec
        reference = weakref.ref(spec)
        # Inject a real surrogate after the collector has captured ASCII node
        # IDs. The mutable ID-list and join contract remains observable here.
        if ordinal in (2, 3, 6, 7):
            spec._idlist[1] = '\\ud800'
        components = list(spec._idlist)
        assert len(components) == 3 and spec.id == '-'.join(components)
        assert item.callspec is spec and reference() is spec
        observed.append([item.nodeid, item.name, components, spec.id])
        item.user_properties.append(('observer', 'kept'))
    Path(f'collection-{os.getpid()}.json').write_text(json.dumps(observed))

    first = items[0].callspec
    global sibling
    sibling = items[1].callspec
    reference = weakref.ref(first)
    params, indices, scopes, ids = first.params, first.indices, first._arg2scope, first._idlist
    params['a'] = 100
    indices['a'] = 99
    # A wide-scoped parameter requires the canonical sorter for the inventory;
    # the body compares public live fields after that mapping promotion.
    scopes['a'] = Scope.Module
    ids.append('after-collection')
    first.marks.append(pytest.mark.projected.mark)
    params['released'] = Payload()
    del params['released']
    assert events == ['c-left-b-left-a-right'], events
    raw = vars(first)
    gc.collect()
    assert items[0].callspec is first and reference() is first
    assert raw is vars(first)
    assert first.params is params and first.indices is indices
    assert first._arg2scope is scopes and first._idlist is ids
    assert first.id == '-'.join(ids)
    Path(f'lifetime-{os.getpid()}.json').write_text(json.dumps(events))
    gc.enable()
"""


def _junit(project: VfyProject) -> list[tuple[object, ...]]:
    return [
        (
            case.attrib["classname"],
            case.attrib["name"],
            tuple((entry.attrib["name"], entry.attrib["value"]) for entry in case.iter("property")),
        )
        for case in ET.parse(project.root / "result.xml").iter("testcase")
    ]


@pytest.mark.parametrize("collection_gc", ["enabled", "disabled"])
def test_packed_products_preserve_components_live_fields_and_collection_lifetimes(
    tmp_path: Path, collection_gc: str
) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_products.py", _CASES)
        project.write("conftest.py", _OBSERVER)
        project.write(
            "pytest.ini",
            "[pytest]\nmarkers =\n    source_marker: source metadata\n"
            "    projected: mutated callspec metadata\n",
        )
        environment = {
            "VFY_WORKER_START": "exec",
            "PRODUCT_ROUTE": route,
            "PRODUCT_GC": collection_gc,
            "VFY_NATIVE_SCHEDULING": "cases",
        }
        result = (
            project.run_pytest("-q", "--junitxml=result.xml", load_xdist=False, env=environment)
            if route == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--native-scheduling=cases",
                "--no-replay",
                "--no-cov",
                "-q",
                "--junitxml=result.xml",
                env=environment,
            )
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert "8 passed" in output, output
        records = {}
        for prefix in ("collection", "lifetime"):
            values = [
                json.loads(path.read_text()) for path in project.root.glob(f"{prefix}-*.json")
            ]
            assert len(values) == (2 if route == "native" else 1), (output, values)
            assert all(value == values[0] for value in values), values
            records[prefix] = values[0]
        cases = sorted(json.loads(path.read_text()) for path in project.root.glob("case-*.json"))
        assert len(cases) == 8, (output, cases)
        observations[route] = records, cases, _junit(project)
    assert observations["native"] == observations["pytest"]
    rows = observations["pytest"][1]
    assert sorted((row[1], row[2], row[3]) for row in rows) == [
        (0, 0, 1),
        (0, 1, 0),
        (0, 1, 1),
        (1, 0, 0),
        (1, 0, 1),
        (1, 1, 0),
        (1, 1, 1),
        (100, 0, 0),
    ]
    collected = observations["pytest"][0]["collection"]
    assert [row[2] for row in collected] == [
        ["c-left", "b-left", "a-left"],
        ["c-left", "b-left", "a-right"],
        ["c-left", "\ud800", "a-left"],
        ["c-left", "\ud800", "a-right"],
        ["c-right", "b-left", "a-left"],
        ["c-right", "b-left", "a-right"],
        ["c-right", "\ud800", "a-left"],
        ["c-right", "\ud800", "a-right"],
    ]
    for ordinal, collection_row in enumerate(collected):
        row = next(row for row in rows if row[0] == collection_row[0])
        expected_indices = {"a": ordinal, "b": ordinal, "c": ordinal}
        if ordinal == 0:
            expected_indices["a"] = 99
        assert row[6] == expected_indices
