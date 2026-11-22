"""Run pytest and record ordered results and installed artifact identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from scripts.process_tree import kill_process_tree
    from scripts.pytest_execution_observations import normalize_text
elif __package__ == "scripts":
    from .process_tree import kill_process_tree
    from .pytest_execution_observations import normalize_text
else:
    from process_tree import kill_process_tree
    from pytest_execution_observations import normalize_text

PINNED_PYTEST_VERSION = "9.1.1"
PINNED_PYTEST_COMMIT = "cf470ec0bf7eb89cd97dd56df4859eae5db46447"
CORPUS_CONFTEST = Path(__file__).with_name("pytest_corpus_conftest.py")
CORPUS_SUBPROCESS_RUNPYTEST_NODEIDS = (
    "testing/test_junitxml.py::test_random_report_log_xdist",
    "testing/test_junitxml.py::test_runs_twice_xdist",
    "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info",
    "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_normal_count",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_verbose",
    "testing/test_terminal.py::TestProgressOutputStyle::test_xdist_times",
    "testing/test_terminal.py::TestProgressWithTeardown::test_xdist_normal",
)
CONTROLLED_ENVIRONMENT_KEYS = (
    "COVERAGE_PROCESS_START",
    "COVERAGE_RCFILE",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONWARNINGS",
    "PYTEST_ADDOPTS",
    "PYTEST_CURRENT_TEST",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYTEST_PLUGINS",
    "VFY_PYTEST_RECORD_INVENTORY_NODEIDS",
    "VFY_PYTEST_RECORD_INVENTORY_NODEIDS_SHA256",
    "NO_COLOR",
)


def controlled_environment(*, pythonpath: str | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    for key in CONTROLLED_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    if pythonpath is not None:
        environment["PYTHONPATH"] = pythonpath
    return environment


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return {str(key): item for key, item in value.items()}


def resolve_executable(value: Path) -> Path:
    resolved = shutil.which(str(value))
    path = (Path(resolved) if resolved is not None else value).expanduser()
    path = path.parent.resolve() / path.name
    if not path.is_file():
        raise FileNotFoundError(f"Python executable is missing: {value}")
    return path


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_records(paths: list[Path], kind: str) -> list[dict[str, str]]:
    if not paths:
        raise ValueError(f"at least one {kind} is required")
    records = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"{kind} is missing: {path}")
        records.append({"filename": path.name, "sha256": file_sha256(path)})
    filenames = [record["filename"] for record in records]
    if len(filenames) != len(set(filenames)):
        raise ValueError(f"{kind} filenames must be unique: {filenames}")
    return sorted(records, key=lambda record: (record["filename"], record["sha256"]))


def artifact_set_sha256(records: list[dict[str, str]]) -> str:
    canonical = json.dumps(records, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def normalized_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_identity(path: Path) -> tuple[str, str]:
    if path.suffix != ".whl":
        raise ValueError(f"runner artifact is not a wheel: {path}")
    with zipfile.ZipFile(path) as wheel:
        metadata_files = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ValueError(f"runner wheel has {len(metadata_files)} METADATA files: {path}")
        metadata = BytesParser().parsebytes(wheel.read(metadata_files[0]))
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ValueError(f"runner wheel has incomplete package identity: {path}")
    return name, version


def wheel_install_manifest(path: Path) -> list[dict[str, str]]:
    """Return the immutable installed files represented by a wheel."""
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"runner wheel contains duplicate archive paths: {path}")
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        wheel_files = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(metadata_files) != 1 or len(wheel_files) != 1:
            raise ValueError(f"runner wheel has invalid metadata layout: {path}")
        dist_info = metadata_files[0].rsplit("/", 1)[0]
        if wheel_files[0].rsplit("/", 1)[0] != dist_info:
            raise ValueError(f"runner wheel metadata directories do not match: {path}")
        wheel_metadata = BytesParser().parsebytes(wheel.read(wheel_files[0]))
        purelib = wheel_metadata.get("Root-Is-Purelib")
        if purelib not in {"true", "false"}:
            raise ValueError(f"runner wheel has invalid Root-Is-Purelib: {path}")
        root_scheme = "purelib" if purelib == "true" else "platlib"
        data_prefix = f"{dist_info.removesuffix('.dist-info')}.data"
        excluded = {
            f"{dist_info}/RECORD",
            f"{dist_info}/RECORD.jws",
            f"{dist_info}/RECORD.p7s",
        }
        records: list[dict[str, str]] = []
        destinations: set[tuple[str, str]] = set()
        for name in names:
            if name.endswith("/") or name in excluded:
                continue
            if "\\" in name:
                raise ValueError(f"runner wheel contains an unsafe path: {name}")
            archive_path = PurePosixPath(name)
            if archive_path.is_absolute() or ".." in archive_path.parts:
                raise ValueError(f"runner wheel contains an unsafe path: {name}")
            if archive_path.parts[0] == data_prefix:
                if len(archive_path.parts) < 3:
                    raise ValueError(f"runner wheel contains an invalid data path: {name}")
                scheme = archive_path.parts[1]
                if scheme not in {"purelib", "platlib"}:
                    raise ValueError(
                        f"runner wheel uses unsupported install scheme {scheme}: {name}"
                    )
                relative = PurePosixPath(*archive_path.parts[2:]).as_posix()
            else:
                if archive_path.parts[0].endswith(".data"):
                    raise ValueError(f"runner wheel contains an unexpected data path: {name}")
                scheme = root_scheme
                relative = archive_path.as_posix()
            destination = (scheme, relative)
            if destination in destinations:
                raise ValueError(f"runner wheel maps multiple files to {scheme}/{relative}")
            destinations.add(destination)
            records.append(
                {
                    "scheme": scheme,
                    "relative_path": relative,
                    "sha256": hashlib.sha256(wheel.read(name)).hexdigest(),
                }
            )
    return sorted(records, key=lambda record: (record["scheme"], record["relative_path"]))


def installed_artifacts(python: Path, manifests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    program = """\
import hashlib
import json
import sys
import sysconfig
from importlib.metadata import distribution
from pathlib import Path

result = {}
for manifest in json.load(sys.stdin):
    name = manifest["distribution"]
    package = distribution(name)
    direct_url = package.read_text("direct_url.json")
    files = []
    installed_paths = set()
    schemes = sysconfig.get_paths()
    for expected in manifest["files"]:
        path = Path(schemes[expected["scheme"]], expected["relative_path"])
        resolved = str(path.resolve())
        if resolved in installed_paths:
            raise RuntimeError(f"wheel files collide at {path}")
        installed_paths.add(resolved)
        if not path.exists():
            actual = "missing"
        elif not path.is_file():
            actual = "not-file"
        else:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({**expected, "installed_sha256": actual})
    result[name] = {
        "version": package.version,
        "direct_url": json.loads(direct_url) if direct_url else None,
        "files": files,
    }
print(json.dumps(result))
"""
    completed = subprocess.run(
        [str(python), "-c", program],
        input=json.dumps(manifests),
        check=False,
        capture_output=True,
        text=True,
        env=controlled_environment(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise ValueError(f"failed to inspect installed runner artifacts with {python}: {detail}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("runner interpreter returned invalid installation metadata") from error
    if not isinstance(value, dict):
        raise ValueError("runner interpreter returned invalid installation metadata")
    return value


def resolved_environment(python: Path) -> dict[str, Any]:
    program = """\
import json
import platform
import sysconfig
from importlib.metadata import distributions, entry_points

packages = sorted(
    ({"name": package.metadata["Name"], "version": package.version} for package in distributions()),
    key=lambda package: (package["name"].lower(), package["version"]),
)
print(json.dumps({
    "schema_version": 2,
    "python": platform.python_version(),
    "platform": sysconfig.get_platform(),
    "packages": packages,
    "pytest_plugins": sorted(
        ({
            "name": point.name,
            "value": point.value,
            "distribution": point.dist.name,
            "version": point.dist.version,
        } for point in entry_points(group="pytest11")),
        key=lambda point: (point["name"], point["value"], point["distribution"].lower()),
    ),
}))
"""
    completed = subprocess.run(
        [str(python), "-c", program],
        check=True,
        capture_output=True,
        text=True,
        env=controlled_environment(),
    )
    value = json.loads(completed.stdout)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 2
        or not isinstance(value.get("python"), str)
        or not isinstance(value.get("platform"), str)
        or not isinstance(value.get("packages"), list)
        or not isinstance(value.get("pytest_plugins"), list)
    ):
        raise ValueError("runner interpreter returned an invalid resolved environment")
    normalized_packages = []
    seen = set()
    for index, package in enumerate(value["packages"]):
        if (
            not isinstance(package, dict)
            or set(package) != {"name", "version"}
            or not all(isinstance(package[field], str) and package[field] for field in package)
        ):
            raise ValueError(f"runner environment package {index} is invalid")
        name = normalized_distribution(package["name"])
        if name in seen:
            raise ValueError(f"runner environment contains duplicate distribution {name}")
        seen.add(name)
        normalized_packages.append({"name": name, "version": package["version"]})
    value["packages"] = sorted(normalized_packages, key=lambda package: package["name"])
    normalized_plugins = []
    for index, plugin in enumerate(value["pytest_plugins"]):
        if (
            not isinstance(plugin, dict)
            or set(plugin) != {"name", "value", "distribution", "version"}
            or not all(isinstance(plugin[field], str) and plugin[field] for field in plugin)
        ):
            raise ValueError(f"runner environment pytest plug-in {index} is invalid")
        normalized_plugins.append(
            {
                **plugin,
                "distribution": normalized_distribution(plugin["distribution"]),
            }
        )
    value["pytest_plugins"] = sorted(
        normalized_plugins,
        key=lambda plugin: (plugin["name"], plugin["value"], plugin["distribution"]),
    )
    return value


def validate_dependency_lock(path: Path, environment: dict[str, Any]) -> None:
    lock = load_object(path)
    if lock != environment:
        raise ValueError(f"dependency lock does not match the invoked interpreter: {path}")


def verify_installed_artifacts(
    python: Path, artifacts: list[Path], runner_distribution: str
) -> list[dict[str, str]]:
    identities = [(path, *wheel_identity(path)) for path in artifacts]
    normalized = [normalized_distribution(name) for _, name, _ in identities]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"runner artifacts contain duplicate distributions: {normalized}")
    if normalized_distribution(runner_distribution) not in set(normalized):
        raise ValueError(f"runner artifacts do not contain {runner_distribution}")
    manifests = [
        {
            "distribution": name,
            "files": wheel_install_manifest(path),
        }
        for path, name, _ in identities
    ]
    installed = installed_artifacts(python, manifests)
    records = []
    for path, name, version in identities:
        metadata = installed.get(name)
        if not isinstance(metadata, dict) or metadata.get("version") != version:
            raise ValueError(f"installed {name} does not match {path.name}")
        direct_url = metadata.get("direct_url")
        url = direct_url.get("url") if isinstance(direct_url, dict) else None
        parsed = urlparse(url) if isinstance(url, str) else None
        installed_from = (
            Path(unquote(parsed.path)).resolve()
            if parsed is not None and parsed.scheme == "file" and not parsed.netloc
            else None
        )
        if installed_from != path.resolve():
            raise ValueError(
                f"installed {name} is not provenance-bound to {path}: {installed_from}"
            )
        installed_files = metadata.get("files")
        if not isinstance(installed_files, list):
            raise ValueError(f"installed {name} returned no payload evidence")
        mismatches = [
            row
            for row in installed_files
            if not isinstance(row, dict) or row.get("installed_sha256") != row.get("sha256")
        ]
        if mismatches:
            labels = [
                f"{row.get('scheme', '?')}/{row.get('relative_path', '?')}"
                if isinstance(row, dict)
                else "<malformed>"
                for row in mismatches[:3]
            ]
            raise ValueError(
                f"installed {name} payload differs from {path.name} at {labels} "
                f"({len(mismatches)} mismatches)"
            )
        records.append(
            {
                "distribution": name,
                "version": version,
                "filename": path.name,
                "sha256": file_sha256(path),
            }
        )
    return sorted(records, key=lambda record: normalized_distribution(record["distribution"]))


def git_output(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def validate_upstream(root: Path, classification: dict[str, Any]) -> str:
    expected = classification.get("inventory", {}).get("upstream_commit")
    if not isinstance(expected, str) or not expected:
        raise ValueError("classification has no pinned upstream commit")
    actual = git_output(root, "rev-parse", "HEAD")
    if actual != expected:
        raise ValueError(f"upstream checkout is {actual}, expected {expected}")
    if git_output(root, "status", "--porcelain"):
        raise ValueError("upstream checkout must be clean")
    ignored = git_output(root, "clean", "-ndx")
    if ignored:
        raise ValueError(f"upstream checkout must be pristine; generated files remain: {ignored}")
    return actual


def _validate_autoload(
    source: str,
    plugin_autoload: Literal["enabled", "disabled"] | None,
) -> Literal["enabled", "disabled"]:
    if plugin_autoload is None:
        plugin_autoload = "enabled" if source == "pytest" else "disabled"
    if plugin_autoload not in {"enabled", "disabled"}:
        raise ValueError(f"invalid pytest plug-in autoload mode: {plugin_autoload!r}")
    if source == "native" and plugin_autoload != "disabled":
        raise ValueError("native execution requires disabled pytest plug-in autoload")
    return plugin_autoload


def _pytester_control(
    mode: Literal["default", "inprocess-with-exceptions"],
    upstream_root: Path,
    arguments: list[str],
    ancestor_config_root: Path | None,
) -> tuple[str, list[str]]:
    config_sha256 = file_sha256(CORPUS_CONFTEST)
    if mode == "inprocess-with-exceptions":
        if ancestor_config_root is None:
            raise ValueError("the controlled inprocess mode requires an ancestor config root")
        ancestor_config_root = ancestor_config_root.resolve()
        if ancestor_config_root != upstream_root.resolve().parent:
            raise ValueError("the corpus config must be the upstream checkout's direct parent")
        config_path = ancestor_config_root / "conftest.py"
        if not config_path.is_file() or config_path.read_bytes() != CORPUS_CONFTEST.read_bytes():
            raise ValueError("the corpus ancestor conftest differs from the pinned template")
        confcutdir_argument = f"--confcutdir={ancestor_config_root}"
        if arguments.count("--runpytest=inprocess") != 1 or (
            arguments.count(confcutdir_argument) != 1
        ):
            raise ValueError(
                "the corpus pair must use inprocess mode and its exact ancestor config"
            )
    elif ancestor_config_root is not None:
        raise ValueError("the default pytester mode cannot install an ancestor config")
    canonical_arguments = [
        "--confcutdir=<CORPUS_PARENT>"
        if mode == "inprocess-with-exceptions"
        and ancestor_config_root is not None
        and argument == f"--confcutdir={ancestor_config_root}"
        else argument
        for argument in arguments
    ]
    return config_sha256, canonical_arguments


def _classification_inventory_nodeids(classification: dict[str, Any]) -> list[str]:
    rows = classification.get("classifications")
    if not isinstance(rows, list):
        raise ValueError("classification has invalid outer pytest node IDs")
    nodeids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("nodeid"), str):
            raise ValueError("classification has invalid outer pytest node IDs")
        nodeids.append(row["nodeid"])
    if len(nodeids) != len(set(nodeids)):
        raise ValueError("classification has duplicate outer pytest node IDs")
    inventory = classification.get("inventory")
    if not isinstance(inventory, dict):
        raise ValueError("classification has no inventory provenance")
    if inventory.get("upstream_commit") != PINNED_PYTEST_COMMIT:
        return []
    return nodeids


def project_identity(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()


def validate_native_identity(
    raw: dict[str, Any], observer: dict[str, Any], upstream_root: Path
) -> None:
    if (
        raw.get("observer_nonce") != observer.get("observer_nonce")
        or raw.get("session_depth") != 0
        or raw.get("rootdir") != str(upstream_root.resolve())
    ):
        raise ValueError("native execution record does not identify the outer pytest session")


def validate_runner_artifacts(
    source: str,
    runner_distribution: str,
    installations: list[dict[str, str]],
) -> None:
    installed = {
        normalized_distribution(record["distribution"]): record for record in installations
    }
    if source == "pytest":
        expected_artifact_sets = ({"pytest"}, {"pytest", "vfy"})
        expected_pytest_version = PINNED_PYTEST_VERSION
        expected_runner = "pytest"
    elif source == "native":
        expected_artifact_sets = ({"pytest", "vfy"},)
        expected_pytest_version = PINNED_PYTEST_VERSION
        expected_runner = "vfy"
    else:
        raise ValueError(f"unknown execution source: {source}")
    if set(installed) not in expected_artifact_sets:
        raise ValueError(
            f"{source} execution requires exactly one of these runner artifact sets: "
            f"{[sorted(artifacts) for artifacts in expected_artifact_sets]}"
        )
    pytest_artifact = installed.get("pytest")
    if pytest_artifact is None or pytest_artifact["version"] != expected_pytest_version:
        raise ValueError(
            f"execution requires a byte-verified pytest {expected_pytest_version} artifact"
        )
    runner = normalized_distribution(runner_distribution)
    if runner != expected_runner:
        raise ValueError(
            f"{source} execution requires {expected_runner} as the runner distribution"
        )


def interpreter_metadata(python: Path, distribution: str) -> dict[str, str]:
    program = (
        "import json,platform,sys,sysconfig; "
        "from importlib.metadata import version; "
        "print(json.dumps({'python': platform.python_version(), "
        "'platform': sysconfig.get_platform(), 'prefix': sys.prefix, "
        "'purelib': sysconfig.get_path('purelib'), "
        "'runner_version': version(" + repr(distribution) + ")}))"
    )
    completed = subprocess.run(
        [str(python), "-c", program],
        check=True,
        capture_output=True,
        text=True,
        env=controlled_environment(),
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or not all(
        isinstance(item, str) and item for item in value.values()
    ):
        raise ValueError("runner interpreter returned invalid metadata")
    return {str(key): item for key, item in value.items()}


def validate_pytest_origin(raw: dict[str, Any], purelib: str) -> None:
    origin = raw.get("pytest_origin")
    expected = (Path(purelib) / "pytest" / "__init__.py").resolve()
    if not isinstance(origin, str) or Path(origin).resolve() != expected:
        raise ValueError(f"execution imported pytest from {origin!r}, expected {expected}")


def validate_observer_identity(
    observer: dict[str, Any],
    *,
    pid: int,
    token: str,
    upstream_root: Path,
    purelib: str,
    inventory_nodeids_sha256: str,
    inventory_nodeid_count: int,
) -> None:
    if (
        observer.get("schema_version") != 2
        or observer.get("producer_pid") != pid
        or observer.get("session_token") != token
        or observer.get("session_depth") != 0
        or observer.get("rootdir") != str(upstream_root.resolve())
        or "pytest_temporary" not in observer
        or (
            observer["pytest_temporary"] is not None
            and (
                not isinstance(observer["pytest_temporary"], str)
                or not Path(observer["pytest_temporary"]).is_absolute()
            )
        )
        or not isinstance(observer.get("observer_nonce"), str)
        or len(observer["observer_nonce"]) != 64
        or not isinstance(observer.get("loaded_pytest_plugins"), list)
        or observer.get("inventory_nodeids_sha256") != inventory_nodeids_sha256
        or observer.get("inventory_nodeid_count") != inventory_nodeid_count
    ):
        raise ValueError("trusted observer did not identify the outer pytest session")
    validate_pytest_origin(observer, purelib)


def validate_native_imports(
    observer: dict[str, Any], purelib: str, wheel: Path
) -> list[dict[str, str]]:
    """Bind loaded adapter and extension bytes to the candidate wheel."""
    imports = observer.get("native_imports")
    required = {"vfy._native", "vfy._pytest.native_pytest"}
    if not isinstance(imports, list) or not imports:
        raise ValueError("native observer has incomplete import origins")
    manifest = {row["relative_path"]: row["sha256"] for row in wheel_install_manifest(wheel)}
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in imports:
        if not isinstance(row, dict) or set(row) != {"module", "origin", "sha256"}:
            raise ValueError("native observer has malformed import origins")
        if not isinstance(row["module"], str) or row["module"] in seen:
            raise ValueError("native observer has duplicate or unexpected import origins")
        seen.add(row["module"])
        if not isinstance(row["origin"], str):
            raise ValueError("native observer has invalid import origin")
        try:
            relative = Path(row["origin"]).resolve().relative_to(Path(purelib).resolve()).as_posix()
        except ValueError as error:
            raise ValueError("native imported outside the installed interpreter") from error
        if relative not in manifest or manifest[relative] != row["sha256"]:
            raise ValueError(f"native import differs from the candidate wheel: {relative}")
        module = row["module"]
        if (module != "vfy" and not module.startswith("vfy.")) or not all(
            part.isidentifier() for part in module.split(".")
        ):
            raise ValueError("native module has an unexpected import origin")
        expected = "vfy/_pytest/native_pytest.py"
        if row["module"] == "vfy._pytest.native_pytest" and relative != expected:
            raise ValueError("native adapter has an unexpected import origin")
        if row["module"] == "vfy._native" and (
            not relative.startswith("vfy/_native.")
            or "/" in relative.removeprefix("vfy/")
            or not relative.endswith((".so", ".pyd"))
        ):
            raise ValueError("native extension has an unexpected import origin")
        module_path = module.replace(".", "/")
        if module not in required and relative not in {
            f"{module_path}.py",
            f"{module_path}/__init__.py",
        }:
            raise ValueError("native module has an unexpected import origin")
        result.append({"module": row["module"], "relative_path": relative, "sha256": row["sha256"]})
    if not required <= seen:
        raise ValueError("native observer has incomplete import origins")
    if [row["module"] for row in result] != sorted(seen):
        raise ValueError("native observer import origins are not sorted by module")
    return result


def native_cases(
    raw: dict[str, Any],
    root: Path,
    temporary: str | None = None,
    runner: str | None = None,
    pytest_temporary: str | None = None,
    *,
    expected_engine: Literal[
        "pytest-runner-native-v1", "vfy-independent-v1"
    ] = "pytest-runner-native-v1",
) -> list[dict[str, Any]]:
    if raw.get("engine") != expected_engine:
        raise ValueError(f"candidate did not publish a {expected_engine} result")
    raw_cases = raw.get("cases")
    raw_warnings = raw.get("warnings")
    if not isinstance(raw_cases, list):
        raise ValueError("native result has no cases array")
    if not isinstance(raw_warnings, list):
        raise ValueError("native result has no warning observations")
    warnings_by_nodeid: dict[str, list[dict[str, object]]] = {}
    for index, warning in enumerate(raw_warnings):
        if (
            not isinstance(warning, dict)
            or not {
                "nodeid",
                "message",
                "filename",
                "lineno",
            }.issubset(warning)
            or set(warning) - {"nodeid", "message", "filename", "lineno", "category", "raw_message"}
        ):
            raise ValueError(f"native warning {index} is malformed")
        if any(
            key in warning and not isinstance(warning[key], str)
            for key in ("category", "raw_message")
        ):
            raise ValueError(f"native warning {index} has invalid structured metadata")
        nodeid = warning["nodeid"]
        lineno = warning["lineno"]
        if (
            not isinstance(nodeid, str)
            or not isinstance(lineno, int)
            or isinstance(lineno, bool)
            or lineno < 1
        ):
            raise ValueError(f"native warning {index} has invalid identity or location")
        warnings_by_nodeid.setdefault(nodeid, []).append(
            {
                "message": normalize_text(
                    warning["message"],
                    root,
                    temporary,
                    runner,
                    pytest_temporary=pytest_temporary,
                ),
                "filename": normalize_text(
                    warning["filename"],
                    root,
                    temporary,
                    runner,
                    pytest_temporary=pytest_temporary,
                ),
                "lineno": lineno,
            }
        )
    cases = []
    for case_index, case in enumerate(raw_cases):
        if not isinstance(case, dict) or not isinstance(case.get("nodeid"), str):
            raise ValueError(f"native case {case_index} is malformed")
        phases = case.get("phases")
        if not isinstance(phases, list) or not phases:
            raise ValueError(f"native case {case['nodeid']} has no phases")
        subtest_reports = _native_subtest_reports(
            case.get("subtests", []),
            case["nodeid"],
            root,
            temporary,
            runner,
            pytest_temporary,
        )
        reports = []
        for phase_index, phase in enumerate(phases):
            report = _native_phase_report(
                phase,
                case["nodeid"],
                phase_index,
                root,
                temporary,
                runner,
                pytest_temporary,
            )
            if report["phase"] == "call":
                # pytest emits each subtest report while the unittest call is
                # running, before the parent call report is logged.
                reports.extend(subtest_reports)
                subtest_reports = []
            reports.append(report)
        messages = warnings_by_nodeid.pop(case["nodeid"], [])
        cases.append({"nodeid": case["nodeid"], "reports": reports, "warnings": messages})
    return cases


def _native_phase_report(
    value: object,
    nodeid: str,
    index: int,
    root: Path,
    temporary: str | None,
    runner: str | None,
    pytest_temporary: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"native phase {index} for {nodeid} is malformed")
    phase = value.get("phase")
    if phase == "run":
        phase = "call"
    if "exception" not in value:
        raise ValueError(f"native phase {index} for {nodeid} has no canonical exception")
    exception = value["exception"]
    if exception is not None and not isinstance(exception, str):
        raise ValueError(f"native phase {index} for {nodeid} has invalid exception")
    outcome = value.get("outcome")
    not_run_xfail = (
        outcome == "xfailed"
        and phase == "setup"
        and isinstance(exception, str)
        and exception.startswith("[NOTRUN] ")
    )
    detail = exception if not_run_xfail else value.get("detail")
    if not isinstance(detail, str):
        raise ValueError(f"native phase {index} for {nodeid} has invalid diagnostic")
    for field in ("stdout", "stderr", "log"):
        if not isinstance(value.get(field), str):
            raise ValueError(f"native phase {index} for {nodeid} has invalid {field}")
    return {
        "phase": phase,
        "outcome": outcome,
        "exception": (
            normalize_text(
                exception,
                root,
                temporary,
                runner,
                pytest_temporary=pytest_temporary,
            )
            if exception is not None
            else None
        ),
        "diagnostic": None if outcome == "xpassed" else detail or None,
        "stdout": value["stdout"],
        "stderr": value["stderr"],
        "log": value["log"],
    }


def _native_subtest_reports(
    value: object,
    nodeid: str,
    root: Path,
    temporary: str | None,
    runner: str | None,
    pytest_temporary: str | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"native case {nodeid} has malformed subtests")
    reports = []
    for index, subtest in enumerate(value):
        required = {
            "outcome",
            "context",
            "exception",
            "detail",
        }
        if not isinstance(subtest, dict) or not required <= set(subtest) <= required | {
            "skip_location"
        }:
            raise ValueError(f"native subtest {index} for {nodeid} is malformed")
        location = subtest.get("skip_location")
        if location is not None and (
            not isinstance(location, dict)
            or set(location) != {"path", "line"}
            or not isinstance(location["path"], str)
            or type(location["line"]) is not int
            or location["line"] < 1
        ):
            raise ValueError(f"native subtest {index} for {nodeid} has invalid skip location")
        outcome = subtest["outcome"]
        context = subtest["context"]
        exception = subtest["exception"]
        detail = subtest["detail"]
        if (
            not isinstance(outcome, str)
            or outcome not in {"passed", "failed", "skipped", "xfailed", "xpassed"}
            or not isinstance(context, str)
            or not isinstance(detail, str)
            or (exception is not None and not isinstance(exception, str))
        ):
            raise ValueError(f"native subtest {index} for {nodeid} has invalid fields")
        reports.append(
            {
                "phase": "call",
                "outcome": outcome,
                "exception": (
                    normalize_text(
                        exception,
                        root,
                        temporary,
                        runner,
                        pytest_temporary=pytest_temporary,
                    )
                    if exception is not None
                    else None
                ),
                "diagnostic": detail or None,
                "stdout": "",
                "stderr": "",
                "log": "",
            }
        )
    return reports


def native_session_warnings(
    raw: dict[str, Any],
    cases: list[dict[str, Any]],
    root: Path,
    temporary: str | None = None,
    runner: str | None = None,
    pytest_temporary: str | None = None,
) -> list[dict[str, Any]]:
    case_nodeids = {case["nodeid"] for case in cases}
    return [
        {
            "nodeid": warning["nodeid"],
            "message": normalize_text(
                warning["message"],
                root,
                temporary,
                runner,
                pytest_temporary=pytest_temporary,
            ),
            "filename": normalize_text(
                warning["filename"],
                root,
                temporary,
                runner,
                pytest_temporary=pytest_temporary,
            ),
            "lineno": warning["lineno"],
        }
        for warning in raw["warnings"]
        if warning["nodeid"] not in case_nodeids
    ]


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("execution timeout must be finite and positive")


def run_record_command(
    command: list[str],
    *,
    root: Path,
    environment: dict[str, str],
    evidence: Path,
    timeout: float,
) -> tuple[int, int]:
    """Retain original output and terminate descendants on an incomplete run."""
    validate_timeout(timeout)
    write_atomic(evidence / "command.json", {"argv": command, "cwd": str(root), "timeout": timeout})
    with (
        (evidence / "stdout.txt").open("w", encoding="utf-8") as stdout,
        (evidence / "stderr.txt").open("w", encoding="utf-8") as stderr,
    ):
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=os.name != "nt",
        )
        try:
            status = process.wait(timeout=timeout)
        except BaseException as error:
            kill_process_tree(process)
            process.wait(timeout=10)
            write_atomic(
                evidence / "status.json",
                {"pid": process.pid, "completed": False, "error": type(error).__name__},
            )
            error.add_note(f"Execution evidence: {evidence}")
            raise
    write_atomic(
        evidence / "status.json", {"pid": process.pid, "completed": True, "exit_status": status}
    )
    return process.pid, status


def record_run(
    *,
    source: str,
    python: Path,
    upstream_root: Path,
    classification_path: Path,
    runner_distribution: str,
    runner_artifacts: list[Path],
    dependency_locks: list[Path],
    pytest_arguments: list[str],
    plugin_autoload: Literal["enabled", "disabled"] | None = None,
    pytester_mode: Literal["default", "inprocess-with-exceptions"] = "default",
    ancestor_config_root: Path | None = None,
    evidence_dir: Path | None = None,
    timeout: float = 3_600,
) -> dict[str, Any]:
    validate_timeout(timeout)
    plugin_autoload = _validate_autoload(source, plugin_autoload)
    if pytester_mode not in {"default", "inprocess-with-exceptions"}:
        raise ValueError(f"invalid pytester execution mode: {pytester_mode!r}")
    config_sha256, recorded_arguments = _pytester_control(
        pytester_mode, upstream_root, pytest_arguments, ancestor_config_root
    )
    classification = load_object(classification_path)
    inventory_nodeids = _classification_inventory_nodeids(classification)
    upstream_commit = validate_upstream(upstream_root, classification)
    artifact_rows = artifact_records(runner_artifacts, "runner artifact")
    installations = verify_installed_artifacts(python, runner_artifacts, runner_distribution)
    validate_runner_artifacts(source, runner_distribution, installations)
    if len(dependency_locks) != 1:
        raise ValueError("exactly one resolved dependency lock is required")
    resolved_env = resolved_environment(python)
    validate_dependency_lock(dependency_locks[0], resolved_env)
    lock_rows = artifact_records(dependency_locks, "dependency lock")
    harness_rows = artifact_records(
        [
            Path(__file__).resolve(),
            Path(__file__).with_name("process_tree.py").resolve(),
            Path(__file__).with_name("pytest_execution_probe.py").resolve(),
            Path(__file__).with_name("pytest_execution_observations.py").resolve(),
            CORPUS_CONFTEST.resolve(),
        ],
        "harness source",
    )
    inventory_sha256 = classification.get("inventory", {}).get("sha256")
    policy_sha256 = classification.get("policy", {}).get("sha256")
    if not all(isinstance(value, str) and value for value in (inventory_sha256, policy_sha256)):
        raise ValueError("classification has incomplete inventory or policy provenance")
    metadata = interpreter_metadata(python, runner_distribution)
    if evidence_dir is None:
        evidence = Path(tempfile.mkdtemp(prefix="vfy-pytest-record-"))
    else:
        evidence = evidence_dir.resolve()
        evidence.mkdir(parents=True, exist_ok=False)
    raw_path = evidence / "raw.json"
    observer_path = evidence / "observer.json"
    inventory_nodeids_path = evidence / "inventory-nodeids.json"
    inventory_nodeids_payload = json.dumps(inventory_nodeids, separators=(",", ":")) + "\n"
    inventory_nodeids_path.write_text(inventory_nodeids_payload, encoding="utf-8")
    inventory_nodeids_sha256 = hashlib.sha256(inventory_nodeids_payload.encode()).hexdigest()
    scripts = str(Path(__file__).resolve().parent)
    run_environment = controlled_environment(pythonpath=scripts)
    run_environment["VFY_PYTEST_RECORD_MODE"] = "pytest" if source == "pytest" else "native"
    run_environment["VFY_PYTEST_RECORD_ROOT"] = str(upstream_root)
    run_environment["VFY_PYTEST_RECORD_TEMP"] = tempfile.gettempdir()
    run_environment["VFY_PYTEST_RECORD_PREFIX"] = metadata["prefix"]
    run_environment["VFY_PYTEST_RECORD_PROBE"] = str(
        Path(__file__).with_name("pytest_execution_probe.py").resolve()
    )
    run_environment["VFY_PYTEST_OBSERVER_RESULT"] = str(observer_path)
    run_environment["VFY_PYTEST_RECORD_INVENTORY_NODEIDS"] = str(inventory_nodeids_path)
    run_environment["VFY_PYTEST_RECORD_INVENTORY_NODEIDS_SHA256"] = inventory_nodeids_sha256
    command = [str(python), "-P", "-m", "pytest", "-p", "pytest_execution_probe"]
    if plugin_autoload == "disabled":
        run_environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if source == "native":
        command.extend(("-p", "no:cacheprovider", "-p", "vfy._pytest.native_pytest"))
    run_environment["VFY_PYTEST_RECORD_RAW"] = str(raw_path)
    if source == "native":
        run_environment["PYTEST_NATIVE_RESULT"] = str(raw_path)
    session_token = secrets.token_hex(32)
    run_environment["VFY_PYTEST_RECORD_SESSION"] = session_token
    producer_pid, returncode = run_record_command(
        [*command, *pytest_arguments],
        root=upstream_root,
        environment=run_environment,
        evidence=evidence,
        timeout=timeout,
    )
    if not raw_path.is_file():
        raise RuntimeError(
            f"{source} runner exited {returncode} without publishing an execution record"
        )
    raw = load_object(raw_path)
    if not observer_path.is_file():
        raise RuntimeError(f"{source} runner exited {returncode} without trusted observer evidence")
    observer = load_object(observer_path)
    validate_observer_identity(
        observer,
        pid=producer_pid,
        token=session_token,
        upstream_root=upstream_root,
        purelib=metadata["purelib"],
        inventory_nodeids_sha256=inventory_nodeids_sha256,
        inventory_nodeid_count=len(inventory_nodeids),
    )
    pytest_temporary = observer["pytest_temporary"]
    if (
        raw.get("producer_pid") != producer_pid
        or raw.get("session_token") != session_token
        or raw.get("observer_nonce") != observer["observer_nonce"]
    ):
        raise ValueError("execution record came from a nested or unrelated pytest session")
    if source == "pytest":
        if (
            raw.get("schema_version") != 2
            or not isinstance(raw.get("cases"), list)
            or not isinstance(raw.get("session_warnings"), list)
        ):
            raise ValueError("pytest probe returned a malformed execution record")
        cases = raw["cases"]
        session_warnings = raw["session_warnings"]
        recorded_exit = raw.get("exit_status")
        if recorded_exit != returncode:
            raise ValueError(
                f"pytest probe exit status {recorded_exit!r} differs from process {returncode}"
            )
    else:
        validate_native_identity(raw, observer, upstream_root)
        temporary = tempfile.gettempdir()
        cases = native_cases(
            raw,
            upstream_root,
            temporary,
            metadata["prefix"],
            pytest_temporary,
        )
        session_warnings = native_session_warnings(
            raw, cases, upstream_root, temporary, metadata["prefix"], pytest_temporary
        )
    native_imports = []
    if source == "native":
        wheel = next(
            path
            for path in runner_artifacts
            if normalized_distribution(wheel_identity(path)[0]) == "vfy"
        )
        native_imports = validate_native_imports(observer, metadata["purelib"], wheel)
    result = {
        "schema_version": 2,
        "comparison": "outer-session-semantics",
        "provenance": {
            "upstream_commit": upstream_commit,
            "inventory_sha256": inventory_sha256,
            "policy_sha256": policy_sha256,
            "project_identity_sha256": project_identity(upstream_root),
            "python": metadata["python"],
            "platform": metadata["platform"],
            "runner_distribution": runner_distribution,
            "runner_version": metadata["runner_version"],
            "plugin_autoload": plugin_autoload,
            "pytest_arguments": recorded_arguments,
            "explicit_deselections": [
                argument.removeprefix("--deselect=")
                for argument in pytest_arguments
                if argument.startswith("--deselect=")
            ],
            "runner_artifact_set_sha256": artifact_set_sha256(artifact_rows),
            "runner_artifacts": artifact_rows,
            "installed_runner_artifacts": installations,
            "dependency_locks": lock_rows,
            "harness_artifacts": harness_rows,
            "resolved_environment_sha256": hashlib.sha256(
                json.dumps(resolved_env, separators=(",", ":"), sort_keys=True).encode()
            ).hexdigest(),
            "resolved_environment": resolved_env,
            "loaded_pytest_plugins": observer["loaded_pytest_plugins"],
            "native_imports": native_imports,
            "pytester_control": {
                "mode": pytester_mode,
                "config_template_sha256": config_sha256,
                "ancestor_config_applied": pytester_mode == "inprocess-with-exceptions",
                "subprocess_nodeids": (
                    list(CORPUS_SUBPROCESS_RUNPYTEST_NODEIDS)
                    if pytester_mode == "inprocess-with-exceptions"
                    else []
                ),
                "inventory_nodeids_sha256": inventory_nodeids_sha256,
                "inventory_nodeid_count": len(inventory_nodeids),
            },
        },
        "collected": raw.get("collected"),
        "exit_status": returncode,
        "cases": cases,
        "session_warnings": session_warnings,
    }
    write_atomic(evidence / "execution-record.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("pytest", "native"), required=True)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--upstream-root", required=True, type=Path)
    parser.add_argument("--classification", required=True, type=Path)
    parser.add_argument("--runner-distribution", required=True)
    parser.add_argument("--runner-artifact", action="append", required=True, type=Path)
    parser.add_argument("--dependency-lock", action="append", required=True, type=Path)
    parser.add_argument("--plugin-autoload", choices=("enabled", "disabled"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=3_600)
    parser.add_argument("pytest_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pytest_arguments = args.pytest_arguments
    if pytest_arguments[:1] == ["--"]:
        pytest_arguments = pytest_arguments[1:]
    record = record_run(
        source=args.source,
        python=resolve_executable(args.python),
        upstream_root=args.upstream_root.resolve(),
        classification_path=args.classification.resolve(),
        runner_distribution=args.runner_distribution,
        runner_artifacts=[path.resolve() for path in args.runner_artifact],
        dependency_locks=[path.resolve() for path in args.dependency_lock],
        pytest_arguments=pytest_arguments,
        plugin_autoload=args.plugin_autoload,
        evidence_dir=args.output.resolve().with_name(f"{args.output.stem}-evidence"),
        timeout=args.timeout,
    )
    write_atomic(args.output.resolve(), record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
