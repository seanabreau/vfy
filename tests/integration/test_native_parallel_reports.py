"""Native worker reports retain metadata and commit only validated coverage."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(os.name != "posix", reason="native test workers require POSIX")


def _observer_publication_audit() -> str:
    return (
        "import json\nimport os\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def worker_finalizer():\n"
        "    yield\n"
        "    worker = os.getenv('VFY_NATIVE_PARALLEL_INDEX', 'serial')\n"
        "    Path(f'finalized-{worker}').touch()\n"
        "@pytest.hookimpl(specname='pytest_sessionfinish', wrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish_observer_audit(session):\n"
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        "        destination = os.getenv('VFY_TEST_RESULTS')\n"
        "        worker = os.getenv('VFY_NATIVE_PARALLEL_INDEX')\n"
        "        if worker is not None:\n"
        "            directory = Path(os.environ['VFY_NATIVE_PARALLEL_DIR'])\n"
        "            Path(f'publication-{worker}.json').write_text(json.dumps({\n"
        "                'private_observer_exists': Path(destination).exists(),\n"
        "                'transport_exists': (directory / f'result.{worker}.json').is_file(),\n"
        "            }))\n"
        "        elif destination and Path(destination).is_file():\n"
        "            Path('controller-observer.json').write_bytes(Path(destination).read_bytes())\n"
    )


def test_parallel_controller_observer_retains_all_outcomes_and_explicit_destinations(
    tmp_path: Path,
) -> None:
    projects = {name: VfyProject(tmp_path / name) for name in ("reference", "native")}
    for project in projects.values():
        project.write("conftest.py", _observer_publication_audit())
        project.write(
            "tests/test_a.py",
            "import pytest\n"
            "def test_pass():\n    pass\n"
            "def test_fail():\n    assert False, 'call failure sentinel'\n"
            "def test_skip():\n    pytest.skip('skip sentinel')\n"
            "@pytest.mark.xfail(reason='expected failure', strict=False)\n"
            "def test_xfail():\n    assert False\n"
            "@pytest.mark.xfail(reason='unexpected pass', strict=False)\n"
            "def test_xpass():\n    pass\n",
        )
        project.write(
            "tests/test_b.py",
            "import pytest\n"
            "@pytest.fixture\n"
            "def broken_setup():\n    raise RuntimeError('setup failure sentinel')\n"
            "@pytest.fixture\n"
            "def broken_teardown():\n"
            "    yield\n    raise RuntimeError('teardown failure sentinel')\n"
            "def test_setup(broken_setup):\n    pass\n"
            "def test_teardown(broken_teardown):\n    pass\n",
        )
    reference = projects["reference"].run_pytest(
        "-q",
        env={"VFY_TEST_RESULTS": str(projects["reference"].root / "reference.json")},
    )
    native = projects["native"].run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-replay",
        "--history=history.json",
        "--no-cov",
        "-q",
    )
    assert reference.returncode == pytest.ExitCode.TESTS_FAILED, reference.stdout + reference.stderr
    assert native.returncode == reference.returncode, native.stdout + native.stderr

    expected = {
        "tests/test_a.py::test_pass": ("passed", None),
        "tests/test_a.py::test_fail": ("failed", "call"),
        "tests/test_a.py::test_skip": ("skipped", None),
        "tests/test_a.py::test_xfail": ("skipped", None),
        "tests/test_a.py::test_xpass": ("passed", None),
        "tests/test_b.py::test_setup": ("failed", "setup"),
        "tests/test_b.py::test_teardown": ("failed", "teardown"),
    }
    for project in projects.values():
        observer = json.loads((project.root / "controller-observer.json").read_text())
        assert observer["infrastructure_failed"] is False
        assert {
            test["nodeid"]: (test["outcome"], test.get("failed_phase"))
            for test in observer["tests"]
        } == expected
    history = json.loads((projects["native"].root / "history.json").read_text())
    assert {
        nodeid: (
            entry["observations"][-1]["outcome"],
            entry["observations"][-1].get("failed_phase"),
        )
        for nodeid, entry in history["tests"].items()
    } == expected
    for worker in ("0", "1"):
        root = projects["native"].root
        assert (root / f"finalized-{worker}").is_file()
        assert json.loads((root / f"publication-{worker}.json").read_text()) == {
            "private_observer_exists": False,
            "transport_exists": True,
        }


def test_parallel_worker_result_round_trip_preserves_large_escaped_capture(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    capture = 'quotes " \\ unicode λ\n' * 1024
    for name in ("a", "b"):
        project.write(
            f"tests/test_{name}.py",
            f"def test_{name}():\n    print({capture!r}, end='')\n",
        )
    options = (
        "-o",
        "junit_logging=all",
        "-o",
        "junit_log_passing_tests=true",
        "-q",
    )
    reference = project.run_pytest("--junitxml=reference.xml", *options, load_xdist=False)
    reference.expect(0)
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--workers=2",
        "--no-replay",
        "--no-cov",
        "--html=results.html",
        "--junitxml=results.xml",
        f"--native-result-json={tmp_path / 'execution.json'}",
        *options,
    )
    assert result.returncode == reference.returncode, result.stdout + result.stderr
    reference_cases = ET.parse(tmp_path / "reference.xml").findall(".//testcase")
    actual_cases = ET.parse(tmp_path / "results.xml").findall(".//testcase")
    assert [case.attrib["name"] for case in actual_cases] == [
        case.attrib["name"] for case in reference_cases
    ]
    for actual, expected in zip(actual_cases, reference_cases, strict=True):
        assert capture in "".join(expected.itertext())
        assert capture in "".join(actual.itertext())
    execution = json.loads((tmp_path / "execution.json").read_text(encoding="utf-8"))
    assert [
        (case["nodeid"], case["phases"][1]["outcome"], case["phases"][1]["stdout"])
        for case in execution["cases"]
    ] == [(f"tests/test_{name}.py::test_{name}", "passed", capture) for name in ("a", "b")]


@pytest.mark.parametrize("worker_failure", ["invalid-result", "intentional-exit"])
def test_invalid_parallel_worker_result_preserves_history_and_execution(
    tmp_path: Path, worker_failure: str
) -> None:
    # Damage the installed-pytest worker result transport at its publication boundary.
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        _observer_publication_audit() + "\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    yield\n"
        "    if os.getenv('VFY_DAMAGE_RESULT') == 'invalid-result' and "
        "os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '1':\n"
        "        result = Path(os.environ['VFY_NATIVE_PARALLEL_DIR']) / 'result.1.json'\n"
        "        assert result.is_file(), 'worker result publication was not reached'\n"
        "        result.write_text('{invalid worker result')\n"
        "        Path('damaged-result').touch()\n",
    )
    for name in ("a", "b"):
        project.write(
            f"tests/test_{name}.py",
            "import os\nfrom pathlib import Path\nimport pytest\n"
            "def test_value():\n"
            "    if (os.getenv('VFY_DAMAGE_RESULT') == 'intentional-exit'\n"
            "            and os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '1'):\n"
            "        Path('intentional-exit').touch()\n"
            "        pytest.exit('intentional worker exit sentinel', returncode=7)\n",
        )
    arguments = ("--no-replay", "--history=history.json", "--no-cov", "-q")
    environment = {"PYTEST_NATIVE_RESULT": str(tmp_path / "execution.json")}
    baseline = project.run_vfy(
        "test", "--engine=pytest-native", "--workers=1", *arguments, env=environment
    )
    baseline.expect(0)
    preserved = {
        name: (tmp_path / name).read_bytes()
        for name in ("history.json", "execution.json", "controller-observer.json")
    }

    rejected = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        *arguments,
        env={**environment, "VFY_DAMAGE_RESULT": worker_failure},
    )

    output = rejected.stdout + rejected.stderr
    if worker_failure == "invalid-result":
        assert rejected.returncode == pytest.ExitCode.INTERNAL_ERROR, output
        assert (tmp_path / "damaged-result").exists(), output
        assert "invalid native parallel worker result at index 1" in output
    else:
        assert rejected.returncode == 7, output
        assert (tmp_path / "intentional-exit").exists(), output
        assert "intentional worker exit sentinel" in output
    assert {name: (tmp_path / name).read_bytes() for name in preserved} == preserved
    workers = ("0", "1") if worker_failure == "invalid-result" else ("1",)
    for worker in workers:
        assert (tmp_path / f"finalized-{worker}").is_file()
        assert json.loads((tmp_path / f"publication-{worker}.json").read_text()) == {
            "private_observer_exists": False,
            "transport_exists": worker_failure == "invalid-result",
        }


def test_parallel_maxfail_reports_only_executed_cases(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cases.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "@pytest.fixture(scope='session', autouse=True)\n"
        "def cleanup():\n"
        "    yield\n"
        "    Path('finalized').write_text('session finalized')\n"
        "@pytest.mark.parametrize('number', [0, 1])\n"
        "def test_case(number):\n"
        "    assert number < 0\n",
    )
    reference = project.run_pytest(
        "--maxfail=1",
        "--junitxml=reference.xml",
        "-q",
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert reference.returncode == pytest.ExitCode.TESTS_FAILED, reference.stdout + reference.stderr
    reference_cases = [
        (case.attrib["name"], case.find("failure") is not None)
        for case in ET.parse(tmp_path / "reference.xml").findall(".//testcase")
    ]
    assert reference_cases == [("test_case[0]", True)]
    finalized = tmp_path / "finalized"
    assert finalized.read_text(encoding="utf-8") == "session finalized"
    finalized.unlink()

    result = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--workers=2",
        # One worker owns both cases, so the shared limit stops the second deterministically.
        "--native-scheduling=file",
        "--no-cov",
        "--html=results.html",
        "--junitxml=results.xml",
        "--maxfail=1",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert result.returncode == reference.returncode, result.stdout + result.stderr
    assert "1 failed" in result.stdout
    html = (tmp_path / "results.html").read_text(encoding="utf-8")
    assert "test_cases.py::test_case[0]" in html
    assert "test_cases.py::test_case[1]" not in html
    assert "1 Failed" in html
    assert [
        (case.attrib["name"], case.find("failure") is not None)
        for case in ET.parse(tmp_path / "results.xml").findall(".//testcase")
    ] == reference_cases
    assert finalized.read_text(encoding="utf-8") == "session finalized"


@pytest.mark.parametrize("junit_source", ["argument", "configuration", "environment"])
def test_parallel_junit_preserves_worker_properties_capture_and_collection_order(
    tmp_path: Path, junit_source: str
) -> None:
    project = VfyProject(tmp_path)
    junit_arguments = ["--junitxml=results.xml"] if junit_source == "argument" else []
    environment = (
        {"PYTEST_ADDOPTS": "--junitxml=results.xml"} if junit_source == "environment" else {}
    )
    if junit_source == "configuration":
        project.write("pytest.ini", "[pytest]\naddopts = --junitxml=results.xml\n")
    project.write(
        "conftest.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item.user_properties.append(('collected', 'yes'))\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    worker = os.getenv('VFY_NATIVE_PARALLEL_INDEX')\n"
        "    if worker is None:\n"
        "        yield\n"
        "        return\n"
        "    location = pytest.Item.location\n"
        "    def completed_location(item):\n"
        "        raise RuntimeError('worker sessionfinish read completed item location')\n"
        "    pytest.Item.location = property(completed_location)\n"
        "    Path(f'worker-{worker}-location-guard').touch()\n"
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        "        pytest.Item.location = location\n",
    )
    project.write(
        "tests/test_a.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', [0, 1])\n"
        "def test_a(value, record_property, record_testsuite_property):\n"
        "    record_property('value', value)\n"
        "    record_testsuite_property('suite', 'a')\n"
        "    print(f'captured-a-{value}')\n",
    )
    project.write(
        "tests/test_b.py",
        "def test_b(record_property, record_testsuite_property):\n"
        "    record_property('value', 'b')\n"
        "    record_testsuite_property('suite', 'b')\n"
        "    print('captured-b')\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-cov",
        *junit_arguments,
        "--html=results.html",
        "-o",
        "junit_family=xunit1",
        "-o",
        "junit_logging=all",
        "-o",
        "junit_log_passing_tests=true",
        env=environment,
    )
    result.expect(0)
    assert all((tmp_path / f"worker-{index}-location-guard").exists() for index in (0, 1))
    root = ET.parse(tmp_path / "results.xml").getroot()
    cases = root.findall(".//testcase")
    assert [case.attrib["name"] for case in cases] == ["test_a[0]", "test_a[1]", "test_b"]
    assert [
        [
            (prop.attrib["name"], prop.attrib["value"])
            for prop in case.findall("properties/property")
        ]
        for case in cases
    ] == [
        [("collected", "yes"), ("value", "0")],
        [("collected", "yes"), ("value", "1")],
        [("collected", "yes"), ("value", "b")],
    ]
    suite_values = {
        prop.attrib["value"] for prop in root.findall(".//testsuite/properties/property")
    }
    assert suite_values == {"a", "b"}
    assert all(
        expected in "".join(case.itertext())
        for case, expected in zip(
            cases, ("captured-a-0", "captured-a-1", "captured-b"), strict=True
        )
    )
    html = (tmp_path / "results.html").read_text(encoding="utf-8")
    for nodeid, output in (
        ("tests/test_a.py::test_a[0]", "captured-a-0"),
        ("tests/test_a.py::test_a[1]", "captured-a-1"),
        ("tests/test_b.py::test_b", "captured-b"),
    ):
        assert nodeid in html
        assert output in html


@pytest.mark.parametrize("damage", ["corrupt", "delete"])
def test_invalid_parallel_coverage_preserves_previous_database_and_report(
    tmp_path: Path, damage: str
) -> None:
    # Damage the installed-pytest worker coverage shard before native aggregation.
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "conftest.py",
        "import os\nfrom pathlib import Path\nimport pytest\n"
        "@pytest.hookimpl(wrapper=True, tryfirst=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    yield\n"
        "    if os.getenv('VFY_NATIVE_PARALLEL_INDEX') == '1':\n"
        "        shard = Path(os.environ['COVERAGE_FILE'])\n"
        "        if os.getenv('VFY_SHARD_DAMAGE') == 'delete':\n"
        "            shard.unlink()\n"
        "        else:\n"
        "            shard.write_bytes(b'invalid coverage shard')\n"
        "        Path('damaged-shard').touch()\n",
    )
    for name in ("a", "b"):
        project.write(
            f"tests/test_{name}.py",
            "from app import value\ndef test_value():\n    assert value() == 1\n",
        )
    arguments = ("--no-replay", "--cov=app", "--cov-report=json:coverage.json")
    baseline = project.run_vfy("test", "--engine=pytest-native", "--workers=1", *arguments)
    baseline.expect(0)
    preserved = {name: (tmp_path / name).read_bytes() for name in (".coverage", "coverage.json")}

    rejected = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        *arguments,
        env={"VFY_SHARD_DAMAGE": damage},
    )

    assert rejected.returncode != 0, rejected.stdout + rejected.stderr
    assert (tmp_path / "damaged-shard").exists(), rejected.stdout + rejected.stderr
    assert {name: (tmp_path / name).read_bytes() for name in preserved} == preserved
