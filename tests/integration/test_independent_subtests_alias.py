"""Protocol aliases preserve the single ordinary subtests declaration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_OBSERVER = """import inspect,json
from pathlib import Path
import pytest

observed={}
owners={}

@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef,request):
    if fixturedef.argname!='subtests':
        return (yield)
    from _pytest import subtests as protocols
    declaration=protocols.subtests
    function=declaration._get_wrapped_function()
    assert fixturedef.func is function
    assert fixturedef.scope==request.scope=='function'
    assert request.fixturename=='subtests'
    assert list(inspect.signature(function).parameters)==['request']
    assert not inspect.isgeneratorfunction(function)
    marker=declaration._fixture_function_marker
    assert marker.scope=='function' and marker.name is None
    assert marker.params is None and marker.ids is None
    filename=Path(inspect.getsourcefile(function)).resolve()
    assert filename==Path(function.__code__.co_filename).resolve()
    assert filename.is_file()
    source=inspect.getsource(function)
    assert 'def subtests(' in source
    value=yield
    assert value._request is request
    owners.update(definition=fixturedef,request=request,value=value)
    observed.update(name=fixturedef.argname,scope=fixturedef.scope,
        arguments=list(inspect.signature(function).parameters),generator=False,
        definition_is_alias=True,request_identity=True,actual_source=True)
"""

_BODY = """import json
from pathlib import Path
import pytest
from conftest import observed,owners

def test_body(subtests,request):
    assert owners['value'] is subtests
    assert subtests._request is owners['request']
    assert subtests._request.node is request.node
    assert request.getfixturevalue('subtests') is subtests
    try:
        request.getfixturevalue('subtests_fixture')
    except pytest.FixtureLookupError as error:
        assert error.request is request and error.args[1] is request
        available=next(line for line in error.formatrepr().errorstring.splitlines()
                       if 'available fixtures:' in line)
        names=available.split('available fixtures:',1)[1].strip().split(', ')
        assert 'subtests' in names and 'subtests_fixture' not in names
        observed.update(cached_identity=True,alias_lookup='FixtureLookupError',
            alias_error_actual_request=True,available_subtests=True,available_alias=False)
    else:
        raise AssertionError('protocol adapter exposed a second subtests fixture')
    Path('observations.json').write_text(json.dumps(observed,indent=2))
"""


@pytest.mark.parametrize("engine", ["stock", pytest.param("native", marks=requires_native_pytest)])
def test_protocol_alias_preserves_fixture_definition_and_rejects_import_alias(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _OBSERVER)
    project.write("test_alias.py", _BODY)
    if engine == "stock":
        result = project.run_python("-m", "pytest", "-q")
    else:
        result = project.run_vfy(
            "test", "--engine=native", "--no-cov", "-q", "-p", "vfy.independent.outcomes"
        )
    project.write(f"{engine}.stdout", result.stdout)
    project.write(f"{engine}.stderr", result.stderr)
    result.expect(0)
    observed = json.loads((tmp_path / "observations.json").read_text())
    assert observed == {
        "name": "subtests",
        "scope": "function",
        "arguments": ["request"],
        "generator": False,
        "definition_is_alias": True,
        "request_identity": True,
        "actual_source": True,
        "cached_identity": True,
        "alias_lookup": "FixtureLookupError",
        "alias_error_actual_request": True,
        "available_subtests": True,
        "available_alias": False,
    }
