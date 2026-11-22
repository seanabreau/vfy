"""Hypothesis runs with pytest-html autoloaded match stock pytest."""

from __future__ import annotations

import json
import re
from importlib.metadata import entry_points
from pathlib import Path
from typing import cast

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_AUTOLOAD = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": None}
_TIMED_SUMMARY = re.compile(r" in \d+(?:\.\d+)?s\b")
_PLUGINS = (
    "import json\n"
    "from pathlib import Path\n"
    "\n"
    "def pytest_configure(config):\n"
    "    Path('plugins.json').write_text(json.dumps({\n"
    "        'html': config.pluginmanager.getplugin('html') is not None,\n"
    "        'hypothesis': config.pluginmanager.getplugin('hypothesispytest') is not None,\n"
    "    }))\n"
)


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("conftest.py", _PLUGINS)
    project.write(
        "test_properties.py",
        """from hypothesis import given, settings, strategies as st

@settings(max_examples=20, database=None, derandomize=True)
@given(st.integers(0, 9))
def test_passes(value):
    assert value >= 0

@settings(max_examples=20, database=None, derandomize=True)
@given(st.integers(0, 9))
def test_fails(value):
    assert value < 5
""",
    )
    return project


def _other_plugins() -> list[str]:
    names = {
        entry.name
        for entry in entry_points(group="pytest11")
        if entry.name not in {"html", "metadata", "hypothesispytest", "pytest_cov"}
    }
    return [argument for name in sorted(names) for argument in ("-p", f"no:{name}")]


def _summary(result: CommandResult) -> str:
    lines = [line for line in result.stdout.splitlines() if _TIMED_SUMMARY.search(line)]
    assert lines, result.stdout + result.stderr
    return _TIMED_SUMMARY.sub(" in <time>", lines[-1]).strip("= ")


def _loaded_plugins(project: VfyProject, result: CommandResult) -> dict[str, bool]:
    path = project.root / "plugins.json"
    assert path.is_file(), result.stdout + result.stderr
    loaded = cast("dict[str, bool]", json.loads(path.read_text(encoding="utf-8")))
    path.unlink()
    return loaded


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param([], id="default"),
        pytest.param(["--hypothesis-show-statistics"], id="statistics"),
        pytest.param(
            ["--html=report.html", "--self-contained-html"],
            id="html-report",
        ),
    ],
)
def test_hypothesis_with_autoloaded_pytest_html_matches_stock(
    tmp_path: Path, arguments: list[str]
) -> None:
    project = _project(tmp_path)
    options = [*_other_plugins(), *arguments]

    oracle = project.run_pytest("-q", *options, env=_AUTOLOAD, load_xdist=False)
    assert _loaded_plugins(project, oracle) == {"html": True, "hypothesis": True}, (
        oracle.stdout + oracle.stderr
    )

    actual = project.run_vfy("test", "--engine=native", "--no-cov", "-q", *options, env=_AUTOLOAD)
    assert _loaded_plugins(project, actual) == {"html": True, "hypothesis": True}, (
        actual.stdout + actual.stderr
    )

    assert "Hypothesis HTML statistics" not in actual.stdout + actual.stderr
    assert actual.returncode == oracle.returncode == 1, oracle.stdout + actual.stdout
    assert _summary(actual) == _summary(oracle) == "1 failed, 1 passed in <time>"
