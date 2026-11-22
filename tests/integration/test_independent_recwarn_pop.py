"""recwarn.pop selects warnings and reports misses as pytest does."""

from __future__ import annotations

import re
from pathlib import Path

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _output(result: CommandResult) -> list[str]:
    return [re.sub(r" in [0-9.]+s$", "", line) for line in result.stdout.splitlines()]


def test_recwarn_pop_prefers_the_most_specific_match_like_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_recwarn_pop.py",
        """import warnings

import pytest


def test_pop_prefers_the_most_specific_match(recwarn):
    warnings.warn("deprecated", DeprecationWarning)
    warnings.warn("user", UserWarning)
    popped = recwarn.pop(Warning)
    print(popped.category.__name__, [record.category.__name__ for record in recwarn])


def test_pop_miss_message(recwarn):
    with pytest.raises(AssertionError) as error:
        recwarn.pop(RuntimeWarning)
    print(error.value)
""",
    )

    stock = project.run_pytest(
        "-q", "-s", "-p", "no:cacheprovider", "test_recwarn_pop.py", load_xdist=False
    )
    independent = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-s", "test_recwarn_pop.py"
    )

    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    assert _output(independent) == _output(stock)
