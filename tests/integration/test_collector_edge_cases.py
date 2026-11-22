"""Collector behavior at branch-event, file-tracer, cover_pylib, and encoding edges."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.skipif(sys.version_info < (3, 14), reason="direct branch events require CPython 3.14")
def test_direct_branch_event_retirement_survives_calls_recursion_suspension_and_reset(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def recurse(depth):\n"
        "    if depth:\n"
        "        return recurse(depth - 1) + 1\n"
        "    return 0\n"
        "\n"
        "def values():\n"
        "    yield 1\n"
        "    yield 2\n",
    )
    project.write(
        "collect.py",
        "import json\n"
        "from vfy import Coverage\n"
        "import subject\n"
        "cov = Coverage(branch=True, source=['.'], data_file=None, config_file=False)\n"
        "cov.start()\n"
        "for context in ('first', 'second'):\n"
        "    cov.switch_context(context)\n"
        "    for _ in range(10):\n"
        "        assert subject.recurse(4) == 4\n"
        "    generator = subject.values()\n"
        "    assert next(generator) == 1\n"
        "    cov.get_data()\n"
        "    assert next(generator) == 2\n"
        "cov.erase()\n"
        "cov.switch_context('after-reset')\n"
        "for _ in range(10):\n"
        "    assert subject.recurse(4) == 4\n"
        "generator = subject.values()\n"
        "assert next(generator) == 1\n"
        "cov.get_data()\n"
        "assert next(generator) == 2\n"
        "data = cov.get_data()\n"
        "path = str(__import__('pathlib').Path('subject.py').resolve())\n"
        "contexts = {str(line): sorted(values) for line, values in "
        "data.contexts_by_lineno(path).items()}\n"
        "print(json.dumps([sorted(data.lines(path) or []), "
        "sorted(data.arcs(path) or []), contexts]))\n"
        "cov.stop()\n",
    )

    result = project.run_python("collect.py")
    result.expect(0)
    lines, arcs, contexts = json.loads(result.stdout)
    assert lines == [2, 3, 4, 7, 8]
    assert arcs == [
        [2, 2],
        [2, 3],
        [2, 4],
        [3, -1],
        [3, 3],
        [4, -1],
        [4, 4],
        [7, 7],
        [8, 8],
    ]
    assert contexts == {str(line): ["after-reset"] for line in lines}


@pytest.mark.skipif(sys.version_info < (3, 14), reason="direct branch events require CPython 3.14")
def test_direct_branch_retirement_attributes_exhausted_suspensions_across_context_and_reset(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def values():\n"
        "    yield 1\n"
        "    yield 2\n"
        "\n"
        "async def calculate(pause):\n"
        "    value = 1\n"
        "    await pause\n"
        "    return value + 1\n",
    )
    project.write(
        "collect.py",
        "import json\n"
        "from pathlib import Path\n"
        "from vfy import Coverage\n"
        "import subject\n"
        "class Pause:\n"
        "    def __await__(self):\n"
        "        yield None\n"
        "def signature(cov):\n"
        "    data = cov.get_data()\n"
        "    path = str(Path('subject.py').resolve())\n"
        "    contexts = {str(line): sorted(values) for line, values in "
        "data.contexts_by_lineno(path).items()}\n"
        "    return [sorted(data.lines(path) or []), sorted(data.arcs(path) or []), contexts]\n"
        "cov = Coverage(branch=True, source=['subject.py'], data_file=None, config_file=False)\n"
        "cov.start()\n"
        "cov.switch_context('generator-before')\n"
        "generator = subject.values()\n"
        "assert next(generator) == 1\n"
        "cov.switch_context('generator-after')\n"
        "assert next(generator) == 2\n"
        "try:\n"
        "    next(generator)\n"
        "except StopIteration:\n"
        "    pass\n"
        "cov.switch_context('coroutine-before')\n"
        "coroutine = subject.calculate(Pause())\n"
        "assert coroutine.send(None) is None\n"
        "cov.switch_context('coroutine-after')\n"
        "try:\n"
        "    coroutine.send(None)\n"
        "except StopIteration as stop:\n"
        "    assert stop.value == 2\n"
        "before_reset = signature(cov)\n"
        "cov.switch_context('reset-before')\n"
        "generator = subject.values()\n"
        "assert next(generator) == 1\n"
        "cov.erase()\n"
        "cov.switch_context('reset-after')\n"
        "assert next(generator) == 2\n"
        "try:\n"
        "    next(generator)\n"
        "except StopIteration:\n"
        "    pass\n"
        "after_reset = signature(cov)\n"
        "cov.stop()\n"
        "print(json.dumps([before_reset, after_reset]))\n",
    )

    result = project.run_python("collect.py")
    result.expect(0)
    before_reset, after_reset = json.loads(result.stdout)
    assert before_reset == [
        [2, 3, 6, 7, 8],
        [[2, 2], [3, -1], [3, 3], [6, 6], [7, 7], [8, -5], [8, 8]],
        {
            "2": ["generator-before"],
            "3": ["generator-after"],
            "6": ["coroutine-before"],
            "7": ["coroutine-before"],
            "8": ["coroutine-after"],
        },
    ]
    assert after_reset == [
        [3],
        [[3, -1], [3, 3]],
        {"3": ["reset-after"]},
    ]


@pytest.mark.parametrize("generator", [False, True])
def test_excluded_callers_preserve_contexts_and_can_be_measured_later(
    tmp_path: Path, generator: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def value(flag):\n"
        "    if flag == -1:\n        raise ValueError('excluded caller')\n"
        "    if flag:\n        return 1\n    return 0\n",
    )
    caller = (
        "from subject import value\n"
        "def invoke(flag):\n"
        "    if flag:\n        return value(flag)\n"
        "    return value(False)\n"
    )
    if generator:
        caller = (
            "from subject import value\n"
            "def values(flag):\n"
            "    yield value(flag)\n"
            "    yield value(False)\n"
            "def invoke(flag):\n"
            "    return sum(values(flag))\n"
        )
    project.write("caller.py", caller)
    project.write(
        "collect.py",
        "import importlib, pathlib, sys\nfrom caller import invoke\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "for source in ('subject', 'caller'):\n"
        "    cov = Coverage(data_file='.' + sys.argv[1] + '-' + source,\n"
        "                   include=[str(pathlib.Path(source + '.py').resolve())],\n"
        "                   branch=True, config_file=False)\n"
        "    cov.start()\n"
        "    for context in ('first', 'second'):\n"
        "        cov.switch_context(context)\n"
        "        for _ in range(3):\n"
        "            assert invoke(True) == 1\n"
        "            assert invoke(False) == 0\n"
        "            try:\n                invoke(-1)\n"
        "            except ValueError:\n                pass\n"
        "    cov.stop()\n    cov.save()\n",
    )
    for tool in ("coverage", "vfy"):
        result = project.run_python("collect.py", tool)
        result.expect(0)
    for source in ("subject", "caller"):
        actual = project.data(f".vfy-{source}")
        expected = project.data(f".coverage-{source}")
        assert actual.measured_files() == expected.measured_files()
        path = str(tmp_path / f"{source}.py")
        # Coverage.py's sysmon core does not re-enable already observed events
        # on a context switch. Each vfy context must retain the full run.
        expected_arcs = sorted(expected.arcs(path) or [])
        assert expected_arcs
        for context in ("first", "second"):
            actual.set_query_context(context)
            assert sorted(actual.arcs(path) or []) == expected_arcs


@pytest.mark.parametrize(
    "source",
    [
        "def gen(flag):\n"
        "    if flag:\n"
        "        yield 1\n"
        "    else:\n"
        "        yield 2\n"
        "    yield 3\n"
        "a = gen(True)\n"
        "b = gen(False)\n"
        "next(a)\n"
        "next(b)\n"
        "next(a)\n",
        "def gen(flag):\n"
        "    try:\n"
        "        if flag:\n"
        "            yield (\n"
        "                1\n"
        "            )\n"
        "        else:\n"
        "            yield 2\n"
        "    except ValueError:\n"
        "        yield 3\n"
        "a = gen(True)\n"
        "b = gen(False)\n"
        "next(a)\n"
        "next(b)\n"
        "a.throw(ValueError)\n",
        "class Pause:\n"
        "    def __await__(self):\n"
        "        yield\n"
        "async def work(flag):\n"
        "    if flag:\n"
        "        await Pause()\n"
        "    else:\n"
        "        await Pause()\n"
        "    return 3\n"
        "a = work(True)\n"
        "b = work(False)\n"
        "a.send(None)\n"
        "b.send(None)\n"
        "try:\n"
        "    a.send(None)\n"
        "except StopIteration:\n"
        "    pass\n",
        "def child():\n"
        "    yield 1\n"
        "def gen(flag):\n"
        "    if flag:\n"
        "        yield from child()\n"
        "    else:\n"
        "        yield from child()\n"
        "    yield 3\n"
        "a = gen(True)\n"
        "b = gen(False)\n"
        "next(a)\n"
        "next(b)\n"
        "next(a)\n",
    ],
    ids=["generator", "throw-multiline", "coroutine", "yield-from"],
)
def test_non_lifo_suspensions_preserve_branch_arcs(tmp_path: Path, source: str) -> None:
    project = VfyProject(tmp_path)
    project.write("sample.py", source)
    project.write(
        "collect.py",
        "import importlib, pathlib, sys\n"
        "source = pathlib.Path('sample.py').resolve()\n"
        "code = compile(source.read_text(), str(source), 'exec')\n"
        "coverage = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    data_file=sys.argv[2], config_file=False, branch=True, include=[str(source)])\n"
        "scope = {}\n"
        "coverage.start()\n"
        "exec(code, scope)\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )
    for package in ("coverage", "vfy"):
        result = project.run_python("collect.py", package, f".{package}")
        result.expect(0)
    assert project.arcs("sample.py", ".vfy") == project.arcs("sample.py", ".coverage")


@pytest.mark.parametrize("decisions", [False, True])
def test_branch_arcs_match_generator_iteration_close_and_explicit_exit(
    tmp_path: Path,
    decisions: bool,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "sample.py",
        "def short_circuit():\n"
        "    return any(item for item in [0, 1])\n"
        "\n"
        "def one_item():\n"
        "    return any(item for item in [1])\n"
        "\n"
        "def empty():\n"
        "    return any(item for item in [])\n"
        "\n"
        "def one_line_for():\n"
        "    for value in (0, 1): total = value\n"
        "    return total\n"
        "\n"
        "def one_line_while():\n"
        "    count = 0\n"
        "    while count < 2: count += 1\n"
        "    return count\n"
        "\n"
        "def closed_values():\n"
        "    yield 1\n"
        "    yield 2\n"
        "\n"
        "def exhausted_values():\n"
        "    yield 1\n"
        "\n"
        "def explicit_generator_exit():\n"
        "    raise GeneratorExit\n"
        "\n"
        "def explicit_value_error():\n"
        "    raise ValueError\n"
        "\n"
        "def closed_with_finally(log):\n"
        "    try:\n"
        "        yield 1\n"
        "    finally:\n"
        "        log.append('closed')\n"
        "\n"
        "def catches_and_returns():\n"
        "    try:\n"
        "        yield 1\n"
        "    except GeneratorExit:\n"
        "        return\n"
        "\n"
        "def catches_and_yields():\n"
        "    try:\n"
        "        yield 1\n"
        "    except GeneratorExit:\n"
        "        yield 2\n"
        "\n"
        "assert short_circuit()\n"
        "assert one_item()\n"
        "assert not empty()\n"
        "assert one_line_for() == 1\n"
        "assert one_line_while() == 2\n"
        "generator = closed_values()\n"
        "assert next(generator) == 1\n"
        "generator.close()\n"
        "assert list(exhausted_values()) == [1]\n"
        "try:\n"
        "    explicit_generator_exit()\n"
        "except GeneratorExit:\n"
        "    pass\n"
        "try:\n"
        "    explicit_value_error()\n"
        "except ValueError:\n"
        "    pass\n"
        "log = []\n"
        "first = closed_with_finally(log)\n"
        "second = closed_with_finally(log)\n"
        "assert next(first) == 1\n"
        "assert next(second) == 1\n"
        "first.close()\n"
        "second.close()\n"
        "returned = catches_and_returns()\n"
        "assert next(returned) == 1\n"
        "returned.close()\n"
        "yielded = catches_and_yields()\n"
        "assert next(yielded) == 1\n"
        "try:\n"
        "    yielded.close()\n"
        "except RuntimeError:\n"
        "    pass\n"
        "assert log == ['closed', 'closed']\n",
    )
    reference = project.run_coverage_py("run", "--branch", "--data-file=.coverage", "sample.py")
    reference.expect(0)

    actual_arguments = ["coverage", "run", "--branch", "--data-file=.vfy"]
    if decisions:
        actual_arguments.append("--decisions")
    actual_arguments.append("sample.py")
    collected = project.run_vfy(*actual_arguments)
    collected.expect(0)

    expected = project.arcs("sample.py", ".coverage")
    actual = project.arcs("sample.py", ".vfy")
    assert actual == expected
    assert expected is not None
    assert (2, 2) in expected
    if sys.version_info < (3, 14):
        # The trace backend needs a second loop iteration before it records
        # this edge; the 3.14 monitoring backend reports the first iteration.
        assert (5, 5) not in expected
        assert (8, 8) not in expected
    assert (11, 11) in expected
    assert (16, 16) in expected


def test_legacy_branch_event_retirement_matches_ctrace_across_flush_and_reset(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def loop(values):\n"
        "    for value in values: last = value\n"
        "    return len(values)\n"
        "\n"
        "def comprehension(values):\n"
        "    return [value for value in values]\n",
    )
    project.write(
        "collect.py",
        "import importlib, json, pathlib, sys\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "import subject\n"
        "path = str(pathlib.Path('subject.py').resolve())\n"
        "coverage = Coverage(branch=True, include=[path], data_file=sys.argv[2], "
        "config_file=False)\n"
        "def arcs_for(contexts):\n"
        "    data = coverage.get_data()\n"
        "    result = {}\n"
        "    for context in contexts:\n"
        "        data.set_query_context(context)\n"
        "        result[context] = sorted(data.arcs(path) or [])\n"
        "    return result\n"
        "def exercise(context):\n"
        "    coverage.switch_context(context)\n"
        "    assert subject.loop([]) == 0\n"
        "    assert subject.comprehension([]) == []\n"
        "    coverage.get_data()\n"
        "    coverage.save()\n"
        "    empty = arcs_for((context,))[context]\n"
        "    assert subject.loop([]) == 0\n"
        "    assert subject.comprehension([]) == []\n"
        "    assert subject.loop([0, 1]) == 2\n"
        "    assert subject.comprehension([0, 1]) == [0, 1]\n"
        "    coverage.get_data()\n"
        "    coverage.save()\n"
        "    return empty\n"
        "coverage.start()\n"
        "empty = {name: exercise(name) for name in ('first', 'second')}\n"
        "coverage.stop()\n"
        "before_reset = arcs_for(('first', 'second'))\n"
        "coverage.erase()\n"
        "coverage.start()\n"
        "exercise('after-reset')\n"
        "coverage.stop()\n"
        "after_reset = arcs_for(('after-reset',))\n"
        "print(json.dumps([empty, before_reset, after_reset]))\n",
    )

    results = []
    for implementation, data_file in (("coverage", ".coverage"), ("vfy", ".vfy")):
        result = project.run_python(
            "collect.py",
            implementation,
            data_file,
            env={"COVERAGE_CORE": "ctrace"},
        )
        result.expect(0)
        results.append(json.loads(result.stdout))

    expected_empty, expected_before_reset, expected_after_reset = results[0]
    actual_empty, actual_before_reset, actual_after_reset = results[1]
    assert (actual_empty, actual_before_reset, actual_after_reset) == (
        expected_empty,
        expected_before_reset,
        expected_after_reset,
    )
    assert [2, 3] in expected_empty["first"]
    assert [2, 2] not in expected_empty["first"]
    assert [2, 2] in expected_before_reset["first"]
    assert [6, 6] in expected_before_reset["first"]
    assert expected_before_reset["first"] == expected_before_reset["second"]
    assert expected_before_reset["first"] == expected_after_reset["after-reset"]


@pytest.mark.parametrize("mapped_line", [100, -1])
def test_file_tracer_maps_repeated_branch_arc_to_virtual_line(
    tmp_path: Path, mapped_line: int
) -> None:
    project = VfyProject(tmp_path)
    virtual = project.write("generated.py", "\n" * 99 + "value = 1\n")
    project.write(
        "template.tpl",
        "def count():\n"
        "    for item in (0, 1): result = item\n"
        "    return result\n"
        "assert count() == 1\n",
    )
    project.write(
        "collect.py",
        "import importlib, pathlib, sys\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "FileTracer = importlib.import_module(sys.argv[1] + '.plugin').FileTracer\n"
        "CoveragePlugin = importlib.import_module(sys.argv[1] + '.plugin').CoveragePlugin\n"
        "executed, virtual = map(lambda name: str(pathlib.Path(name).resolve()), "
        "('template.tpl', 'generated.py'))\n"
        "class Tracer(FileTracer):\n"
        "    def source_filename(self):\n"
        "        return virtual\n"
        "    def line_number_range(self, frame):\n"
        "        line = int(sys.argv[3]) if frame.f_lineno == 2 else 100\n"
        "        return line, line\n"
        "class Plugin(CoveragePlugin):\n"
        "    def file_tracer(self, filename):\n"
        "        return Tracer() if filename == executed else None\n"
        "def initialize(registry):\n"
        "    registry.add_file_tracer(Plugin())\n"
        "coverage = tool.Coverage(data_file=sys.argv[2], branch=True, "
        "config_file=False, plugins=[initialize])\n"
        "coverage.start()\n"
        "exec(compile(pathlib.Path(executed).read_text(), executed, 'exec'), {})\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )
    reference = project.run_python(
        "collect.py", "coverage", ".coverage", str(mapped_line), env={"COVERAGE_CORE": "ctrace"}
    )
    reference.expect(0)
    actual = project.run_python("collect.py", "vfy", ".vfy", str(mapped_line))
    actual.expect(0)

    expected_arcs = project.arcs(virtual.name, ".coverage")
    actual_arcs = project.arcs(virtual.name, ".vfy")
    assert actual_arcs == expected_arcs
    assert expected_arcs is not None
    assert (100, 100) in expected_arcs
    assert (2, 2) not in expected_arcs
    assert project.data(".vfy").file_tracer(str(virtual)) is not None


@pytest.mark.parametrize("mapping", ["stable", "line-change", "return-change"])
def test_dynamic_file_tracer_preserves_measured_lines_in_branch_mode(
    tmp_path: Path, mapping: str
) -> None:
    project = VfyProject(tmp_path)
    source = "first = 1\nsecond = 2\nthird = 3\n"
    for filename in ["executed.py", "first.py", "second.py"]:
        project.write(filename, source)
    project.write(
        "collect.py",
        "import dis, importlib, sys\n"
        "from pathlib import Path\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "class Tracer(protocols.FileTracer):\n"
        "    def has_dynamic_source_filename(self):\n        return True\n"
        "    def dynamic_source_filename(self, filename, frame):\n"
        "        changed = (sys.argv[4] == 'line-change' and frame.f_lineno >= 3) or (\n"
        "            sys.argv[4] == 'return-change' and\n"
        "            dis.opname[frame.f_code.co_code[frame.f_lasti]].startswith('RETURN'))\n"
        "        return str(Path('second.py' if changed else 'first.py').resolve())\n"
        "    def line_number_range(self, frame):\n"
        "        return frame.f_lineno, frame.f_lineno\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    def file_tracer(self, filename):\n"
        "        return Tracer() if Path(filename).name == 'executed.py' else None\n"
        "def initialize(registry):\n    registry.add_file_tracer(Plugin())\n"
        "coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], "
        "branch=sys.argv[3] == 'True', plugins=[initialize])\n"
        "coverage.start()\n"
        "exec(compile(Path('executed.py').read_text(), "
        "str(Path('executed.py').resolve()), 'exec'), {})\n"
        "coverage.stop()\ncoverage.save()\n",
    )

    for branch in [False, True]:
        data_file = f".vfy-{branch}"
        actual = project.run_python("collect.py", "vfy", data_file, str(branch), mapping)
        actual.expect(0)
        expected_lines = [1, 2] if mapping == "line-change" else [1, 2, 3]
        assert project.lines("first.py", data_file) == expected_lines
        assert project.lines("second.py", data_file) == ([3] if mapping == "line-change" else None)
        if mapping != "line-change":
            reference = project.run_python(
                "collect.py",
                "coverage",
                ".coverage-reference",
                str(branch),
                mapping,
                env={"COVERAGE_CORE": "ctrace"},
            )
            reference.expect(0)
            assert project.lines("first.py", data_file) == project.lines(
                "first.py", ".coverage-reference"
            )
            assert project.arcs("first.py", data_file) == project.arcs(
                "first.py", ".coverage-reference"
            )
    if mapping == "line-change":
        assert project.arcs("first.py", ".vfy-True") == [(-1, 1), (1, 2)]
        assert project.arcs("second.py", ".vfy-True") == [(-1, 3), (3, -1)]


def test_cover_pylib_keeps_project_and_standard_library_sources(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "sample.py",
        "from fractions import Fraction\n"
        "from packaging.version import Version\n"
        "value = Fraction(2, 3) + 1\n"
        "parsed = Version('1.2')\n",
    )
    project.write(
        "collect.py",
        "import importlib, runpy, sys\n"
        "coverage = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    config_file=False, cover_pylib=True, data_file=sys.argv[2])\n"
        "coverage.start()\n"
        "runpy.run_path('sample.py')\n"
        "coverage.stop()\n"
        "coverage.save()\n",
    )
    for package in ("coverage", "vfy"):
        result = project.run_python("collect.py", package, f".{package}")
        result.expect(0)
    expected = [1, 2, 3, 4]
    assert project.lines("sample.py", ".vfy") == project.lines("sample.py", ".coverage") == expected
    measured = {
        name: {Path(path) for path in project.data(name).measured_files()}
        for name in (".vfy", ".coverage")
    }
    for paths in measured.values():
        assert any(path.name == "fractions.py" for path in paths)
        # Coverage.py's cover_pylib admits the standard library, not installed packages.
        assert not any("site-packages" in path.parts for path in paths)


@pytest.mark.parametrize("selection", ["include", "source"])
def test_cover_pylib_allows_explicitly_selected_installed_package(
    tmp_path: Path, selection: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "collect.py",
        "import importlib, sys\n"
        "from packaging.version import Version\n"
        f"selection = {selection!r}\n"
        "options = {'include': ['*/packaging/*']} if selection == 'include' "
        "else {'source': ['packaging']}\n"
        "cov = importlib.import_module(sys.argv[1]).Coverage(\n"
        "    config_file=False, cover_pylib=True, data_file=sys.argv[2], **options)\n"
        "cov.start()\n"
        "Version('1.2')\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    measured: dict[str, dict[str, list[int] | None]] = {}
    for package in ("coverage", "vfy"):
        result = project.run_python("collect.py", package, f".{package}")
        result.expect(0)
        data = project.data(f".{package}")
        measured[package] = {
            path: data.lines(path)
            for path in data.measured_files()
            if Path(path).name == "version.py"
        }
    assert measured["coverage"]
    assert measured["vfy"] == measured["coverage"]


def test_encoded_source_collects_and_reports_semantic_evidence(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = project.write("sample.py", "")
    source.write_bytes(b'# coding: latin-1\nname = "caf\xe9"\nif name:\n    value = 1\n')
    collected = project.run_vfy("coverage", "run", "--decisions", "sample.py")
    collected.expect(0)
    report = project.run_vfy("coverage", "check")
    report.expect(0)
    assert "stale" not in report.stderr
    annotation = project.run_vfy("coverage", "annotate")
    annotation.expect(0)
    assert 'name = "café"' in (tmp_path / "sample.py,cover").read_text(encoding="utf-8")


@pytest.mark.skipif(
    sys.version_info >= (3, 14), reason="direct branch events supersede legacy branch events"
)
def test_legacy_branch_local_events_preserve_excluded_frames_and_active_threads(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "import relay\n"
        "\n"
        "def recurse(depth):\n"
        "    if depth:\n"
        "        return relay.invoke(recurse, depth - 1) + 1\n"
        "    return 0\n"
        "\n"
        "def values():\n"
        "    try:\n"
        "        yield relay.invoke(recurse, 1)\n"
        "        yield 9\n"
        "    finally:\n"
        "        closed = True\n"
        "\n"
        "def explode():\n"
        "    raise ValueError('expected')\n"
        "\n"
        "def catches():\n"
        "    try:\n"
        "        relay.invoke(explode)\n"
        "    except ValueError:\n"
        "        return 7\n"
        "    return 0\n"
        "\n"
        "def already_running(gate, result):\n"
        "    gate.wait_for_release()\n"
        "    value = 1\n"
        "    if value:\n"
        "        result.append(value + 1)\n"
        "    else:\n"
        "        result.append(0)\n",
    )
    project.write(
        "relay.py",
        "def invoke(function, *args):\n"
        "    return function(*args)\n"
        "\n"
        "def next_value(generator):\n"
        "    return next(generator)\n"
        "\n"
        "def close(generator):\n"
        "    generator.close()\n",
    )
    project.write(
        "collect.py",
        "import importlib, json, pathlib, sys, threading\n"
        "import relay, subject\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "path = str(pathlib.Path('subject.py').resolve())\n"
        "def signature(coverage, contexts):\n"
        "    data = coverage.get_data()\n"
        "    arcs = []\n"
        "    for context in contexts:\n"
        "        data.set_query_context(context)\n"
        "        arcs.append(sorted(data.arcs(path) or []))\n"
        "    return arcs\n"
        "coverage = Coverage(branch=True, include=[path], data_file='.result', config_file=False)\n"
        "coverage.start()\n"
        "def exercise():\n"
        "    assert subject.recurse(3) == 3\n"
        "    generator = subject.values()\n"
        "    assert relay.next_value(generator) == 1\n"
        "    relay.close(generator)\n"
        "    assert subject.catches() == 7\n"
        "contexts = ('first', 'second')\n"
        "for context in contexts:\n"
        "    coverage.switch_context(context)\n"
        "    exercise()\n"
        "    coverage.get_data()\n"
        "    coverage.save()\n"
        "before_reset = signature(coverage, contexts)\n"
        "assert coverage.get_data().measured_files() == {path}\n"
        "coverage.stop()\n"
        "coverage.erase()\n"
        "coverage.start()\n"
        "coverage.switch_context('after-reset')\n"
        "exercise()\n"
        "coverage.stop()\n"
        "after_reset = signature(coverage, ('after-reset',))\n"
        "\n"
        "class Gate:\n"
        "    def __init__(self):\n"
        "        self.entered = threading.Event()\n"
        "        self.release = threading.Event()\n"
        "    def wait_for_release(self):\n"
        "        self.entered.set()\n"
        "        assert self.release.wait(10)\n"
        "gate = Gate()\n"
        "result = []\n"
        "worker = threading.Thread(target=subject.already_running, args=(gate, result))\n"
        "worker.start()\n"
        "assert gate.entered.wait(10)\n"
        "midframe = Coverage(branch=True, include=[path], data_file=None, config_file=False)\n"
        "midframe.start()\n"
        "midframe.switch_context('midframe')\n"
        "gate.release.set()\n"
        "worker.join(10)\n"
        "assert not worker.is_alive() and result == [2]\n"
        "midframe.stop()\n"
        "midframe_arcs = signature(midframe, ('midframe',))\n"
        "if sys.argv[1] == 'vfy':\n"
        "    assert midframe.get_data().measured_files() == {path}\n"
        "    assert set(midframe_arcs[0]) == {(-25, 27), (27, 28), (28, 29), (29, -25)}\n"
        "print(json.dumps([before_reset, after_reset, midframe_arcs]))\n",
    )

    results = []
    for implementation in ("coverage", "vfy"):
        result = project.run_python("collect.py", implementation)
        result.expect(0)
        results.append(json.loads(result.stdout))
    assert results[0][:2] == results[1][:2]
    assert results[0][0][0]
    assert results[0][0][1]
    assert results[0][1][0]


def test_legacy_branch_events_discover_generators_suspended_before_start(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "subject.py",
        "def resume_after_start():\n"
        "    before = 0\n"
        "    yield before\n"
        "    after = 1\n"
        "    if after:\n"
        "        yield after\n"
        "    else:\n"
        "        yield 0\n"
        "\n"
        "def close_after_start():\n"
        "    try:\n"
        "        yield 'ready'\n"
        "    finally:\n"
        "        closed = 1\n"
        "        if closed:\n"
        "            result = closed + 1\n"
        "\n"
        "def callback():\n"
        "    value = 1\n"
        "    if value:\n"
        "        return value + 1\n"
        "    return 0\n",
    )
    project.write(
        "relay.py",
        "def delayed(callback):\n    yield 'ready'\n    callback()\n    yield 'done'\n",
    )
    project.write(
        "collect.py",
        "import importlib, json, pathlib, sys\n"
        "import relay, subject\n"
        "Coverage = importlib.import_module(sys.argv[1]).Coverage\n"
        "path = str(pathlib.Path('subject.py').resolve())\n"
        "resume = subject.resume_after_start()\n"
        "assert next(resume) == 0\n"
        "closing = subject.close_after_start()\n"
        "assert next(closing) == 'ready'\n"
        "excluded = relay.delayed(subject.callback)\n"
        "assert next(excluded) == 'ready'\n"
        "coverage = Coverage(branch=True, include=[path], data_file='.result', config_file=False)\n"
        "coverage.start()\n"
        "coverage.switch_context('resume')\n"
        "assert next(resume) == 1\n"
        "try:\n"
        "    next(resume)\n"
        "except StopIteration:\n"
        "    pass\n"
        "coverage.switch_context('close')\n"
        "closing.close()\n"
        "coverage.switch_context('excluded')\n"
        "assert next(excluded) == 'done'\n"
        "coverage.stop()\n"
        "data = coverage.get_data()\n"
        "arcs = {}\n"
        "for context in ('resume', 'close', 'excluded'):\n"
        "    data.set_query_context(context)\n"
        "    arcs[context] = sorted(data.arcs(path) or [])\n"
        "print(json.dumps(arcs))\n",
    )

    results = []
    for implementation in ("coverage", "vfy"):
        result = project.run_python("collect.py", implementation, env={"COVERAGE_CORE": "ctrace"})
        result.expect(0)
        results.append(json.loads(result.stdout))

    expected, actual = results
    assert actual == expected
    assert expected["resume"]
    assert expected["close"]
    assert expected["excluded"]
