"""Zero-argument calls preserve live replacements and fixture inventory."""

from __future__ import annotations

import json
import shlex
import shutil
import sysconfig
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_empty_arguments_preserve_sync_and_late_async_callables(tmp_path: Path) -> None:
    source = """import asyncio
import inspect
import json
from pathlib import Path
import pytest

events = []
def record(event):
    events.append(event)
    Path('events.json').write_text(json.dumps(events))

@pytest.fixture(autouse=True)
def lifecycle(request):
    name = request.node.name
    assert request.node._fixtureinfo.argnames == ()
    record('setup:' + name)
    def finalizer():
        assert 'lifecycle' in request.node.funcargs
        record('node:' + name)
    request.node.addfinalizer(finalizer)
    class Sync:
        def __call__(self, *args, **kwargs):
            record({
                'kind': 'sync',
                'args': list(args),
                'kwargs': kwargs,
                'inventory': 'lifecycle' in request.node.funcargs,
            })
    async def coroutine():
        await asyncio.sleep(0)
        record({
            'kind': name,
            'running_loop': asyncio.get_running_loop().is_running(),
            'inventory': 'lifecycle' in request.node.funcargs,
        })
    @inspect.markcoroutinefunction
    def marked():
        return coroutine()
    if name == 'test_sync':
        request.node.obj = Sync()
    else:
        request.node.obj = coroutine if name == 'test_coroutine' else marked
        request.node.add_marker('asyncio')
    yield
    assert 'lifecycle' in request.node.funcargs
    record('fixture:' + name)

def test_sync():
    raise AssertionError('original callable ran')

def test_coroutine():
    raise AssertionError('original callable ran')

def test_marked():
    raise AssertionError('original callable ran')
"""
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_mode=strict\n")
        project.write("test_calls.py", source)
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
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "setup:test_sync",
            {"kind": "sync", "args": [], "kwargs": {}, "inventory": True},
            "fixture:test_sync",
            "node:test_sync",
            "setup:test_coroutine",
            {"kind": "test_coroutine", "running_loop": True, "inventory": True},
            "fixture:test_coroutine",
            "node:test_coroutine",
            "setup:test_marked",
            {"kind": "test_marked", "running_loop": True, "inventory": True},
            "fixture:test_marked",
            "node:test_marked",
        ]
    )


def test_empty_arguments_preserve_opaque_c_callables(tmp_path: Path) -> None:
    compiler = shlex.split(sysconfig.get_config_var("LDSHARED") or "cc -shared")
    executable = shutil.which(compiler[0]) or shutil.which("cc") or shutil.which("gcc")
    if executable is None:
        pytest.skip("opaque callable regression requires a C compiler")
    extension_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    assert isinstance(extension_suffix, str)
    include_directories = dict.fromkeys(
        sysconfig.get_path(name) for name in ("include", "platinclude")
    )
    extension = tmp_path / ("opaque_call" + extension_suffix)
    c_source = tmp_path / "opaque_call.c"
    c_source.write_text(r"""#include <Python.h>

static PyObject *probe_call(PyObject *self, PyObject *args, PyObject *kwargs) {
    if (kwargs == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "opaque tp_call received NULL kwargs");
        return NULL;
    }
    if (PyTuple_Size(args) != 0 || !PyDict_Check(kwargs) || PyDict_Size(kwargs) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "opaque tp_call expected empty arguments");
        return NULL;
    }
    PySys_WriteStdout("opaque-kwargs:{}\n");
    Py_RETURN_NONE;
}

static PyType_Slot slots[] = {
    {Py_tp_call, probe_call},
    {Py_tp_new, PyType_GenericNew},
    {0, NULL},
};
static PyType_Spec spec = {
    .name = "opaque_call.Probe",
    .basicsize = sizeof(PyObject),
    .flags = Py_TPFLAGS_DEFAULT,
    .slots = slots,
};
static PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "opaque_call",
    .m_size = -1,
};
PyMODINIT_FUNC PyInit_opaque_call(void) {
    PyObject *result = PyModule_Create(&module);
    if (result == NULL) return NULL;
    PyObject *type = PyType_FromSpec(&spec);
    if (type == NULL || PyModule_AddObject(result, "Probe", type) < 0) {
        Py_XDECREF(type);
        Py_DECREF(result);
        return NULL;
    }
    return result;
}
""")
    built = VfyProject(tmp_path).run(
        executable,
        *compiler[1:],
        "-fPIC",
        *("-I" + directory for directory in include_directories),
        str(c_source),
        "-o",
        str(extension),
    )
    assert built.returncode == 0, built.stdout + built.stderr
    source = """import json
from pathlib import Path
import opaque_call
import pytest

events = []

def record(event):
    events.append(event)
    Path('events.json').write_text(json.dumps(events))

@pytest.fixture(autouse=True)
def lifecycle(request):
    name = request.node.name
    record('setup:' + name)
    request.node.addfinalizer(lambda: record('node:' + name))
    if name == 'test_opaque':
        opaque_call.Probe.__code__ = request.node.obj.__code__
        request.node.obj = opaque_call.Probe()
    yield
    record('fixture:' + name)

def test_opaque():
    raise AssertionError('original callable ran')

def test_normal():
    record('call:test_normal')
"""
    events: dict[str, list[str]] = {}
    reports: dict[str, list[tuple[str | None, list[tuple[str, str | None]]]]] = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("test_calls.py", source)
        shutil.copy2(extension, project.root / extension.name)
        result = project.run_vfy(
            "test",
            f"--engine={engine}",
            "--workers=1",
            "--no-cov",
            "-q",
            "--junitxml=result.xml",
            "-o",
            "junit_logging=all",
            env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
        project.write("stdout.txt", result.stdout)
        project.write("stderr.txt", result.stderr)
        result.expect(0)
        assert "2 passed" in result.stdout
        events[engine] = json.loads((project.root / "events.json").read_text())
        reports[engine] = [
            (case.get("name"), [(child.tag, child.text) for child in case])
            for case in ET.parse(project.root / "result.xml").iter("testcase")
        ]
    assert (
        events["pytest-native"]
        == events["pytest"]
        == [
            "setup:test_opaque",
            "fixture:test_opaque",
            "node:test_opaque",
            "setup:test_normal",
            "call:test_normal",
            "fixture:test_normal",
            "node:test_normal",
        ]
    )
    assert reports["pytest-native"] == reports["pytest"]
    assert "opaque-kwargs:{}" in "".join(text or "" for _, text in reports["pytest"][0][1])
