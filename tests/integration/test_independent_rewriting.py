"""Collected assertion diagnostics match the pinned pytest compiler."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject
from tests.support.isolated_package import isolated_vfy_path
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_SOURCE = isolated_vfy_path()


def test_assertion_values_messages_and_helper_registration_match_pytest(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\npytest.register_assert_rewrite('helpers')\n",
    )
    project.write("helpers/__init__.py", "")
    project.write(
        "helpers/checks.py",
        "def check(left, right):\n    assert left == right, 'helper failure'\n",
    )
    project.write(
        "test_assertions.py",
        """from __future__ import annotations
import json
from pathlib import Path
import pytest
from helpers.checks import check


def test_observations():
    observed = []
    left, right = 1, 2
    checks = [lambda: check(left, right)]
    for check_function in checks:
        with pytest.raises(AssertionError) as info:
            check_function()
        observed.append(str(info.value))
    with pytest.raises(AssertionError) as info:
        assert left == right
    observed.append(str(info.value))
    with pytest.raises(AssertionError) as info:
        assert left + 1 == right + 1
    observed.append(str(info.value))
    with pytest.raises(AssertionError) as info:
        assert not right, 'message'
    observed.append(str(info.value))
    with pytest.raises(AssertionError) as info:
        assert left == 0 or right == 0
    observed.append(str(info.value))
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    oracle = project.run_pytest("-q", load_xdist=False)
    oracle.expect(0)
    observations = json.loads((tmp_path / "observed.json").read_text())
    (tmp_path / "observed.json").unlink()
    actual = project.run_engine("native")
    actual.expect(0)
    assert json.loads((tmp_path / "observed.json").read_text()) == observations


def test_registered_helpers_are_rewritten_without_warnings(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("helpers/__init__.py", "")
    project.write("helpers/checks.py", "def check(value):\n    assert value == 2\n")
    project.write("conftest.py", "import pytest\n\npytest.register_assert_rewrite('helpers')\n")
    project.write(
        "test_helpers.py",
        "from helpers.checks import check\n\ndef test_helper():\n    check(1)\n",
    )
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONWARNINGS": "always"}
    for engine in ("stock", "native"):
        result = project.run_engine(engine, "-q", "-p", "no:hypothesispytest", env=environment)
        result.expect(1)
        assert result.stderr == ""
        assert "assert 1 == 2" in result.stdout
        assert "PytestAssertRewriteWarning" not in result.stdout
