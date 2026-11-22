"""Builtin node-ID persistence creates the configured cache lazily at finish."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

_CASE = """import os
from pathlib import Path

def observe(name):
    Path(Path(__file__).stem + '-' + name + '.body').write_text(str(os.getpid()))

def test_first():
    observe('first')

def test_second():
    observe('second')
"""

_CONFIG = """import json
from pathlib import Path

def pytest_sessionstart(session):
    config = session.config
    cache = getattr(config, 'cache', None)
    directory = config.rootpath / config.getini('cache_dir') if cache is not None else None
    Path('startup.json').write_text(json.dumps({
        'cache': cache is not None,
        'exists': directory is not None and directory.exists(),
    }))
"""


@requires_native_pytest
@pytest.mark.parametrize("engine", ["stock", "native"])
def test_nodeids_public_set_runs_with_a_sorted_list_even_when_unchanged(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_a.py", "def test_a():\n    pass\n")
    project.write(
        "conftest.py",
        """import json
from pathlib import Path

def pytest_sessionstart(session):
    cache = session.config.cache
    original_set = cache.set
    def observed_set(key, value):
        if key == 'cache/nodeids':
            with Path('cache-calls.jsonl').open('a') as stream:
                stream.write(json.dumps([type(value).__name__, value]) + '\\n')
        return original_set(key, value)
    cache.set = observed_set
""",
    )
    for _ in range(2):
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "-q")
        )
        result.expect(0)
    calls = [
        json.loads(line) for line in (project.root / "cache-calls.jsonl").read_text().splitlines()
    ]
    assert calls == [["list", ["test_a.py::test_a"]]] * 2


@requires_native_pytest
def test_nodeids_publication_allows_gc_reentry_and_retains_its_snapshot(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("test_a.py", "def test_a():\n    pass\n")
    project.write(
        "conftest.py",
        """import gc, json, sys
from pathlib import Path

active = False
hits = []
errors = []
published = []

class Item:
    nodeid = 'reentrant.py::test_gc'

class LateItem:
    nodeid = 'late.py::test_late'

item = Item()
late_item = LateItem()

def pytest_sessionstart(session):
    global state, old_profile, old_threshold
    state = session.config.pluginmanager.getplugin('nfplugin')._state
    cache = session.config.cache
    original_set = cache.set
    old_profile = sys.getprofile()
    old_threshold = gc.get_threshold()

    def observed_set(key, value):
        if key == 'cache/nodeids':
            state.collect([late_item])
            published.append(value)
        return original_set(key, value)

    def event(phase, info):
        if active and phase == 'start':
            try:
                state.collect([item])
                hits.append(1)
            except BaseException as error:
                errors.append(repr(error))

    def profiler(frame, event, arg):
        global active
        if (event == 'c_call' and getattr(arg, '__self__', None) is state
                and getattr(arg, '__name__', '') == 'finish'):
            gc.collect()
            active = True
            gc.set_threshold(1, 1, 1)
        elif (event in ('c_return', 'c_exception')
                and getattr(arg, '__self__', None) is state
                and getattr(arg, '__name__', '') == 'finish'):
            active = False
            gc.set_threshold(*old_threshold)

    cache.set = observed_set
    gc.callbacks.append(event)
    sys.setprofile(profiler)

def pytest_unconfigure():
    sys.setprofile(old_profile)
    gc.set_threshold(*old_threshold)
    Path('reentry.json').write_text(json.dumps({
        'hits': len(hits), 'errors': errors, 'published': published,
    }))
""",
    )
    result = project.run_vfy("test", "--engine=native", "--no-cov", "-q")
    result.expect(0)
    observed = json.loads((project.root / "reentry.json").read_text())
    assert observed["hits"] > 0
    assert observed["errors"] == []
    assert len(observed["published"]) == 1
    assert "test_a.py::test_a" in observed["published"][0]
    assert "late.py::test_late" not in observed["published"][0]


@requires_native_pytest
@pytest.mark.parametrize("engine", ["stock", "native"])
@pytest.mark.parametrize("mode", ["default", "override", "collectonly", "disabled", "removed"])
def test_nodeids_cache_follows_selected_items_and_lazy_session_lifecycle(
    tmp_path: Path, engine: str, mode: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    configuration = _CONFIG
    if mode == "removed":
        configuration += """
import pytest
@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    config.pluginmanager.unregister(name='cacheprovider')
"""
    project.write("conftest.py", configuration)
    project.write("test_a.py", _CASE)
    project.write("test_b.py", _CASE)
    cache = project.root / ("my cache" if mode == "override" else ".pytest_cache")
    options = ["-o", "cache_dir=my cache"] if mode == "override" else []
    if mode == "collectonly":
        options.append("--collect-only")
    elif mode == "disabled":
        options.extend(["-p", "no:cacheprovider"])

    for selection in ("first", "second"):
        args = ["-q", "-k", selection, *options]
        result = (
            project.run_pytest(*args, load_xdist=False)
            if engine == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", *args)
        )
        result.expect(0)
        startup = json.loads((project.root / "startup.json").read_text())
        enabled = mode != "disabled"
        assert startup == {
            "cache": enabled,
            "exists": selection == "second" and mode in {"default", "override", "removed"},
        }
        if mode in {"collectonly", "disabled"}:
            assert not cache.exists()
            assert bool(list(project.root.glob("*.body"))) == (mode == "disabled")
        else:
            expected = [f"test_{file}.py::test_first" for file in ("a", "b")]
            if selection == "second":
                expected.extend(f"test_{file}.py::test_second" for file in ("a", "b"))
            assert json.loads((cache / "v/cache/nodeids").read_text()) == sorted(expected)
            assert all(
                (cache / name).is_file() for name in ("README.md", ".gitignore", "CACHEDIR.TAG")
            )
        if mode == "override":
            assert not (project.root / ".pytest_cache").exists()


@requires_native_pytest
def test_worker_runs_publish_the_complete_nodeid_union(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _CONFIG)
    project.write("test_a.py", _CASE)
    project.write("test_b.py", _CASE)
    first = [f"test_{file}.py::test_first" for file in ("a", "b")]
    union = [f"test_{file}.py::test_{name}" for file in ("a", "b") for name in ("first", "second")]
    for selection, expected in (("first", first), ("second", union)):
        result = project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "--workers=2",
            "-q",
            "-k",
            selection,
            "-o",
            "cache_dir=my cache",
            env={"VFY_WORKER_START": "exec"},
        )
        result.expect(0)
        assert json.loads((project.root / "my cache/v/cache/nodeids").read_text()) == expected
    assert sorted(path.name for path in project.root.glob("*.body")) == [
        f"test_{file}-{name}.body" for file in ("a", "b") for name in ("first", "second")
    ]
    assert not (project.root / ".pytest_cache").exists()


@requires_native_pytest
@pytest.mark.parametrize("engine", ["stock", "native"])
def test_early_exit_caches_selected_items_that_never_execute(tmp_path: Path, engine: str) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    control_case = _CASE.replace("import os\n", "import os, pytest\n").replace(
        "    observe('second')\n",
        "    observe('second')\n    pytest.exit('actual cache frontier control', returncode=42)\n",
    )
    control_case += "\ndef test_third():\n    observe('third')\n"
    project.write("test_a.py", control_case)
    project.write("test_b.py", control_case)
    result = (
        project.run_pytest("-q", load_xdist=False)
        if engine == "stock"
        else project.run_vfy(
            "test",
            "--engine=native",
            "--no-cov",
            "--workers=2",
            "--native-scheduling=file",
            "-q",
            env={"VFY_WORKER_START": "exec"},
        )
    )
    result.expect(42)
    bodies = {path.name for path in project.root.glob("*.body")}
    assert bodies & {"test_a-second.body", "test_b-second.body"}
    assert bodies <= {
        f"test_{file}-{name}.body" for file in ("a", "b") for name in ("first", "second")
    }
    assert json.loads((project.root / ".pytest_cache/v/cache/nodeids").read_text()) == [
        f"test_{file}.py::test_{name}"
        for file in ("a", "b")
        for name in ("first", "second", "third")
    ]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX signals and FIFOs")
@pytest.mark.parametrize(
    ("handler", "cleanup_error"),
    [
        (handler, cleanup_error)
        for handler in ("callable", "default", "replace-ignore")
        for cleanup_error in (False, True)
    ]
    + [("raise-error", False), ("raise-exit", False)],
)
def test_peer_interrupt_waits_for_worker_cleanup_publication(
    tmp_path: Path, cleanup_error: bool, handler: str
) -> None:
    project = VfyProject(tmp_path, timeout=15)
    os.mkfifo(tmp_path / "peer-cleaning")
    project.write(
        "conftest.py",
        f"""import atexit, os, signal, threading
from pathlib import Path

def pytest_sessionstart(session):
    if os.environ.get('VFY_NATIVE_PARALLEL_INDEX') != '1':
        return
    def interrupted(signum, frame):
        assert signal.getsignal(signal.SIGINT) is interrupted
        Path('peer-handler-restored').touch()
        if {handler!r} == 'raise-error':
            raise ValueError('actual prior handler failure')
        if {handler!r} == 'raise-exit':
            raise SystemExit(1)
        signal.default_int_handler(signum, frame)
    previous_handler = signal.SIG_DFL if {handler!r} == 'default' else interrupted
    signal.signal(signal.SIGINT, previous_handler)
    def after_shutdown():
        expected = signal.SIG_IGN if {handler!r} == 'replace-ignore' else previous_handler
        assert signal.getsignal(signal.SIGINT) is expected
        Path('peer-handler-restored-at-exit').touch()
    atexit.register(after_shutdown)
    read, write = os.pipe()
    os.set_blocking(write, False)
    previous = signal.set_wakeup_fd(write)
    delivered = threading.Event()
    def observe_signal():
        assert os.read(read, 1) == bytes([signal.SIGINT])
        Path('peer-signal-received').touch()
        delivered.set()
    observer = threading.Thread(target=observe_signal)
    observer.start()
    def cleanup():
        try:
            with open('peer-cleaning', 'w') as ready:
                ready.write('ready')
            assert delivered.wait(10), 'coordinator did not interrupt cleaning peer'
            observer.join()
            if {handler!r} == 'replace-ignore':
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            if {cleanup_error!r}:
                raise ValueError('actual worker cleanup failure')
            Path('peer-cleanup-complete').touch()
        finally:
            signal.set_wakeup_fd(previous)
            os.close(write)
            os.close(read)
    session.config.add_cleanup(cleanup)
""",
    )
    project.write(
        "test_a.py",
        """import pytest
def test_prefix():
    pass
def test_control():
    with open('peer-cleaning') as ready:
        assert ready.read() == 'ready'
    pytest.exit('actual primary control', returncode=42)
""",
    )
    project.write(
        "test_b.py",
        f"""import pytest
def test_prefix():
    pass
def test_control():
    pytest.exit('actual peer control', returncode={1 if handler.startswith("raise-") else 42})
""",
    )
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        "--native-scheduling=file",
        "--native-result-json=result.json",
        env={"VFY_WORKER_START": "exec"},
    )
    assert (tmp_path / "peer-signal-received").exists(), result.stdout + result.stderr
    failed = cleanup_error or handler.startswith("raise-")
    assert result.returncode == (3 if failed else 42), result.stdout + result.stderr
    if handler == "callable" and not cleanup_error:
        assert (tmp_path / "peer-handler-restored").exists()
    if cleanup_error or handler != "default":
        assert (tmp_path / "peer-handler-restored-at-exit").exists()
    if failed:
        if cleanup_error:
            assert "actual worker cleanup failure" in result.stdout + result.stderr
        else:
            assert (tmp_path / "peer-handler-restored").exists()
            if handler == "raise-error":
                assert "actual prior handler failure" in result.stdout + result.stderr
        assert not (tmp_path / "result.json").exists()
    else:
        assert (tmp_path / "peer-cleanup-complete").exists()
        record = json.loads((tmp_path / "result.json").read_text())
        assert record["exitstatus"] == 42
        assert [case["nodeid"] for case in record["cases"]] == [
            "test_a.py::test_prefix",
            "test_b.py::test_prefix",
        ]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX SIGINT")
def test_worker_shutdown_preserves_ignored_sigint(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """import os, signal
from pathlib import Path
def pytest_sessionstart(session):
    index = os.environ.get('VFY_NATIVE_PARALLEL_INDEX')
    if index is None:
        return
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    read, write = os.pipe()
    os.set_blocking(read, False)
    os.set_blocking(write, False)
    previous = signal.set_wakeup_fd(write)
    def cleanup():
        try:
            assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
            os.kill(os.getpid(), signal.SIGINT)
            try:
                os.read(read, 1)
            except BlockingIOError:
                pass
            else:
                raise AssertionError('ignored signal wrote a wakeup byte')
            Path('ignored-' + index).touch()
        finally:
            signal.set_wakeup_fd(previous)
            os.close(write)
            os.close(read)
    session.config.add_cleanup(cleanup)
""",
    )
    for name in ("a", "b"):
        project.write(f"test_{name}.py", "def test_prefix():\n    pass\n")
    result = project.run_vfy(
        "test",
        "--engine=native",
        "--no-cov",
        "--workers=2",
        env={"VFY_WORKER_START": "exec"},
    )
    result.expect(0)
    assert all((tmp_path / f"ignored-{index}").exists() for index in range(2))
