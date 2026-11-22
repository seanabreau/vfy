"""``pytest.approx`` and doctest ``NUMBER`` comparisons match pytest."""

from __future__ import annotations

from pathlib import Path

from tests.support import VfyProject
from tests.support.differential import run_matching
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_approx_collections_and_failure_explanations_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_approx.py",
        """from decimal import Decimal

import pytest


def test_sequences():
    assert [0.1 + 0.2, 1.0] == pytest.approx([0.3, 1.0])
    assert (1.0, 2.0000001) == pytest.approx((1.0, 2.0))


def test_mapping_and_decimal():
    assert {"a": 0.1 + 0.2} == pytest.approx({"a": 0.3})
    assert Decimal("1.0000001") == pytest.approx(Decimal("1.0"))


def test_mismatch():
    assert [1.0, 2.5] == pytest.approx([1.0, 2.0])
""",
    )

    result = run_matching(project)

    assert result.returncode == 1
    assert "2 passed" in result.stdout


def test_doctest_number_uses_only_the_absolute_tolerance(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "numbers.py",
        '''def value():
    """
    >>> 1000000.5  # doctest: +NUMBER
    1000000.0
    """
''',
    )

    result = run_matching(project, "--doctest-modules", "numbers.py")

    assert result.returncode == 1
