"""Assertion representations use the live configured verbosity at the public CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize(
    ("arguments", "ini"),
    [
        ([], ""),
        (["-v"], ""),
        (["-vv"], ""),
        (["-vvv"], ""),
        (["-q"], ""),
        (["-vv"], "verbosity_assertions = 0"),
        ([], "verbosity_assertions = 1"),
        (["-q"], "verbosity_assertions = 2"),
        (["-v"], "verbosity_assertions = auto"),
        (["-vv", "-o", "verbosity_assertions=1"], "verbosity_assertions = 0"),
        (["-vv", "-o", "verbosity_assertions=0"], "verbosity_assertions = 2"),
        (["-o", "verbosity_assertions=2"], "verbosity_assertions = 0"),
        (["-vv", "-o", "verbosity_assertions=auto"], "verbosity_assertions = 0"),
    ],
)
def test_assertion_verbosity_matches_the_consumed_configuration(
    tmp_path: Path, arguments: list[str], ini: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", f"[pytest]\n{ini}\n")
    project.write(
        "test_explanations.py",
        """import json
from pathlib import Path

class FalseValue:
    def __bool__(self): return False
    def __repr__(self): return 'HEAD' + 'a' * 3000 + 'TAIL'

value = FalseValue()
def returned(argument): return value
class Owner:
    member = value
    def __repr__(self): return 'Owner'
owner = Owner()

def test_observed():
    observed = {}
    try:
        assert value
    except AssertionError as error:
        observed['value'] = str(error)
    try:
        assert returned(value)
    except AssertionError as error:
        observed['call'] = str(error)
    try:
        assert owner.member
    except AssertionError as error:
        observed['attribute'] = str(error)
    left = 'prefix' * 30 + 'LEFT' + 'suffix' * 30
    right = 'prefix' * 30 + 'RIGHT' + 'suffix' * 30
    try:
        assert left == right
    except AssertionError as error:
        observed['string'] = str(error)
    left = '\\n'.join('left ' + str(index) for index in range(20))
    right = '\\n'.join('right ' + str(index) for index in range(20))
    try:
        assert left == right
    except AssertionError as error:
        observed['multiline'] = str(error)
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    stock = project.run_pytest(*arguments, "test_explanations.py", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "observed.json").read_text())
    (tmp_path / "observed.json").unlink()
    actual = project.run_vfy(
        "test", "--engine=native", "--no-cov", *arguments, "test_explanations.py"
    )
    actual.expect(0)
    assert json.loads((tmp_path / "observed.json").read_text()) == expected


@pytest.mark.parametrize("fails", [False, True])
def test_invalid_assertion_verbosity_is_evaluated_only_for_a_failed_assertion(
    tmp_path: Path, fails: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nverbosity_assertions = invalid\n")
    project.write(
        "test_invalid.py",
        f"""import json
from pathlib import Path

def test_observed():
    observed = None
    try:
        assert {not fails!r}
    except ValueError as error:
        observed = [type(error).__name__, str(error)]
    Path('observed.json').write_text(json.dumps(observed))
""",
    )
    stock = project.run_pytest("test_invalid.py", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "observed.json").read_text())
    assert (expected is not None) is fails
    (tmp_path / "observed.json").unlink()
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "test_invalid.py")
    actual.expect(0)
    assert json.loads((tmp_path / "observed.json").read_text()) == expected


def test_assertion_verbosity_reads_live_config_after_collection_hooks(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "def pytest_collection_modifyitems(config): config.option.verbose = 2\n",
    )
    project.write(
        "test_live.py",
        """import json
from pathlib import Path

class FalseValue:
    def __bool__(self): return False
    def __repr__(self): return 'a' * 3000

def test_observed(request):
    value = FalseValue()
    try:
        assert value
    except AssertionError as error:
        Path('observed.json').write_text(json.dumps([
            request.config.get_verbosity(),
            request.config.get_verbosity('assertions'),
            request.config.get_verbosity('unregistered'),
            str(error),
        ]))
""",
    )
    stock = project.run_pytest("-q", "test_live.py", load_xdist=False)
    stock.expect(0)
    expected = json.loads((tmp_path / "observed.json").read_text())
    assert expected[:3] == [2, 2, 2]
    assert len(expected[3]) > 3000
    (tmp_path / "observed.json").unlink()
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "-q", "test_live.py")
    actual.expect(0)
    assert json.loads((tmp_path / "observed.json").read_text()) == expected
