"""The actual doctest declaration follows ordinary session fixture lifetimes."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import ENGINES, VfyProject
from tests.support.independent_private_aliases import PRIVATE_ALIASES
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@ENGINES
def test_namespace_session_owner_shares_mutations_and_dynamic_requests(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import json, pytest
from pathlib import Path
setups=[]
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.argname == 'doctest_namespace':
        setups.append([fixturedef.scope, fixturedef.func.__name__])
@pytest.fixture(scope='session')
def broad(request):
    return request.getfixturevalue('doctest_namespace')
def pytest_sessionfinish(session):
    Path('setups.json').write_text(json.dumps(setups))
""",
    )
    project.write(
        "test_a.py",
        """from pathlib import Path
def test_first(broad, request):
    namespace = request.getfixturevalue('doctest_namespace')
    assert namespace is broad and namespace == {} and type(namespace) is dict
    namespace['entries'] = ['first']
    Path('first-body').touch()
""",
    )
    project.write(
        "test_b.py",
        """import json
from pathlib import Path
def test_second(doctest_namespace, broad, request):
    assert doctest_namespace is broad is request.getfixturevalue('doctest_namespace')
    assert doctest_namespace == {'entries': ['first']}
    doctest_namespace['entries'].append('second')
    Path('namespace.json').write_text(json.dumps(doctest_namespace))
""",
    )
    result = project.run_engine(engine, "-q")
    result.expect(0)
    assert (tmp_path / "first-body").is_file()
    assert json.loads((tmp_path / "namespace.json").read_text()) == {"entries": ["first", "second"]}
    assert json.loads((tmp_path / "setups.json").read_text()) == [["session", "doctest_namespace"]]


def test_namespace_fixture_runs_without_importing_upstream_pytest_modules(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("private_aliases.py", PRIVATE_ALIASES)
    project.write(
        "test_namespace.py",
        """import sys
from pathlib import Path
from private_aliases import assert_private_aliases
def test_guard(doctest_namespace):
    assert doctest_namespace == {}
    assert not any(name.split('.')[0] in {'pluggy', 'packaging'}
                   for name in sys.modules)
    assert_private_aliases()
    Path('guard-body').touch()
""",
    )
    result = project.run_engine("native", "-q")
    result.expect(0)
    assert (tmp_path / "guard-body").is_file()


def test_doctest_public_item_loads_on_access_and_matches_collected_item(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import pytest

def pytest_configure(config):
    assert 'DoctestItem' in dir(pytest)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        classes = list(pool.map(lambda _: pytest.DoctestItem, range(8)))
    assert all(cls is classes[0] for cls in classes)
    namespace = {}
    exec("from pytest import *", namespace)
    assert namespace["DoctestItem"] is classes[0]
    assert namespace["fixture"] is pytest.fixture

def pytest_collection_modifyitems(items):
    assert any(type(item) is pytest.DoctestItem for item in items)
""",
    )
    project.write("test_example.txt", ">>> 1 + 1\n2\n")
    result = project.run_engine("native", "-q", ".")
    result.expect(0)
    assert "1 passed" in result.stdout
