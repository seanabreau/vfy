"""Compare reviewed pytest lifecycle and fixture edges through installed wheels."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject

PROBE = Path(__file__).resolve().parent


def normalize_diagnostic_output(output: str) -> str:
    lines = []
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        lines.append(stripped[1:].strip() if stripped.startswith("E ") else stripped)
    return "\n".join(lines)


def diagnostic_excerpt(source: str, output: str) -> str:
    normalized = normalize_diagnostic_output(output)
    if source == "test_scope_mismatch.py":
        start = normalized.find("ScopeMismatch:")
        if start < 0:
            return normalized
        requested = normalized.find("Requested fixture:\n", start)
        if requested < 0:
            return normalized[start:]
        line_start = requested + len("Requested fixture:\n")
        end = normalized.find("\n", line_start)
        return normalized[start:] if end < 0 else normalized[start:end]

    start = normalized.find(f"file <project>/{source}, line")
    if start < 0:
        return normalized
    end = normalized.find(f"\n\n<project>/{source}:", start)
    return normalized[start:] if end < 0 else normalized[start:end]


def run_case(
    root: Path,
    python: Path,
    files: dict[str, str],
    arguments: tuple[str, ...],
    *,
    phase_log: bool = False,
) -> dict[str, object]:
    project = VfyProject(root)
    for name, source in files.items():
        project.write(name, source)
    (project.root / "trace.txt").unlink(missing_ok=True)
    phase_path = project.root / "phases.json"
    environment: dict[str, str] = {}
    command = [
        str(python),
        "-m",
        "pytest",
        "-q",
        "--tb=auto",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
    ]
    # Match the candidate's Rust-backed cache fixture in the lookup error list.
    if not (root.name == "stock" and "test_dynamic_lookup.py" in files):
        command.extend(("-p", "no:cacheprovider"))
    # vfy installs its asyncio fixtures for both engines on the public route.
    command.extend(("-p", "vfy.asyncio_plugin"))
    if phase_log:
        phase_path.unlink(missing_ok=True)
        if root.name == "stock":
            environment.update(PYTHONPATH=str(PROBE), PYTEST_STOCK_PHASE_LOG=str(phase_path))
            command.extend(("-p", "stock_phase_probe"))
        else:
            environment["PYTEST_NATIVE_RESULT"] = str(phase_path)
    if root.name != "stock":
        command.extend(("-p", "vfy._pytest.native_pytest"))
    command.extend(arguments)
    result = project.run(*command, env=environment)
    observation: dict[str, object] = {
        "exit_status": result.returncode,
        "trace": (project.root / "trace.txt").read_text()
        if (project.root / "trace.txt").exists()
        else None,
    }
    if phase_log:
        if root.name == "stock":
            observation["phases"] = [
                {key: record[key] for key in ("nodeid", "phase", "outcome")}
                for line in phase_path.read_text().splitlines()
                for record in (json.loads(line),)
            ]
        else:
            record = json.loads(phase_path.read_text())
            if record["engine"] != "pytest-runner-native-v1":
                raise RuntimeError(f"native result not activated: {record}")
            observation["phases"] = [
                {"nodeid": case["nodeid"], "phase": phase["phase"], "outcome": phase["outcome"]}
                for case in record["cases"]
                for phase in case["phases"]
            ]
    observation["double_yield_error"] = "fixture function has more than one 'yield'" in (
        result.stdout + result.stderr
    )
    if "test_yield.py" in arguments:
        output = result.stdout + result.stderr
        observation["double_yield_source"] = "yield 2" in output
        observation["double_yield_location"] = "test_yield.py:3" in output
    if "test_callback.py" in arguments:
        observation["callback_error"] = "ZeroDivisionError" in result.stdout + result.stderr
    if "test_awaitable.py" in arguments:
        output = result.stdout + result.stderr
        normalized_output = normalize_diagnostic_output(output)
        awaitable_diagnostic = """async def functions are not natively supported.
You need to install a suitable plugin for your async framework, for example:
- anyio
- pytest-asyncio
- pytest-tornasync
- pytest-trio
- pytest-twisted"""
        observation["awaitable_diagnostic"] = awaitable_diagnostic in normalized_output
        observation["unawaited_coroutine_warning"] = (
            "RuntimeWarning: coroutine 'body' was never awaited" in output
        )
    if "test_return_value.py" in arguments:
        output = result.stdout + result.stderr
        normalized_output = normalize_diagnostic_output(output)
        observation["return_value_warning"] = "PytestReturnNotNoneWarning" in output
        return_value_message = (
            "returned <class 'int'>.\n"
            "Did you mean to use `assert` instead of `return`?\n"
            "See https://docs.pytest.org/en/stable/how-to/assert.html"
            "#return-not-none for more information."
        )
        observation["return_value_message"] = return_value_message in normalized_output
        if "error::pytest.PytestReturnNotNoneWarning" not in arguments:
            observation["return_value_location"] = "_pytest/python.py:171:" in output
    if any(source in arguments for source in ("test_dynamic_lookup.py", "test_scope_mismatch.py")):
        output = (result.stdout + result.stderr).replace(str(project.root), "<project>")
        source = next(
            source
            for source in ("test_dynamic_lookup.py", "test_scope_mismatch.py")
            if source in arguments
        )
        observation["diagnostic"] = diagnostic_excerpt(source, output)
    return observation


CASES: dict[str, tuple[dict[str, str], tuple[str, ...], bool]] = {
    "awaitable_return": (
        {
            "test_awaitable.py": """\
async def body():
    raise AssertionError("coroutine body must not run")

def test_returns_coroutine():
    return body()
""",
        },
        ("test_awaitable.py",),
        True,
    ),
    "return_value_warning": (
        {
            "test_return_value.py": """\
def test_returns_value():
    return 7
""",
        },
        ("test_return_value.py",),
        True,
    ),
    "return_value_warning_as_error": (
        {
            "test_return_value.py": """\
def test_returns_value():
    return 7
""",
        },
        (
            "-W",
            "error::pytest.PytestReturnNotNoneWarning",
            "test_return_value.py",
        ),
        True,
    ),
    "collection_error": (
        {
            "test_ok.py": "def test_ok():\n    assert True\n",
            "test_broken.py": "import missing_review_module\n",
        },
        ("--continue-on-collection-errors", "test_ok.py", "test_broken.py"),
        False,
    ),
    "collection_error_with_no_selected_tests": (
        {
            "test_ok.py": "def test_ok():\n    assert True\n",
            "test_broken.py": "import missing_review_module\n",
        },
        (
            "--continue-on-collection-errors",
            "-k",
            "never_matches",
            "test_ok.py",
            "test_broken.py",
        ),
        False,
    ),
    "package_scope": (
        {
            "pkg/__init__.py": "",
            "pkg/conftest.py": """\
from pathlib import Path
import pytest

@pytest.fixture(scope="package")
def shared():
    with Path("trace.txt").open("a") as stream:
        stream.write("setup\\n")
    yield
    with Path("trace.txt").open("a") as stream:
        stream.write("teardown\\n")
""",
            "pkg/test_a.py": "def test_a(shared):\n    pass\n",
            "pkg/subdir/test_b.py": "def test_b(shared):\n    pass\n",
        },
        ("pkg",),
        False,
    ),
    "dynamic_package_scope": (
        {
            "pkg/__init__.py": "",
            "pkg/conftest.py": """\
from pathlib import Path
import pytest

@pytest.fixture(scope="package")
def shared():
    with Path("trace.txt").open("a") as stream:
        stream.write("setup\\n")
    yield
    with Path("trace.txt").open("a") as stream:
        stream.write("teardown\\n")
""",
            "pkg/test_a.py": """\
from pathlib import Path

def test_a(request):
    request.getfixturevalue('shared')
    with Path("trace.txt").open("a") as stream:
        stream.write("first\\n")

def test_b(request):
    request.getfixturevalue('shared')
    with Path("trace.txt").open("a") as stream:
        stream.write("second\\n")
""",
            "zz_other/__init__.py": "",
            "zz_other/test_mid.py": """\
from pathlib import Path

def test_mid():
    with Path("trace.txt").open("a") as stream:
        stream.write("mid\\n")
""",
        },
        ("pkg", "zz_other"),
        False,
    ),
    "request_scope_and_dynamic_names": (
        {
            "test_request.py": """\
import pytest

@pytest.fixture(scope="module")
def shared(request):
    assert type(request.node).__name__ == "Module"
    assert request.module.__name__ == "test_request"
    with pytest.raises(AttributeError):
        request.function
    return 1

@pytest.fixture(scope="session")
def session_view(request):
    assert type(request.node).__name__ == "Session"
    with pytest.raises(AttributeError):
        request.path
    return 1

@pytest.fixture(scope="class")
def class_view(request):
    assert type(request.node).__name__ == "Class"
    assert request.cls.__name__ == "TestGroup"
    assert request.module.__name__ == "test_request"
    with pytest.raises(AttributeError):
        request.function
    return 1

@pytest.fixture
def extra():
    return 2

def test_request(shared, request):
    assert "extra" not in request.fixturenames
    assert request.getfixturevalue("extra") == 2
    assert "extra" in request.fixturenames

class TestGroup:
    def test_class(self, class_view, session_view, request):
        assert request.instance is self
""",
        },
        ("test_request.py",),
        False,
    ),
    "dynamic_names_during_scoped_teardown": (
        {
            "test_request.py": """\
from pathlib import Path
import pytest

@pytest.fixture(scope="module")
def dyn():
    return 1

@pytest.fixture(scope="module")
def outer(request):
    assert request.getfixturevalue("dyn") == 1
    yield
    Path("trace.txt").write_text(str("dyn" in request.fixturenames))

def test_first(outer):
    pass

def test_second(outer):
    pass
""",
        },
        ("test_request.py",),
        False,
    ),
    "dynamic_lookup_missing_fixture": (
        {
            "test_dynamic_lookup.py": """\
import pytest

@pytest.fixture
def outer(request):
    request.getfixturevalue("missing_dynamic_fixture")

def test_missing_fixture(outer):
    pass
""",
        },
        ("test_dynamic_lookup.py",),
        True,
    ),
    "dynamic_lookup_scope_mismatch": (
        {
            "test_scope_mismatch.py": """\
import pytest

@pytest.fixture(scope="session")
def session_request(request):
    return request

@pytest.fixture
def function_value():
    return 1

def test_scope_mismatch(session_request):
    session_request.getfixturevalue("function_value")
""",
        },
        ("test_scope_mismatch.py",),
        True,
    ),
    "double_yield": (
        {
            "test_yield.py": """\
import pytest

@pytest.fixture
def resource():
    yield 1
    yield 2

def test_yield(resource):
    assert resource == 1
""",
        },
        ("test_yield.py",),
        False,
    ),
    "callable_finalizer": (
        {
            "test_finalizer.py": """\
from pathlib import Path
import pytest

class Finalizer:
    def __call__(self):
        Path("trace.txt").write_text("call")

    def __next__(self):
        Path("trace.txt").write_text("next")

@pytest.fixture
def resource(request):
    request.addfinalizer(Finalizer())

def test_finalizer(resource):
    pass
""",
        },
        ("test_finalizer.py",),
        False,
    ),
    "finalizer_added_during_teardown": (
        {
            "test_finalizer.py": """\
from pathlib import Path
import pytest

@pytest.fixture
def resource(request):
    yield
    request.addfinalizer(lambda: Path("trace.txt").write_text("late"))

def test_finalizer(resource):
    pass
""",
        },
        ("test_finalizer.py",),
        False,
    ),
    "yield_fixture_teardown_skip": (
        {
            "test_teardown_skip.py": """\
import pytest

@pytest.fixture
def resource():
    yield
    pytest.skip("cleanup is unavailable")

def test_resource(resource):
    pass
""",
        },
        ("test_teardown_skip.py",),
        True,
    ),
    "addfinalizer_teardown_skip": (
        {
            "test_teardown_skip.py": """\
import pytest

@pytest.fixture
def resource(request):
    request.addfinalizer(lambda: pytest.skip("cleanup is unavailable"))

def test_resource(resource):
    pass
""",
        },
        ("test_teardown_skip.py",),
        True,
    ),
    "teardown_skip_does_not_hide_error": (
        {
            "test_teardown_skip.py": """\
import pytest

def skip_cleanup():
    pytest.skip("cleanup is unavailable")

def fail_cleanup():
    raise ValueError("cleanup failed")

@pytest.fixture
def resource(request):
    request.addfinalizer(skip_cleanup)
    request.addfinalizer(fail_cleanup)

def test_resource(resource):
    pass
""",
        },
        ("test_teardown_skip.py",),
        True,
    ),
    "scoped_fixture_invalidation_skip": (
        {
            "test_teardown_skip.py": """\
import pytest

@pytest.fixture(scope="module", params=(1, 2))
def resource(request):
    yield request.param
    if request.param == 1:
        pytest.skip("first parameter cleanup is unavailable")

def test_resource(resource):
    assert resource in (1, 2)
""",
        },
        ("test_teardown_skip.py",),
        True,
    ),
    "xfail_run_false_phase": (
        {
            "test_xfail.py": """\
import pytest

@pytest.mark.xfail(run=False, reason="not run")
def test_xfail():
    raise AssertionError("must not run")
""",
        },
        ("test_xfail.py",),
        True,
    ),
    "xfail_exception_filter": (
        {
            "test_xfail.py": """\
import pytest

@pytest.mark.xfail(raises=ValueError, reason="specific")
def test_matching_exception():
    raise ValueError("expected")

@pytest.mark.xfail(raises=ValueError, reason="specific")
def test_other_exception():
    raise AssertionError("unexpected")
""",
        },
        ("test_xfail.py",),
        True,
    ),
    "xfail_check_callback_error": (
        {
            "test_callback.py": """\
import pytest

@pytest.mark.xfail(raises=pytest.RaisesExc(ValueError, check=lambda error: 1 / 0))
def test_callback():
    raise ValueError("expected")
""",
        },
        ("test_callback.py",),
        False,
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    observations: dict[str, dict[str, dict[str, object]]] = {}
    mismatches: list[str] = []
    for name, (files, arguments, phase_log) in CASES.items():
        observations[name] = {
            kind: run_case(
                output / name / kind, python.absolute(), files, arguments, phase_log=phase_log
            )
            for kind, python in (("stock", args.stock_python), ("candidate", args.candidate_python))
        }
        stock = observations[name]["stock"]
        candidate = observations[name]["candidate"]
        if stock != candidate:
            mismatches.append(name)
    capture_files = {
        "test_capture.py": """\
from pathlib import Path
Path("trace.txt").write_text("imported")

def test_capture():
    pass
""",
    }
    observations["capture_validation"] = {
        kind: run_case(
            output / "capture_validation" / kind,
            python.absolute(),
            capture_files,
            ("--capture=sys", "test_capture.py"),
        )
        for kind, python in (("stock", args.stock_python), ("candidate", args.candidate_python))
    }
    if observations["capture_validation"]["stock"]["exit_status"] != 0:
        raise RuntimeError("stock pytest 9.1.1 no longer supports --capture=sys")
    if observations["capture_validation"]["stock"]["trace"] != "imported":
        raise RuntimeError("stock pytest did not import the capture probe")
    if (
        observations["capture_validation"]["candidate"]
        != observations["capture_validation"]["stock"]
    ):
        mismatches.append("capture_validation")
    expected = {
        "awaitable_return": {
            "exit_status": 1,
            "awaitable_diagnostic": True,
            "unawaited_coroutine_warning": True,
        },
        "return_value_warning": {
            "exit_status": 0,
            "return_value_warning": True,
            "return_value_message": True,
            "return_value_location": True,
        },
        "return_value_warning_as_error": {
            "exit_status": 1,
            "return_value_warning": True,
            "return_value_message": True,
        },
        "collection_error": {"exit_status": 1},
        "collection_error_with_no_selected_tests": {"exit_status": 1},
        "package_scope": {"trace": "setup\nteardown\n"},
        "dynamic_package_scope": {"trace": "setup\nfirst\nsecond\nteardown\nmid\n"},
        "request_scope_and_dynamic_names": {"exit_status": 0},
        "dynamic_names_during_scoped_teardown": {"trace": "True"},
        "dynamic_lookup_missing_fixture": {
            "exit_status": 1,
            "phases": [
                {
                    "nodeid": "test_dynamic_lookup.py::test_missing_fixture",
                    "phase": "setup",
                    "outcome": "error",
                },
                {
                    "nodeid": "test_dynamic_lookup.py::test_missing_fixture",
                    "phase": "teardown",
                    "outcome": "passed",
                },
            ],
        },
        "dynamic_lookup_scope_mismatch": {
            "exit_status": 1,
            "phases": [
                {
                    "nodeid": "test_scope_mismatch.py::test_scope_mismatch",
                    "phase": "setup",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_scope_mismatch.py::test_scope_mismatch",
                    "phase": "call",
                    "outcome": "failed",
                },
                {
                    "nodeid": "test_scope_mismatch.py::test_scope_mismatch",
                    "phase": "teardown",
                    "outcome": "passed",
                },
            ],
        },
        "double_yield": {
            "exit_status": 1,
            "double_yield_error": True,
            "double_yield_source": True,
            "double_yield_location": True,
        },
        "callable_finalizer": {"trace": "call"},
        "finalizer_added_during_teardown": {"trace": "late"},
        "yield_fixture_teardown_skip": {
            "exit_status": 0,
            "phases": [
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "setup",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "call",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "teardown",
                    "outcome": "skipped",
                },
            ],
        },
        "addfinalizer_teardown_skip": {
            "exit_status": 0,
            "phases": [
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "setup",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "call",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "teardown",
                    "outcome": "skipped",
                },
            ],
        },
        "teardown_skip_does_not_hide_error": {
            "exit_status": 1,
            "phases": [
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "setup",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "call",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource",
                    "phase": "teardown",
                    "outcome": "error",
                },
            ],
        },
        "scoped_fixture_invalidation_skip": {
            "exit_status": 0,
            "phases": [
                {
                    "nodeid": "test_teardown_skip.py::test_resource[1]",
                    "phase": "setup",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource[1]",
                    "phase": "call",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource[1]",
                    "phase": "teardown",
                    "outcome": "passed",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource[2]",
                    "phase": "setup",
                    "outcome": "skipped",
                },
                {
                    "nodeid": "test_teardown_skip.py::test_resource[2]",
                    "phase": "teardown",
                    "outcome": "passed",
                },
            ],
        },
        "xfail_run_false_phase": {"exit_status": 0},
        "xfail_exception_filter": {"exit_status": 1},
        "xfail_check_callback_error": {"exit_status": 3, "callback_error": True},
    }
    for name, fields in expected.items():
        for key, value in fields.items():
            if observations[name]["stock"][key] != value:
                raise RuntimeError(f"stock pytest 9.1.1 oracle changed for {name}.{key}")
    report = output / "review-regressions.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(observations, indent=2) + "\n")
    if mismatches:
        raise RuntimeError(f"installed pytest differential failed for {mismatches}: {report}")
    sys.stdout.write(f"{report}\n")


if __name__ == "__main__":
    main()
