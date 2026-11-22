"""Ignore hooks preserve default priority, initial anchors, and lazy conftests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

import pytest

from tests.support import ENGINES, CommandResult, VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

Engine = Literal["stock", "native"]
_COLLECTION_OBSERVER = """import json
from pathlib import Path

reports = []

def pytest_collectreport(report):
    crash = getattr(report.longrepr, 'reprcrash', None)
    reports.append({
        'nodeid': report.nodeid,
        'outcome': report.outcome,
        'when': report.when,
        'passed': report.passed,
        'failed': report.failed,
        'skipped': report.skipped,
        'longreprtext': report.longreprtext,
        'crash': [str(crash.path), crash.lineno, crash.message] if crash is not None else None,
        'stdout': report.capstdout,
        'stderr': report.capstderr,
        'log': report.caplog,
        'sections': list(report.sections),
        'result': [node.nodeid for node in report.result],
    })
    Path('collection-reports.json').write_text(json.dumps(reports))
"""

_TEST = """from pathlib import Path

def test_first(request):
    with Path('events').open('a') as output:
        output.write(request.node.nodeid + '\\n')

def test_second(request):
    with Path('events').open('a') as output:
        output.write(request.node.nodeid + '\\n')
"""


def _run(
    project: VfyProject, engine: Engine, arguments: list[str], *, exitstatus: int = 0
) -> CommandResult:
    result = project.run_engine(
        engine,
        *arguments,
        stock=("-q",),
        independent=("--native-result-json=record.json",),
    )
    assert result.returncode == exitstatus, result.stdout + result.stderr
    return result


def _events(project: VfyProject) -> list[str]:
    return (project.root / "events").read_text().splitlines()


def _assert_directory_failure_traceback(text: str, failure: str) -> None:
    location = "4: in pytest_ignore_collect" if failure == "hook" else "2: in <module>"
    source = (
        f"branch/conftest.py:{location}\n"
        "    raise RuntimeError('original directory failure')\n"
        "E   RuntimeError: original directory failure"
    )
    prefix, separator, suffix = text.partition(source)
    assert (separator, suffix) == (source, ""), text
    # The dispatcher and import loader have different real framework frames.
    # Keep the complete diagnostic and reject any additional user-code frames.
    framework_files = (
        ("/pluggy/_hooks.py", "/pluggy/_manager.py")
        if failure == "hook"
        else ("/importlib/__init__.py", "/_pytest/assertion/rewrite.py")
    )
    for line in prefix.splitlines():
        if line.startswith(" "):
            continue
        frame = re.fullmatch(r"(.+):\d+: in \w+", line)
        assert frame is not None, text
        path = frame[1].replace("\\", "/")
        assert path.endswith(framework_files) or (
            failure == "conftest" and path.startswith("<frozen importlib.") and path.endswith(">")
        ), text


@ENGINES
@pytest.mark.parametrize(
    ("priority", "answer", "collect_child"),
    [
        ("", "False", True),
        ("tryfirst=True", "False", True),
        ("", "None", False),
        ("trylast=True", "False", False),
    ],
)
def test_firstresult_false_none_and_builtin_priority(
    tmp_path: Path, engine: Engine, priority: str, answer: str, collect_child: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nnorecursedirs = blocked\n")
    project.write(
        "conftest.py",
        f"""from pathlib import Path
import pytest
started = False

def pytest_sessionstart(session):
    global started
    started = True

@pytest.hookimpl({priority})
def pytest_ignore_collect(collection_path, config):
    assert started, 'ignore hook ran before sessionstart'
    assert isinstance(collection_path, Path)
    if collection_path.name == 'blocked':
        with Path('calls').open('a') as output:
            output.write('blocked\\n')
        print('ignore callback output')
        return {answer}
""",
    )
    project.write("test_root.py", _TEST)
    project.write("blocked/test_child.py", _TEST)
    result = _run(project, engine, [])
    assert ("ignore callback output" in result.stdout + result.stderr) == (
        priority != "trylast=True"
    )
    expected = [f"test_root.py::{name}" for name in ("test_first", "test_second")]
    if collect_child:
        expected[:0] = [f"blocked/test_child.py::{name}" for name in ("test_first", "test_second")]
    assert _events(project) == expected
    calls = project.root / "calls"
    if priority == "trylast=True":
        assert not calls.exists()
    else:
        assert calls.read_text().splitlines() == ["blocked"]


@ENGINES
def test_nearest_conftest_paths_and_globs_preserve_pathlike_spelling(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", "collect_ignore = ['branch/test_from_parent.py']\n")
    project.write(
        "branch/conftest.py",
        """from pathlib import Path
collect_ignore = [
    'test_string.py', Path('test_relative.py'), Path(__file__).parent / 'test_absolute.py',
]
collect_ignore_glob = ['test_glob_*.py']
""",
    )
    for filename in (
        "test_from_parent.py",
        "test_string.py",
        "test_relative.py",
        "test_absolute.py",
        "test_glob_ignored.py",
    ):
        project.write(f"branch/{filename}", _TEST)
    _run(project, engine, [])
    assert _events(project) == [
        f"branch/{filename}::{name}"
        for filename in ("test_from_parent.py", "test_relative.py")
        for name in ("test_first", "test_second")
    ]


@ENGINES
def test_initial_test_prefix_anchor_is_shallow_but_deep_ignored_conftest_is_unimported(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nnorecursedirs = blocked testblocked\n")
    project.write(
        "conftest.py",
        "from pathlib import Path\n"
        "def pytest_sessionstart(session):\n"
        "    with Path('events').open('a') as output: output.write('sessionstart\\n')\n",
    )
    project.write(
        "testblocked/conftest.py",
        "from pathlib import Path\n"
        "with Path('events').open('a') as output: output.write('initial child\\n')\n"
        "collect_ignore = ['../test_root.py']\n"
        "def pytest_ignore_collect(collection_path, config):\n"
        "    raise AssertionError('ignored child hook leaked into parent')\n",
    )
    project.write(
        "testblocked/test_unreachable.py", "raise AssertionError('ignored module imported')\n"
    )
    project.write(
        "blocked/deep/conftest.py", "raise AssertionError('deep ignored conftest imported')\n"
    )
    project.write(
        "blocked/deep/test_unreachable.py", "raise AssertionError('ignored module imported')\n"
    )
    project.write("test_root.py", _TEST)
    _run(project, engine, [])
    assert _events(project) == [
        "initial child",
        "sessionstart",
        "test_root.py::test_first",
        "test_root.py::test_second",
    ]


@ENGINES
@pytest.mark.parametrize("selection", ["file", "directory", "redundant"])
def test_explicit_selectors_bypass_ignore_but_redundant_child_only_preloads(
    tmp_path: Path, engine: Engine, selection: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """from pathlib import Path

def pytest_ignore_collect(collection_path, config):
    if collection_path.name == 'blocked':
        with Path('events').open('a') as output: output.write('ignored blocked\\n')
        return True
""",
    )
    project.write(
        "blocked/conftest.py",
        "from pathlib import Path\n"
        "with Path('events').open('a') as output: output.write('anchor imported\\n')\n",
    )
    project.write("blocked/test_child.py", _TEST)
    project.write("test_root.py", _TEST)
    if selection == "file":
        arguments = ["blocked/test_child.py::test_second", "blocked/test_child.py::test_first"]
        expected = ["blocked/test_child.py::test_second", "blocked/test_child.py::test_first"]
    elif selection == "directory":
        arguments = ["blocked"]
        expected = ["blocked/test_child.py::test_first", "blocked/test_child.py::test_second"]
    else:
        arguments = [".", "blocked/test_child.py::test_first"]
        expected = ["ignored blocked", "test_root.py::test_first", "test_root.py::test_second"]
    _run(project, engine, arguments)
    assert _events(project) == ["anchor imported", *expected]


@ENGINES
def test_hypothesis_shaped_hook_ignores_database_after_norecursedirs_override(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nnorecursedirs =\n")
    project.write(
        "conftest.py",
        """import warnings

def pytest_ignore_collect(collection_path, config):
    if collection_path.name == '.hypothesis' and collection_path.is_dir():
        assert config.getini('norecursedirs') == []
        warnings.warn('hypothesis directory ignored', UserWarning)
        return True
""",
    )
    project.write(
        ".hypothesis/deep/conftest.py", "raise AssertionError('database conftest imported')\n"
    )
    project.write(".hypothesis/test_unreachable.py", "raise AssertionError('database imported')\n")
    project.write("test_root.py", _TEST)
    result = _run(project, engine, [])
    assert "hypothesis directory ignored" in result.stdout + result.stderr
    assert _events(project) == ["test_root.py::test_first", "test_root.py::test_second"]


@ENGINES
@pytest.mark.parametrize("failure", ["hook", "conftest"])
def test_directory_error_preserves_exception_and_collects_later_siblings(
    tmp_path: Path, engine: Engine, failure: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write("conftest.py", _COLLECTION_OBSERVER)
    project.write(
        "global_collection_observer.py",
        _COLLECTION_OBSERVER.replace("collection-reports.json", "global-collection-reports.json"),
    )
    if failure == "hook":
        source = """def pytest_ignore_collect(collection_path, config):
    if collection_path.name == 'test_branch.py':
        print('directory failure output')
        raise RuntimeError('original directory failure')
"""
    else:
        source = (
            "print('directory failure output')\nraise RuntimeError('original directory failure')\n"
        )
    project.write("branch/conftest.py", source)
    project.write("branch/test_branch.py", "raise AssertionError('failed branch imported')\n")
    for filename in ("a_test.py", "z_test.py"):
        project.write(
            filename,
            "from pathlib import Path\n"
            "with Path('events').open('a') as output:\n"
            f"    output.write('{filename} imported\\n')\n"
            "def test_never_runs(): Path('body-ran').touch()\n",
        )
    observed = project.root / "collection-reports.json"
    global_observed = project.root / "global-collection-reports.json"
    arguments = ["-p", "global_collection_observer"]
    expected = None
    global_expected = None
    if engine == "native":
        oracle = _run(project, "stock", arguments, exitstatus=2)
        assert "RuntimeError: original directory failure" in oracle.stdout + oracle.stderr
        assert "directory failure output" in oracle.stdout + oracle.stderr
        assert _events(project) == ["a_test.py imported", "z_test.py imported"]
        assert not (project.root / "body-ran").exists()
        expected = json.loads(observed.read_text())
        global_expected = json.loads(global_observed.read_text())
        project.write("stock-collection-reports.json", observed.read_text())
        project.write("stock-global-collection-reports.json", global_observed.read_text())
        observed.unlink()
        global_observed.unlink()
        (project.root / "events").unlink()
    result = _run(project, engine, arguments, exitstatus=2)
    assert "RuntimeError: original directory failure" in result.stdout + result.stderr
    assert "directory failure output" in result.stdout + result.stderr
    assert _events(project) == ["a_test.py imported", "z_test.py imported"]
    assert not (project.root / "body-ran").exists()
    reports = json.loads(observed.read_text())
    global_reports = json.loads(global_observed.read_text())
    if expected is not None:
        assert global_expected is not None
        for actual_reports, reference_reports in (
            (reports, expected),
            (global_reports, global_expected),
        ):
            for actual, reference in zip(actual_reports, reference_reports, strict=True):
                if actual["failed"]:
                    _assert_directory_failure_traceback(actual["longreprtext"], failure)
                    _assert_directory_failure_traceback(reference["longreprtext"], failure)
                    assert {
                        key: value for key, value in actual.items() if key != "longreprtext"
                    } == {key: value for key, value in reference.items() if key != "longreprtext"}
                else:
                    assert actual == reference
    assert [(report["nodeid"], report["outcome"]) for report in reports] == [
        ("", "passed"),
        ("a_test.py", "passed"),
        *([("branch", "failed")] if failure == "hook" else []),
        ("z_test.py", "passed"),
        (".", "passed"),
    ]
    assert [(report["nodeid"], report["outcome"]) for report in global_reports] == [
        ("", "passed"),
        ("a_test.py", "passed"),
        ("branch", "failed"),
        ("z_test.py", "passed"),
        (".", "passed"),
    ]
    assert global_reports[2]["failed"]
    assert global_reports[2]["crash"][2] == "RuntimeError: original directory failure"
    _assert_directory_failure_traceback(global_reports[2]["longreprtext"], failure)
    assert global_reports[2]["stdout"] == ""
    if engine == "native":
        record = json.loads((project.root / "record.json").read_text())
        assert record["cases"] == []
        assert [
            [
                report["nodeid"],
                report["outcome"],
                report["detail"],
                report["stdout"],
                report["stderr"],
                report["log"],
            ]
            for report in record["collection_reports"]
        ] == [
            [
                report["nodeid"],
                report["outcome"],
                report["longreprtext"],
                report["stdout"],
                report["stderr"],
                report["log"],
            ]
            for report in global_reports
        ]
        assert record["collection_reports"][2]["detail"] == global_reports[2]["longreprtext"]
        assert record["collection_reports"][2]["stdout"] == ""


@ENGINES
def test_configure_mutated_names_are_live_but_selected_testpaths_stay_fixed(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\ntestpaths = suite\n")
    project.write(
        "conftest.py",
        "def pytest_configure(config):\n"
        "    config._inicache['testpaths'] = ['other']\n"
        "    config._inicache['python_files'] = ['check_*.py']\n",
    )
    project.write("suite/check_live.py", _TEST)
    project.write("suite/test_unreachable.py", "raise AssertionError('stale names')\n")
    project.write("other/check_unreachable.py", "raise AssertionError('testpaths reselected')\n")
    _run(project, engine, [])
    assert _events(project) == [
        "suite/check_live.py::test_first",
        "suite/check_live.py::test_second",
    ]


@ENGINES
@pytest.mark.parametrize("exclusion", ["ignore", "ignore_glob", "collect_ignore"])
@pytest.mark.parametrize("answer", ["False", "None"])
def test_cli_and_conftest_exclusions_remain_overridable_defaults(
    tmp_path: Path, engine: Engine, exclusion: str, answer: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    declaration = "collect_ignore = ['blocked']\n" if exclusion == "collect_ignore" else ""
    project.write(
        "conftest.py",
        declaration
        + "def pytest_ignore_collect(collection_path, config):\n"
        + f"    if collection_path.name == 'blocked': return {answer}\n",
    )
    project.write("test_root.py", _TEST)
    project.write("blocked/test_child.py", _TEST)
    arguments = {
        "ignore": ["--ignore=blocked"],
        "ignore_glob": ["--ignore-glob=*blocked"],
        "collect_ignore": [],
    }[exclusion]
    _run(project, engine, arguments)
    expected = ["test_root.py::test_first", "test_root.py::test_second"]
    if answer == "False":
        expected[:0] = ["blocked/test_child.py::test_first", "blocked/test_child.py::test_second"]
    assert _events(project) == expected


@ENGINES
@pytest.mark.parametrize("allow_environment", [False, True])
def test_virtual_environment_recursion_uses_active_option(
    tmp_path: Path, engine: Engine, allow_environment: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\nnorecursedirs =\n")
    project.write("environment/pyvenv.cfg", "home = /unused\n")
    project.write("environment/test_child.py", _TEST)
    project.write("test_root.py", _TEST)
    _run(project, engine, ["--collect-in-virtualenv"] if allow_environment else [])
    expected = ["test_root.py::test_first", "test_root.py::test_second"]
    if allow_environment:
        expected[:0] = [
            "environment/test_child.py::test_first",
            "environment/test_child.py::test_second",
        ]
    assert _events(project) == expected


@ENGINES
def test_default_reads_conftest_declaration_after_earlier_hook_mutation(
    tmp_path: Path, engine: Engine
) -> None:
    project = VfyProject(tmp_path)
    project.write("pytest.ini", "[pytest]\n")
    project.write(
        "conftest.py",
        """collect_ignore = ['blocked']

def pytest_ignore_collect(collection_path, config):
    if collection_path.name == 'blocked':
        collect_ignore.clear()
        return None
""",
    )
    project.write("blocked/test_child.py", _TEST)
    _run(project, engine, [])
    assert _events(project) == [
        "blocked/test_child.py::test_first",
        "blocked/test_child.py::test_second",
    ]


_READONLY_COLLECTION_HOOK = """import json, pytest
from pathlib import Path

calls = []
proxy = None

class Proxy:
    def __init__(self, delegate): self.delegate = delegate
    def __getattr__(self, name): return getattr(self.delegate, name)
    def pytest_collectreport(self, report):
        assert self is proxy
        calls.append([report.nodeid, report.outcome])
        return self.delegate.pytest_collectreport(report=report)

def pytest_configure(config):
    global proxy
    proxy = Proxy(config.hook)
    missing = object()
    for kind in (pytest.Session, pytest.Dir):
        previous = kind.__dict__.get('ihook', missing)
        kind.ihook = property(lambda self: proxy)
        def restore(kind=kind, previous=previous):
            if previous is missing: delattr(kind, 'ihook')
            else: kind.ihook = previous
        config.add_cleanup(restore)

def pytest_unconfigure():
    Path('proxy-calls.json').write_text(json.dumps(calls))
"""


def test_collection_reports_honor_readonly_custom_ihook(tmp_path: Path) -> None:
    observations = []
    for engine in ("stock", "native"):
        project = VfyProject(tmp_path / engine)
        project.write("pytest.ini", "[pytest]\n")
        project.write("conftest.py", _COLLECTION_OBSERVER + _READONLY_COLLECTION_HOOK)
        project.write(
            "global_collection_observer.py",
            _COLLECTION_OBSERVER.replace(
                "collection-reports.json", "global-collection-reports.json"
            ),
        )
        project.write("test_a.py", _TEST)
        project.write("branch/test_b.py", _TEST)
        _run(project, engine, ["-p", "global_collection_observer"])
        reports = json.loads((project.root / "collection-reports.json").read_text())
        global_reports = json.loads((project.root / "global-collection-reports.json").read_text())
        calls = json.loads((project.root / "proxy-calls.json").read_text())
        assert reports == global_reports
        assert [(report["nodeid"], report["outcome"]) for report in reports] == [
            ("", "passed"),
            ("branch/test_b.py", "passed"),
            ("branch", "passed"),
            ("test_a.py", "passed"),
            (".", "passed"),
        ]
        assert calls == [["", "passed"], ["branch", "passed"], [".", "passed"]]
        assert _events(project) == [
            f"{module}::{name}"
            for module in ("branch/test_b.py", "test_a.py")
            for name in ("test_first", "test_second")
        ]
        observations.append((reports, calls))
    assert observations[0] == observations[1]
