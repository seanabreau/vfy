"""Compare native execution through the installed upstream xdist boundary."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from html import unescape
from pathlib import Path
from textwrap import dedent
from typing import Literal

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")) or importlib.util.find_spec("xdist") is None,
    reason="native xdist execution requires Linux or macOS and pytest-xdist",
)

Route = Literal["upstream", "native"]
JunitElement = tuple[str, tuple[tuple[str, str], ...], str]
JunitCase = tuple[str, str, tuple[JunitElement, ...]]

_PROBE_CONFTEST = dedent("""\
    import json
    import os
    from pathlib import Path
    import pytest

    def record(config, event):
        worker = getattr(config, 'workerinput', {}).get('workerid', 'controller')
        root = Path('trace')
        root.mkdir(exist_ok=True)
        with (root / (worker + '.jsonl')).open('a') as stream:
            stream.write(json.dumps(event) + '\\n')

    def pytest_configure(config):
        pass

    def pytest_xdist_auto_num_workers(config):
        return 2

    @pytest.hookimpl(wrapper=True)
    def pytest_xdist_make_scheduler(config, log):
        scheduler = yield
        record(config, {'event': 'scheduler', 'class': type(scheduler).__name__,
                        'module': type(scheduler).__module__})
        return scheduler

    def pytest_configure_node(node):
        node.workerinput['proof'] = 'controller-input'

    def pytest_testnodedown(node, error):
        record(node.config, {'event': 'down', 'worker': node.gateway.id,
                            'error': error is not None,
                            'proof': getattr(node, 'workeroutput', {}).get('proof')})

    def pytest_sessionfinish(session, exitstatus):
        if hasattr(session.config, 'workerinput'):
            session.config.workeroutput['proof'] = 'worker-output'

    def pytest_runtest_logreport(report):
        if hasattr(report, 'node'):
            record(report.node.config,
                   {'event': 'report', 'nodeid': report.nodeid,
                    'when': report.when, 'outcome': report.outcome,
                    'worker': getattr(report, 'worker_id', report.node.gateway.id),
                    'uid': getattr(report, 'testrun_uid', 'shared-run')})

    @pytest.fixture(autouse=True)
    def observed_case(request, worker_id, testrun_uid):
        assert worker_id == request.config.workerinput['workerid']
        assert worker_id == os.environ['PYTEST_XDIST_WORKER']
        assert os.environ['PYTEST_XDIST_WORKER_COUNT'] == '2'
        assert testrun_uid == request.config.workerinput['testrunuid'] == 'shared-run'
        assert os.environ['PYTEST_XDIST_TESTRUNUID'] == testrun_uid
        assert request.config.workerinput['proof'] == 'controller-input'
        record(request.config, {'event': 'case', 'nodeid': request.node.nodeid,
                                'worker': worker_id, 'uid': testrun_uid})
    """)


def _run(
    project: VfyProject,
    route: Route,
    *arguments: str,
    env: Mapping[str, str | None] | None = None,
) -> CommandResult:
    environment = dict(env or {})
    if route == "native":
        return project.run_vfy(
            "test",
            "--engine=pytest-native",
            "--no-replay",
            "--no-cov",
            "--testrunuid=shared-run",
            *arguments,
            env=environment,
        )
    return project.run_pytest(
        "-p",
        "xdist.plugin",
        "--testrunuid=shared-run",
        *arguments,
        env=environment,
        load_xdist=False,
    )


def _events(project: VfyProject, name: str = "controller") -> list[dict[str, object]]:
    return [
        json.loads(line) for line in (project.root / f"trace/{name}.jsonl").read_text().splitlines()
    ]


def _reports(project: VfyProject) -> list[tuple[str, str, str]]:
    return sorted(
        (str(event["nodeid"]), str(event["when"]), str(event["outcome"]))
        for event in _events(project)
        if event["event"] == "report"
    )


def _cases(project: VfyProject) -> list[dict[str, object]]:
    return [
        event
        for path in sorted((project.root / "trace").glob("gw*.jsonl"))
        for event in _events(project, path.stem)
        if event["event"] == "case"
    ]


@pytest.mark.parametrize("fixture_name", ["capsys", "capfd", "capsysbinary", "capfdbinary"])
def test_native_xdist_borrows_live_fd_capture_across_case_boundaries(
    tmp_path: Path, fixture_name: str
) -> None:
    observations = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                import logging
                import sys

                configure_proof = pytest_configure
                boundary_config = None
                initial_boundary = None

                def pytest_configure(config):
                    global boundary_config
                    boundary_config = config
                    configure_proof(config)

                def pytest_runtest_logstart(nodeid, location):
                    global initial_boundary
                    if not hasattr(boundary_config, 'workerinput'):
                        return
                    manager = boundary_config.pluginmanager.getplugin('capturemanager')
                    capture = manager._global_capturing
                    initial_boundary = (capture, sys.stdout, sys.stderr)

                def pytest_runtest_logfinish(nodeid, location):
                    if not hasattr(boundary_config, 'workerinput'):
                        return
                    manager = boundary_config.pluginmanager.getplugin('capturemanager')
                    capture = manager._global_capturing
                    assert (capture, sys.stdout, sys.stderr) == initial_boundary
                    record(boundary_config, {
                        'event': 'capture-boundary', 'nodeid': nodeid,
                        'state': capture._state, 'stdin_suspended': capture._in_suspended,
                    })
                    logging.warning('protocol-after-teardown')

                @pytest.hookimpl(trylast=True)
                def pytest_runtest_teardown(item, nextitem):
                    print('teardown-hook-output')
                    logging.warning('teardown-hook-log')

                def pytest_runtest_logreport(report):
                    if hasattr(report, 'node'):
                        record(report.node.config, {
                            'event': 'capture-report', 'nodeid': report.nodeid,
                            'when': report.when, 'outcome': report.outcome,
                            'sections': report.sections,
                        })
                """),
        )
        project.write(
            "test_capture.py",
            dedent("""\
                import os
                import sys

                collection_stdout = sys.stdout
                collection_stderr = sys.stderr

                def test_first(request):
                    assert sys.stdout is collection_stdout
                    assert sys.stderr is collection_stderr
                    assert not sys.stdout.closed and not sys.stderr.closed
                    print('first-python-output')
                    os.write(1, b'first-fd-output\\n')
                    with __import__('pytest').raises(OSError):
                        sys.stdin.read(1)

                def test_fixture(FIXTURE):
                    print('fixture-output')
                    sys.stderr.buffer.write(b'fixture-bytes\\n')
                    captured = FIXTURE.readouterr()
                    decode = lambda value: value.decode() if isinstance(value, bytes) else value
                    assert decode(captured.out) == 'fixture-output\\n'
                    assert decode(captured.err) == 'fixture-bytes\\n'
                    with FIXTURE.disabled():
                        print('disabled-output', flush=True)
                    print('fixture-resumed')
                    assert decode(FIXTURE.readouterr().out) == 'fixture-resumed\\n'

                def test_last():
                    assert sys.stdout is collection_stdout
                    assert sys.stderr is collection_stderr
                    assert not sys.stdout.closed and not sys.stderr.closed
                    print('last-python-output')
                    os.write(2, b'last-fd-error\\n')
                """).replace("FIXTURE", fixture_name),
        )
        result = _run(project, route, "--numprocesses=2", "--dist=loadfile", "--capture=fd", "-q")
        result.expect(0)
        assert "3 passed" in result.stdout
        reports = [event for event in _events(project) if event["event"] == "capture-report"]
        boundaries = [
            event
            for path in (project.root / "trace").glob("gw*.jsonl")
            for event in _events(project, path.stem)
            if event["event"] == "capture-boundary"
        ]
        observations[route] = (reports, boundaries)
    assert observations["native"] == observations["upstream"]
    assert len(observations["upstream"][0]) == 9


@pytest.mark.parametrize(
    ("distribution", "scheduler"),
    [
        ("load", "LoadScheduling"),
        ("loadscope", "LoadScopeScheduling"),
        ("loadfile", "LoadFileScheduling"),
        ("loadgroup", "LoadGroupScheduling"),
        ("worksteal", "WorkStealingScheduling"),
        ("each", "EachScheduling"),
    ],
)
def test_native_xdist_uses_upstream_schedulers_fixtures_and_worker_hooks(
    tmp_path: Path, distribution: str, scheduler: str
) -> None:
    reports: dict[str, list[tuple[str, str, str]]] = {}
    junit: dict[str, list[JunitCase]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _PROBE_CONFTEST)
        for module in ("alpha", "beta"):
            project.write(
                f"test_{module}.py",
                dedent("""\
                    import pytest
                    @pytest.mark.xdist_group('red')
                    class TestRed:
                        def test_first(self): pass
                        def test_second(self): pass
                    @pytest.mark.xdist_group('blue')
                    class TestBlue:
                        def test_first(self): pass
                        def test_second(self): pass
                    """),
            )
        result = _run(
            project,
            route,
            "--numprocesses",
            "2",
            f"--dist={distribution}",
            "--junitxml=results.xml",
            "-q",
        )
        result.expect(0)
        cases = _cases(project)
        multiplicity = 2 if distribution == "each" else 1
        assert len(cases) == 8 * multiplicity
        assert set(Counter(str(case["nodeid"]) for case in cases).values()) == {multiplicity}
        assert {case["worker"] for case in cases} == {"gw0", "gw1"}
        controller = _events(project)
        assert [event for event in controller if event["event"] == "scheduler"] == [
            {"event": "scheduler", "class": scheduler, "module": f"xdist.scheduler.{distribution}"}
        ]
        assert sorted(
            (str(event["worker"]), event["error"], event["proof"])
            for event in controller
            if event["event"] == "down"
        ) == [("gw0", False, "worker-output"), ("gw1", False, "worker-output")]
        groups: dict[str, set[str]] = {}
        for case in cases:
            nodeid = str(case["nodeid"])
            if distribution == "loadfile":
                group = nodeid.split("::")[0]
            elif distribution == "loadscope":
                group = nodeid.rsplit("::", 1)[0]
            elif distribution == "loadgroup":
                if tuple(map(int, pytest.version_tuple[:2])) >= (9, 2) and "@" not in nodeid:
                    # xdist 3.8 writes its group suffix to the retired _nodeid field.
                    continue
                group = nodeid.rsplit("@", 1)[1]
            else:
                continue
            groups.setdefault(group, set()).add(str(case["worker"]))
        assert all(len(workers) == 1 for workers in groups.values()), groups
        reports[route] = _reports(project)
        junit[route] = _junit_cases(project)
        assert len(junit[route]) == 8 * multiplicity
    assert reports["native"] == reports["upstream"]
    assert junit["native"] == junit["upstream"]


@pytest.mark.parametrize(
    "source", ["argument", "configuration", "environment", "plugins", "explicit-plugin"]
)
def test_native_xdist_loads_options_from_pytest_sources(tmp_path: Path, source: str) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _PROBE_CONFTEST)
        project.write("test_cases.py", "def test_first(): pass\ndef test_second(): pass\n")
        arguments = ("--numprocesses=2", "--dist=load", "--maxschedchunk=1")
        environment: dict[str, str | None] = {}
        if source == "configuration":
            project.write(
                "pytest.ini", "[pytest]\naddopts = --numprocesses 2 --dist=load --maxschedchunk=1\n"
            )
            arguments = ()
        elif source == "environment":
            environment["PYTEST_ADDOPTS"] = "--numprocesses 2 --dist=load --maxschedchunk=1"
            arguments = ()
        elif source == "plugins":
            environment["PYTEST_PLUGINS"] = "xdist.plugin"
            arguments = ("--numprocesses", "auto", "--maxprocesses=2")
        elif source == "explicit-plugin":
            arguments = ("-p", "xdist.plugin", "--numprocesses=2", "--dist=load")
        result = _run(project, route, *arguments, "-q", env=environment)
        result.expect(0)
        assert len(_cases(project)) == 2
        assert {event["class"] for event in _events(project) if event["event"] == "scheduler"} == {
            "LoadScheduling"
        }


def test_native_xdist_disabled_uses_serial_fixtures(tmp_path: Path) -> None:
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "test_serial.py",
            dedent("""\
                import os
                def test_serial(request, worker_id, testrun_uid):
                    assert worker_id == 'master'
                    assert len(testrun_uid) == 32
                    assert not hasattr(request.config, 'workerinput')
                    assert 'PYTEST_XDIST_WORKER' not in os.environ
                """),
        )
        result = _run(project, route, "--numprocesses=0", "-q")
        result.expect(0)
        assert "1 passed" in result.stdout


def test_native_xdist_preserves_scoped_fixtures_between_scheduling_chunks(tmp_path: Path) -> None:
    lifetimes: dict[str, list[tuple[str, ...]]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                @pytest.fixture(scope='session', autouse=True)
                def lifetime(request):
                    record(request.config, {'event': 'lifetime', 'action': 'session:setup'})
                    yield
                    record(request.config, {'event': 'lifetime', 'action': 'session:teardown'})
                @pytest.fixture(scope='module', autouse=True)
                def module_lifetime(request):
                    record(request.config, {'event': 'lifetime', 'action': 'module:setup'})
                    yield
                    record(request.config, {'event': 'lifetime', 'action': 'module:teardown'})
                @pytest.fixture(scope='class', autouse=True)
                def class_lifetime(request):
                    record(request.config, {'event': 'lifetime', 'action': 'class:setup'})
                    yield
                    record(request.config, {'event': 'lifetime', 'action': 'class:teardown'})
                """),
        )
        project.write(
            "test_chunks.py",
            "import pytest\nclass TestChunks:\n"
            "    @pytest.mark.parametrize('case', range(12))\n"
            "    def test_chunk(self, case): pass\n",
        )
        result = _run(project, route, "--numprocesses=2", "--maxschedchunk=1", "-q")
        result.expect(0)
        lifetimes[route] = []
        for worker in ("gw0", "gw1"):
            events = _events(project, worker)
            actions = tuple(
                str(event["action"]) for event in events if event["event"] == "lifetime"
            )
            assert sum(event["event"] == "case" for event in events) >= 2
            lifetimes[route].append(actions)
        expected = (
            "session:setup",
            "module:setup",
            "class:setup",
            "class:teardown",
            "module:teardown",
            "session:teardown",
        )
        assert lifetimes[route] == [expected, expected]
    assert lifetimes["native"] == lifetimes["upstream"]


def _junit_cases(project: VfyProject) -> list[JunitCase]:
    return sorted(
        (
            case.attrib["name"],
            case.attrib["classname"],
            tuple(
                (
                    child.tag,
                    tuple(sorted(child.attrib.items())),
                    re.sub(
                        r"(?<= object at )0x[0-9a-fA-F]+(?=>)",
                        "ADDRESS",
                        (child.text or "").replace(str(project.root), "PROJECT"),
                    ),
                )
                for child in case
                if child.tag in {"failure", "error", "skipped", "system-out", "system-err"}
            ),
        )
        for case in ET.parse(project.root / "results.xml").findall(".//testcase")
    )


def test_native_xdist_transports_fixture_failures_skips_and_junit_outcomes(tmp_path: Path) -> None:
    reports: dict[str, list[tuple[str, str, str]]] = {}
    junit: dict[str, list[JunitCase]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _PROBE_CONFTEST)
        project.write(
            "pytest.ini", "[pytest]\njunit_logging = all\njunit_log_passing_tests = true\n"
        )
        project.write(
            "test_outcomes.py",
            dedent("""\
                import sys
                import pytest
                @pytest.fixture
                def broken_setup():
                    print('setup-capture-sentinel')
                    raise RuntimeError('setup-sentinel')
                @pytest.fixture
                def broken_teardown():
                    yield
                    print('teardown-capture-sentinel')
                    raise RuntimeError('teardown-sentinel')
                def test_pass(): print('passing-capture-sentinel')
                def test_fail():
                    print('call-capture-sentinel')
                    print('stderr-capture-sentinel', file=sys.stderr)
                    assert False, 'call-sentinel'
                def test_setup(broken_setup): pass
                def test_teardown(broken_teardown): pass
                @pytest.mark.skip(reason='skip-sentinel')
                def test_skip(): pass
                @pytest.mark.xfail(reason='xfail-sentinel')
                def test_xfail(): assert False
                """),
        )
        result = _run(project, route, "--numprocesses=2", "--junitxml=results.xml", "-q")
        result.expect(1)
        assert "1 failed, 2 passed, 1 skipped, 1 xfailed, 2 errors" in result.stdout
        reports[route] = _reports(project)
        junit[route] = _junit_cases(project)
    assert reports["native"] == reports["upstream"]
    assert junit["native"] == junit["upstream"]


def _terminal_failures(stdout: str, *, include_summary: bool) -> str:
    """Return the failure sections, normalizing only worker ids and the duration."""
    start = re.search(r"^=+ (?:ERRORS|FAILURES|short test summary info) =+$", stdout, re.MULTILINE)
    assert start is not None, stdout
    failures = stdout[start.start() :]
    if not include_summary:
        # Stop at the first terminal-summary section after the failure reports.
        summary = re.search(r"^[=-]+ (?!ERRORS =|FAILURES =).+ [=-]+$", failures, re.MULTILINE)
        assert summary is not None, stdout
        failures = failures[: summary.start()]
    failures = re.sub(r"\bgw\d+\b", "gwN", failures)
    return re.sub(r" in \d+\.\d+s ", " in N.NNs ", failures)


@pytest.mark.parametrize("report_chars", [(), ("-rA",)], ids=["default", "rA"])
@pytest.mark.parametrize(
    ("tbstyle", "html"),
    [("auto", False), ("auto", True), ("no", False)],
    ids=["auto", "auto-html", "no"],
)
def test_native_xdist_failure_tracebacks_match_upstream_terminal_output(
    tmp_path: Path, tbstyle: str, html: bool, report_chars: tuple[str, ...]
) -> None:
    failures: dict[str, str] = {}
    for route in ("upstream", "native"):
        # Equal-length roots keep separator widths independent of the route.
        project = VfyProject(tmp_path / route[:6])
        project.write(
            "test_tracebacks.py",
            dedent("""\
                import pytest

                def check(value):
                    total = value + 1
                    assert total == 3, 'helper-sentinel'

                @pytest.fixture
                def broken():
                    raise RuntimeError('fixture-sentinel')

                def test_helper():
                    first = 1
                    second = 2
                    check(first)
                    assert second

                def test_multiline():
                    values = [1, 2, 3]
                    assert sum(
                        values
                    ) == 7

                def test_fixture(broken):
                    pass
                """),
        )
        # One file group runs in one worker, so the report order is deterministic.
        arguments = ["--numprocesses=2", "--dist=loadfile", f"--tb={tbstyle}", *report_chars]
        if html:
            # HTML publication projects each phase from a native result object.
            arguments.append("--html=report.html")
            if route == "upstream":
                arguments[:0] = ["-p", "pytest_html.plugin", "-p", "pytest_metadata.plugin"]
        result = _run(project, route, *arguments)
        result.expect(1)
        failures[route] = _terminal_failures(result.stdout, include_summary=not html)
    assert failures["native"] == failures["upstream"]


def test_native_xdist_maxfail_finalizes_live_worker_fixtures(tmp_path: Path) -> None:
    reports: dict[str, list[tuple[str, str, str]]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                @pytest.fixture(scope='session', autouse=True)
                def lifetime(request):
                    record(request.config, {'event': 'lifetime', 'action': 'setup'})
                    yield
                    record(request.config, {'event': 'lifetime', 'action': 'teardown'})
                """),
        )
        project.write(
            "test_failure.py",
            "def test_failure(): assert False, 'maxfail-sentinel'\n"
            "def test_pending_one(): assert False, 'pending-sentinel'\n"
            "def test_pending_two(): assert False, 'pending-sentinel'\n",
        )
        result = _run(
            project,
            route,
            "--numprocesses=2",
            "--dist=loadfile",
            "--maxfail=1",
            "--junitxml=results.xml",
            "-q",
        )
        assert result.returncode == pytest.ExitCode.INTERRUPTED, result.stdout + result.stderr
        assert "maxfail-sentinel" in result.stdout
        assert [name for name, _, _ in _junit_cases(project)] == ["test_failure"]
        active = {str(case["worker"]) for case in _cases(project)}
        assert len(active) == 1
        for worker in active:
            assert [
                event["action"]
                for event in _events(project, worker)
                if event["event"] == "lifetime"
            ] == ["setup", "teardown"]
        reports[route] = _reports(project)
    assert reports["native"] == reports["upstream"]


def test_native_xdist_restarts_a_crashed_worker_and_finishes_pending_cases(tmp_path: Path) -> None:
    reports: dict[str, list[tuple[str, str, str]]] = {}
    cache_failures: dict[str, bytes] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _PROBE_CONFTEST)
        project.write(
            "test_crash.py",
            "import os\nimport pytest\n"
            "def test_crash(): os._exit(23)\n"
            "@pytest.mark.parametrize('case', range(6))\ndef test_survivor(case): pass\n",
        )
        result = _run(project, route, "--numprocesses=2", "--max-worker-restart=1", "-q")
        result.expect(1)
        assert "1 failed, 6 passed" in result.stdout
        down = [event for event in _events(project) if event["event"] == "down"]
        assert sum(event["error"] is True for event in down) == 1
        assert {event["worker"] for event in down} == {"gw0", "gw1", "gw2"}
        assert all(event["proof"] == "worker-output" for event in down if event["error"] is False)
        reports[route] = _reports(project)
        cache_failures[route] = (project.root / ".pytest_cache/v/cache/lastfailed").read_bytes()
    assert reports["native"] == reports["upstream"]
    assert cache_failures["native"] == cache_failures["upstream"]


def test_native_xdist_cache_uses_mutated_reports_and_last_failed_recovery(tmp_path: Path) -> None:
    caches: dict[str, list[dict[str, bytes]]] = {}
    selected: dict[str, list[str]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                @pytest.hookimpl(specname='pytest_runtest_logreport', tryfirst=True)
                def pytest_change_published_outcome(report):
                    if (hasattr(report, 'node') and report.when == 'call'
                            and report.nodeid.endswith('test_recover')
                            and not Path('recovered').exists()):
                        report.outcome = 'failed'
                        report.longrepr = 'published controller failure'

                @pytest.fixture
                def recovering_teardown():
                    yield
                    if not Path('recovered').exists():
                        raise RuntimeError('teardown failure')
                """),
        )
        project.write(
            "test_cases.py",
            "import pytest\n"
            "def test_recover(): pass\n"
            "def test_teardown(recovering_teardown): pass\n"
            "def test_skip(): pytest.skip('call skipped')\n"
            "def test_other(): pass\n",
        )
        project.write(
            ".pytest_cache/v/cache/lastfailed",
            '{"historical.py::test_prior":true,"test_cases.py::test_skip":true}',
        )
        project.write(
            ".pytest_cache/v/cache/nodeids",
            json.dumps(["historical.py::test_prior"], indent=2),
        )
        runs = []
        for recovering in (False, True):
            if recovering:
                project.write("recovered", "done")
            result = _run(
                project, route, "--numprocesses=2", "-q", *(("--lf",) if recovering else ())
            )
            assert result.returncode == (0 if recovering else 1), result.stdout + result.stderr
            cache = project.root / ".pytest_cache/v/cache"
            runs.append({name: (cache / name).read_bytes() for name in ("lastfailed", "nodeids")})
            if not recovering:
                assert json.loads(runs[-1]["lastfailed"]) == {
                    "historical.py::test_prior": True,
                    "test_cases.py::test_recover": True,
                    "test_cases.py::test_teardown": True,
                }
                project.write("trace/controller.jsonl", "")
                for path in (project.root / "trace").glob("gw*.jsonl"):
                    path.write_text("", encoding="utf-8")
        assert json.loads(runs[-1]["lastfailed"]) == {"historical.py::test_prior": True}
        selected[route] = sorted(str(event["nodeid"]) for event in _cases(project))
        caches[route] = runs
    assert caches["native"] == caches["upstream"]
    assert (
        selected["native"]
        == selected["upstream"]
        == [
            "test_cases.py::test_recover",
            "test_cases.py::test_teardown",
        ]
    )


def test_native_xdist_each_html_keeps_both_worker_rows(tmp_path: Path) -> None:
    rows: dict[str, Counter[str]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _PROBE_CONFTEST)
        project.write("test_rows.py", "def test_first(): pass\ndef test_second(): pass\n")
        arguments = (
            "--numprocesses=2",
            "--dist=each",
            "--html=results.html",
            "--self-contained-html",
            "-q",
        )
        if route == "upstream":
            result = _run(
                project,
                route,
                "-p",
                "pytest_html.plugin",
                "-p",
                "pytest_metadata.plugin",
                *arguments,
            )
        else:
            result = _run(project, route, *arguments)
        result.expect(0)
        report = (project.root / "results.html").read_text()
        if route == "upstream":
            blob = re.search(r'data-jsonblob="([^"]+)"', report)
            assert blob is not None
            tests = json.loads(unescape(blob[1]))["tests"]
            row_ids = []
            for records in tests.values():
                for record in records:
                    cells = record["resultsTableRow"]
                    assert ">Passed</td>" in cells[0]
                    identity = re.search(r">([^<]+)</td>", cells[1])
                    assert identity is not None
                    row_ids.append(unescape(identity[1]))
        else:
            row_ids = [
                unescape(nodeid)
                for nodeid in re.findall(r'<td class="Passed">Passed</td><td>([^<]+)</td>', report)
            ]
        rows[route] = Counter(row_ids)
        assert rows[route] == {"test_rows.py::test_first": 2, "test_rows.py::test_second": 2}
    assert rows["native"] == rows["upstream"]


def test_native_xdist_transports_real_warning_objects_and_source_locations(tmp_path: Path) -> None:
    warnings: dict[str, list[list[object]]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                def pytest_warning_recorded(warning_message, when, nodeid, location):
                    if when != 'runtest':
                        return
                    worker = os.environ.get('PYTEST_XDIST_WORKER', 'controller')
                    event = [str(warning_message.message),
                             type(warning_message.message).__name__,
                             warning_message.category.__name__,
                             Path(warning_message.filename).name, warning_message.lineno,
                             nodeid, when]
                    with Path(f'warnings-{worker}.jsonl').open('a') as stream:
                        stream.write(json.dumps(event) + '\\n')
                """),
        )
        project.write(
            "test_warnings.py",
            dedent("""\
                import warnings
                import pytest
                @pytest.fixture
                def warned():
                    warnings.warn('setup-warning', UserWarning)
                    yield
                    warnings.warn('teardown-warning', UserWarning)
                @pytest.mark.parametrize('case', [0, 1])
                def test_warning(warned, case):
                    warnings.warn('call-warning', UserWarning)
                """),
        )
        result = _run(project, route, "--numprocesses=2", "-Walways::UserWarning", "-q")
        result.expect(0)
        assert "2 passed, 6 warnings" in result.stdout
        warnings[route] = sorted(
            json.loads(line)
            for line in (project.root / "warnings-controller.jsonl").read_text().splitlines()
        )
        assert Counter(tuple(event[:3]) for event in warnings[route]) == {
            ("setup-warning", "UserWarning", "UserWarning"): 2,
            ("call-warning", "UserWarning", "UserWarning"): 2,
            ("teardown-warning", "UserWarning", "UserWarning"): 2,
        }
    assert warnings["native"] == warnings["upstream"]


@pytest.mark.parametrize("workers", ["0", "2"])
def test_native_xdist_stopped_protocol_retains_warnings_phases_and_diagnostic_capture(
    tmp_path: Path, workers: str
) -> None:
    reports = {}
    warnings = {}
    junit = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\njunit_logging=all\njunit_log_passing_tests=true\n")
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                base_configure = pytest_configure
                proof_config = None

                def pytest_configure(config):
                    global proof_config
                    proof_config = config
                    base_configure(config)

                @pytest.fixture(autouse=True)
                def observed_case(request):
                    record(request.config, {'event': 'case', 'nodeid': request.node.nodeid})

                def pytest_runtest_logreport(report):
                    if not hasattr(proof_config, 'workerinput'):
                        record(proof_config, {'event': 'report', 'nodeid': report.nodeid,
                                              'when': report.when, 'outcome': report.outcome})

                def pytest_warning_recorded(warning_message, when, nodeid, location):
                    if when == 'runtest' and not hasattr(proof_config, 'workerinput'):
                        record(proof_config, {'event': 'warning', 'nodeid': nodeid,
                                              'message': str(warning_message.message),
                                              'category': warning_message.category.__name__,
                                              'filename': Path(warning_message.filename).name,
                                              'line': warning_message.lineno})
                """),
        )
        project.write(
            "test_warning_stop.py",
            dedent("""\
                import warnings
                import pytest

                @pytest.fixture
                def phases():
                    print('setup-capture-evidence')
                    warnings.warn('setup-warning', UserWarning)
                    yield
                    print('teardown-capture-evidence')
                    warnings.warn('teardown-warning', UserWarning)

                def test_failure(phases):
                    print('call-capture-evidence')
                    warnings.warn('call-warning', UserWarning)
                    assert False, 'stop-evidence'

                def test_pending():
                    raise AssertionError('pending-evidence')
                """),
        )
        destination = project.root / "native.json"
        result = _run(
            project,
            route,
            f"--numprocesses={workers}",
            "--dist=loadfile",
            "--maxfail=1",
            "-Walways::UserWarning",
            "--junitxml=results.xml",
            "-q",
            env={"PYTEST_NATIVE_RESULT": str(destination) if route == "native" else None},
        )
        expected = pytest.ExitCode.TESTS_FAILED if workers == "0" else pytest.ExitCode.INTERRUPTED
        assert result.returncode == expected, result.stdout + result.stderr
        assert "stop-evidence" in result.stdout
        reports[route] = _reports(project)
        assert reports[route] == sorted(
            ("test_warning_stop.py::test_failure", phase, outcome)
            for phase, outcome in (("setup", "passed"), ("call", "failed"), ("teardown", "passed"))
        )
        warnings[route] = sorted(
            (event["nodeid"], event["message"], event["category"], event["filename"], event["line"])
            for event in _events(project)
            if event["event"] == "warning"
        )
        assert len(warnings[route]) == 3
        junit[route] = _junit_cases(project)
        assert [name for name, _, _ in junit[route]] == ["test_failure"]
        for phase in ("setup", "call", "teardown"):
            assert any(f"{phase}-capture-evidence" in text for _, _, text in junit[route][0][2])
        if route == "native":
            paths = [destination] if destination.exists() else project.root.glob("native.json.gw*")
            documents = [json.loads(path.read_text()) for path in paths]
            cases = [case for document in documents for case in document["cases"]]
            assert len(cases) == 1
            assert cases[0]["nodeid"] == "test_warning_stop.py::test_failure"
            assert [phase["phase"] for phase in cases[0]["phases"]] == ["setup", "call", "teardown"]
            for phase in cases[0]["phases"]:
                assert f"{phase['phase']}-capture-evidence" in phase["stdout"]
            assert sum(len(document["warnings"]) for document in documents) == 3
    assert reports["native"] == reports["upstream"]
    assert warnings["native"] == warnings["upstream"]
    assert junit["native"] == junit["upstream"]


def test_native_xdist_without_html_preserves_upstream_serialized_report_fields(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "conftest.py",
        dedent("""\
            import json
            import os
            from pathlib import Path

            proof_config = None
            def pytest_configure(config):
                global proof_config
                proof_config = config

            def pytest_runtest_logreport(report):
                worker = getattr(proof_config, 'workerinput', {}).get('workerid', 'controller')
                with Path(f'wire-{worker}.jsonl').open('a') as stream:
                    stream.write(json.dumps(report._to_json(), default=repr) + '\\n')
            """),
    )
    project.write(
        "test_wire.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_pass(value, request):\n"
        "    request.node.user_properties.append(('wire', {'unicode': 'é🙂',\n"
        "        'bytes': b'\\x00\\xff', 'long': 2**200, 'negative': -42,\n"
        "        'nested': [None, True, False, 3.5, (value, 'tuple')],\n"
        "        'complex': complex(2.5, -4.5), 'set': {1, 2}}))\n"
        "    assert value\n",
    )
    serialized = {}
    for route in ("upstream", "native"):
        for path in project.root.glob("wire-*.jsonl"):
            path.unlink()
        result = _run(project, route, "--numprocesses=2", "-q")
        result.expect(0)
        reports = [
            json.loads(line)
            for path in project.root.glob("wire-gw*.jsonl")
            for line in path.read_text().splitlines()
        ]
        assert len(reports) == 6
        for report in reports:
            for field in ("start", "stop", "duration"):
                del report[field]
        serialized[route] = sorted(reports, key=lambda report: (report["nodeid"], report["when"]))
        controller = [
            json.loads(line)
            for line in (project.root / "wire-controller.jsonl").read_text().splitlines()
        ]
        assert sorted(
            json.dumps(report["user_properties"], sort_keys=True) for report in controller
        ) == sorted(json.dumps(report["user_properties"], sort_keys=True) for report in reports)
    assert serialized["native"] == serialized["upstream"]


@pytest.mark.skipif(
    int(pytest.version_tuple[0]) < 9, reason="pytest 9 provides the subtests fixture"
)
def test_native_xdist_subtests_preserve_protocol_order_context_and_capture(tmp_path: Path) -> None:
    protocols: dict[str, list[dict[str, object]]] = {}
    junit: dict[str, list[JunitCase]] = {}
    summaries: dict[str, str] = {}
    cache_failures: dict[str, bytes] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                @pytest.hookimpl(specname='pytest_runtest_logreport')
                def pytest_observe_subtests(report):
                    if not hasattr(report, 'node') or not report.nodeid.endswith('test_subtests'):
                        return
                    context = getattr(report, 'context', None)
                    record(report.node.config,
                           {'event': 'subtest-protocol', 'when': report.when,
                            'outcome': report.outcome, 'type': type(report).__name__,
                            'context': None if context is None else
                                       {'msg': context.msg, 'kwargs': dict(context.kwargs)},
                            'sections': report.sections})
                """),
        )
        project.write(
            "pytest.ini", "[pytest]\njunit_logging = all\njunit_log_passing_tests = true\n"
        )
        project.write(
            "test_subtests.py",
            dedent("""\
                import sys
                import pytest
                @pytest.fixture
                def resource():
                    print('parent-setup-capture')
                    yield
                    print('parent-teardown-capture')
                def test_subtests(resource, subtests):
                    print('parent-call-capture')
                    for value in ('pass', 'fail', 'skip'):
                        with subtests.test('group', value=value):
                            print('subtest-stdout-' + value)
                            print('subtest-stderr-' + value, file=sys.stderr)
                            if value == 'skip':
                                pytest.skip('subtest-skip-sentinel')
                            assert value != 'fail', 'subtest-failure-sentinel'
                """),
        )
        project.write("test_other.py", "def test_other(): pass\n")
        result = _run(project, route, "--numprocesses=2", "--junitxml=results.xml", "-q")
        result.expect(1)
        assert "subtest-failure-sentinel" in result.stdout
        protocols[route] = [
            event for event in _events(project) if event["event"] == "subtest-protocol"
        ]
        assert [(event["when"], event["type"]) for event in protocols[route]] == [
            ("setup", "TestReport"),
            ("call", "SubtestReport"),
            ("call", "SubtestReport"),
            ("call", "SubtestReport"),
            ("call", "TestReport"),
            ("teardown", "TestReport"),
        ]
        junit[route] = _junit_cases(project)
        summary = re.search(r"^\d+ failed,.* in [\d.]+s", result.stdout, re.MULTILINE)
        assert summary is not None, result.stdout
        summaries[route] = summary[0].rsplit(" in ", 1)[0]
        cache_path = project.root / ".pytest_cache/v/cache/lastfailed"
        cache_failures[route] = cache_path.read_bytes() if cache_path.is_file() else b""
    assert protocols["native"] == protocols["upstream"]
    assert junit["native"] == junit["upstream"]
    assert summaries["native"] == summaries["upstream"]
    assert cache_failures["native"] == cache_failures["upstream"]


def test_native_xdist_late_session_finalizer_warnings_keep_configuration_attribution(
    tmp_path: Path,
) -> None:
    warning_events: dict[str, list[list[object]]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                import warnings
                stopped_session = None
                @pytest.fixture(scope='session', autouse=True)
                def lifetime(request):
                    global stopped_session
                    stopped_session = request.session
                    yield
                    Path('finalized').touch()
                    warnings.warn('late-finalizer-warning', UserWarning)
                @pytest.hookimpl(specname='pytest_runtest_logreport')
                def pytest_stop_after_first_teardown(report):
                    if report.when == 'teardown' and not hasattr(report, 'node'):
                        stopped_session.shouldstop = 'late-stop-sentinel'
                def pytest_warning_recorded(warning_message, when, nodeid, location):
                    worker = os.environ.get('PYTEST_XDIST_WORKER', 'controller')
                    event = [str(warning_message.message), when, nodeid,
                             type(warning_message.message).__name__,
                             warning_message.category.__name__,
                             Path(warning_message.filename).name, warning_message.lineno]
                    with Path(f'warnings-{worker}.jsonl').open('a') as stream:
                        stream.write(json.dumps(event) + '\\n')
                """),
        )
        project.write(
            "test_stop.py",
            "import pytest\n@pytest.mark.filterwarnings('error:late-finalizer-warning')\n"
            "def test_first(): pass\ndef test_pending(): pass\n",
        )
        result = _run(
            project, route, "--numprocesses=2", "--dist=loadfile", "-Walways::UserWarning", "-q"
        )
        assert result.returncode == pytest.ExitCode.INTERRUPTED, result.stdout + result.stderr
        assert "1 passed, 1 warning" in result.stdout
        assert "late-stop-sentinel" in result.stdout
        assert (project.root / "finalized").exists()
        assert [case["nodeid"] for case in _cases(project)] == ["test_stop.py::test_first"]
        warning_events[route] = [
            json.loads(line)
            for line in (project.root / "warnings-controller.jsonl").read_text().splitlines()
        ]
        assert [event[:5] for event in warning_events[route]] == [
            ["late-finalizer-warning", "config", "", "UserWarning", "UserWarning"]
        ]
    assert warning_events["native"] == warning_events["upstream"]


@pytest.mark.parametrize("workers", [0, 2])
def test_native_xdist_scoped_mockers_restore_patches(tmp_path: Path, workers: int) -> None:
    pytest.importorskip("pytest_mock")
    cleanup: dict[str, list[list[int]]] = {}
    junit: dict[str, list[JunitCase]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("app.py", "session = module = cls = function = 0\n")
        project.write(
            "conftest.py",
            dedent("""\
                import json
                import os
                from pathlib import Path
                import pytest
                import app
                def pytest_configure(config):
                    pass
                @pytest.fixture(scope='session')
                def cleanup(worker_id):
                    yield
                    values = [app.session, app.module, app.cls, app.function]
                    Path(f'cleanup-{worker_id}.json').write_text(json.dumps(values))
                    assert values == [0, 0, 0, 0]
                @pytest.fixture(scope='session')
                def session_mocker(cleanup, session_mocker):
                    return session_mocker
                @pytest.fixture(scope='session')
                def session_patch(session_mocker):
                    session_mocker.patch.object(app, 'session', 1)
                @pytest.fixture(scope='module')
                def module_patch(module_mocker):
                    module_mocker.patch.object(app, 'module', 2)
                @pytest.fixture(scope='class')
                def class_patch(class_mocker):
                    class_mocker.patch.object(app, 'cls', 3)
                """),
        )
        for module in ("alpha", "beta"):
            project.write(
                f"test_{module}.py",
                dedent("""\
                    import pytest
                    import app
                    @pytest.mark.usefixtures('session_patch', 'module_patch', 'class_patch')
                    class TestScoped:
                        def test_first(self, mocker):
                            assert [app.session, app.module, app.cls, app.function] == [1, 2, 3, 0]
                            mocker.patch.object(app, 'function', 4)
                        def test_second(self):
                            assert [app.session, app.module, app.cls, app.function] == [1, 2, 3, 0]
                    def test_after_class():
                        assert app.cls == app.function == 0
                    """),
            )
        result = _run(
            project,
            route,
            f"--numprocesses={workers}",
            "--dist=loadfile",
            "--maxschedchunk=1",
            "-p",
            "pytest_mock",
            "--junitxml=results.xml",
            "-q",
        )
        result.expect(0)
        assert "6 passed" in result.stdout
        cleanup[route] = [
            json.loads(path.read_text()) for path in sorted(project.root.glob("cleanup-*.json"))
        ]
        assert cleanup[route] == [[0, 0, 0, 0]] * (workers or 1)
        junit[route] = _junit_cases(project)
    assert cleanup["native"] == cleanup["upstream"]
    assert junit["native"] == junit["upstream"]


def test_native_xdist_async_fixture_lifetime_matches_pytest_asyncio(tmp_path: Path) -> None:
    pytest.importorskip(
        "pytest_asyncio", reason="the upstream async oracle requires pytest-asyncio"
    )
    lifetimes: dict[str, list[list[list[object]]]] = {}
    junit: dict[str, list[JunitCase]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("conftest.py", _PROBE_CONFTEST)
        project.write("pytest.ini", "[pytest]\nasyncio_default_fixture_loop_scope=module\n")
        fixture_module, fixture_name = (
            ("pytest_asyncio", "fixture") if route == "upstream" else ("vfy", "async_fixture")
        )
        for module in ("alpha", "beta"):
            project.write(
                f"test_{module}.py",
                dedent(f"""\
                    import asyncio
                    import json
                    import os
                    from pathlib import Path
                    import pytest
                    from {fixture_module} import {fixture_name}
                    def record(event):
                        root = Path('lifecycle')
                        root.mkdir(exist_ok=True)
                        worker = os.environ['PYTEST_XDIST_WORKER']
                        with (root / (worker + '.jsonl')).open('a') as stream:
                            stream.write(json.dumps([__name__, *event]) + '\\n')
                    @{fixture_name}(scope='module', loop_scope='module')
                    async def lifetime():
                        loop = asyncio.get_running_loop()
                        await asyncio.sleep(0)
                        record(['setup', loop.is_running()])
                        yield loop
                        await asyncio.sleep(0)
                        record(['teardown', asyncio.get_running_loop() is loop])
                    @pytest.mark.asyncio(loop_scope='module')
                    @pytest.mark.parametrize('index', range(3))
                    async def test_async(index, lifetime):
                        await asyncio.sleep(0)
                        assert asyncio.get_running_loop() is lifetime
                        record(['call', index, lifetime.is_running()])
                    """),
            )
        plugins = ("-p", "pytest_asyncio.plugin") if route == "upstream" else ()
        result = _run(
            project,
            route,
            *plugins,
            "--numprocesses=2",
            "--dist=loadfile",
            "--maxschedchunk=1",
            "--junitxml=results.xml",
            "-q",
        )
        result.expect(0)
        assert "6 passed" in result.stdout
        # Worker assignment is unordered; fixture-owner event order is part of the contract.
        lifetimes[route] = sorted(
            [json.loads(line) for line in path.read_text().splitlines()]
            for path in (project.root / "lifecycle").glob("*.jsonl")
        )
        assert lifetimes[route] == [
            [
                [f"test_{module}", "setup", True],
                *[[f"test_{module}", "call", index, True] for index in range(3)],
                [f"test_{module}", "teardown", True],
            ]
            for module in ("alpha", "beta")
        ]
        junit[route] = _junit_cases(project)
    assert lifetimes["native"] == lifetimes["upstream"]
    assert junit["native"] == junit["upstream"]


def test_native_xdist_disabled_late_session_finalizer_exception_propagates(tmp_path: Path) -> None:
    warning_events: dict[str, list[list[object]]] = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write(
            "conftest.py",
            dedent("""\
                import json
                import os
                import warnings
                from pathlib import Path
                import pytest
                stopped_session = None
                def pytest_configure(config):
                    pass
                @pytest.fixture(scope='session', autouse=True)
                def lifetime(request):
                    global stopped_session
                    stopped_session = request.session
                    yield
                    Path('finalized').touch()
                    warnings.warn('late-finalizer-warning', UserWarning)
                    raise RuntimeError('late-finalizer-error')
                def pytest_runtest_logreport(report):
                    event = [report.nodeid, report.when, report.outcome]
                    with Path('reports.jsonl').open('a') as stream:
                        stream.write(json.dumps(event) + '\\n')
                    if report.when == 'teardown':
                        stopped_session.shouldstop = 'late-stop-sentinel'
                def pytest_warning_recorded(warning_message, when, nodeid, location):
                    event = [str(warning_message.message), when, nodeid,
                             type(warning_message.message).__name__,
                             warning_message.category.__name__,
                             Path(warning_message.filename).name, warning_message.lineno]
                    with Path('warnings.jsonl').open('a') as stream:
                        stream.write(json.dumps(event) + '\\n')
                """),
        )
        project.write("test_stop.py", "def test_first(): pass\ndef test_pending(): pass\n")
        result = _run(project, route, "--numprocesses=0", "-Walways::UserWarning", "-q")
        result.expect(1)
        assert "RuntimeError: late-finalizer-error" in result.stderr
        assert "late-stop-sentinel" in result.stdout
        assert (project.root / "finalized").exists()
        assert [
            json.loads(line) for line in (project.root / "reports.jsonl").read_text().splitlines()
        ] == [
            ["test_stop.py::test_first", "setup", "passed"],
            ["test_stop.py::test_first", "call", "passed"],
            ["test_stop.py::test_first", "teardown", "passed"],
        ]
        assert "1 error" not in result.stdout
        warning_events[route] = [
            json.loads(line) for line in (project.root / "warnings.jsonl").read_text().splitlines()
        ]
        assert [event[:5] for event in warning_events[route]] == [
            ["late-finalizer-warning", "config", "", "UserWarning", "UserWarning"]
        ]
    assert warning_events["native"] == warning_events["upstream"]


@pytest.mark.parametrize("workers", ["0", "2"])
@pytest.mark.parametrize("consumer", ["reports", "diagnostic", "html"])
def test_native_xdist_retains_passing_capture_for_requested_consumers(
    tmp_path: Path, workers: str, consumer: str
) -> None:
    junit: dict[str, list[JunitCase]] = {}
    for route in ("upstream", "native"):
        # Existing HTML parity tests cover its renderer; here exercise the native
        # retention guard without requiring a second HTML plugin configuration.
        if consumer != "reports" and route == "upstream":
            continue
        project = VfyProject(tmp_path / route)
        ini = (
            "[pytest]\njunit_logging = all\njunit_log_passing_tests = true\n"
            "log_format = %(message)s\n"
        )
        if consumer == "html":
            ini += "generate_report_on_test = true\n"
        project.write("pytest.ini", ini)
        project.write(
            "test_capture.py",
            dedent("""\
                import logging
                import sys
                import pytest

                def emit(phase):
                    print(phase + '-stdout-evidence')
                    print(phase + '-stderr-evidence', file=sys.stderr)
                    logging.warning(phase + '-log-evidence')

                @pytest.fixture
                def phases():
                    emit('setup')
                    yield
                    emit('teardown')

                @pytest.mark.parametrize('value', [0, 1])
                def test_pass(value, phases):
                    emit('call')
                """),
        )
        record = project.root / "native.json"
        arguments = [f"--numprocesses={workers}", "-rP", "--junitxml=results.xml", "-q"]
        if consumer == "html":
            arguments.append("--html=results.html")
        result = _run(
            project,
            route,
            *arguments,
            env={"PYTEST_NATIVE_RESULT": str(record) if consumer == "diagnostic" else None},
        )
        result.expect(0)
        junit[route] = _junit_cases(project)
        assert len(junit[route]) == 2
        for phase in ("setup", "call", "teardown"):
            for stream in ("stdout", "stderr", "log"):
                marker = f"{phase}-{stream}-evidence"
                assert marker in result.stdout
                for _, _, sections in junit[route]:
                    assert any(marker in text for _, _, text in sections)
                if consumer == "html":
                    assert marker in (project.root / "results.html").read_text()
        if consumer == "diagnostic":
            paths = [record] if record.exists() else sorted(project.root.glob("native.json.gw*"))
            documents = [json.loads(path.read_text()) for path in paths]
            assert documents
            cases = [case for document in documents for case in document["cases"]]
            assert len(cases) == 2
            for case in cases:
                assert [phase["phase"] for phase in case["phases"]] == [
                    "setup",
                    "call",
                    "teardown",
                ]
                for phase in case["phases"]:
                    for stream in ("stdout", "stderr", "log"):
                        assert f"{phase['phase']}-{stream}-evidence" in phase[stream]
    if consumer == "reports":
        assert junit["native"] == junit["upstream"]


@pytest.mark.parametrize("workers", ["0", "2"])
def test_native_xdist_phase_reports_and_junit_match_upstream_with_report_hooks(
    tmp_path: Path, workers: str
) -> None:
    reports = {}
    junit = {}
    for route in ("upstream", "native"):
        project = VfyProject(tmp_path / route)
        project.write("pytest.ini", "[pytest]\njunit_logging=all\njunit_log_passing_tests=true\n")
        project.write(
            "conftest.py",
            _PROBE_CONFTEST
            + dedent("""\
                projection_config = None

                @pytest.fixture(autouse=True)
                def observed_case(request):
                    record(request.config, {'event': 'case', 'nodeid': request.node.nodeid})

                def pytest_runtest_logreport(report):
                    if not hasattr(projection_config, 'workerinput'):
                        record(projection_config, {'event': 'report', 'nodeid': report.nodeid,
                                                   'when': report.when, 'outcome': report.outcome})

                def pytest_configure(config):
                    global projection_config
                    projection_config = config
                """),
        )
        project.write(
            "test_case.py",
            dedent("""\
                import pytest
                @pytest.fixture
                def value():
                    print('setup-capture-evidence')
                    yield 1
                    print('teardown-capture-evidence')
                def test_pass(value):
                    print('passing-capture-evidence')
                    assert value == 1
                def test_fail(value):
                    print('failing-capture-evidence')
                    assert False, 'failure-evidence'
                """),
        )
        result = _run(project, route, f"--numprocesses={workers}", "--junitxml=results.xml", "-q")
        result.expect(1)
        reports[route] = _reports(project)
        assert reports[route] == sorted(
            (
                f"test_case.py::{name}",
                phase,
                "failed" if name == "test_fail" and phase == "call" else "passed",
            )
            for name in ("test_pass", "test_fail")
            for phase in ("setup", "call", "teardown")
        ), result.stdout + result.stderr
        assert "1 failed, 1 passed" in result.stdout + result.stderr
        junit[route] = _junit_cases(project)
    assert reports["native"] == reports["upstream"]
    assert junit["native"] == junit["upstream"]
