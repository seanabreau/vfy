"""Prepare pinned project checkouts and isolated benchmark environments.

Resolve packages, build wheels, and check baselines before timing begins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

# Direct execution starts with benchmarks/ on sys.path rather than the checkout
# root. Use one package-qualified import path in both direct and module execution.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.benchmark_harness import benchmark_environment, git_value, run_checked
from benchmarks.project_manifest import PROJECTS, ProjectManifest, manifest

_SOURCE_INPUTS = (
    Path("Cargo.toml"),
    Path("Cargo.lock"),
    Path("pyproject.toml"),
    Path("rust-toolchain.toml"),
    Path("crates/vfy/Cargo.toml"),
)


def _source_input_hashes(root: Path) -> dict[str, str]:
    return {
        str(path): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in _SOURCE_INPUTS
    }


def _check_vfy_source(root: Path, revision: str, phase: str) -> None:
    if git_value(root, "rev-parse", "HEAD") != revision:
        raise RuntimeError(f"vfy source revision changed {phase}")
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
        text=True,
        env=benchmark_environment(),
        timeout=30,
    ).strip()
    if status:
        raise RuntimeError(f"vfy source changed {phase}")


def preparation_environment(destination: Path) -> dict[str, str]:
    """Keep uv project state inside the prepared destination."""

    environment = benchmark_environment()
    environment.pop("VIRTUAL_ENV", None)
    environment["UV_PROJECT_ENVIRONMENT"] = str(destination / ".uv-project")
    return environment


def checkout_project(project: ProjectManifest, destination: Path) -> Path:
    """Clone and detach ``destination`` at the manifest's immutable revision."""

    destination = destination.resolve()
    if destination.exists():
        if any(destination.iterdir()):
            raise FileExistsError(f"project destination is not empty: {destination}")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            "git",
            "clone",
            "--filter=blob:none",
            f"https://github.com/{project.repository}.git",
            str(destination),
        ],
        cwd=destination.parent,
        env=benchmark_environment(),
        timeout=600,
    )
    run_checked(
        ["git", "checkout", "--detach", project.revision],
        cwd=destination,
        env=benchmark_environment(),
        timeout=120,
    )
    return destination


def install_project(
    project: ProjectManifest,
    checkout: Path,
    *,
    python: str,
    constraints: Path | None = None,
) -> Path:
    """Install the manifest lock and checkout into an isolated environment."""

    lockfile = constraints or Path(__file__).resolve().parent / "locks" / f"{project.key}.txt"
    if not lockfile.is_file():
        raise FileNotFoundError(f"benchmark dependency lock is missing: {lockfile}")
    lockfile = lockfile.resolve()
    environment = preparation_environment(checkout.parent.parent)
    run_checked(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            python,
            "--require-hashes",
            str(lockfile),
        ],
        cwd=checkout,
        env=environment,
        timeout=1800,
    )
    # Keep the benchmark pointed at the pinned checkout, including ``src/``
    # layouts.  A regular wheel install would make pytest import a second copy
    # from site-packages, so source edits would not be observable by the
    # workflow.  The lock remains authoritative for dependencies; editable
    # installation contributes only the project itself.
    run_checked(
        [
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--editable",
            str(checkout),
            "--no-deps",
            "--no-build-isolation",
        ],
        cwd=checkout,
        env=environment,
        timeout=1800,
    )
    return lockfile


def create_environment(
    destination: Path,
    *,
    environment_path: Path | None = None,
    base_python: str = sys.executable,
) -> Path:
    """Create and return a project-local virtual environment interpreter."""

    environment = (environment_path or destination / "environment").resolve()
    if environment.exists() and any(environment.iterdir()):
        raise FileExistsError(f"environment destination is not empty: {environment}")
    environment.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        ["uv", "venv", "--python", base_python, str(environment)],
        cwd=destination,
        env=preparation_environment(destination),
        timeout=300,
    )
    interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not interpreter.is_file():
        raise RuntimeError(f"uv did not create the project interpreter: {interpreter}")
    return interpreter


def install_vfy(
    interpreter: Path,
    destination: Path,
    *,
    vfy_root: Path,
) -> tuple[Path, str, dict[str, str]]:
    """Build and install a release vfy wheel before any timed command."""

    vfy_root = vfy_root.resolve()
    wheel_dir = destination / "wheels"
    if wheel_dir.exists() and any(wheel_dir.iterdir()):
        raise FileExistsError(f"wheel destination is not empty: {wheel_dir}")
    wheel_dir.mkdir(parents=True, exist_ok=True)
    source_status = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ],
        cwd=vfy_root,
        text=True,
        env=benchmark_environment(),
        timeout=30,
    ).strip()
    if source_status:
        raise RuntimeError("vfy source must be clean before the release benchmark build")
    source_revision = git_value(vfy_root, "rev-parse", "HEAD")
    if source_revision is None:
        raise RuntimeError(f"vfy source is not a Git checkout: {vfy_root}")
    source_inputs = _source_input_hashes(vfy_root)
    environment = preparation_environment(destination)
    target_dir = destination / "cargo-target"
    if target_dir.exists():
        raise FileExistsError(f"build target already exists: {target_dir}")
    maturin = shutil.which("maturin")
    if maturin is None:
        candidate = Path(sys.executable).with_name("maturin")
        maturin = str(candidate) if candidate.is_file() else None
    if maturin is None:
        raise RuntimeError("maturin is required to prepare a release vfy environment")
    run_checked(
        [
            maturin,
            "build",
            "--release",
            "--locked",
            "--interpreter",
            str(interpreter),
            "--target-dir",
            str(target_dir),
            "--out",
            str(wheel_dir),
            "--manifest-path",
            str(vfy_root / "crates/vfy/Cargo.toml"),
        ],
        cwd=vfy_root,
        env=environment,
        timeout=1800,
    )
    wheels = sorted(wheel_dir.glob("vfy-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"release build must produce exactly one vfy wheel in {wheel_dir}, found {len(wheels)}"
        )
    wheel = wheels[0]
    _check_vfy_source(vfy_root, source_revision, "during the release benchmark build")
    if _source_input_hashes(vfy_root) != source_inputs:
        raise RuntimeError("vfy build inputs changed during the release benchmark build")
    run_checked(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(interpreter),
            "--force-reinstall",
            "--no-deps",
            str(wheel),
        ],
        cwd=destination,
        env=environment,
        timeout=600,
    )
    return wheel, source_revision, source_inputs


def write_prepared_build_manifest(
    destination: Path,
    interpreter: Path,
    vfy_root: Path,
    wheel: Path,
    source_revision: str,
    source_inputs: dict[str, str],
) -> Path:
    """Bind the selected interpreter, release wheel, source revision, and inputs."""

    _check_vfy_source(vfy_root, source_revision, "after the release build")
    if _source_input_hashes(vfy_root) != source_inputs:
        raise RuntimeError("vfy build inputs changed after the release build")
    manifest_path = destination / "vfy-build.json"
    build_command = (
        "maturin",
        "build",
        "--release",
        "--locked",
        "--interpreter",
        str(interpreter),
        "--target-dir",
        str(destination / "cargo-target"),
        "--out",
        str(destination / "wheels"),
        "--manifest-path",
        str(vfy_root / "crates/vfy/Cargo.toml"),
    )
    environment = preparation_environment(destination)
    pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(vfy_root) if not pythonpath else str(vfy_root) + os.pathsep + pythonpath
    )
    script = (
        "from pathlib import Path; "
        "import sys; "
        "project=Path(sys.argv[2]).resolve(); "
        "source=Path(sys.argv[3]).resolve(); "
        "sys.path=[str(source), *[p for p in sys.path if not p or "
        "Path(p).resolve() != project]]; "
        "from benchmarks.benchmark_provenance import write_build_manifest; "
        "write_build_manifest(Path(sys.argv[1]), source_root=source, "
        "source_revision=sys.argv[4], wheel=Path(sys.argv[5]), "
        "build_command=tuple(sys.argv[6:]))"
    )
    with TemporaryDirectory(prefix=".vfy-build-", dir=destination) as pending_directory:
        pending_path = Path(pending_directory) / "vfy-build.json"
        run_checked(
            [
                str(interpreter),
                "-c",
                script,
                str(pending_path),
                str(destination / "project"),
                str(vfy_root),
                source_revision,
                str(wheel),
                *build_command,
            ],
            cwd=vfy_root,
            env=environment,
            timeout=120,
        )
        record = json.loads(pending_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise RuntimeError(f"build manifest is not an object: {pending_path}")
        _check_vfy_source(vfy_root, source_revision, "before publishing the build manifest")
        if _source_input_hashes(vfy_root) != source_inputs:
            raise RuntimeError("vfy build inputs changed before publishing the build manifest")
        record["source_inputs"] = source_inputs
        pending_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        pending_path.replace(manifest_path)
    return manifest_path


def record_environment(
    project: ProjectManifest,
    checkout: Path,
    destination: Path,
    *,
    python: str = sys.executable,
    lockfile: Path | None = None,
    build_manifest: Path | None = None,
) -> Path:
    """Write manifest, revision, and resolved package records beside a checkout."""

    destination.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        text=True,
        env=preparation_environment(destination),
        timeout=30,
    ).strip()
    freeze = subprocess.check_output(
        ["uv", "pip", "freeze", "--python", python],
        cwd=checkout,
        text=True,
        env=preparation_environment(destination),
        timeout=120,
    )
    (destination / "requirements.txt").write_text(freeze, encoding="utf-8")
    default_lock = Path(__file__).resolve().parent / "locks" / f"{project.key}.txt"
    lockfile = lockfile or default_lock
    lockfile = lockfile.resolve()
    lock_digest = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    manifest_path = destination / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": project.key,
                "repository": project.repository,
                "revision": project.revision,
                "checked_out_revision": revision,
                "dependency_lock": (
                    project.dependency_lock if lockfile == default_lock else str(lockfile)
                ),
                "dependency_lock_sha256": lock_digest,
                "vfy_build_manifest": str(build_manifest.resolve()) if build_manifest else None,
                "requirements": list(project.requirements),
                "python": str(Path(python).absolute()),
                "parallel_args": list(project.parallel_args),
                "serial_args": list(project.serial_args),
                "plugins": list(project.plugins),
                "workers": project.workers,
                "serial_workers": project.serial_workers,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def prepare(
    project: ProjectManifest,
    destination: Path,
    *,
    environment: Path | None = None,
    metadata: Path | None = None,
    python: str = sys.executable,
    constraints: Path | None = None,
    vfy_root: Path | None = None,
) -> Path:
    """Prepare a checkout and environment, returning its metadata path."""

    destination = destination.resolve()
    environment_path = (environment or destination / "environment").resolve()
    metadata_path = (metadata or destination / "artifacts").resolve()
    checkout = checkout_project(project, destination / "project")
    interpreter = create_environment(
        destination, environment_path=environment_path, base_python=python
    )
    lockfile = install_project(project, checkout, python=str(interpreter), constraints=constraints)
    wheel, source_revision, source_inputs = install_vfy(
        interpreter,
        destination,
        vfy_root=vfy_root or Path(__file__).resolve().parents[1],
    )
    source_root = (vfy_root or Path(__file__).resolve().parents[1]).resolve()
    build_manifest = write_prepared_build_manifest(
        destination, interpreter, source_root, wheel, source_revision, source_inputs
    )
    return record_environment(
        project,
        checkout,
        metadata_path,
        python=str(interpreter),
        lockfile=lockfile,
        build_manifest=build_manifest,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=sorted(PROJECTS))
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--environment",
        type=Path,
        help="virtual environment destination (default: DESTINATION/environment)",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        help="metadata destination (default: DESTINATION/artifacts)",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--constraints", type=Path)
    parser.add_argument("--vfy-root", type=Path)
    args = parser.parse_args()
    selected = manifest(args.project)
    metadata = prepare(
        selected,
        args.destination,
        environment=args.environment,
        metadata=args.metadata,
        python=args.python,
        constraints=args.constraints,
        vfy_root=args.vfy_root,
    )
    print(metadata)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
