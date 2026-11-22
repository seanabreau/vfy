"""Parameter iterators retain pytest's public deprecation and filter behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

_OBSERVER = """import json
from pathlib import Path
seen = []
def pytest_warning_recorded(warning_message, when, nodeid):
    seen.append([warning_message.category.__name__, str(warning_message.message),
                 warning_message.filename, warning_message.lineno, when, nodeid])
def pytest_sessionfinish(session, exitstatus):
    Path('warnings.json').write_text(json.dumps(seen))
"""


@pytest.mark.parametrize("action", ["always", "ignore", "error"])
@pytest.mark.parametrize("collection", [False, True])
def test_parameter_iterator_warning_preserves_filters_and_consumption(
    tmp_path: Path, action: str, collection: bool
) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _OBSERVER)
        expression = "zip(values('left'), values('right'))"
        if collection:
            expression = f"list({expression})"
        project.write(
            "test_parameters.py",
            "from pathlib import Path\nimport pytest\n"
            "def values(label):\n"
            "    with Path('consumed').open('a') as stream:\n"
            "        stream.write(label + '\\n')\n"
            "    yield 1\n"
            f"@pytest.mark.parametrize(('left', 'right'), {expression})\n"
            "def test_values(left, right):\n    assert left == right == 1\n",
        )
        result = project.run_engine(
            engine, "-q", "-W", f"{action}::pytest.PytestRemovedIn10Warning:_pytest.python"
        )
        text = result.stdout + result.stderr
        rejected = action == "error" and not collection
        assert result.returncode == (2 if rejected else 0), text
        consumed = project.root / "consumed"
        assert (consumed.read_text() if consumed.exists() else None) == (
            None if rejected else "left\nright\n"
        )
        rows = json.loads((project.root / "warnings.json").read_text())
        if action == "always" and not collection:
            assert len(rows) == 1, (rows, text)
            category, message, filename, line, when, nodeid = rows[0]
            assert Path(filename).is_file(), rows
            assert 0 < line <= len(Path(filename).read_text().splitlines()), rows
            observed.append([category, message, when, nodeid])
        else:
            assert rows == [], (rows, text)
        if rejected:
            assert "PytestRemovedIn10Warning" in text, text
    if observed:
        expected = [
            "PytestRemovedIn10Warning",
            "Passing a non-Collection iterable to parametrize is deprecated.\n"
            "Test: test_parameters.py::test_values, argvalues type: zip\n"
            "Please convert to a list or tuple.\n"
            "See https://docs.pytest.org/en/stable/deprecations.html#parametrize-iterators",
            "collect",
            "",
        ]
        assert observed == [expected, expected]


@pytest.mark.parametrize("action", ["always", "ignore", "error"])
@pytest.mark.parametrize("module", ["_pytest.python", "conftest"])
def test_direct_parameter_warning_uses_the_project_callsite(
    tmp_path: Path, action: str, module: str
) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        source = (
            _OBSERVER + "def values():\n"
            "    Path('consumed').write_text('yes')\n"
            "    yield 1\n"
            "def pytest_generate_tests(metafunc):\n"
            "    metafunc.parametrize('value', values())\n"
            "    Path('continued').write_text('yes')\n"
        )
        project.write("conftest.py", source)
        project.write("test_parameters.py", "def test_value(value):\n    assert value == 1\n")
        result = project.run_engine(
            engine, "-q", "-W", f"{action}::pytest.PytestRemovedIn10Warning:{module}"
        )
        text = result.stdout + result.stderr
        rejected = action == "error" and module == "conftest"
        assert result.returncode == (2 if rejected else 0), text
        assert (project.root / "consumed").exists() is not rejected, text
        assert (project.root / "continued").exists() is not rejected, text
        rows = json.loads((project.root / "warnings.json").read_text())
        if rejected or (action == "ignore" and module == "conftest"):
            assert rows == [], (rows, text)
        else:
            assert len(rows) == 1, (rows, text)
            rows[0][2] = str(Path(rows[0][2]).relative_to(project.root))
            assert rows[0][2:4] == [
                "conftest.py",
                source.splitlines().index("    metafunc.parametrize('value', values())") + 1,
            ]
        observed.append(rows)
    assert observed[0] == observed[1]


@pytest.mark.parametrize("action", ["default", "module", "always"])
def test_direct_parameter_warning_deduplicates_at_the_shared_callsite(
    tmp_path: Path, action: str
) -> None:
    observed = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        source = (
            _OBSERVER + "def values():\n"
            "    yield 1\n"
            "def pytest_generate_tests(metafunc):\n"
            "    for name in ('first', 'second'):\n"
            "        metafunc.parametrize(name, values())\n"
        )
        project.write("conftest.py", source)
        project.write(
            "test_parameters.py",
            "def test_values(first, second):\n    assert first == second == 1\n",
        )
        result = project.run_engine(
            engine, "-q", "-W", f"{action}::pytest.PytestRemovedIn10Warning:conftest"
        )
        result.expect(0)
        rows = json.loads((project.root / "warnings.json").read_text())
        assert len(rows) == (2 if action == "always" else 1), rows
        for row in rows:
            row[2] = str(Path(row[2]).relative_to(project.root))
            assert row[2:4] == [
                "conftest.py",
                source.splitlines().index("        metafunc.parametrize(name, values())") + 1,
            ]
        observed.append(rows)
    assert observed[0] == observed[1]
