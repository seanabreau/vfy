"""Random-order's cache integration reads the inactive failed-first mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_random_order_keeps_the_stock_order_with_failed_first_disabled(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_collection_finish(session):
    config = session.config
    Path('collection.json').write_text(json.dumps({
        'items': [item.nodeid for item in session.items],
        'failedfirst': [config.getoption('failedfirst'),
                        config.getoption('failedfirst', True),
                        config.option.failedfirst],
        'plugin': config.pluginmanager.hasplugin('random_order.plugin'),
    }))
""",
    )
    project.write(
        "test_order.py",
        """from pathlib import Path

def observe(name):
    with Path('calls').open('a') as output:
        output.write(name + '\\n')

def test_a(): observe('a')
def test_b(): observe('b')
def test_c(): observe('c')
def test_d(): observe('d')
""",
    )
    observations = []
    for engine in ("stock", "native"):
        result = project.run_engine(
            engine,
            "-p",
            "random_order.plugin",
            "--random-order",
            "--random-order-bucket=global",
            "--random-order-seed=1",
            "-q",
        )
        result.expect(0)
        collection = json.loads((project.root / "collection.json").read_text())
        assert collection["plugin"] is True
        assert collection["failedfirst"] == [False, False, False]
        calls = (project.root / "calls").read_text().splitlines()
        assert len(calls) == 4
        assert set(calls) == {"a", "b", "c", "d"}
        assert calls != ["a", "b", "c", "d"]
        observations.append((collection["items"], calls))
        (project.root / "calls").unlink()
    assert observations[0] == observations[1]


@pytest.mark.parametrize("argument", ["--ff", "--failed-first"])
def test_unimplemented_failed_first_requests_remain_usage_errors(
    tmp_path: Path, argument: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_unrun.py", "def test_unrun(): raise AssertionError('must not run')\n")
    result = project.run_vfy("test", "--engine=native", "--no-cov", argument)
    result.expect(4)
    assert argument in result.stderr
