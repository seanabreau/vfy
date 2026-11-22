"""Independent pytest objects and builtin resources behave like stock pytest."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import VfyProject


def test_pytest_objects_work_in_a_collected_module(tmp_path: Path) -> None:
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_facade.py",
            """import pytest

def test_objects():
    @pytest.fixture(scope='module', autouse=True, name='answer', params=[2, 3], ids=['a', 'b'])
    def resource():
        return 42

    with pytest.raises(pytest.fail.Exception, match='called directly'):
        resource()
    assert resource._get_wrapped_function()() == 42

    @pytest.mark.skip(reason='optional')
    @pytest.mark.parametrize('value', [pytest.param(3, marks=pytest.mark.xfail, id='three')])
    def case(value):
        return value + 1

    assert case(3) == 4
    assert [mark.name for mark in case.pytestmark] == ['parametrize', 'skip']
    parameter = case.pytestmark[0].args[1][0]
    assert parameter.values == (3,)
    assert parameter.marks[0].name == 'xfail'
    assert parameter.id == 'three'

    @pytest.hookimpl(tryfirst=True, wrapper=True)
    def pytest_runtest_call(item):
        yield item

    assert list(pytest_runtest_call('item')) == ['item']
    assert pytest.CaptureFixture[str]
""",
        )
        result = project.run_engine(engine, "-q", "test_facade.py")
        assert result.returncode == 0, engine + result.stdout + result.stderr


def test_scalar_assertion_and_outcome_contracts_match_pytest(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_scalar.py",
            """import json
import math
import pytest as api

def test_contracts():
    observed = {}
    with api.raises(ValueError, match='bad value') as info:
        raise ValueError('bad value')
    observed['raises'] = [info.type.__name__, str(info.value), info.match('value')]
    with api.raises(ValueError, match='extra context'):
        error = ValueError('bad value')
        error.add_note('extra context')
        raise error
    try:
        with api.raises(ValueError):
            pass
    except BaseException as error:
        observed['did_not_raise'] = type(error).__name__
    try:
        with api.raises(ValueError):
            raise TypeError('wrong exception')
    except BaseException as error:
        observed['wrong_type'] = type(error).__name__
    observed['approx'] = [
        1.0000001 == api.approx(1),
        1.1 == api.approx(1),
        100.00001 == api.approx(100, abs=1e-8),
        0.00001 == api.approx(0, abs=1e-4),
        math.nan == api.approx(math.nan, nan_ok=True),
        math.nan == api.approx(math.nan),
        math.inf == api.approx(math.inf),
        -math.inf == api.approx(math.inf),
        (1+2.00000001j) == api.approx(1+2j),
    ]
    for name in ('skip', 'xfail', 'fail'):
        operation = getattr(api, name)
        try:
            operation('reason')
        except operation.Exception as error:
            observed[name] = [type(error).__name__, str(error)]
    observed['import'] = api.importorskip('math').__name__
    try:
        api.importorskip('vfy_missing_optional_module', reason='optional')
    except api.skip.Exception as error:
        observed['import_skip'] = str(error)
    Path = __import__('pathlib').Path
    Path('observed.json').write_text(json.dumps(observed, sort_keys=True))
""",
        )
        result = project.run_engine(engine, "-q", "test_scalar.py")
        assert result.returncode == 0, engine + result.stdout + result.stderr
        observations.append(json.loads((project.root / "observed.json").read_text()))
    assert observations[1] == observations[0]
