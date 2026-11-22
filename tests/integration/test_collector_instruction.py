"""Large literal initialization preserves coverage and monitoring ownership."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


def literal_source() -> str:
    entries = [
        f"        {index}: "
        + (f"helper({index}, explode)" if index in (0, 1000, 1999) else str(index))
        + ",\n"
        for index in range(2000)
    ]
    return (
        "def helper(value, explode):\n"
        "    if explode and value == 1999:\n"
        "        raise ValueError('literal interrupted')\n"
        "    return value\n"
        "\n"
        "def fill(explode=False):\n"
        "    values = {\n" + "".join(entries) + "    }\n"
        "    return values\n"
    )


@pytest.mark.parametrize("branch", [False, True])
def test_large_literal_preserves_nested_calls_exceptions_and_contexts(
    tmp_path: Path, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("subject.py", literal_source())
    project.write(
        "collect.py",
        "import importlib, pathlib, sys\n"
        "from subject import fill\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "path = str(pathlib.Path('subject.py').resolve())\n"
        "coverage = Coverage(data_file='.' + sys.argv[1], include=[path],\n"
        f"                    branch={branch!r}, config_file=False)\n"
        "coverage.start()\n"
        "for context in ('first', 'second'):\n"
        "    coverage.switch_context(context)\n"
        "    for _ in range(2):\n"
        "        values = fill()\n"
        "        assert len(values) == 2000 and values[1999] == 1999\n"
        "    try:\n"
        "        fill(True)\n"
        "    except ValueError as error:\n"
        "        assert str(error) == 'literal interrupted'\n"
        "    else:\n"
        "        raise AssertionError('nested exception did not escape')\n"
        "    coverage.get_data()\n"
        "    coverage.save()\n"
        "coverage.stop()\n"
        "coverage.start()\n"
        "coverage.switch_context('restarted')\n"
        "assert len(fill()) == 2000\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
    actual = project.data(".vfy")
    expected = project.data(".coverage")
    try:
        assert actual.measured_files() == expected.measured_files() == {str(source)}
        assert actual.has_arcs() == expected.has_arcs() == branch
        assert actual.measured_contexts() == expected.measured_contexts()
        # Coverage.py builds context lists from sets, so their order varies by hash seed.
        actual_contexts = {
            line: sorted(contexts)
            for line, contexts in actual.contexts_by_lineno(str(source)).items()
        }
        expected_contexts = {
            line: sorted(contexts)
            for line, contexts in expected.contexts_by_lineno(str(source)).items()
        }
        assert actual_contexts == expected_contexts
        assert actual.lines(str(source)) == expected.lines(str(source))
        if branch:
            assert sorted(actual.arcs(str(source)) or []) == sorted(
                expected.arcs(str(source)) or []
            )
        for context in ("first", "second", "restarted"):
            actual.set_query_context(context)
            expected.set_query_context(context)
            assert actual.lines(str(source)) == expected.lines(str(source))
            assert actual.lines(str(source))
            if branch:
                assert sorted(actual.arcs(str(source)) or []) == sorted(
                    expected.arcs(str(source)) or []
                )
    finally:
        actual.close(force=True)
        expected.close(force=True)


@pytest.mark.parametrize("branch", [False, True])
def test_large_branching_literal_preserves_line_fallback_and_coverage(
    tmp_path: Path, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "subject.py",
        literal_source().replace(
            "def fill(explode=False):\n",
            "def fill(explode=False):\n    if explode:\n        return {}\n",
        ),
    )
    project.write(
        "collect.py",
        "import importlib, pathlib, sys\n"
        "from subject import fill\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "path = str(pathlib.Path('subject.py').resolve())\n"
        "coverage = Coverage(data_file='.' + sys.argv[1], include=[path],\n"
        f"                    branch={branch!r}, config_file=False)\n"
        "coverage.start()\n"
        "for context, early_return in (('early', True), ('full', False)):\n"
        "    coverage.switch_context(context)\n"
        "    values = fill(early_return)\n"
        "    assert len(values) == (0 if early_return else 2000)\n"
        "    if sys.argv[1] == 'vfy' and sys.version_info[:2] == (3, 12):\n"
        "        monitoring = sys.monitoring\n"
        "        mask = monitoring.get_local_events(monitoring.COVERAGE_ID, fill.__code__)\n"
        "        assert mask & monitoring.events.LINE\n"
        "        assert not mask & monitoring.events.INSTRUCTION\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
    actual = project.data(".vfy")
    expected = project.data(".coverage")
    try:
        assert actual.measured_files() == expected.measured_files() == {str(source)}
        assert actual.has_arcs() == expected.has_arcs() == branch
        assert actual.measured_contexts() == expected.measured_contexts() == {"early", "full"}
        for context in ("early", "full"):
            actual.set_query_context(context)
            expected.set_query_context(context)
            assert actual.lines(str(source)) == expected.lines(str(source))
            assert actual.lines(str(source))
            if branch:
                assert sorted(actual.arcs(str(source)) or []) == sorted(
                    expected.arcs(str(source)) or []
                )
    finally:
        actual.close(force=True)
        expected.close(force=True)


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="instruction local-event replacement applies to CPython 3.12",
)
def test_large_literal_releases_local_events_before_monitoring_slot_reuse(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("subject.py", literal_source())
    project.write(
        "ownership.py",
        "import sys\n"
        "from vfy import Coverage\n"
        "from subject import fill\n"
        "coverage = Coverage(data_file=None, include=['*/subject.py'], config_file=False)\n"
        "coverage.start()\n"
        "assert len(fill()) == 2000\n"
        "coverage.stop()\n"
        "monitoring = sys.monitoring\n"
        "slot = monitoring.COVERAGE_ID\n"
        "assert monitoring.get_tool(slot) is None\n"
        "monitoring.use_tool_id(slot, 'independent observer')\n"
        "seen = set()\n"
        "def observe(code, line):\n"
        "    if code is fill.__code__:\n"
        "        seen.add(line)\n"
        "try:\n"
        "    assert monitoring.get_local_events(slot, fill.__code__) == 0\n"
        "    monitoring.register_callback(slot, monitoring.events.LINE, observe)\n"
        "    monitoring.set_events(slot, monitoring.events.LINE)\n"
        "    monitoring.restart_events()\n"
        "    assert len(fill()) == 2000\n"
        "    assert len(seen) >= 2000\n"
        "finally:\n"
        "    monitoring.set_events(slot, 0)\n"
        "    monitoring.register_callback(slot, monitoring.events.LINE, None)\n"
        "    monitoring.free_tool_id(slot)\n"
        "coverage.start()\n"
        "assert len(fill()) == 2000\n"
        "coverage.stop()\n",
    )
    result = project.run_python("ownership.py")
    result.expect(0)
