"""Compare independent and stock-pytest HTML reports and check pytest-native rejection."""

from __future__ import annotations

import argparse
import json
import re
import sys
from html import unescape
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject

TESTS = """\
import logging
import sys

import pytest


def test_pass(metadata):
    metadata["Runtime"] = "fixture"
    print("pass stdout sentinel")


def test_fail():
    print("failure stdout sentinel")
    print("failure stderr sentinel", file=sys.stderr)
    logging.warning("failure log sentinel")
    assert False, "failure assertion sentinel"


@pytest.mark.skip(reason="skip reason sentinel")
def test_skip():
    pass


@pytest.mark.xfail(reason="xfail reason sentinel")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="xpass reason sentinel")
def test_xpass():
    pass


@pytest.fixture
def broken():
    raise RuntimeError("setup error sentinel")


def test_setup_error(broken):
    pass
"""

CONFTEST = """\
from vfy.pytest_metadata import metadata_key


def pytest_configure(config):
    config.stash[metadata_key]["Project"] = "native HTML differential"


def pytest_html_report_title(report):
    report.title = "vfy native HTML"


def pytest_html_results_summary(prefix, summary, postfix):
    summary.append("<p>summary hook sentinel</p>")


def pytest_html_results_table_header(cells):
    cells.append("Owned header")
"""

IMPOSTOR_CONFTEST = """\
class HtmlImpostor:
    def _native_complete(self, result):
        raise AssertionError("impostor adapter executed")


def pytest_configure(config):
    reporter = config.pluginmanager.getplugin("_vfy_html_reporter")
    assert reporter is not None
    config.pluginmanager.unregister(reporter)
    config.pluginmanager.register(HtmlImpostor(), "_vfy_html_reporter")
"""

NOT_RUN_TEST = """\
from pathlib import Path


def test_not_run():
    Path("executed").write_text("yes", encoding="utf-8")
"""

DETAIL_MARKERS = (
    "pass stdout sentinel",
    "failure stdout sentinel",
    "failure stderr sentinel",
    "failure log sentinel",
    "failure assertion sentinel",
    "skip reason sentinel",
    "xfail reason sentinel",
    "xpass reason sentinel",
    "setup error sentinel",
)


def _text(fragment: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", fragment))


def observe_report(path: Path) -> dict[str, object]:
    html = path.read_text(encoding="utf-8")
    metadata_match = re.search(
        r'<table class="meta">.*?<tbody>(.*?)</tbody></table>', html, re.DOTALL
    )
    metadata = {}
    if metadata_match is not None:
        metadata = {
            _text(key): _text(value)
            for key, value in re.findall(
                r"<tr><th>(.*?)</th><td>(.*?)</td></tr>", metadata_match[1], re.DOTALL
            )
        }
    rows = []
    for outcome, nodeid, body in re.findall(
        r'<tr><td class="([^"]+)">[^<]*</td><td>(.*?)</td><td>.*?</td><td>(.*?)</td></tr>',
        html,
        re.DOTALL,
    ):
        details = _text(body)
        rows.append(
            {
                "nodeid": _text(nodeid),
                "outcome": outcome,
                "details": [marker for marker in DETAIL_MARKERS if marker in details],
            }
        )
    return {
        "title": "<h1>vfy native HTML</h1>" in html,
        "summary_hook": "summary hook sentinel" in html,
        "header_hook": "<th>Owned header</th>" in html,
        "metadata": {
            "CLI": metadata.get("CLI"),
            "Project": metadata.get("Project"),
            "Runtime": metadata.get("Runtime"),
        },
        "rows": rows,
    }


def run(
    project: VfyProject, python: Path, engine: str, report: str, *, workers: int = 1
) -> tuple[int, str]:
    if not (project.root / "pytest.ini").exists():
        project.write("pytest.ini", "[pytest]\n")
    completed = project.run(
        str(python.absolute()),
        "-m",
        "vfy",
        "test",
        "--all",
        "-j",
        str(workers),
        f"--engine={engine}",
        "--no-cov",
        "--rootdir",
        str(project.root),
        f"--html={report}",
        "--self-contained-html",
        "--metadata",
        "CLI",
        "installed",
        env={"PYTHONPATH": None},
    )
    return completed.returncode, completed.stdout + completed.stderr


def assert_rejected(
    root: Path,
    python: Path,
    *,
    conftest: str = "",
    ini: str = "",
    test: str,
    diagnostic: str,
    workers: int = 1,
) -> None:
    project = VfyProject(root)
    if conftest:
        project.write("conftest.py", conftest)
    if ini:
        project.write("pytest.ini", ini)
    project.write("test_rejected.py", test)
    project.write("preserved.html", "preserved report\n")
    status, terminal = run(project, python, "pytest-native", "preserved.html", workers=workers)
    if (
        status != 4
        or diagnostic not in terminal
        or (project.root / "executed").exists()
        or (project.root / "preserved.html").read_text(encoding="utf-8") != "preserved report\n"
    ):
        raise RuntimeError(f"pytest-native did not reject {root.name}\n{terminal}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    observations: dict[str, object] = {}
    for name, python, engine in (
        ("pytest", args.stock_python, "pytest"),
        ("native", args.candidate_python, "native"),
    ):
        project = VfyProject(output / name)
        project.write("conftest.py", CONFTEST)
        project.write("test_html.py", TESTS)
        report = "results.html"
        status, terminal = run(project, python, engine, report)
        if status != 1:
            raise RuntimeError(f"{name} route returned {status}\n{terminal}")
        observations[name] = observe_report(project.root / report)

    if observations["native"] != observations["pytest"]:
        raise RuntimeError(
            "native HTML differs from the pytest route:\n"
            + json.dumps(observations, indent=2, sort_keys=True)
        )
    expected = {
        "test_html.py::test_fail": "Failed",
        "test_html.py::test_pass": "Passed",
        "test_html.py::test_setup_error": "Error",
        "test_html.py::test_skip": "Skipped",
        "test_html.py::test_xfail": "XFailed",
        "test_html.py::test_xpass": "XPassed",
    }
    native_observation = cast(dict[str, object], observations["native"])
    expected_document = {
        "title": True,
        "summary_hook": True,
        "header_hook": True,
        "metadata": {
            "CLI": "installed",
            "Project": "native HTML differential",
            "Runtime": "fixture",
        },
    }
    if {key: native_observation[key] for key in expected_document} != expected_document:
        raise RuntimeError(f"native HTML document hooks differ: {native_observation!r}")
    native_rows = cast(list[dict[str, object]], native_observation["rows"])
    actual = {str(row["nodeid"]): str(row["outcome"]) for row in native_rows}
    if actual != expected:
        raise RuntimeError(f"native HTML outcomes differ: {actual!r}")

    assert_rejected(
        output / "impostor",
        args.candidate_python,
        conftest=IMPOSTOR_CONFTEST,
        test=NOT_RUN_TEST,
        diagnostic="unsupported vfy HTML adapter",
    )
    project = VfyProject(output / "attachments")
    project.write(
        "test_attachment.py",
        "from vfy.pytest_html import extras as html_extras\n\n"
        "def test_attachment(extras):\n"
        "    extras.append(html_extras.text('attachment contents', name='attachment sentinel'))\n",
    )
    status, terminal = run(project, args.candidate_python, "pytest-native", "attachments.html")
    if status != 0:
        raise RuntimeError(f"pytest-native attachment test returned {status}\n{terminal}")
    attachment_report = (project.root / "attachments.html").read_text(encoding="utf-8")
    expected_attachment = ("attachment sentinel", "attachment contents")
    if any(detail not in attachment_report for detail in expected_attachment):
        raise RuntimeError("pytest-native omitted the captured attachment")
    assert_rejected(
        output / "row-hook",
        args.candidate_python,
        conftest="def pytest_html_results_table_row(report, cells): pass\n",
        test=NOT_RUN_TEST,
        diagnostic="pytest_html_results_table_row",
    )
    assert_rejected(
        output / "parallel-streaming",
        args.candidate_python,
        ini="[pytest]\ngenerate_report_on_test = true\n",
        test=NOT_RUN_TEST,
        diagnostic="generate_report_on_test requires --workers=1",
        workers=2,
    )
    observations["attachments"] = True
    observations["rejections"] = ["impostor", "row-hook", "parallel-streaming"]
    record = output / "native-vfy-html.json"
    record.write_text(json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"{record}\n")


if __name__ == "__main__":
    main()
