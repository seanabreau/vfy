"""Coverage.py differentials for dynamic contexts."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.support import VfyProject

_CHECKS = """\
from collections import namedtuple


def work(value):
    return value + 1


def test_helper():
    return work(1)


def test_module():
    test_helper()
    return work(2)


def test_recursive(depth=2):
    if depth:
        return test_recursive(depth - 1)
    return work(3)


class Case:
    def test_method(self):
        return work(4)

    def runTest(self):
        return self.test_method()


def main():
    namedtuple("Pair", "left right")
    test_module()
    test_recursive()
    Case().test_method()
    Case().runTest()
    work(5)


main()
"""

_NESTED_PLUGIN = """\
import importlib
import sys

tool = importlib.import_module(sys.argv[1])
protocols = importlib.import_module(sys.argv[1] + ".plugin")


class Plugin(protocols.CoveragePlugin):
    def dynamic_context(self, frame):
        name = frame.f_code.co_name
        return name if name.startswith("test") else None


def initialize(registry):
    registry.add_dynamic_context(Plugin())


coverage = tool.Coverage(config_file=False, data_file=sys.argv[2], plugins=[initialize])
coverage.start()
import checks

coverage.stop()
coverage.save()
"""


def _by_context(
    project: VfyProject, data_file: str
) -> dict[str, tuple[list[int], list[tuple[int, int]]]]:
    data = project.data(data_file)
    source = str(project.root / "checks.py")
    observed: dict[str, tuple[list[int], list[tuple[int, int]]]] = {}
    for context in sorted(data.measured_contexts()):
        data.set_query_contexts([f"^{re.escape(context)}$"])
        observed[context] = (sorted(data.lines(source) or []), sorted(data.arcs(source) or []))
    return observed


@pytest.mark.parametrize("branch", [False, True], ids=["line", "branch"])
def test_test_function_contexts_match_coverage_py(tmp_path: Path, *, branch: bool) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\ndynamic_context = test_function\n")
    project.write("checks.py", _CHECKS)
    options = ("--branch",) if branch else ()

    # Coverage.py's sys.monitoring core does not support dynamic contexts.
    reference = project.run_coverage_py(
        "run",
        *options,
        "--data-file=.coverage.reference",
        "checks.py",
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_vfy(
        "coverage", "run", *options, "--data-file=.coverage.actual", "checks.py"
    )

    reference.expect(0)
    actual.expect(0)
    expected = _by_context(project, ".coverage.reference")
    assert set(expected) == {
        "",
        "__main__.Case.runTest",
        "__main__.Case.test_method",
        "__main__.test_module",
        "__main__.test_recursive",
    }
    assert _by_context(project, ".coverage.actual") == expected


def test_nested_plugin_context_keeps_the_context_that_started_first(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("checks.py", _CHECKS)
    project.write("collect.py", _NESTED_PLUGIN)

    reference = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")

    reference.expect(0)
    actual.expect(0)
    expected = _by_context(project, ".coverage.reference")
    assert "test_helper" not in expected
    assert _by_context(project, ".coverage.actual") == expected


def test_test_function_switcher_precedes_plugins(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("checks.py", _CHECKS)
    project.write(
        "collect.py",
        _NESTED_PLUGIN.replace(
            "coverage.start()",
            'coverage.set_option("run:dynamic_context", "test_function")\ncoverage.start()',
        ),
    )
    reference = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")
    reference.expect(0)
    actual.expect(0)
    expected = _by_context(project, ".coverage.reference")
    assert "checks.test_module" in expected
    assert "test_module" not in expected
    assert _by_context(project, ".coverage.actual") == expected


def test_empty_plugin_context_keeps_static_label_and_owns_nested_frames(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "checks.py",
        "def test_helper():\n    return 1\n"
        "def test_outer():\n    return test_helper()\n"
        "test_outer()\n",
    )
    project.write(
        "collect.py",
        _NESTED_PLUGIN.replace(
            'return name if name.startswith("test") else None',
            'return "" if name == "test_outer" else "helper" if name == "test_helper" else None',
        ).replace("config_file=False,", 'config_file=False, context="static",'),
    )
    reference = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")
    reference.expect(0)
    actual.expect(0)
    expected = _by_context(project, ".coverage.reference")
    assert set(expected) == {"static"}
    assert _by_context(project, ".coverage.actual") == expected


def test_unknown_dynamic_context_is_a_configuration_error(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\ndynamic_context = test_method\n")
    project.write("checks.py", "value = 1\n")

    reference = project.run_coverage_py("run", "checks.py")
    actual = project.run_vfy("coverage", "run", "checks.py")

    assert reference.returncode == actual.returncode == 1
    message = "Don't understand dynamic_context setting: 'test_method'"
    assert message in reference.stdout + reference.stderr
    assert message in actual.stdout + actual.stderr


@pytest.mark.parametrize("mode", ["line", "branch", "semantic"])
def test_dynamic_contexts_follow_generator_and_exception_boundaries(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\ndynamic_context = test_function\ncontext = static\n")
    project.write(
        "checks.py",
        "def test_generator():\n"
        "    try:\n"
        "        yield 1\n"
        "    except ValueError:\n"
        "        yield 2\n"
        "    finally:\n"
        "        cleanup = True\n"
        "\n"
        "def test_failure():\n"
        "    raise ValueError()\n"
        "\n"
        "first, second = test_generator(), test_generator()\n"
        "assert next(first) == 1\n"
        "outside_first = True\n"
        "assert next(second) == 1\n"
        "assert first.throw(ValueError()) == 2\n"
        "outside_throw = True\n"
        "second.close()\n"
        "first.close()\n"
        "try:\n"
        "    test_failure()\n"
        "except ValueError:\n"
        "    outside_failure = True\n",
    )
    _assert_context_parity(project, mode)


@pytest.mark.parametrize("mode", ["line", "branch"])
def test_overlapping_threads_keep_independent_dynamic_context_owners(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(".coveragerc", "[run]\ndynamic_context = test_function\n")
    project.write(
        "checks.py",
        "def test_a(pause):\n"
        "    before = True\n"
        "    pause()\n"
        "    a = True\n"
        "\n"
        "def test_b(pause):\n"
        "    before = True\n"
        "    pause()\n"
        "    b = True\n",
    )
    project.write(
        "driver.py",
        "from threading import Event, Thread\n"
        "from checks import test_a, test_b\n"
        "ready_a, ready_b, go_a, go_b = (Event() for _ in range(4))\n"
        "def pause_a():\n"
        "    ready_a.set()\n"
        "    assert go_a.wait(10)\n"
        "def pause_b():\n"
        "    ready_b.set()\n"
        "    assert go_b.wait(10)\n"
        "a = Thread(target=test_a, args=(pause_a,))\n"
        "b = Thread(target=test_b, args=(pause_b,))\n"
        "a.start()\n"
        "assert ready_a.wait(10)\n"
        "b.start()\n"
        "assert ready_b.wait(10)\n"
        "go_a.set()\n"
        "a.join(10)\n"
        "assert not a.is_alive()\n"
        "go_b.set()\n"
        "b.join(10)\n"
        "assert not b.is_alive()\n",
    )
    # Coverage.py keeps each tracer's owner independently, but changing a
    # dynamic context changes the shared data label for every thread.
    _assert_context_parity(project, mode, "driver.py")


def _assert_context_parity(project: VfyProject, mode: str, driver: str = "checks.py") -> None:
    branch = ("--branch",) if mode != "line" else ()
    reference = project.run_coverage_py(
        "run",
        *branch,
        "--data-file=.coverage.reference",
        driver,
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_vfy(
        "coverage",
        "run",
        *branch,
        *(("--decisions",) if mode == "semantic" else ()),
        "--data-file=.coverage.actual",
        driver,
    )
    reference.expect(0)
    actual.expect(0)
    assert _by_context(project, ".coverage.actual") == _by_context(project, ".coverage.reference")


def test_plugin_switchers_are_only_consulted_on_frame_entry(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("checks.py", "def work():\n    value = 1\n    return value\nwork()\n")
    project.write(
        "collect.py",
        _NESTED_PLUGIN.replace(
            'return name if name.startswith("test") else None',
            'if name == "work":\n'
            "            calls.append(frame.f_lineno)\n"
            "            if frame.f_lineno != frame.f_code.co_firstlineno:\n"
            '                return "late"\n'
            "        return None",
        ).replace("coverage.start()", "calls = []\ncoverage.start()")
        + "\nimport json\nprint(json.dumps(calls))\n",
    )
    reference = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")
    reference.expect(0)
    actual.expect(0)
    assert json.loads(reference.stdout) == [1]
    assert json.loads(actual.stdout) == json.loads(reference.stdout)
    assert _by_context(project, ".coverage.actual") == _by_context(project, ".coverage.reference")


def test_non_test_frames_do_not_request_dynamic_context_frame_objects(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "collect.py",
        "import importlib, sys\n"
        "def work():\n"
        "    return 1\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "cov = tool.Coverage(config_file=False, data_file=sys.argv[2])\n"
        "cov.set_option('run:dynamic_context', 'test_function')\n"
        "cov.start()\n"
        "assert work() == 1\n"
        "def audit(event, args):\n"
        "    if event == 'sys._getframe' and args[0].f_code is work.__code__:\n"
        "        raise RuntimeError('non-test frame requested by dynamic context switcher')\n"
        "sys.addaudithook(audit)\n"
        "assert work() == 1\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    reference = project.run_python(
        "collect.py", "coverage", ".coverage.reference", env={"COVERAGE_CORE": "ctrace"}
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual")
    reference.expect(0)
    actual.expect(0)


@pytest.mark.parametrize("selector", ["plugin", "receiver"])
@pytest.mark.parametrize("mode", ["line", "branch"])
def test_context_selection_allows_other_threads_to_collect(
    tmp_path: Path, selector: str, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "checks.py",
        "def test_owner(self=None):\n    return 1\ndef work():\n    value = 2\n    return value\n",
    )
    project.write(
        "collect.py",
        "import importlib, sys\n"
        "from threading import Event, Thread\n"
        "from checks import test_owner, work\n"
        "tool = importlib.import_module(sys.argv[1])\n"
        "protocols = importlib.import_module(sys.argv[1] + '.plugin')\n"
        "entered, proceed = Event(), Event()\n"
        "armed = False\n"
        "def pause():\n"
        "    entered.set()\n"
        "    assert proceed.wait(5)\n"
        "class Plugin(protocols.CoveragePlugin):\n"
        "    def dynamic_context(self, frame):\n"
        "        if frame.f_code.co_name == 'test_owner':\n"
        "            pause()\n"
        "            return 'owner'\n"
        "        return None\n"
        "def initialize(registry):\n"
        "    registry.add_dynamic_context(Plugin())\n"
        "class Case:\n"
        "    test_owner = test_owner\n"
        "    def __getattribute__(self, name):\n"
        "        if armed and name == 'test_owner':\n"
        "            pause()\n"
        "        return object.__getattribute__(self, name)\n"
        "plugin = sys.argv[3] == 'plugin'\n"
        "cov = tool.Coverage(config_file=False, data_file=sys.argv[2],\n"
        "    branch=sys.argv[4] == 'branch', plugins=[initialize] if plugin else [])\n"
        "target = test_owner if plugin else Case().test_owner\n"
        "if not plugin:\n"
        "    cov.set_option('run:dynamic_context', 'test_function')\n"
        "armed = True\n"
        "cov.start()\n"
        "worker = Thread(target=target, daemon=True)\n"
        "worker.start()\n"
        "assert entered.wait(5)\n"
        "try:\n"
        "    assert work() == 2\n"
        "finally:\n"
        "    proceed.set()\n"
        "worker.join(5)\n"
        "assert not worker.is_alive()\n"
        "cov.stop()\n"
        "cov.save()\n",
    )
    reference = project.run_python(
        "collect.py",
        "coverage",
        ".coverage.reference",
        selector,
        mode,
        env={"COVERAGE_CORE": "ctrace"},
    )
    actual = project.run_python("collect.py", "vfy", ".coverage.actual", selector, mode)
    reference.expect(0)
    actual.expect(0)
    assert actual.stderr == reference.stderr == ""
    expected = _by_context(project, ".coverage.reference")
    assert set(expected) == {"", "owner" if selector == "plugin" else "checks.test_owner"}
    assert _by_context(project, ".coverage.actual") == expected
