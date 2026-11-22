"""Reproducibility records shared by benchmark workloads and comparisons."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import sysconfig
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from benchmarks.benchmark_harness import git_value

type JSONValue = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None

BUILD_MANIFEST_ENV = "VFY_BENCHMARK_BUILD_MANIFEST"


@dataclass(frozen=True)
class SourceState:
    """Git revision and whether the worktree has uncommitted files."""

    revision: str
    dirty: bool

    def as_dict(self) -> dict[str, object]:
        return {"revision": self.revision, "dirty": self.dirty}


def source_state(root: Path) -> SourceState:
    """Capture the commit and uncommitted source used by a benchmark result."""

    revision = git_value(root, "rev-parse", "HEAD")
    if revision is None:
        raise RuntimeError(f"{root} is not a git checkout")
    status = git_value(root, "status", "--porcelain", "--untracked-files=normal")
    if status is None:
        raise RuntimeError(f"could not inspect source files in {root}")
    return SourceState(revision, bool(status))


def require_revision(root: Path, revision: str) -> None:
    """Reject a checkout whose HEAD differs from the recorded revision."""

    current = git_value(root, "rev-parse", "HEAD")
    if current != revision:
        raise RuntimeError(f"source revision changed during benchmark: {revision} -> {current}")


def require_clean(root: Path) -> SourceState:
    """Require committed source, allowing ignored build and evidence files."""

    state = source_state(root)
    if state.dirty:
        raise RuntimeError("source files must be committed before a publication benchmark")
    return state


def hardware_details() -> dict[str, str | int | bool | None]:
    """Collect portable machine details without making them a timing dependency."""

    gil_probe = getattr(sys, "_is_gil_enabled", None)
    if callable(gil_probe):
        python_gil = "enabled" if gil_probe() else "disabled"
    elif sysconfig.get_config_var("Py_GIL_DISABLED"):
        python_gil = "disabled"
    else:
        # Older CPython versions lack the probe; an absent free-threading
        # build flag identifies a GIL-enabled interpreter.
        python_gil = "enabled"
    debug_value = sysconfig.get_config_var("Py_DEBUG")
    python_debug = bool(debug_value) if debug_value is not None else None
    python_build = " ".join(part for part in platform.python_build() if part)
    python_abi = sysconfig.get_config_var("SOABI")

    details: dict[str, str | int | bool | None] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu": platform.processor() or None,
        "logical_cpus": os.cpu_count(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_build": python_build or None,
        "python_abi": str(python_abi) if python_abi else None,
        "python_debug": python_debug,
        "python_gil": python_gil,
        "memory_bytes": None,
    }
    try:
        if sys.platform == "darwin":
            details["cpu"] = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
            ).strip()
            details["memory_bytes"] = int(
                subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
            )
        elif sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    details["cpu"] = line.partition(":")[2].strip()
                    break
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    details["memory_bytes"] = int(line.split()[1]) * 1024
                    break
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return details


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wheel_native_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith((".so", ".pyd", ".dylib")):
                digest = hashlib.sha256(archive.read(name)).hexdigest()
                hashes.add(digest)
    return hashes


def _native_module_path(package: str) -> Path | None:
    try:
        module = importlib.import_module(f"{package}._native")
        value = getattr(module, "__file__", None)
    except (ImportError, OSError):
        return None
    return Path(value).resolve() if isinstance(value, str) else None


def write_build_manifest(
    path: Path,
    *,
    source_root: Path,
    source_revision: str,
    wheel: Path,
    package: str = "vfy",
    build_command: tuple[str, ...] = (),
) -> dict[str, object]:
    """Record hashes captured immediately after a release wheel is built.

    Bind the source revision to hashes of the wheel and imported extension.
    Consumers must verify this manifest: the surrounding checkout does not
    identify the source used to build an installed extension.
    """

    source_root = source_root.resolve()
    wheel = wheel.resolve()
    if len(source_revision) != 40 or not all(c in "0123456789abcdef" for c in source_revision):
        raise ValueError("build manifests require a full source revision")
    if not wheel.is_file():
        raise FileNotFoundError(wheel)
    module_path = _native_module_path(package)
    if module_path is None or not module_path.is_file():
        raise RuntimeError("release build did not provide an importable native artifact")
    extension_hash = _file_sha256(module_path)
    if extension_hash not in _wheel_native_hashes(wheel):
        raise RuntimeError("installed native artifact is not present in the release wheel")
    record: dict[str, object] = {
        "schema": 1,
        "package": package,
        "source_root": str(source_root),
        "source_revision": source_revision,
        "native_profile": "release",
        "extension_path": str(module_path),
        "extension_sha256": extension_hash,
        "wheel_path": str(wheel),
        "wheel_sha256": _file_sha256(wheel),
        "build_command": list(build_command),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def _read_build_manifest() -> tuple[Path, dict[str, object]] | None:
    raw_path = os.environ.get(BUILD_MANIFEST_ENV)
    if not raw_path:
        return None
    path = Path(raw_path).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError(f"invalid benchmark build manifest: {path}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"benchmark build manifest is not an object: {path}")
    record: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeError(f"benchmark build manifest has a non-string key: {path}")
        record[key] = item
    return path, record


def _verify_build_manifest(
    manifest_item: tuple[Path, dict[str, object]] | None,
    *,
    extension: Path | None,
    source_root: Path | None,
    source_revision: str | None,
    profile: str | None,
) -> dict[str, str | None]:
    if manifest_item is None:
        raise RuntimeError(
            f"publication requires {BUILD_MANIFEST_ENV} with verified wheel provenance"
        )
    manifest_path, manifest = manifest_item
    expected_root = str(source_root.resolve()) if source_root is not None else None
    expected_extension_hash = _file_sha256(extension) if extension is not None else None
    wheel_path_value = manifest.get("wheel_path")
    wheel_path = Path(wheel_path_value).resolve() if isinstance(wheel_path_value, str) else None
    required_strings = ("source_root", "source_revision", "native_profile", "extension_sha256")
    if any(not isinstance(manifest.get(key), str) for key in required_strings):
        raise RuntimeError(f"benchmark build manifest is incomplete: {manifest_path}")
    if manifest.get("native_profile") != "release":
        raise RuntimeError("publication requires a release build manifest")
    if expected_root is not None and manifest.get("source_root") != expected_root:
        raise RuntimeError("build manifest source root does not match benchmark source")
    if source_revision is not None and manifest.get("source_revision") != source_revision:
        raise RuntimeError("build manifest revision does not match benchmark source")
    if profile != manifest.get("native_profile"):
        raise RuntimeError("installed native profile does not match build manifest")
    if expected_extension_hash is None or expected_extension_hash != manifest.get(
        "extension_sha256"
    ):
        raise RuntimeError("installed native artifact does not match build manifest")
    if wheel_path is None or not wheel_path.is_file():
        raise RuntimeError("build manifest wheel is missing")
    wheel_hash = manifest.get("wheel_sha256")
    if not isinstance(wheel_hash, str) or _file_sha256(wheel_path) != wheel_hash:
        raise RuntimeError("wheel hash does not match build manifest")
    if expected_extension_hash not in _wheel_native_hashes(wheel_path):
        raise RuntimeError("wheel does not contain the installed native artifact")
    return {
        "path": str(manifest_path),
        "source_root": str(manifest["source_root"]),
        "source_revision": str(manifest["source_revision"]),
        "native_profile": str(manifest["native_profile"]),
        "extension_sha256": str(manifest["extension_sha256"]),
        "wheel_sha256": wheel_hash,
    }


def _verify_installed_wheel(wheel_path: Path) -> None:
    # Reuse the wheel inventory validator used by independent coverage.  Scan
    # the installed package roots so added files cannot hide outside RECORD.
    from validation.pytest_runner.benchmark_vfy_coverage import (  # noqa: PLC0415
        verify_installed_distribution_files,
    )

    distribution = importlib.metadata.distribution("vfy")
    files = distribution.files or ()
    metadata_roots = {
        str(item).split("/", 1)[0]
        for item in files
        if str(item).split("/", 1)[0].endswith(".dist-info")
    }
    if len(metadata_roots) != 1:
        raise RuntimeError("installed vfy has no unique metadata directory")
    metadata_root = next(iter(metadata_roots))
    installed_files: dict[str, str] = {}
    for root in ("vfy", metadata_root):
        base = Path(str(distribution.locate_file(root)))
        for path in base.rglob("*") if base.is_dir() else ():
            if (
                not path.is_file()
                or path.suffix == ".pyc"
                or "__pycache__" in path.parts
                or (
                    root == metadata_root
                    and path.name
                    in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}
                )
            ):
                continue
            installed_files[f"{root}/{path.relative_to(base).as_posix()}"] = _file_sha256(path)
    verify_installed_distribution_files(
        "publication", "vfy", {"vfy_files": installed_files}, wheel_path
    )


def artifact_provenance(
    package: str = "vfy",
    *,
    strict: bool = False,
    source_root: Path | None = None,
    source_revision: str | None = None,
) -> dict[str, str | None]:
    """Hash the installed extension and distribution metadata used by a run."""

    module_path = _native_module_path(package)

    distribution_hash: str | None = None
    try:
        distribution = importlib.metadata.distribution(package)
        digest = hashlib.sha256()
        root = Path(str(distribution.locate_file("."))).resolve()
        files = sorted(distribution.files or (), key=str)
        for relative in files:
            parts = Path(str(relative)).parts
            if (
                "__pycache__" in parts
                or str(relative).endswith(".pyc")
                or (
                    parts
                    and parts[0].endswith(".dist-info")
                    and Path(str(relative)).name
                    in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}
                )
            ):
                continue
            path = (root / relative).resolve()
            if path.is_file():
                digest.update(str(relative).encode("utf-8"))
                digest.update(path.read_bytes())
        distribution_hash = digest.hexdigest()
    except (importlib.metadata.PackageNotFoundError, OSError):
        pass

    profile: str | None
    try:
        profile_function = importlib.import_module("vfy.native").build_profile
        profile = profile_function()
    except (ImportError, AttributeError):
        profile = None
    try:
        extension_hash = _file_sha256(module_path) if module_path else None
    except OSError:
        extension_hash = None
    result = {
        "package": package,
        "extension_path": str(module_path) if module_path else None,
        "extension_sha256": extension_hash,
        "distribution_sha256": distribution_hash,
        "native_profile": profile,
        "source_root": None,
        "source_revision": None,
        "build_manifest": None,
    }
    manifest = _read_build_manifest()
    if strict:
        if module_path is None or distribution_hash is None or profile != "release":
            raise RuntimeError("publication requires a hashed release vfy artifact")
        verified = _verify_build_manifest(
            manifest,
            extension=module_path,
            source_root=source_root,
            source_revision=source_revision,
            profile=profile,
        )
        assert manifest is not None
        wheel_path = manifest[1].get("wheel_path")
        assert isinstance(wheel_path, str)
        _verify_installed_wheel(Path(wheel_path))
        result["source_root"] = verified["source_root"]
        result["source_revision"] = verified["source_revision"]
        result["build_manifest"] = verified["path"]
        result["wheel_sha256"] = verified["wheel_sha256"]
    elif manifest is not None:
        result["build_manifest"] = str(manifest[0])
        value = manifest[1].get("source_revision")
        if isinstance(value, str):
            result["source_revision"] = value
    return result


def tool_versions(packages: tuple[str, ...] = ()) -> dict[str, str | None]:
    """Return versions for the package set relevant to a workload."""

    result: dict[str, str | None] = {"python": sys.version.split()[0]}
    for package in packages:
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def require_unchanged_artifact(before: dict[str, str | None]) -> None:
    """Recheck the wheel and installed payload after a publication cohort."""
    if artifact_provenance(strict=True) != before:
        raise RuntimeError("publication artifact changed during measurement")


def _provenance_envelope(
    state: SourceState,
    *,
    workload: JSONValue,
    workers: int | None,
    cache: JSONValue,
    packages: tuple[str, ...],
    artifact: dict[str, object],
) -> dict[str, object]:
    """Construct the shared envelope after artifact validation."""

    result: dict[str, object] = {
        "source": state.as_dict(),
        "environment": {
            **hardware_details(),
            "tools": tool_versions(packages),
            "artifact": artifact,
        },
        "workload": workload,
        "workers": workers,
        "cache": cache,
        "validation": {"status": "passed"},
    }
    if isinstance(artifact.get("source_revision"), str):
        result["engine_source"] = {
            "revision": artifact["source_revision"],
            "root": artifact.get("source_root"),
        }
    return result


def build_provenance(
    root: Path,
    *,
    workload: JSONValue,
    workers: int | None = None,
    cache: JSONValue = None,
    packages: tuple[str, ...] = (),
    publication: bool = False,
) -> dict[str, object]:
    """Build the common record envelope used by retained benchmark results."""

    state = source_state(root)
    if publication and state.dirty:
        raise RuntimeError("publication provenance requires a clean tracked worktree")
    manifest_supplied = os.environ.get(BUILD_MANIFEST_ENV) is not None
    artifact = artifact_provenance(
        strict=publication,
        source_root=None if manifest_supplied else root,
        source_revision=None if manifest_supplied else state.revision,
    )
    return _provenance_envelope(
        state,
        workload=workload,
        workers=workers,
        cache=cache,
        packages=packages,
        artifact=dict(artifact),
    )


def _verify_prepared_project_artifact(
    build_manifest: Path, wheel: dict[str, object], source_revision: str
) -> Path:
    """Verify the release wheel binding consumed by a project result."""

    if not build_manifest.is_file():
        raise RuntimeError(f"prepared project build manifest is missing: {build_manifest}")
    try:
        build_record = json.loads(build_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"prepared project build manifest is invalid: {build_manifest}"
        ) from error
    if not isinstance(build_record, dict):
        raise RuntimeError(f"prepared project build manifest is not an object: {build_manifest}")
    if build_record.get("source_revision") != source_revision:
        raise RuntimeError("prepared build manifest revision does not match the benchmark result")
    if build_record.get("native_profile") != "release":
        raise RuntimeError("prepared build manifest is not a release artifact")
    if build_record.get("source_root") != wheel.get("source_root"):
        raise RuntimeError("prepared build manifest root does not match the wheel")
    if build_record.get("extension_sha256") != wheel.get("extension_sha256"):
        raise RuntimeError("prepared extension hash does not match its build manifest")
    if build_record.get("wheel_sha256") != wheel.get("wheel_sha256"):
        raise RuntimeError("prepared wheel hash does not match its build manifest")
    wheel_path_value = build_record.get("wheel_path")
    if not isinstance(wheel_path_value, str):
        raise RuntimeError("prepared build manifest has no wheel path")
    wheel_path = Path(wheel_path_value)
    if not wheel_path.is_file() or _file_sha256(wheel_path) != wheel.get("wheel_sha256"):
        raise RuntimeError("prepared wheel does not match its build manifest")
    return build_manifest


def prepared_project_provenance(
    root: Path,
    *,
    workload: JSONValue,
    workers: int | None,
    cache: JSONValue,
    packages: tuple[str, ...],
    selected: Mapping[str, object],
    prepared: Mapping[str, object],
    source_revision: str,
    upstream_revision: str,
    sampling: object,
    publication: bool,
    validation: str,
    build_manifest: Path | None = None,
) -> dict[str, object]:
    """Build one common envelope for a prepared external project workload."""

    wheel_value = prepared.get("vfy_wheel", prepared.get("vfy_build"))
    if not isinstance(wheel_value, dict):
        raise RuntimeError("prepared project metadata has no vfy build record")
    manifest_value = prepared.get("manifest")
    if not isinstance(manifest_value, str):
        raise RuntimeError("prepared project metadata has no preparation manifest")
    build_value = prepared.get("vfy_build_manifest")
    if build_value is not None and not isinstance(build_value, str):
        raise RuntimeError("preparation manifest has an invalid vfy build manifest path")
    build_manifest = build_manifest or (
        Path(build_value)
        if build_value is not None
        else Path(manifest_value).parent.parent / "vfy-build.json"
    )
    wheel = dict(wheel_value)
    if wheel.get("source_revision") != source_revision:
        raise RuntimeError("prepared wheel revision does not match the benchmark result")
    if wheel.get("native_profile") != "release":
        raise RuntimeError("prepared project benchmark requires a release vfy wheel")
    for key in ("extension_sha256", "wheel_sha256"):
        value = wheel.get(key)
        if not isinstance(value, str) or len(value) != 64:
            raise RuntimeError(f"prepared wheel has no valid {key}")
    if selected.get("extension_sha256") != wheel.get("extension_sha256"):
        raise RuntimeError("selected interpreter does not match the prepared extension")
    artifact = {
        "package": "vfy",
        "extension_path": selected.get("extension_path"),
        "extension_sha256": wheel.get("extension_sha256"),
        "distribution_sha256": None,
        "native_profile": wheel.get("native_profile"),
        "source_root": wheel.get("source_root"),
        "source_revision": source_revision,
        "build_manifest": str(build_manifest),
        "wheel_sha256": wheel.get("wheel_sha256"),
    }
    _verify_prepared_project_artifact(build_manifest, wheel, source_revision)
    state = source_state(root)
    if publication and state.dirty:
        raise RuntimeError("publication provenance requires a clean tracked worktree")
    provenance = _provenance_envelope(
        state,
        workload=workload,
        workers=workers,
        cache=cache,
        packages=packages,
        artifact=artifact,
    )
    environment = provenance["environment"]
    if not isinstance(environment, dict):
        raise RuntimeError("common provenance environment is malformed")
    tools = environment.get("tools")
    if not isinstance(tools, dict):
        raise RuntimeError("common provenance tools are malformed")
    tools.update(
        {
            "python": selected.get("python"),
            "vfy": selected.get("vfy"),
            "pytest-cov": selected.get("pytest_cov"),
        }
    )
    provenance.update(
        {
            "engine_source": {"revision": source_revision, "root": wheel.get("source_root")},
            "upstream_revision": upstream_revision,
            "interpreter": dict(selected),
            "vfy_build": wheel,
            "sampling": sampling,
            "validation": {"status": "passed", "evidence": validation},
        }
    )
    return provenance
