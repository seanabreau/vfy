"""Live independent function items own public request representation callbacks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_SOURCE = """import atexit,json,pytest
from pathlib import Path
observed={'cleanup':{}}
saved=[]
quoted_name="quoted'\\\\name Ω"
def publish():
    if not saved:return
    observed['retained']=[repr(value) for value in saved]
    Path('observations.json').write_text(json.dumps(observed))
atexit.register(publish)
@pytest.fixture
def plain():return 31
@pytest.fixture(name=quoted_name)
def quoted(request):
    yield request
    observed['cleanup']['quoted']=repr(request)
@pytest.fixture(scope='module')
def module_request(request):
    yield request
    observed['cleanup']['module']=repr(request)
@pytest.fixture(scope='session')
def session_request(request):
    yield request
    observed['cleanup']['session']=repr(request)
class Sentinel(Exception):pass
# CPython names the user-owned class in a non-string repr diagnostic.
class NonstringFunction(pytest.Function):pass
def test_control(request,module_request,session_request):
    item=request.node
    assert type(item) is pytest.Function
    assert isinstance(item,pytest.Item)
    assert pytest.Function is not pytest.Item
    assert pytest.Function.__name__=='Function'
    assert item in item.session.items
    requests=[request,request.getfixturevalue(quoted_name),module_request,session_request]
    saved.extend(requests)
    observed['normal']=[repr(value) for value in requests]
    if CASE=='custom_item':
        events=[]
        class CustomItem(pytest.Item):
            def runtest(self):pass
        custom=object.__new__(CustomItem)
        observed['custom']=[repr(custom)]
        custom.name='ordinary'
        observed['custom'].append(repr(custom))
        class Name:
            def __format__(self,spec):
                events.append(['format',spec])
                return 'live name'
        class NamedItem(CustomItem):
            @property
            def name(self):
                events.append('name')
                return Name()
        observed['custom'].append(repr(object.__new__(NamedItem)))
        observed['events']=events
        return
    if CASE=='subclass':
        original=type(item)
        original_name=item.name
        class SpecialFunction(pytest.Function):pass
        item.__class__=SpecialFunction
        item.name='renamed[actual]'
        try:
            assert type(item) is SpecialFunction and isinstance(item,pytest.Function)
            observed['subclass']=[repr(value) for value in requests]
        finally:
            item.__class__=original
            item.name=original_name
        return
    if CASE=='normal':return
    original=pytest.Function.__repr__
    original_type=type(item)
    if CASE=='nonstring':item.__class__=NonstringFunction
    calls=[]
    sentinel=Sentinel('actual item repr')
    def live_repr(current):
        calls.append(current is item)
        assert request.getfixturevalue('plain')==31
        if CASE=='error':raise sentinel
        if CASE=='nonstring':return 17
        if CASE=='surrogate':return '<LiveItem \\ud800>'
        return '<LiveItem '+current.name+'>'
    pytest.Function.__repr__=live_repr
    try:
        if CASE in {'error','nonstring'}:
            errors=[]
            for value in requests:
                try:repr(value)
                except BaseException as error:
                    errors.append([type(error).__name__,str(error),error is sentinel])
                else:raise AssertionError('repr callback did not fail')
            observed['errors']=errors
        else:observed['live']=[repr(value) for value in requests]
        observed['calls']=calls
    finally:
        pytest.Function.__repr__=original
        item.__class__=original_type
"""

_CASES = ["normal", "reentry", "error", "nonstring", "surrogate", "custom_item", "subclass"]


def _project(root: Path, case: str, *, interpreter: str | Path | None = None) -> VfyProject:
    project = VfyProject(root, interpreter=interpreter)
    project.write("test_request.py", f"CASE={case!r}\nfrom engine import NATIVE\n" + _SOURCE)
    return project


def _run(project: VfyProject, route: str, case: str) -> dict[str, Any]:
    expected = None
    if route != "stock":
        expected = _run(project, "stock", case)
        (project.root / "observations.json").unlink()
    project.write("engine.py", f"NATIVE={route != 'stock'}\n")
    result = (
        project.run_python("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider")
        if route == "stock"
        else project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            f"--workers={2 if route == 'workers' else 1}",
            "-q",
        )
    )
    project.write(f"{route}.stdout", result.stdout)
    project.write(f"{route}.stderr", result.stderr)
    project.write(f"{route}.status", str(result.returncode))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (project.root / "observations.json").is_file(), result.stdout + result.stderr
    observed = json.loads((project.root / "observations.json").read_text())
    project.write(f"{route}-observations.json", json.dumps(observed))
    quoted = "quoted'\\name Ω"
    normal = [
        "<FixtureRequest for <Function test_control>>",
        f"<SubRequest {quoted!r} for <Function test_control>>",
        "<SubRequest 'module_request' for <Function test_control>>",
        "<SubRequest 'session_request' for <Function test_control>>",
    ]
    assert observed["normal"] == observed["retained"] == normal
    assert observed["cleanup"] == dict(
        zip(["quoted", "module", "session"], normal[1:], strict=True)
    )
    if case == "custom_item":
        assert observed["custom"] == [
            "<CustomItem None>",
            "<CustomItem ordinary>",
            "<NamedItem live name>",
        ]
        assert observed["events"] == ["name", ["format", ""]]
    elif case == "subclass":
        assert observed["subclass"] == [
            "<FixtureRequest for <SpecialFunction renamed[actual]>>",
            f"<SubRequest {quoted!r} for <SpecialFunction renamed[actual]>>",
            "<SubRequest 'module_request' for <SpecialFunction renamed[actual]>>",
            "<SubRequest 'session_request' for <SpecialFunction renamed[actual]>>",
        ]
    elif case != "normal":
        assert observed["calls"] == [True] * 4
        if case == "error":
            assert observed["errors"] == [["Sentinel", "actual item repr", True]] * 4
        elif case == "nonstring":
            invalid = type(
                "NonstringFunction",
                (),
                {"__module__": "test_request", "__repr__": lambda self: 17},
            )()
            with pytest.raises(TypeError) as reference:
                repr(invalid)
            assert observed["errors"] == [["TypeError", str(reference.value), False]] * 4
        else:
            item = "<LiveItem \ud800>" if case == "surrogate" else "<LiveItem test_control>"
            assert observed["live"] == [
                f"<FixtureRequest for {item}>",
                f"<SubRequest {quoted!r} for {item}>",
                f"<SubRequest 'module_request' for {item}>",
                f"<SubRequest 'session_request' for {item}>",
            ]
    if route != "stock":
        assert observed == expected
    return observed


@pytest.mark.parametrize(
    "route",
    [
        "stock",
        pytest.param("serial", marks=requires_native_pytest),
        pytest.param("workers", marks=requires_native_pytest),
    ],
)
@pytest.mark.parametrize("case", _CASES)
def test_live_function_and_request_representations(tmp_path: Path, route: str, case: str) -> None:
    project = _project(tmp_path, case)
    _run(project, route, case)
