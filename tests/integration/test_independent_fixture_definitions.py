"""Fixture declarations preserve object identity while Rust owns their execution."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from textwrap import indent

import pytest

from tests.support import ENGINES, VfyProject
from tests.support.platforms import requires_native_pytest


def _run(project: VfyProject, engine: str, *arguments: str, status: int = 0) -> None:
    result = project.run_engine(engine, *arguments, stock=("-q",))
    assert result.returncode == status, result.stdout + result.stderr


def _run_descriptor(project: VfyProject, engine: str, method: str) -> None:
    # CPython 3.13 removes classmethod's chaining of the fixture descriptor.
    # Pytest then omits this declaration and reports a missing fixture at setup.
    missing = method == "class_outer" and sys.version_info >= (3, 13)
    _run(project, engine, "--junitxml=descriptors.xml", status=1 if missing else 0)
    cases = ET.parse(project.root / "descriptors.xml").findall("./testsuite/testcase")
    assert len(cases) == 2
    outcomes = [
        next((part.tag for part in case if part.tag in {"error", "failure", "skipped"}), "passed")
        for case in cases
    ]
    assert outcomes == ["error" if missing else "passed"] * 2
    if missing:
        for case in cases:
            error = case.find("error")
            assert error is not None
            assert "fixture 'value' not found" in (error.text or "")


_PROTOCOL = """import inspect, json
from pathlib import Path
import pytest

def capture(action):
    try:
        action()
    except BaseException as error:
        return [type(error).__name__, str(error), getattr(error, 'pytrace', None)]
    raise AssertionError('declaration unexpectedly succeeded')

events = []
def raw(request):
    'fixture description'
    events.append('called')
    return request
raw.custom = {'token': 7}
definition = pytest.fixture(name='chosen')(raw)
class Owner:
    @pytest.fixture(name='member')
    def factory(self, request):
        return self
owner = Owner()
bound = owner.factory
marker = pytest.fixture(scope='session', params=iter([1,2]), ids=iter(['a','b']))
def named(): pass
def keyword_target(): pass

result = {
    'definition_type': type(definition).__name__,
    'marker_type': type(marker).__name__,
    'name': definition.name,
    '__name__': definition.__name__,
    'doc': definition.__doc__,
    'custom': definition.custom,
    'signature': str(inspect.signature(definition)),
    'bound_signature': str(inspect.signature(bound)),
    'unwrapped': definition.__wrapped__ is raw,
    'wrapped_function': definition._get_wrapped_function() is raw,
    'bound_self': bound._get_wrapped_function().__self__ is owner,
    'class_descriptor_identity': Owner.factory is Owner.factory,
    'repr_prefix': repr(definition).startswith('<pytest_fixture(<function raw at '),
    'marker': [marker.scope, marker.params, marker.autouse, marker.ids, marker.name],
    'direct': capture(lambda: definition()),
    'wrong_arguments': capture(lambda: definition(1, unexpected=True)),
    'double': capture(lambda: pytest.fixture(definition)),
    'class': capture(lambda: pytest.fixture(Owner)),
    'mark_before': capture(lambda: pytest.fixture(pytest.mark.example(named))),
    'mark_after': capture(lambda: pytest.mark.example(pytest.fixture(raw))),
    'keyword': type(pytest.fixture(fixture_function=keyword_target)).__name__,
    'events': events,
}
Path('protocol.json').write_text(json.dumps(result))
"""


def test_fixture_object_protocol_matches_stock(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("probe.py", _PROTOCOL)
    oracle = project.run_python("probe.py")
    oracle.expect(0)
    expected = json.loads((tmp_path / "protocol.json").read_text())
    assert expected["definition_type"] == "FixtureFunctionDefinition"
    assert expected["bound_self"]
    assert not expected["class_descriptor_identity"]
    assert expected["events"] == []
    actual = project.run_python(
        "-c",
        "from vfy.independent.facade import install_pytest_facade; "
        "install_pytest_facade(); "
        "import runpy; runpy.run_path('probe.py', run_name='__main__')",
    )
    actual.expect(0)
    assert json.loads((tmp_path / "protocol.json").read_text()) == expected


_METHODS = {
    "instance": """@pytest.fixture
def value(self):
    return type(self).__name__
""",
    "static_outer": """@staticmethod
@pytest.fixture
def value():
    return 'TestBinding'
""",
    "static_inner": """@pytest.fixture
@staticmethod
def value():
    return 'TestBinding'
""",
    "class_outer": """@classmethod
@pytest.fixture
def value(cls):
    return cls.__name__
""",
    "class_inner": """@pytest.fixture
@classmethod
def value(cls):
    return cls.__name__
""",
}


@requires_native_pytest
@ENGINES
@pytest.mark.parametrize("method", list(_METHODS))
def test_fixture_descriptors_bind_before_native_execution(
    tmp_path: Path, engine: str, method: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_binding.py",
        "import pytest\n\nclass TestBinding:\n"
        + indent(_METHODS[method], "    ")
        + """
    def test_first(self, value):
        assert value == type(self).__name__
    def test_second(self, value):
        assert value == type(self).__name__
""",
    )
    _run_descriptor(project, engine, method)


@requires_native_pytest
@ENGINES
@pytest.mark.parametrize("method", ["class_outer", "class_inner", "instance"])
def test_inherited_fixture_binds_the_actual_derived_class(
    tmp_path: Path, engine: str, method: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_inherited.py",
        "import pytest\n\nclass Base:\n"
        + indent(_METHODS[method], "    ")
        + """
    def test_value(self, value):
        assert value == type(self).__name__
class TestFirst(Base): pass
class TestSecond(Base): pass
""",
    )
    _run_descriptor(project, engine, method)


@requires_native_pytest
@ENGINES
@pytest.mark.parametrize("declaration", ["distinct", "alias"])
def test_fixture_registration_keeps_distinct_names_separate(
    tmp_path: Path, engine: str, declaration: str
) -> None:
    project = VfyProject(tmp_path)
    declarations = (
        "first = pytest.fixture(name='first')(source)\n"
        "second = pytest.fixture(name='second')(source)\n"
        if declaration == "distinct"
        else "first = pytest.fixture(source)\nsecond = first\n"
    )
    project.write(
        "test_names.py",
        """import pytest
calls = []
def source():
    calls.append('called')
    return len(calls)
"""
        + declarations
        + """
def test_values(first, second):
    assert (first, second) == (1, 2)
    assert calls == ['called', 'called']
""",
    )
    _run(project, engine)


@requires_native_pytest
@ENGINES
def test_callable_scope_uses_the_actual_config_before_execution(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_scope.py",
        """import pytest
events = []
def choose_scope(*, fixture_name, config):
    assert fixture_name == 'value'
    assert config.rootpath.name
    events.append('scope')
    return 'session'
@pytest.fixture(scope=choose_scope)
def value():
    events.append('setup')
    yield 7
    events.append('teardown')
def test_first(value):
    assert value == 7
    assert events == ['scope', 'setup']
def test_second(value):
    assert value == 7
    assert events == ['scope', 'setup']
""",
    )
    _run(project, engine)


@requires_native_pytest
@ENGINES
def test_imported_session_fixture_has_each_modules_registration(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "support.py",
        """import pytest
calls = []
@pytest.fixture(scope='session')
def value():
    calls.append('setup')
    return len(calls)
""",
    )
    for name, expected in [("a", 1), ("b", 2)]:
        project.write(
            f"test_{name}.py",
            f"from support import value\ndef test_value(value): assert value == {expected}\n",
        )
    _run(project, engine)


@requires_native_pytest
@ENGINES
@pytest.mark.parametrize("failure", ["raises", "return"])
def test_callable_scope_failures_are_collection_errors(
    tmp_path: Path, engine: str, failure: str
) -> None:
    project = VfyProject(tmp_path)
    action = "raise RuntimeError('scope failed')" if failure == "raises" else "return 7"
    project.write(
        "test_scope_error.py",
        "import pytest\nfrom pathlib import Path\n"
        f"def choose(*, fixture_name, config):\n    {action}\n"
        """@pytest.fixture(scope=choose)
def value(): return 7
def test_never_runs(value): Path('body').touch()
""",
    )
    _run(project, engine, status=2)
    assert not (tmp_path / "body").exists()


@requires_native_pytest
@ENGINES
def test_test_named_fixture_is_not_a_test_body(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_names.py",
        """import pytest
@pytest.fixture
def test_factory():
    return 7
def test_uses_fixture(test_factory):
    assert test_factory == 7
""",
    )
    _run(project, engine)
