"""Run pinned real-project comparisons through stock pytest and ``vfy test``.

The matrix is an untimed adoption check. It retains each route's command,
transcript, ordered execution records, warnings, JUnit report, and (when the
outcome gate passes) vfy coverage data and normalized JSON report.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import logging
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from coverage import CoverageData

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
logger = logging.getLogger(__name__)
SOURCE_INPUT_SHA256 = {
    name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    for name in (
        "validation/pytest_runner/check_native_adoption_matrix.py",
        "validation/pytest_runner/adoption_observations.py",
        "validation/pytest_runner/check_native_project.py",
        "validation/pytest_runner/patches/rich-deterministic-workload.patch",
        "benchmarks/project_manifest.py",
        "benchmarks/benchmark_projects.py",
        "scripts/process_tree.py",
        "scripts/pytest_execution_record.py",
        "scripts/pytest_execution_observations.py",
        "scripts/pytest_execution_probe.py",
    )
}

from benchmarks.project_manifest import ProjectManifest, manifest  # noqa: E402
from scripts.process_tree import run_captured  # noqa: E402
from scripts.pytest_execution_observations import TextNormalizer  # noqa: E402
from scripts.pytest_execution_record import (  # noqa: E402
    controlled_environment,
    file_sha256,
    interpreter_metadata,
    native_cases,
    native_session_warnings,
    resolved_environment,
    validate_native_identity,
    validate_native_imports,
    validate_pytest_origin,
    verify_installed_artifacts,
    write_atomic,
)
from validation.pytest_runner.adoption_observations import (  # noqa: E402
    RICH_REVISION,
    SORTEDCONTAINERS_REVISION,
    normalize_adoption_observations,
)
from validation.pytest_runner.check_native_project import coverage_reference  # noqa: E402


@dataclass(frozen=True)
class Project:
    key: str
    repository: str
    revision: str
    source_path: str
    tests: tuple[str, ...]
    coverage_args: tuple[str, ...]
    plugins: tuple[str, ...] = ()
    stock_plugins: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    stock_extra_args: tuple[str, ...] = ()
    outcome_args: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    dependency_lock: Path | None = None
    setup_args: tuple[str, ...] = ()
    generated_files: tuple[str, ...] = ()
    adoption_class: str = "zero-config"
    scope: str = "full pytest suite"


SIMPLE_PROJECTS = (
    Project(
        "iniconfig",
        "pytest-dev/iniconfig",
        "00e7d87c7353b1ffecc4cd55f19acfffedd5233e",
        "src",
        ("testing",),
        ("--cov=iniconfig",),
    ),
    Project(
        "pluggy",
        "pytest-dev/pluggy",
        "6abdcf6ca167f5d61297b5d86e22f3dd058031b3",
        "src",
        ("testing",),
        ("--cov=pluggy",),
    ),
    Project(
        "packaging",
        "pypa/packaging",
        "7b898d9f0b343ca06993157fc328d7caad51d5c2",
        "src",
        ("tests",),
        ("--cov=packaging", "--cov-branch"),
    ),
    Project(
        "attrs",
        "python-attrs/attrs",
        "8f767776326faaed11e6c2974798787f6e19b343",
        "src",
        ("tests",),
        ("--cov=attrs",),
        setup_args=(".",),
    ),
    Project(
        "colorama",
        "tartley/colorama",
        "841634ed2a0da5d5ac2d867db533da8131266cb2",
        ".",
        ("colorama/tests/*_test.py",),
        ("--cov=colorama",),
        adoption_class="small-config",
        scope="pytest-discoverable unittest cases under colorama/",
    ),
    Project(
        "platformdirs",
        "tox-dev/platformdirs",
        "ea7be87d58a05c5e0ac27b6e56aee6a5583a951e",
        "src",
        ("tests",),
        ("--cov=platformdirs",),
        plugins=("pytest_mock", "platformdirs.pytest_plugin"),
        setup_args=(".",),
        generated_files=("src/platformdirs/version.py",),
        adoption_class="small-config",
    ),
    Project(
        "python-dotenv",
        "theskumar/python-dotenv",
        "a00cb2eed0704cd6d2071b2004c37e95ccc86ee5",
        "src",
        ("tests",),
        ("--cov=dotenv",),
    ),
    Project(
        "typing_extensions",
        "python/typing_extensions",
        "3ae9b7553a5c560dc09b484078d98ece2985eecd",
        "src",
        ("src",),
        ("--cov=typing_extensions",),
        adoption_class="small-config",
        scope="pytest-discoverable unittest cases under src/",
    ),
    Project(
        "sortedcontainers",
        "grantjenks/python-sortedcontainers",
        "3ac358631f58c1347f1d6d2d92784117db0f38ed",
        "src",
        (),
        ("--cov=sortedcontainers",),
        scope="configured full suite including doctests",
    ),
    Project(
        "python-dateutil",
        "dateutil/dateutil",
        "2642afacc33fb839c404b75b230fff58b79793f2",
        "src",
        (),
        ("--cov=dateutil",),
        stock_plugins=("pytest_cov.plugin",),
        setup_args=(".",),
        generated_files=(
            "src/dateutil/zoneinfo/dateutil-zoneinfo.tar.gz",
            "src/dateutil/_version.py",
        ),
        scope="configured full suite, including the exercise solution",
    ),
    Project(
        "click",
        "pallets/click",
        "8b19813f2bfca99f1018a587a8cf54fc959f2e5d",
        "src",
        ("tests",),
        ("--cov=click", "--cov-branch"),
        scope="configured pytest suite; upstream defaults deselect stress tests",
    ),
    Project(
        "wcwidth",
        "jquast/wcwidth",
        "074bc286335d33de6bb475cf3bab82eacd946c75",
        ".",
        ("tests", "wcwidth"),
        ("--cov=wcwidth", "--cov-branch"),
        stock_plugins=("pytest_cov.plugin",),
        outcome_args=("--no-cov",),
        dependency_lock=ROOT / "validation/pytest_runner/wcwidth-requirements.txt",
        scope="full upstream tests and module doctests with the compiled C extension",
    ),
    Project(
        "poetry-core",
        "python-poetry/poetry-core",
        "b52c25c451bbaed9f309d45fc6e6e35c704e55bf",
        "src",
        (
            "tests/constraints",
            "tests/json",
            "tests/packages",
            "tests/pyproject",
            "tests/spdx",
            "tests/utils",
            "tests/vcs",
            "tests/version",
            "tests/test_core_version.py",
            "tests/test_factory.py",
        ),
        ("--cov=poetry.core",),
        plugins=("pytest_mock",),
        adoption_class="small-config",
        scope="complete non-masonry unit suites: constraints, json, packages, pyproject, spdx, "
        "utils, vcs, version, test_core_version.py and test_factory.py; "
        "build and integration suites are outside this matrix run",
    ),
    Project(
        "pyproject-api",
        "tox-dev/pyproject-api",
        "651b63254b5558399909cf3e041d5d4c71b6c802",
        "src",
        ("tests",),
        ("--cov=pyproject_api", "--cov-config=/dev/null"),
        plugins=("pytest_mock",),
        setup_args=(".",),
        generated_files=("src/pyproject_api/_version.py",),
        adoption_class="small-config",
        scope="full pytest suite; coverage configuration disables upstream covdefaults settings",
    ),
)


def benchmark_project(key: str) -> Project:
    source: ProjectManifest = manifest(key)
    coverage_args = source.coverage_args
    if key == "rich":
        coverage_args = (*coverage_args, "--cov-branch", "--cov-context=test")
    extra_args = source.serial_args
    plugins = source.plugins
    stock_plugins: tuple[str, ...] = ()
    stock_extra_args: tuple[str, ...] = ()
    setup_args: tuple[str, ...] = ()
    generated_files: tuple[str, ...] = ()
    if key == "black":
        # Match Black's tox setup, which installs the editable project before
        # tests and relies on xdist only for the stock pytest command. vfy's
        # own worker setting replaces xdist for the native route.
        plugins = tuple(plugin for plugin in source.plugins if plugin != "xdist.plugin")
        stock_plugins = ("xdist.plugin",)
        if "-n" not in extra_args and "--numprocesses" not in extra_args:
            stock_extra_args = ("-n", "0")
        setup_args = ("--editable", ".[d]")
        generated_files = ("src/_black_version.py",)
    return Project(
        key=key,
        repository=source.repository,
        revision=source.revision,
        source_path=Path(source.package_path).parent.as_posix() or ".",
        tests=source.tests,
        coverage_args=coverage_args,
        plugins=plugins,
        stock_plugins=stock_plugins,
        extra_args=extra_args,
        stock_extra_args=stock_extra_args,
        environment=source.environment,
        dependency_lock=ROOT / source.dependency_lock,
        setup_args=setup_args,
        generated_files=generated_files,
        adoption_class="small-config" if source.plugins or source.environment else "zero-config",
        scope=source.scope,
    )


PROJECTS_BY_KEY = {project.key: project for project in SIMPLE_PROJECTS}
PROJECTS_BY_KEY.update(
    {key: benchmark_project(key) for key in ("rich", "fastapi", "black", "requests")}
)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def checkout(project: Project, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--filter=blob:none",
            f"https://github.com/{project.repository}.git",
            str(destination),
        ],
        check=True,
        timeout=600,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(destination), "checkout", "--detach", project.revision],
        check=True,
        timeout=120,
        stdout=subprocess.DEVNULL,
    )
    if git(destination, "rev-parse", "HEAD") != project.revision:
        raise RuntimeError(f"{project.key}: checkout did not resolve to its pinned revision")
    if git(destination, "status", "--porcelain"):
        raise RuntimeError(f"{project.key}: upstream checkout is dirty before the run")
    return destination


def prepare_python(
    project: Project,
    work_root: Path,
    output_root: Path,
    wheel: Path,
    python_version: str,
) -> Path:
    environment = work_root / "environment"
    interpreter = environment / "bin" / "python"
    environment.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "venv", "--python", python_version, str(environment)], check=True, timeout=300
    )
    lock = (
        project.dependency_lock
        or ROOT / "validation/pytest_runner/native-adoption-requirements.txt"
    )
    subprocess.run(
        ["uv", "pip", "sync", "--python", str(interpreter), "--require-hashes", str(lock)],
        check=True,
        timeout=1800,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(interpreter),
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        ],
        check=True,
        timeout=600,
    )
    installed = verify_installed_artifacts(interpreter, [wheel], "vfy")
    actual = resolved_environment(interpreter)
    write_atomic(
        output_root / "environments" / f"{project.key}-environment.json",
        {
            "installed_wheels": installed,
            "resolved_environment": actual,
            "dependency_lock": str(lock.resolve()),
            "dependency_lock_sha256": file_sha256(lock),
        },
    )
    version = subprocess.check_output(
        [str(interpreter), "-c", "import pytest; print(pytest.__version__)"], text=True
    ).strip()
    if version != "9.1.1":
        raise RuntimeError(f"{project.key}: expected pytest 9.1.1, found {version}")
    # Preserve the venv executable path; resolving its symlink selects the base
    # interpreter and bypasses the installed test dependencies.
    return interpreter.absolute()


def project_environment(project: Project, root: Path, python: Path) -> dict[str, str]:
    env = controlled_environment()
    scripts_path = str((ROOT / "scripts").resolve())
    path_parts = [str((root / project.source_path).resolve()), scripts_path]
    configured = dict(project.environment).get("PYTHONPATH")
    if configured:
        path_parts.extend(
            str((root / value).resolve()) for value in configured.split(os.pathsep) if value
        )
    env["PYTHONPATH"] = os.pathsep.join(path_parts)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONNOUSERSITE"] = "1"
    env.update({key: value for key, value in project.environment if key != "PYTHONPATH"})
    for name in tuple(env):
        if name.startswith(("VFY_", "PYTEST_NATIVE_", "COVERAGE_")):
            env.pop(name, None)
    env["PATH"] = f"{python.parent}{os.pathsep}{env.get('PATH', '')}"
    return env


DATEUTIL_ZONEINFO_SHA256 = "d3ea52e7b6e968de0d884df1288193596fa95b803db4f92a18279a7398004475"


def prepare_dateutil_zoneinfo(root: Path, python: Path, output: Path) -> None:
    """Validate the locked wheel's timezone fixture before copying it into source."""
    frozen = python.parent.parent / "dateutil-zoneinfo-input.tar.gz"
    provenance_path = frozen.with_suffix(".json")
    if frozen.exists():
        provenance = json.loads(provenance_path.read_text())
        source = frozen
    else:
        provenance = json.loads(
            subprocess.check_output(
                [
                    str(python),
                    "-c",
                    "import importlib.metadata, json; "
                    "d = importlib.metadata.distribution('python-dateutil'); "
                    "print(json.dumps({'distribution': d.metadata['Name'], "
                    "'version': d.version, 'path': str(d.locate_file("
                    "'dateutil/zoneinfo/dateutil-zoneinfo.tar.gz'))}))",
                ],
                text=True,
                timeout=60,
            )
        )
        source = Path(provenance["path"])
    if provenance["distribution"] != "python-dateutil" or provenance["version"] != "2.9.0.post0":
        raise RuntimeError("Dateutil timezone fixture requires the locked python-dateutil wheel")
    contents = source.read_bytes()
    digest = hashlib.sha256(contents).hexdigest()
    if digest != DATEUTIL_ZONEINFO_SHA256:
        raise RuntimeError("Dateutil timezone fixture archive SHA256 differs from the locked input")
    with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as archive:
        metadata_file = archive.extractfile("METADATA")
        if metadata_file is None:
            raise RuntimeError("Dateutil timezone fixture archive has no METADATA")
        with metadata_file:
            metadata = json.load(metadata_file)
        member_count = len(archive.getmembers())
    expected = json.loads((root / "zonefile_metadata.json").read_text())
    if metadata != expected:
        raise RuntimeError("Dateutil timezone fixture METADATA differs from the pinned checkout")
    provenance.update(sha256=digest, metadata=metadata, members=member_count)
    if not frozen.exists():
        # Source installation replaces the locked distribution; retain its original input.
        frozen.write_bytes(contents)
        write_atomic(provenance_path, provenance)
    destination = root / "src/dateutil/zoneinfo/dateutil-zoneinfo.tar.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(contents)
    output.mkdir(parents=True, exist_ok=True)
    (output / "zoneinfo-input.tar.gz").write_bytes(contents)
    write_atomic(output / "zoneinfo-input.json", provenance)


def prepare_wcwidth_extension(
    project: Project, root: Path, python: Path, output: Path, timeout: float
) -> None:
    """Build and verify the pinned source extension before taking the snapshot."""
    if sys.platform != "linux":
        raise RuntimeError("the compiled wcwidth workload currently requires Linux and GCC")
    environment = project_environment(project, root, python)
    environment.update(CC="gcc", LDSHARED="gcc -shared", CIBUILDWHEEL="1")
    command = [str(python), "setup.py", "build_ext", "--inplace"]
    run = invoke(command, root, environment, output / "build", timeout)
    if run["exit_status"] != 0:
        raise RuntimeError(f"wcwidth extension build failed; see {output / 'build'}")
    identity = invoke(
        [
            str(python),
            "-c",
            "import json, wcwidth, wcwidth._wcwidth_c as extension; "
            "print(json.dumps({'version': wcwidth.__version__, "
            "'compiled': wcwidth.HAS_C_EXTENSION, 'source': wcwidth.__file__, "
            "'extension': extension.__file__}))",
        ],
        root,
        environment,
        output / "identity",
        timeout,
    )
    if identity["exit_status"] != 0:
        raise RuntimeError("wcwidth source extension could not be imported")
    metadata = json.loads(identity["stdout"])
    source = Path(metadata["source"]).resolve()
    extension = Path(metadata["extension"]).resolve()
    if (
        metadata["version"] != "0.9.1"
        or metadata["compiled"] is not True
        or source != (root / "wcwidth/__init__.py").resolve()
        or extension.parent != (root / "wcwidth").resolve()
        or not extension.is_file()
    ):
        raise RuntimeError("wcwidth must load the compiled extension from the pinned checkout")
    write_atomic(
        output / "extension.json",
        {
            **metadata,
            "sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
            "build_environment": {
                key: environment[key] for key in ("CC", "LDSHARED", "CIBUILDWHEEL")
            },
        },
    )


SORTEDCONTAINERS_STRESS_FIXTURE = """import random
import pytest

@pytest.fixture(autouse=True)
def _vfy_stress_seed(request):
    cases = {
        # The key-function examples call random() during membership tests.
        "docs/introduction.rst::introduction.rst",
        "tests/test_stress_sortedlist.py::test_stress",
        "tests/test_stress_sortedkeylist.py::test_stress",
        "tests/test_stress_sortedset.py::test_stress",
    }
    if request.node.nodeid not in cases:
        yield
        return
    state = random.getstate()
    random.seed(0)
    try:
        yield
    finally:
        random.setstate(state)
"""


def prepare_project(
    project: Project,
    root: Path,
    python: Path,
    output: Path,
    timeout: float,
) -> None:
    """Prepare the reviewed workload and verify upstream generated files."""

    if project.key == "sortedcontainers":
        revision = SORTEDCONTAINERS_REVISION
        if project.revision != revision or git(root, "rev-parse", "HEAD") != revision:
            raise RuntimeError("sortedcontainers stress seed requires its reviewed revision")
        fixture = root / "conftest.py"
        if fixture.exists():
            raise RuntimeError("sortedcontainers stress seed cannot replace an upstream conftest")
        fixture.write_text(SORTEDCONTAINERS_STRESS_FIXTURE, encoding="utf-8")
        write_atomic(
            output / "workload-recipe.json",
            {
                "project": project.key,
                "revision": revision,
                "fixture_sha256": file_sha256(fixture),
                "reason": "Seed the introduction doctest and the three random stress tests "
                "independently and restore global random state; worker partitioning otherwise "
                "changes per-test coverage.",
            },
        )

    if project.key == "rich":
        revision = RICH_REVISION
        if project.revision != revision or git(root, "rev-parse", "HEAD") != revision:
            raise RuntimeError("Rich broken-pipe workload patch requires its reviewed revision")
        patch = ROOT / "validation/pytest_runner/patches/rich-deterministic-workload.patch"
        sources = {
            name: {"before_sha256": file_sha256(root / name)}
            for name in ("tests/test_console.py", "tests/test_live.py", "tests/test_traceback.py")
        }
        subprocess.run(
            ["git", "-C", str(root), "apply", "--check", str(patch)], check=True, timeout=timeout
        )
        subprocess.run(["git", "-C", str(root), "apply", str(patch)], check=True, timeout=timeout)
        for name, hashes in sources.items():
            hashes["after_sha256"] = file_sha256(root / name)
        write_atomic(
            output / "workload-patch.json",
            {
                "project": project.key,
                "revision": revision,
                "patch": patch.relative_to(ROOT).as_posix(),
                "patch_sha256": file_sha256(patch),
                "sources": sources,
                "tests": [
                    "test_console.py::test_brokenpipeerror",
                    "test_live.py::test_growing_display_autorefresh",
                    "test_traceback.py::test_recursive",
                ],
                "reason": "Close the pipe reader before Rich starts to guarantee BrokenPipeError; "
                "retain the demo precheck and assert the producer exits with status 1. "
                "Route background refreshes to StringIO, retaining auto-refresh and final Step 9. "
                "Raise RecursionError after bounded recursion to retain traceback truncation "
                "without depending on the runner's Python stack depth.",
            },
        )

    if project.key == "wcwidth":
        prepare_wcwidth_extension(project, root, python, output / "extension", timeout)
    if project.key == "python-dateutil":
        prepare_dateutil_zoneinfo(root, python, output.with_name(f"{output.name}-zoneinfo"))
    if project.setup_args:
        command = [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            *project.setup_args,
        ]
        run = invoke(command, root, project_environment(project, root, python), output, timeout)
        if run["exit_status"] != 0:
            raise RuntimeError(
                f"{project.key}: upstream setup exited {run['exit_status']}; "
                f"see {output / 'stdout.txt'} and {output / 'stderr.txt'}"
            )
    generated = {name: (root / name).is_file() for name in project.generated_files}
    write_atomic(output / "generated-files.json", generated)
    missing = [name for name, exists in generated.items() if not exists]
    if missing:
        raise RuntimeError(f"{project.key}: upstream setup did not create {', '.join(missing)}")


def copy_checkout(upstream: Path, destination: Path) -> Path:
    subprocess.run(
        # Reuse the upstream object database while both checkouts are live.
        # The work tree is removed only after all routes for this project finish.
        ["git", "clone", "--quiet", "--shared", str(upstream), str(destination)],
        check=True,
        timeout=300,
    )
    return destination


def expanded_tests(root: Path, tests: tuple[str, ...]) -> tuple[str, ...]:
    files: list[str] = []
    for value in tests:
        filename, separator, suffix = value.partition("::")
        if "*" in filename or "?" in filename or "[" in filename:
            matches = sorted(path.relative_to(root).as_posix() for path in root.glob(filename))
            if not matches:
                raise ValueError(f"test filename glob matched no paths: {filename!r}")
            files.extend(f"{path}{separator}{suffix}" for path in matches)
        else:
            files.append(value)
    return tuple(files)


def observer_environment(
    env: dict[str, str], root: Path, python: Path, output: Path
) -> dict[str, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    inventory = output.with_name(f"{output.name}-inventory-nodeids.json")
    inventory.write_text("[]\n", encoding="utf-8")
    env = dict(env)
    env.update(
        {
            "VFY_PYTEST_RECORD_MODE": "pytest",
            "VFY_PYTEST_RECORD_ROOT": str(root.resolve()),
            "VFY_PYTEST_RECORD_TEMP": tempfile.gettempdir(),
            "VFY_PYTEST_RECORD_PREFIX": str(python.parent.parent.resolve()),
            "VFY_PYTEST_RECORD_PROBE": str((ROOT / "scripts/pytest_execution_probe.py").resolve()),
            "VFY_PYTEST_OBSERVER_RESULT": str((output / "observer.json").resolve()),
            "VFY_PYTEST_RECORD_INVENTORY_NODEIDS": str(inventory.resolve()),
            "VFY_PYTEST_RECORD_INVENTORY_NODEIDS_SHA256": hashlib.sha256(
                inventory.read_bytes()
            ).hexdigest(),
            "VFY_PYTEST_RECORD_RAW": str((output / "execution.json").resolve()),
            "VFY_PYTEST_RECORD_SESSION": secrets.token_hex(32),
        }
    )
    return env


def invoke(
    command: list[str], root: Path, env: dict[str, str], output: Path, timeout: float
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    write_atomic(
        output / "command.json", {"argv": command, "cwd": str(root), "timeout_seconds": timeout}
    )
    started = time.perf_counter()
    try:
        completed = run_captured(command, cwd=root, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        seconds = time.perf_counter() - started
        for name, contents in (("stdout", error.stdout), ("stderr", error.stderr)):
            decoded = (
                contents.decode("utf-8", errors="replace")
                if isinstance(contents, bytes)
                else contents
            )
            (output / f"{name}.txt").write_text(decoded or "", encoding="utf-8")
        write_atomic(
            output / "status.json",
            {"completed": False, "reason": "timeout", "seconds": seconds},
        )
        raise
    seconds = time.perf_counter() - started
    (output / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (output / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    write_atomic(
        output / "status.json",
        {"completed": True, "exit_status": completed.returncode, "seconds": seconds},
    )
    return {
        "exit_status": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "seconds": seconds,
    }


def plugin_args(
    project: Project,
    *,
    stock: bool = False,
    coverage_provider: Literal["project", "vfy"] = "project",
) -> list[str]:
    stock_plugins = project.stock_plugins if stock else ()
    if coverage_provider == "vfy":
        stock_plugins = tuple(plugin for plugin in stock_plugins if plugin != "pytest_cov.plugin")
    plugins = (*project.plugins, *stock_plugins)
    return [item for plugin in plugins for item in ("-p", plugin)]


def stock_outcome_command(
    project: Project,
    root: Path,
    python: Path,
    output: Path,
    basetemp: Path,
    *,
    observer: Literal["enabled", "disabled"] = "enabled",
) -> list[str]:
    return [
        str(python),
        "-P",
        "-m",
        "pytest",
        *(["-p", "pytest_execution_probe"] if observer == "enabled" else []),
        *plugin_args(project, stock=True),
        "--basetemp",
        str(basetemp),
        "--junitxml",
        str(output / "junit.xml"),
        "-q",
        *project.extra_args,
        *project.outcome_args,
        *project.stock_extra_args,
        *expanded_tests(root, project.tests),
    ]


def stock_outcome_run(
    project: Project, root: Path, python: Path, output: Path, basetemp: Path, timeout: float
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    env = observer_environment(project_environment(project, root, python), root, python, output)
    run = invoke(
        stock_outcome_command(project, root, python, output, basetemp), root, env, output, timeout
    )
    return run, load_outcome_record(run, output, env, "execution.json", root)


def native_outcome_command(
    project: Project,
    root: Path,
    python: Path,
    output: Path,
    basetemp: Path,
    *,
    workers: int = 1,
) -> list[str]:
    return [
        str(python),
        "-m",
        "vfy",
        "test",
        "--engine=pytest-native",
        f"--workers={workers}",
        "--history",
        str((output / "history.json").resolve()),
        "--no-cov",
        "--basetemp",
        str(basetemp),
        "--junitxml",
        str(output / "junit.xml"),
        "-q",
        *plugin_args(project),
        *project.extra_args,
        *project.outcome_args,
        *expanded_tests(root, project.tests),
    ]


def native_outcome_run(
    project: Project,
    root: Path,
    python: Path,
    output: Path,
    basetemp: Path,
    timeout: float,
    *,
    workers: int = 1,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    env = project_environment(project, root, python)
    env["PYTEST_NATIVE_RESULT"] = str((output / "native.json").resolve())
    command = native_outcome_command(project, root, python, output, basetemp, workers=workers)
    run = invoke(command, root, env, output, timeout)
    run["pytest_worker_roots"] = (
        [str((basetemp / f"native-worker-{index}").resolve()) for index in range(workers)]
        if workers > 1
        else []
    )
    return run, load_outcome_record(run, output, env, "native.json", root)


def load_outcome_record(
    run: dict[str, Any], output: Path, env: dict[str, str], filename: str, root: Path
) -> dict[str, Any] | None:
    run["record_errors"] = []
    try:
        raw = json.loads((output / filename).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("execution record is not an object")
        if filename == "native.json":
            return raw
        observer = json.loads((output / "observer.json").read_text(encoding="utf-8"))
        if not isinstance(observer, dict):
            raise ValueError("observer is not an object")
        if observer.get("session_token") != env["VFY_PYTEST_RECORD_SESSION"] or raw.get(
            "session_token"
        ) != observer.get("session_token"):
            raise ValueError("observer did not identify the requested session")
        observed_root = observer.get("rootdir")
        if not isinstance(observed_root, str) or not Path(observed_root).is_absolute():
            raise ValueError("observer root directory must be absolute")
        observed_root_path = Path(observed_root).resolve()
        if not observed_root_path.is_relative_to(root.resolve()):
            raise ValueError("observer root directory is outside the pinned checkout")
        validate_native_identity(raw, observer, observed_root_path)
        run["observer"] = observer
        run["observer_source_sha256"] = file_sha256(Path(env["VFY_PYTEST_RECORD_PROBE"]))
    except (OSError, ValueError) as error:
        run["record_errors"].append(str(error))
        return None
    return raw


def normalize_native(
    raw: dict[str, Any], root: Path, python: Path, basetemp: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases = native_cases(
        raw,
        root,
        tempfile.gettempdir(),
        str(python.parent.parent.resolve()),
        str(basetemp.resolve()),
    )
    session = native_session_warnings(
        raw,
        cases,
        root,
        tempfile.gettempdir(),
        str(python.parent.parent.resolve()),
        str(basetemp.resolve()),
    )
    return cases, session


@dataclass(frozen=True)
class ObservationNormalizer:
    """One invocation's resolved path identities and literal replacements."""

    prefixes: tuple[str, ...]
    checkout_prefixes: tuple[str, ...]
    text: TextNormalizer
    worker_aliases: tuple[str, ...]
    basetemp: str

    @classmethod
    def create(
        cls,
        root: Path,
        python: Path,
        basetemp: Path,
        *,
        worker_roots: tuple[Path, ...] = (),
    ) -> ObservationNormalizer:
        prefixes, checkout_prefixes = _nodeid_paths(root)
        base = str(basetemp.resolve())
        text = TextNormalizer.from_paths(
            root,
            tempfile.gettempdir(),
            str(python.parent.parent.resolve()),
            pytest_temporary=base,
        )
        aliases = []
        for worker in worker_roots:
            alias = str(worker.resolve())
            aliases.extend((alias, text(alias)))
        return cls(
            prefixes,
            checkout_prefixes,
            text,
            tuple(aliases),
            base,
        )

    def nodeid(self, nodeid: str) -> str:
        return _normalize_nodeid(nodeid, self.prefixes, self.checkout_prefixes)

    def case(self, case: dict[str, Any]) -> dict[str, Any]:
        normalized = {**copy.deepcopy(case), "nodeid": self.nodeid(case["nodeid"])}
        for report in normalized.get("reports", []):
            for field in ("exception", "diagnostic", "stdout", "stderr", "log"):
                value = report.get(field)
                if value is not None:
                    for alias in self.worker_aliases:
                        value = value.replace(f"{alias}{os.sep}", f"{self.basetemp}{os.sep}")
                    report[field] = self.text(value)
        for warning in normalized.get("warnings", []):
            for field in ("message", "filename"):
                warning[field] = self.text(warning[field])
        return normalized

    def warning(self, warning: dict[str, Any]) -> dict[str, Any]:
        normalized = self.case({"nodeid": warning["nodeid"], "warnings": [warning]})
        return {**normalized["warnings"][0], "nodeid": normalized["nodeid"]}


def _normalize_nodeid(
    nodeid: str,
    prefixes: tuple[str, ...],
    checkout_prefixes: tuple[str, ...],
) -> str:
    path, separator, suffix = nodeid.partition("::")
    for prefix in prefixes:
        if path.startswith(f"{prefix}/"):
            path = path[len(prefix) + 1 :]
            break
    value = f"{path}{separator}{suffix}"
    for prefix in checkout_prefixes:
        value = value.replace(prefix, "<UPSTREAM>/")
    return value


def _nodeid_paths(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    resolved = root.resolve()
    prefixes = tuple(
        sorted(
            {
                resolved.as_posix().rstrip("/"),
                Path(os.path.relpath(resolved, Path.cwd())).as_posix().rstrip("/"),
            },
            key=len,
            reverse=True,
        )
    )
    checkout = tuple(sorted({f"{resolved}{os.sep}", f"{resolved.as_posix()}/"}))
    return prefixes, checkout


def normalize_nodeid(nodeid: str, root: Path) -> str:
    """Remove the route-specific checkout root from a pytest node ID."""
    return _normalize_nodeid(nodeid, *_nodeid_paths(root))


def normalize_checkout_paths(value: str, root: Path) -> str:
    """Normalize known checkout paths embedded in parameter identities."""
    resolved = root.resolve()
    for prefix in sorted({f"{resolved}{os.sep}", f"{resolved.as_posix()}/"}):
        value = value.replace(prefix, "<UPSTREAM>/")
    return value


def normalize_case_paths(
    case: dict[str, Any],
    root: Path,
    python: Path,
    basetemp: Path,
    *,
    worker_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    return ObservationNormalizer.create(root, python, basetemp, worker_roots=worker_roots).case(
        case
    )


def normalize_cases_paths(
    cases: list[dict[str, Any]],
    root: Path,
    python: Path,
    basetemp: Path,
    *,
    worker_roots: tuple[Path, ...] = (),
) -> list[dict[str, Any]]:
    normalizer = ObservationNormalizer.create(root, python, basetemp, worker_roots=worker_roots)
    return [normalizer.case(case) for case in cases]


def validate_cases(cases: list[dict[str, Any]], session_warnings: object) -> list[dict[str, Any]]:
    """Reject partial phase records and missing literal observations."""
    if not isinstance(session_warnings, list):
        raise ValueError("missing session warnings array")
    for case in cases:
        reports = case.get("reports")
        warnings = case.get("warnings")
        if not isinstance(reports, list) or not reports or not isinstance(warnings, list):
            raise ValueError(f"{case['nodeid']}: missing reports or warnings")
        for report in reports:
            if not isinstance(report, dict):
                raise ValueError(f"{case['nodeid']}: malformed phase report")
            if report.get("phase") not in {"setup", "call", "teardown"} or report.get(
                "outcome"
            ) not in {"passed", "failed", "error", "skipped", "xfailed", "xpassed"}:
                raise ValueError(f"{case['nodeid']}: invalid phase or outcome")
            for field in ("exception", "diagnostic"):
                if field not in report or (
                    report[field] is not None and not isinstance(report[field], str)
                ):
                    raise ValueError(f"{case['nodeid']}: missing or invalid {field}")
            for field in ("stdout", "stderr", "log"):
                if not isinstance(report.get(field), str):
                    raise ValueError(f"{case['nodeid']}: missing or invalid {field}")
        phases = [report["phase"] for report in reports]
        if (
            phases[0] != "setup"
            or phases[-1] != "teardown"
            or any(phase != "call" for phase in phases[1:-1])
            or (reports[0]["outcome"] == "passed" and len(phases) < 3)
        ):
            raise ValueError(f"{case['nodeid']}: incomplete or unordered phase reports")
        validate_warnings(warnings)
    validate_warnings(session_warnings, session=True)
    return session_warnings


def validate_warnings(warnings: list[dict[str, Any]], *, session: bool = False) -> None:
    for warning in warnings:
        if (
            not isinstance(warning, dict)
            or any(not isinstance(warning.get(field), str) for field in ("message", "filename"))
            or (session and not isinstance(warning.get("nodeid"), str))
        ):
            raise ValueError("malformed warning observation")
        line = warning.get("lineno")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ValueError("warning has invalid line number")


def normalize_outcome_record(
    route: str,
    raw: dict[str, Any] | None,
    run: dict[str, Any],
    root: Path,
    python: Path,
    basetemp: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    if not isinstance(raw, dict):
        raise ValueError("missing execution record")
    raw_cases = raw.get("cases")
    collected = raw.get("collected")
    if not isinstance(collected, int) or isinstance(collected, bool) or collected <= 0:
        raise ValueError("collected count must be a positive integer")
    if not isinstance(raw_cases, list) or len(raw_cases) != collected:
        raise ValueError("execution case count does not match collected count")
    if any(
        not isinstance(case, dict) or not isinstance(case.get("nodeid"), str) or not case["nodeid"]
        for case in raw_cases
    ):
        raise ValueError("malformed execution case identity")
    normalizer = ObservationNormalizer.create(root, python, basetemp)
    ids = [normalizer.nodeid(case["nodeid"]) for case in raw_cases]
    duplicates = {nodeid for nodeid, count in Counter(ids).items() if count > 1}
    if route == "stock":
        if raw.get("schema_version") != 2 or raw.get("session_depth") != 0:
            raise ValueError("stock observer does not identify the outer session")
        if raw.get("exit_status") != run["exit_status"]:
            raise ValueError("stock observer exit status differs from process status")
        inventory = raw.get("collected_nodeids")
        if not isinstance(inventory, list) or any(
            not isinstance(nodeid, str) or not nodeid for nodeid in inventory
        ):
            raise ValueError("missing ordered collection identities")
        collected_ids = [normalizer.nodeid(nodeid) for nodeid in inventory]
        if len(inventory) != collected or collected_ids != ids:
            raise ValueError("ordered execution identities differ from collection")
        cases = [normalizer.case(case) for case in raw_cases]
        session = raw.get("session_warnings")
        session = validate_cases(cases, session)
        if any(case["warnings"] for case in cases if case["nodeid"] in duplicates):
            raise ValueError("warnings cannot disambiguate repeated node IDs")
        session = [normalizer.warning(warning) for warning in session]
    else:
        if duplicates and any(
            isinstance(warning, dict) and normalizer.nodeid(warning.get("nodeid", "")) in duplicates
            for warning in raw.get("warnings", [])
        ):
            raise ValueError("warnings cannot disambiguate repeated node IDs")
        cases, session = normalize_native(raw, root, python, basetemp)
        worker_roots = tuple(Path(value) for value in run.get("pytest_worker_roots", []))
        if worker_roots:
            normalizer = ObservationNormalizer.create(
                root, python, basetemp, worker_roots=worker_roots
            )
        cases = [normalizer.case(case) for case in cases]
        validate_cases(cases, session)
        session = [normalizer.warning(warning) for warning in session]
        # Successful complete native records retain one case for each
        # collected item, in collection order, including duplicate IDs.
        collected_ids = ids
    return cases, session, collected_ids


def compare_outcomes(
    stock: dict[str, Any],
    native: dict[str, Any],
    stock_raw: dict[str, Any] | None,
    native_raw: dict[str, Any] | None,
    stock_root: Path,
    native_root: Path,
    python: Path,
    basetemp: Path,
    *,
    project: Project | None = None,
) -> dict[str, Any]:
    record_errors: list[str] = []
    stock_cases: list[dict[str, Any]] = []
    native_normalized: list[dict[str, Any]] = []
    stock_session: list[dict[str, Any]] = []
    native_session: list[dict[str, Any]] = []
    stock_collected_ids: list[str] = []
    native_collected_ids: list[str] = []
    for route, raw, run, root in (
        ("stock", stock_raw, stock, stock_root),
        ("native", native_raw, native, native_root),
    ):
        record_errors.extend(f"{route}: {error}" for error in run.get("record_errors", []))
        try:
            cases, session, collected_ids = normalize_outcome_record(
                route, raw, run, root, python, basetemp
            )
            if route == "stock":
                stock_cases, stock_session, stock_collected_ids = cases, session, collected_ids
            else:
                native_normalized, native_session, native_collected_ids = (
                    cases,
                    session,
                    collected_ids,
                )
        except (KeyError, TypeError, ValueError) as error:
            record_errors.append(f"{route}: {error}")
    stock_raw = stock_raw if isinstance(stock_raw, dict) else {}
    native_raw = native_raw if isinstance(native_raw, dict) else {}
    collected_equal = stock_raw.get("collected") == native_raw.get("collected")
    collected_nodeids_equal = stock_collected_ids == native_collected_ids
    engine_equal = native_raw.get("engine") == "pytest-runner-native-v1"
    literal_case_differences = copy.deepcopy(first_difference(stock_cases, native_normalized))
    literal_session_warning_differences = copy.deepcopy(
        first_difference(stock_session, native_session)
    )
    normalizations: dict[str, list[dict[str, Any]]] = {"stock": [], "native": []}
    for nodeid, warnings in (
        *((case["nodeid"], case["warnings"]) for case in native_normalized),
        ("", native_session),
    ):
        for index, warning in enumerate(warnings):
            message = warning["message"]
            if message.endswith("\n") and not message.endswith("\n\n"):
                warning["message"] = message[:-1]
                normalizations["native"].append(
                    {
                        "nodeid": warning.get("nodeid", nodeid),
                        "warning_index": index,
                        "field": "message",
                        "rule": "warning-format-terminator",
                        "occurrences": 1,
                        "rationale": "Stock's observer omits one final formatter newline.",
                    }
                )
    if project is not None and not record_errors:
        try:
            stock_cases, normalizations["stock"] = normalize_adoption_observations(
                stock_cases, project=project.key, revision=project.revision
            )
            native_normalized, native_rules = normalize_adoption_observations(
                native_normalized, project=project.key, revision=project.revision
            )
            normalizations["native"].extend(native_rules)
        except ValueError as error:
            record_errors.append(f"observation normalization: {error}")
    ordered_nodeids_equal = [case["nodeid"] for case in stock_cases] == [
        case["nodeid"] for case in native_normalized
    ]
    outcome_fields = ("phase", "outcome", "exception")
    diagnostic_fields = (*outcome_fields, "diagnostic")
    capture_fields = (*outcome_fields, "stdout", "stderr", "log")

    def project_cases(
        cases: list[dict[str, Any]], fields: tuple[str, ...], include_warnings: bool = False
    ) -> list[dict[str, Any]]:
        projected = []
        for case in cases:
            item: dict[str, Any] = {
                "nodeid": case["nodeid"],
                "reports": [
                    {field: report.get(field) for field in fields} for report in case["reports"]
                ],
            }
            if include_warnings:
                item["warnings"] = case.get("warnings", [])
            projected.append(item)
        return projected

    outcome_equal = project_cases(stock_cases, outcome_fields) == project_cases(
        native_normalized, outcome_fields
    )
    diagnostics_equal = project_cases(stock_cases, diagnostic_fields) == project_cases(
        native_normalized, diagnostic_fields
    )
    captured_output_equal = project_cases(stock_cases, capture_fields) == project_cases(
        native_normalized, capture_fields
    )
    case_warnings_equal = all(
        stock_case.get("warnings", []) == native_case.get("warnings", [])
        for stock_case, native_case in zip(stock_cases, native_normalized, strict=False)
    ) and len(stock_cases) == len(native_normalized)
    session_warnings_equal = stock_session == native_session
    status_equal = stock["exit_status"] == native["exit_status"]
    equal = (
        not record_errors
        and stock_cases == native_normalized
        and session_warnings_equal
        and collected_equal
        and collected_nodeids_equal
        and engine_equal
        and status_equal
    )
    stock_ids = [case.get("nodeid") for case in stock_cases]
    native_ids = [case.get("nodeid") for case in native_normalized]
    phases: dict[str, int] = {}
    for case in native_normalized:
        for report in case["reports"]:
            outcome = str(report["outcome"])
            phases[outcome] = phases.get(outcome, 0) + 1
    return {
        "equal": equal,
        "literal_case_differences": literal_case_differences,
        "literal_session_warning_differences": literal_session_warning_differences,
        "observation_normalizations": normalizations,
        "record_errors": record_errors,
        "engine_equal": engine_equal,
        "native_engine": native_raw.get("engine"),
        "collected_nodeids_equal": collected_nodeids_equal,
        "collected_nodeid_differences": first_difference(stock_collected_ids, native_collected_ids),
        "stock_collected": stock_raw.get("collected"),
        "native_collected": native_raw.get("collected"),
        "collected_equal": collected_equal,
        "outcome_equal": outcome_equal,
        "diagnostics_equal": diagnostics_equal,
        "captured_output_equal": captured_output_equal,
        "case_warnings_equal": case_warnings_equal,
        "session_warnings_equal": session_warnings_equal,
        "mismatch_kinds": [
            kind
            for kind, matches in (
                ("ordered-nodeids", ordered_nodeids_equal),
                ("collected-nodeids", collected_nodeids_equal),
                ("collection-count", collected_equal),
                ("execution-record", not record_errors),
                ("engine", engine_equal),
                ("exit-status", status_equal),
                ("case-outcomes", outcome_equal),
                ("diagnostics", diagnostics_equal),
                ("captured-output", captured_output_equal),
                ("case-warnings", case_warnings_equal),
                ("session-warnings", session_warnings_equal),
            )
            if not matches
        ],
        "stock_case_count": len(stock_cases),
        "native_case_count": len(native_normalized),
        "stock_ordered_nodeids_sha256": hashlib.sha256(
            json.dumps(stock_ids, separators=(",", ":")).encode()
        ).hexdigest(),
        "native_ordered_nodeids_sha256": hashlib.sha256(
            json.dumps(native_ids, separators=(",", ":")).encode()
        ).hexdigest(),
        "phase_outcomes": phases,
        "stock_session_warnings": stock_session,
        "native_session_warnings": native_session,
        "case_differences": first_difference(stock_cases, native_normalized),
        "status_equal": status_equal,
        "stock_exit_status": stock["exit_status"],
        "native_exit_status": native["exit_status"],
        "ordered_nodeids_equal": ordered_nodeids_equal,
    }


def first_difference(left: object, right: object) -> dict[str, object] | None:
    if left == right:
        return None
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys()):
            if key not in left or key not in right:
                return {"path": [key], "stock_present": key in left, "native_present": key in right}
            difference = first_difference(left[key], right[key])
            if difference is not None:
                path = difference.pop("path", [])
                assert isinstance(path, list)
                return {"path": [key, *path], **difference}
    if isinstance(left, list) and isinstance(right, list):
        for index, (a, b) in enumerate(zip(left, right, strict=False)):
            if a != b:
                return {"index": index, "stock": a, "native": b}
        return {"stock_length": len(left), "native_length": len(right)}
    return {"stock": left, "native": right}


def normalize_session_warning(
    warning: dict[str, Any], root: Path, python: Path, basetemp: Path
) -> dict[str, Any]:
    normalized = normalize_case_paths(
        {"nodeid": warning["nodeid"], "warnings": [warning]}, root, python, basetemp
    )
    return {**normalized["warnings"][0], "nodeid": normalized["nodeid"]}


def coverage_signature(data_path: Path, root: Path) -> dict[str, Any]:
    data = CoverageData(basename=str(data_path))
    try:
        data.read()
        files: dict[str, Any] = {}
        original_names: dict[str, str] = {}
        context_names: dict[str, str] = {}
        original_context_names: dict[str, str] = {}
        for name in sorted(data.measured_contexts()):
            normalized = normalize_checkout_paths(name, root)
            if normalized in original_context_names:
                raise ValueError(
                    f"coverage data context collision after checkout normalization: "
                    f"{original_context_names[normalized]!r} and {name!r}"
                )
            original_context_names[normalized] = name
            context_names[name] = normalized
        checkout_prefix = f"{root.resolve().as_posix()}/"
        for filename in sorted(data.measured_files()):
            # Stored absolute and relative aliases have distinct evidence keys.
            relative = (
                f"<UPSTREAM>/{filename[len(checkout_prefix) :]}"
                if filename.startswith(checkout_prefix)
                else filename
            )
            if relative in files:
                raise ValueError(
                    f"coverage data filename collision after checkout normalization: "
                    f"{original_names[relative]!r} and {filename!r}"
                )
            original_names[relative] = filename
            contexts = data.contexts_by_lineno(filename)
            files[relative] = {
                "lines": sorted(data.lines(filename) or []),
                "arcs": sorted(data.arcs(filename) or []),
                "tracer": data.file_tracer(filename),
                "contexts": {
                    str(line): sorted(context_names[name] for name in names)
                    for line, names in sorted(contexts.items())
                },
            }
            context_arcs = {}
            if data.has_arcs():
                names = {name for values in contexts.values() for name in values}
                if any(start <= 0 and end <= 0 for start, end in files[relative]["arcs"]):
                    names.update(data.measured_contexts())
                for name in sorted(names):
                    data.set_query_context(name)
                    arcs = sorted(data.arcs(filename) or [])
                    if arcs:
                        context_arcs[context_names[name]] = arcs
                data.set_query_contexts(None)
            files[relative]["context_arcs"] = context_arcs
        return {
            "has_arcs": data.has_arcs(),
            "files": files,
            "measured_contexts": sorted(context_names.values()),
        }
    finally:
        data.close(force=True)


def aggregate_coverage_data(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "has_arcs": data["has_arcs"],
        "files": {
            name: {"lines": record["lines"], "arcs": record["arcs"], "tracer": record["tracer"]}
            for name, record in data["files"].items()
        },
    }


def normalized_coverage_json(path: Path, root: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        raise ValueError(f"coverage JSON has no file mapping: {path}")
    files: dict[str, Any] = {}
    original_names: dict[str, str] = {}
    for filename, value in raw_files.items():
        if not isinstance(filename, str) or not isinstance(value, dict):
            raise ValueError(f"coverage JSON has an invalid file entry: {path}")
        file_path = Path(filename)
        if not file_path.is_absolute():
            file_path = root / file_path
        try:
            relative = file_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            relative = os.path.normpath(filename)
        if relative in files:
            raise ValueError(
                f"coverage report filename collision after checkout normalization: "
                f"{original_names[relative]!r} and {filename!r}"
            )
        original_names[relative] = filename
        files[relative] = value
    return {"files": files, "totals": payload.get("totals")}


def coverage_command(
    project: Project,
    root: Path,
    python: Path,
    output: Path,
    route: str,
    *,
    workers: int = 1,
    basetemp: Path | None = None,
) -> list[str]:
    args: list[str] = [
        *project.coverage_args,
        f"--cov-report=json:{output / 'coverage.json'}",
        f"--junitxml={output / 'junit.xml'}",
        "-q",
        *project.extra_args,
        *(["--basetemp", str(basetemp)] if basetemp is not None else []),
        *expanded_tests(root, project.tests),
    ]
    if not any(item == "--cov-context" or item.startswith("--cov-context=") for item in args):
        args.append("--cov-context=test")
    if route == "pytest":
        command = [
            str(python),
            "-P",
            "-m",
            "pytest",
            "-p",
            "vfy.pytest_plugin",
            *plugin_args(project, stock=True, coverage_provider="vfy"),
            *project.stock_extra_args,
            *args,
        ]
    else:
        command = [
            str(python),
            "-m",
            "vfy",
            "test",
            "--engine=pytest-native",
            f"--workers={workers}",
            "--history",
            str((output / "history.json").resolve()),
            *plugin_args(project),
            *args,
        ]
    return command


def coverage_run(
    project: Project,
    root: Path,
    python: Path,
    output: Path,
    route: str,
    timeout: float,
    *,
    workers: int = 1,
    basetemp: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    env = project_environment(project, root, python)
    env["COVERAGE_FILE"] = str((output / ".coverage").resolve())
    if route != "pytest":
        env["PYTEST_NATIVE_RESULT"] = str((output / "native.json").resolve())
    command = coverage_command(
        project, root, python, output, route, workers=workers, basetemp=basetemp
    )
    run = invoke(command, root, env, output, timeout)
    run["record_errors"] = []
    try:
        run["junit_outcomes"] = junit_outcomes(output / "junit.xml", root=root)
        run["junit_cases"] = [
            {"classname": case["classname"], "name": case["name"]} for case in run["junit_outcomes"]
        ]
        if not run["junit_cases"]:
            raise ValueError("coverage run has no JUnit cases")
    except (OSError, ET.ParseError, ValueError) as error:
        run["junit_cases"] = []
        run["junit_outcomes"] = []
        run["record_errors"].append(str(error))
    if route != "pytest":
        try:
            raw = json.loads((output / "native.json").read_text(encoding="utf-8"))
            cases, session = normalize_native(raw, root, python, basetemp or root / "pytest-tmp")
            validate_cases(cases, session)
            if not cases or raw.get("collected") != len(cases):
                raise ValueError("native coverage run has incomplete execution records")
        except (OSError, KeyError, TypeError, ValueError) as error:
            run["record_errors"].append(str(error))
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--cov-config", default=True)
    options, _ = parser.parse_known_args(command)
    configured = options.cov_config
    config_file = str((root / configured).resolve()) if isinstance(configured, str) else True
    return run, coverage_result(root, output, config_file=config_file)


def coverage_result(
    root: Path,
    output: Path,
    *,
    retention: Literal["normalized", "raw"] = "normalized",
    config_file: str | bool = True,
) -> dict[str, Any] | None:
    report_path = output / "coverage.json"
    result = None
    if report_path.is_file() and (output / ".coverage").is_file():
        result = {
            "normalized_json": normalized_coverage_json(report_path, root),
            "data": coverage_signature(output / ".coverage", root),
        }
        result["aggregate_data"] = aggregate_coverage_data(result["data"])
        oracle: dict[str, Any] = {"equal": False, "expected": None, "error": None}
        try:
            oracle["expected"] = coverage_reference(
                root, output, config_file=config_file, branch=result["data"]["has_arcs"]
            )
            oracle["equal"] = oracle["expected"] == result["normalized_json"]
        except (OSError, ValueError) as error:
            oracle["error"] = str(error)
        result["report_oracle"] = oracle
        if retention == "normalized":
            write_atomic(output / "coverage-normalized.json", result)
    return result


def route_summary(
    run: dict[str, Any], raw: dict[str, Any] | None, output: Path, *, root: Path | None = None
) -> dict[str, Any]:
    junit = output / "junit.xml"
    cases = []
    if junit.is_file():
        cases = junit_outcomes(junit, root=root)
    warning_count = None
    if raw:
        raw_warnings = raw.get("warnings")
        if isinstance(raw_warnings, list):
            warning_count = len(raw_warnings)
        else:
            warning_count = len(raw.get("session_warnings", [])) + sum(
                len(case.get("warnings", []))
                for case in raw.get("cases", [])
                if isinstance(case, dict)
            )
    return {
        "exit_status": run["exit_status"],
        "engine": raw.get("engine") if raw else None,
        "execution_records": len(raw.get("cases", [])) if raw else 0,
        "observer_source_sha256": run.get("observer_source_sha256"),
        "observer": run.get("observer"),
        "verified_native_imports": run.get("verified_native_imports"),
        "pytest_worker_roots": run.get("pytest_worker_roots", []),
        "diagnostic": f"{run['stdout']}\n{run['stderr']}" if raw is None else None,
        "collected": raw.get("collected") if raw else None,
        "cases": len(cases),
        "ordered_junit_cases": [
            {"classname": case["classname"], "name": case["name"]} for case in cases
        ],
        "ordered_junit_outcomes": cases,
        "passed": sum(not case["outcomes"] for case in cases),
        "skipped_or_xfailed": sum(
            any(outcome["outcome"] == "skipped" for outcome in case["outcomes"]) for case in cases
        ),
        "failed_or_errored": sum(
            any(outcome["outcome"] in {"failure", "error"} for outcome in case["outcomes"])
            for case in cases
        ),
        "warnings": warning_count,
    }


def junit_outcomes(path: Path, *, root: Path | None = None) -> list[dict[str, Any]]:
    return [
        {
            "classname": case.attrib.get("classname", ""),
            "name": normalize_checkout_paths(case.attrib.get("name", ""), root)
            if root is not None
            else case.attrib.get("name", ""),
            "outcomes": [
                {"outcome": child.tag, "type": child.attrib.get("type", "")}
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ],
        }
        for case in ET.parse(path).getroot().iter("testcase")
    ]


def verify_outcome_artifacts(run: dict[str, Any], purelib: str, wheel: Path, route: str) -> None:
    observer = run.get("observer")
    if not isinstance(observer, dict):
        return
    try:
        validate_pytest_origin(observer, purelib)
        if route != "stock":
            run["verified_native_imports"] = validate_native_imports(observer, purelib, wheel)
    except ValueError as error:
        run["record_errors"].append(str(error))


def run_project(
    project: Project, output: Path, wheel: Path, python_version: str, timeout: float
) -> dict[str, Any]:
    # Equal-length checkout names preserve wrapping in diagnostics and captures.
    routes = (
        ("stock", "pytest-0", 1),
        ("native", "native-1", 1),
        ("native_workers2", "native-2", 2),
    )
    # Keep checkouts outside vfy so pytest discovers each project's own root.
    with tempfile.TemporaryDirectory(prefix=f"vfy-adoption-{project.key}-") as temporary_root:
        work_root = Path(temporary_root)
        project_root = checkout(project, work_root / "checkouts" / "upstream")
        python = prepare_python(project, work_root, output, wheel, python_version)
        metadata = interpreter_metadata(python, "vfy")
        pytest_tmp = work_root / "pytest-tmp"
        outcome_runs: dict[str, tuple[dict[str, Any], dict[str, Any] | None, Path]] = {}
        summaries: dict[str, Any] = {}
        for route, checkout_name, workers in routes:
            root = copy_checkout(
                project_root, work_root / "checkouts" / f"{checkout_name}-outcomes"
            )
            route_output = output / "runs" / project.key / f"{route}-outcomes"
            if project.setup_args or project.key in {"rich", "wcwidth", "sortedcontainers"}:
                prepare_project(
                    project, root, python, route_output.with_name(f"{route}-setup"), timeout
                )
            if route == "stock":
                run, raw = stock_outcome_run(
                    project, root, python, route_output, pytest_tmp, timeout
                )
            else:
                run, raw = native_outcome_run(
                    project, root, python, route_output, pytest_tmp, timeout, workers=workers
                )
            verify_outcome_artifacts(run, metadata["purelib"], wheel, route)
            outcome_runs[route] = run, raw, root
            summaries[route] = route_summary(run, raw, route_output, root=root)
        stock_run, stock_raw, stock_root = outcome_runs["stock"]
        comparisons = {
            comparison_name: compare_outcomes(
                stock_run,
                run,
                stock_raw,
                raw,
                stock_root,
                root,
                python,
                pytest_tmp,
                project=project,
            )
            for comparison_name, (run, raw, root) in (
                ("native-default", outcome_runs["native"]),
                ("native-workers2", outcome_runs["native_workers2"]),
            )
        }
        missing_native = [
            run for route, (run, raw, _) in outcome_runs.items() if route != "stock" and raw is None
        ]
        if stock_raw is None or stock_run["exit_status"] in {2, 3, 4}:
            status = "stock-setup-failure"
        elif any(
            any(
                text in f"{run['stdout']}\n{run['stderr']}".lower()
                for text in (
                    "unsupported",
                    "does not support",
                    "does not load pytest_plugins",
                    "native pytest does not execute conftest hook",
                )
            )
            for run in missing_native
        ):
            status = "unsupported-native"
        elif missing_native:
            status = "native-setup-failure"
        elif all(comparison["equal"] for comparison in comparisons.values()):
            status = "parity" if stock_run["exit_status"] == 0 else "matching-test-failure"
        else:
            status = "execution-record-mismatch"
        project_record: dict[str, Any] = {
            "project": project.key,
            "repository": project.repository,
            "revision": project.revision,
            "scope": project.scope,
            "adoption_class": project.adoption_class,
            "test_args": list(project.tests),
            "plugins": list(project.plugins),
            "dependency_lock": str(
                (
                    project.dependency_lock
                    or ROOT / "validation/pytest_runner/native-adoption-requirements.txt"
                ).resolve()
            ),
            **summaries,
            "outcome_comparisons": comparisons,
            "status": status,
            "coverage": {"status": "not-run-until-outcome-parity"},
        }
        if status != "parity":
            return project_record
        coverage_runs: dict[str, dict[str, Any]] = {}
        coverage_records: dict[str, dict[str, Any] | None] = {}
        for route, checkout_name, workers in routes:
            root = copy_checkout(
                project_root, work_root / "checkouts" / f"{checkout_name}-coverage"
            )
            route_output = output / "runs" / project.key / f"{route}-coverage"
            if project.setup_args or project.key in {"rich", "wcwidth", "sortedcontainers"}:
                prepare_project(
                    project,
                    root,
                    python,
                    route_output.with_name(f"{route}-coverage-setup"),
                    timeout,
                )
            run, record = coverage_run(
                project,
                root,
                python,
                route_output,
                "pytest" if route == "stock" else "native",
                timeout,
                workers=workers,
            )
            coverage_runs[route] = run
            coverage_records[route] = record
            if run["junit_outcomes"] != summaries["stock"]["ordered_junit_outcomes"]:
                run["record_errors"].append(
                    "coverage JUnit outcomes differ from stock outcome execution"
                )
        stock_coverage = coverage_records["stock"]
        expectation = PROJECT_EXPECTATIONS.get(project.key, ExpectedParity())
        contract = expectation.coverage
        reason = expectation.reason
        coverage_result: dict[str, Any] = {
            **coverage_records,
            "contract": contract,
            "reason": reason,
            "junit_cases": {route: run["junit_cases"] for route, run in coverage_runs.items()},
            "junit_outcomes": {
                route: run["junit_outcomes"] for route, run in coverage_runs.items()
            },
            "source_files_equal": bool(
                stock_coverage
                and all(
                    record
                    and record["data"]["files"].keys() == stock_coverage["data"]["files"].keys()
                    for record in coverage_records.values()
                )
            ),
        }
        for route, prefix in (("native", ""), ("native_workers2", "native_workers2_")):
            record = coverage_records[route]
            coverage_result[f"{prefix}normalized_json_equal"] = bool(
                stock_coverage
                and record
                and stock_coverage["normalized_json"] == record["normalized_json"]
            )
            coverage_result[f"{prefix}schema7_data_equal"] = bool(
                stock_coverage and record and stock_coverage["data"] == record["data"]
            )
            coverage_result[f"{prefix}aggregate_data_equal"] = bool(
                stock_coverage
                and record
                and stock_coverage["aggregate_data"] == record["aggregate_data"]
            )
            coverage_result[f"{prefix}data_differences"] = first_difference(
                stock_coverage["data"] if stock_coverage else None,
                record["data"] if record else None,
            )
        for route, run in coverage_runs.items():
            coverage_result[f"{route}_exit_status"] = run["exit_status"]
            coverage_result[f"{route}_record_errors"] = run["record_errors"]
        comparison_fields = {
            "exact": ("normalized_json_equal", "schema7_data_equal"),
            "aggregate-only": ("normalized_json_equal", "aggregate_data_equal"),
            "same-run-report": (),
        }[contract]
        coverage_equal = (
            all(
                run["junit_outcomes"] == coverage_runs["stock"]["junit_outcomes"]
                for run in coverage_runs.values()
            )
            and all(
                coverage_result[f"{prefix}{field}"]
                for prefix in ("", "native_workers2_")
                for field in comparison_fields
            )
            and coverage_result["source_files_equal"]
            and not any(run["record_errors"] for run in coverage_runs.values())
            and all(
                record and record["report_oracle"]["equal"] for record in coverage_records.values()
            )
        )
        if coverage_equal and all(run["exit_status"] == 0 for run in coverage_runs.values()):
            coverage_status = "validated"
        else:
            coverage_status = "mismatch-or-incomplete"
        coverage_result["status"] = coverage_status
        project_record["coverage"] = coverage_result
        return project_record


REQUIRED_PROJECTS = ("iniconfig", "sortedcontainers", "pluggy", "colorama", "rich")


@dataclass(frozen=True)
class ExpectedParity:
    """The reviewed coverage comparison scope for a project's successful runs."""

    coverage: Literal["exact", "aggregate-only", "same-run-report"] = "exact"
    reason: str | None = None


PROJECT_EXPECTATIONS: dict[str, ExpectedParity] = {
    "iniconfig": ExpectedParity(),
    "pluggy": ExpectedParity(
        "aggregate-only",
        "pytest executes measured Pluggy framework calls that native execution does not; "
        "cross-engine contexts are retained but not compared",
    ),
    "packaging": ExpectedParity(),
    "attrs": ExpectedParity(),
    "wcwidth": ExpectedParity(
        reason="upstream --cov-append blocks native coverage; exact parity is required if supported"
    ),
    "colorama": ExpectedParity(),
    "platformdirs": ExpectedParity(),
    "python-dotenv": ExpectedParity(),
    "typing_extensions": ExpectedParity(),
    "sortedcontainers": ExpectedParity(),
    "python-dateutil": ExpectedParity(),
    "click": ExpectedParity(
        "aggregate-only",
        "cyclic finalizers can run under different test contexts",
    ),
    "poetry-core": ExpectedParity(
        "aggregate-only",
        "worker-local lazy grammar and marker parser caches attribute initialization to "
        "different first-use tests; cross-worker contexts are retained but not compared",
    ),
    "pyproject-api": ExpectedParity(),
    "rich": ExpectedParity(
        "same-run-report",
        "threaded workload and import or worker state can change observed hits; "
        "each route's own data is checked with Coverage.py and source inventories must match",
    ),
    "fastapi": ExpectedParity(),
    "black": ExpectedParity(),
    "requests": ExpectedParity(),
}


def positive_count(value: object) -> bool:
    return type(value) is int and value > 0


def outcome_parity_errors(comparison: object, route: str) -> list[str]:
    if not isinstance(comparison, dict):
        return [f"{route}: missing outcome comparison"]
    errors = [
        f"{route}: {field} is not true"
        for field in (
            "equal",
            "status_equal",
            "engine_equal",
            "collected_equal",
            "collected_nodeids_equal",
            "ordered_nodeids_equal",
            "outcome_equal",
            "diagnostics_equal",
            "captured_output_equal",
            "case_warnings_equal",
            "session_warnings_equal",
        )
        if comparison.get(field) is not True
    ]
    if comparison.get("record_errors") != []:
        errors.append(f"{route}: execution records are missing or malformed")
    for prefix in ("stock", "native"):
        collected = comparison.get(f"{prefix}_collected")
        cases = comparison.get(f"{prefix}_case_count")
        if not positive_count(collected) or not positive_count(cases) or cases != collected:
            errors.append(f"{route}: {prefix} execution inventory is incomplete")
        status = comparison.get(f"{prefix}_exit_status")
        if type(status) is not int or status != 0:
            errors.append(f"{route}: {prefix} exit status is not zero")
    return errors


def parity_errors(record: dict[str, Any], expectation: ExpectedParity) -> list[str]:
    if record.get("status") != "parity":
        return [
            f"expected parity, got {record.get('status')!r}",
            *([str(record["error"])] if record.get("error") else []),
        ]
    errors = []
    for route in ("stock", "native", "native_workers2"):
        summary = record.get(route)
        if (
            not isinstance(summary, dict)
            or type(summary.get("exit_status")) is not int
            or summary.get("exit_status") != 0
        ):
            errors.append(f"{route}: missing successful execution")
            continue
        if not positive_count(summary.get("collected")) or not positive_count(summary.get("cases")):
            errors.append(f"{route}: missing nonempty execution inventory")
        if route != "stock" and summary.get("engine") != "pytest-runner-native-v1":
            errors.append(f"{route}: execution did not use the native engine")
    comparisons = record.get("outcome_comparisons")
    for route in ("native-default", "native-workers2"):
        comparison = comparisons.get(route) if isinstance(comparisons, dict) else None
        errors.extend(outcome_parity_errors(comparison, route))
    errors.extend(coverage_parity_errors(record.get("coverage"), expectation))
    coverage = record.get("coverage")
    junit = coverage.get("junit_cases") if isinstance(coverage, dict) else None
    stock = record.get("stock")
    expected_junit = stock.get("ordered_junit_cases") if isinstance(stock, dict) else None
    errors.extend(
        f"coverage {route}: JUnit inventory differs from stock outcome execution"
        for route in ("stock", "native", "native_workers2")
        if (
            not isinstance(expected_junit, list)
            or not expected_junit
            or not isinstance(junit, dict)
            or junit.get(route) != expected_junit
        )
    )
    outcomes = coverage.get("junit_outcomes") if isinstance(coverage, dict) else None
    expected_outcomes = stock.get("ordered_junit_outcomes") if isinstance(stock, dict) else None
    errors.extend(
        f"coverage {route}: JUnit outcomes differ from stock outcome execution"
        for route in ("stock", "native", "native_workers2")
        if (
            not isinstance(expected_outcomes, list)
            or not expected_outcomes
            or not isinstance(outcomes, dict)
            or outcomes.get(route) != expected_outcomes
        )
    )
    return errors


def coverage_result_errors(result: object, route: str) -> list[str]:
    if not isinstance(result, dict):
        return [f"coverage {route}: missing report and data"]
    errors = []
    for field in ("normalized_json", "data"):
        payload = result.get(field)
        files = payload.get("files") if isinstance(payload, dict) else None
        if (
            not isinstance(files, dict)
            or not files
            or any(
                not isinstance(name, str) or not isinstance(value, dict) or not value
                for name, value in files.items()
            )
        ):
            errors.append(f"coverage {route}: {field} has missing or malformed files")
    report = result.get("normalized_json")
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("totals"), dict)
        or not report["totals"]
    ):
        errors.append(f"coverage {route}: missing report totals")
    data = result.get("data")
    if isinstance(data, dict):
        data_errors = coverage_data_errors(data, route)
        errors.extend(data_errors)
        if not data_errors and result.get("aggregate_data") != aggregate_coverage_data(data):
            errors.append(f"coverage {route}: aggregate data does not match retained schema-7 data")
    oracle = result.get("report_oracle")
    if (
        not isinstance(oracle, dict)
        or oracle.get("equal") is not True
        or "error" not in oracle
        or oracle.get("error") is not None
        or oracle.get("expected") != report
    ):
        errors.append(
            f"coverage {route}: same-run Coverage.py report comparison is incomplete or differs"
        )
    return errors


def coverage_context_errors(
    attributed: object, lines: object, contexts: object, route: str
) -> list[str]:
    if not isinstance(attributed, dict) or any(
        not isinstance(line, str)
        or not line.isascii()
        or not line.isdecimal()
        or not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) for name in names)
        for line, names in attributed.items()
    ):
        return [f"coverage {route}: malformed schema-7 context attribution"]
    errors = []
    if isinstance(lines, list) and set(attributed) != {str(line) for line in lines}:
        errors.append(f"coverage {route}: context attribution does not cover measured lines")
    if isinstance(contexts, list) and any(
        name not in contexts for names in attributed.values() for name in names
    ):
        errors.append(f"coverage {route}: attribution names an unmeasured context")
    return errors


def coverage_data_errors(data: dict[str, Any], route: str) -> list[str]:
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        return [f"coverage {route}: missing schema-7 source inventory"]
    errors = []
    contexts = data.get("measured_contexts")
    if (
        type(data.get("has_arcs")) is not bool
        or not isinstance(contexts, list)
        or not contexts
        or any(not isinstance(context, str) for context in contexts)
    ):
        errors.append(f"coverage {route}: malformed schema-7 mode or measured contexts")
    observed = False
    for value in files.values():
        if not isinstance(value, dict):
            errors.append(f"coverage {route}: malformed schema-7 file")
            continue
        lines = value.get("lines")
        arcs = value.get("arcs")
        attributed = value.get("contexts")
        if not isinstance(value.get("tracer"), str):
            errors.append(f"coverage {route}: malformed schema-7 file tracer")
        context_arcs = value.get("context_arcs")
        malformed_context_arcs = not isinstance(context_arcs, dict) or any(
            not isinstance(context, str)
            or not isinstance(contexts, list)
            or context not in contexts
            or not isinstance(values, list)
            or any(
                not isinstance(arc, (list, tuple))
                or len(arc) != 2
                or any(type(line) is not int for line in arc)
                for arc in values
            )
            for context, values in context_arcs.items()
        )
        if malformed_context_arcs:
            errors.append(f"coverage {route}: malformed schema-7 context arcs")
        if not isinstance(lines, list) or any(type(line) is not int or line < 0 for line in lines):
            errors.append(f"coverage {route}: malformed schema-7 lines")
        else:
            observed = observed or any(line > 0 for line in lines)
        if not isinstance(arcs, list) or any(
            not isinstance(arc, (list, tuple))
            or len(arc) != 2
            or any(type(line) is not int for line in arc)
            for arc in arcs
        ):
            errors.append(f"coverage {route}: malformed schema-7 arcs")
        elif not malformed_context_arcs and (
            {tuple(arc) for values in context_arcs.values() for arc in values}
            != {tuple(arc) for arc in arcs}
            or (not data.get("has_arcs") and context_arcs)
        ):
            errors.append(f"coverage {route}: context arcs do not cover measured arcs")
        errors.extend(coverage_context_errors(attributed, lines, contexts, route))
    if not observed:
        errors.append(f"coverage {route}: no measured source lines")
    return errors


def coverage_route_comparison_errors(
    stock: object, candidate: object, route: str, expectation: ExpectedParity
) -> list[str]:
    if not isinstance(stock, dict) or not isinstance(candidate, dict):
        return []
    errors = []
    for kind in ("normalized_json", "data"):
        stock_payload = stock.get(kind)
        candidate_payload = candidate.get(kind)
        if not isinstance(stock_payload, dict) or not isinstance(candidate_payload, dict):
            continue
        stock_files = stock_payload.get("files")
        candidate_files = candidate_payload.get("files")
        if (
            isinstance(stock_files, dict)
            and isinstance(candidate_files, dict)
            and set(stock_files) != set(candidate_files)
        ):
            errors.append(f"coverage {route}: source inventory differs from stock")
    compared = {
        "exact": ("normalized_json", "data"),
        "aggregate-only": ("normalized_json", "aggregate_data"),
        "same-run-report": (),
    }[expectation.coverage]
    if any(stock.get(field) != candidate.get(field) for field in compared):
        errors.append(f"coverage {route}: report or schema-7 data differs from stock")
    return errors


def coverage_parity_errors(coverage: object, expectation: ExpectedParity) -> list[str]:
    if not isinstance(coverage, dict):
        return ["missing coverage comparison"]
    errors = []
    if coverage.get("status") != "validated":
        errors.append(f"expected validated coverage, got {coverage.get('status')!r}")
    if (
        coverage.get("contract") != expectation.coverage
        or coverage.get("reason") != expectation.reason
    ):
        errors.append("coverage comparison scope does not match the reviewed project expectation")
    if coverage.get("source_files_equal") is not True:
        errors.append("coverage source inventory differs across routes")
    errors.extend(coverage_outcome_errors(coverage))
    for route in ("stock", "native", "native_workers2"):
        status = coverage.get(f"{route}_exit_status")
        if type(status) is not int or status != 0:
            errors.append(f"coverage {route}: exit status is not zero")
        errors.extend(coverage_result_errors(coverage.get(route), route))
        if coverage.get(f"{route}_record_errors") != []:
            errors.append(f"coverage {route}: execution records are missing or malformed")
    for route, prefix in (("native", ""), ("native_workers2", "native_workers2_")):
        fields = {
            "exact": ("normalized_json_equal", "schema7_data_equal"),
            "aggregate-only": ("normalized_json_equal", "aggregate_data_equal"),
            "same-run-report": (),
        }[expectation.coverage]
        errors.extend(
            f"coverage {route}: {field} is not true"
            for field in fields
            if coverage.get(f"{prefix}{field}") is not True
        )
        errors.extend(
            coverage_route_comparison_errors(
                coverage.get("stock"), coverage.get(route), route, expectation
            )
        )
    return errors


def coverage_outcome_errors(coverage: dict[str, Any]) -> list[str]:
    observations = coverage.get("junit_outcomes")
    inventories = coverage.get("junit_cases")
    stock_cases = observations.get("stock") if isinstance(observations, dict) else None
    errors = []
    for route in ("stock", "native", "native_workers2"):
        cases = observations.get(route) if isinstance(observations, dict) else None
        inventory = inventories.get(route) if isinstance(inventories, dict) else None
        if (
            not isinstance(cases, list)
            or not cases
            or any(
                not isinstance(case, dict)
                or not isinstance(case.get("classname"), str)
                or not isinstance(case.get("name"), str)
                or not isinstance(case.get("outcomes"), list)
                or any(
                    not isinstance(outcome, dict)
                    or not isinstance(outcome.get("outcome"), str)
                    or outcome.get("outcome") not in {"skipped", "failure", "error"}
                    or not isinstance(outcome.get("type"), str)
                    for outcome in case["outcomes"]
                )
                for case in cases
            )
            or [{"classname": case["classname"], "name": case["name"]} for case in cases]
            != inventory
        ):
            errors.append(f"coverage {route}: missing or malformed JUnit outcomes")
            continue
        if any(
            outcome["outcome"] in {"failure", "error"}
            for case in cases
            for outcome in case["outcomes"]
        ):
            errors.append(f"coverage {route}: JUnit reports an unsuccessful execution")
        if route != "stock" and cases != stock_cases:
            errors.append(f"coverage {route}: JUnit outcomes differ from stock coverage execution")
    return errors


def validate_matrix(records: object, selected: list[str]) -> dict[str, Any]:
    """Fail closed on absent projects, incomplete runs, and unexpected refusals."""
    errors = []
    verdicts = []
    if len(selected) != len(set(selected)):
        errors.append("selected projects contain duplicates")
    if not isinstance(records, list):
        return {"passed": False, "errors": ["project records must be a list"], "projects": []}
    projects = [record.get("project") for record in records if isinstance(record, dict)]
    if len(projects) != len(records) or projects != selected:
        errors.append("project records do not match the selected ordered inventory")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"project record {index} is malformed")
            continue
        key = record.get("project")
        expectation = PROJECT_EXPECTATIONS.get(key) if isinstance(key, str) else None
        if isinstance(expectation, ExpectedParity):
            failures = parity_errors(record, expectation)
            expected = "execution-parity"
        else:
            failures = ["project has no explicit expectation"]
            expected = "unconfigured"
        verdict = {"project": key, "expected": expected, "passed": not failures}
        if isinstance(expectation, ExpectedParity):
            verdict["coverage_contract"] = expectation.coverage
            verdict["coverage_reason"] = expectation.reason
        verdicts.append(verdict)
        errors.extend(f"{key}: {failure}" for failure in failures)
    if not selected:
        errors.append("no projects selected")
    return {"passed": not errors, "errors": errors, "projects": verdicts}


def optional_xdist_status(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int | None:
    """Dispatch explicit distributed workloads without changing the required matrix."""

    if args.xdist_project:
        if args.project or args.required_subset:
            parser.error("--xdist-project cannot be combined with --project or --required-subset")
        # The optional checker shares this module's preparation and comparison
        # helpers; import only after routing to avoid their module cycle.
        from validation.pytest_runner.check_native_xdist_adoption import (  # noqa: PLC0415
            main as xdist_main,
        )

        xdist_arguments = [
            "--wheel",
            str(args.wheel),
            "--build-manifest",
            str(args.build_manifest),
            "--output",
            str(args.output),
            "--python-version",
            args.python_version,
            "--timeout",
            str(args.timeout),
        ]
        for key in args.xdist_project:
            xdist_arguments.extend(("--project", key))
        for workers in args.xdist_workers or ():
            xdist_arguments.extend(("--workers", str(workers)))
        for phase in args.xdist_phase or ():
            xdist_arguments.extend(("--phase", phase))
        return xdist_main(xdist_arguments)
    if args.xdist_workers is not None or args.xdist_phase is not None:
        parser.error("--xdist-workers and --xdist-phase require --xdist-project")
    return None


def validate_source_revision(build_manifest: dict[str, Any]) -> str:
    """Require the candidate manifest to identify one full source revision."""
    revision = build_manifest.get("source_revision")
    if (
        not isinstance(revision, str)
        or len(revision) != 40
        or any(character not in "0123456789abcdef" for character in revision)
    ):
        raise ValueError("candidate build manifest must record a full source revision")
    return revision


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python-version", default="3.14")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--project", choices=sorted(PROJECTS_BY_KEY), action="append")
    parser.add_argument("--required-subset", action="store_true")
    parser.add_argument(
        "--xdist-project", choices=("black", "chardet", "requests-cache"), action="append"
    )
    parser.add_argument("--xdist-workers", type=int, action="append")
    parser.add_argument("--xdist-phase", choices=("outcomes", "coverage"), action="append")
    args = parser.parse_args()
    xdist_status = optional_xdist_status(parser, args)
    if xdist_status is not None:
        return xdist_status
    if args.required_subset and args.project:
        parser.error("--required-subset cannot be combined with --project")
    selected = (
        list(REQUIRED_PROJECTS) if args.required_subset else args.project or list(PROJECTS_BY_KEY)
    )
    source_inputs = {
        **SOURCE_INPUT_SHA256,
        **{
            str(lock.relative_to(ROOT)): file_sha256(lock)
            for key in selected
            for lock in (
                PROJECTS_BY_KEY[key].dependency_lock
                or ROOT / "validation/pytest_runner/native-adoption-requirements.txt",
            )
        },
    }
    if len(selected) != len(set(selected)):
        parser.error("--project selections must be unique")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    wheel = args.wheel.resolve()
    build_manifest = json.loads(args.build_manifest.read_text(encoding="utf-8"))
    if build_manifest.get("wheel_sha256") != file_sha256(wheel):
        raise ValueError("candidate wheel hash differs from its build manifest")
    if build_manifest.get("native_profile") != "release":
        raise ValueError("candidate build manifest must record the release profile")
    revision = validate_source_revision(build_manifest)
    if args.required_subset and (
        revision != git(ROOT, "rev-parse", "HEAD")
        or build_manifest.get("source_tree") != git(ROOT, "rev-parse", "HEAD^{tree}")
    ):
        raise ValueError("required subset candidate revision and tree must match this checkout")
    if args.required_subset and git(ROOT, "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("required subset source checkout must have no uncommitted files")
    records = []
    for key in selected:
        logger.info("[%s] preparing pinned checkout and runner environment", key)
        project = PROJECTS_BY_KEY[key]
        try:
            record = run_project(project, output, wheel, args.python_version, args.timeout)
        except Exception as error:  # noqa: BLE001 - retain setup and route failures as matrix rows.
            record = {
                "project": key,
                "repository": project.repository,
                "revision": project.revision,
                "scope": project.scope,
                "adoption_class": project.adoption_class,
                "status": "setup-failure",
                "error": f"{type(error).__name__}: {error}",
            }
        records.append(record)
        write_atomic(output / "partial-result.json", {"projects": records})
        logger.info(
            "[%s] %s", key, record.get("status") if isinstance(record, dict) else "malformed"
        )
    validation = validate_matrix(records, selected)
    changed_inputs = [
        name for name, digest in source_inputs.items() if file_sha256(ROOT / name) != digest
    ]
    if changed_inputs:
        validation["passed"] = False
        validation["errors"].extend(
            f"source input changed during execution: {name}" for name in changed_inputs
        )
    result = {
        "schema_version": 2,
        "candidate": {
            "source_revision": build_manifest.get("source_revision"),
            "source_tree": build_manifest.get("source_tree"),
            "wheel_filename": wheel.name,
            "wheel_sha256": file_sha256(wheel),
            "build_manifest": str(args.build_manifest.resolve()),
            "build_manifest_sha256": file_sha256(args.build_manifest),
            "build_profile": build_manifest.get("native_profile"),
        },
        "python_version": args.python_version,
        "pytest_version": "9.1.1",
        "method": (
            "stock pytest with autoload disabled versus public vfy test --no-cov; "
            "compare ordered phase records, warnings, status, then gate coverage on "
            "full outcome parity"
        ),
        "projects": records,
        "validation": validation,
        "harness_sha256": SOURCE_INPUT_SHA256[
            "validation/pytest_runner/check_native_adoption_matrix.py"
        ],
        "source_input_sha256": source_inputs,
    }
    write_atomic(output / "result.json", result)
    (output / "partial-result.json").unlink(missing_ok=True)
    logger.info("%s", output / "result.json")
    for error in validation["errors"]:
        logger.error("%s", error)
    return 0 if validation["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
