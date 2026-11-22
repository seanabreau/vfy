"""Mutation cache failures preserve the existing cache on both engine routes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param("pytest-native", marks=requires_native_pytest),
        pytest.param(
            "native",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="independent mutations require Linux"
            ),
        ),
    ],
)
def test_corrupt_cache_fails_without_replacement(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value():\n    return 1\n")
    project.write(
        "test_app.py", "from app import value\n\ndef test_value():\n    assert value() == 1\n"
    )
    args = (
        "mutate",
        f"--engine={engine}",
        "--all",
        "--source=app.py",
        "--operator=integer_plus_one",
        "--max-mutations=1",
        "--format=json",
    )
    first = project.run_vfy(*args)
    first.expect(0)
    cache = tmp_path / ".vfy" / "mutations.json"
    assert cache.is_file(), first.stdout + first.stderr
    damaged = b'{"schema": "vfy.mutation", broken'
    cache.write_bytes(damaged)

    result = project.run_vfy(*args)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "invalid mutation cache" in result.stderr, result.stdout + result.stderr
    assert cache.read_bytes() == damaged
