"""Nested ``with`` statements report Coverage.py's branches and executed arcs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from coverage import Coverage as CoveragePy

from tests.support.reference import reference_coverage
from vfy import Coverage

# Coverage.py rewrites the arcs that leave a ``with`` statement through its
# header. When a ``with`` statement ends the suite of a ``with`` statement that
# itself ends an enclosing ``with`` suite, the rewrite keeps an arc from the
# inner header to the statement after the outermost one, so that header
# reports two branches. Each scenario runs to completion when ``RUN`` is set.
_SCENARIOS = {
    "two_levels": """\
def scenario():
    with null():
        with null():
            done()
    done()
""",
    "three_levels": """\
def scenario():
    with null():
        with null():
            with null():
                done()
    done()
""",
    "four_levels": """\
def scenario():
    with null():
        with null():
            with null():
                with null():
                    done()
    done()
""",
    "function_end": """\
def scenario():
    with null():
        with null():
            with null():
                done()
""",
    "inner_followed_by_statement": """\
def scenario():
    with null():
        with null():
            with null():
                done()
            done()
    done()
""",
    "middle_followed_by_statement": """\
def scenario():
    with null():
        with null():
            with null():
                done()
        done()
    done()
""",
    "inner_returns": """\
def scenario():
    with null():
        with null():
            with null():
                return done()
    done()
""",
    "inside_loop": """\
def scenario():
    for _ in range(2):
        with null():
            with null():
                with null():
                    done()
    done()
""",
    "loop_between": """\
def scenario():
    with null():
        for _ in range(2):
            with null():
                with null():
                    with null():
                        done()
    done()
""",
    "inside_try": """\
def scenario():
    try:
        with null():
            with null():
                with null():
                    done()
    except ValueError:
        done()
    done()
""",
    "try_without_finally": """\
def scenario():
    with null():
        with null():
            try:
                with null():
                    done()
            except ValueError:
                done()
    done()
""",
    "try_with_finally": """\
def scenario():
    with null():
        with null():
            try:
                with null():
                    done()
            finally:
                done()
    done()
""",
    "through_if": """\
def scenario(flag=True):
    with null():
        with null():
            if flag:
                with null():
                    done()
    done()


def other():
    scenario(False)
""",
    "through_for_else": """\
def scenario():
    with null():
        with null():
            for _ in range(1):
                done()
            else:
                with null():
                    done()
    done()
""",
    "multiple_items": """\
def scenario():
    with null(), null():
        with null():
            with null():
                done()
    done()
""",
    "async": """\
async def body():
    async with anull():
        async with anull():
            async with anull():
                done()
    done()


def scenario():
    asyncio.run(body())
""",
}
_PRELUDE = """\
import asyncio
import contextlib

null = contextlib.nullcontext


@contextlib.asynccontextmanager
async def anull():
    yield


def done():
    return None


"""


def _branch_report(measured: Coverage | CoveragePy, source: Path, output: Path) -> object:
    measured.start()
    try:
        namespace: dict[str, object] = {}
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
        if namespace.get("RUN"):
            for name in ("scenario", "other"):
                function = namespace.get(name)
                if callable(function):
                    function()
    finally:
        measured.stop()
    measured.json_report(morfs=[str(source)], outfile=str(output))
    files = json.loads(output.read_text(encoding="utf-8"))["files"]
    (report,) = files.values()
    summary = report["summary"]
    return {
        "executed_branches": report["executed_branches"],
        "missing_branches": report["missing_branches"],
        "num_branches": summary["num_branches"],
        "covered_branches": summary["covered_branches"],
    }


@pytest.mark.parametrize("run", [True, False], ids=["executed", "unexecuted"])
@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_nested_with_branches_match_coverage_py(tmp_path: Path, scenario: str, run: bool) -> None:
    source = tmp_path / f"{scenario}.py"
    source.write_text(f"RUN = {run}\n{_PRELUDE}{_SCENARIOS[scenario]}", encoding="utf-8")

    with reference_coverage(branch=True, config_file=False) as reference:
        expected = _branch_report(reference, source, tmp_path / "reference.json")
    candidate = Coverage(data_file=None, branch=True, config_file=False)
    try:
        actual = _branch_report(candidate, source, tmp_path / "vfy.json")
    finally:
        candidate.get_data().close(force=True)

    assert actual == expected
