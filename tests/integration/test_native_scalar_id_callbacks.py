"""Exact scalar IDs retain callback effects from Python codec customization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


@pytest.mark.parametrize("mode", ["hook_dispatch", "hook_call", "manager_dispatch"])
@pytest.mark.parametrize("family", ["range", "table"])
def test_numeric_ids_preserve_custom_hook_dispatch(tmp_path: Path, mode: str, family: str) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cases.py",
            f"MODE = {mode!r}\nFAMILY = {family!r}\n"
            + r"""
import json
from pathlib import Path
from types import MethodType
from pluggy import HookCaller


def test_cases(value):
    assert value in range(3)


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_cases":
        return
    events = []
    manager = metafunc.config.pluginmanager
    hook = metafunc.config.hook.pytest_make_parametrize_id
    original_dispatch = hook._hookexec
    original_call = HookCaller.__call__

    def dispatch(name, implementations, kwargs, firstresult):
        events.append(kwargs["val"])
        return original_dispatch(name, implementations, kwargs, firstresult)

    def call(self, **kwargs):
        if self is hook:
            events.append(kwargs["val"])
        return original_call(self, **kwargs)

    def manager_dispatch(self, name, implementations, kwargs, firstresult):
        return dispatch(name, implementations, kwargs, firstresult)

    if MODE == "hook_dispatch":
        hook._hookexec = dispatch
    elif MODE == "hook_call":
        HookCaller.__call__ = call
    else:
        manager._hookexec = MethodType(manager_dispatch, manager)
        hook._hookexec = manager._hookexec
    try:
        if FAMILY == "range":
            metafunc.parametrize("value", range(3))
        else:
            metafunc.parametrize(("value",), [(0,), (1,), (2,)])
    finally:
        hook._hookexec = original_dispatch
        HookCaller.__call__ = original_call
        if MODE == "manager_dispatch":
            del manager._hookexec
    Path("observations.json").write_text(json.dumps(events))
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        observations[engine] = json.loads((project.root / "observations.json").read_text())
    assert observations["pytest-native"] == observations["pytest"] == [0, 1, 2]


def test_numeric_id_digit_limit_keeps_pytest_exception_frames(tmp_path: Path) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cases.py",
            r"""
import json
import sys
import traceback
from pathlib import Path


def test_cases(value):
    pass


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_cases":
        return
    original = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        metafunc.parametrize(("value",), [(1,), (10**700,)])
    except ValueError as error:
        frames = [frame.name for frame in traceback.extract_tb(error.__traceback__)]
        Path("observations.json").write_text(json.dumps([
            str(error), [frame for frame in frames if frame in (
                "_resolve_parameter_set_ids", "make_unique_parameterset_ids",
                "_resolve_ids", "_idval", "_idval_from_value",
            )],
        ]))
        raise
    finally:
        sys.set_int_max_str_digits(original)
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        assert result.returncode != 0, result.stdout + result.stderr
        observations[engine] = json.loads((project.root / "observations.json").read_text())
    assert observations["pytest-native"] == observations["pytest"]
    assert "_idval_from_value" in observations["pytest"][1]


def test_numeric_range_ids_preserve_empty_hook_monitoring(tmp_path: Path) -> None:
    """Pluggy monitoring observes ID dispatch even with no implementations."""
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cases.py",
            r"""
import json
from pathlib import Path


def test_cases(value):
    assert value in range(3)


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_cases":
        return
    events = []

    def before(name, implementations, kwargs):
        if name == "pytest_make_parametrize_id":
            events.append([kwargs["val"], kwargs["argname"]])

    def after(outcome, name, implementations, kwargs):
        pass

    undo = metafunc.config.pluginmanager.add_hookcall_monitoring(before, after)
    try:
        metafunc.parametrize("value", range(3))
    finally:
        undo()
    Path("observations.json").write_text(json.dumps(events))
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        assert "3 passed" in result.stdout
        observations[engine] = json.loads((project.root / "observations.json").read_text())
    assert (
        observations["pytest-native"]
        == observations["pytest"]
        == [[0, "value"], [1, "value"], [2, "value"]]
    )


def test_duplicate_byte_ids_do_not_replay_codec_callbacks(tmp_path: Path) -> None:
    """Duplicate repair must not repeat observable ID normalization."""
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cases.py",
            r"""
import codecs
import json
from pathlib import Path

events = []


def test_cases(value):
    assert value == b"\xff"


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_cases":
        return
    original = codecs.lookup_error("backslashreplace")

    def observed(error):
        events.append([error.object.hex(), error.start, error.end])
        return original(error)

    codecs.register_error("backslashreplace", observed)
    try:
        metafunc.parametrize(("value",), [(b"\xff",), (b"\xff",)])
    finally:
        codecs.register_error("backslashreplace", original)
    Path("observations.json").write_text(json.dumps({
        "events": events,
        "ids": [case.id for case in metafunc._calls],
    }))
""",
        )
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "2 passed" in result.stdout
        observations[engine] = json.loads((project.root / "observations.json").read_text())
    assert observations["pytest-native"] == observations["pytest"]
    assert observations["pytest"]["events"] == [["ff", 0, 1], ["ff", 0, 1]]
    assert len(set(observations["pytest"]["ids"])) == 2


def test_printable_ascii_ids_preserve_registered_unicode_escape_codec(tmp_path: Path) -> None:
    """Plain ASCII values still pass through the public codec registry."""
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cases.py",
            r"""
import codecs
import encodings
import json
from pathlib import Path


def test_cases(value):
    assert value in ("first", "second")


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_cases":
        return
    original = codecs.lookup("unicode_escape")
    events = []

    def encode(value, errors="strict"):
        events.append(value)
        result, consumed = original.encode(value, errors)
        return b"custom_" + result, consumed

    def search(name):
        if name == "unicode_escape":
            return codecs.CodecInfo(
                encode=encode, decode=original.decode, name="unicode-escape"
            )
        return None

    # Unregistering invalidates the codec cache. Restore the standard search
    # function behind the custom entry so unrelated encodings remain available.
    codecs.unregister(encodings.search_function)
    codecs.register(search)
    codecs.register(encodings.search_function)
    try:
        metafunc.parametrize(("value",), [("first",), ("second",)])
    finally:
        codecs.unregister(search)
    Path("observations.json").write_text(json.dumps({
        "events": events,
        "ids": [case.id for case in metafunc._calls],
    }))
""",
        )
        result = project.run_vfy("test", f"--engine={engine}", "--workers=1", "--no-cov", "-q")
        result.expect(0)
        assert "2 passed" in result.stdout
        observations[engine] = json.loads((project.root / "observations.json").read_text())
    assert observations["pytest-native"] == observations["pytest"]
    assert observations["pytest"] == {
        "events": ["first", "second"],
        "ids": ["custom_first", "custom_second"],
    }


@pytest.mark.parametrize("mode", ["numeric", "duplicates", "subclass", "helper_code"])
def test_numeric_table_ids_and_public_callspecs_match_pytest(tmp_path: Path, mode: str) -> None:
    observations = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "test_cases.py",
            f"MODE = {mode!r}\n"
            + r"""
import json
from pathlib import Path
from _pytest import python

events = []


class Number(int):
    def __str__(self):
        events.append(int(self))
        return "custom"


def test_cases(left, right, request):
    call = request.node.callspec
    assert call.params["left"] is left
    assert call.params["right"] is right
    assert call._idlist[-1] == "public"


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ != "test_cases":
        return
    rows = [
        (0, None), (1, False), (2, True), (3, 10**100),
        (4, -0.0), (5, float("nan")), (6, float("inf")), (7, 2 + 3j),
    ]
    if MODE == "duplicates":
        rows = [(1, 2), (1, 2), (1, 20)]
    elif MODE == "subclass":
        rows = [(1, 2), (Number(2), 3)]
    original = python.IdMaker._idval_from_value.__code__

    def changed(self, value):
        return "changed"

    if MODE == "helper_code":
        python.IdMaker._idval_from_value.__code__ = changed.__code__
    try:
        metafunc.parametrize(("left", "right"), rows)
    finally:
        python.IdMaker._idval_from_value.__code__ = original
    observed = []
    for call in metafunc._calls:
        observed.append([
            call.id, [(key, repr(value)) for key, value in call.params.items()],
            list(call.indices.items()), list(call._idlist),
            [scope.value for scope in call._arg2scope.values()],
        ])
        call._idlist.append("public")
    Path("observations.json").write_text(json.dumps([events, observed]))
""",
        )
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        result.expect(0)
        observations[engine] = json.loads((project.root / "observations.json").read_text())
    assert observations["pytest-native"] == observations["pytest"]
    events, records = observations["pytest"]
    assert events == ([2] if mode == "subclass" else [])
    assert len(records) == (3 if mode == "duplicates" else 2 if mode == "subclass" else 8)
