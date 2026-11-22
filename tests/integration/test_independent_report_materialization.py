"""Completed protocols preserve report edits and undelivered phase snapshots."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("delivery", ["retained", "unretained"])
def test_wrapper_report_edits_survive_its_error_without_revising_undelivered_phases(
    tmp_path: Path, delivery: str
) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            f"""import json, pytest
from pathlib import Path
reports = []

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    report.duration = 0.5
    report.sections.append(('publication', call.when))
    return report

def pytest_runtest_logreport(report):
    reports.append(report)

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    if {delivery!r} == 'unretained':
        assert item.config.pluginmanager.unregister(name='session') is not None
    result = yield
    assert result is True and [report.when for report in reports] == ['setup', 'call', 'teardown']
    report = reports[1]
    report.outcome = 'failed'
    report.duration = 1.25
    report.longrepr = 'actual protocol wrapper report edit'
    report.sections.append(('wrapper', 'completed'))
    raise RuntimeError('actual protocol wrapper failure')

def pytest_sessionfinish():
    report = reports[1]
    Path('observed.json').write_text(json.dumps({{
        'phases': [report.when for report in reports],
        'call': [report.outcome, report.duration, report.longrepr, report.sections],
    }}))
""",
        )
        project.write("test_body.py", "def test_body(): pass\n")
        result = (
            project.run_python("-m", "pytest", "-q")
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--workers=1",
                "-q",
                "--native-result-json=result.json",
            )
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "actual protocol wrapper failure" in result.stdout + result.stderr
        observed = json.loads((project.root / "observed.json").read_text())
        assert observed == {
            "phases": ["setup", "call", "teardown"],
            "call": [
                "failed",
                1.25,
                "actual protocol wrapper report edit",
                [["publication", "call"], ["wrapper", "completed"]],
            ],
        }
        observations.append(observed)
        if engine == "native":
            record = json.loads((project.root / "result.json").read_text())
            assert len(record["cases"]) == 1
            call = record["cases"][0]["phases"][1]
            retained = delivery == "retained"
            assert record["counts"]["failed" if retained else "passed"] == 1
            assert call["outcome"] == ("failed" if retained else "passed")
            assert call["duration_ms"] == (1250 if retained else 500)
            assert call["detail"] == ("actual protocol wrapper report edit" if retained else "")
            assert call["report_sections"] == [
                {"name": "publication", "content": "call"},
                *([{"name": "wrapper", "content": "completed"}] if retained else []),
            ]
            assert "actual protocol wrapper failure" in str(record["internal_errors"])
    assert observations[0] == observations[1]


def test_rejected_foreign_report_keeps_worker_progress_at_its_publication_boundary(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json, os, pytest
from pathlib import Path
calls = []

@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    report.duration = 0.5
    return report

def pytest_runtest_logreport(report):
    if report.when == 'call':
        calls.append(report)

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result = yield
    if len(calls) > 1:
        report = calls[-1]
        report.outcome = 'failed'
        report.duration = 1.25
        report.longrepr = 'actual rejected wrapper edit'
        Path(f'mutated-{os.getpid()}.json').write_text(json.dumps(report.nodeid))
        item.ihook.pytest_runtest_logreport(report=calls[0])
    return result

@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    try:
        return (yield)
    except BaseException as error:
        Path(f'rejected-{os.getpid()}.json').write_text(json.dumps({
            'type': type(error).__name__, 'message': str(error),
        }))
        raise

def pytest_unconfigure():
    directory = os.environ.get('VFY_NATIVE_PARALLEL_DIR')
    if directory is not None:
        source = Path(directory) / ('progress.' + os.environ['VFY_NATIVE_PARALLEL_INDEX'])
        if source.exists():
            Path(f'progress-{os.getpid()}.jsonl').write_bytes(source.read_bytes())
""",
    )
    for label in ("a", "b"):
        project.write(f"test_{label}.py", "def test_first(): pass\ndef test_second(): pass\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "-v",
        env={"VFY_WORKER_START": "exec"},
    )
    diagnostic = result.stdout + result.stderr
    assert result.returncode == 4, diagnostic
    mutations = sorted(project.root.glob("mutated-*.json"))
    assert mutations, diagnostic
    for mutation in mutations:
        nodeid = json.loads(mutation.read_text())
        rejection = mutation.with_name(mutation.name.replace("mutated-", "rejected-"))
        assert json.loads(rejection.read_text()) == {
            "type": "UnsupportedFeatureError",
            "message": "test protocol report belongs to another native case",
        }, diagnostic
        progress = mutation.with_name(mutation.name.replace("mutated-", "progress-")).with_suffix(
            ".jsonl"
        )
        assert progress.exists(), diagnostic
        rows = [json.loads(line) for line in progress.read_text().splitlines()]
        (case,) = [row for row in rows if row["nodeid"] == nodeid]
        call = next(phase for phase in case["phases"] if phase["phase"] == "call")
        assert [call["outcome"], call["duration_ms"], call["detail"]] == ["passed", 500, ""], (
            diagnostic
        )


def test_late_report_conversion_error_keeps_completed_case_after_internalerror_repair(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
calls = []
observed = {}
class ProjectionError(Exception): pass
failure = ProjectionError('actual report projection failure')

class BrokenRepresentation:
    def __str__(self):
        raise failure

def pytest_runtest_logreport(report):
    if report.when == 'call':
        calls.append(report)
        report.sections.append(('publication', 'actual publication section'))

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result = yield
    calls[-1].longrepr = BrokenRepresentation()
    return result

def pytest_internalerror(excinfo):
    observed['original_error'] = excinfo.value is failure
    calls[-1].longrepr = 'actual repaired report'
    calls[-1].sections.append(('repair', 'actual repaired section'))

def pytest_sessionfinish(session, exitstatus):
    observed.update(testsfailed=session.testsfailed, exitstatus=int(exitstatus))
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    project.write("test_body.py", "def test_body(): print('actual body output')\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=1",
        "-q",
        "--native-result-json=result.json",
    )
    diagnostic = result.stdout + result.stderr
    assert result.returncode == 3, diagnostic
    assert json.loads((project.root / "observed.json").read_text()) == {
        "original_error": True,
        "testsfailed": 0,
        "exitstatus": 3,
    }, diagnostic
    record = json.loads((project.root / "result.json").read_text())
    assert record["counts"]["passed"] == 1, diagnostic
    assert len(record["cases"]) == 1
    call = record["cases"][0]["phases"][1]
    assert call["outcome"] == "passed"
    assert call["detail"] == "actual repaired report"
    assert call["stdout"] == "actual body output\n"
    assert call["report_sections"][-2:] == [
        {"name": "publication", "content": "actual publication section"},
        {"name": "repair", "content": "actual repaired section"},
    ]
    assert "actual report projection failure" in str(record["internal_errors"])


def test_protocol_repairs_unrendered_reports_before_their_first_consumer(
    tmp_path: Path,
) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import json, pytest
from pathlib import Path
reports = []

class UnrenderedRepresentation:
    def __str__(self):
        raise AssertionError('representation consumed before protocol repair')

def pytest_runtest_logreport(report):
    report.longrepr = UnrenderedRepresentation()
    report.sections.append(('custom', report.when))
    reports.append(report)

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result = yield
    assert result is True
    assert [report.when for report in reports] == ['setup', 'call', 'teardown']
    for report in reports:
        report.longrepr = 'actual repaired ' + report.when
    return result

def pytest_sessionfinish():
    Path('observed.json').write_text(json.dumps([
        [report.when, report.outcome, str(report.longrepr)] for report in reports
    ]))
""",
        )
        project.write("test_body.py", "def test_body(): print('actual body output')\n")
        result = (
            project.run_python("-m", "pytest", "-q")
            if engine == "stock"
            else project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--workers=1",
                "-q",
                "--native-result-json=result.json",
            )
        )
        diagnostic = result.stdout + result.stderr
        assert result.returncode == 0, diagnostic
        observed = json.loads((project.root / "observed.json").read_text())
        assert observed == [
            [phase, "passed", f"actual repaired {phase}"] for phase in ("setup", "call", "teardown")
        ], diagnostic
        observations.append(observed)
        if engine == "native":
            record = json.loads((project.root / "result.json").read_text())
            assert record["counts"]["passed"] == 1
            (case,) = record["cases"]
            assert [phase["detail"] for phase in case["phases"]] == [
                "actual repaired setup",
                "actual repaired call",
                "actual repaired teardown",
            ]
            call = case["phases"][1]
            assert call["stdout"] == "actual body output\n"
            assert call["report_sections"][-1] == {"name": "custom", "content": "call"}
    assert observations[0] == observations[1]
