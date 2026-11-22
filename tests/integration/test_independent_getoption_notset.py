"""Config.getoption treats pytest's NOTSET sentinel as the absence of a default."""

from __future__ import annotations

import re
from pathlib import Path

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _output(result: CommandResult) -> list[str]:
    return [re.sub(r" in [0-9.]+s$", "", line) for line in result.stdout.splitlines()]


def test_getoption_with_explicit_notset_default_matches_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_getoption_notset.py",
        """from _pytest.compat import NOTSET


def test_explicit_notset_is_not_a_default(pytestconfig):
    try:
        value = pytestconfig.getoption("no_such_option", NOTSET)
    except ValueError as error:
        print("raised", error)
    else:
        print("returned", value is NOTSET)
""",
    )

    stock = project.run_pytest(
        "-q", "-s", "-p", "no:cacheprovider", "test_getoption_notset.py", load_xdist=False
    )
    independent = project.run_vfy(
        "test", "--engine=native", "--no-cov", "-q", "-s", "test_getoption_notset.py"
    )

    assert independent.returncode == stock.returncode, independent.stdout + independent.stderr
    assert _output(independent) == _output(stock)
