"""Native HTML preserves execution and collection evidence at the public CLI."""

from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path

import pytest

from tests.support import VfyProject

ISOLATED = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")


def _html_rows(path: Path) -> list[tuple[str, str, str]]:
    document = path.read_text(encoding="utf-8")
    return [
        (outcome, unescape(nodeid), unescape(details))
        for outcome, nodeid, details in re.findall(
            r'<tr><td class="(\w+)">\w+</td><td>(.*?)</td><td>.*?</td><td>(.*?)</td></tr>',
            document,
            re.DOTALL,
        )
    ]


@pytest.mark.skipif(
    int(pytest.version_tuple[0]) < 9, reason="pytest 9 owns unittest subtest reports"
)
@pytest.mark.parametrize("workers", ["1", "2"])
def test_native_html_retains_failing_unittest_subtest(tmp_path: Path, workers: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_subtest.py",
        "from unittest import TestCase\n\n"
        "class TestSubtest(TestCase):\n"
        "    def test_values(self):\n"
        "        with self.subTest(value='broken'):\n"
        "            self.fail('subtest assertion evidence')\n",
    )
    project.write("test_other.py", "def test_other(): pass\n")
    # Stock pytest observes a passing parent call plus a failed subtest.
    oracle = project.run_pytest(
        "-p",
        "pytest_html.plugin",
        "-p",
        "pytest_metadata.plugin",
        "--html=oracle.html",
        "-q",
        load_xdist=False,
        env=ISOLATED,
    )
    assert oracle.returncode == pytest.ExitCode.TESTS_FAILED, oracle.stdout + oracle.stderr
    assert "subtest assertion evidence" in oracle.stdout
    assert "SUBFAILED(value='broken')" in oracle.stdout
    oracle_document = (tmp_path / "oracle.html").read_text(encoding="utf-8")
    oracle_blob = re.search(r'data-jsonblob="([^"]*)"', oracle_document)
    assert oracle_blob is not None
    oracle_results = json.loads(unescape(oracle_blob.group(1)))["tests"]
    nodeid = "test_subtest.py::TestSubtest::test_values"
    assert [row["result"] for row in oracle_results[nodeid]] == ["Failed", "Passed"]
    assert "subtest assertion evidence" in oracle_results[nodeid][0]["log"]

    native = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        f"--workers={workers}",
        "--no-cov",
        "--html=results.html",
        "-q",
        env=ISOLATED,
    )
    assert native.returncode == oracle.returncode, native.stdout + native.stderr
    rows = _html_rows(tmp_path / "results.html")
    assert [(outcome, nodeid) for outcome, nodeid, _ in rows] == [
        ("Passed", "test_other.py::test_other"),
        ("Failed", "test_subtest.py::TestSubtest::test_values"),
    ]
    assert "subtest assertion evidence" in rows[1][2]
    assert "value='broken'" in rows[1][2]


@pytest.mark.parametrize("collection_outcome", ["skip", "error"])
def test_native_html_combines_collection_reports_and_executed_cases(
    tmp_path: Path, collection_outcome: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_runs.py", "def test_runs(): pass\n")
    project.write(
        "test_collection.py",
        "import pytest\npytest.skip('module skip evidence', allow_module_level=True)\n"
        if collection_outcome == "skip"
        else "raise RuntimeError('collection error evidence')\n",
    )
    arguments = ("--continue-on-collection-errors",) if collection_outcome == "error" else ()
    oracle = project.run_pytest("-q", *arguments, load_xdist=False, env=ISOLATED)
    expected_status = (
        pytest.ExitCode.TESTS_FAILED if collection_outcome == "error" else pytest.ExitCode.OK
    )
    assert oracle.returncode == expected_status, oracle.stdout + oracle.stderr
    assert "1 passed" in oracle.stdout
    assert ("1 error" if collection_outcome == "error" else "1 skipped") in oracle.stdout

    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--workers=1",
        "--no-cov",
        "--html=results.html",
        "-q",
        *arguments,
        env=ISOLATED,
    )
    assert native.returncode == oracle.returncode, native.stdout + native.stderr
    rows = _html_rows(tmp_path / "results.html")
    assert [(outcome, nodeid) for outcome, nodeid, _ in rows] == [
        ("Error" if collection_outcome == "error" else "Skipped", "test_collection.py"),
        ("Passed", "test_runs.py::test_runs"),
    ]
    assert (
        "collection error evidence" if collection_outcome == "error" else "module skip evidence"
    ) in rows[0][2]


def test_native_streaming_finalization_restores_attachment_and_refreshes_metadata(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n"
        "from vfy.pytest_metadata import metadata_key\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_sessionfinish(session):\n"
        "    session.config.stash[metadata_key]['Completed'] = 'yes'\n",
    )
    project.write(
        "test_streaming.py",
        "from pathlib import Path\n"
        "from vfy.pytest_html import extras as html_extras\n"
        "def test_attachment(extras):\n"
        "    extras.append(html_extras.text('retained attachment', name='proof'))\n"
        "def test_remove_streamed_asset():\n"
        "    report = Path('results.html').read_text()\n"
        "    assert 'test_streaming.py::test_attachment' in report\n"
        "    assert 'test_streaming.py::test_remove_streamed_asset' not in report\n"
        "    assets = list(Path('assets').glob('*.txt'))\n"
        "    assert len(assets) == 1\n"
        "    assert assets[0].read_text() == 'retained attachment'\n"
        "    assets[0].unlink()\n",
    )
    native = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--workers=1",
        "--no-cov",
        "--html=results.html",
        "-o",
        "generate_report_on_test=true",
        "-q",
        env=ISOLATED,
    )
    assert native.returncode == pytest.ExitCode.OK, native.stdout + native.stderr
    document = (tmp_path / "results.html").read_text(encoding="utf-8")
    assert "<th>Completed</th><td>yes</td>" in document
    assert [(outcome, nodeid) for outcome, nodeid, _ in _html_rows(tmp_path / "results.html")] == [
        ("Passed", "test_streaming.py::test_attachment"),
        ("Passed", "test_streaming.py::test_remove_streamed_asset"),
    ]
    assets = list((tmp_path / "assets").glob("*.txt"))
    assert [path.read_text(encoding="utf-8") for path in assets] == ["retained attachment"]
