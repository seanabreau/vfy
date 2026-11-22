"""Compare compiler-folded __debug__ conditions with Coverage.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject


@pytest.mark.parametrize(
    "optimization", ["", "-O", "-OO"], ids=["normal", "optimized", "no-docstrings"]
)
@pytest.mark.parametrize(
    "condition", ["__debug__", "not __debug__", "(__debug__ and True)", "True", "(True)"]
)
@pytest.mark.parametrize("suite", ["if", "while"])
def test_debug_condition_obligations_follow_interpreter_optimization(
    tmp_path: Path, optimization: str, condition: str, suite: str
) -> None:
    loop_break = "        break\n" if suite == "while" else ""
    source = f"""def choose(flag):
    {suite} {condition}:
        if flag:
            answer = 1
        else:
            answer = 2
{loop_break}    else:
        if flag:
            answer = 3
        else:
            answer = 4
    return answer

choose(True)
"""
    _compare_folded_condition(tmp_path, source, optimization)


@pytest.mark.parametrize(
    "optimization", ["", "-O", "-OO"], ids=["normal", "optimized", "no-docstrings"]
)
@pytest.mark.parametrize(
    "condition",
    [
        "__debug__ and flag",
        "not __debug__ and flag",
        "flag and __debug__",
        "flag and not __debug__",
        "__debug__ or flag",
        "not __debug__ or flag",
        "flag or __debug__",
        "flag or not __debug__",
        "False and flag",
        "True or flag",
    ],
)
@pytest.mark.parametrize("suite", ["if", "while"])
def test_mixed_debug_conditions_preserve_compiled_statements_and_reference_branches(
    tmp_path: Path, optimization: str, condition: str, suite: str
) -> None:
    if suite == "if":
        source = (
            f"def choose(flag):\n    if {condition}:\n"
            "        return 1\n    return 0\nchoose(True)\n"
        )
    else:
        source = (
            f"def choose(flag):\n    while {condition}:\n"
            "        answer = 1\n        break\n    else:\n"
            "        answer = 0\n    return answer\nchoose(True)\n"
        )
    _compare_folded_condition(tmp_path, source, optimization)


def _compare_folded_condition(tmp_path: Path, source: str, optimization: str) -> None:
    project = VfyProject(tmp_path)
    project.write("example.py", source)
    project.write(
        "compare.py",
        """import json
import runpy
from pathlib import Path

import coverage
import vfy

observed = {}
for name, cls in [('reference', coverage.Coverage), ('vfy', vfy.Coverage)]:
    cov = cls(data_file=f'.{name}', branch=True, config_file=False)
    try:
        cov.start()
        try:
            runpy.run_path('example.py')
        finally:
            cov.stop()
        cov.json_report(morfs=['example.py'], outfile=f'{name}.json')
        report = json.loads(Path(f'{name}.json').read_text())
        observed[name] = {
            'report': next(iter(report['files'].values())),
            'analysis2': cov.analysis2('example.py'),
            'lines': sorted(cov.get_data().lines(str(Path('example.py').resolve())) or []),
            'arcs': sorted(cov.get_data().arcs(str(Path('example.py').resolve())) or []),
        }
    finally:
        cov.get_data().close()
print(json.dumps(observed))
""",
    )
    result = project.run_python(*((optimization,) if optimization else ()), "compare.py")
    result.expect(0)
    observed = json.loads(result.stdout)
    assert observed["vfy"] == observed["reference"]


@pytest.mark.parametrize(
    "optimization", ["", "-O", "-OO"], ids=["normal", "optimized", "no-docstrings"]
)
@pytest.mark.parametrize("condition", ["__debug__ and flag", "not __debug__ and flag"])
def test_folded_else_return_prunes_only_compiler_unreachable_following_statements(
    tmp_path: Path, optimization: str, condition: str
) -> None:
    source = (
        f"def choose(flag):\n    if {condition}:\n"
        "        value = 1\n    else:\n        return 0\n"
        "    return value\nchoose(True)\n"
    )
    _compare_folded_condition(tmp_path, source, optimization)


@pytest.mark.parametrize(
    "optimization", ["", "-O", "-OO"], ids=["normal", "optimized", "no-docstrings"]
)
@pytest.mark.parametrize("condition", ["__debug__", "not __debug__", "True", "False"])
@pytest.mark.parametrize("suite", ["if", "while"])
def test_inline_folded_body_preserves_executable_condition_header(
    tmp_path: Path, optimization: str, condition: str, suite: str
) -> None:
    operation = "return 1" if suite == "if" else "break"
    source = f"def choose():\n    {suite} {condition}: {operation}\n    return 0\nchoose()\n"
    _compare_folded_condition(tmp_path, source, optimization)


@pytest.mark.parametrize(
    "optimization", ["", "-O", "-OO"], ids=["normal", "optimized", "no-docstrings"]
)
def test_folded_nested_header_stays_removed_with_its_outer_suite(
    tmp_path: Path, optimization: str
) -> None:
    source = (
        "def choose():\n    if not __debug__:\n"
        "        if __debug__: return 1\n    return 0\nchoose()\n"
    )
    _compare_folded_condition(tmp_path, source, optimization)
