"""``Config.inicfg`` exposes the file settings and its deprecation as pytest 9.1 does."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_PROBE = """\
import json
import warnings
from pathlib import Path


def pytest_configure(config):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        view = config.inicfg
        observed = {
            "items": sorted(view.items()),
            "ordered_items": list(view.items()),
            "get_missing": view.get("missing", "fallback"),
            "contains": ["markers" in view, "missing" in view],
            "length": len(view),
        }
    observed["warnings"] = [
        [type(warning.message).__name__, str(warning.message), Path(warning.filename).name]
        for warning in caught
    ]
    Path("observed.json").write_text(json.dumps(observed))
"""


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        (
            "pytest.ini",
            "[pytest]\nminversion = 8.0\nmarkers =\n    slow: slow\n    fast: fast\n"
            "custom_unknown = value\n",
        ),
        (
            "pyproject.toml",
            '[tool.pytest]\nminversion = "8.0"\nmarkers = ["slow: slow", "fast: fast"]\n'
            "custom_unknown = 3\n",
        ),
    ],
    ids=["ini", "toml"],
)
def test_inicfg_matches_stock(tmp_path: Path, filename: str, contents: str) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(filename, contents)
        project.write("conftest.py", _PROBE)
        project.write("test_body.py", "def test_body():\n    pass\n")
        result = project.run_engine(engine, "-q", env={"PYTEST_ADDOPTS": None})
        result.expect(0)
        text = (project.root / "observed.json").read_text()
        observed.append(json.loads(text.replace(str(project.root), "<root>")))
    assert observed[0]["warnings"], "stock pytest must warn about config.inicfg"
    assert observed[1] == observed[0]


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("pytest.ini", "[pytest]\nzz = first\nalpha = second\n"),
        ("pyproject.toml", '[tool.pytest]\nzz = "first"\nalpha = "second"\n'),
    ],
)
def test_inicfg_iteration_preserves_file_and_override_order(
    tmp_path: Path, filename: str, contents: str
) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(filename, contents)
        project.write("conftest.py", _PROBE)
        project.write("test_body.py", "def test_body():\n    pass\n")
        result = project.run_engine(
            engine,
            "-q",
            "-o",
            "zz=updated",
            "-o",
            "middle=added",
            env={"PYTEST_ADDOPTS": None},
        )
        result.expect(0)
        observed.append(json.loads((project.root / "observed.json").read_text())["ordered_items"])
    assert observed[0] == [["zz", "updated"], ["alpha", "second"], ["middle", "added"]]
    assert observed[1] == observed[0]
