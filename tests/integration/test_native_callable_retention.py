"""Ordinary pytest items own the lifetime of their live test callable."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from tests.support import VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")) or importlib.util.find_spec("xdist") is None,
    reason="native xdist execution requires Linux or macOS and pytest-xdist",
)


@pytest.mark.parametrize("workers", ["0", "2"])
def test_replaced_ordinary_callable_is_released_before_the_new_callable_runs(
    tmp_path: Path, workers: str
) -> None:
    # This proof mutates installed pytest Function._obj and forbids its stock runner.
    observations: dict[str, list[str]] = {}
    for route in ("stock", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            dedent("""\
                import os
                from pathlib import Path
                import pytest
                from _pytest.python import Function

                def record(event):
                    with Path('lifetime.txt').open('a') as stream:
                        stream.write(event + '\\n')

                class OriginalCallable:
                    def __init__(self, name):
                        self.name = name
                        self.replaced = False

                    def __call__(self):
                        raise AssertionError('original callable must be replaced')

                    def __del__(self):
                        if self.replaced:
                            Path('released-' + self.name).touch()
                            record('release ' + self.name)

                def pytest_collection_modifyitems(items):
                    for item in items:
                        assert type(item) is Function
                        item._obj = OriginalCallable(item.name)

                @pytest.fixture(autouse=True)
                def replace_callable(request):
                    name = request.node.name
                    def replacement():
                        assert Path('released-' + name).exists()
                        record('call ' + name)
                    request.node._obj.replaced = True
                    request.node._obj = replacement
                    assert Path('released-' + name).exists()
                    record('setup ' + name)
                    yield
                    record('teardown ' + name)
                """),
        )
        project.write("test_callables.py", "def test_first(): pass\ndef test_second(): pass\n")
        arguments = (f"--numprocesses={workers}", "--dist=loadfile", "-q")
        result = (
            project.run_pytest("-p", "xdist.plugin", *arguments, load_xdist=False)
            if route == "stock"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--no-replay",
                "--no-cov",
                *arguments,
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout, result.stdout + result.stderr
        observations[route] = (project.root / "lifetime.txt").read_text().splitlines()
    assert (
        observations["native"]
        == observations["stock"]
        == [
            f"{event} test_{name}"
            for name in ("first", "second")
            for event in ("release", "setup", "call", "teardown")
        ]
    )
