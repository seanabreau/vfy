"""Reject changed worker startup before reusing saved passing test evidence."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


def _project(root: Path) -> VfyProject:
    project = VfyProject(root)
    project.write("src/app.py", "def read_value(value):\n    return value\n")
    project.write(
        "test_cases.py",
        "from pathlib import Path\nimport pytest\nfrom src.app import read_value\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_case(value):\n"
        "    assert read_value(value) == value\n"
        "    path = Path(f'.vfy/executions-{value}')\n"
        "    path.parent.mkdir(exist_ok=True)\n"
        "    with path.open('a') as stream:\n        stream.write('called\\n')\n",
    )
    project.git_init()
    return project


def _arguments(workers: int, engine: str = "pytest-native") -> tuple[str, ...]:
    return (
        "test",
        f"--engine={engine}",
        f"--workers={workers}",
        "--source=src",
        "--cov-report=",
        "-q",
    )


@requires_native_pytest
@pytest.mark.parametrize(
    ("mode", "workers", "collection", "diagnostic"),
    [
        ("unknown", 2, None, "VFY_WORKER_START must be fork or exec"),
        ("", 2, None, "VFY_WORKER_START must be fork or exec"),
        (
            "fork",
            1,
            None,
            "collected fork workers require Linux and at least two explicit workers",
        ),
        ("fork", 2, "1", "collected fork workers require controller collection"),
    ],
)
def test_rejected_worker_start_cannot_reuse_saved_green_run(
    tmp_path: Path, mode: str, workers: int, collection: str | None, diagnostic: str
) -> None:
    project = _project(tmp_path)
    saved = project.run_vfy(
        *_arguments(workers),
        env={
            "VFY_WORKER_START": None,
            "VFY_WORKER_COLLECTION": None,
        },
    )
    saved.expect(0)
    green_path = project.root / ".vfy/replay.coverage"
    green = green_path.read_bytes()
    rejected = project.run_vfy(
        *_arguments(workers),
        env={
            "VFY_WORKER_START": mode,
            "VFY_WORKER_COLLECTION": collection,
        },
    )
    text = rejected.stdout + rejected.stderr
    assert rejected.returncode != 0, text
    if mode == "fork" and sys.platform != "linux":
        diagnostic = "collected fork workers require Linux and at least two explicit workers"
    assert diagnostic in text
    assert "Reused 4 tests from passing baseline" not in text
    assert green_path.read_bytes() == green
    assert [(project.root / f".vfy/executions-{value}").read_text() for value in range(4)] == [
        "called\n"
    ] * 4


@pytest.mark.parametrize(
    ("engine", "mode"),
    [pytest.param("pytest-native", "exec", marks=requires_native_pytest), ("pytest", "unknown")],
)
def test_default_or_unused_worker_start_preserves_saved_run_identity(
    tmp_path: Path, engine: str, mode: str
) -> None:
    project = _project(tmp_path)
    for index, requested in enumerate((None, mode)):
        result = project.run_vfy(
            *_arguments(2, engine),
            env={
                "VFY_WORKER_START": requested,
                "VFY_WORKER_COLLECTION": None,
            },
        )
        text = result.stdout + result.stderr
        assert result.returncode == 0, text
        assert ("Reused 4 tests from passing baseline" in text) is bool(index)
    assert [(project.root / f".vfy/executions-{value}").read_text() for value in range(4)] == [
        "called\n"
    ] * 4
