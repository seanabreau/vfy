"""Project ``pytest_runtest_protocol`` hooks run around native tests as in pytest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_CONFTEST = """\
import json
from pathlib import Path

import pytest

events = []


def name(item):
    return None if item is None else item.name


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    # falcon stores the running item on its test class this way.
    events.append(["legacy-before", item.name, name(nextitem)])
    outcome = yield
    events.append(["legacy-after", item.name, outcome.get_result()])


@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_wrapped_protocol(item):
    events.append(["wrapper-before", item.name])
    try:
        return (yield)
    finally:
        events.append(["wrapper-after", item.name])



@pytest.hookimpl(tryfirst=True, specname="pytest_runtest_protocol")
def pytest_plain_protocol(item):
    events.append(["plain", item.name])



def pytest_runtest_setup(item):
    events.append(["setup", item.name])


def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
"""


def test_protocol_hooks_match_pytest_order_and_results(tmp_path: Path) -> None:
    events = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("conftest.py", _CONFTEST)
        project.write(
            "test_protocol.py",
            "def test_first():\n    pass\n\ndef test_second():\n    assert False\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 1, result.stdout + result.stderr
        events[engine] = json.loads((project.root / "events.json").read_text())
    assert events["pytest-native"] == events["pytest"]
    assert ["wrapper-before", "test_first"] in events["pytest-native"]
    assert ["plain", "test_first"] in events["pytest-native"]
    assert ["legacy-before", "test_first", "test_second"] in events["pytest-native"]
    assert ["legacy-after", "test_second", True] in events["pytest-native"]


def test_protocol_hook_that_replaces_the_protocol_is_rejected(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n\n@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_runtest_protocol(item, nextitem):\n    return True\n",
    )
    project.write("test_protocol.py", "def test_first():\n    pass\n")
    result = project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
    assert result.returncode != 0
    assert "native pytest cannot run the test protocol from" in result.stdout + result.stderr


def test_mixed_protocol_wrappers_propagate_results_and_recover_exceptions(tmp_path: Path) -> None:
    """Each wrapper observes the result or exception produced by the inner one."""

    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer(item):
    try:
        result = yield
    except RuntimeError as error:
        events.append([item.name, "recovered", str(error)])
        return True
    events.append([item.name, "outer", result])
    return result

@pytest.hookimpl(hookwrapper=True, specname="pytest_runtest_protocol")
def pytest_middle(item):
    outcome = yield
    if outcome.excinfo is not None:
        events.append([item.name, "legacy-error", str(outcome.excinfo[1])])
        outcome.force_exception(RuntimeError("replacement"))
    else:
        events.append([item.name, "legacy-result", outcome.get_result()])
        outcome.force_result("legacy")

@pytest.hookimpl(wrapper=True, trylast=True, specname="pytest_runtest_protocol")
def pytest_inner(item):
    result = yield
    events.append([item.name, "inner", result])
    if item.name == "test_error":
        raise ValueError("original")
    return "inner"

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write("test_protocol.py", "def test_result(): pass\ndef test_error(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            ["test_result", "inner", True],
            ["test_result", "legacy-result", "inner"],
            ["test_result", "outer", "legacy"],
            ["test_error", "inner", True],
            ["test_error", "legacy-error", "original"],
            ["test_error", "recovered", "replacement"],
        ]
    )


def test_protocol_hooks_match_global_conftest_scope_and_parametrized_identity(
    tmp_path: Path,
) -> None:
    """Protocol hooks see concrete cases and pytest's global conftest scope."""

    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import json
from pathlib import Path
import pytest

events = []

def pytest_sessionstart(session):
    session.config._protocol_events = events

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    events.append(["root", item.nodeid, None if nextitem is None else nextitem.nodeid])
    return (yield)

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write(
            "nested/conftest.py",
            """\
import pytest

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    value = item.callspec.params["value"] if hasattr(item, "callspec") else None
    item.config._protocol_events.append(["nested", item.nodeid, value])
    return (yield)
""",
        )
        # Pytest invokes protocol hooks globally, including the nested conftest.
        project.write("test_root.py", "def test_root(): pass\n")
        project.write(
            "nested/test_nested.py",
            "import pytest\n@pytest.mark.parametrize('value', [1, 2, 3])\n"
            "def test_case(value): pass\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert observed["pytest-native"] == observed["pytest"]
    assert [row[2] for row in observed["pytest-native"] if row[0] == "nested"] == [1, 2, 3, None]
    assert [row[1] for row in observed["pytest-native"] if row[0] == "root"] == [
        "nested/test_nested.py::test_case[1]",
        "nested/test_nested.py::test_case[2]",
        "nested/test_nested.py::test_case[3]",
        "test_root.py::test_root",
    ]


def test_protocol_wrapper_recovers_failed_entry_before_running_next_test(tmp_path: Path) -> None:
    """Recovery before the runner skips that protocol and permits the next test."""

    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(hookwrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer(item):
    outcome = yield
    if outcome.excinfo is not None:
        events.append([item.name, "recovered", str(outcome.excinfo[1])])
        outcome.force_result(True)
    else:
        events.append([item.name, "completed", outcome.get_result()])

@pytest.hookimpl(wrapper=True, specname="pytest_runtest_protocol")
def pytest_inner(item):
    events.append([item.name, "enter"])
    if item.name == "test_first":
        raise ValueError("entry failed")
    try:
        return (yield)
    finally:
        events.append([item.name, "cleanup"])

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write(
            "test_protocol.py",
            "def test_first(): raise AssertionError('must not run')\ndef test_second(): pass\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            ["test_first", "enter"],
            ["test_first", "recovered", "entry failed"],
            ["test_second", "enter"],
            ["test_second", "cleanup"],
            ["test_second", "completed", True],
        ]
    )


@pytest.mark.parametrize("suppress", [False, True], ids=["propagate", "suppress"])
def test_protocol_wrapper_observes_interruption_before_fixture_cleanup(
    tmp_path: Path, suppress: bool
) -> None:
    """A control exception reaches wrappers before session fixture cleanup."""

    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            f"""\
import json
from pathlib import Path
import pytest

events = []

@pytest.fixture(scope="session")
def resource():
    events.append("setup")
    yield events
    events.append("finalize")

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    try:
        return (yield)
    except KeyboardInterrupt:
        events.append("interrupted")
        {"return True" if suppress else "raise"}

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish():
    try:
        return (yield)
    finally:
        Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write(
            "test_protocol.py",
            "def test_first(resource):\n    resource.append('first')\n    raise KeyboardInterrupt\n"
            "def test_second(resource):\n    resource.append('second')\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--workers=1", "--no-cov", "-q")
        )
        assert result.returncode == (1 if suppress else 2), result.stdout + result.stderr
        if suppress:
            assert "previous item was not torn down properly" in result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [
            "setup",
            "first",
            "interrupted",
            "finalize",
        ]
    )


def test_protocol_replacement_rejection_cannot_be_suppressed(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """\
import pytest

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer():
    try:
        return (yield)
    except Exception:
        return True

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol():
    return False
""",
    )
    project.write(
        "test_protocol.py",
        "from pathlib import Path\ndef test_first(): Path('ran').touch()\n",
    )
    result = project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
    assert result.returncode != 0
    assert "native pytest cannot run the test protocol from" in result.stdout + result.stderr
    assert not (project.root / "ran").exists()


@pytest.mark.parametrize("tryfirst", [False, True])
def test_protocol_warning_filters_follow_the_warnings_wrapper_slot(
    tmp_path: Path, tryfirst: bool
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope = function\n")
        project.write(
            "conftest.py",
            f"""\
import json
from pathlib import Path
import warnings
import pytest

events = []

def pytest_sessionstart():
    warnings.filterwarnings("error", message="protocol sentinel", category=UserWarning)

def warn(stage):
    try:
        warnings.warn("protocol sentinel", UserWarning)
    except UserWarning:
        events.append([stage, "error"])
    else:
        events.append([stage, "ignored"])

@pytest.hookimpl(wrapper=True, tryfirst={tryfirst!r})
def pytest_runtest_protocol():
    warn("before")
    result = yield
    warn("after")
    return result

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write(
            "test_protocol.py",
            "import pytest\n@pytest.mark.filterwarnings('ignore:protocol sentinel:UserWarning')\n"
            "def test_case(): pass\n",
        )
        result = (
            project.run_pytest(
                "-p", "vfy.asyncio_plugin", "-q", "-W", "error::UserWarning", load_xdist=False
            )
            if engine == "pytest"
            else project.run_vfy(
                "test", "--engine=pytest-native", "--no-cov", "-q", "-W", "error::UserWarning"
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    expected = "error" if tryfirst else "ignored"
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == [["before", expected], ["after", expected]]
    )


@pytest.mark.parametrize("legacy", [False, True], ids=["wrapper", "hookwrapper"])
@pytest.mark.parametrize("invalid", ["no_yield", "second_yield"])
def test_invalid_protocol_wrappers_match_pytest_errors_cleanup_and_reuse(
    tmp_path: Path, legacy: bool, invalid: str
) -> None:
    implementations = {
        "no_yield": """\
def inner(item):
    try:
        if item.name != "test_first":
            yield
    finally:
        events.append([item.name, "closed"])
""",
        "second_yield": """\
def inner(item):
    try:
        yield
        if item.name == "test_first":
            yield
    finally:
        events.append([item.name, "closed"])
""",
    }
    fragment = {
        "no_yield": "did not yield",
        "second_yield": "has second yield",
    }[invalid]
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            f"""\
import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer(item):
    try:
        return (yield)
    except Exception as error:
        events.append([item.name, type(error).__name__, {fragment!r} in str(error)])
        return True

{implementations[invalid]}
pytest_inner = pytest.hookimpl(
    {"hookwrapper" if legacy else "wrapper"}=True, specname="pytest_runtest_protocol"
)(inner)

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write("test_protocol.py", "def test_first(): pass\ndef test_second(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode in (0, 3), result.stdout + result.stderr
        if result.returncode:
            assert fragment in result.stdout + result.stderr
        observed[engine] = (
            result.returncode,
            json.loads((project.root / "events.json").read_text()),
        )
    assert observed["pytest-native"] == observed["pytest"]
    status, events = observed["pytest-native"]
    if not legacy or status == 0:
        assert ["test_first", "closed"] in events
    if status == 0:
        assert ["test_first", "RuntimeError", True] in events
        assert ["test_second", "closed"] in events


def test_non_generator_protocol_wrapper_is_rejected_before_execution(tmp_path: Path) -> None:
    results = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            "import pytest\n@pytest.hookimpl(wrapper=True)\n"
            "def pytest_runtest_protocol(): return None\n",
        )
        project.write(
            "test_protocol.py", "from pathlib import Path\ndef test_case(): Path('ran').touch()\n"
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert "function is not a generator function" in result.stdout + result.stderr
        assert not (project.root / "ran").exists()
        results[engine] = result.returncode
    assert results["pytest-native"] == results["pytest"] != 0


@pytest.mark.parametrize("failure", ["entry", "call"])
def test_parallel_protocol_recovery_is_rejected_before_omitting_worker_cases(
    tmp_path: Path, failure: str
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        f"""\
from pathlib import Path
import pytest

@pytest.hookimpl(hookwrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer(item):
    outcome = yield
    if outcome.excinfo is not None:
        outcome.force_result(True)
        Path(item.path.stem + ".recovered").touch()

@pytest.hookimpl(wrapper=True, specname="pytest_runtest_protocol")
def pytest_inner(item):
    if item.name == "test_first" and {failure == "entry"!r}:
        raise ValueError("entry failed")
    return (yield)
""",
    )
    for name in ("test_a", "test_b"):
        project.write(
            f"{name}.py",
            "from pathlib import Path\n"
            "def test_first(): raise KeyboardInterrupt\n"
            "def test_second(): Path(__file__).with_suffix('.completed').touch()\n",
        )
    result = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--no-cov",
        "-q",
        env={"VFY_WORKER_START": "exec"},
    )
    assert result.returncode != 0
    assert "suppressed a protocol error; use --workers=1" in result.stdout + result.stderr
    assert list(project.root.glob("*.recovered"))
    assert not list(project.root.glob("*.completed"))


def test_protocol_wrappers_run_in_each_native_worker(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
from pathlib import Path
import os
import pytest

@pytest.hookimpl(hookwrapper=True, specname="pytest_runtest_protocol")
def pytest_legacy(item):
    item.legacy_entered = True
    outcome = yield
    assert outcome.get_result() is True
    Path(item.path.stem + ".completed").write_text(str(os.getpid()))

@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    item.wrapper_entered = True
    return (yield)
""",
        )
        for name in ("test_a", "test_b"):
            project.write(
                f"{name}.py",
                "def test_case(request):\n"
                "    assert request.node.wrapper_entered\n"
                "    assert request.node.legacy_entered\n",
            )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy(
                "test",
                "--engine=pytest-native",
                "--workers=2",
                "--no-cov",
                "-q",
                env={"VFY_WORKER_START": "exec"},
            )
        )
        assert result.returncode == 0, result.stdout + result.stderr
        worker_pids = {
            (project.root / f"{name}.completed").read_text() for name in ("test_a", "test_b")
        }
        assert len(worker_pids) == (2 if engine == "pytest-native" else 1), (
            result.stdout + result.stderr
        )


@pytest.mark.parametrize("legacy", [False, True], ids=["wrapper", "hookwrapper"])
def test_protocol_wrappers_preserve_stop_iteration_from_plain_hooks(
    tmp_path: Path, legacy: bool
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini", "[pytest]\nfilterwarnings=ignore::pluggy.PluggyTeardownRaisedWarning\n"
        )
        project.write(
            "conftest.py",
            f"""\
import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer(item):
    try:
        return (yield)
    except BaseException as error:
        events.append([item.name, type(error).__name__, str(error)])
        return True

@pytest.hookimpl({"hookwrapper" if legacy else "wrapper"}=True,
                 specname="pytest_runtest_protocol")
def pytest_inner():
    outcome = yield
    {"outcome.get_result()" if legacy else "return outcome"}

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item):
    if item.name == "test_first":
        raise StopIteration("stopped")

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write(
            "test_protocol.py",
            "def test_first(): raise AssertionError('must not run')\ndef test_second(): pass\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode in (0, 3), result.stdout + result.stderr
        if result.returncode:
            assert "RuntimeError: generator raised StopIteration" in result.stdout + result.stderr
        observed[engine] = (
            result.returncode,
            json.loads((project.root / "events.json").read_text()),
        )
    assert observed["pytest-native"] == observed["pytest"]
    status, events = observed["pytest-native"]
    if status == 0:
        assert len(events) == 1
        assert events[0][0] == "test_first"
        assert events[0][1] in ("StopIteration", "RuntimeError")


@pytest.mark.parametrize("stop", ["shouldstop", "shouldfail"])
def test_protocol_session_stop_finalizes_broad_fixtures_before_wrapper_exit(
    tmp_path: Path, stop: str
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            f"""\
import json
from pathlib import Path
import pytest

events = []

@pytest.fixture(scope="session")
def resource():
    events.append("setup")
    yield events
    events.append("finalize")

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    item.session.{stop} = "protocol stop sentinel"
    result = yield
    events.append("wrapper-exit")
    return result

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write(
            "test_protocol.py",
            "def test_first(resource): resource.append('first')\n"
            "def test_second(resource): resource.append('second')\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode == (2 if stop == "shouldstop" else 1), (
            result.stdout + result.stderr
        )
        assert "protocol stop sentinel" in result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert (
        observed["pytest-native"]
        == observed["pytest"]
        == ["setup", "first", "finalize", "wrapper-exit"]
    )


def test_protocol_failure_preserves_session_fixture_cleanup_failure(tmp_path: Path) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import pytest

@pytest.fixture(scope="session")
def resource():
    yield
    raise RuntimeError("session cleanup sentinel")

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol():
    yield
    raise ValueError("protocol failure sentinel")
""",
        )
        project.write(
            "test_protocol.py",
            "def test_first(resource): pass\ndef test_second(resource): pass\n",
        )
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "ValueError: protocol failure sentinel" in result.stdout + result.stderr
        assert "RuntimeError: session cleanup sentinel" in result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["call", "entry_after_pass"])
def test_suppressed_final_protocol_defers_finalizer_failure_to_sessionfinish(
    tmp_path: Path, failure: str
) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import pytest

@pytest.fixture(scope="session")
def resource():
    yield
    raise RuntimeError("suppressed protocol cleanup sentinel")

@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol():
    try:
        return (yield)
    except (KeyboardInterrupt, ValueError):
        return True
""",
        )
        if failure == "entry_after_pass":
            with (project.root / "conftest.py").open("a") as stream:
                stream.write(
                    "\n@pytest.hookimpl(wrapper=True, specname='pytest_runtest_protocol')\n"
                    "def pytest_inner(item):\n"
                    "    if item.name == 'test_omitted': raise ValueError('entry failed')\n"
                    "    return (yield)\n"
                )
            source = "def test_first(resource): pass\ndef test_omitted(): assert False\n"
        else:
            source = "def test_case(resource): raise KeyboardInterrupt\n"
        project.write("test_protocol.py", source)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "RuntimeError: suppressed protocol cleanup sentinel" in result.stderr
        assert "INTERNALERROR" not in result.stdout + result.stderr


def test_native_xdist_rejects_project_protocol_hooks_before_execution(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        "import pytest\n@pytest.hookimpl(wrapper=True)\n"
        "def pytest_runtest_protocol(): return (yield)\n",
    )
    project.write(
        "test_protocol.py", "from pathlib import Path\ndef test_case(): Path('ran').touch()\n"
    )
    result = project.run_vfy("test", "--engine=pytest-native", "--numprocesses=2", "--no-cov", "-q")
    assert result.returncode != 0
    assert (
        "native pytest-xdist does not execute project pytest_runtest_protocol hooks; "
        "remove pytest-xdist options such as -n from the command line and addopts and "
        "use vfy's --workers"
    ) in (result.stdout + result.stderr)
    assert not (project.root / "ran").exists()


def test_disabled_xdist_protocol_hooks_and_reports_match_pytest(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            _CONFTEST + "\n@pytest.fixture(scope='session')\n"
            "def resource():\n"
            "    events.append(['resource-setup'])\n"
            "    yield\n"
            "    events.append(['resource-cleanup'])\n\n"
            "def pytest_runtest_logstart(nodeid):\n"
            "    events.append(['start', nodeid])\n\n"
            "def pytest_runtest_logreport(report):\n"
            "    events.append(['report', report.nodeid, report.when, report.outcome])\n\n"
            "def pytest_runtest_logfinish(nodeid):\n"
            "    events.append(['finish', nodeid])\n",
        )
        project.write(
            "test_protocol.py",
            "def test_first(resource): pass\n"
            "def test_failure(resource): assert False\n"
            "def test_omitted(): raise RuntimeError('must not run')\n",
        )
        arguments = ("-p", "xdist", "--numprocesses=0", "--maxfail=1", "-q")
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", *arguments)
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "1 failed, 1 passed" in result.stdout
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations[0] == observations[1]
    assert ["plain", "test_first"] in observations[1]
    assert ["resource-cleanup"] in observations[1]
    assert ["plain", "test_omitted"] not in observations[1]


def test_disabled_xdist_interruption_omits_protocol_finish_as_pytest(tmp_path: Path) -> None:
    observations = []
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            "import json\nfrom pathlib import Path\nimport pytest\nevents = []\n\n"
            "@pytest.hookimpl(wrapper=True)\n"
            "def pytest_runtest_protocol(item):\n"
            "    events.append('before')\n"
            "    try:\n"
            "        return (yield)\n"
            "    finally:\n"
            "        events.append('after')\n\n"
            "def pytest_runtest_logstart(nodeid): events.append('start')\n"
            "def pytest_runtest_logfinish(nodeid): events.append('finish')\n"
            "def pytest_sessionfinish():\n"
            "    Path('events.json').write_text(json.dumps(events))\n",
        )
        project.write("test_protocol.py", "def test_interrupt(): raise KeyboardInterrupt\n")
        arguments = ("-p", "xdist", "--numprocesses=0", "-q")
        result = (
            project.run_pytest(*arguments, load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", *arguments)
        )
        assert result.returncode == pytest.ExitCode.INTERRUPTED, result.stdout + result.stderr
        observations.append(json.loads((project.root / "events.json").read_text()))
    assert observations == [["before", "start", "after"]] * 2


def test_protocol_hooks_registered_during_execution_are_rejected(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        """\
from pathlib import Path

class LatePlugin:
    def pytest_runtest_protocol(self):
        Path("late-hook-ran").touch()

def pytest_runtest_setup(item):
    item.config.pluginmanager.register(LatePlugin(), "late-protocol")
""",
    )
    project.write(
        "test_protocol.py", "from pathlib import Path\ndef test_case(): Path('test-ran').touch()\n"
    )
    result = project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
    result.expect(1)
    assert "requires protocol hooks to register before execution" in result.stdout + result.stderr
    assert "late-protocol" in result.stdout + result.stderr
    assert not (project.root / "late-hook-ran").exists()
    assert not (project.root / "test-ran").exists()


@pytest.mark.parametrize("failure", ["call", "entry_after_pass"])
def test_suppressed_final_protocol_retains_session_cleanup_warnings(
    tmp_path: Path, failure: str
) -> None:
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """\
import warnings
import pytest

@pytest.fixture(scope="session")
def resource():
    yield
    warnings.warn("suppressed cleanup warning", UserWarning)

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer():
    try:
        return (yield)
    except (KeyboardInterrupt, ValueError):
        return True

@pytest.hookimpl(wrapper=True, specname="pytest_runtest_protocol")
def pytest_inner(item):
    if item.name == "test_omitted":
        raise ValueError("entry failed")
    return (yield)
""",
        )
        source = (
            "def test_first(resource): pass\ndef test_omitted(): assert False\n"
            if failure == "entry_after_pass"
            else "def test_case(resource): raise KeyboardInterrupt\n"
        )
        project.write("test_protocol.py", source)
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 warning in" in result.stdout
        assert "UserWarning: suppressed cleanup warning" in result.stdout
        assert "UserWarning: suppressed cleanup warning" not in result.stderr


def test_legacy_wrappers_preserve_installed_result_identity_and_exception_traceback(
    tmp_path: Path,
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            """\
import json
from pathlib import Path
import pytest

saved = []
events = []

@pytest.hookimpl(hookwrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_z_outer():
    outcome = yield
    events.append([outcome is saved[0], outcome.excinfo[2] is None])
    assert str(outcome.exception) == "forced exception"
    outcome.force_result(True)

@pytest.hookimpl(hookwrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_a_inner():
    outcome = yield
    saved.append(outcome)
    outcome.force_exception(ValueError("forced exception"))

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write("test_protocol.py", "def test_case(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode == 0, result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert observed["pytest-native"] == observed["pytest"]
    assert len(observed["pytest-native"]) == 1


def test_legacy_teardown_warning_identifies_plugin_and_active_dispatcher(tmp_path: Path) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "pytest.ini", "[pytest]\nfilterwarnings=always::pluggy.PluggyTeardownRaisedWarning\n"
        )
        project.write(
            "conftest.py",
            """\
import json
import linecache
from pathlib import Path
import warnings
import pytest
from pluggy import PluggyTeardownRaisedWarning

recovered = []
original_formatwarning = warnings.formatwarning

def formatwarning(message, category, filename, lineno, line=None):
    if issubclass(category, PluggyTeardownRaisedWarning):
        metadata = [str(message), category.__name__, filename, lineno,
                    linecache.getline(filename, lineno).strip()]
        Path("warning.json").write_text(json.dumps(metadata))
    return original_formatwarning(message, category, filename, lineno, line)

def pytest_sessionstart():
    warnings.formatwarning = formatwarning

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_outer():
    try:
        return (yield)
    except ValueError:
        recovered.append("recovered")
        return True

@pytest.hookimpl(hookwrapper=True, specname="pytest_runtest_protocol")
def pytest_inner(item):
    yield
    if item.name == "test_first":
        raise ValueError("legacy teardown sentinel")

def pytest_sessionfinish():
    Path("events.json").write_text(json.dumps(recovered))
""",
        )
        project.write("test_protocol.py", "def test_first(): pass\ndef test_second(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode in (0, 3), result.stdout + result.stderr
        record = json.loads((project.root / "warning.json").read_text())
        recovered = json.loads((project.root / "events.json").read_text())
        message, category, filename, lineno, source = record
        assert Path(filename).name == ("main.py" if engine == "pytest" else "native_pytest.py")
        assert lineno > 0
        assert source
        assert "Hook: pytest_runtest_protocol" in message
        assert "ValueError: legacy teardown sentinel" in message
        observed[engine] = (
            result.returncode,
            message.replace(str(project.root), "<project>"),
            category,
            recovered,
        )
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"][2] == "PluggyTeardownRaisedWarning"


@pytest.mark.parametrize("collect_cycles", [False, True])
def test_aborted_legacy_protocol_preserves_retained_wrapper_cleanup_order(
    tmp_path: Path, collect_cycles: bool
) -> None:
    observed = {}
    for engine in ("pytest", "pytest-native"):
        project = VfyProject(tmp_path / engine)
        project.write(
            "conftest.py",
            f"""\
import gc
import json
from pathlib import Path
import pytest

events = []

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_z_outer():
    try:
        return (yield)
    finally:
        events.append("outer-finally")

@pytest.hookimpl(wrapper=True, tryfirst=True, specname="pytest_runtest_protocol")
def pytest_y_inner():
    try:
        return (yield)
    finally:
        events.append("inner-finally")

@pytest.hookimpl(hookwrapper=True, specname="pytest_runtest_protocol")
def pytest_bad():
    yield
    raise ValueError("abort legacy teardown")

def pytest_sessionfinish():
    events.append("sessionfinish")
    if {collect_cycles!r}:
        gc.collect()
    Path("events.json").write_text(json.dumps(events))
""",
        )
        project.write("test_protocol.py", "def test_case(): pass\n")
        result = (
            project.run_pytest("-q", load_xdist=False)
            if engine == "pytest"
            else project.run_vfy("test", "--engine=pytest-native", "--no-cov", "-q")
        )
        assert result.returncode == 3, result.stdout + result.stderr
        assert "ValueError: abort legacy teardown" in result.stdout + result.stderr
        observed[engine] = json.loads((project.root / "events.json").read_text())
    assert observed["pytest-native"] == observed["pytest"]
    assert observed["pytest-native"].count("sessionfinish") == 1
    if collect_cycles:
        assert sorted(observed["pytest-native"]) == [
            "inner-finally",
            "outer-finally",
            "sessionfinish",
        ]
