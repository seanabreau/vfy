"""Optional HTML transport leaves unrelated report extras uninterpreted."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.platform == "win32", reason="native workers require Unix")
@pytest.mark.parametrize("engine", ["pytest", "native"])
@pytest.mark.parametrize("workers", [1, 2])
def test_reporting_without_html_preserves_noniterable_report_extras_and_fixtures(
    tmp_path: Path, engine: str, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport():\n"
        "    report = (yield).get_result()\n"
        "    report.extras = 17\n",
    )
    for label in ("first", "second"):
        project.write(
            f"test_{label}.py",
            "import os\n"
            "from importlib import import_module\n"
            "html_module = import_module(os.environ['HTML_EXTRAS'])\n"
            "html_extras = getattr(html_module, 'extras', html_module)\n"
            "def test_fixtures(extras, metadata):\n"
            "    assert metadata['Build'] == 'fixture proof'\n"
            "    metadata['Runtime'] = __name__\n"
            "    extras.append(html_extras.text('unused attachment'))\n"
            "    assert extras[0]['content'] == 'unused attachment'\n",
        )
    arguments = ("-q", "--metadata", "Build", "fixture proof")
    reference = project.run_pytest(
        "-p",
        "pytest_html.fixtures",
        "-p",
        "pytest_metadata.plugin",
        *arguments,
        load_xdist=False,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "HTML_EXTRAS": "pytest_html.extras"},
    )
    actual = project.run_vfy(
        "test",
        f"--engine={engine}",
        f"--workers={workers}",
        "--all",
        "--no-cov",
        *arguments,
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "HTML_EXTRAS": "vfy.pytest_html"},
    )
    assert actual.returncode == reference.returncode == 0, (
        reference.stdout + reference.stderr + actual.stdout + actual.stderr
    )
    assert "2 passed" in reference.stdout
    assert "2 passed" in actual.stdout
