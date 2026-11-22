"""Item protocol hooks retain real reports and release native callbacks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def test_protocol_order_and_nextitem_follow_selected_execution_sequence(tmp_path: Path) -> None:
    snapshots = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "outer.py",
            """import pytest
from pathlib import Path

def event(value):
    with Path('events.jsonl').open('a') as stream:
        import json
        stream.write(json.dumps(value)+'\\n')
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_protocol(item,nextitem):
    event(['outer-before',item.name,None if nextitem is None else nextitem.name])
    result=yield
    event(['outer-after',item.name,result])
    return result
@pytest.hookimpl(tryfirst=True,specname='pytest_runtest_protocol')
def pytest_plain(item):
    event(['plain',item.name])
""",
        )
        project.write(
            "conftest.py",
            """import pytest
from outer import event

def pytest_collection_modifyitems(config,items):
    by_name={item.name:item for item in items}
    config.hook.pytest_deselected(items=[by_name['test_b']])
    items[:]=[by_name[name] for name in ['test_d','test_c','test_a']]
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    event(['legacy-before',item.name])
    outcome=yield
    event(['legacy-after',item.name,outcome.get_result()])
""",
        )
        project.write(
            "test_body.py",
            "from outer import event\n"
            + "".join(f"def test_{name}(): event(['body','test_{name}'])\n" for name in "abcd"),
        )
        result = (
            project.run_python("-m", "pytest", "-q", "-p", "outer")
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=native", "--no-cov", "--no-header", "-p", "outer"
            )
        )
        result.expect(0)
        events = [
            json.loads(line) for line in (project.root / "events.jsonl").read_text().splitlines()
        ]
        snapshots.append(events)
    assert snapshots[0] == snapshots[1]
    assert [value for value in snapshots[1] if value[0] == "outer-before"] == [
        ["outer-before", "test_d", "test_c"],
        ["outer-before", "test_c", "test_a"],
        ["outer-before", "test_a", None],
    ]
    assert [value[1] for value in snapshots[1] if value[0] == "body"] == [
        "test_d",
        "test_c",
        "test_a",
    ]


def test_protocol_wrapper_error_after_default_retains_completed_case(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """import pytest
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result=yield
    assert result is True
    raise RuntimeError('after completed native protocol')
""",
    )
    project.write("test_body.py", "def test_body(): pass\n")
    result = project.run_vfy(
        "test", "--engine=native", "--no-cov", "--native-result-json=results.json"
    )
    result.expect(3)
    record = json.loads((tmp_path / "results.json").read_text())
    assert len(record["cases"]) == 1
    assert record["counts"]["passed"] == 1
    assert "after completed native protocol" in str(record["internal_errors"])


@pytest.mark.parametrize("mode", ["same-phase", "foreign"])
def test_post_default_protocol_reports_preserve_edits_and_enforce_item_ownership(
    tmp_path: Path, mode: str
) -> None:
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            f"""import pytest
from pathlib import Path
calls=[]
def pytest_runtest_logreport(report):
    if report.when=='call': calls.append(report)
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result=yield
    if {mode!r}=='same-phase':
        report=calls[-1]
        report.outcome='failed'
        report.longrepr='actual post-default report edit'
        item.ihook.pytest_runtest_logreport(report=report)
    elif item.name=='test_b':
        item.ihook.pytest_runtest_logreport(report=calls[0])
    return result
""",
        )
        project.write(
            "test_body.py",
            "from pathlib import Path\n"
            + "".join(
                f"def test_{name}(): Path('{name}.body').write_text('ran')\n"
                for name in (("a",) if mode == "same-phase" else ("a", "b"))
            ),
        )
        result = (
            project.run_python("-m", "pytest", "-q")
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=native", "--no-cov", "--native-result-json=results.json"
            )
        )
        expected = 1 if mode == "same-phase" else (0 if route == "stock" else 4)
        assert result.returncode == expected, result.stdout + result.stderr
        assert (project.root / "a.body").read_text() == "ran"
        if mode == "foreign":
            assert (project.root / "b.body").read_text() == "ran"
            if route == "independent":
                assert "test protocol report belongs to another native case" in result.stderr
        elif route == "independent":
            record = json.loads((project.root / "results.json").read_text())
            assert len(record["cases"]) == 1
            assert record["counts"]["failed"] == 1
            assert record["counts"]["passed"] == 0
            assert len(record["cases"][0]["phases"]) == 3
            assert "actual post-default report edit" in str(record["cases"])


def test_post_default_setup_skip_replay_uses_current_case_reports(tmp_path: Path) -> None:
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "observer.py",
            """import pytest
current_setup=None
def pytest_collection_modifyitems(items):
    items[0].add_marker(pytest.mark.skip(reason='no native call phase'))
def pytest_runtest_logreport(report):
    global current_setup
    if report.when=='setup': current_setup=report
@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    result=yield
    assert current_setup.nodeid==item.nodeid
    item.ihook.pytest_runtest_logreport(report=current_setup)
    return result
""",
        )
        project.write("test_body.py", "def test_a():\n    raise AssertionError('skipped')\n")
        arguments = ["-p", "observer", "test_body.py"]
        if route == "stock":
            result = project.run_python("-m", "pytest", "-q", *arguments)
        else:
            result = project.run_vfy(
                "test",
                "--engine=native",
                "--no-cov",
                "--native-result-json=results.json",
                *arguments,
            )
        result.expect(0)
        if route == "independent":
            record = json.loads((project.root / "results.json").read_text())
            assert len(record["cases"]) == 1
            assert record["counts"]["skipped"] == 1
            assert [phase["phase"] for phase in record["cases"][0]["phases"]] == [
                "setup",
                "teardown",
            ]


@pytest.mark.parametrize("stop", ["maxfail", "shouldfail", "shouldstop"])
def test_native_session_stop_keeps_teardown_target_and_public_exception(
    tmp_path: Path, stop: str
) -> None:
    snapshots = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "conftest.py",
            """import json,pytest
from pathlib import Path
events=[]
def event(value): events.append(value)
@pytest.fixture(scope='session')
def shared():
    event(['fixture-enter'])
    yield
    event(['fixture-exit'])
@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item,nextitem):
    event(['teardown-next',item.name,None if nextitem is None else nextitem.name])
    yield
    event(['teardown-done'])
@pytest.hookimpl(wrapper=True)
def pytest_runtestloop(session):
    try: result=yield
    except session.Failed as error:
        event(['loop-failed',type(error).__name__,str(error)])
        raise
    except session.Interrupted as error:
        event(['loop-interrupted',type(error).__name__,str(error)])
        raise
    return result
def pytest_keyboard_interrupt(): event(['keyboard-interrupt'])
def pytest_internalerror(): event(['internal-error'])
def pytest_sessionfinish(exitstatus): event(['finish',int(exitstatus)])
def pytest_unconfigure(): Path('events.json').write_text(json.dumps(events))
""",
        )
        project.write(
            "test_body.py",
            f"""from conftest import event

def test_a(request,shared):
    event(['body-a'])
    if {stop!r}=='maxfail': assert False
    if {stop!r}=='shouldfail': request.session.shouldfail='actual session failure'
    if {stop!r}=='shouldstop': request.session.shouldstop='actual session interruption'
def test_b(): event(['body-b'])
""",
        )
        arguments = ("-x",) if stop == "maxfail" else ()
        result = (
            project.run_python("-m", "pytest", "-q", *arguments)
            if route == "stock"
            else project.run_vfy("test", "--engine=native", "--no-cov", "--no-header", *arguments)
        )
        assert result.returncode == (2 if stop == "shouldstop" else 1), (
            result.stdout + result.stderr
        )
        events = json.loads((project.root / "events.json").read_text())
        assert ["body-a"] in events
        assert ["body-b"] not in events
        assert ["teardown-next", "test_a", None] in events
        assert events.index(["fixture-exit"]) < events.index(["teardown-done"])
        assert ["internal-error"] not in events
        snapshots.append(events)
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("-x",),
        ("--maxfail=0",),
        ("--maxfail=-1",),
        (f"--maxfail={10**100}",),
        ("-x", "--maxfail=0"),
        ("--maxfail=0", "-x"),
    ],
)
@pytest.mark.parametrize("workers", [1, 2])
def test_maxfail_cli_values_control_actual_failing_body_count(
    tmp_path: Path, arguments: tuple[str, ...], workers: int
) -> None:
    observations = []
    for route in ("stock", "independent"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\n")
        project.write(
            "test_body.py",
            """import json
from pathlib import Path

def fail(request,name):
    with Path('bodies.jsonl').open('a') as stream:
        stream.write(json.dumps([name,request.config.getoption('maxfail')])+'\\n')
    assert False,'actual body failure'
def test_a(request): fail(request,'a')
def test_b(request): fail(request,'b')
""",
        )
        result = (
            project.run_python("-m", "pytest", "-q", *arguments)
            if route == "stock"
            else project.run_vfy(
                "test", "--engine=native", f"--workers={workers}", "--no-cov", *arguments
            )
        )
        result.expect(1)
        bodies = [
            json.loads(line) for line in (project.root / "bodies.jsonl").read_text().splitlines()
        ]
        assert bodies
        observations.append(bodies)
    if workers == 1:
        assert observations[0] == observations[1]
        return
    expected, actual = (sorted(rows, key=json.dumps) for rows in observations)
    if len(expected) == 2:
        assert actual == expected
        return
    # Either worker can fail first; the other may finish its body before the shared limit lands.
    assert 1 <= len(actual) <= 2
    assert {name for name, _ in actual} <= {"a", "b"}
    assert {limit for _, limit in actual} == {expected[0][1]}
