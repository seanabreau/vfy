"""Owned interpreter startup preserves CPython's exception formatting policy."""

from __future__ import annotations

import json
import sys
import sysconfig
from importlib.machinery import EXTENSION_SUFFIXES
from importlib.util import find_spec
from pathlib import Path

import pytest

from tests.support import CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _run(project: VfyProject, module: str, *arguments: str, no_site: bool = False) -> CommandResult:
    if no_site:
        # -S skips editable-install .pth files as well as site initialization.
        spec = find_spec(module)
        assert spec is not None
        assert spec.origin is not None
        paths = list(
            dict.fromkeys([sysconfig.get_path("purelib"), str(Path(spec.origin).parent.parent)])
        )
        return project.run_python(
            "-S",
            "-c",
            f"import runpy, sys; sys.path.extend({paths!r}); "
            "module = sys.argv.pop(1); "
            "runpy.run_module(module, run_name='__main__', alter_sys=True)",
            module,
            *arguments,
        )
    return project.run_python("-m", module, *arguments)


@pytest.mark.parametrize("no_site", [False, True])
@pytest.mark.parametrize("workers", [1, 2])
def test_owned_traceback_import_hints_match_the_callers_site_policy(
    tmp_path: Path, no_site: bool, workers: int
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "format_probe.py",
        """import importlib.machinery, io, json, os, sys, traceback
from pathlib import Path
from unittest.mock import patch

class Suggestions:
    def find_spec(self, fullname, path, target=None):
        return None
    def discover(self, parent=None):
        return [importlib.machinery.ModuleSpec('startup_suggestion', None)]


def observe(destination):
    flags = sys.flags
    records = {}
    original_formatter = traceback.format_exception_only
    calls = []
    def user_formatter(*args, **kwargs):
        calls.append('called')
        return original_formatter(*args, **kwargs)
    traceback.format_exception_only = user_formatter
    finder = Suggestions()
    sys.meta_path.insert(0, finder)
    sys._format_probe_live = 'first'
    try:
        errors = []
        for name in ('vfy_startup_missing_module', 'startup_sugestion',
                     'startup_wrong_abi', 'winreg' if os.name != 'nt' else 'pwd'):
            try:
                __import__(name)
            except ModuleNotFoundError as error:
                error.add_note('preserved import note')
                errors.append(error)
                records[name] = ''.join(traceback.format_exception_only(error))
            else:
                raise AssertionError('missing-module witness unexpectedly imported: ' + name)
        try:
            raise RuntimeError('outer import failure') from errors[0]
        except RuntimeError as error:
            records['chain'] = ''.join(traceback.format_exception(error))
        records['group'] = ''.join(traceback.format_exception(
            ExceptionGroup('import failures', errors)))
        stderr = sys.stderr
        captured = io.StringIO()
        with patch('traceback.sys.stderr', captured):
            assert sys.stderr is captured
            traceback.print_exception(errors[0])
        assert sys.stderr is stderr
        records['printed'] = captured.getvalue()
        assert 'vfy_startup_missing_module' in records['printed']
        with patch('traceback.sys._format_probe_temporary', 'patched', create=True):
            assert sys._format_probe_temporary == 'patched'
        assert not hasattr(sys, '_format_probe_temporary')
        assert getattr(traceback.sys, '_format_probe_live') == 'first'
        sys._format_probe_live = 'second'
        assert getattr(traceback.sys, '_format_probe_live') == 'second'
        assert traceback.sys.stderr is sys.stderr
        assert traceback.sys.flags.optimize == flags.optimize
        assert sys.flags is flags and type(sys.flags) is type(flags)
        assert calls == ['called'] * len(errors)
        Path(destination).write_text(json.dumps(records))
    finally:
        sys.meta_path.remove(finder)
        traceback.format_exception_only = original_formatter
        del sys._format_probe_live
""",
    )
    suffix = EXTENSION_SUFFIXES[-1]
    project.write(f"startup_wrong_abi.cpython-399-foreign{suffix}", "not a loadable extension\n")
    for name in ("first", "second"):
        project.write(
            f"test_{name}.py",
            "from format_probe import observe\n\ndef test_formats():\n"
            f"    observe('{name}.json')\n",
        )
    stock = _run(project, "pytest", "-q", "-p", "no:cacheprovider", no_site=no_site)
    stock.expect(0)
    expected = {
        name: json.loads((tmp_path / f"{name}.json").read_text()) for name in ("first", "second")
    }
    for name in expected:
        (tmp_path / f"{name}.json").unlink()
    independent = _run(
        project,
        "vfy",
        "test",
        "--no-replay",
        "--no-cov",
        f"--workers={workers}",
        "-q",
        no_site=no_site,
    )
    independent.expect(0)
    observed = {name: json.loads((tmp_path / f"{name}.json").read_text()) for name in expected}
    assert observed == expected
    if sys.version_info >= (3, 15):
        for record in expected.values():
            assert (
                "Site initialization is disabled" in record["vfy_startup_missing_module"]
            ) is no_site
            assert (
                "standard library module"
                in record["winreg" if sys.platform != "win32" else "pwd"].lower()
            )
            if not no_site:
                assert "Did you mean: 'startup_suggestion'?" in record["startup_sugestion"]
                assert "different Python version" in record["startup_wrong_abi"]


@pytest.mark.parametrize(
    ("outcome", "replacement"),
    [("pass", "none"), ("fail", "none"), ("pass", "replace"), ("pass", "delete")],
)
def test_traceback_startup_cleanup_preserves_user_replacements(
    tmp_path: Path, outcome: str, replacement: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import atexit, json, sys, traceback
from pathlib import Path
flags = sys.flags
replacement = None

@atexit.register
def observe_cleanup():
    Path('cleanup.json').write_text(json.dumps({
        'restored': getattr(traceback, 'sys', None) is sys,
        'replacement': replacement is not None and getattr(traceback, 'sys', None) is replacement,
        'deleted': not hasattr(traceback, 'sys'),
        'real_flags_unchanged': sys.flags is flags,
    }))
""",
    )
    project.write(
        "test_scope.py",
        f"""import conftest, sys, traceback

def test_format_scope():
    if {replacement!r} == 'replace':
        class UserSys:
            def __getattr__(self, name):
                return getattr(sys, name)
        conftest.replacement = UserSys()
        traceback.sys = conftest.replacement
    elif {replacement!r} == 'delete':
        del traceback.sys
    if {outcome!r} == 'fail':
        raise ModuleNotFoundError('user failure remains visible', name='vfy_startup_missing_module')
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-replay", "--no-cov", "-q")
    assert result.returncode == (1 if outcome == "fail" else 0), result.stdout + result.stderr
    if outcome == "fail":
        assert "user failure remains visible" in result.stdout
    assert json.loads((tmp_path / "cleanup.json").read_text()) == {
        "restored": replacement == "none",
        "replacement": replacement == "replace",
        "deleted": replacement == "delete",
        "real_flags_unchanged": True,
    }
