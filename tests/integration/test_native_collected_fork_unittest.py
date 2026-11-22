"""Prove stock unittest items execute from collected worker snapshots."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.integration.test_native_collected_fork import _case_records
from tests.integration.test_native_unittest import _normalized_summary, _unittest_project_source
from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


def _run(project: VfyProject, *, fork: bool, scheduling: str = "file") -> CommandResult:
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
        "--tb=no",
        env={
            "VFY_WORKER_START": "fork" if fork else "exec",
            "VFY_NATIVE_SCHEDULING": scheduling,
            "VFY_WORKER_COLLECTION": "0",
            "VFY_NATIVE_STARTUP_GRACE_MS": None,
            "PYTEST_NATIVE_RESULT": str(project.root / "native.json"),
        },
    )


def test_collected_fork_unittest_lifecycle_matches_stock_and_fresh_workers(tmp_path: Path) -> None:
    projects = {route: VfyProject(tmp_path / route) for route in ("stock", "fresh", "fork")}
    for project in projects.values():
        for name in ("alpha", "beta"):
            source = "import os\n" + _unittest_project_source()
            source = source.replace("unittest-events.json", f"{name}-events.json")
            source = source.replace(
                "class TestLifecycle(TestCase):\n",
                "class TestLifecycle(TestCase):\n"
                "    def __init__(self, methodName='runTest'):\n"
                "        super().__init__(methodName)\n"
                "        self.birth_pid = os.getpid()\n"
                "    @pytest.fixture(autouse=True)\n"
                "    def bound_resource(self, request):\n"
                "        assert request.instance is self\n"
                "        self.bound_pid = os.getpid()\n"
                "        yield\n"
                "        assert self.bound_pid == os.getpid()\n",
            )
            source = source.replace(
                "        assert self._vfy_instance_token\n",
                "        assert self._vfy_instance_token\n"
                f"        Path('{name}-instance.json').write_text(json.dumps({{\n"
                "            'birth_pid': self.birth_pid, 'pid': os.getpid(),\n"
                "        }))\n",
            )
            project.write(f"test_{name}.py", source)
    stock = projects["stock"].run_pytest(
        "-n",
        "2",
        "--dist=loadfile",
        "--capture=sys",
        "--junitxml=results.xml",
        "-o",
        "junit_family=xunit1",
        "-o",
        "junit_logging=all",
        "-q",
        "--tb=no",
    )
    results = {"stock": stock, "fresh": _run(projects["fresh"], fork=False)}
    results["fork"] = _run(projects["fork"], fork=True)
    for route, result in results.items():
        assert result.returncode == 1, result.stdout + result.stderr
        assert _normalized_summary(result.stdout) == _normalized_summary(stock.stdout)
        assert _case_records(projects[route].root / "results.xml") == _case_records(
            projects["stock"].root / "results.xml"
        )
        for name in ("alpha", "beta"):
            assert json.loads((projects[route].root / f"{name}-events.json").read_text()) == (
                json.loads((projects["stock"].root / f"{name}-events.json").read_text())
            )
    observations = {
        route: [
            json.loads((project.root / f"{name}-instance.json").read_text())
            for name in ("alpha", "beta")
        ]
        for route, project in projects.items()
    }
    assert len({record["pid"] for record in observations["fork"]}) == 2
    assert len({record["birth_pid"] for record in observations["fork"]}) == 1
    assert all(record["birth_pid"] != record["pid"] for record in observations["fork"])
    assert all(record["birth_pid"] == record["pid"] for record in observations["fresh"])
    for route in ("fresh", "fork"):
        assert (projects[route].root / "history.json").is_file()
        result = json.loads((projects[route].root / "native.json").read_text())
        assert result["collected"] == 16
        assert len({case["nodeid"] for case in result["cases"]}) == 16


def test_collected_fork_unittest_subtests_maxfail_preserves_cleanup_and_reports(
    tmp_path: Path,
) -> None:
    projects = {route: VfyProject(tmp_path / route) for route in ("stock", "fork")}
    source = """
from pathlib import Path
from unittest import TestCase
import pytest

@pytest.fixture(autouse=True)
def resource(request):
    assert isinstance(request.instance, TestCase)
    request.addfinalizer(lambda: Path('fixture-finalized').touch())

class TestCases(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addClassCleanup(lambda: Path('class-finalized').touch())

    def setUp(self):
        self.addCleanup(lambda: Path('instance-finalized').touch())

    def test_00_subtests(self):
        Path('first-body').touch()
        for value in (1, 2):
            with self.subTest(value=value):
                self.fail('subtest failure sentinel')
        with self.subTest(value=3):
            self.skipTest('subtest skip sentinel')

class TestLater(TestCase):
    pass

def later(self):
    Path('later-body').touch()

for value in range(1, 20):
    setattr(TestCases, f'test_{value:02}', later)
for value in range(20):
    setattr(TestLater, f'test_{value:02}', later)
"""
    for project in projects.values():
        project.write("test_cases.py", source)
    stock = projects["stock"].run_pytest(
        "--maxfail=1", "--capture=sys", "--junitxml=results.xml", "-o", "junit_family=xunit1", "-q"
    )
    fork = projects["fork"].run_vfy(
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
        "--maxfail=1",
        "-q",
        env={
            "VFY_WORKER_START": "fork",
            "VFY_NATIVE_SCHEDULING": "demand",
            "VFY_WORKER_COLLECTION": "0",
            "VFY_NATIVE_STARTUP_GRACE_MS": "5000",
            "PYTEST_NATIVE_RESULT": str(projects["fork"].root / "native.json"),
        },
    )
    for route, completed in (("stock", stock), ("fork", fork)):
        assert completed.returncode == 1, completed.stdout + completed.stderr
        root = projects[route].root
        assert (root / "first-body").exists()
        assert not (root / "later-body").exists()
        assert all(
            (root / f"{scope}-finalized").exists() for scope in ("fixture", "instance", "class")
        )
    assert _case_records(projects["fork"].root / "results.xml") == _case_records(
        projects["stock"].root / "results.xml"
    )
    result = json.loads((projects["fork"].root / "native.json").read_text())
    assert result["collected"] == 40
    assert len(result["cases"]) == 1
    assert len(result["cases"][0]["subtests"]) == 3
    assert (projects["fork"].root / "history.json").is_file()


@pytest.mark.parametrize("custom_item", [False, True], ids=["isolated-asyncio", "custom-item"])
def test_collected_fork_rejects_async_or_custom_unittest_items_before_bodies(
    tmp_path: Path, custom_item: bool
) -> None:
    project = VfyProject(tmp_path)
    if custom_item:
        project.write(
            "conftest.py",
            "from _pytest.unittest import TestCaseFunction\n"
            "class CustomCaseFunction(TestCaseFunction): pass\n"
            "def pytest_collection_modifyitems(items):\n"
            "    for item in items: item.__class__ = CustomCaseFunction\n",
        )
    project.write(
        "test_cases.py",
        "from pathlib import Path\nfrom unittest import "
        + (
            "TestCase\nclass TestCases(TestCase):\n    def test_case(self):\n"
            if custom_item
            else (
                "IsolatedAsyncioTestCase\nclass TestCases(IsolatedAsyncioTestCase):\n"
                "    async def test_case(self):\n"
            )
        )
        + "        Path('body').touch()\n",
    )
    completed = _run(project, fork=True)
    completed.expect(4)
    assert "stock synchronous TestCaseFunction" in completed.stdout + completed.stderr
    assert not (tmp_path / "body").exists()
