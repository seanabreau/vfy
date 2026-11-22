"""Resolve upstream test groups and extras into replayable benchmark locks."""

from __future__ import annotations

import argparse
import tempfile
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

from .benchmark_harness import benchmark_environment, git_value, run_checked
from .project_manifest import manifest

# These are the upstream environments exercised by the declared full-suite commands.
UPSTREAM_TEST_GROUPS = {
    "click": ("tests", None),
    "packaging": ("test", None),
    "pluggy": ("testing", None),
    "attrs": ("tests", None),
    "platformdirs": ("test", None),
    "black": ("tests", "d"),
    "datasette": ("dev", None),
    "fastapi": ("tests", "all"),
    "requests": ("test", "socks"),
    "rich": (None, None),
}


def regenerate(project: str, checkout: Path, output: Path) -> None:
    selected = manifest(project)
    checkout = checkout.resolve()
    if git_value(checkout, "rev-parse", "HEAD") != selected.revision:
        raise ValueError(f"{project}: checkout does not match the pinned manifest revision")
    if git_value(checkout, "status", "--porcelain", "--", "pyproject.toml", "uv.lock") != "":
        raise ValueError(f"{project}: upstream dependency metadata must be clean")
    metadata = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
    group, extra = UPSTREAM_TEST_GROUPS[project]
    requirements = [*selected.requirements, *metadata["build-system"]["requires"]]
    if project == "black":
        # Hatchling requests this through its PEP 660 hook for Black's editable mapping.
        requirements.append("editables")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vfy-lock-") as temporary:
        inputs = Path(temporary) / "harness.in"
        inputs.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        constraints = Path(temporary) / "upstream.txt"
        if group is not None and (checkout / "uv.lock").is_file():
            export = [
                "uv",
                "export",
                "--project",
                str(checkout),
                "--frozen",
                "--no-default-groups",
                "--group",
                group,
                "--no-emit-project",
                "--no-hashes",
                "--no-header",
                "--no-annotate",
                "--output-file",
                str(constraints),
            ]
            if extra is not None:
                export.extend(("--extra", extra))
            # Keep the benchmark's pinned oracle versions while preserving the
            # upstream lock for application dependencies and warning behavior.
            for requirement in requirements:
                export.extend(("--no-emit-package", Requirement(requirement).name))
            run_checked(
                export,
                cwd=Path(__file__).resolve().parents[1],
                env=benchmark_environment(),
                timeout=1800,
            )
        command = [
            "uv",
            "pip",
            "compile",
            str(checkout / "pyproject.toml"),
            str(inputs),
            *(("--group", f"{checkout / 'pyproject.toml'}:{group}") if group else ()),
            "--no-emit-package",
            project,
            "--universal",
            "--python-version",
            "3.12",
            "--generate-hashes",
            "--no-annotate",
            "--custom-compile-command",
            f"python -m benchmarks.lock_projects --project {project} --checkout PINNED_CHECKOUT",
            "--output-file",
            str(output),
        ]
        if extra is not None:
            command.extend(("--extra", extra))
        if constraints.is_file():
            command.extend(("--constraints", str(constraints)))
        run_checked(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=benchmark_environment(),
            timeout=1800,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=sorted(UPSTREAM_TEST_GROUPS), required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    args = parser.parse_args()
    regenerate(args.project, args.checkout, Path(__file__).parent / "locks" / f"{args.project}.txt")


if __name__ == "__main__":
    main()
