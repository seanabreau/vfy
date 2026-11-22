"""Assertion rewriting preserves source locations for original expressions."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_multiline_assertion_expression_locations_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_locations.py",
        """import json
from pathlib import Path
import traceback


def walrus():
    value = 3
    assert value == (
        value := 1 / 0
    )


def comprehension():
    assert [
        1 / number
        for number in [0]
    ]


def message():
    assert False, (
        1 / 0
    )


def test_locations():
    observations = []
    for operation in (walrus, comprehension, message):
        try:
            operation()
        except ZeroDivisionError as error:
            frames = traceback.extract_tb(error.__traceback__)
            observations.append([
                operation.__name__,
                [[frame.name, frame.lineno, frame.end_lineno, frame.colno, frame.end_colno]
                 for frame in frames if frame.name != "test_locations"],
            ])
    Path("locations.json").write_text(json.dumps(observations))
""",
    )

    observations = []
    for candidate in (False, True):
        result = (
            project.run_vfy("test", "--engine=native", "--no-cov", "-q")
            if candidate
            else project.run_python("-m", "pytest", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        path = tmp_path / "locations.json"
        observations.append(json.loads(path.read_text()))
        path.unlink()

    assert [row[0] for row in observations[0]] == ["walrus", "comprehension", "message"]
    assert observations[1] == observations[0]
