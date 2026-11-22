"""Native xdist preserves the metadata consumed by pytest report hooks."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from html import unescape
from pathlib import Path
from textwrap import dedent

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")) or importlib.util.find_spec("xdist") is None,
    reason="native xdist execution requires Linux or macOS and pytest-xdist",
)


def test_phase_reports_preserve_pytest_diagnostics(tmp_path: Path) -> None:
    observed_by_route = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            dedent("""\
                import json
                from pathlib import Path
                import pytest
                observations = []

                def pytest_runtest_logreport(report):
                    observations.append({
                        'nodeid': report.nodeid,
                        'phase': report.when,
                        'outcome': report.outcome,
                        'xfail': getattr(report, 'wasxfail', None),
                        'captured': bool(report.capstdout),
                        'skip_location': isinstance(report.longrepr, tuple),
                        'diagnostics': [
                            text
                            for text in ('failure evidence', 'setup evidence', 'teardown evidence')
                            if text in str(report.longrepr)
                        ],
                    })

                @pytest.fixture(autouse=True)
                def capture_each_phase():
                    print('setup evidence')
                    yield
                    print('teardown evidence')

                @pytest.fixture
                def broken_setup():
                    raise RuntimeError('setup evidence')

                @pytest.fixture
                def broken_teardown():
                    yield
                    raise RuntimeError('teardown evidence')

                def pytest_sessionfinish(session):
                    Path('projection.json').write_text(json.dumps(observations))
                """),
        )
        project.write(
            "test_projection.py",
            dedent("""\
                import pytest

                def test_pass():
                    print('call evidence')

                def test_fail():
                    assert False, 'failure evidence'

                def test_skip():
                    pytest.skip('skip evidence')

                @pytest.mark.xfail(reason='expected evidence')
                def test_xfail():
                    assert False

                @pytest.mark.xfail(reason='unexpected evidence')
                def test_xpass():
                    pass

                @pytest.mark.xfail(reason='strict evidence', strict=True)
                def test_strict_xpass():
                    pass

                def test_setup_error(broken_setup):
                    pass

                def test_teardown_error(broken_teardown):
                    pass
                """),
        )
        if route == "upstream":
            result = project.run_pytest(
                "-p", "xdist.plugin", "--numprocesses=0", "-q", load_xdist=False
            )
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--no-replay",
                "--no-cov",
                "--numprocesses=0",
                "-q",
            )
        result.expect(1)
        observed_by_route[route] = json.loads((project.root / "projection.json").read_text())
    observations = observed_by_route["native"]
    assert observations == observed_by_route["upstream"]
    assert len(observations) == 23
    assert {entry["phase"] for entry in observations} == {"setup", "call", "teardown"}
    assert {entry["outcome"] for entry in observations} == {"passed", "failed", "skipped"}
    assert any(entry["xfail"] for entry in observations)
    assert any(entry["skip_location"] for entry in observations)
    assert any(entry["captured"] for entry in observations)


@pytest.mark.parametrize("workers", ["0", "1"])
def test_report_hooks_keep_custom_sections_and_phase_clock_bounds(
    tmp_path: Path, workers: str
) -> None:
    sections: dict[str, list[tuple[str, str, object]]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            dedent("""\
                import json
                import logging
                import sys
                import time
                from pathlib import Path
                import pytest

                phase_times = {}

                def record_phase(request, phase):
                    phase_times[request.node.nodeid, phase] = time.time()
                    request.node.add_report_section(phase, 'custom', phase + '-first')
                    request.node.add_report_section(phase, 'custom', phase + '-second')
                    print(phase + '-stdout')
                    print(phase + '-stderr', file=sys.stderr)
                    logging.warning('%s-log', phase)

                @pytest.fixture(autouse=True)
                def observed(request):
                    record_phase(request, 'setup')
                    yield
                    record_phase(request, 'teardown')

                @pytest.fixture
                def broken():
                    raise RuntimeError('setup failure evidence')

                def pytest_runtest_logreport(report):
                    if hasattr(report, 'node'):
                        return
                    event = {
                        'nodeid': report.nodeid, 'when': report.when,
                        'sections': report.sections,
                        'start': report.start, 'stop': report.stop,
                        'duration': report.duration, 'published': time.time(),
                        'inside': phase_times.get((report.nodeid, report.when)),
                    }
                    with Path('reports.jsonl').open('a') as stream:
                        stream.write(json.dumps(event) + '\\n')
                """),
        )
        project.write(
            "test_phases.py",
            dedent("""\
                from conftest import record_phase

                def test_pass(request):
                    record_phase(request, 'call')

                def test_call_failure(request):
                    record_phase(request, 'call')
                    assert False, 'call failure evidence'

                def test_setup_failure(broken):
                    raise AssertionError('the body must not run')
                """),
        )
        arguments = (f"--numprocesses={workers}", "-q")
        if route == "upstream":
            result = project.run_pytest("-p", "xdist.plugin", *arguments, load_xdist=False)
        else:
            result = project.run_vfy(
                "test", "--engine=pytest-native", "--no-replay", "--no-cov", *arguments
            )
        result.expect(1)
        assert "1 failed, 1 passed, 1 error" in result.stdout
        reports = [
            json.loads(line) for line in (project.root / "reports.jsonl").read_text().splitlines()
        ]
        assert len(reports) == 8
        previous_stop = 0.0
        for report in reports:
            assert previous_stop <= report["start"] < report["stop"] <= report["published"], report
            assert report["duration"] > 0, report
            if report["inside"] is not None:
                assert report["start"] <= report["inside"] <= report["stop"], report
            current_logs = [
                content
                for name, content in report["sections"]
                if name == f"Captured log {report['when']}"
            ]
            assert len(current_logs) == 1, report
            assert current_logs[0].endswith(f"{report['when']}-log"), report
            previous_stop = report["stop"]
        sections[route] = [
            (report["nodeid"], report["when"], report["sections"]) for report in reports
        ]
        assert [
            section
            for section in reports[1]["sections"]
            if not section[0].startswith("Captured log")
        ] == [
            ["Captured custom setup", "setup-first"],
            ["Captured custom setup", "setup-second"],
            ["Captured stdout setup", "setup-stdout\n"],
            ["Captured stderr setup", "setup-stderr\n"],
            ["Captured custom call", "call-first"],
            ["Captured custom call", "call-second"],
            ["Captured stdout call", "call-stdout\n"],
            ["Captured stderr call", "call-stderr\n"],
        ]
    assert sections["native"] == sections["upstream"]


@pytest.mark.parametrize("workers", ["0", "1"])
def test_dispatched_cases_retain_their_own_html_attachment_assets(
    tmp_path: Path, workers: str
) -> None:
    evidence: dict[str, dict[str, list[str]]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\ngenerate_report_on_test = true\n")
        project.write(
            "test_attachments.py",
            dedent("""\
                import os
                from importlib import import_module
                import pytest

                html_extras = import_module(os.environ['HTML_EXTRAS']).extras

                @pytest.fixture
                def attachment(value, extras):
                    extras.append(html_extras.text(value + ' setup', name='setup'))
                    yield
                    extras.append(html_extras.text(value + ' teardown', name='teardown'))

                @pytest.mark.parametrize('value', ['alpha', 'beta', 'gamma'])
                def test_attachment(value, attachment, extras):
                    extras.append(html_extras.text(value + ' call', name='call'))
                """),
        )
        arguments = (f"--numprocesses={workers}", "--html=results.html", "-q")
        if route == "upstream":
            result = project.run_pytest(
                "-p",
                "xdist.plugin",
                "-p",
                "pytest_html.plugin",
                "-p",
                "pytest_html.fixtures",
                "-p",
                "pytest_metadata.plugin",
                *arguments,
                load_xdist=False,
                env={"HTML_EXTRAS": "pytest_html"},
            )
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--no-replay",
                "--no-cov",
                *arguments,
                env={"HTML_EXTRAS": "vfy.pytest_html"},
            )
        result.expect(0)
        assert "3 passed" in result.stdout
        document = (project.root / "results.html").read_text()
        if route == "upstream":
            blob = re.search(r'data-jsonblob="([^"]+)"', document)
            assert blob is not None
            evidence[route] = {
                nodeid: [
                    (project.root / extra["content"]).read_text() for extra in rows[0]["extras"]
                ]
                for nodeid, rows in json.loads(unescape(blob[1]))["tests"].items()
            }
        else:
            evidence[route] = {}
            for value in ("alpha", "beta", "gamma"):
                nodeid = f"test_attachments.py::test_attachment[{value}]"
                row = next(row for row in document.split("<tr>") if nodeid in row)
                evidence[route][nodeid] = [
                    (project.root / asset).read_text()
                    for asset in re.findall(r'href="(assets/[^"]+\.txt)"', row)
                ]
        assert evidence[route] == {
            f"test_attachments.py::test_attachment[{value}]": [
                f"{value} setup",
                f"{value} call",
                *([f"{value} teardown"] if route == "native" else []),
            ]
            for value in ("alpha", "beta", "gamma")
        }
    # Pytest-html retains call attachments; vfy also retains fixture teardown evidence.
    assert {nodeid: assets[:2] for nodeid, assets in evidence["native"].items()} == evidence[
        "upstream"
    ]


@pytest.mark.parametrize("workers", ["0", "1"])
def test_failure_summary_starts_on_its_own_line(tmp_path: Path, workers: str) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("test_failure.py", "def test_failure(): assert False\n")
        arguments = (f"--numprocesses={workers}", "-q")
        if route == "upstream":
            result = project.run_pytest("-p", "xdist.plugin", *arguments, load_xdist=False)
        else:
            result = project.run_vfy(
                "test", "--engine=pytest-native", "--no-replay", "--no-cov", *arguments
            )
        result.expect(1)
        assert re.search(r"^=+ short test summary info =+$", result.stdout, re.MULTILINE), (
            result.stdout
        )
