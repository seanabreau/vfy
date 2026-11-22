"""Prove scope-contiguous demand leases through complete public project runs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration.test_native_collected_fork import _case_records
from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")

_DEMAND_ENV = {
    "VFY_WORKER_START": "fork",
    "VFY_NATIVE_SCHEDULING": "demand",
    "VFY_WORKER_COLLECTION": "0",
}


def _run(project: VfyProject, *extra: str) -> CommandResult:
    return project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--history",
        "history.json",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "--junitxml=results.xml",
        "-o",
        "junit_family=xunit1",
        "-o",
        "junit_logging=all",
        "-q",
        *extra,
        env={**_DEMAND_ENV, "PYTEST_NATIVE_RESULT": str(project.root / "native.json")},
    )


def test_collected_demand_global_inventory_and_scope_epochs_match_pytest(tmp_path: Path) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("pytest", "native")}
    for project in projects.values():
        project.write(
            "conftest.py",
            """
import json, os
from pathlib import Path
import pytest

def pytest_collection_finish(session):
    collected = [item.nodeid for item in session.items]
    Path(f'collection.{os.getpid()}.json').write_text(json.dumps(collected))

@pytest.fixture(scope='class')
def class_resource(request):
    token = os.getpid()
    Path(f'class-setup.{token}').touch()
    request.addfinalizer(lambda: Path(f'class-finish.{token}').touch())
    return token

@pytest.fixture(scope='module')
def module_resource(request):
    token = os.getpid()
    Path(f'module-setup.{token}').touch()
    request.addfinalizer(lambda: Path(f'module-finish.{token}').touch())
    return token
""",
        )
        project.write(
            "tests/test_cases.py",
            """
import json, os, time, warnings
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', range(64))
def test_function(value, request):
    if os.environ.get('VFY_NATIVE_SCHEDULING') == 'demand' and value in (0, 16):
        Path(f'lease.{value}').write_text(str(os.getpid()))
        deadline = time.monotonic() + 5
        while not Path(f'lease.{16 if value == 0 else 0}').exists():
            assert time.monotonic() < deadline, 'first two demand leases must execute concurrently'
            time.sleep(.005)
    record = {'pid': os.getpid(), 'inventory': len(request.session.items)}
    Path(f'case.{value}.json').write_text(json.dumps(record))
    print(f'body {value}')
    time.sleep(.002)
    if value == 1:
        warnings.warn('demand warning', UserWarning)
    if value == 2:
        pytest.skip('demand skip')
    if value == 3:
        pytest.xfail('demand xfail')

class TestResource:
    @pytest.mark.parametrize('value', range(40))
    def test_class(self, value, class_resource):
        assert class_resource == os.getpid()
        Path(f'class-case.{value}').write_text(str(class_resource))
""",
        )
        project.write(
            "tests/test_wide.py",
            """
import os
from pathlib import Path
import pytest

def test_first(module_resource):
    Path('module-first').write_text(str(module_resource))

@pytest.mark.parametrize('value', range(38))
def test_unused_middle(value):
    Path(f'module-middle.{value}').write_text(str(os.getpid()))

def test_last(module_resource):
    Path('module-last').write_text(str(module_resource))
""",
        )
    stock = projects["pytest"].run_pytest(
        "-n",
        "2",
        "--dist=loadscope",
        "--capture=sys",
        "--junitxml=results.xml",
        "-o",
        "junit_family=xunit1",
        "-o",
        "junit_logging=all",
        "-q",
    )
    native = _run(projects["native"])
    for completed in (stock, native):
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "142 passed" in completed.stdout
        assert "1 skipped" in completed.stdout
        assert "1 xfailed" in completed.stdout
    assert _case_records(projects["native"].root / "results.xml") == _case_records(
        projects["pytest"].root / "results.xml"
    )
    for project in projects.values():
        assert len(list(project.root.glob("class-setup.*"))) == 1
        assert len(list(project.root.glob("class-finish.*"))) == 1
        assert len(list(project.root.glob("module-setup.*"))) == 1
        assert len(list(project.root.glob("module-finish.*"))) == 1
        assert (project.root / "module-first").read_text() == (
            project.root / "module-last"
        ).read_text()
        assert len({(project.root / f"class-case.{value}").read_text() for value in range(40)}) == 1
        assert (
            len({(project.root / f"module-middle.{value}").read_text() for value in range(38)}) == 1
        )
    root = projects["native"].root
    assert (root / "lease.0").read_text() != (root / "lease.16").read_text()
    observations = [json.loads((root / f"case.{value}.json").read_text()) for value in range(64)]
    assert {entry["inventory"] for entry in observations} == {144}
    assert len({entry["pid"] for entry in observations}) == 2
    collection_paths = list(root.glob("collection.*.json"))
    assert len(collection_paths) == 1
    expected = json.loads(collection_paths[0].read_text())
    result = json.loads((root / "native.json").read_text())
    assert result["collected"] == 144
    assert [case["nodeid"] for case in result["cases"]] == expected
    assert len(set(expected)) == 144
    assert (root / "history.json").is_file()


def test_collected_demand_maxfail_closes_retained_requests(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """
import os
from pathlib import Path
import pytest

@pytest.fixture(scope='session')
def retained(request):
    token = os.getpid()
    Path(f'setup.{token}').touch()
    request.addfinalizer(lambda: Path(f'finished.{token}').touch())
    return request
""",
    )
    project.write(
        "tests/test_cases.py",
        """
import os, time
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', range(96))
def test_case(value, retained):
    Path(f'ran.{value}').write_text(str(os.getpid()))
    assert retained.node is retained.session
    if value in (0, 16):
        Path(f'first.{value}').touch()
        deadline = time.monotonic() + 5
        while not Path(f'first.{16 if value == 0 else 0}').exists():
            assert time.monotonic() < deadline
            time.sleep(.005)
        pytest.fail('demand stop')
""",
    )
    completed = _run(project, "--maxfail=1")
    completed.expect(1)
    assert "demand stop" in completed.stdout + completed.stderr
    started = {path.suffix for path in project.root.glob("setup.*")}
    finished = {path.suffix for path in project.root.glob("finished.*")}
    assert started == finished
    assert len(started) == 2
    result = json.loads((project.root / "native.json").read_text())
    assert result["collected"] == 96
    assert 1 <= result["counts"]["failed"] <= 2
    assert len(result["cases"]) < 96
    assert len({case["nodeid"] for case in result["cases"]}) == len(result["cases"])
    assert not (project.root / "ran.32").exists()
    assert (project.root / "history.json").is_file()


def test_collected_demand_rejects_late_unsafe_item_before_bodies(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        """
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', range(96))
def test_case(value):
    Path(f'ran.{value}').touch()

async def test_unsupported():
    pass
""",
    )
    completed = _run(project)
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert "plain synchronous" in completed.stdout + completed.stderr
    assert not list(project.root.glob("ran.*"))


def test_collected_demand_function_only_public_smoke(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_cases.py",
        """
import json, os, time
from pathlib import Path
import pytest

@pytest.mark.parametrize('value', range(64))
def test_case(value, request):
    if value in (0, 16):
        Path(f'lease.{value}').write_text(str(os.getpid()))
        deadline = time.monotonic() + 5
        while not Path(f'lease.{16 if value == 0 else 0}').exists():
            assert time.monotonic() < deadline, 'demand leases did not dispatch concurrently'
            time.sleep(.005)
    record = {'pid': os.getpid(), 'inventory': len(request.session.items)}
    Path(f'ran.{value}.json').write_text(json.dumps(record))
    time.sleep(.002)
""",
    )
    completed = _run(project)
    completed.expect(0)
    assert "64 passed" in completed.stdout
    assert (tmp_path / "lease.0").read_text() != (tmp_path / "lease.16").read_text()
    observations = [json.loads((tmp_path / f"ran.{value}.json").read_text()) for value in range(64)]
    assert {entry["inventory"] for entry in observations} == {64}
    assert len({entry["pid"] for entry in observations}) == 2
    result = json.loads((tmp_path / "native.json").read_text())
    assert result["collected"] == 64
    assert result["counts"]["passed"] == 64
    assert [case["nodeid"] for case in result["cases"]] == [
        f"tests/test_cases.py::test_case[{value}]" for value in range(64)
    ]
    assert len(_case_records(tmp_path / "results.xml")) == 64
    assert (tmp_path / "history.json").is_file()


@pytest.mark.parametrize(("workers", "start"), [("1", "fork"), ("2", "exec")])
def test_collected_demand_rejects_invalid_launch_mode(
    tmp_path: Path, workers: str, start: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "tests/test_case.py",
        "from pathlib import Path\ndef test_case():\n    Path('body-ran').touch()\n",
    )
    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        f"--workers={workers}",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "-q",
        env={**_DEMAND_ENV, "VFY_WORKER_START": start},
    )
    completed.expect(4)
    assert "demand scheduling requires collected fork" in completed.stdout + completed.stderr
    assert not (tmp_path / "body-ran").exists()


def test_collected_demand_rechecks_cleanup_added_by_compilation_getter(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """
import inspect, os
from pathlib import Path
import pytest

def pytest_collection_finish(session):
    original = inspect.getattr_static(pytest.Function, 'obj')
    target = session.items[0]
    reads = 0
    def observed_obj(item):
        nonlocal reads
        if item is target:
            reads += 1
            if reads == 3:
                pid = os.getpid()
                Path('cleanup-registered').write_text(str(pid))
                session.config.add_cleanup(lambda: Path(f'cleanup-ran.{pid}').touch())
        return original.__get__(item, type(item))
    pytest.Function.obj = property(observed_obj)
""",
    )
    project.write(
        "tests/test_cases.py",
        "from pathlib import Path\nimport pytest\n"
        "@pytest.mark.parametrize('value', range(32))\n"
        "def test_case(value):\n    Path(f'body-ran.{value}').touch()\n",
    )
    completed = _run(project)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 4, output
    assert "unreviewed config cleanup callback" in output, output
    parent_pid = (tmp_path / "cleanup-registered").read_text()
    assert [path.name for path in tmp_path.glob("cleanup-ran.*")] == [f"cleanup-ran.{parent_pid}"]
    assert not list(tmp_path.glob("body-ran.*")), output
