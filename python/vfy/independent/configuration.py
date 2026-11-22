"""Project independent pytest configuration once for either controller path."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from vfy.errors import PytestConfigurationError, VfyError

INDEPENDENT_FULL_REASON = "the requested workflow requires a complete test run"


def full_selection_message() -> str:
    """Describe the independent engine's full requested suite."""
    return (
        "Running the full suite\nKnown tests before collection: unknown\n"
        f"Note: {INDEPENDENT_FULL_REASON}"
    )


if TYPE_CHECKING:
    from vfy.native_types import PytestConfigPolicy, PytestConfigRecord


class PytestConfiguration(NamedTuple):
    """Effective configuration shared by all test controller routes."""

    projection: PytestConfigRecord
    effective_args: tuple[str, ...]
    version: str | None
    version_error: str | None
    policy: PytestConfigPolicy
    plugins: tuple[str, ...]
    coverage_enabled: bool


def resolve(arguments: list[str], invocation_dir: Path | None = None) -> PytestConfiguration:
    try:
        from vfy.native import (  # noqa: PLC0415 - report an unavailable adapter at resolution
            NativePytestConfiguration,
            NativeTestInvocation,
            _pytest,
        )
    except ImportError as exc:
        raise VfyError(
            "independent pytest cannot run this workflow: "
            "the independent pytest adapter is not installed in this Python environment"
        ) from exc

    try:
        projection = NativePytestConfiguration(
            arguments, str(invocation_dir or Path.cwd()), os.environ.get("PYTEST_ADDOPTS")
        ).projection()
    except _pytest.IndependentUsageError as exc:
        raise PytestConfigurationError(str(exc)) from exc
    plugins = (
        *NativeTestInvocation.plugin_names([*projection["configured_args"], *arguments]),
        *(name.strip() for name in os.environ.get("PYTEST_PLUGINS", "").split(",") if name.strip()),
    )
    effective_args = tuple(projection["effective_args"])
    return PytestConfiguration(
        projection,
        effective_args,
        "9.1.1",
        None,
        "toml_with_addopts_overrides",
        plugins,
        "--no-cov" not in effective_args,
    )


def validate(
    args: object, plugins: tuple[str, ...], xdist_requested: bool, reason: str | None
) -> None:
    """Apply the independent engine's shared pre-launch policy."""
    if reason:
        raise VfyError(f"independent pytest cannot run this workflow: {reason}")
    scheduling = getattr(args, "native_scheduling", None)
    if scheduling is not None and xdist_requested:
        raise VfyError("--native-scheduling cannot be combined with pytest-xdist; use --dist")
    if "vfy._pytest.native_pytest" in plugins:
        raise VfyError(
            "vfy manages its native pytest adapter; remove the explicit "
            "vfy._pytest.native_pytest plug-in request"
        )


def unavailability_reason() -> str | None:
    """Check platform and optional independent native adapter."""
    import sys  # noqa: PLC0415 - independent route only

    if not sys.platform.startswith(("linux", "darwin")):
        return "independent pytest is supported only on Linux and macOS"
    try:
        from vfy.native import _pytest  # noqa: PLC0415 - independent route only
    except ImportError:
        return "the independent pytest adapter is not installed in this Python environment"
    if not hasattr(_pytest, "NativeIndependentInvocation"):
        return "the independent pytest adapter is not installed in this Python environment"
    return None


def worker_arguments(
    pytest_args: list[str], workers: str | int | None, selected_count: int | None = None
) -> list[str]:
    """Apply the same independent worker policy to either controller route."""
    if workers is None:
        return pytest_args
    if workers in {"auto", "logical"}:
        import sys  # noqa: PLC0415 - automatic worker count only

        from vfy.workers import worker_count  # noqa: PLC0415 - automatic worker count only

        workers = worker_count(workers, sys.maxsize if selected_count is None else selected_count)
    return [*pytest_args, f"--workers={workers}"]
