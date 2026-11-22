"""Pinned pytest and native worker routes for parameter adapter differentials."""

from __future__ import annotations

from collections.abc import Mapping

from .vfy_project import CommandResult, VfyProject


def run_parameter_suite(
    project: VfyProject,
    route: str,
    *,
    env: Mapping[str, str | None],
) -> CommandResult:
    """Run stock pytest or deferred collection on two native case workers.

    Fixtures must assert activation themselves; a passing run does not
    prove that the optimized path ran.
    """
    if route == "pytest":
        return project.run_pytest("-q", load_xdist=False, env=env)
    if route != "native":
        raise ValueError(f"unknown parameter suite route: {route}")
    return project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=2",
        "--native-scheduling=cases",
        "--no-replay",
        "--no-cov",
        "-q",
        env=env,
    )
