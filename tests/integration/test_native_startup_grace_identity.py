"""Reject changed startup grace before reusing saved passing test evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.test_native_worker_start_identity import _arguments, _project
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize(
    ("grace", "diagnostic"),
    [
        ("5000", "native startup grace requires demand scheduling"),
        ("nan", "native startup grace must be integer milliseconds from 0 to 60000"),
    ],
)
def test_rejected_startup_grace_cannot_reuse_saved_green_run(
    tmp_path: Path, grace: str, diagnostic: str
) -> None:
    project = _project(tmp_path)
    environment = {
        "VFY_WORKER_START": None,
        "VFY_NATIVE_SCHEDULING": "file",
        "VFY_WORKER_COLLECTION": None,
        "VFY_NATIVE_STARTUP_GRACE_MS": None,
    }
    saved = project.run_vfy(*_arguments(2), env=environment)
    saved.expect(0)
    green_path = project.root / ".vfy/replay.coverage"
    green = green_path.read_bytes()
    rejected = project.run_vfy(
        *_arguments(2),
        env={**environment, "VFY_NATIVE_STARTUP_GRACE_MS": grace},
    )
    text = rejected.stdout + rejected.stderr
    assert rejected.returncode == 4, text
    assert diagnostic in text
    assert "Reused 4 tests from passing baseline" not in text
    assert green_path.read_bytes() == green
    assert [(project.root / f".vfy/executions-{value}").read_text() for value in range(4)] == [
        "called\n"
    ] * 4
