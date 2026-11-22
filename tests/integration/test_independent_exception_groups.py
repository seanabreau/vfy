"""Public group searches include notes and preserve pytest's depth policy."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_exception_group_search_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "test_groups.py",
        r"""import json
import re
from pathlib import Path

import pytest


def test_group_search():
    error = ValueError("parser failed")
    error.add_note("configuration: input.toml")
    nested = ExceptionGroup("nested", [error])
    nested.add_note("nested group note")
    with pytest.raises(ExceptionGroup) as info:
        raise ExceptionGroup("validation", [TypeError("outer"), nested])
    rows = {}

    def capture(label, operation):
        try:
            rows[label] = ["result", operation()]
        except BaseException as error:
            rows[label] = [type(error).__name__, str(error)]

    cases = [
        ("note", ValueError, {"match": "input.toml"}),
        ("nested_note", ValueError, {"match": re.compile("INPUT.TOML", re.I), "depth": 2}),
        ("wrong_depth", ValueError, {"match": "input.toml", "depth": 1}),
        ("outer", TypeError, {"depth": 1}),
        ("tuple", (TypeError, LookupError), {"depth": True}),
        ("group_message", ExceptionGroup, {"match": r"nested \(1 sub-exception\)"}),
        ("group_note", ExceptionGroup, {"match": "nested group note"}),
        ("absent", LookupError, {}),
        ("zero_depth", ValueError, {"depth": 0}),
        ("negative_depth", ValueError, {"depth": -1}),
        ("fractional_depth", ValueError, {"depth": 1.5}),
        ("invalid_depth", ValueError, {"depth": "nested"}),
        ("regex_error", ValueError, {"match": "["}),
        ("unused_regex", LookupError, {"match": "["}),
    ]
    for label, expected, options in cases:
        capture(label, lambda: info.group_contains(expected, **options))

    depth_events = []

    class Depth:
        def __ge__(self, other):
            depth_events.append(["ge", other])
            return True

        def __lt__(self, other):
            depth_events.append(["lt", other])
            raise LookupError("depth comparison")

    capture("depth_comparison", lambda: info.group_contains(ValueError, depth=Depth()))
    rows["depth_events"] = depth_events

    with pytest.raises(ValueError) as plain:
        raise ValueError("plain")
    capture("ordinary_exception", lambda: plain.group_contains(ValueError))

    with pytest.raises(BaseExceptionGroup) as base:
        raise BaseExceptionGroup("interruptions", [KeyboardInterrupt()])
    capture("base_exception", lambda: base.group_contains(KeyboardInterrupt))

    class BrokenNotes(ValueError):
        @property
        def __notes__(self):
            raise LookupError("notes unavailable")

    with pytest.raises(ExceptionGroup) as broken:
        raise ExceptionGroup("broken notes", [BrokenNotes("error")])
    capture("notes_unused", lambda: broken.group_contains(ValueError))
    capture("notes_error", lambda: broken.group_contains(ValueError, match="error"))
    Path("observations.json").write_text(json.dumps(rows))
""",
    )
    stock = project.run_python("-m", "pytest", "-q")
    observations = project.root / "observations.json"
    stock.expect(0)
    expected = json.loads(observations.read_text())
    observations.unlink()
    candidate = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    candidate.expect(0)
    assert json.loads(observations.read_text()) == expected
    assert expected["note"] == expected["nested_note"] == ["result", True]
    assert expected["group_message"] == expected["group_note"] == ["result", True]
    assert expected["wrong_depth"] == ["result", False]
    assert expected["zero_depth"] == ["AssertionError", "`depth` must be >= 1 if specified"]
    assert expected["notes_error"] == ["LookupError", "notes unavailable"]
    assert expected["depth_comparison"] == ["LookupError", "depth comparison"]
    assert expected["depth_events"] == [["ge", 1], ["lt", 1]]
