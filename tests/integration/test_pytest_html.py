"""vfy's built-in pytest HTML and metadata replacement."""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import time
from html import unescape
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

ISOLATED = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


@pytest.mark.parametrize("self_contained", [False, True])
def test_image_bytes_and_references_match_pytest_html(tmp_path: Path, self_contained: bool) -> None:
    project = VfyProject(tmp_path)
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOioAAAAASUVORK5CYII="
    )
    (tmp_path / "pixel.png").write_bytes(pixel)
    (tmp_path / "logo").write_bytes(b"path contents")
    project.write(
        "test_image.py",
        "import base64\n"
        "import os\n"
        "from importlib import import_module\n"
        "from pathlib import Path\n"
        "html_extras = import_module(os.environ['HTML_EXTRAS']).extras\n"
        "def test_image(extras):\n"
        "    encoded = base64.b64encode(Path('pixel.png').read_bytes()).decode('ascii')\n"
        "    extras.append(html_extras.png(encoded))\n"
        "    extras.append(html_extras.png('pixel.png'))\n"
        "    extras.append(html_extras.png('https://example.com/pixel.png?x=1&y=2'))\n"
        "    extras.append(html_extras.png('logo'))\n",
    )
    options = ("--self-contained-html",) if self_contained else ()
    reference = project.run_pytest(
        "-p",
        "pytest_html.plugin",
        "-p",
        "pytest_html.fixtures",
        "-p",
        "pytest_metadata.plugin",
        "--html=oracle.html",
        *options,
        load_xdist=False,
        env={**ISOLATED, "HTML_EXTRAS": "pytest_html"},
    )
    reference.expect(0)
    reference_document = (tmp_path / "oracle.html").read_text(encoding="utf-8")
    blob = re.search(r'data-jsonblob="([^"]+)"', reference_document)
    assert blob is not None
    rows = json.loads(unescape(blob[1]))["tests"]["test_image.py::test_image"]
    assert len(rows) == 1
    reference_sources = [extra["content"] for extra in rows[0]["extras"]]
    actual = project.run_pytest(
        "-p",
        "vfy.pytest_reporting",
        "--html=actual.html",
        *options,
        load_xdist=False,
        env={**ISOLATED, "HTML_EXTRAS": "vfy.pytest_html"},
    )
    assert actual.returncode == reference.returncode, actual.stdout + actual.stderr
    document = (tmp_path / "actual.html").read_text(encoding="utf-8")
    actual_sources = [
        unescape(source) for source in re.findall(r'<img[^>]+src="([^"]+)"', document)
    ]
    assert len(actual_sources) == len(reference_sources) == 4
    assert (
        actual_sources[1:3]
        == reference_sources[1:3]
        == [
            "pixel.png",
            "https://example.com/pixel.png?x=1&y=2",
        ]
    )
    if self_contained:
        assert actual_sources[0] == reference_sources[0]
        assert actual_sources[0].startswith("data:image/png;base64,")
        assert base64.b64decode(actual_sources[0].partition(",")[2], validate=True) == pixel
        assert actual_sources[3] == reference_sources[3] == "data:image/png;base64,logo"
        assert not (tmp_path / "assets").exists()
    else:
        for source in (reference_sources[0], actual_sources[0]):
            assert source.startswith("assets/")
            assert source.endswith(".png")
            assert (tmp_path / source).read_bytes() == pixel
        for source in (reference_sources[3], actual_sources[3]):
            assert source.startswith("assets/")
            assert (tmp_path / source).read_bytes() == base64.b64decode("logo", validate=True)


@pytest.mark.parametrize("reporting_first", [False, True])
def test_direct_plugin_allows_autoload_with_installed_reporters_and_xdist(
    tmp_path: Path, reporting_first: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "src/app.py", "def choose(value):\n    if value:\n        return 1\n    return 0\n"
    )
    project.write(
        "test_app.py",
        "from src.app import choose\n"
        "from vfy.pytest_html import extras as html_extras\n\n"
        "def test_true(extras):\n"
        "    extras.append(html_extras.text('true attachment'))\n"
        "    assert choose(True) == 1\n\n"
        "def test_false(extras):\n"
        "    extras.append(html_extras.text('false attachment'))\n"
        "    assert choose(False) == 0\n",
    )
    plugins = ["vfy.pytest_plugin", "vfy.pytest_reporting"]
    if reporting_first:
        plugins.reverse()
    result = project.run_pytest(
        "-p",
        plugins[0],
        "-p",
        plugins[1],
        "-Werror",
        "-n",
        "2",
        "--cov=src",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=json:coverage.json",
        "--html=results.html",
        "--self-contained-html",
        "--metadata",
        "Region",
        "autoload",
        "-q",
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
    )
    result.expect(0)
    assert "2 passed" in result.stdout
    assert project.lines("src/app.py") == [1, 2, 3, 4]
    coverage = json.loads((tmp_path / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["totals"]["percent_covered"] == 100
    report = (tmp_path / "results.html").read_text(encoding="utf-8")
    assert "true attachment" in report
    assert "false attachment" in report
    assert "autoload" in report


@pytest.mark.parametrize(
    "plugin", ["pytest_cov.plugin", "pytest_html.plugin", "pytest_metadata.plugin"]
)
def test_direct_plugin_rejects_an_explicit_reporter_loaded_first(
    tmp_path: Path, plugin: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_case.py", "def test_case():\n    raise AssertionError('must not run')\n")
    replacement = "vfy.pytest_plugin" if plugin == "pytest_cov.plugin" else "vfy.pytest_reporting"
    result = project.run_pytest("-p", plugin, "-p", replacement, load_xdist=False)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert f"vfy replaces the {plugin!r} plug-in" in result.stderr
    assert "remove its explicit load" in result.stderr
    assert "Traceback" not in result.stderr


@requires_native_pytest
@pytest.mark.parametrize("feature", ["extras", "streaming"])
def test_autoload_repair_preserves_native_html_features(tmp_path: Path, feature: str) -> None:
    project = VfyProject(tmp_path)
    project.write("results.html", "previous report")
    if feature == "streaming":
        project.write("pytest.ini", "[pytest]\ngenerate_report_on_test = true\n")
    parameters = "extras" if feature == "extras" else ""
    project.write("test_case.py", f"def test_case({parameters}):\n    assert True\n")
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--workers=1",
        "--no-cov",
        "--html=results.html",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None},
    )
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
    assert "test_case.py::test_case" in (tmp_path / "results.html").read_text(encoding="utf-8")


def test_html_report_preserves_metadata_hooks_extras_redaction_and_css(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\nenvironment_table_redact_list = ^SECRET$\n",
    )
    project.write("custom.css", ".vfy-proof { color: purple; }\n")
    project.write(
        "conftest.py",
        "from vfy.pytest_metadata import metadata_key\n\n"
        "def pytest_configure(config):\n"
        "    config.stash[metadata_key]['Project'] = 'vfy integration'\n"
        "    config.stash[metadata_key]['SECRET'] = 'must-not-appear'\n\n"
        "def pytest_html_report_title(report):\n"
        "    report.title = 'vfy pytest results'\n\n"
        "def pytest_html_results_summary(prefix, summary, postfix):\n"
        "    summary.append('<p>custom summary</p>')\n\n"
        "def pytest_html_results_table_header(cells):\n"
        "    cells.append('Owner')\n\n"
        "def pytest_html_results_table_row(report, cells):\n"
        "    cells.append('vfy')\n\n"
        "def pytest_sessionfinish(session):\n"
        "    session.config.stash[metadata_key]['Finished'] = 'yes'\n",
    )
    project.write(
        "test_report.py",
        "from vfy.pytest_html import extras as html_extras\n\n"
        "def test_extra(extras):\n"
        "    extras.append(html_extras.text('attached evidence', name='proof'))\n",
    )

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--all",
        "-j",
        "1",
        "--no-cov",
        "--html=reports/results.html",
        "--css=custom.css",
        "--metadata",
        "CLI",
        "yes",
        env=ISOLATED,
    )

    completed.expect(0)
    report = (tmp_path / "reports/results.html").read_text(encoding="utf-8")
    stylesheet = (tmp_path / "reports/assets/style.css").read_text(encoding="utf-8")
    assert "vfy pytest results" in report
    assert "vfy integration" in report
    assert "custom summary" in report
    assert "Owner" in report
    assert "vfy" in report
    assert "CLI" in report
    assert "yes" in report
    assert "Finished" in report
    attachments = list((tmp_path / "reports/assets").glob("*.txt"))
    assert [path.read_text(encoding="utf-8") for path in attachments] == ["attached evidence"]
    assert "must-not-appear" not in report
    assert ".vfy-proof { color: purple; }" in stylesheet


def test_self_contained_html_reports_async_xdist_results(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_async.py",
        "import vfy\n"
        "import pytest\n\n"
        "from vfy.pytest_html import extras as html_extras\n\n"
        "@vfy.async_fixture\n"
        "async def value():\n"
        "    return 3\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_async_value(value, extras):\n"
        "    extras.append(html_extras.text('worker evidence'))\n"
        "    assert value == 3\n\n"
        "def test_sync_value():\n"
        "    assert 2 + 2 == 4\n",
    )

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--all",
        "-j",
        "2",
        "--no-cov",
        "--html=results.html",
        "--self-contained-html",
        env=ISOLATED,
    )

    completed.expect(0)
    report = (tmp_path / "results.html").read_text(encoding="utf-8")
    assert "test_async_value" in report
    assert "test_sync_value" in report
    assert "worker evidence" in report
    assert not (tmp_path / "assets").exists()


@requires_native_pytest
def test_html_report_records_pytest_outcomes_and_errors(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_outcomes.py",
        "import pytest\n\n"
        "def test_pass(): pass\n\n"
        "def test_fail(): assert False\n\n"
        "@pytest.mark.skip(reason='later')\n"
        "def test_skip(): pass\n\n"
        "@pytest.mark.xfail(reason='known')\n"
        "def test_xfail(): assert False\n\n"
        "@pytest.mark.xfail(reason='unexpected')\n"
        "def test_xpass(): pass\n\n"
        "@pytest.fixture\n"
        "def broken(): raise RuntimeError('setup broke')\n\n"
        "def test_error(broken): pass\n",
    )

    completed = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "-j",
        "1",
        "--no-cov",
        "--html",
        "results.html",
        env=ISOLATED,
    )

    assert completed.returncode == 1
    report = (tmp_path / "results.html").read_text(encoding="utf-8")
    for expected in (
        "1 Passed,",
        "1 Failed,",
        "1 Skipped,",
        "1 XFailed,",
        "1 XPassed,",
        "1 Error,",
        "setup broke",
    ):
        assert expected in report


@pytest.mark.parametrize("streaming", [pytest.param(False, marks=requires_native_pytest), True])
def test_report_failure_preserves_existing_report_and_test_status(
    tmp_path: Path, streaming: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\ngenerate_report_on_test = {streaming}\n")
    project.write("test_ok.py", "def test_ok(): pass\n")
    project.write("results.html", "accepted report")
    engine = ("--engine=pytest",) if streaming else ("--engine=pytest-native",)

    completed = project.run_vfy(
        "test",
        *engine,
        "--all",
        "-j",
        "1",
        "--no-cov",
        "--html=results.html",
        "--css=missing.css",
        env=ISOLATED,
    )

    assert completed.returncode == 0
    assert "failed to write HTML report" in completed.stdout
    assert (tmp_path / "results.html").read_text(encoding="utf-8") == "accepted report"


def test_confirmation_preserves_configured_primary_html_report(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "pytest.ini",
        "[pytest]\naddopts = --html=primary.html\n",
    )
    project.write(
        "test_unstable.py",
        "from pathlib import Path\n\n"
        "def test_unstable():\n"
        "    attempt = Path('.attempt')\n"
        "    if not attempt.exists():\n"
        "        attempt.write_text('primary')\n"
        "        assert False\n",
    )

    completed = project.run_vfy(
        "test",
        "--engine=pytest",
        "--confirm-failures",
        "-j",
        "1",
        "--no-cov",
        env=ISOLATED,
    )

    assert completed.returncode == 1
    assert "passed on confirmation; the original run remains failed" in completed.stderr
    report = (tmp_path / "primary.html").read_text(encoding="utf-8")
    assert "1 Failed," in report
    assert "0 Passed," in report


@pytest.mark.skipif(os.name == "nt", reason="managed Ctrl-C is POSIX-specific")
def test_watch_publishes_html_assets_without_replacing_unrelated_files(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("reports/app.py", "VALUE = 1\n")
    project.write(
        "test_app.py",
        "from pathlib import Path\n"
        "from reports.app import VALUE\n\n"
        "def test_value():\n"
        "    assert VALUE > 0\n"
        "    Path('.vfy').mkdir(exist_ok=True)\n"
        "    Path('.vfy/watch-value').write_text(str(VALUE), encoding='utf-8')\n",
    )
    project.write("reports/keep.txt", "user file")
    project.git_init()
    command = project.start_vfy(
        "--verbose",
        "test",
        "--engine=pytest",
        "--watch",
        "-j",
        "1",
        "--no-cov",
        env={
            **ISOLATED,
            "PYTEST_ADDOPTS": "--html=reports/results.html",
        },
    )
    try:
        baseline = command.wait_for_match(r"Watch run (?P<number>\d+): pytest exited 0", timeout=30)
        assert int(baseline.group("number")) >= 1
        assert (tmp_path / "reports/results.html").is_file()
        assert (tmp_path / "reports/assets/style.css").is_file()
        assert (tmp_path / "reports/keep.txt").read_text(encoding="utf-8") == "user file"
        assert (tmp_path / ".vfy/watch-value").read_text(encoding="utf-8") == "1"
        output_offset = len(command.text())
        project.write("reports/app.py", "VALUE = 2\n")
        changed = command.wait_for_match(
            r"Watch changes:",
            after=output_offset,
            timeout=30,
        )
        marker = tmp_path / ".vfy/watch-value"
        deadline = time.monotonic() + 30
        while marker.read_text(encoding="utf-8") != "2":
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "watch did not execute the edited app.py\n"
                    f"stdout:\n{command.text('stdout')}\nstderr:\n{command.text()}"
                )
            time.sleep(0.01)
        match = command.wait_for_match(
            r"Watch run (?P<number>\d+): pytest exited 0",
            after=changed.end(),
            timeout=30,
        )
        assert int(match.group("number")) > 1
        assert (tmp_path / ".vfy/watch-value").read_text(encoding="utf-8") == "2"
        assert (tmp_path / "reports/keep.txt").read_text(encoding="utf-8") == "user file"
    finally:
        command.send_signal(signal.SIGINT)
        result = command.wait(timeout=10)
    assert result.returncode == 2


@requires_native_pytest
def test_no_change_replay_preserves_existing_pytest_html_report(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "VALUE = 1\n")
    project.write(
        "test_app.py", "from app import VALUE\ndef test_value():\n    assert VALUE == 1\n"
    )
    project.git_init()
    pytest_arguments = (
        "test",
        "--engine=pytest-native",
        "-j",
        "1",
        "--html=results.html",
    )
    baseline = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "-j",
        "1",
        "--html=results.html",
        env=ISOLATED,
    )
    baseline.expect(0)
    accepted = (tmp_path / "results.html").read_bytes()

    replay = project.run_vfy(*pytest_arguments, env=ISOLATED)

    replay.expect(0)
    assert "complete coverage restored without running pytest" in replay.stderr
    assert "Existing HTML report preserved" in replay.stderr
    assert (tmp_path / "results.html").read_bytes() == accepted


@requires_native_pytest
@pytest.mark.parametrize(
    ("source", "outcome", "diagnostic", "status"),
    [
        (
            "import pytest\n@pytest.mark.xfail(run=False, reason='not ready')\n"
            "def test_wait(): pass\n",
            "XFailed",
            "not ready",
            0,
        ),
        (
            "import pytest\n@pytest.fixture\n"
            "def value(): pytest.xfail('fixture unavailable')\n"
            "def test_wait(value): pass\n",
            "XFailed",
            "fixture unavailable",
            0,
        ),
        ("raise RuntimeError('collection broke')\n", "Error", "collection broke", 2),
    ],
)
def test_non_call_outcomes_match_pytest_html(
    tmp_path: Path, source: str, outcome: str, diagnostic: str, status: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_result.py", source)
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_html.plugin",
        "-p",
        "pytest_metadata.plugin",
        "--html=reference.html",
        "--self-contained-html",
        env=ISOLATED,
    )
    actual = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "-j",
        "1",
        "--no-cov",
        "--html=actual.html",
        "--self-contained-html",
        env=ISOLATED,
    )
    assert actual.returncode == reference.returncode == status
    reference_html = (tmp_path / "reference.html").read_text(encoding="utf-8")
    blob = re.search(r'data-jsonblob="([^"]+)"', reference_html)
    assert blob is not None
    records = json.loads(unescape(blob[1]))["tests"]
    rows = [row for results in records.values() for row in results]
    assert len(rows) == 1
    assert outcome in rows[0]["resultsTableRow"][0]
    report = (tmp_path / "actual.html").read_text(encoding="utf-8")
    assert f"1 {outcome}," in report
    assert diagnostic in report


def test_long_test_id_and_all_phase_attachments(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\nfrom vfy.pytest_html import extras as html_extras\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = (yield).get_result()\n"
        "    report.extras = [html_extras.text(report.when, name=report.when)]\n",
    )
    project.write(
        "test_result.py",
        "import pytest\nfrom vfy.pytest_html import extras as html_extras\n"
        "@pytest.fixture\n"
        "def attachment(extras):\n"
        "    extras.append(html_extras.text('fixture setup'))\n"
        "    yield\n"
        "    extras.append(html_extras.text('fixture teardown'))\n"
        "@pytest.mark.parametrize('value', [1], ids=['x' * 400])\n"
        "def test_long(value, attachment): pass\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest",
        "--all",
        "-j",
        "1",
        "--no-cov",
        "--html=report.html",
        env=ISOLATED,
    )
    result.expect(0)
    assert (tmp_path / "report.html").is_file(), result.stdout
    assert sorted(
        path.read_text(encoding="utf-8") for path in (tmp_path / "assets").glob("*.txt")
    ) == ["call", "fixture setup", "fixture teardown", "setup", "teardown"]


@requires_native_pytest
def test_metadata_redaction_matches_pytest_html(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_ok.py", "def test_ok(): pass\n")
    project.write("pytest.ini", "[pytest]\nenvironment_table_redact_list = SECRET\n")
    arguments = (
        "--self-contained-html",
        "--metadata",
        "SECRET",
        "hidden-value",
        "--metadata",
        "NOT_SECRET",
        "visible-value",
    )
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_html.plugin",
        "-p",
        "pytest_metadata.plugin",
        "--html=reference.html",
        *arguments,
        env=ISOLATED,
    )
    actual = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "-j",
        "1",
        "--no-cov",
        "--html=actual.html",
        *arguments,
        env=ISOLATED,
    )
    assert actual.returncode == reference.returncode == 0
    for name in ("reference.html", "actual.html"):
        report = (tmp_path / name).read_text(encoding="utf-8")
        assert "hidden-value" not in report
        assert "visible-value" in report


@requires_native_pytest
@pytest.mark.parametrize("self_contained", [False, True])
def test_streaming_report_is_available_to_the_next_test(
    tmp_path: Path, self_contained: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_streaming.py",
        "from pathlib import Path\n"
        "import os\n"
        "from importlib import import_module\n"
        "html_extras = import_module(os.environ['HTML_EXTRAS']).extras\n"
        "def test_z_first(extras):\n"
        "    extras.append(html_extras.text('first evidence'))\n"
        "def test_a_second(extras):\n"
        "    report = Path('report.html').read_text()\n"
        "    assert 'test_z_first' in report\n"
        "    assert 'test_a_second' not in report\n"
        "    extras.append(html_extras.text('second evidence'))\n",
    )
    arguments = (
        "--html=report.html",
        *(("--self-contained-html",) if self_contained else ()),
        "-o",
        "generate_report_on_test=true",
    )
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_html.plugin",
        "-p",
        "pytest_html.fixtures",
        "-p",
        "pytest_metadata.plugin",
        *arguments,
        env={**ISOLATED, "HTML_EXTRAS": "pytest_html"},
    )
    (tmp_path / "report.html").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "-j",
        "1",
        "--no-cov",
        *arguments,
        env={**ISOLATED, "HTML_EXTRAS": "vfy.pytest_html"},
    )
    assert actual.returncode == reference.returncode == 0, actual.stdout + actual.stderr
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "2 Passed," in report
    for nodeid, evidence in (
        ("test_z_first", "first evidence"),
        ("test_a_second", "second evidence"),
    ):
        row = next(row for row in report.split("<tr>") if nodeid in row)
        if self_contained:
            assert evidence in row
        else:
            attachment = re.search(r'href="(assets/[^"]+\.txt)"', row)
            assert attachment is not None
            assert (tmp_path / attachment[1]).read_text() == evidence
