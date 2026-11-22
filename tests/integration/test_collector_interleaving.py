"""Real interpreter interleavings preserve collection and semantic evidence."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TypedDict

import pytest
from coverage import CoverageData

from tests.support import VfyProject
from vfy import Coverage


class _CoverageSnapshot(TypedDict):
    lines: set[int]
    arcs: set[tuple[int, int]]
    contexts_by_lineno: dict[int, set[str]]
    context_arcs: dict[str, set[tuple[int, int]]]


@pytest.mark.parametrize("mode", ["line", "branch", "semantic"])
def test_threads_preserve_interleaved_decisions(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "subject.py",
        "def pause(value, ready, proceed):\n"
        "    ready.set()\n"
        "    assert proceed.wait(10)\n"
        "    return value\n"
        "\n"
        "def choose(left, right, ready, proceed):\n"
        "    if left and pause(right, ready, proceed):\n"
        "        return 1\n"
        "    return 0\n",
    )
    driver = (
        "from threading import Event, Thread\n"
        "from subject import choose\n"
        "ready_a, ready_b, go_a, go_b = (Event() for _ in range(4))\n"
        "a = Thread(target=choose, args=(True, True, ready_a, go_a))\n"
        "b = Thread(target=choose, args=(True, False, ready_b, go_b))\n"
        "a.start()\n"
        "assert ready_a.wait(10)\n"
        "b.start()\n"
        "assert ready_b.wait(10)\n"
        "go_a.set()\n"
        "a.join(10)\n"
        "assert not a.is_alive()\n"
        "go_b.set()\n"
        "b.join(10)\n"
        "assert not b.is_alive()\n"
    )
    expected = {("11", True), ("10", False)}
    expression = "left and pause(right, ready, proceed)"
    assert_interleaving(
        project, source, driver, mode, [1, 2, 3, 4, 6, 7, 8, 9], expression, [7], expected
    )


@pytest.mark.parametrize("mode", ["line", "branch", "semantic"])
def test_generators_resume_out_of_suspension_order(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "subject.py",
        "def values(flag):\n"
        "    if flag:\n"
        "        yield 1\n"
        "    else:\n"
        "        yield 2\n"
        "    if flag:\n"
        "        yield 3\n"
        "    else:\n"
        "        yield 4\n",
    )
    driver = (
        "from subject import values\n"
        "a, b = values(True), values(False)\n"
        "assert next(a) == 1\n"
        "assert next(b) == 2\n"
        "assert next(a) == 3\n"
        "assert next(b) == 4\n"
        "assert list(a) == []\n"
        "assert list(b) == []\n"
    )
    expected = {("1", True), ("0", False)}
    expression = "flag"
    assert_interleaving(
        project, source, driver, mode, [1, 2, 3, 5, 6, 7, 9], expression, [2, 6], expected
    )


@pytest.mark.parametrize("mode", ["line", "branch", "semantic"])
def test_throw_resumes_the_correct_generator(tmp_path: Path, mode: str) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "subject.py",
        "def values(flag):\n"
        "    try:\n"
        "        if flag:\n"
        "            yield 1\n"
        "        else:\n"
        "            yield 2\n"
        "    except ValueError:\n"
        "        yield 3\n",
    )
    driver = (
        "from subject import values\n"
        "a, b = values(True), values(False)\n"
        "assert next(a) == 1\n"
        "assert next(b) == 2\n"
        "assert a.throw(ValueError()) == 3\n"
        "assert b.throw(ValueError()) == 3\n"
        "assert list(a) == []\n"
        "assert list(b) == []\n"
    )
    assert_interleaving(
        project,
        source,
        driver,
        mode,
        [1, 2, 3, 4, 6, 7, 8],
        "flag",
        [3],
        {("1", True), ("0", False)},
    )


@pytest.mark.parametrize("core", ["ctrace", "default"])
@pytest.mark.parametrize("mode", ["branch", "semantic"])
def test_async_generator_cleanup_preserves_only_source_mapped_self_arcs(
    tmp_path: Path, mode: str, core: str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "subject.py",
        "def decorate(function):\n"
        "    return function\n"
        "\n"
        "async def source_values(values):\n"
        "    for value in values:\n"
        "        yield value\n"
        "\n"
        "@decorate\n"
        "async def decorated(\n"
        "    values,\n"
        "):\n"
        "    if values:\n"
        "        async for value in source_values(values):\n"
        "            yield value\n"
        "\n"
        "async def plain(\n"
        "    values,\n"
        "):\n"
        "    if values:\n"
        "        async for value in source_values(values):\n"
        "            yield value\n"
        "\n"
        "def legitimate_self_arc():\n"
        "    count = 3\n"
        "    while count: count -= 1\n"
        "    return count\n",
    )
    project.write(
        "driver.py",
        "import asyncio\n"
        "from importlib import import_module\n"
        "import sys\n"
        "\n"
        "module, destination, mode, core = sys.argv[1:]\n"
        "options = {'branch': True, 'source': ['subject'], 'config_file': False}\n"
        "if module == 'vfy.api' and mode == 'semantic':\n"
        "    options['decisions'] = True\n"
        "coverage = import_module(module).Coverage(data_file=destination, **options)\n"
        "if core != 'default':\n"
        "    coverage.set_option('run:core', core)\n"
        "coverage.start()\n"
        "try:\n"
        "    import subject\n"
        "    async def collect(function, values):\n"
        "        return [value async for value in function(values)]\n"
        "    for name in ('decorated', 'plain'):\n"
        "        function = getattr(subject, name)\n"
        "        for suffix, values in (('nonempty', [1, 2]), ('empty', [])):\n"
        "            coverage.switch_context(name + '-' + suffix)\n"
        "            assert asyncio.run(collect(function, values)) == values\n"
        "    coverage.switch_context('real-loop')\n"
        "    assert subject.legitimate_self_arc() == 0\n"
        "finally:\n"
        "    coverage.stop()\n"
        "coverage.save()\n",
    )
    snapshots: dict[str, _CoverageSnapshot] = {}
    for module, destination in (
        ("coverage", "reference.coverage"),
        ("vfy.api", "actual.coverage"),
    ):
        result = project.run_python("driver.py", module, destination, mode, core)
        result.expect(0)
        data = CoverageData(basename=str(tmp_path / destination))
        try:
            data.read()
            lines = data.lines(str(source))
            arcs = data.arcs(str(source))
            assert lines is not None
            assert arcs is not None
            context_arcs: dict[str, set[tuple[int, int]]] = {}
            for context in sorted(data.measured_contexts()):
                data.set_query_context(context)
                context_arcs[context] = set(data.arcs(str(source)) or [])
            data.set_query_contexts(None)
            snapshots[module] = {
                "lines": set(lines),
                "arcs": set(arcs),
                "contexts_by_lineno": {
                    line: set(contexts)
                    for line, contexts in data.contexts_by_lineno(str(source)).items()
                },
                "context_arcs": context_arcs,
            }
        finally:
            data.close(force=True)
    expected = snapshots["coverage"]
    actual = snapshots["vfy.api"]
    assert (25, 25) in expected["arcs"]
    assert (25, 25) in actual["context_arcs"]["real-loop"]
    for context in ("decorated-nonempty", "decorated-empty", "plain-nonempty", "plain-empty"):
        assert (8, 8) not in actual["context_arcs"][context]
        assert (16, 16) not in actual["context_arcs"][context]
    if core == "ctrace" or sys.version_info < (3, 14):
        assert actual == expected
    else:
        # Coverage.py's sysmon core does not retain complete manual contexts.
        assert (actual["lines"], actual["arcs"]) == (expected["lines"], expected["arcs"])


def assert_interleaving(
    project: VfyProject,
    source: Path,
    driver: str,
    mode: str,
    expected_lines: list[int],
    expression: str,
    decision_lines: list[int],
    expected: set[tuple[str, bool]],
) -> None:
    project.write("driver.py", driver)
    branch = [] if mode == "line" else ["--branch"]
    actual = project.run_vfy(
        "coverage",
        "run",
        *branch,
        *(["--decisions"] if mode == "semantic" else []),
        "--source=.",
        "--data-file=actual.coverage",
        "driver.py",
    )
    reference = project.run_coverage_py(
        "run",
        *branch,
        "--source=.",
        "--data-file=reference.coverage",
        "driver.py",
    )
    assert (actual.returncode, actual.stderr) == (0, "")
    assert (reference.returncode, reference.stderr) == (0, "")
    assert project.lines(source, "reference.coverage") == expected_lines
    assert project.lines(source, "actual.coverage") == expected_lines
    if branch:
        assert project.arcs(source, "actual.coverage") == project.arcs(source, "reference.coverage")
    if mode == "semantic":
        measured = Coverage(data_file=str(source.parent / "actual.coverage"), config_file=False)
        measured.load()
        semantic = measured.get_decision_data()
        definitions = [item for item in semantic.definitions if item.expression == expression]
        assert [item.line for item in definitions] == decision_lines
        for definition in definitions:
            observations = {
                (item.vector, item.outcome)
                for item in semantic.observations
                if item.decision == definition.key
            }
            assert observations == expected
