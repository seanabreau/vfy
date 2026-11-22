"""Fresh native workers apply the public assertion and import-mode lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import vfy
from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_parallel_workers_populate_rewritten_code_once(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "sitecustomize.py",
        """import os, sys, time
def observe(event, args):
    if event == 'compile' and isinstance(args[0], bytes) and str(args[1]).endswith('/helper.py'):
        with open('rewrites', 'a') as stream:
            stream.write(str(os.getpid()) + '\\n')
        time.sleep(0.2)
sys.addaudithook(observe)
""",
    )
    project.write("conftest.py", "import pytest\npytest.register_assert_rewrite('helper')\n")
    project.write("helper.py", "def check():\n    assert 1 + 1 == 2\n")
    for name in ("a", "b", "c", "d"):
        project.write(f"test_{name}.py", "from helper import check\ndef test_value(): check()\n")
    environment = {
        "PYTHONPATH": os.pathsep.join(
            (str(project.root), str(Path(vfy.__file__).resolve().parents[1]))
        )
    }
    oracle = project.run_pytest("-q", "-n", "4", env=environment)
    oracle.expect(0)
    rewrites = tmp_path / "rewrites"
    assert rewrites.read_text().splitlines()
    rewrites.unlink()
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=4",
        "--native-result-json=result.json",
        env=environment,
    )
    actual.expect(0)
    record = json.loads((tmp_path / "result.json").read_text())
    assert record["execution_topology"]["active_workers"] == 4
    assert len(rewrites.read_text().splitlines()) == 1


def test_workers_expose_the_real_public_package_source(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    rejected = project.run_python(
        "-c",
        """import sys, pytest
from vfy.independent.outcomes import install_outcome_modules
previous = sys.modules['_pytest']
try:
    install_outcome_modules()
except RuntimeError as error:
    assert 'require the independent pytest facade' in str(error)
else:
    raise AssertionError('real pytest must not receive independent aliases')
assert sys.modules['_pytest'] is previous
""",
    )
    rejected.expect(0)
    source = """import importlib.util
from pathlib import Path
import pytest

def test_public_package():
    path = Path(pytest.__file__)
    assert path.is_file()
    assert (path.name, path.parent.name) == ('__init__.py', 'pytest')
    assert importlib.util.find_spec('pytest').origin == str(path)
    assert importlib.util.find_spec('pytest').has_location
"""
    for name in ("a", "b"):
        project.write(f"test_{name}.py", source)
    oracle = project.run_pytest(load_xdist=False)
    oracle.expect(0)
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "--workers=2")
    actual.expect(0)


def test_worker_imports_recover_after_compile_failure_with_optional_cache(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = """import importlib, sys
import pytest

@pytest.mark.parametrize('blocked', [False, True])
def test_recover(tmp_path, monkeypatch, blocked):
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop('helper', None)
    pytest.register_assert_rewrite('helper')
    if blocked:
        (tmp_path / '__pycache__').write_text('unavailable cache')
    helper = tmp_path / 'helper.py'
    helper.write_text('assert (')
    with pytest.raises(SyntaxError):
        importlib.import_module('helper')
    helper.write_text('VALUE = 42\\nassert VALUE == 42\\n')
    assert importlib.import_module('helper').VALUE == 42
"""
    project.write("test_import.py", source)
    oracle = project.run_pytest(load_xdist=False)
    oracle.expect(0)
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "--workers=2")
    actual.expect(0)


def test_compile_audit_hook_can_reload_its_source(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_reload.py",
        """import importlib, sys
import pytest
@pytest.mark.parametrize('through_alias', [False, True])
def test_reload(tmp_path, monkeypatch, through_alias):
    monkeypatch.syspath_prepend(str(tmp_path))
    alias = tmp_path.with_name(tmp_path.name + '-alias')
    alias.symlink_to(tmp_path, target_is_directory=True)
    sys.modules.pop('audit_helper', None)
    helper = tmp_path / 'audit_helper.py'
    helper.write_text('VALUE = 42\\nassert VALUE == 42\\n')
    pytest.register_assert_rewrite('audit_helper')
    entered = []
    def audit(event, args):
        if event == 'compile' and args[1] == str(helper) and not entered:
            entered.append(True)
            if through_alias:
                monkeypatch.syspath_prepend(str(alias))
            importlib.reload(sys.modules['audit_helper'])
    sys.addaudithook(audit)
    assert importlib.import_module('audit_helper').VALUE == 42
    assert entered == [True]
""",
    )
    oracle = project.run_pytest(load_xdist=False)
    oracle.expect(0)
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "--workers=2")
    actual.expect(0)


def test_compile_audit_hook_can_import_a_module_being_imported_by_another_thread(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=15)
    project.write(
        "test_audit_threads.py",
        """import importlib, sys, threading
import pytest

def test_concurrent_import(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ('audit_a', 'audit_b'):
        sys.modules.pop(name, None)
        (tmp_path / (name + '.py')).write_text('VALUE = 42\\nassert VALUE == 42\\n')
    pytest.register_assert_rewrite('audit_a', 'audit_b')
    cache_opened = threading.Event()
    threads = []
    entered = []
    def audit(event, args):
        if event == 'open' and 'audit_b.' in str(args[0]) and '__pycache__' in str(args[0]):
            cache_opened.set()
        if event == 'compile' and str(args[1]) == str(tmp_path / 'audit_a.py') and not entered:
            entered.append(True)
            thread = threading.Thread(
                target=importlib.import_module, args=('audit_b',), daemon=True)
            threads.append(thread)
            thread.start()
            assert cache_opened.wait(3), 'concurrent import did not open its cache'
            assert importlib.import_module('audit_b').VALUE == 42
    sys.addaudithook(audit)
    assert importlib.import_module('audit_a').VALUE == 42
    threads[0].join(3)
    assert not threads[0].is_alive()
    assert entered == [True]
""",
    )
    oracle = project.run_pytest(load_xdist=False)
    oracle.expect(0)
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "--workers=2")
    actual.expect(0)


def test_compile_audit_hook_can_run_another_process_importing_a_different_module(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=30)
    project.write(
        "test_audit_process.py",
        """import importlib, os, subprocess, sys
import pytest

def test_child_import(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in ('audit_a', 'audit_b'):
        sys.modules.pop(name, None)
        (tmp_path / (name + '.py')).write_text('VALUE = 42\\nassert VALUE == 42\\n')
    (tmp_path / 'test_child.py').write_text(
        'import pytest\\npytest.register_assert_rewrite(\"audit_b\")\\n'
        'import audit_b\\ndef test_value(): assert audit_b.VALUE == 42\\n'
    )
    pytest.register_assert_rewrite('audit_a')
    entered = []
    def audit(event, args):
        if event == 'compile' and str(args[1]) == str(tmp_path / 'audit_a.py') and not entered:
            entered.append(True)
            if os.environ['AUDIT_CHILD_ENGINE'] == 'stock':
                command = [sys.executable, '-m', 'pytest', '-q', 'test_child.py']
            else:
                command = [sys.executable, '-m', 'vfy', 'test', '--engine=native',
                           '--no-cov', '--workers=1', '-q', 'test_child.py']
            result = subprocess.run(
                command, cwd=tmp_path, capture_output=True, text=True, timeout=8)
            assert result.returncode == 0, result.stdout + result.stderr
    sys.addaudithook(audit)
    assert importlib.import_module('audit_a').VALUE == 42
    assert entered == [True]
""",
    )
    oracle = project.run_pytest(load_xdist=False, env={"AUDIT_CHILD_ENGINE": "stock"})
    oracle.expect(0)
    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        env={"AUDIT_CHILD_ENGINE": "independent"},
    )
    actual.expect(0)


def test_four_workers_populate_the_bytecode_cache_without_lock_files(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for index in range(8):
        project.write(
            f"test_cached_{index}.py",
            f"def test_value():\n    assert {index} + 1 == {index + 1}\n",
        )
    actual = project.run_vfy("test", "--engine=native", "--no-cov", "--workers=4")
    actual.expect(0)
    cache_dir = project.root / "__pycache__"
    assert list(cache_dir.glob("test_cached_*-vfy-*.pyc"))
    assert not list(cache_dir.glob("*.lock"))


@pytest.mark.parametrize("mode", ["prepend", "append", "importlib"])
def test_two_workers_match_pytest_package_imports_and_registered_assertions(
    tmp_path: Path, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("conftest.py", "import pytest\npytest.register_assert_rewrite('helpers')\n")
    project.write("helpers/__init__.py", "")
    project.write("helpers/checks.py", "def check(value):\n    assert value == 2\n")
    project.write("suite_pkg/__init__.py", "from . import test_a\n")
    source = """import builtins
import importlib
import json
import os
from pathlib import Path
import pytest
from helpers.checks import check

if not hasattr(builtins, 'vfy_public_import_counts'):
    builtins.vfy_public_import_counts = {}
counts = builtins.vfy_public_import_counts
counts[__name__] = counts.get(__name__, 0) + 1

def test_observe():
    with pytest.raises(AssertionError) as helper:
        check(1)
    left, right = 1, 2
    with pytest.raises(AssertionError) as local:
        assert left + 1 == right + 1
    observed = {
        'name': __name__, 'package': __package__, 'counts': counts,
        'identity': importlib.import_module(__name__).__dict__ is globals(),
        'helper': str(helper.value), 'local': str(local.value),
    }
    Path('observed').mkdir(exist_ok=True)
    Path('observed', Path(__file__).stem + '.json').write_text(json.dumps(observed))
    Path(Path(__file__).stem + '.pid').write_text(str(os.getpid()))
"""
    for name in ("a", "b"):
        project.write(f"suite_pkg/test_{name}.py", source)
    oracle = project.run_python("-m", "pytest", "-q", f"--import-mode={mode}", "suite_pkg")
    oracle.expect(0)
    expected = {
        path.name: json.loads(path.read_text()) for path in (tmp_path / "observed").glob("*.json")
    }
    for path in (tmp_path / "observed").glob("*.json"):
        path.unlink()

    actual = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        f"--import-mode={mode}",
        "--native-result-json=results.json",
        "suite_pkg",
    )

    actual.expect(0)
    assert {
        path.name: json.loads(path.read_text()) for path in (tmp_path / "observed").glob("*.json")
    } == expected
    record = json.loads((tmp_path / "results.json").read_text())
    pids = {int(path.read_text()) for path in tmp_path.glob("*.pid")}
    assert len(pids) == record["execution_topology"]["active_workers"] == 2
    assert set(record["execution_topology"]["worker_pids"]) == pids
