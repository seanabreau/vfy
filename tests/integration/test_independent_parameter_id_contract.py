"""Parameter IDs agree with pytest at the public collection boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import CommandResult

pytestmark = requires_native_pytest


def _collect(project: VfyProject, engine: str) -> CommandResult:
    return project.run_engine(
        engine,
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )


def test_supported_and_callable_ids_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_ids.py",
        """import enum
import re
import pytest

class Choice(enum.Enum):
    VALUE = 1

def named(): pass

ids = [None, "é", b"\\xff", 3, 1.5, 2j, True, Choice.VALUE, re.compile("é"), named]
@pytest.mark.parametrize("value", list(range(len(ids))), ids=ids)
def test_explicit(value): pass

@pytest.mark.parametrize("value",
                         [3, object(), "é", b"\\xff", Choice.VALUE, re.compile("é"), named],
                         ids=lambda value: object() if type(value) in (int, object) else value)
def test_callable(value): pass

@pytest.fixture(params=[1, object()], ids=lambda value: object())
def resource(request): return request.param

def test_fixture(resource): pass
""",
    )
    stock = _collect(project, "stock")
    candidate = _collect(project, "independent")
    assert stock.returncode == candidate.returncode == 0, (
        stock.stdout,
        candidate.stdout,
        candidate.stderr,
    )
    expected = [line for line in stock.stdout.splitlines() if "::" in line]
    actual = [line for line in candidate.stdout.splitlines() if "::" in line]
    assert actual == expected
    assert "test_ids.py::test_callable[3]" in actual
    assert "test_ids.py::test_callable[value1]" in actual
    assert "test_ids.py::test_fixture[resource1]" in actual


@pytest.mark.parametrize(
    "definition",
    [
        "@pytest.mark.parametrize('value', [1], ids=[Invalid()])\ndef test_value(value): pass",
        "@pytest.fixture(params=[1], ids=[Invalid()])\n"
        "def value(request): return request.param\ndef test_value(value): pass",
    ],
)
def test_unsupported_explicit_id_is_collection_error(tmp_path: Path, definition: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_ids.py",
        "import pytest\nclass Invalid:\n    def __repr__(self): return 'invalid-id'\n"
        + definition
        + "\n",
    )
    stock = _collect(project, "stock")
    candidate = _collect(project, "independent")
    assert stock.returncode == candidate.returncode == 2, (
        stock.stdout,
        candidate.stdout,
        candidate.stderr,
    )
    for result in (stock, candidate):
        assert "ids contains unsupported value invalid-id" in result.stdout
        assert "at index 0" in result.stdout
