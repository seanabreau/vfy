"""Compare fixture cache and node ownership with stock pytest."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_HEADER = """from pathlib import Path
import pytest

def record(event):
    with Path('events.txt').open('a', encoding='utf-8') as stream:
        stream.write(event + '\\n')

"""


def _run_case(
    root: Path, files: dict[str, str], *options: str
) -> dict[str, tuple[int, list[str], str]]:
    observed: dict[str, tuple[int, list[str], str]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(root / engine)
        project.write("pytest.ini", "[pytest]\n")
        for name, source in files.items():
            project.write(name, source)
        environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = (
            project.run_python("-m", "pytest", "-q", *options, env=environment)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=1",
                "--no-cov",
                "-q",
                *options,
                env=environment,
            )
        )
        path = project.root / "events.txt"
        events = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        observed[engine] = (result.returncode, events, result.stdout + result.stderr)
    return observed


def test_dynamic_session_fixture_outlives_module_cleanup(tmp_path: Path) -> None:
    source = (
        _HEADER
        + """@pytest.fixture(scope='module')
def observer(request):
    record('observer setup')
    yield
    record('observer teardown')
    assert request.getfixturevalue('shared') == 42

@pytest.fixture(scope='session')
def shared():
    record('shared setup')
    yield 42
    record('shared teardown')

def test_case(observer, request):
    assert request.getfixturevalue('shared') == 42
"""
    )
    expected = ["observer setup", "shared setup", "observer teardown", "shared teardown"]
    for status, events, transcript in _run_case(tmp_path, {"test_case.py": source}).values():
        assert (status, events) == (0, expected), transcript


def test_indirect_package_scope_preserves_defining_package_owner(tmp_path: Path) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/conftest.py": _HEADER
        + """@pytest.fixture
def resource(request):
    record(f'setup {request.node.nodeid}')
    yield request.param
    record('teardown')
    Path('released.txt').touch()
""",
        "pkg/test_case.py": """import pytest

@pytest.mark.parametrize('resource', [42], indirect=True, scope='package')
def test_resource(resource):
    assert resource == 42
""",
        "test_after.py": _HEADER
        + """def test_after():
    assert Path('released.txt').exists()
    record('outside package')
""",
    }
    expected = ["setup pkg", "teardown", "outside package"]
    for status, events, transcript in _run_case(
        tmp_path, files, "pkg/test_case.py", "test_after.py"
    ).values():
        assert (status, events) == (0, expected), transcript


@pytest.mark.parametrize("first_scope", ["module", "session"])
def test_indirect_scope_change_retains_original_cache_owner(
    tmp_path: Path, first_scope: str
) -> None:
    second_scope = "session" if first_scope == "module" else "module"
    conftest = (
        _HEADER
        + """shared = object()
values = []

@pytest.fixture
def value(request):
    result = object()
    values.append(result)
    record('setup')
    yield result
    record('teardown')
"""
    )
    first_module = f"""import pytest
import conftest

@pytest.mark.parametrize('value', [conftest.shared], indirect=True, scope='{first_scope}')
def test_first(value):
    assert value is conftest.values[0]

@pytest.mark.parametrize('value', [conftest.shared], indirect=True, scope='{second_scope}')
def test_second(value):
    assert value is conftest.values[0]
    assert len(conftest.values) == 1
"""
    expected_count = 2 if first_scope == "module" else 1
    second_module = f"""import pytest
import conftest

@pytest.mark.parametrize('value', [conftest.shared], indirect=True, scope='session')
def test_third(value):
    assert value is conftest.values[-1]
    assert len(conftest.values) == {expected_count}
"""
    expected = ["setup", "teardown"] * expected_count
    files = {
        "conftest.py": conftest,
        "test_a.py": first_module,
        "test_b.py": second_module,
    }
    for status, events, transcript in _run_case(tmp_path, files).values():
        assert (status, events) == (0, expected), transcript


def test_changed_dependency_invalidates_parent_before_parameter_comparison(tmp_path: Path) -> None:
    source = (
        _HEADER
        + """class Parameter:
    def __eq__(self, other):
        raise TypeError('parent parameter must not be compared after dependency changes')

@pytest.fixture(scope='module', params=[1, 2])
def dependency(request):
    record(f'dependency setup {request.param}')
    yield request.param
    record(f'dependency teardown {request.param}')

@pytest.fixture(scope='module', params=[Parameter()])
def value(dependency, request):
    record(f'value setup {dependency}')
    yield dependency
    record(f'value teardown {dependency}')

def test_case(value):
    assert value in (1, 2)
"""
    )
    expected = [
        "dependency setup 1",
        "value setup 1",
        "value teardown 1",
        "dependency teardown 1",
        "dependency setup 2",
        "value setup 2",
        "value teardown 2",
        "dependency teardown 2",
    ]
    for status, events, transcript in _run_case(tmp_path, {"test_case.py": source}).values():
        assert (status, events) == (0, expected), transcript


@pytest.mark.parametrize("scope", ["function", "module", "session"])
def test_node_finalizers_share_owner_lifo_with_fixture_finalizers(
    tmp_path: Path, scope: str
) -> None:
    source = (
        _HEADER
        + f"""@pytest.fixture(scope='{scope}')
def first(request):
    request.node.addfinalizer(lambda: record('first node'))
    request.addfinalizer(lambda: record('first request'))
    yield
    record('first yield')

@pytest.fixture(scope='{scope}')
def second(request):
    request.node.addfinalizer(lambda: record('second node'))
    request.addfinalizer(lambda: record('second request'))
    yield
    record('second yield')

def test_case(first, second, request):
    request.node.addfinalizer(lambda: record('test node'))
    request.addfinalizer(lambda: record('test request'))
"""
    )
    expected = [
        "test request",
        "test node",
        "second yield",
        "second request",
        "second node",
        "first yield",
        "first request",
        "first node",
    ]
    for status, events, transcript in _run_case(tmp_path, {"test_case.py": source}).values():
        assert (status, events) == (0, expected), transcript


def test_node_finalizer_without_fixture_runs_at_its_owner_boundary(tmp_path: Path) -> None:
    source = (
        _HEADER
        + """def test_first(request):
    request.session.addfinalizer(lambda: record('session node'))
    request.node.addfinalizer(lambda: record('test node'))

def test_second():
    record('second body')
"""
    )
    for status, events, transcript in _run_case(tmp_path, {"test_case.py": source}).values():
        assert (status, events) == (0, ["test node", "second body", "session node"]), transcript


@pytest.mark.parametrize(
    "lookup_scope", [None, "function", "module"], ids=["node", "function", "module"]
)
def test_duplicate_file_owners_reject_escaped_requests_with_equal_nodeids(
    tmp_path: Path, lookup_scope: str | None
) -> None:
    lookup = (
        "saved[0].node.addfinalizer(lambda: record('must not run'))"
        if lookup_scope is None
        else "saved[0].getfixturevalue('extra')"
    )
    source = (
        _HEADER
        + f"""saved = []

@pytest.fixture(scope='{lookup_scope or "function"}')
def extra(request):
    record('extra setup')
    request.addfinalizer(lambda: record('extra teardown'))
    return 42

@pytest.fixture(scope='module')
def resource(request):
    record('module setup')
    request.addfinalizer(lambda: record('module teardown'))

def test_case(resource, request):
    record('body')
    if saved:
        try:
            {lookup}
        except (AssertionError, pytest.FixtureLookupError):
            record('escaped rejected')
        else:
            pytest.fail('inactive request accepted')
    saved.append(request)
"""
    )
    observed = _run_case(
        tmp_path, {"test_case.py": source}, "--keep-duplicates", "test_case.py", "test_case.py"
    )
    stock_status, stock_events, stock_transcript = observed["pytest"]
    assert stock_status == 0, stock_transcript
    assert stock_events.count("module teardown") == 2
    assert "escaped rejected" in stock_events
    status, events, transcript = observed["pytest-native"]
    assert (status, events) == (stock_status, stock_events), transcript


def test_parametrized_node_callbacks_bind_each_active_function_item(tmp_path: Path) -> None:
    source = (
        _HEADER
        + """@pytest.mark.parametrize('value', [1, 2])
def test_case(request, value):
    request.node.addfinalizer(lambda: record(request.node.name))
"""
    )
    for status, events, transcript in _run_case(tmp_path, {"test_case.py": source}).values():
        assert (status, events) == (0, ["test_case[1]", "test_case[2]"]), transcript


def test_fixture_finalizer_registration_during_teardown_drains_before_earlier_callbacks(
    tmp_path: Path,
) -> None:
    source = (
        _HEADER
        + """@pytest.fixture(scope='module')
def value(request):
    def late():
        record('late')
        request.addfinalizer(lambda: record('added'))
    request.addfinalizer(lambda: record('first'))
    request.addfinalizer(late)
    yield
    record('yield')
    with pytest.raises(AssertionError):
        request.node.addfinalizer(lambda: record('must not run'))

def test_case(value):
    pass
"""
    )
    for status, events, transcript in _run_case(tmp_path, {"test_case.py": source}).values():
        assert (status, events) == (0, ["yield", "late", "added", "first"]), transcript


@pytest.mark.parametrize("mode", ["normal", "maxfail", "interrupt", "finalizer-error"])
def test_node_lifecycle_adapter_restores_original_setup_state_on_completion(
    tmp_path: Path, mode: str
) -> None:
    conftest = """from pathlib import Path
original = None

def pytest_collection_modifyitems(session):
    global original
    original = session._setupstate

def pytest_sessionfinish(session):
    assert session._setupstate is original
    Path('restored.txt').write_text('restored', encoding='utf-8')
"""
    body = {
        "normal": "pass",
        "maxfail": "assert False",
        "interrupt": "raise KeyboardInterrupt()",
        "finalizer-error": "pass",
    }[mode]
    finalizer = "raise ValueError('teardown failure')" if mode == "finalizer-error" else "pass"
    source = (
        _HEADER
        + f"""@pytest.fixture(scope='session')
def value(request):
    request.node.addfinalizer(lambda: record('node'))
    yield
    record('fixture')
    {finalizer}

def test_first(value):
    {body}

def test_second(value):
    record('second body')
"""
    )
    options = ("-x",) if mode == "maxfail" else ()
    expected_status = {"normal": 0, "maxfail": 1, "interrupt": 2, "finalizer-error": 1}[mode]
    expected_events = (
        ["second body", "fixture", "node"]
        if mode in {"normal", "finalizer-error"}
        else ["fixture", "node"]
    )
    files = {"conftest.py": conftest, "test_case.py": source}
    for engine, (status, events, transcript) in _run_case(tmp_path, files, *options).items():
        assert (status, events) == (expected_status, expected_events), transcript
        assert (tmp_path / engine / "restored.txt").read_text(encoding="utf-8") == "restored"


def test_shared_owner_keys_preserve_compact_case_and_class_cleanup(tmp_path: Path) -> None:
    conftest = (
        _HEADER
        + """@pytest.fixture(scope='module')
def module_owner(request):
    owner = request.node.nodeid
    record('module setup ' + owner)
    yield owner
    record('module teardown ' + owner)

@pytest.fixture(scope='class')
def class_owner(request):
    owner = request.node.nodeid
    record('class setup ' + owner)
    request.node.addfinalizer(lambda: record('class node finalizer ' + owner))
    yield owner
    record('class teardown ' + owner)
"""
    )
    source = """import pytest
from conftest import record

@pytest.mark.parametrize('value', [0, 1])
def test_free(value, request, module_owner, class_owner):
    assert module_owner == __name__ + '.py'
    assert class_owner == __name__ + '.py::test_free[' + str(value) + ']'
    assert request.node.nodeid == class_owner
    record('call ' + request.node.nodeid)

class TestCases:
    @pytest.mark.parametrize('value', [0, 1])
    def test_method(self, value, request, module_owner, class_owner):
        assert module_owner == __name__ + '.py'
        assert class_owner == __name__ + '.py::TestCases'
        record('call ' + request.node.nodeid)
"""
    observations = _run_case(
        tmp_path,
        {"conftest.py": conftest, "test_a.py": source, "test_b.py": source},
    )
    for status, _, transcript in observations.values():
        assert status == 0, transcript
        assert "8 passed" in transcript, transcript
    assert observations["pytest-native"][1] == observations["pytest"][1]
    events = observations["pytest"][1]
    assert [event for event in events if event.startswith("class setup ")] == [
        "class setup test_a.py::test_free[0]",
        "class setup test_a.py::test_free[1]",
        "class setup test_a.py::TestCases",
        "class setup test_b.py::test_free[0]",
        "class setup test_b.py::test_free[1]",
        "class setup test_b.py::TestCases",
    ]
    assert events[-1] == "module teardown test_b.py"
