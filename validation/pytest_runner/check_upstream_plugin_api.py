"""Compare pytest's programmatic plugin help API through installed wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support import VfyProject
from upstream_pytest import UPSTREAM_COMMIT, git_output, pytest_version

SOURCE = "testing/acceptance_test.py"
NODEID = "acceptance_test.py::TestInvocationVariants::test_invoke_plugin_api"
CONFIG_ERROR_NODEID = "acceptance_test.py::TestGeneralUsage::test_config_error"
CONFIG_ERROR_EXAMPLE = "testing/example_scripts/conftest_usageerror/conftest.py"
UNCONFIGURE_NODEID = (
    "acceptance_test.py::TestGeneralUsage::test_file_not_found_unconfigure_issue143"
)
TRACE_SOURCE = "testing/test_pluginmanager.py"
TRACE_NODEID = "testing/test_pluginmanager.py::TestPytestPluginInteractions::test_hook_tracing"
NATIVE_PLUGIN = "vfy._pytest.native_pytest"
MIXED_PLUGIN = """import os
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption("--myopt")


def pytest_load_initial_conftests(early_config, parser, args):
    del early_config, parser, args
    with Path(os.environ["PYTEST_PLUGIN_API_EVENT"]).open("a", encoding="utf-8") as stream:
        stream.write("initial\\n")


def pytest_runtest_setup(item):
    del item
    with Path(os.environ["PYTEST_PLUGIN_API_EVENT"]).open("a", encoding="utf-8") as stream:
        stream.write("setup\\n")
"""
TRACED_RUNTIME_PLUGIN = """import os
from pathlib import Path


def pytest_plugin_registered(plugin, plugin_name, manager):
    del plugin, manager
    with Path(os.environ["PYTEST_PLUGIN_API_TRACE_EVENT"]).open("a", encoding="utf-8") as stream:
        stream.write(f"registered:{plugin_name}\\n")


def pytest_runtest_setup(item):
    del item
    with Path(os.environ["PYTEST_PLUGIN_API_TRACE_EVENT"]).open("a", encoding="utf-8") as stream:
        stream.write("setup\\n")
"""
TRACED_RUNTIME_TEST = """import os
from pathlib import Path


def test_runs():
    with Path(os.environ["PYTEST_PLUGIN_API_TRACE_EVENT"]).open("a", encoding="utf-8") as stream:
        stream.write("test\\n")
"""
PRELOADED_MANAGER_CHECK = """import importlib
import os
from pathlib import Path

import pytest
from _pytest.config import PytestPluginManager

event_path = Path(os.environ["PYTEST_PLUGIN_API_PRELOADED_EVENT"])


class PreloadedRuntimePlugin:
    def pytest_runtest_setup(self, item):
        del item
        event_path.write_text("runtime hook ran", encoding="utf-8")


manager = PytestPluginManager()
manager.register(PreloadedRuntimePlugin(), "preloaded-runtime-plugin")
native_plugin = importlib.import_module("vfy._pytest.native_pytest")
try:
    manager.register(native_plugin, "native_pytest")
except pytest.UsageError as error:
    if "pytest_runtest_setup" not in str(error):
        raise
else:
    raise RuntimeError("native registration accepted a previously registered runtime hook")
print("preloaded runtime hook rejected")
"""


def _activate_native(command: list[str], *, native: bool) -> None:
    if native:
        index = command.index("no:cacheprovider") + 1
        command[index:index] = ["-p", NATIVE_PLUGIN]


MIXED_TEST = """import os
from pathlib import Path


def test_runs():
    with Path(os.environ["PYTEST_PLUGIN_API_EVENT"]).open("a", encoding="utf-8") as stream:
        stream.write("test\\n")
"""
CONFTEST = """def pytest_addoption(parser):
    parser.addoption("--project-option")
"""
LIFECYCLE_PLUGIN = """import os
from pathlib import Path


def pytest_addoption(parser):
    parser.addoption("--early-option")


def pytest_load_initial_conftests(early_config, parser, args):
    del parser, args
    value = early_config.known_args_namespace.early_option
    with Path(os.environ["PYTEST_PLUGIN_API_EARLY_EVENT"]).open("w", encoding="utf-8") as stream:
        stream.write(value)
"""
LIFECYCLE_TEST = """import os
from pathlib import Path


def test_early_plugin_hook_ran():
    event = Path(os.environ["PYTEST_PLUGIN_API_EARLY_EVENT"])
    assert event.read_text(encoding="utf-8") == "ready"
"""
MIXED_CONFTEST = """import os
from pathlib import Path


def _record(value):
    with Path(os.environ["PYTEST_PLUGIN_API_CONFTEST_EVENT"]).open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write(f"{value}\\n")


def pytest_configure(config):
    del config
    _record("configure")


def pytest_unconfigure(config):
    del config
    _record("unconfigure")


def pytest_runtest_setup(item):
    del item
    _record("runtime")
"""


def run_exact_case(
    project: VfyProject,
    python: Path,
    result_path: Path | None,
) -> tuple[int, str, str]:
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
        "-p",
        "no:cacheprovider",
        NODEID,
    ]
    _activate_native(command, native=result_path is not None)
    result = project.run(
        *command,
        env={
            "PYTHONPATH": None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_NATIVE_RESULT": str(result_path) if result_path else None,
        },
    )
    return result.returncode, result.stdout, result.stderr


def run_mixed_plugin_case(
    project: VfyProject,
    python: Path,
    event_path: Path,
    *,
    native: bool,
) -> tuple[int, str, str]:
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
        "-p",
        "no:cacheprovider",
        "-p",
        "mixed_plugin",
        "test_mixed_plugin.py",
    ]
    _activate_native(command, native=native)
    result = project.run(
        *command,
        env={
            "PYTHONPATH": None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_PLUGIN_API_EVENT": str(event_path),
        },
    )
    return result.returncode, result.stdout, result.stderr


def run_upstream_hook_tracing_case(
    project: VfyProject,
    python: Path,
    result_path: Path | None,
) -> tuple[int, str, str]:
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
        "-p",
        "no:cacheprovider",
        "-p",
        "pytester",
        TRACE_NODEID,
    ]
    _activate_native(command, native=result_path is not None)
    result = project.run(
        *command,
        env={
            "PYTHONPATH": None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_NATIVE_RESULT": str(result_path) if result_path else None,
        },
    )
    return result.returncode, result.stdout, result.stderr


def run_traced_mixed_plugin_case(
    project: VfyProject,
    python: Path,
    event_path: Path,
    *,
    native: bool,
) -> tuple[int, str, str]:
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
        "-p",
        "no:cacheprovider",
        "-p",
        "traced_runtime_plugin",
        "test_traced_runtime_plugin.py",
    ]
    _activate_native(command, native=native)
    result = project.run(
        *command,
        env={
            "PYTHONPATH": None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_PLUGIN_API_TRACE_EVENT": str(event_path),
        },
    )
    return result.returncode, result.stdout, result.stderr


def run_preloaded_manager_case(
    project: VfyProject,
    python: Path,
    event_path: Path,
) -> tuple[int, str, str]:
    script = project.write("check_preloaded_manager.py", PRELOADED_MANAGER_CHECK)
    result = project.run(
        str(python.absolute()),
        str(script),
        env={
            "PYTHONPATH": None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_PLUGIN_API_PRELOADED_EVENT": str(event_path),
        },
    )
    return result.returncode, result.stdout, result.stderr


def run_conftest_help(project: VfyProject, python: Path, *, native: bool) -> tuple[int, str, str]:
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-h",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
        "-p",
        "no:cacheprovider",
    ]
    _activate_native(command, native=native)
    result = project.run(
        *command,
        env={
            "PYTHONPATH": None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
    )
    return result.returncode, result.stdout, result.stderr


def run_lifecycle_plugin_case(
    project: VfyProject,
    python: Path,
    event_path: Path,
    result_path: Path | None,
) -> tuple[int, str, str]:
    command = [
        str(python.absolute()),
        "-m",
        "pytest",
        "-q",
        "-c",
        os.devnull,
        "--rootdir",
        str(project.root),
        "-p",
        "no:cacheprovider",
        "-p",
        "lifecycle_plugin",
        "--early-option=ready",
        "test_lifecycle_plugin.py",
    ]
    _activate_native(command, native=result_path is not None)
    result = project.run(
        *command,
        env={
            "PYTHONPATH": None,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_PLUGIN_API_EARLY_EVENT": str(event_path),
            "PYTEST_NATIVE_RESULT": str(result_path) if result_path else None,
        },
    )
    return result.returncode, result.stdout, result.stderr


def check_exact_upstream_case(
    destination: Path,
    source: bytes,
    source_hash: str,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    native_result = destination / "candidate-native-result.json"
    for name, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"plugin-api-{name}")
        project.root.mkdir(parents=True, exist_ok=True)
        copied_source = project.root / "acceptance_test.py"
        copied_source.write_bytes(source)
        if hashlib.sha256(copied_source.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("upstream acceptance test changed while copying")
        result_path = native_result if name == "candidate" else None
        status, stdout, stderr = run_exact_case(project, python, result_path)
        if status != 0 or "1 passed" not in stdout:
            raise RuntimeError(
                f"{name} upstream plugin API case failed with exit {status}:\n{stdout}\n{stderr}"
            )
        observations[name] = {"returncode": status, "passed": 1}
        if result_path is not None:
            record = json.loads(result_path.read_text(encoding="utf-8"))
            cases = record.get("cases", [])
            if (
                record.get("engine") != "pytest-runner-native-v1"
                or [case.get("nodeid") for case in cases] != [NODEID]
                or [
                    (phase.get("phase"), phase.get("outcome"))
                    for phase in cases[0].get("phases", [])
                ]
                != [
                    ("setup", "passed"),
                    ("call", "passed"),
                    ("teardown", "passed"),
                ]
            ):
                raise RuntimeError(f"candidate did not pass the upstream case natively: {record}")
    if observations["stock"] != observations["candidate"]:
        raise RuntimeError(f"upstream plugin API differs: {observations}")
    return observations


def check_conftest_session_lifecycle(
    destination: Path,
    source: bytes,
    source_hash: str,
    example: bytes,
    example_hash: str,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    cases = (
        ("configure-error", CONFIG_ERROR_NODEID, True),
        ("unconfigure-after-collection-error", UNCONFIGURE_NODEID, False),
    )
    for name, nodeid, needs_example in cases:
        observations[name] = {}
        native_result = destination / f"candidate-{name}-native-result.json"
        for engine, python in (("stock", stock_python), ("candidate", candidate_python)):
            project = VfyProject(destination / f"conftest-lifecycle-{name}-{engine}")
            project.root.mkdir(parents=True, exist_ok=True)
            copied_source = project.root / "acceptance_test.py"
            copied_source.write_bytes(source)
            if hashlib.sha256(copied_source.read_bytes()).hexdigest() != source_hash:
                raise RuntimeError("upstream acceptance test changed while copying")
            example_directory = project.root / "testing/example_scripts/conftest_usageerror"
            if needs_example:
                example_path = example_directory / "conftest.py"
                example_path.parent.mkdir(parents=True, exist_ok=True)
                example_path.write_bytes(example)
                if hashlib.sha256(example_path.read_bytes()).hexdigest() != example_hash:
                    raise RuntimeError("upstream conftest example changed while copying")

            command = [
                str(python.absolute()),
                "-m",
                "pytest",
                "-q",
                "-c",
                os.devnull,
                "--rootdir",
                str(project.root),
                "-p",
                "no:cacheprovider",
                "-p",
                "pytester",
            ]
            _activate_native(command, native=engine == "candidate")
            if needs_example:
                command.extend(
                    ["-o", f"pytester_example_dir={project.root / 'testing/example_scripts'}"]
                )
            command.append(nodeid)
            result_path = native_result if engine == "candidate" else None
            result = project.run(
                *command,
                env={
                    "PYTHONPATH": None,
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "PYTEST_NATIVE_RESULT": str(result_path) if result_path else None,
                },
            )
            if result.returncode != 0 or "1 passed" not in result.stdout:
                raise RuntimeError(
                    f"{engine} upstream conftest lifecycle case {nodeid} failed "
                    f"with exit {result.returncode}:\n{result.stdout}\n{result.stderr}"
                )
            observations[name][engine] = {"returncode": result.returncode, "passed": 1}
            if result_path is not None:
                record = json.loads(result_path.read_text(encoding="utf-8"))
                cases_result = record.get("cases", [])
                if (
                    record.get("engine") != "pytest-runner-native-v1"
                    or [case.get("nodeid") for case in cases_result] != [nodeid]
                    or [
                        (phase.get("phase"), phase.get("outcome"))
                        for phase in cases_result[0].get("phases", [])
                    ]
                    != [("setup", "passed"), ("call", "passed"), ("teardown", "passed")]
                ):
                    raise RuntimeError(
                        f"candidate did not execute the upstream case natively: {record}"
                    )
        if observations[name]["stock"] != observations[name]["candidate"]:
            raise RuntimeError(
                f"conftest lifecycle behavior differs for {nodeid}: {observations[name]}"
            )
    return observations


def check_mixed_conftest_lifecycle(
    destination: Path,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    for engine, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"mixed-conftest-lifecycle-{engine}")
        project.root.mkdir(parents=True, exist_ok=True)
        project.write("conftest.py", MIXED_CONFTEST)
        project.write("test_case.py", "def test_runs():\n    pass\n")
        event_path = destination / f"mixed-conftest-lifecycle-{engine}-event.txt"
        event_path.unlink(missing_ok=True)
        command = [
            str(python.absolute()),
            "-m",
            "pytest",
            "-q",
            "-c",
            os.devnull,
            "--rootdir",
            str(project.root),
            "-p",
            "no:cacheprovider",
            "test_case.py",
        ]
        _activate_native(command, native=engine == "candidate")
        result = project.run(
            *command,
            env={
                "PYTHONPATH": None,
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTEST_PLUGIN_API_CONFTEST_EVENT": str(event_path),
            },
        )
        events = event_path.read_text(encoding="utf-8").splitlines() if event_path.is_file() else []
        if (
            result.returncode != 0
            or "1 passed" not in result.stdout
            or events != ["configure", "runtime", "unconfigure"]
        ):
            raise RuntimeError(
                f"{engine} mixed conftest ran {events!r}:\n{result.stdout}\n{result.stderr}"
            )
        observations[engine] = {"returncode": result.returncode, "hooks": events}
    return observations


def check_hook_tracing(
    destination: Path,
    source: bytes,
    source_hash: str,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    native_result = destination / "candidate-hook-tracing-native-result.json"
    for name, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"hook-tracing-{name}")
        project.root.mkdir(parents=True, exist_ok=True)
        copied_source = project.root / TRACE_SOURCE
        copied_source.parent.mkdir(parents=True, exist_ok=True)
        copied_source.write_bytes(source)
        if hashlib.sha256(copied_source.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("upstream hook-tracing test changed while copying")
        result_path = native_result if name == "candidate" else None
        status, stdout, stderr = run_upstream_hook_tracing_case(project, python, result_path)
        if status != 0 or "1 passed" not in stdout:
            raise RuntimeError(
                f"{name} upstream hook-tracing case failed with exit {status}:\n{stdout}\n{stderr}"
            )
        observations[name] = {"returncode": status, "passed": 1}
        if result_path is not None:
            record = json.loads(result_path.read_text(encoding="utf-8"))
            cases = record.get("cases", [])
            if (
                record.get("engine") != "pytest-runner-native-v1"
                or [case.get("nodeid") for case in cases] != [TRACE_NODEID]
                or [
                    (phase.get("phase"), phase.get("outcome"))
                    for phase in cases[0].get("phases", [])
                ]
                != [("setup", "passed"), ("call", "passed"), ("teardown", "passed")]
            ):
                raise RuntimeError(f"candidate did not pass the upstream case natively: {record}")
    if observations["stock"] != observations["candidate"]:
        raise RuntimeError(f"upstream hook-tracing behavior differs: {observations}")
    return observations


def check_conftest_help(
    destination: Path,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    for name, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"conftest-option-{name}")
        project.root.mkdir(parents=True, exist_ok=True)
        project.write("conftest.py", CONFTEST)
        status, stdout, stderr = run_conftest_help(project, python, native=name == "candidate")
        if status != 0 or "--project-option" not in stdout:
            raise RuntimeError(
                f"{name} conftest parser option is missing from help:\n{stdout}\n{stderr}"
            )
        observations[name] = {"returncode": status, "option_visible": True}
    return observations


def check_lifecycle_plugin(
    destination: Path,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    for name, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"lifecycle-plugin-{name}")
        project.root.mkdir(parents=True, exist_ok=True)
        project.write("lifecycle_plugin.py", LIFECYCLE_PLUGIN)
        project.write("test_lifecycle_plugin.py", LIFECYCLE_TEST)
        event_path = destination / f"lifecycle-plugin-{name}-event.txt"
        result_path = destination / f"lifecycle-plugin-{name}-native-result.json"
        status, stdout, stderr = run_lifecycle_plugin_case(
            project, python, event_path, result_path if name == "candidate" else None
        )
        if (
            status != 0
            or "1 passed" not in stdout
            or not event_path.is_file()
            or event_path.read_text(encoding="utf-8") != "ready"
        ):
            raise RuntimeError(
                f"{name} startup hook case failed with exit {status}:\n{stdout}\n{stderr}"
            )
        observations[name] = {"returncode": status, "early_option": "ready", "passed": 1}
        if name == "candidate":
            record = json.loads(result_path.read_text(encoding="utf-8"))
            cases = record.get("cases", [])
            if (
                record.get("engine") != "pytest-runner-native-v1"
                or [case.get("nodeid") for case in cases]
                != ["test_lifecycle_plugin.py::test_early_plugin_hook_ran"]
                or [
                    (phase.get("phase"), phase.get("outcome"))
                    for phase in cases[0].get("phases", [])
                ]
                != [("setup", "passed"), ("call", "passed"), ("teardown", "passed")]
            ):
                raise RuntimeError(
                    f"candidate did not pass the startup hook case natively: {record}"
                )
    if observations["stock"] != observations["candidate"]:
        raise RuntimeError(f"startup plugin lifecycle differs: {observations}")
    return observations


def check_mixed_plugin_rejection(
    destination: Path,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    for name, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"mixed-plugin-{name}")
        project.root.mkdir(parents=True, exist_ok=True)
        project.write("mixed_plugin.py", MIXED_PLUGIN)
        project.write("test_mixed_plugin.py", MIXED_TEST)
        event_path = destination / f"mixed-plugin-{name}-events.txt"
        status, stdout, stderr = run_mixed_plugin_case(
            project, python, event_path, native=name == "candidate"
        )
        if name == "stock":
            events = (
                event_path.read_text(encoding="utf-8").splitlines() if event_path.exists() else []
            )
            if status != 0 or events != ["initial", "setup", "test"]:
                raise RuntimeError(f"stock mixed plugin case failed:\n{stdout}\n{stderr}")
            observations[name] = {"returncode": status, "events": events}
            continue
        if status != 4 or "pytest_runtest_setup" not in stderr or event_path.exists():
            raise RuntimeError(
                "candidate did not reject the runtime hook before startup or test hooks: "
                f"exit={status}\n{stdout}\n{stderr}"
            )
        observations[name] = {
            "returncode": status,
            "rejected_hook": "pytest_runtest_setup",
            "startup_hook_invoked": False,
            "test_executed": False,
        }
    return observations


def check_traced_runtime_plugin_rejection(
    destination: Path,
    stock_python: Path,
    candidate_python: Path,
) -> dict[str, dict[str, object]]:
    observations: dict[str, dict[str, object]] = {}
    for name, python in (("stock", stock_python), ("candidate", candidate_python)):
        project = VfyProject(destination / f"traced-mixed-plugin-{name}")
        project.root.mkdir(parents=True, exist_ok=True)
        project.write("traced_runtime_plugin.py", TRACED_RUNTIME_PLUGIN)
        project.write("test_traced_runtime_plugin.py", TRACED_RUNTIME_TEST)
        event_path = destination / f"traced-mixed-plugin-{name}-events.txt"
        status, stdout, stderr = run_traced_mixed_plugin_case(
            project, python, event_path, native=name == "candidate"
        )
        if name == "stock":
            events = (
                event_path.read_text(encoding="utf-8").splitlines() if event_path.exists() else []
            )
            if (
                status != 0
                or "1 passed" not in stdout
                or not any(event.startswith("registered:") for event in events)
                or "setup" not in events
                or "test" not in events
            ):
                raise RuntimeError(f"stock traced plugin case failed:\n{stdout}\n{stderr}")
            observations[name] = {"returncode": status, "events": events}
            continue
        if status != 4 or "pytest_runtest_setup" not in stderr or event_path.exists():
            raise RuntimeError(
                "candidate did not reject a mixed traced/runtime plugin before callback or test: "
                f"exit={status}\n{stdout}\n{stderr}"
            )
        observations[name] = {
            "returncode": status,
            "rejected_hook": "pytest_runtest_setup",
            "registration_callback_invoked": False,
            "test_executed": False,
        }
    return observations


def check_preloaded_manager_rejection(
    destination: Path,
    candidate_python: Path,
) -> dict[str, object]:
    project = VfyProject(destination / "preloaded-manager")
    project.root.mkdir(parents=True, exist_ok=True)
    event_path = destination / "preloaded-manager-events.txt"
    status, stdout, stderr = run_preloaded_manager_case(project, candidate_python, event_path)
    if status != 0 or "preloaded runtime hook rejected" not in stdout or event_path.exists():
        raise RuntimeError(
            "candidate did not reject a plugin present before native activation: "
            f"exit={status}\n{stdout}\n{stderr}"
        )
    return {
        "returncode": status,
        "rejected_hook": "pytest_runtest_setup",
        "runtime_hook_invoked": False,
    }


def write_report(path: Path, report: dict[str, object]) -> None:
    if path.exists():
        raise FileExistsError(f"upstream report already exists: {path}")
    descriptor, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_path, path)
    finally:
        Path(temporary_path).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-root", required=True, type=Path)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    upstream = args.upstream_root.resolve()
    if git_output(upstream, "rev-parse", "HEAD") != UPSTREAM_COMMIT:
        raise RuntimeError(f"pytest source must be pinned to {UPSTREAM_COMMIT}")
    if git_output(upstream, "status", "--porcelain"):
        raise RuntimeError("pytest source checkout must be clean")
    versions = {
        "stock": pytest_version(args.stock_python),
        "candidate": pytest_version(args.candidate_python),
    }
    if versions != {"stock": "9.1.1", "candidate": "9.1.1"}:
        raise RuntimeError(f"installed pytest versions differ from the pinned pair: {versions}")

    source = (upstream / SOURCE).read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    configure_example = (upstream / CONFIG_ERROR_EXAMPLE).read_bytes()
    configure_example_hash = hashlib.sha256(configure_example).hexdigest()
    trace_source = (upstream / TRACE_SOURCE).read_bytes()
    trace_source_hash = hashlib.sha256(trace_source).hexdigest()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    observations = check_exact_upstream_case(
        destination, source, source_hash, args.stock_python, args.candidate_python
    )
    conftest_lifecycle_observations = check_conftest_session_lifecycle(
        destination,
        source,
        source_hash,
        configure_example,
        configure_example_hash,
        args.stock_python,
        args.candidate_python,
    )
    mixed_conftest_observations = check_mixed_conftest_lifecycle(
        destination, args.stock_python, args.candidate_python
    )
    hook_tracing_observations = check_hook_tracing(
        destination,
        trace_source,
        trace_source_hash,
        args.stock_python,
        args.candidate_python,
    )
    configuration_help = check_conftest_help(destination, args.stock_python, args.candidate_python)
    lifecycle_observations = check_lifecycle_plugin(
        destination, args.stock_python, args.candidate_python
    )
    failure_observations = check_mixed_plugin_rejection(
        destination, args.stock_python, args.candidate_python
    )
    traced_failure_observations = check_traced_runtime_plugin_rejection(
        destination, args.stock_python, args.candidate_python
    )
    preloaded_manager_observation = check_preloaded_manager_rejection(
        destination, args.candidate_python
    )

    report: dict[str, object] = {
        "upstream_revision": UPSTREAM_COMMIT,
        "source_path": SOURCE,
        "source_sha256": source_hash,
        "conftest_configure_example_path": CONFIG_ERROR_EXAMPLE,
        "conftest_configure_example_sha256": configure_example_hash,
        "hook_tracing_source_path": TRACE_SOURCE,
        "hook_tracing_source_sha256": trace_source_hash,
        "pytest_versions": versions,
        "nodeid": NODEID,
        "upstream_stock_candidate_match": observations["stock"] == observations["candidate"],
        "hook_tracing_nodeid": TRACE_NODEID,
        "hook_tracing_stock_candidate_match": hook_tracing_observations["stock"]
        == hook_tracing_observations["candidate"],
        "native_phase_results_checked": True,
        "conftest_session_lifecycle": conftest_lifecycle_observations,
        "mixed_conftest_lifecycle_runtime": mixed_conftest_observations,
        "conftest_addoption_help": configuration_help,
        "startup_plugin_hooks": lifecycle_observations,
        "mixed_addoption_runtime_plugin": failure_observations,
        "mixed_hook_tracing_runtime_plugin": traced_failure_observations,
        "plugin_registered_before_native_activation": preloaded_manager_observation,
    }
    report_path = destination / "upstream-plugin-api.json"
    write_report(report_path, report)
    sys.stdout.write(f"{report_path}\n")


if __name__ == "__main__":
    main()
