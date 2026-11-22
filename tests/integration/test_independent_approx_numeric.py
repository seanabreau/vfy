"""Approximate comparison diagnostics retain the values' numeric precision."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.differential import run_matching
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_decimal_sequence_differences_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_numeric.py",
        """from decimal import Decimal
import pytest


def test_large():
    assert [Decimal('2e400')] == pytest.approx([Decimal('1e400')])


def test_precision():
    assert [Decimal('1.123456789012345678901234567')] == pytest.approx([Decimal('1')])


def test_mixed():
    assert [3.0, Decimal('2.123456789012345678901234567')] == pytest.approx(
        [2.0, Decimal('1')]
    )


def test_close():
    assert [Decimal('1.00000001'), 2.0] == pytest.approx([Decimal('1'), 2.0])
""",
    )

    result = run_matching(project)

    assert result.returncode == 1
    assert "Max absolute difference: 1E+400" in result.stdout
    assert "Max absolute difference: 0.123456789012345678901234567" in result.stdout
    assert "3 failed, 1 passed" in result.stdout
