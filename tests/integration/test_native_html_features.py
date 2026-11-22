"""Native HTML attachments survive serial execution and worker transport."""

from __future__ import annotations

import base64
import json
import re
import sys
from html import unescape
from pathlib import Path

import pytest

from tests.support import VfyProject

ISOLATED = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("self_contained", [False, True])
def test_native_attachments_match_pytest_html(
    tmp_path: Path, workers: int, self_contained: bool
) -> None:
    project = VfyProject(tmp_path)
    for label in ("alpha", "beta"):
        project.write(
            f"test_{label}.py",
            "from importlib import import_module\n"
            "import os\n"
            "html_extras = import_module(os.environ['HTML_EXTRAS']).extras\n"
            "def test_evidence(extras):\n"
            f"    extras.append(html_extras.text('{label} evidence', name='proof'))\n"
            f"    extras.append(html_extras.url('https://example.com/{label}', name='source'))\n"
            f"    extras.append(html_extras.html('<strong>{label} fragment</strong>'))\n",
        )
    project.write("test_other.py", "def test_other(): pass\n")
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
    blob = re.search(r'data-jsonblob="([^"]*)"', reference_document)
    assert blob is not None
    reference_evidence: dict[str, list[str]] = {}
    for nodeid, rows in json.loads(unescape(blob.group(1)))["tests"].items():
        assert len(rows) == 1
        assert rows[0]["result"] == "Passed"
        extras = rows[0]["extras"]
        if extras:
            assert [extra["name"] for extra in extras] == ["proof", "source", None]
            text = extras[0]["content"]
            if self_contained:
                text = base64.b64decode(text.partition(",")[2]).decode("utf-8")
            else:
                text = (tmp_path / text).read_text(encoding="utf-8")
            reference_evidence[nodeid] = [text, extras[1]["content"], extras[2]["content"]]
        else:
            reference_evidence[nodeid] = []
    assert reference_evidence == {
        "test_alpha.py::test_evidence": [
            "alpha evidence",
            "https://example.com/alpha",
            "<strong>alpha fragment</strong>",
        ],
        "test_beta.py::test_evidence": [
            "beta evidence",
            "https://example.com/beta",
            "<strong>beta fragment</strong>",
        ],
        "test_other.py::test_other": [],
    }

    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        f"--workers={workers}",
        "--no-cov",
        "--html=native.html",
        *options,
        env={**ISOLATED, "HTML_EXTRAS": "vfy.pytest_html"},
    )
    assert native.returncode == reference.returncode, native.stdout + native.stderr
    document = (tmp_path / "native.html").read_text(encoding="utf-8")
    assert "3 Passed," in document
    native_evidence: dict[str, list[str]] = {}
    for nodeid in reference_evidence:
        row = next(row for row in document.split("<tr>") if nodeid in row)
        attachments: list[str] = []
        if self_contained:
            text = re.search(r"<summary>proof</summary><pre>(.*?)</pre>", row)
            if text is not None:
                attachments.append(unescape(text.group(1)))
        else:
            asset = re.search(r'href="(assets/[^"]+\.txt)"', row)
            if asset is not None:
                attachments.append((tmp_path / asset.group(1)).read_text(encoding="utf-8"))
        url = re.search(r'<a href="([^"]+)">source</a>', row)
        if url is not None:
            attachments.append(unescape(url.group(1)))
        fragment = re.search(r"<strong>[^<]+</strong>", row)
        if fragment is not None:
            attachments.append(fragment.group(0))
        native_evidence[nodeid] = attachments
    assert native_evidence == reference_evidence


def test_native_extras_retain_fixture_teardown_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_teardown.py",
        "import pytest\n"
        "from vfy.pytest_html import extras as html_extras\n"
        "@pytest.fixture\n"
        "def attachment(extras):\n"
        "    extras.append(html_extras.text('setup proof'))\n"
        "    yield\n"
        "    extras.append(html_extras.text('teardown proof'))\n"
        "def test_attachment(attachment): pass\n",
    )
    options = ("--no-cov", "--self-contained-html")
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.pytest_plugin",
        "-p",
        "vfy.pytest_reporting",
        "--html=oracle.html",
        *options,
        env=ISOLATED,
    )
    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        "--workers=1",
        "--html=native.html",
        *options,
        env=ISOLATED,
    )
    assert native.returncode == reference.returncode == 0, native.stdout + native.stderr
    for name in ("oracle.html", "native.html"):
        document = (tmp_path / name).read_text(encoding="utf-8")
        assert "setup proof" in document
        assert "teardown proof" in document


def test_native_parallel_streaming_rejects_before_replacing_report(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("test_case.py", "def test_case(): pass\n")
    project.write("results.html", "accepted report")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--workers=2",
        "--no-cov",
        "--html=results.html",
        "-o",
        "generate_report_on_test=true",
        env=ISOLATED,
    )
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, result.stdout + result.stderr
    assert "generate_report_on_test requires --workers=1" in result.stderr
    assert (tmp_path / "results.html").read_text(encoding="utf-8") == "accepted report"


@pytest.mark.parametrize(("workers", "streaming"), [(1, False), (2, False), (1, True)])
def test_invalid_native_attachment_preserves_test_outcomes_and_previous_report(
    tmp_path: Path, workers: int, streaming: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "@pytest.fixture(autouse=True)\n"
        "def cleanup():\n"
        "    yield\n"
        "    with Path('finalized').open('a') as output:\n"
        "        output.write('finalized\\n')\n",
    )
    project.write(
        "test_a_invalid.py",
        "def test_invalid(extras):\n    extras.append({'kind': 'text', 'name': 'broken'})\n",
    )
    project.write(
        "test_b_valid.py",
        "from vfy.pytest_html import extras as html_extras\n"
        "def test_valid(extras):\n"
        "    extras.append(html_extras.text('later valid evidence'))\n",
    )
    options = (
        "--no-cov",
        "--html=results.html",
        "--self-contained-html",
        "-q",
        *(("-o", "generate_report_on_test=true") if streaming else ()),
    )
    report = tmp_path / "results.html"
    report.write_text("accepted report", encoding="utf-8")
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.pytest_plugin",
        "-p",
        "vfy.pytest_reporting",
        *options,
        env=ISOLATED,
    )
    reference.expect(0)
    assert "vfy failed to write HTML report" in reference.stdout
    assert "content" in reference.stdout
    assert report.read_text(encoding="utf-8") == "accepted report"
    assert (tmp_path / "finalized").read_text(encoding="utf-8").splitlines() == [
        "finalized",
        "finalized",
    ]
    (tmp_path / "finalized").unlink()

    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--all",
        f"--workers={workers}",
        *options,
        env=ISOLATED,
    )
    assert native.returncode == reference.returncode, native.stdout + native.stderr
    assert "2 passed" in native.stdout
    assert "vfy failed to write HTML report" in native.stdout
    assert "content" in native.stdout
    assert report.read_text(encoding="utf-8") == "accepted report"
    assert (tmp_path / "finalized").read_text(encoding="utf-8").splitlines() == [
        "finalized",
        "finalized",
    ]


def test_compact_final_attachment_error_survives_session_cleanup(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_compact.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "from vfy.pytest_html import extras as html_extras\n"
        "@pytest.fixture(scope='session')\n"
        "def session_state():\n"
        "    yield\n"
        "    Path('finalized').write_text('session finalized')\n"
        "@pytest.mark.parametrize('number', [0, 1])\n"
        "def test_attachment(number, extras, session_state):\n"
        "    if number == 1:\n"
        "        extras.append({'kind': 'text', 'name': 'broken'})\n"
        "    else:\n"
        "        extras.append(html_extras.text('valid evidence'))\n",
    )
    options = ("--no-cov", "--html=results.html", "--self-contained-html", "-q")
    report = tmp_path / "results.html"
    report.write_text("accepted report", encoding="utf-8")
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.pytest_plugin",
        "-p",
        "vfy.pytest_reporting",
        *options,
        env=ISOLATED,
    )
    reference.expect(0)
    assert "content" in reference.stdout
    assert report.read_text(encoding="utf-8") == "accepted report"
    assert (tmp_path / "finalized").read_text(encoding="utf-8") == "session finalized"
    (tmp_path / "finalized").unlink()
    native = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "--workers=1", *options, env=ISOLATED
    )
    assert native.returncode == reference.returncode, native.stdout + native.stderr
    assert "2 passed" in native.stdout
    assert "content" in native.stdout
    assert report.read_text(encoding="utf-8") == "accepted report"
    assert (tmp_path / "finalized").read_text(encoding="utf-8") == "session finalized"


def test_maxfail_session_cleanup_clears_native_attachment_snapshot(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_maxfail.py",
        "from pathlib import Path\n"
        "import pytest\n"
        "from vfy.pytest_html import extras as html_extras\n"
        "values = []\n"
        "@pytest.fixture(scope='session')\n"
        "def broad():\n"
        "    yield\n"
        "    values.clear()\n"
        "    Path('finalized').write_text('session finalized')\n"
        "@pytest.mark.parametrize('number', [0, 1])\n"
        "def test_attachment(number, broad, extras):\n"
        "    global values\n"
        "    values = extras\n"
        "    extras.append(html_extras.text('attachment evidence', name='unique-label'))\n"
        "    assert number < 0\n",
    )
    options = (
        "--no-cov",
        "--html=results.html",
        "--self-contained-html",
        "--maxfail=1",
        "-q",
    )
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "vfy.pytest_plugin",
        "-p",
        "vfy.pytest_reporting",
        *options,
        env=ISOLATED,
    )
    assert reference.returncode == pytest.ExitCode.TESTS_FAILED, reference.stdout + reference.stderr
    report = tmp_path / "results.html"
    assert "<summary>unique-label</summary>" not in report.read_text(encoding="utf-8")
    assert (tmp_path / "finalized").read_text(encoding="utf-8") == "session finalized"
    report.unlink()
    (tmp_path / "finalized").unlink()
    native = project.run_vfy(
        "test", "--engine=pytest-native", "--all", "--workers=1", *options, env=ISOLATED
    )
    assert native.returncode == reference.returncode, native.stdout + native.stderr
    assert "1 failed" in native.stdout
    assert "<summary>unique-label</summary>" not in report.read_text(encoding="utf-8")
    assert (tmp_path / "finalized").read_text(encoding="utf-8") == "session finalized"
