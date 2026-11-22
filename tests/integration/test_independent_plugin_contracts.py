"""Differentials for the independent unmeasured plugin replacements."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_ENV = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def _compare(project: VfyProject, upstream: str, replacement: str, *options: str) -> None:
    stock = project.run_python(
        "-m", "pytest", "-p", upstream, "-p", "no:cacheprovider", "-q", *options, env=_ENV
    )
    stock.expect(0)
    expected = json.loads((project.root / "observed.json").read_text())
    (project.root / "observed.json").unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "-p",
        replacement,
        "-p",
        "no:cacheprovider",
        "-q",
        *options,
        env=_ENV,
    )
    actual.expect(0)
    assert json.loads((project.root / "observed.json").read_text()) == expected


@requires_native_pytest
def test_benchmark_one_use_matches_disabled_reference(tmp_path: Path) -> None:
    assert version("pytest-benchmark") == "5.3.1"
    project = VfyProject(tmp_path)
    project.write(
        "test_contract.py",
        """\
import json
from pathlib import Path
import pytest

records = []

@pytest.mark.parametrize("first,second", [
    ("call", "call"), ("call", "pedantic"),
    ("pedantic", "call"), ("pedantic", "pedantic"),
    ("invalid", "call"), ("raises", "pedantic"),
])
def test_guard(benchmark, first, second):
    events = []
    def target():
        events.append("target")
        if first == "raises":
            raise ValueError("target failed")
        return 42
    try:
        if first in ("call", "raises"):
            benchmark(target)
        elif first == "invalid":
            benchmark.pedantic(target, rounds=0)
        else:
            benchmark.pedantic(target)
    except ValueError as error:
        events.append(str(error))
    try:
        if second == "call":
            benchmark(lambda: events.append("second target"))
        else:
            benchmark.pedantic(lambda: events.append("second target"))
    except Exception as error:
        events.append([type(error).__name__, str(error)])
    records.append([first, second, events])

def test_record():
    Path("observed.json").write_text(json.dumps(records))
""",
    )
    _compare(project, "pytest_benchmark.plugin", "vfy.independent.benchmark", "--benchmark-disable")


@requires_native_pytest
def test_mock_spy_iterator_and_reset_match_reference(tmp_path: Path) -> None:
    assert version("pytest-mock") == "3.15.1"
    project = VfyProject(tmp_path)
    project.write(
        "test_contract.py",
        """\
import asyncio
import json
from pathlib import Path

class Target:
    def values(self, kind):
        return iter([1, 2]) if kind == "iterator" else [3, 4]
    async def async_values(self):
        return iter([5, 6])

def test_spy(mocker):
    target = Target()
    spy = mocker.spy(target, "values", duplicate_iterators=True)
    records = [[spy.spy_return is None, spy.spy_return_iter is None, len(spy.spy_return_list)]]
    result = target.values("iterator")
    records.append([list(result), list(spy.spy_return_iter), len(spy.spy_return_list)])
    result = target.values("list")
    records.append([result, spy.spy_return_iter is None, len(spy.spy_return_list)])
    mocker.resetall()
    records.append([spy.spy_return_iter is None, spy.spy_return_list, spy.call_count])
    mocker.stop(spy)
    spy = mocker.spy(target, "values")
    result = target.values("iterator")
    records.append([list(result), spy.spy_return_iter is None, len(spy.spy_return_list)])
    async_spy = mocker.spy(target, "async_values", duplicate_iterators=True)
    records.append([list(asyncio.run(target.async_values())), async_spy.spy_return_iter is None])
    mocker.resetall()
    records.append([async_spy.spy_return_iter is None, async_spy.spy_return_list])
    Path("observed.json").write_text(json.dumps(records))
""",
    )
    _compare(project, "pytest_mock.plugin", "vfy.independent.mock_support")
