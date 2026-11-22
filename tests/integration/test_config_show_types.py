"""``vfy config show`` prints each configured value with its declared JSON type."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject

_TOML = """
[tool.coverage.run]
branch = true

[tool.coverage.report]
precision = 2
fail_under = 90

[tool.coverage.xml]
package_depth = 3

[tool.vfy.test]
workers = {workers}

[tool.vfy.mutate]
workers = {workers}
timeout = 5

[tool.vfy.check]
fail_under_decisions = 80
"""

_INI = """
[coverage:run]
branch = true

[coverage:report]
precision = 2
fail_under = 90

[coverage:xml]
package_depth = 3
"""


def _show(root: Path) -> dict[str, Any]:
    result = VfyProject(root).run_vfy("config", "show")
    result.expect(0)
    return json.loads(result.stdout)["settings"]


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [("3", 3), ('"3"', 3), ('"auto"', "auto"), ('"logical"', "logical")],
)
def test_worker_counts_print_as_numbers_and_modes_as_strings(
    tmp_path: Path, spelling: str, expected: int | str
) -> None:
    (tmp_path / "pyproject.toml").write_text(_TOML.format(workers=spelling), encoding="utf-8")

    vfy = _show(tmp_path)["vfy"]

    for section in ("test", "mutate"):
        assert vfy[section]["workers"] == expected
        assert type(vfy[section]["workers"]) is type(expected)
    assert vfy["mutate"]["timeout"] == 5
    assert vfy["check"]["fail_under_decisions"] == 80


@pytest.mark.parametrize("name", ["pyproject.toml", "setup.cfg"])
def test_numeric_coverage_options_print_as_numbers(tmp_path: Path, name: str) -> None:
    text = _TOML.format(workers="2") if name == "pyproject.toml" else _INI
    (tmp_path / name).write_text(text, encoding="utf-8")

    settings = _show(tmp_path)

    assert settings["report"]["precision"] == 2
    assert type(settings["report"]["precision"]) is int
    assert settings["report"]["fail_under"] == 90
    assert settings["xml"]["package_depth"] == 3
    assert type(settings["xml"]["package_depth"]) is int
    assert settings["run"]["branch"] is True
