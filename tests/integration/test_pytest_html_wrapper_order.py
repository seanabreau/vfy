"""HTML attachments honor report edits made by outer wrappers."""

from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform == "win32", reason="independent workers require Unix")
@pytest.mark.parametrize("mutation", ["assign", "append"])
@pytest.mark.parametrize("workers", [1, 2])
def test_tryfirst_report_wrapper_attachments_match_pytest_html(
    tmp_path: Path, mutation: str, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import os, pytest\n"
        "from importlib import import_module\n"
        "html_extras = import_module(os.environ['HTML_EXTRAS']).extras\n"
        "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
        "def pytest_runtest_makereport(item):\n"
        "    report = (yield).get_result()\n"
        "    if report.when == 'call':\n"
        "        extra = html_extras.url('https://example.com/' + item.name, "
        "name='late evidence')\n"
        f"        if {mutation!r} == 'assign':\n"
        "            report.extras = [extra]\n"
        "        else:\n"
        "            report.extras = getattr(report, 'extras', [])\n"
        "            report.extras.append(extra)\n",
    )
    for label in ("alpha", "beta"):
        project.write(f"test_{label}.py", f"def test_{label}(): pass\n")
    reference = project.run_pytest(
        "-p",
        "pytest_html.plugin",
        "-p",
        "pytest_metadata.plugin",
        "--html=reference.html",
        "--self-contained-html",
        "-q",
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "HTML_EXTRAS": "pytest_html"},
    )
    actual = project.run_vfy(
        "test",
        "--engine=native",
        f"--workers={workers}",
        "--all",
        "--no-cov",
        "--html=actual.html",
        "--self-contained-html",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "HTML_EXTRAS": "vfy.pytest_html"},
    )
    assert actual.returncode == reference.returncode == 0, (
        reference.stdout + reference.stderr + actual.stdout + actual.stderr
    )
    oracle = (tmp_path / "reference.html").read_text()
    blob = re.search(r'data-jsonblob="([^"]+)"', oracle)
    assert blob is not None
    rows = json.loads(unescape(blob[1]))["tests"]
    expected = sorted(
        extra["content"]
        for reports in rows.values()
        for report in reports
        for extra in report["extras"]
        if extra["name"] == "late evidence"
    )
    document = (tmp_path / "actual.html").read_text()
    attachments = sorted(
        unescape(url) for url in re.findall(r'<a href="([^"]+)">late evidence</a>', document)
    )
    assert (
        attachments
        == expected
        == [
            "https://example.com/test_alpha",
            "https://example.com/test_beta",
        ]
    )
