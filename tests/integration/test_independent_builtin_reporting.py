"""The owned HTML controller publishes only completed, validated worker results."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_worker_bootstrap_failure_preserves_the_previous_html_report(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    for name in ("first", "second"):
        project.write(f"test_{name}.py", f"def test_{name}():\n    assert True\n")
    arguments = ("--workers=2", "--no-cov", "--html=report.html", "-q")
    baseline = project.run_vfy("test", "--engine=native", *arguments)
    baseline.expect(0)
    report = tmp_path / "report.html"
    accepted = report.read_bytes()
    assert b"2 Passed," in accepted
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "def pytest_sessionstart(session):\n"
        "    # The coordinator starts first; every later process is a worker.\n"
        "    try:\n"
        "        Path('coordinator').touch(exist_ok=False)\n"
        "        return\n"
        "    except FileExistsError:\n"
        "        pass\n"
        "    Path('bootstrap-failed').touch()\n"
        "    raise RuntimeError('HTML worker bootstrap witness')\n",
    )

    failed = project.run_vfy("test", "--engine=native", *arguments)

    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert (tmp_path / "bootstrap-failed").is_file(), failed.stdout + failed.stderr
    assert "HTML worker bootstrap witness" in failed.stdout + failed.stderr
    assert report.read_bytes() == accepted


@pytest.mark.parametrize("workers", [1, 2])
def test_raw_owned_html_preserves_report_when_a_report_hook_fails(
    tmp_path: Path, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write("test_case.py", "def test_case(): pass\n")
    arguments = (
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.pytest_reporting",
        f"--workers={workers}",
        "--html=report.html",
        "-q",
    )
    baseline = project.run_vfy(*arguments)
    baseline.expect(0)
    report = tmp_path / "report.html"
    accepted = report.read_bytes()
    assert b"1 Passed," in accepted
    project.write(
        "conftest.py",
        "def pytest_html_report_title(report):\n"
        "    report.title = 'rejected publication'\n"
        "def pytest_runtest_logreport(report):\n"
        "    if report.when == 'call':\n"
        "        raise RuntimeError('HTML report hook witness')\n",
    )

    failed = project.run_vfy(*arguments)

    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert "HTML report hook witness" in failed.stdout + failed.stderr
    assert report.read_bytes() == accepted


def test_raw_owned_html_preserves_report_when_the_coordinator_hook_fails(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name in ("first", "second"):
        project.write(f"test_{name}.py", f"def test_{name}(): pass\n")
    arguments = (
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.pytest_reporting",
        "--workers=2",
        "--html=report.html",
        "-q",
    )
    baseline = project.run_vfy(*arguments)
    baseline.expect(0)
    report = tmp_path / "report.html"
    accepted = report.read_bytes()
    assert b"2 Passed," in accepted
    project.write(
        "conftest.py",
        "import pytest\n"
        "from pathlib import Path\n"
        "COORDINATOR = []\n"
        "def pytest_sessionstart(session):\n"
        "    try:\n"
        "        Path('coordinator').touch(exist_ok=False)\n"
        "        COORDINATOR.append(True)\n"
        "    except FileExistsError:\n"
        "        pass\n"
        "def pytest_html_report_title(report):\n"
        "    report.title = 'rejected publication'\n"
        "@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtestloop(session):\n"
        "    result = yield\n"
        "    if COORDINATOR:\n"
        "        raise RuntimeError('HTML coordinator hook witness')\n"
        "    return result\n"
        "def pytest_sessionfinish(session):\n"
        "    session.exitstatus = 0\n",
    )

    failed = project.run_vfy(*arguments)

    failed.expect(0)
    assert "HTML coordinator hook witness" in failed.stdout + failed.stderr
    assert report.read_bytes() == accepted


@pytest.mark.parametrize("blocked", ["html", "vfy.pytest_reporting"])
@pytest.mark.parametrize("reenable", [False, True])
def test_owned_html_plugin_identity_respects_explicit_activation_order(
    tmp_path: Path, blocked: str, reenable: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_plugin.py",
        "def test_plugin(pytestconfig):\n"
        "    plugin = pytestconfig.pluginmanager.getplugin('html')\n"
        f"    assert (plugin is not None) is {reenable!r}\n"
        "    if plugin is not None:\n"
        "        assert plugin.extras.text('attachment')['content'] == 'attachment'\n",
    )
    oracle = project.run_python(
        "-m",
        "pytest",
        "-p",
        "no:html" if reenable else "html",
        "-p",
        "html" if reenable else "no:html",
        "-q",
    )
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        f"no:{blocked}" if reenable else "vfy.pytest_reporting",
        "-p",
        "vfy.pytest_reporting" if reenable else f"no:{blocked}",
        "-q",
    )

    assert actual.returncode == oracle.returncode == 0, (
        oracle.stdout + oracle.stderr + actual.stdout + actual.stderr
    )


def test_owned_html_report_includes_extras_and_metadata(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_html.py",
        "from vfy.pytest_html import extras as html_extras\n"
        "def test_html(extras, metadata):\n"
        "    extras.append(html_extras.text('owned attachment'))\n"
        "    metadata['Lifecycle'] = 'recorded'\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        "vfy.pytest_reporting",
        "--html=owned.html",
        "--self-contained-html",
        "-q",
    )
    result.expect(0)
    document = (tmp_path / "owned.html").read_text()
    assert "1 Passed," in document
    assert "owned attachment" in document
    assert "<th>Lifecycle</th><td>recorded</td>" in document
