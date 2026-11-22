"""Ordinary parametrize spelling retains canonical values and direct production."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration.test_native_parameter_families import LIFETIME_SOURCE, TABLE_CALLBACK_SOURCE
from tests.support import VfyProject
from tests.support.native import run_parameter_suite

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or pytest.__version__ != "9.1.1",
    reason="direct parameter producer requires Unix and pytest 9.1.1",
)

_SOURCE = """\
import hashlib
import json
import os
from pathlib import Path

specs = {
    "test_scalar_list": ("value", [None, True, 12, 1.5, "é", b"x"]),
    "test_scalar_tuple": ("value", ("a", "a", "a0")),
    "test_tuple_scalar": ("value", [(1, 2), (3, 4)]),
    "test_list_scalar": ("value", [[1, 2], [3, 4]]),
    "test_comma_list": (" left , right ", [("é", b"\\xff"), ("é", b"\\xff"),
                                              ("é0", b"\\xff")]),
    "test_comma_tuple": ("left,right", ((1, True), (2, False))),
    "test_trailing_comma": (" value, ", [(1,), (2,)]),
    "test_list_rows": ("left,right", [[1, 2], [3, 4]]),
}

def pytest_generate_tests(metafunc):
    spec = specs.get(metafunc.function.__name__)
    if spec is None:
        return
    names, values = spec
    metafunc.parametrize(names, values)

def record(request):
    call = request.node.callspec
    assert type(call).__name__ == "CallSpec2"
    row = [request.node.nodeid, [[name, repr(value)] for name, value in call.params.items()],
           list(call.indices.items()), list(call._idlist),
           [[name, scope.value] for name, scope in call._arg2scope.items()],
           [mark.name for mark in call.marks]]
    suffix = hashlib.sha256(request.node.nodeid.encode()).hexdigest()
    Path("event-" + suffix + ".json").write_text(json.dumps(row))

def test_scalar_list(value, request):
    record(request)

def test_scalar_tuple(value, request):
    record(request)

def test_tuple_scalar(value, request):
    assert isinstance(value, tuple) and len(value) == 2
    record(request)

def test_list_scalar(value, request):
    assert isinstance(value, list) and len(value) == 2
    record(request)

def test_comma_list(left, right, request):
    record(request)

def test_comma_tuple(left, right, request):
    record(request)

def test_trailing_comma(value, request):
    assert type(value) is int
    record(request)

def test_list_rows(left, right, request):
    record(request)
"""


def test_parameter_spelling_preserves_rows_and_ids(tmp_path: Path) -> None:
    observations = {}
    for route in ("pytest", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_shapes.py", _SOURCE)
        environment = {
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
        result = run_parameter_suite(project, route, env=environment)
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        observations[route] = sorted(
            json.loads(path.read_text()) for path in project.root.glob("event-*.json")
        )
        assert len(observations[route]) == 22, output
    assert observations["native"] == observations["pytest"]


@pytest.mark.parametrize(
    ("source", "artifact", "restore_helper"),
    [
        (TABLE_CALLBACK_SOURCE, "callback.json", False),
        (LIFETIME_SOURCE, "lifetime.json", False),
        (LIFETIME_SOURCE, "lifetime.json", True),
    ],
    ids=["callback", "lifetime", "restored-helper-lifetime"],
)
def test_comma_parameters_preserve_id_callbacks_and_discarded_row_lifetimes(
    tmp_path: Path, source: str, artifact: str, restore_helper: bool
) -> None:
    outputs = []
    source = source.replace(
        'metafunc.parametrize(("left", "right"), rows)',
        'metafunc.parametrize(" left , right ", rows)',
    )
    source = source.replace("RESTORE_IDS = False", f"RESTORE_IDS = {restore_helper}")
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("test_callback.py", source)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        outputs.append((project.root / artifact).read_text())
    assert outputs[0] == outputs[1]
