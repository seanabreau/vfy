"""Independent coverage preserves upstream pytest-cov data and fixture effects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def _run(project: VfyProject, route: str, *arguments: str) -> CommandResult:
    if route == "oracle":
        return project.run_python(
            "-m",
            "pytest",
            "-p",
            "pytest_cov.plugin",
            *arguments,
            env={"COVERAGE_FILE": str(project.root / ".coverage.oracle")},
        )
    return project.run_python(
        "-m",
        "vfy.independent.entry",
        "-p",
        "vfy.independent.coverage_support",
        "--native-result-json=candidate-result.json",
        *arguments,
        env={
            "COVERAGE_FILE": str(project.root / ".coverage.candidate"),
            "PYTHONPATH": _SOURCE,
        },
    )


def _data(project: VfyProject, route: str) -> dict[str, object]:
    data = project.data(f".coverage.{route}")
    path = str(project.root / "product.py")
    return {
        "lines": data.lines(path),
        "arcs": data.arcs(path),
        "contexts": {line: sorted(names) for line, names in data.contexts_by_lineno(path).items()},
    }


def test_collection_fixture_contexts_and_no_cover_match_stock(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    # Coverage.py's default sysmon core cannot preserve dynamic contexts on 3.14+.
    project.write(".coveragerc", "[run]\ncore = ctrace\n")
    project.write(
        "product.py",
        "def choose(value):\n"
        "    if value:\n"
        "        return 'positive'\n"
        "    return 'negative'\n\n"
        "def excluded():\n"
        "    return 'excluded'\n\n"
        "def uncalled():\n"
        "    return 'uncalled'\n",
    )
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "from product import choose\n\n"
        "@pytest.fixture\n"
        "def resource():\n"
        "    assert choose(False) == 'negative'\n"
        "    yield\n"
        "    assert choose(True) == 'positive'\n"
        "    Path('cleaned').write_text('done')\n",
    )
    project.write(
        "test_product.py",
        "import pytest\n"
        "from product import choose, excluded\n\n"
        "@pytest.mark.parametrize('value', [True, False])\n"
        "def test_value(value, resource, cov):\n"
        "    assert cov is not None\n"
        "    assert choose(value) == ('positive' if value else 'negative')\n\n"
        "@pytest.mark.no_cover\n"
        "def test_marker():\n"
        "    assert excluded() == 'excluded'\n\n"
        "def test_fixture(no_cover):\n"
        "    assert excluded() == 'excluded'\n",
    )
    records = []
    for route in ("oracle", "candidate"):
        result = _run(
            project,
            route,
            "--cov=product",
            "--cov-branch",
            "--cov-context=test",
            f"--cov-report=json:{route}.json",
            "--strict-markers",
            "-q",
        )
        result.expect(0)
        assert (project.root / "cleaned").read_text() == "done"
        (project.root / "cleaned").unlink()
        report = json.loads((project.root / f"{route}.json").read_text())
        records.append((_data(project, route), report["files"]["product.py"]))
    assert records[1] == records[0]
    assert records[0][0]["lines"] == [1, 2, 3, 4, 6, 9]
    data = project.data(".coverage.oracle")
    try:
        assert set(data.contexts_by_lineno(str(project.root / "product.py"))[2]) == {
            f"test_product.py::test_value[{value}]|{phase}"
            for value in (True, False)
            for phase in ("setup", "run", "teardown")
        }
    finally:
        data.close(force=True)
    candidate = json.loads((project.root / "candidate-result.json").read_text())
    assert candidate["engine"] == "vfy-independent-v1"


@pytest.mark.parametrize("mode", ["threshold", "no_cov", "no_report_on_fail"])
def test_coverage_exit_status_and_report_side_effects_match_stock(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("product.py", "def covered():\n    return 1\n\ndef missed():\n    return 2\n")
    disabled = mode == "no_cov"
    fails = mode == "no_report_on_fail"
    project.write(
        "test_product.py",
        "from product import covered\n\n"
        "def test_value(cov):\n"
        f"    assert (cov is None) is {disabled!r}\n"
        f"    assert covered() == {2 if fails else 1}\n",
    )
    if fails:
        project.write(
            "oracle_failure_capture.py",
            "import json\nfrom pathlib import Path\n\n"
            "def pytest_runtest_logreport(report):\n"
            "    if report.when == 'call' and report.failed:\n"
            "        Path('oracle-failure.json').write_text(\n"
            "            json.dumps(report.longrepr.reprcrash.message), encoding='utf-8')\n",
        )
    arguments = {"threshold": "--cov-fail-under=100", "no_cov": "--no-cov"}
    extra = arguments.get(mode, "--no-cov-on-fail")
    observations = []
    oracle_message: str | None = None
    for route in ("oracle", "candidate"):
        oracle_arguments = ("-p", "oracle_failure_capture") if route == "oracle" and fails else ()
        result = _run(
            project,
            route,
            *oracle_arguments,
            "--cov=product",
            f"--cov-report=json:{route}.json",
            extra,
            "-q",
        )
        if route == "oracle" and fails:
            capture = project.root / "oracle-failure.json"
            assert capture.is_file(), result.stdout + result.stderr
            captured: object = json.loads(capture.read_text(encoding="utf-8"))
            assert isinstance(captured, str)
            oracle_message = captured
        if route == "candidate":
            record = json.loads((project.root / "candidate-result.json").read_text())
            assert len(record["cases"]) == 1
            phases = record["cases"][0]["phases"]
            assert [phase["outcome"] for phase in phases] == [
                "passed",
                "failed" if fails else "passed",
                "passed",
            ]
            if fails:
                assert phases[1]["exception"] == oracle_message
                assert "1 == 2" in phases[1]["exception"]
        observations.append(
            (
                result.returncode,
                (project.root / f".coverage.{route}").is_file(),
                (project.root / f"{route}.json").is_file(),
            )
        )
    assert observations[1] == observations[0]
    assert (
        observations[0]
        == {
            "threshold": (1, True, True),
            "no_cov": (0, False, False),
            "no_report_on_fail": (1, True, False),
        }[mode]
    )
