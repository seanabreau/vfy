"""Prepare test command arguments once through the Rust parser for every controller route."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    import argparse

    from .independent.configuration import PytestConfiguration
    from .native import NativeTestInvocation
    from .native_types import TestInvocationPlan


class PreparedInvocation(NamedTuple):
    """Rust-prepared invocation passed between controller routes."""

    owner: NativeTestInvocation
    configuration: PytestConfiguration
    plan: TestInvocationPlan


def start(args: argparse.Namespace) -> NativeTestInvocation:
    from .native import NativeTestInvocation  # noqa: PLC0415 - execution only

    raw_targets = args.target
    targets = (
        [raw_targets]
        if isinstance(raw_targets, str) and raw_targets
        else ([] if isinstance(raw_targets, str) else list(raw_targets or []))
    )
    location = None
    if len(targets) == 1 and ":" in targets[0]:
        path, line = targets[0].rsplit(":", 1)
        if path and line.isdigit() and int(line) > 0:
            location = targets[0]
    return NativeTestInvocation.start(
        targets,
        location,
        list(args.pytest_args),
        list(getattr(args, "_watch_original_pytest_args", args.pytest_args)),
        getattr(args, "rcfile", None),
    )


def pytest_option(arguments: Sequence[str], name: str) -> str | None:
    """Find the last explicit value of a pytest option."""
    for index in range(len(arguments) - 1, -1, -1):
        argument = arguments[index]
        if argument.startswith(f"{name}="):
            return argument.partition("=")[2]
        if argument == name and index + 1 < len(arguments):
            return arguments[index + 1]
    return None


def finish(
    args: argparse.Namespace,
    owner: NativeTestInvocation,
    configuration: PytestConfiguration,
    *,
    force_full: bool,
    native_xdist_requested: bool,
) -> TestInvocationPlan:
    """Finish the single native plan using the resolved effective configuration."""
    import os  # noqa: PLC0415 - execution only

    from .errors import VfyError  # noqa: PLC0415 - execution only

    effective = configuration.effective_args
    explicit_distribution = pytest_option(effective, "--dist")
    try:
        plan = owner.finish(
            {
                "engine": getattr(args, "engine", "native"),
                "mode": args.mode,
                "force_full": force_full,
                "fresh": args.fresh,
                "no_replay": args.no_replay,
                "workers": args.workers,
                "independent_workers_configured": getattr(
                    args, "_independent_workers_configured", False
                ),
                "native_xdist_requested": native_xdist_requested,
                "scheduling": getattr(args, "native_scheduling", None),
                "env_scheduling": os.environ.get("VFY_NATIVE_SCHEDULING"),
                "explicit_distribution": explicit_distribution,
                "invocation_location": configuration.projection.get("invocation_location")
                is not None,
                "filtered_scope": configuration.projection["filtered_scope"],
                "coverage_enabled": configuration.coverage_enabled,
                "cov_append": "--cov-append" in effective,
            }
        )
    except ValueError as exc:
        raise VfyError(str(exc)) from exc
    args.mode = plan["mode"]
    args.fresh = plan["fresh"]
    return plan
