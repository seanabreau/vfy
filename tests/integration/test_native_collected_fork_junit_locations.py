"""Forked JUnit publication uses the locations retained by its workers."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import dedent

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="collected fork requires Linux")


@pytest.mark.parametrize("family", ["legacy", "xunit1", "xunit2"])
@pytest.mark.parametrize("scheduling", ["file", "demand"])
def test_fork_junit_publishes_worker_locations_without_rediscovering_controller_items(
    tmp_path: Path, family: str, scheduling: str
) -> None:
    cases_by_route: dict[str, list[str]] = {}
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            dedent("""\
                import os
                from pathlib import Path
                import pytest

                def pytest_collection_modifyitems(items):
                    for item in items:
                        domain = item.reportinfo()[2]
                        item.reportinfo = lambda domain=domain: ('virtual-source.py', 73, domain)

                def pytest_collection_finish(session):
                    if os.environ.get('FORK_LOCATION_GUARD') != '1':
                        return
                    role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')
                    Path('collected-' + role).touch()
                    original = pytest.Item.location
                    def guarded(item):
                        role = os.environ.get('VFY_NATIVE_PARALLEL_INDEX', 'controller')
                        if role == 'controller' and Path('worker-body.json').exists():
                            Path('controller-location-read').touch()
                            raise RuntimeError('completed fork controller rediscovered a location')
                        return original.__get__(item, type(item))
                    pytest.Item.location = property(guarded)
                """),
        )
        project.write(
            "test_locations.py",
            dedent("""\
                import json
                import os
                from pathlib import Path
                import pytest

                def test_pass(record_property):
                    record_property('proof', 'worker-property')
                    print('passing-location-output')
                    Path('worker-body.json').write_text(json.dumps({
                        'pid': os.getpid(),
                        'worker': os.environ.get('VFY_NATIVE_PARALLEL_INDEX'),
                    }))

                def test_skip():
                    pytest.skip('worker-location-skip')

                def test_fail():
                    pytest.fail('worker-location-error')

                @pytest.mark.xfail(reason='worker-location-xfail')
                def test_xfail():
                    assert False, 'worker-location-failure'
                """),
        )
        arguments = (
            "-q",
            "--capture=sys",
            "--junitxml=results.xml",
            "-o",
            f"junit_family={family}",
            "-o",
            "junit_logging=all",
            "-o",
            "junit_log_passing_tests=true",
        )
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-replay",
                "--no-cov",
                *arguments,
                env={
                    "FORK_LOCATION_GUARD": "1",
                    "VFY_WORKER_START": "fork",
                    "VFY_WORKER_COLLECTION": "0",
                    "VFY_NATIVE_SCHEDULING": scheduling,
                    "VFY_NATIVE_STARTUP_GRACE_MS": "0",
                    "PYTEST_NATIVE_RESULT": None,
                },
            )
        )
        assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
        cases = list(ET.parse(project.root / "results.xml").iter("testcase"))
        assert len(cases) == 4
        for case in cases:
            case.attrib.pop("time", None)
            if family in {"legacy", "xunit1"}:
                assert case.attrib["file"] == "virtual-source.py"
                assert case.attrib["line"] == "73"
        cases_by_route[route] = sorted(
            ET.tostring(case, encoding="unicode").replace(str(project.root), "<project>")
            for case in cases
        )
        if route == "native":
            assert sorted(path.name for path in project.root.glob("collected-*")) == [
                "collected-controller"
            ]
            body = json.loads((project.root / "worker-body.json").read_text())
            assert body["worker"] in {"0", "1"}
            assert not (project.root / "controller-location-read").exists()
    assert cases_by_route["native"] == cases_by_route["stock"]
