"""One Function identity owns generated definitions and executed request items."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_HOOK = """import os, pytest

def pytest_generate_tests(metafunc):
    definition = metafunc.definition
    assert isinstance(definition, pytest.Function)
    assert isinstance(definition, pytest.Item)
    assert definition.nodeid.endswith('::test_live')
    assert type(definition).__name__ == 'FunctionDefinition'
    assert type(definition).__bases__ == (pytest.Function,)
"""

_CASE = r"""import json, os, pytest
from pathlib import Path

@pytest.fixture
def plain(): return 31

@pytest.fixture
def subrequest(request): return request

def test_live(request, subrequest):
    item = request.node
    assert type(item) is pytest.Function
    assert isinstance(item, pytest.Item)
    assert item.nodeid.endswith('::test_live')
    assert item in item.session.items
    assert item.obj is item.function
    assert item.function.__name__ == item.originalname == 'test_live'
    assert item.path.name == Path(__file__).name
    original = pytest.Function.__repr__
    events = []
    def live(current):
        assert request.getfixturevalue('plain') == 31
        events.append(current is item)
        return '<Live ' + current.name + '\ud800>'
    try:
        pytest.Function.__repr__ = live
        representations = [repr(request), repr(subrequest)]
    finally:
        pytest.Function.__repr__ = original
    assert events == [True, True]
    assert representations == [
        '<FixtureRequest for <Live test_live\ud800>>',
        "<SubRequest 'subrequest' for <Live test_live\ud800>>",
    ]
    Path(Path(__file__).stem + '.json').write_text(json.dumps({
        'pid':os.getpid(), 'representations':representations, 'events':events,
    }))
"""


def test_function_identity_and_reentrant_request_repr_with_two_real_workers(
    tmp_path: Path,
) -> None:
    observed = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", _HOOK)
        for filename in ("test_a.py", "test_b.py"):
            project.write(filename, _CASE)
        result = (
            project.run_pytest("-q", load_xdist=False, env={"TEST_NATIVE": "0"})
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--workers=2",
                "--native-result-json=record.json",
                "-q",
                env={"TEST_NATIVE": "1", "VFY_WORKER_START": "exec"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        rows = [json.loads((project.root / f"test_{name}.json").read_text()) for name in ("a", "b")]
        observed.append([[row["representations"], row["events"]] for row in rows])
        if route == "independent":
            record = json.loads((project.root / "record.json").read_text())
            assert [case["nodeid"] for case in record["cases"]] == [
                "test_a.py::test_live",
                "test_b.py::test_live",
            ]
            topology = record["execution_topology"]
            assert topology["active_workers"] == topology["requested_workers"] == 2
            pids = topology["worker_pids"]
            assert len(pids) == len(set(pids)) == 2
            assert {row["pid"] for row in rows} == set(pids)
    assert observed[0] == observed[1]
