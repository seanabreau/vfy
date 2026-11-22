"""Prove pytest-mock assertion wrappers restore in collected worker processes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.support.vfy_project import VfyProject

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="collected fork workers require Linux"
)

_FORK_ENV = {
    "VFY_WORKER_START": "fork",
    "VFY_NATIVE_SCHEDULING": "cases",
    "VFY_WORKER_COLLECTION": "0",
}


def test_collected_fork_pytest_mock_restores_assertion_methods(tmp_path: Path) -> None:
    observations: dict[str, list[dict[str, object]]] = {}
    for route in ("stock", "exec", "fork"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            """
import json, os, weakref
from pathlib import Path
import unittest.mock as mock
import pytest_mock.plugin as plugin
import pytest


def record_cleanup(originals):
    restored = all(
        getattr(mock.AsyncMock if 'await' in name else mock.NonCallableMock, name) is original
        for name, original in originals.items()
    )
    Path(f'restored.{os.getpid()}.json').write_text(json.dumps({
        'restored': restored,
        'patches_empty': not plugin._mock_module_patches,
        'originals_empty': not plugin._mock_module_originals,
    }))


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    assert len(plugin._mock_module_patches) == 14
    originals = dict(plugin._mock_module_originals)
    # The last patcher dies after unwrap has cleared both process-local tables.
    weakref.finalize(plugin._mock_module_patches[-1], record_cleanup, originals)
""",
        )
        project.write(
            "tests/test_cases.py",
            """
import os
from pathlib import Path
import pytest
import pytest_mock.plugin as plugin


@pytest.mark.parametrize('value', [0, 1])
def test_mock(value, mocker):
    assert mocker.Mock.assert_called_once is plugin.wrap_assert_called_once
    ordinary = mocker.Mock()
    ordinary(value)
    ordinary.assert_called_once_with(value)
    with pytest.raises(AssertionError, match='Expected'):
        ordinary.assert_called_once_with('wrong')
    asynchronous = mocker.AsyncMock()
    import asyncio
    asyncio.run(asynchronous(value))
    asynchronous.assert_awaited_once_with(value)
    with pytest.raises(AssertionError):
        asynchronous.assert_awaited_once_with('wrong')
    Path(f'body.{value}').write_text(str(os.getpid()))
""",
        )
        if route == "stock":
            result = project.run_pytest("-p", "pytest_mock", "-n", "2", "--capture=sys", "-q")
        else:
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--history",
                "history.json",
                "--no-replay",
                "--no-cov",
                "--capture=sys",
                "-p",
                "pytest_mock",
                "-q",
                env={
                    **_FORK_ENV,
                    "VFY_WORKER_START": route if route == "exec" else "fork",
                },
            )
        result.expect(0)
        body_pids = {(project.root / f"body.{value}").read_text() for value in (0, 1)}
        assert len(body_pids) == 2
        rows = [
            json.loads((project.root / f"restored.{pid}.json").read_text()) for pid in body_pids
        ]
        assert all(
            row == {"restored": True, "patches_empty": True, "originals_empty": True}
            for row in rows
        )
        cleanup_files = list(project.root.glob("restored.*.json"))
        assert len(cleanup_files) == 3
        assert all(json.loads(path.read_text()) == rows[0] for path in cleanup_files)
        observations[route] = rows
    assert observations["stock"] == observations["exec"] == observations["fork"]


@pytest.mark.parametrize("boundary", ["foreign_cleanup", "getter", "target", "stop", "exit_stack"])
def test_collected_fork_rejects_foreign_pytest_mock_cleanup(tmp_path: Path, boundary: str) -> None:
    project = VfyProject(tmp_path)
    mutation = {
        "foreign_cleanup": """
    def unwrap_assert_methods():
        pass
    unwrap_assert_methods.__module__ = 'pytest_mock.plugin'
    session.config.add_cleanup(unwrap_assert_methods)
""",
        "getter": """
    plugin._mock_module_patches[0].getter = lambda: mock.NonCallableMock
""",
        "target": """
    class Foreign:
        pass
    plugin._mock_module_patches[0].target = Foreign
""",
        "stop": """
    plugin._mock_module_patches[0].stop = lambda: None
""",
        "exit_stack": """
    plugin._mock_module_patches[0]._exit_stack.__exit__ = lambda *args: None
""",
    }[boundary]
    project.write(
        "conftest.py",
        "import unittest.mock as mock\nimport pytest_mock.plugin as plugin\n"
        "def pytest_collection_finish(session):\n" + mutation,
    )
    project.write(
        "tests/test_case.py",
        "from pathlib import Path\ndef test_body(mocker):\n    Path('body-ran').touch()\n",
    )
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--history",
        "history.json",
        "--no-replay",
        "--no-cov",
        "--capture=sys",
        "-p",
        "pytest_mock",
        "-q",
        env=_FORK_ENV,
    )
    assert result.returncode != 0
    assert "unreviewed config cleanup callback" in result.stdout + result.stderr
    assert not (project.root / "body-ran").exists()
