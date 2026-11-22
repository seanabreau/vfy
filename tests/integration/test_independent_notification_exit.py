"""Secondary notification exits keep stock status, INTERNALERROR output, and hook order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.test_independent_invocation import (
    _OBSERVER,
    _environment,
    _project,
)
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SECONDARY = """
_original_internalerror = pytest_internalerror
_original_keyboard_interrupt = pytest_keyboard_interrupt


def secondary_exit():
    try:
        pytest.exit(
            'secondary notification stop',
            returncode=json.loads(os.environ['NOTIFICATION_CODE']),
        )
    except BaseException:
        note('notification raise')
        raise


def pytest_internalerror(excrepr, excinfo):
    _original_internalerror(excrepr, excinfo)
    secondary_exit()


def pytest_keyboard_interrupt(excinfo):
    _original_keyboard_interrupt(excinfo)
    secondary_exit()
"""


@pytest.mark.parametrize("workers", [False, True])
@pytest.mark.parametrize("secondary_code", [7, None])
@pytest.mark.parametrize("primary", ["runtime", "exit", "keyboard"])
def test_secondary_notification_exit_matches_stock_status_output_and_hook_order(
    tmp_path: Path, workers: bool, secondary_code: int | None, primary: str
) -> None:
    project = _project(tmp_path)
    (tmp_path / "observer_plugin.py").write_text(_OBSERVER + _SECONDARY)
    plugins = ["-p", "observer_plugin", "-p", "broken_plugin"]
    environments = {
        engine: {
            **_environment(tmp_path, engine, "loop", primary, False),
            "NOTIFICATION_CODE": json.dumps(secondary_code),
        }
        for engine in ("oracle", "native")
    }
    oracle = project.run_python("-m", "pytest", "-q", *plugins, env=environments["oracle"])
    arguments = ["test", "--engine=native", "--no-cov", "-q", *plugins]
    if workers:
        arguments.append("--workers=2")
    actual = project.run_vfy(*arguments, env=environments["native"])

    if primary == "runtime":
        assert oracle.returncode == (3 if secondary_code is None else secondary_code)
    else:
        oracle.expect(1)
    assert actual.returncode == oracle.returncode, (actual.stdout, actual.stderr)
    assert ("INTERNALERROR>" in oracle.stdout + oracle.stderr) is (primary == "runtime")
    assert ("INTERNALERROR>" in actual.stdout + actual.stderr) is (primary == "runtime")
    expected = json.loads((tmp_path / "events-oracle.json").read_text())
    assert "notification raise" in expected
    assert json.loads((tmp_path / "events-native.json").read_text()) == expected
