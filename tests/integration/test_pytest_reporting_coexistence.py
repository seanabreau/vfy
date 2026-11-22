"""Coverage activation preserves existing pytest reporting plug-ins."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize("autoload", [True, False])
@pytest.mark.parametrize("html", [True, False])
def test_coverage_preserves_upstream_metadata_and_html(
    tmp_path: Path, autoload: bool, html: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def answer():\n    return 42\n")
    project.write(
        "conftest.py",
        "import pytest\n"
        "from pytest_metadata.plugin import metadata_key\n"
        "@pytest.hookimpl(trylast=True)\n"
        "def pytest_configure(config):\n"
        "    config.stash[metadata_key]['Build'] = 'local-build-proof'\n",
    )
    project.write(
        "test_app.py",
        "from app import answer\n"
        "def test_answer(metadata):\n"
        "    assert metadata['Build'] == 'local-build-proof'\n"
        "    assert answer() == 42\n",
    )
    plugins = [] if autoload else ["-p", "pytest_metadata.plugin"]
    if html and not autoload:
        plugins.extend(["-p", "pytest_html.plugin"])
    reports = ["--html=report.html", "--self-contained-html"] if html else []
    env = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None if autoload else "1"}
    for coverage in (False, True):
        args = [*plugins, *reports, "-q"]
        if coverage:
            args.extend(["-p", "no:pytest_cov", "-p", "vfy.pytest_plugin", "--cov=app"])
        result = project.run_pytest(*args, load_xdist=False, env=env)
        result.expect(0)
        assert "1 passed" in result.stdout
        if html:
            assert "local-build-proof" in (tmp_path / "report.html").read_text(encoding="utf-8")
    assert project.lines("app.py") == [1, 2]
