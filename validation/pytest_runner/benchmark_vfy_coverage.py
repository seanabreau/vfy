"""Parity-gated speed and memory measurements for vfy serial test engines."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import re
import shutil
import statistics
import sys
import tomllib
import zipfile
from datetime import datetime
from email import message_from_bytes
from pathlib import Path
from typing import Literal, TypeGuard

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    benchmark_environment,
    git_value,
    rotated_orders,
    run_checked,
    sampling_config,
    summarize,
    timed_process,
)
from benchmarks.benchmark_projects import normalized_data
from benchmarks.benchmark_provenance import (
    _verify_build_manifest,
    hardware_details,
    require_clean,
    require_revision,
)
from scripts.process_tree import run_captured

CASES = 1_000
NATIVE_ENGINE = "pytest-runner-native-v1"
ORDER_PATH = "VFY_BENCHMARK_ORDER"
LIFECYCLE_PATH = "VFY_BENCHMARK_LIFECYCLE"
_DISTRIBUTION_PACKAGE_ROOTS = {
    "vfy": ("vfy",),
    "pytest": ("_pytest", "pytest", "py.py"),
}
_INSTALLER_GENERATED_METADATA = frozenset(
    {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}
)
MACOS_RSS = re.compile(r"(?m)^\s*(?P<bytes>\d+)\s+maximum resident set size$")
LINUX_RSS = re.compile(r"(?m)^\s*Maximum resident set size \(kbytes\):\s*(?P<kb>\d+)\s*$")


def is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def case_ids(cases: int = CASES) -> list[str]:
    return [f"test_workload.py::test_case[case-{index:04d}]" for index in range(cases)]


def create_project(
    root: Path,
    cases: int = CASES,
    workload: str = "sync",
    *,
    extra_source_files: int = 0,
    functions_per_extra_source: int = 32,
) -> str:
    """Write identical test fixtures and optional source files that tests never import."""

    if extra_source_files < 0 or functions_per_extra_source < 1:
        raise ValueError("extra source count must be non-negative and function count positive")
    if root.exists():
        raise FileExistsError(f"benchmark project already exists: {root}")
    root.mkdir(parents=True)
    (root / "src").mkdir()
    files = {
        "pytest.ini": "[pytest]\n",
        "src/__init__.py": "",
        "src/sample.py": (
            "def measured(value):\n"
            "    if value % 2:\n"
            "        return value + 3\n"
            "    return value - 1\n"
        ),
        "test_workload.py": f"""from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.sample import measured

_executed: list[str] = []
_lifecycle_path = os.environ.get("{LIFECYCLE_PATH}")
_lifecycle_stream = (
    Path(_lifecycle_path).open("a", encoding="utf-8") if _lifecycle_path else None
)


def _record_lifecycle(event: str, nodeid: str | None = None) -> None:
    if _lifecycle_stream is not None:
        suffix = f":{{nodeid}}" if nodeid is not None else ""
        _lifecycle_stream.write(f"{{event}}{{suffix}}\\n")


@pytest.fixture(scope="session", autouse=True)
def record_order():
    yield
    try:
        destination = os.environ.get("VFY_BENCHMARK_ORDER")
        if destination:
            Path(destination).write_text("\\n".join(_executed) + "\\n", encoding="utf-8")
    finally:
        if _lifecycle_stream is not None:
            _lifecycle_stream.close()


@pytest.mark.parametrize(
    "value",
    range({cases}),
    ids=[f"case-{{value:04d}}" for value in range({cases})],
)
def test_case(value, request):
    measured_value = value % 101
    expected = measured_value + 3 if measured_value % 2 else measured_value - 1
    assert measured(measured_value) == expected
    if os.environ.get("VFY_BENCHMARK_ORDER"):
        _executed.append(request.node.nodeid)
""",
    }
    if workload == "async":
        files["test_workload.py"] = (
            files["test_workload.py"]
            .replace(
                "import pytest", "import pytest\nimport asyncio\nfrom vfy import async_fixture"
            )
            .replace(
                "@pytest.mark.parametrize(",
                "@async_fixture\nasync def resource(request):\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('async.setup', request.node.nodeid)\n"
                "    await asyncio.sleep(0)\n    yield None\n    await asyncio.sleep(0)\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('async.teardown', request.node.nodeid)\n\n"
                "@pytest.mark.parametrize(",
            )
            .replace(
                "def test_case(value, request):",
                "@pytest.mark.asyncio\nasync def test_case(value, request, resource):\n"
                "    await asyncio.sleep(0)",
            )
        )
    elif workload == "fixtures":
        files["test_workload.py"] = (
            files["test_workload.py"]
            .replace(
                "@pytest.mark.parametrize(",
                "@pytest.fixture(scope='session')\ndef session_value():\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('session.setup')\n"
                "    yield 10\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('session.teardown')\n\n"
                "@pytest.fixture(scope='module')\ndef module_value(session_value):\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('module.setup')\n"
                "    yield session_value + 1\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('module.teardown')\n\n"
                "@pytest.fixture\ndef resource(module_value, request):\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('function.setup', request.node.nodeid)\n"
                "    yield module_value + 1\n"
                "    if _lifecycle_stream is not None:\n"
                "        _record_lifecycle('function.teardown', request.node.nodeid)\n\n"
                "@pytest.mark.parametrize(",
            )
            .replace(
                "def test_case(value, request):",
                "def test_case(value, request, resource):\n    assert resource == 12",
            )
        )
    elif workload == "capture":
        files["test_workload.py"] = files["test_workload.py"].replace(
            "def test_case(value, request):",
            "def test_case(value, request, capfd):\n"
            "    os.write(1, b'x' * 32768)\n    os.write(2, b'y' * 32768)\n"
            "    captured = capfd.readouterr()\n"
            "    assert captured.out == 'x' * 32768\n"
            "    assert captured.err == 'y' * 32768",
        )
    elif workload != "sync":
        raise ValueError(f"unknown benchmark workload: {workload}")
    for file_index in range(extra_source_files):
        files[f"src/extra_{file_index:04d}.py"] = "\n".join(
            f"def unexecuted_{function_index:04d}(value):\n"
            "    if value % 2:\n"
            f"        return value + {file_index + function_index + 1}\n"
            f"    return value - {file_index + function_index + 1}\n"
            for function_index in range(functions_per_extra_source)
        )
    digest = hashlib.sha256()
    for relative, contents in files.items():
        payload = contents.encode("utf-8")
        (root / relative).write_bytes(payload)
        digest.update(relative.encode("utf-8") + b"\0" + payload + b"\0")
    return digest.hexdigest()


def wheel_details(path: Path, expected_name: str) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"wheel is missing: {path}")
    with zipfile.ZipFile(path) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise RuntimeError(f"expected one wheel METADATA file in {path}")
        metadata = message_from_bytes(archive.read(metadata_paths[0]))
    name = metadata.get("Name", "").lower().replace("_", "-")
    version = metadata.get("Version")
    if name != expected_name or not version:
        raise RuntimeError(f"unexpected {expected_name} wheel metadata in {path}")
    return {
        "path": str(path),
        "version": version,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def wheel_member_sha256(path: Path, member: str) -> str:
    with zipfile.ZipFile(path) as archive:
        try:
            contents = archive.read(member)
        except KeyError as error:
            raise RuntimeError(f"wheel {path} is missing required member {member!r}") from error
    return hashlib.sha256(contents).hexdigest()


def wheel_distribution_files(path: Path, distribution: str) -> dict[str, str]:
    """Hash the import and metadata files owned by one wheel distribution."""

    try:
        package_roots = _DISTRIBUTION_PACKAGE_ROOTS[distribution]
    except KeyError as error:
        raise ValueError(f"unsupported benchmark distribution: {distribution}") from error
    with zipfile.ZipFile(path) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise RuntimeError(f"expected one wheel METADATA file in {path}")
        metadata_root = metadata_paths[0].partition("/")[0]
        allowed_roots = {*package_roots, metadata_root}
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if not name.endswith(("/", ".pyc"))
            and name.partition("/")[0] in allowed_roots
            and not (
                name.partition("/")[0] == metadata_root
                and Path(name).name in _INSTALLER_GENERATED_METADATA
            )
        }


def verify_installed_distribution_files(
    owner: str,
    distribution: str,
    installed: object,
    wheel: Path,
) -> None:
    """Require installed import and metadata files to match the supplied wheel."""

    installed_files = installed.get(f"{distribution}_files") if is_object(installed) else None
    if not is_object(installed_files):
        raise RuntimeError(f"{owner} installed {distribution} file inventory is missing")
    if installed_files != wheel_distribution_files(wheel, distribution):
        raise RuntimeError(f"{owner} installed {distribution} files differ from supplied wheel")


def interpreter_details(python: Path, cwd: Path, env: dict[str, str]) -> dict[str, object]:
    program = """import hashlib, importlib.metadata as m, json, platform, sys
import importlib
from pathlib import Path
import vfy._native as native
import _pytest.runner as runner
def version(name):
    try: return m.version(name)
    except m.PackageNotFoundError: return None
native_shell = importlib.import_module('vfy._pytest.native_pytest')
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None
def distribution_files(name, roots):
    distribution = m.distribution(name)
    files = distribution.files or ()
    metadata_roots = {str(f).split('/', 1)[0] for f in files
                      if str(f).split('/', 1)[0].endswith('.dist-info')}
    if len(metadata_roots) != 1:
        raise RuntimeError(f'unexpected metadata roots for {name}: {metadata_roots}')
    metadata_root = next(iter(metadata_roots))
    result = {}
    for root in (*roots, metadata_root):
        base = Path(distribution.locate_file(root))
        if base.is_file():
            candidates = (base,)
        elif base.is_dir():
            candidates = base.rglob('*')
        else:
            continue
        for path in candidates:
            if not path.is_file():
                continue
            relative = root if base.is_file() else f'{root}/{path.relative_to(base).as_posix()}'
            if (path.suffix == '.pyc'
                    or '__pycache__' in path.parts
                    or (root == metadata_root
                        and path.name in {'RECORD', 'INSTALLER', 'REQUESTED',
                                          'direct_url.json', 'uv_cache.json'})):
                continue
            result[relative] = sha(path)
    return result
print(json.dumps({
    'python': platform.python_version(), 'implementation': platform.python_implementation(),
    'prefix': sys.prefix, 'vfy': version('vfy'), 'pytest': version('pytest'),
    'dependencies': sorted((d.metadata['Name'].lower().replace('_', '-'), d.version)
                           for d in m.distributions()),
    'vfy_files': distribution_files('vfy', ('vfy',)),
    'pytest_files': distribution_files('pytest', ('_pytest', 'pytest', 'py.py')),
    'vfy_extension_sha256': sha(native.__file__),
    'pytest_runner_sha256': sha(runner.__file__),
    'vfy_extension_path': native.__file__,
    'native_profile': native.build_profile(),
    'native_shell_sha256': sha(getattr(native_shell, '__file__', None)),
}))
"""
    result = run_checked([str(python), "-c", program], cwd=cwd, env=env, timeout=30)
    value: object = json.loads(result.stdout)
    if not is_object(value):
        raise RuntimeError(f"invalid interpreter metadata from {python}")
    # Resolve the venv directories only; resolving the executable follows uv's
    # symlink to the shared base interpreter and loses the venv prefix.
    expected_prefix = python.parent.parent.resolve()
    if Path(str(value.get("prefix", ""))).resolve() != expected_prefix:
        raise RuntimeError(f"interpreter escaped the selected environment: {value}")
    extension = value.get("vfy_extension_path")
    if not isinstance(extension, str) or not Path(extension).resolve().is_relative_to(
        expected_prefix
    ):
        raise RuntimeError(f"native extension is outside the installed environment: {extension}")
    return value


def environment(home: Path) -> dict[str, str]:
    env = benchmark_environment({"HOME": str(home), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    env.pop(ORDER_PATH, None)
    env.pop(LIFECYCLE_PATH, None)
    return env


def executable_path(path: Path) -> Path:
    """Normalize the venv directory while retaining uv's Python executable symlink."""

    return path.parent.resolve() / path.name


def route_engine(
    role: str, stock_engine: Literal["pytest", "pytest-native"]
) -> Literal["pytest", "pytest-native"]:
    return "pytest" if role == "stock" and stock_engine == "pytest" else "pytest-native"


def command(
    python: Path,
    root: Path,
    report: Path,
    *,
    engine: Literal["pytest", "pytest-native"],
    workers: str | int = 1,
) -> list[str]:
    return [
        str(python),
        "-m",
        "vfy",
        "--isolated",
        "test",
        f"--workers={workers}",
        f"--engine={engine}",
        "--data-file",
        str(root / ".coverage"),
        "--history",
        str(root / ".vfy" / "test-history.json"),
        "--source",
        "src",
        "test_workload.py",
        "-q",
        "-p",
        "no:cacheprovider",
        "--cov=src",
        "--cov-branch",
        "--cov-context=test",
        f"--cov-report=json:{report}",
    ]


def outcomes_from_history(path: Path, expected: list[str]) -> list[tuple[str, str]]:
    root: object = json.loads(path.read_text(encoding="utf-8"))
    tests = root.get("tests") if is_object(root) else None
    if not is_object(tests):
        raise RuntimeError(f"invalid vfy test history: {path}")
    actual: list[tuple[str, str]] = []
    for nodeid, test in tests.items():
        observations = test.get("observations") if is_object(test) else None
        if not isinstance(observations, list):
            raise RuntimeError(f"invalid test history entry for {nodeid!r}")
        latest = observations[-1] if observations else None
        if not is_object(latest) or latest.get("attempt") != "primary":
            raise RuntimeError(f"missing primary result for {nodeid!r}")
        outcome = latest.get("outcome")
        if not isinstance(outcome, str):
            raise RuntimeError(f"missing outcome for {nodeid!r}")
        actual.append((nodeid, outcome))
    actual.sort()
    wanted = sorted((nodeid, "passed") for nodeid in expected)
    if actual != wanted:
        raise RuntimeError(f"vfy outcomes differ: got {len(actual)}, expected {len(wanted)}")
    return actual


def check_ordered_execution(path: Path, expected: list[str]) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError(f"ordered execution evidence is missing: {path}")
    payload = path.read_bytes()
    actual = payload.decode("utf-8").splitlines()
    if actual != expected:
        raise RuntimeError("vfy case execution order differs")
    return {"case_count": len(actual), "sha256": hashlib.sha256(payload).hexdigest()}


def normalize_report_filename(filename: str, root: Path) -> str:
    path = Path(filename)
    if not path.is_absolute():
        return filename
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return filename
    return "<ROOT>" if relative == "." else f"<ROOT>/{relative}"


def load_coverage_report(path: Path, root: Path) -> dict[str, object]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not is_object(payload):
        raise RuntimeError(f"coverage JSON is not an object: {path}")
    meta = payload.get("meta")
    files = payload.get("files")
    totals = payload.get("totals")
    if not is_object(meta) or not is_object(files) or not is_object(totals):
        raise RuntimeError(f"coverage JSON needs meta, files, and totals objects: {path}")
    timestamp = meta.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        raise RuntimeError(f"coverage JSON has no valid report timestamp: {path}")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp)
    except ValueError as error:
        raise RuntimeError(f"coverage JSON has no valid report timestamp: {path}") from error
    if parsed_timestamp.tzinfo is None:
        raise RuntimeError(f"coverage JSON report timestamp has no timezone: {path}")

    normalized_meta = dict(meta)
    normalized_meta["timestamp"] = "<report timestamp>"
    normalized_files: dict[str, object] = {}
    for filename, value in files.items():
        if not isinstance(filename, str) or not is_object(value):
            raise RuntimeError(f"coverage JSON has an invalid file entry: {path}")
        normalized_filename = normalize_report_filename(filename, root)
        if normalized_filename in normalized_files:
            raise RuntimeError(f"coverage JSON has duplicate normalized file paths: {path}")
        normalized_files[normalized_filename] = value

    for source in sorted((root / "src").glob("extra_*.py")):
        filename = normalize_report_filename(str(source.resolve()), root)
        value = normalized_files.get(filename)
        if not is_object(value):
            raise RuntimeError(f"coverage JSON omits an unexecuted source file: {source}")
        if value.get("executed_lines") != []:
            raise RuntimeError(
                f"coverage JSON reports executed lines in unimported source: {source}"
            )

    normalized = dict(payload)
    normalized["meta"] = normalized_meta
    normalized["files"] = normalized_files
    return normalized


def check_coverage_report_equal(actual: object, expected: object) -> None:
    try:
        actual_json = json.dumps(actual, sort_keys=True, separators=(",", ":"), allow_nan=False)
        expected_json = json.dumps(expected, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RuntimeError("coverage report contains non-JSON values") from error
    if actual_json != expected_json:
        raise RuntimeError("vfy coverage report fields differ")


def report_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def check_native_result(path: Path, expected: list[str]) -> None:
    record: object = json.loads(path.read_text(encoding="utf-8"))
    if not is_object(record) or record.get("engine") != NATIVE_ENGINE:
        raise RuntimeError(f"candidate did not write a native engine result: {path}")
    cases = record.get("cases")
    if not isinstance(cases, list) or len(cases) != len(expected):
        raise RuntimeError(f"native engine result has the wrong case count: {path}")
    for nodeid, case in zip(expected, cases, strict=True):
        if not is_object(case) or case.get("nodeid") != nodeid:
            raise RuntimeError(f"native engine case order differs at {nodeid!r}")
        phases = case.get("phases")
        if not isinstance(phases, list) or len(phases) != 3:
            raise RuntimeError(f"native engine phases are incomplete for {nodeid!r}")
        phase_names: list[str] = []
        for phase in phases:
            if not is_object(phase):
                raise RuntimeError(f"native engine has a malformed phase for {nodeid!r}")
            phase_name = phase.get("phase")
            if not isinstance(phase_name, str):
                raise RuntimeError(f"native engine has a malformed phase for {nodeid!r}")
            phase_names.append(phase_name)
            if phase.get("outcome") != "passed":
                raise RuntimeError(f"native engine phase failed for {nodeid!r}")
        if phase_names != ["setup", "call", "teardown"]:
            raise RuntimeError(f"native engine phases are out of order for {nodeid!r}")


def reset(root: Path, report: Path, native_result: Path | None) -> None:
    (root / ".coverage").unlink(missing_ok=True)
    (root / ".coverage-journal").unlink(missing_ok=True)
    history = root / ".vfy"
    if history.is_dir():
        shutil.rmtree(history)
    report.unlink(missing_ok=True)
    if native_result is not None:
        native_result.unlink(missing_ok=True)


def check_run(
    root: Path,
    report: Path,
    expected: list[str],
    *,
    coverage: object | None = None,
    report_data: object | None = None,
    ordered_file: Path,
    native_result: Path | None = None,
) -> dict[str, object]:
    data_path = root / ".coverage"
    if not data_path.is_file():
        raise RuntimeError(f"vfy did not write coverage data: {data_path}")
    actual_coverage = normalized_data(data_path, root)
    actual_report = load_coverage_report(report, root)
    actual_outcomes = outcomes_from_history(root / ".vfy" / "test-history.json", expected)
    if coverage is not None and actual_coverage != coverage:
        raise RuntimeError("schema-7 lines, arcs or contexts changed after parity")
    if report_data is not None:
        check_coverage_report_equal(actual_report, report_data)
    ordered = check_ordered_execution(ordered_file, expected)
    if native_result is not None:
        check_native_result(native_result, expected)
    return {
        "coverage": actual_coverage,
        "report": actual_report,
        "outcomes": actual_outcomes,
        "ordered": ordered,
    }


def expected_lifecycle_events(workload: str, expected: list[str]) -> list[str]:
    if workload == "fixtures":
        events = ["session.setup", "module.setup"]
        for nodeid in expected:
            events.extend((f"function.setup:{nodeid}", f"function.teardown:{nodeid}"))
        events.extend(("module.teardown", "session.teardown"))
        return events
    if workload == "async":
        return [
            event
            for nodeid in expected
            for event in (f"async.setup:{nodeid}", f"async.teardown:{nodeid}")
        ]
    if workload in ("sync", "capture"):
        return []
    raise ValueError(f"unknown benchmark workload: {workload}")


def check_lifecycle(path: Path, workload: str, expected: list[str]) -> dict[str, object]:
    actual = path.read_text(encoding="utf-8").splitlines()
    wanted = expected_lifecycle_events(workload, expected)
    if actual != wanted:
        mismatch = next(
            (
                index
                for index, (got, want) in enumerate(zip(actual, wanted, strict=False))
                if got != want
            ),
            min(len(actual), len(wanted)),
        )
        got = actual[mismatch] if mismatch < len(actual) else "<missing>"
        want = wanted[mismatch] if mismatch < len(wanted) else "<end>"
        raise RuntimeError(
            f"{workload} fixture lifecycle differs at event {mismatch}: "
            f"got {got!r}, expected {want!r} "
            f"(got {len(actual)} events, expected {len(wanted)})"
        )
    return {
        "event_count": len(actual),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def prepare_run_traces(
    evidence: Path,
    role: str,
    phase: str,
    pair_index: int,
    environment: dict[str, str],
) -> tuple[dict[str, str], Path, Path]:
    trace_root = evidence / "run-traces"
    trace_root.mkdir(exist_ok=True)
    stem = f"{role}-{phase}-{pair_index:02d}"
    ordered = trace_root / f"{stem}-ordered.txt"
    lifecycle = trace_root / f"{stem}-lifecycle.txt"
    paths = (
        ordered,
        lifecycle,
        *(trace_root / f"{stem}-{stream}.txt" for stream in ("stdout", "stderr")),
    )
    archives = tuple(path.with_suffix(path.suffix + ".gz") for path in paths)
    if any(path.exists() for path in (*paths, *archives)):
        raise FileExistsError(f"benchmark trace paths already exist for {stem}")
    lifecycle.touch()
    run_environment = environment.copy()
    run_environment[ORDER_PATH] = str(ordered)
    run_environment[LIFECYCLE_PATH] = str(lifecycle)
    return run_environment, ordered, lifecycle


def preserve_trace(path: Path, evidence: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError(f"benchmark trace is missing: {path}")
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    archive = path.with_suffix(path.suffix + ".gz")
    compressed = gzip.compress(payload, mtime=0)
    with archive.open("xb") as stream:
        stream.write(compressed)
    path.unlink()
    return {
        "path": archive.relative_to(evidence).as_posix(),
        "sha256": digest,
        "archive_sha256": hashlib.sha256(compressed).hexdigest(),
        "bytes": len(payload),
        "archive_bytes": len(compressed),
    }


def preserve_run_stream(
    stream_text: str,
    evidence: Path,
    role: str,
    phase: str,
    pair_index: int,
    stream: str,
) -> dict[str, object]:
    if stream not in {"stdout", "stderr"}:
        raise ValueError(f"unsupported process stream: {stream}")
    trace_root = evidence / "run-traces"
    raw_path = trace_root / f"{role}-{phase}-{pair_index:02d}-{stream}.txt"
    archive_path = raw_path.with_suffix(raw_path.suffix + ".gz")
    if raw_path.exists() or archive_path.exists():
        raise FileExistsError(f"benchmark {stream} artifact already exists: {raw_path}")
    with raw_path.open("xb") as destination:
        destination.write(stream_text.encode("utf-8"))
    return preserve_trace(raw_path, evidence)


def record_process_streams(
    validation: dict[str, object],
    stdout: str,
    stderr: str,
    evidence: Path,
    role: str,
    phase: str,
    pair_index: int,
    exit_status: int,
) -> None:
    if exit_status != 0:
        raise RuntimeError(f"cannot record successful benchmark streams with status {exit_status}")
    stdout_payload = stdout.encode("utf-8")
    stderr_payload = stderr.encode("utf-8")
    trace_root = evidence / "run-traces"
    stem = f"{role}-{phase}-{pair_index:02d}"
    stream_paths = tuple(trace_root / f"{stem}-{stream}.txt" for stream in ("stdout", "stderr"))
    stream_archives = tuple(path.with_suffix(path.suffix + ".gz") for path in stream_paths)
    if any(path.exists() for path in (*stream_paths, *stream_archives)):
        raise FileExistsError(f"benchmark process stream artifacts already exist for {stem}")
    stdout_trace = preserve_run_stream(stdout, evidence, role, phase, pair_index, "stdout")
    stderr_trace = preserve_run_stream(stderr, evidence, role, phase, pair_index, "stderr")
    validation.update(
        {
            "exit_status": exit_status,
            "stdout_sha256": hashlib.sha256(stdout_payload).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr_payload).hexdigest(),
            "stdout_bytes": len(stdout_payload),
            "stderr_bytes": len(stderr_payload),
            "stdout_trace": stdout_trace,
            "stderr_trace": stderr_trace,
        }
    )


def check_measured_run(
    role: str,
    phase: str,
    pair_index: int,
    root: Path,
    report: Path,
    expected: list[str],
    workload: str,
    ordered_file: Path,
    lifecycle_file: Path,
    evidence: Path,
    proof: dict[str, object],
) -> dict[str, object]:
    result = check_run(
        root,
        report,
        expected,
        coverage=proof["coverage"],
        report_data=proof["report"],
        ordered_file=ordered_file,
    )
    ordered = result["ordered"]
    if not is_object(ordered):
        raise RuntimeError(f"{role} {phase} ordered execution evidence is malformed")
    event_count = ordered.get("case_count")
    ordered_sha256 = ordered.get("sha256")
    if not isinstance(event_count, int) or not isinstance(ordered_sha256, str):
        raise RuntimeError(f"{role} {phase} ordered execution evidence is malformed")
    if ordered != proof["ordered"]:
        raise RuntimeError(f"{role} {phase} execution order differs from parity proof")
    lifecycle = check_lifecycle(lifecycle_file, workload, expected)
    if lifecycle != proof["lifecycle"]:
        raise RuntimeError(f"{role} {phase} fixture lifecycle differs from parity proof")
    ordered_trace = preserve_trace(ordered_file, evidence)
    lifecycle_trace = preserve_trace(lifecycle_file, evidence)
    return {
        "role": role,
        "phase": phase,
        "pair_index": pair_index,
        "case_count": len(expected),
        "report_path": report.relative_to(evidence).as_posix(),
        "outcomes_sha256": report_digest(result["outcomes"]),
        "coverage_sha256": report_digest(result["coverage"]),
        "report_sha256": report_digest(result["report"]),
        "ordered_event_count": event_count,
        "ordered_sha256": ordered_sha256,
        "lifecycle_event_count": lifecycle["event_count"],
        "lifecycle_sha256": lifecycle["sha256"],
        "ordered_trace": ordered_trace,
        "lifecycle_trace": lifecycle_trace,
    }


def append_validation_record(path: Path, record: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()


def run_parity(
    name: str,
    python: Path,
    root: Path,
    home: Path,
    evidence: Path,
    wheels: dict[str, Path],
    *,
    engine: Literal["pytest", "pytest-native"],
    workers: str | int,
    expected: list[str],
    workload: str,
    timeout: float,
) -> tuple[dict[str, object], dict[str, str], dict[str, object]]:
    env = environment(home)
    installed = interpreter_details(python, root, env)
    wheel_records: dict[str, dict[str, object]] = {}
    for distribution, wheel in wheels.items():
        detail = wheel_details(wheel, distribution)
        version_key = distribution
        if installed.get(version_key) != detail["version"]:
            raise RuntimeError(
                f"{name} has {distribution} {installed.get(version_key)}, "
                f"but the supplied wheel is {detail['version']}"
            )
        wheel_records[distribution] = dict(detail)
    for distribution, wheel in wheels.items():
        verify_installed_distribution_files(name, distribution, installed, wheel)
    extension_checks = {
        "vfy": [("vfy/_native.abi3.so", "vfy_extension_sha256")],
        "pytest": [("_pytest/runner.py", "pytest_runner_sha256")],
    }
    extension_checks["vfy"].append(("vfy/_pytest/native_pytest.py", "native_shell_sha256"))
    for distribution, checks in extension_checks.items():
        verified: list[dict[str, str]] = []
        for member, installed_key in checks:
            installed_sha = installed.get(installed_key)
            wheel_sha = wheel_member_sha256(wheels[distribution], member)
            if installed_sha != wheel_sha:
                raise RuntimeError(
                    f"{name} installed {member} bytes do not match supplied {distribution} wheel"
                )
            verified.append({"member": member, "sha256": wheel_sha})
        wheel_records[distribution]["verified_members"] = verified
    report = evidence / f"{name}-coverage.json"
    native_result = evidence / f"{name}-native-result.json" if engine == "pytest-native" else None
    native_stages = evidence / (
        "native-stages.json" if name == "candidate" else "stock-native-stages.json"
    )
    ordered = evidence / f"{name}-ordered.txt"
    lifecycle = evidence / f"{name}-lifecycle.txt"
    reset(root, report, native_result)
    ordered.unlink(missing_ok=True)
    lifecycle.write_text("", encoding="utf-8")
    run_env = {**env, ORDER_PATH: str(ordered), LIFECYCLE_PATH: str(lifecycle)}
    if native_result is not None:
        run_env["PYTEST_NATIVE_RESULT"] = str(native_result)
        run_env["PYTEST_NATIVE_STAGE_OUTPUT"] = str(native_stages)
    command_arguments = command(
        python,
        root,
        report,
        engine=engine,
        workers=workers,
    )
    result = run_checked(
        command_arguments,
        cwd=root,
        env=run_env,
        timeout=timeout,
    )
    proof = check_run(root, report, expected, ordered_file=ordered, native_result=native_result)
    lifecycle_proof = check_lifecycle(lifecycle, workload, expected)
    proof["lifecycle"] = lifecycle_proof
    return (
        proof,
        env,
        {
            "wheels": wheel_records,
            "installed": installed,
            "engine": engine,
            "workers": str(workers),
            "command": command_arguments,
            "ordered_event_sha256": hashlib.sha256(ordered.read_bytes()).hexdigest(),
            "lifecycle_trace": {"path": str(lifecycle), **lifecycle_proof},
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        },
    )


def peak_rss(
    python: Path,
    root: Path,
    report: Path,
    env: dict[str, str],
    expected: list[str],
    *,
    evidence: Path,
    role: str,
    pair_index: int,
    workload: str,
    timeout: float,
    proof: dict[str, object],
    engine: Literal["pytest", "pytest-native"],
    workers: str | int,
) -> tuple[int, dict[str, object]]:
    if platform.system() == "Darwin":
        reporter, pattern, multiplier, group = ["/usr/bin/time", "-l"], MACOS_RSS, 1, "bytes"
    elif platform.system() == "Linux" and Path("/usr/bin/time").is_file():
        reporter, pattern, multiplier, group = ["/usr/bin/time", "-v"], LINUX_RSS, 1024, "kb"
    else:
        raise RuntimeError(f"peak RSS measurement is unsupported on {platform.system()}")
    reset(root, report, None)
    measured_env, ordered, lifecycle = prepare_run_traces(evidence, role, "rss", pair_index, env)
    command_arguments = [
        *reporter,
        *command(python, root, report, engine=engine, workers=workers),
    ]
    result = run_captured(
        command_arguments,
        cwd=root,
        env=measured_env,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"RSS run failed ({result.returncode}):\n{result.stderr[-3000:]}")
    validation = check_measured_run(
        role,
        "rss",
        pair_index,
        root,
        report,
        expected,
        workload=workload,
        ordered_file=ordered,
        lifecycle_file=lifecycle,
        evidence=evidence,
        proof=proof,
    )
    validation["command"] = command_arguments
    record_process_streams(
        validation,
        result.stdout,
        result.stderr,
        evidence,
        role,
        "rss",
        pair_index,
        result.returncode,
    )
    match = pattern.search(result.stderr)
    if match is None:
        raise RuntimeError(f"resource reporter omitted peak RSS:\n{result.stderr[-3000:]}")
    return int(match.group(group)) * multiplier, validation


def verify_build_revision(source_root: Path, revision: str, *, retained: bool = False) -> None:
    if retained:
        # A retained wheel stays bound to its manifest even after its checkout advances.
        if git_value(source_root, "rev-parse", f"{revision}^{{commit}}") != revision:
            raise RuntimeError("retained build revision is not an exact source commit")
    else:
        require_revision(source_root, revision)


def verify_build_identity(
    manifest_path: Path, installed: object, supplied_wheel: Path, *, retained: bool = False
) -> dict[str, str | None]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not is_object(installed):
        raise RuntimeError("missing installed artifact metadata")
    extension_path = installed.get("vfy_extension_path")
    profile = installed.get("native_profile")
    if not isinstance(extension_path, str) or not isinstance(profile, str):
        raise RuntimeError("missing native extension identity")
    revision = manifest.get("source_revision")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise RuntimeError("build manifest has no complete source revision")
    verify_build_revision(Path(manifest["source_root"]), revision, retained=retained)
    verified = _verify_build_manifest(
        (manifest_path, manifest),
        extension=Path(extension_path),
        source_root=Path(manifest["source_root"]),
        source_revision=revision,
        profile=profile,
    )
    verify_manifest_wheel_hash(verified, supplied_wheel)
    return verified


def verify_manifest_wheel_hash(manifest: dict[str, str | None], supplied_wheel: Path) -> None:
    supplied_hash = hashlib.sha256(supplied_wheel.read_bytes()).hexdigest()
    if manifest.get("wheel_sha256") != supplied_hash:
        raise RuntimeError("build manifest wheel does not match the supplied vfy wheel")


def verify_wheel_parity(
    artifacts: dict[str, dict[str, object]], *, allow_vfy_difference: bool = False
) -> None:
    stock_wheels = artifacts["stock"].get("wheels")
    candidate_wheels = artifacts["candidate"].get("wheels")
    if not is_object(stock_wheels) or not is_object(candidate_wheels):
        raise RuntimeError("missing stock or candidate wheel identity")
    for distribution in ("vfy", "pytest"):
        stock_wheel = stock_wheels.get(distribution)
        candidate_wheel = candidate_wheels.get(distribution)
        if not is_object(stock_wheel) or not is_object(candidate_wheel):
            raise RuntimeError(f"missing {distribution} wheel identity")
        if distribution == "vfy" and allow_vfy_difference:
            continue
        if stock_wheel.get("sha256") != candidate_wheel.get("sha256"):
            raise RuntimeError(f"stock and candidate {distribution} wheel bytes differ")


def _locked_dependency_inventory(
    dependency_lock: Path,
) -> tuple[set[tuple[str, str]], set[str]]:
    root_editables: set[str] = set()
    versions: set[tuple[str, str]] = set()
    lock = tomllib.loads(dependency_lock.read_text(encoding="utf-8"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise RuntimeError("uv.lock has no package inventory")
    for package in packages:
        if not isinstance(package, dict):
            raise RuntimeError("uv.lock contains a malformed package entry")
        name = package.get("name")
        if not isinstance(name, str) or not name:
            raise RuntimeError("uv.lock contains a package without a valid name")
        normalized_name = name.lower().replace("_", "-")
        version = package.get("version")
        if version is None:
            if normalized_name == "vfy" and package.get("source") == {"editable": "."}:
                if normalized_name in root_editables:
                    raise RuntimeError("uv.lock contains duplicate root vfy entries")
                root_editables.add(normalized_name)
                continue
            raise RuntimeError(f"uv.lock package {name!r} has no version")
        if not isinstance(version, str) or not version:
            raise RuntimeError(f"uv.lock package {name!r} has an invalid version")
        versions.add((normalized_name, version))
    return versions, root_editables


def _verify_vfy_artifact_versions(
    artifacts: dict[str, dict[str, object]],
    environments: dict[str, dict[str, object]],
) -> None:
    for role, record, installed in (
        ("stock", artifacts["stock"], environments["stock"]),
        ("candidate", artifacts["candidate"], environments["candidate"]),
    ):
        wheels = record.get("wheels")
        build = record.get("build")
        if not is_object(wheels) or not is_object(build):
            raise RuntimeError(f"{role} vfy artifact provenance is missing")
        vfy_wheel = wheels.get("vfy")
        if not is_object(vfy_wheel):
            raise RuntimeError(f"{role} vfy wheel identity is missing")
        installed_version = installed.get("vfy")
        wheel_version = vfy_wheel.get("version")
        wheel_hash = vfy_wheel.get("sha256")
        if not isinstance(installed_version, str) or installed_version != wheel_version:
            raise RuntimeError(f"{role} installed vfy version differs from its wheel")
        if not isinstance(wheel_hash, str) or build.get("wheel_sha256") != wheel_hash:
            raise RuntimeError(f"{role} vfy build manifest differs from its wheel")


def verify_environment_parity(
    artifacts: dict[str, dict[str, object]], dependency_lock: Path
) -> None:
    stock = artifacts["stock"]["installed"]
    candidate = artifacts["candidate"]["installed"]
    if not is_object(stock) or not is_object(candidate):
        raise RuntimeError("missing installed environment identity")
    for field in ("python", "implementation", "dependencies"):
        if stock[field] != candidate[field]:
            raise RuntimeError(f"stock and candidate {field} differ")

    # The root project is editable in uv.lock and intentionally has no version.
    # This exception is safe only after run_parity has matched each installed
    # vfy distribution to its wheel and verify_build_identity has bound that
    # wheel to the release build manifest.
    versions, root_editables = _locked_dependency_inventory(dependency_lock)
    _verify_vfy_artifact_versions(artifacts, {"stock": stock, "candidate": candidate})

    dependencies = stock["dependencies"]
    if not isinstance(dependencies, list):
        raise RuntimeError("missing installed dependency inventory")
    for dependency in dependencies:
        if (
            not isinstance(dependency, list)
            or len(dependency) != 2
            or not all(isinstance(value, str) and value for value in dependency)
        ):
            raise RuntimeError(f"installed dependency entry is malformed: {dependency}")
        name = dependency[0].lower().replace("_", "-")
        version = dependency[1]
        if (name, version) not in versions and not (
            name in root_editables and version == stock.get(name)
        ):
            raise RuntimeError(f"installed dependency is absent from uv.lock: {dependency}")


def verify_parity_equal(parity: dict[str, dict[str, object]]) -> None:
    for field in ("coverage", "report", "outcomes", "lifecycle"):
        if field == "report":
            check_coverage_report_equal(parity["stock"][field], parity["candidate"][field])
        elif parity["stock"][field] != parity["candidate"][field]:
            raise RuntimeError(f"stock and candidate vfy {field} differ")


def verify_artifact_stability(
    tools: dict[str, Path],
    roots: dict[str, Path],
    envs: dict[str, dict[str, str]],
    artifact_records: dict[str, dict[str, object]],
    manifests: dict[str, Path],
    *,
    retained_stock: bool = False,
) -> None:
    for name, python in tools.items():
        installed = interpreter_details(python, roots[name], envs[name])
        if installed != artifact_records[name]["installed"]:
            raise RuntimeError(f"{name} installed artifacts changed during the benchmark")
        wheels = artifact_records[name].get("wheels")
        vfy = wheels.get("vfy") if is_object(wheels) else None
        wheel_path = vfy.get("path") if is_object(vfy) else None
        if not isinstance(wheel_path, str):
            raise RuntimeError(f"{name} supplied vfy wheel identity is missing")
        manifest = verify_build_identity(
            manifests[name],
            installed,
            Path(wheel_path),
            retained=retained_stock and name == "stock",
        )
        if manifest != artifact_records[name]["build"]:
            raise RuntimeError(f"{name} build provenance changed during the benchmark")


def timed_measurement(
    name: str,
    phase: str,
    pair_index: int,
    *,
    python: Path,
    root: Path,
    env: dict[str, str],
    expected: list[str],
    workload: str,
    evidence: Path,
    proof: dict[str, object],
    timeout: float,
    engine: Literal["pytest", "pytest-native"],
    workers: str | int,
) -> tuple[float, dict[str, object]]:
    report = evidence / f"{name}-{phase}-{pair_index:02d}-coverage.json"
    reset(root, report, None)
    run_env, ordered, lifecycle = prepare_run_traces(evidence, name, phase, pair_index, env)
    command_arguments = command(
        python,
        root,
        report,
        engine=engine,
        workers=workers,
    )
    elapsed, result = timed_process(
        command_arguments,
        cwd=root,
        env=run_env,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"{name} {phase} run failed:\n{result.stderr[-3000:]}")
    validation = check_measured_run(
        name,
        phase,
        pair_index,
        root,
        report,
        expected,
        workload,
        ordered,
        lifecycle,
        evidence,
        proof,
    )
    validation["command"] = command_arguments
    validation["elapsed_ms"] = elapsed
    record_process_streams(
        validation,
        result.stdout,
        result.stderr,
        evidence,
        name,
        phase,
        pair_index,
        result.returncode,
    )
    return elapsed, validation


def validate_extra_source_arguments(
    parser: argparse.ArgumentParser,
    extra_source_files: int,
    functions_per_extra_source: int,
) -> None:
    """Reject invalid source workload sizes before creating benchmark artifacts."""
    if extra_source_files < 0:
        parser.error("--extra-source-files must be non-negative")
    if functions_per_extra_source < 1:
        parser.error("--functions-per-extra-source must be positive")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-python", required=True, type=Path)
    parser.add_argument("--candidate-python", required=True, type=Path)
    parser.add_argument("--stock-wheel", required=True, type=Path, help="installed vfy wheel")
    parser.add_argument("--candidate-wheel", required=True, type=Path, help="installed vfy wheel")
    parser.add_argument("--stock-pytest-wheel", required=True, type=Path)
    parser.add_argument("--candidate-pytest-wheel", required=True, type=Path)
    parser.add_argument("--stock-build-manifest", required=True, type=Path)
    parser.add_argument("--candidate-build-manifest", required=True, type=Path)
    parser.add_argument(
        "--stock-engine",
        choices=("pytest", "native"),
        default="pytest",
        help="engine used by the stock wheel (default: pytest)",
    )
    parser.add_argument(
        "--workers",
        default="1",
        help="worker count or auto/logical, shared by both runs (default: 1)",
    )
    parser.add_argument("--cases", type=int, choices=(1, 10, 1_000, 10_000, 100_000), default=CASES)
    parser.add_argument(
        "--workload", choices=("sync", "async", "fixtures", "capture"), default="sync"
    )
    parser.add_argument(
        "--extra-source-files",
        type=int,
        default=0,
        help="unimported source files included in complete coverage reports (default: 0)",
    )
    parser.add_argument(
        "--functions-per-extra-source",
        type=int,
        default=32,
        help="branching functions in each extra source file (default: 32)",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--validate-only", action="store_true")
    add_sampling_arguments(parser)
    args = parser.parse_args()
    validate_extra_source_arguments(
        parser, args.extra_source_files, args.functions_per_extra_source
    )
    if args.workers not in {"auto", "logical"}:
        try:
            if int(args.workers) < 1:
                raise ValueError
        except ValueError:
            parser.error("--workers must be auto, logical, or a positive integer")
    sampling = sampling_config(args)
    source = require_clean(Path(__file__).resolve().parents[2])
    output = args.output.resolve()
    evidence = output.with_suffix(output.suffix + ".evidence")
    if output.exists() or evidence.exists():
        raise FileExistsError(f"output or evidence path already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence.mkdir()
    roots = {name: evidence / f"project-{name}" for name in ("stock", "candidate")}
    homes = {name: evidence / f"home-{name}" for name in roots}
    for home in homes.values():
        home.mkdir()
    hashes = {
        name: create_project(
            root,
            args.cases,
            args.workload,
            extra_source_files=args.extra_source_files,
            functions_per_extra_source=args.functions_per_extra_source,
        )
        for name, root in roots.items()
    }
    if hashes["stock"] != hashes["candidate"]:
        raise RuntimeError("stock and candidate workloads are not byte-identical")
    expected = case_ids(args.cases)
    tools = {
        "stock": executable_path(args.stock_python),
        "candidate": executable_path(args.candidate_python),
    }
    manifests = {name: getattr(args, f"{name}_build_manifest").resolve() for name in tools}
    wheels = {
        "stock": {
            "vfy": args.stock_wheel.resolve(),
            "pytest": args.stock_pytest_wheel.resolve(),
        },
        "candidate": {
            "vfy": args.candidate_wheel.resolve(),
            "pytest": args.candidate_pytest_wheel.resolve(),
        },
    }
    parity: dict[str, dict[str, object]] = {}
    envs: dict[str, dict[str, str]] = {}
    artifact_records: dict[str, dict[str, object]] = {}
    for name in tools:
        engine = route_engine(name, args.stock_engine)
        proof, env, artifacts = run_parity(
            name,
            tools[name],
            roots[name],
            homes[name],
            evidence,
            wheels[name],
            engine=engine,
            workers=args.workers,
            expected=expected,
            workload=args.workload,
            timeout=args.timeout,
        )
        artifacts["build"] = verify_build_identity(
            manifests[name],
            artifacts["installed"],
            wheels[name]["vfy"],
            retained=args.stock_engine == "pytest-native" and name == "stock",
        )
        parity[name], envs[name], artifact_records[name] = proof, env, artifacts
    verify_wheel_parity(
        artifact_records,
        allow_vfy_difference=args.stock_engine == "pytest-native",
    )
    dependency_lock = evidence / "uv.lock"
    shutil.copyfile(Path(__file__).resolve().parents[2] / "uv.lock", dependency_lock)
    verify_environment_parity(artifact_records, dependency_lock)
    verify_parity_equal(parity)
    if args.validate_only:
        sys.stdout.write(f"{evidence}\n")
        return
    rss: dict[str, list[int]] = {name: [] for name in tools}
    validation_log = evidence / "measurement-validation.jsonl"
    validation_log.touch()
    validation_records: list[dict[str, object]] = []
    for pair_index, order in enumerate(rotated_orders(tuple(tools), sampling.runs), start=1):
        for name in order:
            report = evidence / f"{name}-rss-{pair_index:02d}-coverage.json"
            rss_bytes, validation = peak_rss(
                tools[name],
                roots[name],
                report,
                envs[name],
                expected,
                evidence=evidence,
                role=name,
                pair_index=pair_index,
                workload=args.workload,
                timeout=args.timeout,
                proof=parity[name],
                engine=route_engine(name, args.stock_engine),
                workers=args.workers,
            )
            validation["peak_rss_bytes"] = rss_bytes
            append_validation_record(validation_log, validation)
            validation_records.append(validation)
            rss[name].append(rss_bytes)
    samples: dict[str, list[float]] = {name: [] for name in tools}
    for phase, count in (("warmup", sampling.warmups), ("sample", sampling.runs)):
        for pair_index, order in enumerate(rotated_orders(tuple(tools), count), start=1):
            for name in order:
                elapsed, validation = timed_measurement(
                    name,
                    phase,
                    pair_index,
                    python=tools[name],
                    root=roots[name],
                    env=envs[name],
                    expected=expected,
                    workload=args.workload,
                    evidence=evidence,
                    proof=parity[name],
                    timeout=args.timeout,
                    engine=route_engine(name, args.stock_engine),
                    workers=args.workers,
                )
                append_validation_record(validation_log, validation)
                validation_records.append(validation)
                if phase == "sample":
                    samples[name].append(elapsed)
            sys.stderr.write(f"{phase}: {pair_index}/{count} pairs\n")
    record = {
        "benchmark": "vfy-first-party-pytest-coverage-bridge",
        "diagnostic_stages": json.loads(
            (evidence / "native-stages.json").read_text(encoding="utf-8")
        ),
        "dependency_lock_sha256": hashlib.sha256(dependency_lock.read_bytes()).hexdigest(),
        "schema_version": 1,
        "source_revision": git_value(Path(__file__).resolve().parents[2], "rev-parse", "HEAD"),
        "platform": platform.platform(),
        "hardware": hardware_details(),
        "sampling": sampling.as_dict(),
        "cases": args.cases,
        "workload": args.workload,
        "extra_source_files": args.extra_source_files,
        "functions_per_extra_source": args.functions_per_extra_source,
        "workload_sha256": hashes["stock"],
        "ordered_outcomes_equal": True,
        "schema7_lines_arcs_contexts_equal": True,
        "coverage_reports_equal": True,
        "outcomes_sha256": hashlib.sha256(
            json.dumps(parity["stock"]["outcomes"], separators=(",", ":")).encode()
        ).hexdigest(),
        "coverage_sha256": hashlib.sha256(
            json.dumps(parity["stock"]["coverage"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "coverage_report_sha256": report_digest(parity["stock"]["report"]),
        "tools": artifact_records,
        "timing": {name: summarize(values) for name, values in samples.items()},
        "rss_measurement": "time utility maximum RSS; not summed concurrent process memory",
        "peak_rss_bytes": {
            name: {"samples": values, "median": statistics.median(values)}
            for name, values in rss.items()
        },
        "measurement_validation": {
            "ledger_path": validation_log.relative_to(evidence).as_posix(),
            "run_count": len(validation_records),
            "runs": validation_records,
            "instrumentation": {
                "enabled_for": ["rss", "warmup", "sample"],
                "ordered_trace": "the generated session fixture records every executed node ID",
                "lifecycle_trace": "the generated fixtures record fixture setup and teardown",
                "equal_for_stock_and_candidate": True,
                "included_in_child_measurement": True,
                "process_wall_time_includes_trace_writes": True,
                "process_wall_time_includes_captured_output": True,
                "peak_rss_includes_trace_state": True,
                "post_run_validation_and_gzip_archiving_excluded": True,
                "scope": (
                    "Child wall time and peak RSS include the same node-order and "
                    "fixture-lifecycle writes for both engines. Timed wall time also "
                    "includes captured stdout/stderr. "
                    "Post-run parity checks, hashing, and gzip archiving happen after each child "
                    "measurement."
                ),
            },
        },
        "route_arguments": {
            name: {
                "engine": artifact_records[name]["engine"],
                "workers": artifact_records[name]["workers"],
                "command": artifact_records[name]["command"],
            }
            for name in tools
        },
        "candidate_arguments": ["--engine=pytest-native", f"--workers={args.workers}"],
        "evidence_directory": str(evidence),
    }
    require_clean(Path(__file__).resolve().parents[2])
    verify_artifact_stability(
        tools,
        roots,
        envs,
        artifact_records,
        manifests,
        retained_stock=args.stock_engine == "pytest-native",
    )
    require_revision(Path(__file__).resolve().parents[2], source.revision)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(f"{output}\n")


if __name__ == "__main__":
    main()
