"""Instruction collection preserves another tool's concurrent LINE observations."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(
    sys.version_info[:2] != (3, 12),
    reason="instruction local-event replacement applies to CPython 3.12",
)
def test_instruction_collection_preserves_concurrent_line_observer(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    entries = "".join(f"        {index}: {index},\n" for index in range(2000))
    source = project.write(
        "subject.py", "def fill():\n    values = {\n" + entries + "    }\n    return values\n"
    )
    project.write(
        "observe.py",
        "import importlib, pathlib, sys\n"
        "from subject import fill\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "path = str(pathlib.Path('subject.py').resolve())\n"
        "monitoring = sys.monitoring\n"
        "slot = monitoring.DEBUGGER_ID\n"
        "monitoring.use_tool_id(slot, 'independent line observer')\n"
        "seen = []\n"
        "def observe(code, line):\n"
        "    if code is fill.__code__:\n"
        "        seen.append(line)\n"
        "coverage = None\n"
        "try:\n"
        "    monitoring.register_callback(slot, monitoring.events.LINE, observe)\n"
        "    monitoring.set_local_events(slot, fill.__code__, monitoring.events.LINE)\n"
        "    assert len(fill()) == 2000\n"
        "    expected = list(seen)\n"
        "    assert expected\n"
        "    coverage = Coverage(data_file='.' + sys.argv[1], include=[path],\n"
        "                        branch=True, config_file=False)\n"
        "    coverage.start()\n"
        "    for context in ('first', 'second'):\n"
        "        coverage.switch_context(context)\n"
        "        seen.clear()\n"
        "        assert len(fill()) == 2000\n"
        "        assert seen == expected\n"
        "        if sys.argv[1] == 'vfy':\n"
        "            mask = monitoring.get_local_events(monitoring.COVERAGE_ID, fill.__code__)\n"
        "            assert mask & monitoring.events.INSTRUCTION\n"
        "            assert not mask & monitoring.events.LINE\n"
        "    coverage.stop()\n"
        "    coverage.save()\n"
        "finally:\n"
        "    if coverage is not None:\n"
        "        coverage.stop()\n"
        "    monitoring.set_local_events(slot, fill.__code__, 0)\n"
        "    monitoring.register_callback(slot, monitoring.events.LINE, None)\n"
        "    monitoring.free_tool_id(slot)\n",
    )
    for tool in ("coverage", "vfy"):
        result = project.run_python("observe.py", tool, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
    actual = project.data(".vfy")
    expected = project.data(".coverage")
    try:
        assert actual.measured_files() == expected.measured_files() == {str(source)}
        assert actual.measured_contexts() == expected.measured_contexts()
        assert actual.contexts_by_lineno(str(source)) == expected.contexts_by_lineno(str(source))
        for context in ("first", "second"):
            actual.set_query_context(context)
            expected.set_query_context(context)
            assert actual.lines(str(source)) == expected.lines(str(source))
            assert sorted(actual.arcs(str(source)) or []) == sorted(
                expected.arcs(str(source)) or []
            )
    finally:
        actual.close(force=True)
        expected.close(force=True)
