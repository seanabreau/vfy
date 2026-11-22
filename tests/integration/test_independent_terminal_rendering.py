"""Native terminal text follows pytest's width, whitespace, and ANSI rules."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

if sys.platform == "win32":
    pytest.skip("native pytest requires Unix", allow_module_level=True)

_SOURCE = """import json
from io import StringIO
from pathlib import Path

from _pytest._io import TerminalWriter


class Flag:
    def __bool__(self):
        calls.append("bool")
        raise RuntimeError("flag failed")


calls = []


def outcome(function):
    calls.clear()
    try:
        value = function()
    except (RuntimeError, ValueError, ZeroDivisionError) as error:
        value = [type(error).__name__, str(error)]
    return [value, list(calls)]


def test_terminal_rendering():
    observed = {"separators": [], "markup": [], "errors": []}
    for separator in ["=", "- ", "*~", "λ", "🧪", "_ \\x1e", "\\u3000="]:
        for title in [None, "", "tests coverage", "宽 🎉", "a\\nb"]:
            for width in [-3, 0, 1, 2, 5, 12, 25, 80]:
                output = StringIO()
                writer = TerminalWriter(output)
                writer.hasmarkup = False
                writer.sep(separator, title, width)
                observed["separators"].append(output.getvalue())
    for title in [None, "coverage"]:
        observed["errors"].append(
            outcome(lambda: TerminalWriter(StringIO()).sep("", title, 80))
        )
    for enabled in [False, True]:
        writer = TerminalWriter(StringIO())
        writer.hasmarkup = enabled
        for markup in [
            {},
            {"red": True},
            {"bold": True, "green": True},
            {"green": True, "bold": True},
            {"Red": True, "light": True, "cyan": False},
            {"black": True, "white": True, "blink": True, "invert": True},
            {"Black": True, "White": True, "yellow": True, "Yellow": True},
            {"blue": True, "Blue": True, "purple": True, "Purple": True, "Cyan": True},
            {"Green": True, "red": False},
        ]:
            for text in ["", "coverage: 90%", "宽 🎉\\nnext line"]:
                observed["markup"].append(writer.markup(text, **markup))
        for name in ["unknown", "a'b", 'a"b', "bad\\nmarkup"]:
            observed["errors"].append(outcome(lambda: writer.markup("text", **{name: False})))
        for mode in ["inactive", "active", "invalid"]:
            flags = {"red": Flag()}
            if mode == "invalid":
                flags["unknown"] = False
            writer.hasmarkup = mode != "inactive"
            observed["errors"].append(outcome(lambda: writer.markup("text", **flags)))
    Path("observed.json").write_text(json.dumps(observed))
"""


def test_terminal_separators_and_markup_match_pytest(tmp_path: Path) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("test_terminal.py", _SOURCE)
        result = project.run_engine(route, "-q", "-p", "no:cacheprovider")
        result.expect(0)
        observations.append(json.loads((project.root / "observed.json").read_text()))
    assert observations[0] == observations[1]
    assert observations[0]["separators"]
