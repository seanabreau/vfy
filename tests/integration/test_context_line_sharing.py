from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject


def _sorted_contexts(contexts: dict[int, list[str]]) -> dict[int, list[str]]:
    return {line: sorted(names) for line, names in contexts.items()}


def test_shared_context_line_sets_match_coverage_after_reentry_snapshot_and_restart(
    tmp_path: Path,
) -> None:
    """Contexts with equal line sets stay independent through every collection path."""
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def parse(value):\n"
        "    text = str(value)\n"
        "    if text.startswith('-'):\n"
        "        return -int(text[1:])\n"
        "    return int(text)\n"
        "\n"
        "def describe(value):\n"
        "    return f'value={value}'\n",
    )
    project.write(
        "run.py",
        "import importlib, sys\n"
        "import subject\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    data_file='.' + sys.argv[1], source=['.'], config_file=False)\n"
        "cov.start()\n"
        "for index in range(20):\n"
        "    cov.switch_context(f'test_parse[{index}]')\n"
        "    subject.parse(index)\n"
        "cov.switch_context('test_negative')\n"
        "subject.parse(-1)\n"
        "cov.get_data()\n"
        "cov.switch_context('test_parse[3]')\n"
        "subject.describe(3)\n"
        "cov.switch_context('test_parse[4]')\n"
        "subject.parse(4)\n"
        "cov.stop()\n"
        "cov.start()\n"
        "cov.switch_context('test_after_restart')\n"
        "subject.parse(5)\n"
        "cov.switch_context('test_parse[5]')\n"
        "subject.parse(-5)\n"
        "cov.stop()\n"
        "cov.save()\n",
    )

    # Coverage.py's sysmon core does not re-enable observed lines after a
    # context switch, so the oracle uses its per-context tracer.
    expected_run = project.run_python("run.py", "coverage", env={"COVERAGE_CORE": "ctrace"})
    actual_run = project.run_python("run.py", "vfy")

    expected_run.expect(0)
    actual_run.expect(0)
    actual, expected = project.data(".vfy"), project.data(".coverage")
    path = str(tmp_path / "subject.py")
    assert actual.measured_contexts() == expected.measured_contexts()
    # Coverage.py's reader collects each line's contexts in a set.
    assert _sorted_contexts(actual.contexts_by_lineno(path)) == _sorted_contexts(
        expected.contexts_by_lineno(path)
    )
    assert actual.contexts_by_lineno(path)[8] == ["test_parse[3]"]
    assert sorted(actual.contexts_by_lineno(path)[4]) == ["test_negative", "test_parse[5]"]
