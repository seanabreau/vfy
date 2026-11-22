"""Resolve worker counts from the requested width and available host capacity."""

from __future__ import annotations

import os
from contextlib import suppress
from typing import TYPE_CHECKING

from .native import recommended_worker_count

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

XDIST_PLUGIN_NAMES = frozenset({"xdist", "xdist.plugin"})
XDIST_OPTION_NAMES = frozenset(
    {
        "--numprocesses",
        "--dist",
        "--tx",
        "--maxprocesses",
        "--maxschedchunk",
        "--max-worker-restart",
        "--testrunuid",
        "--rsyncdir",
        "--rsyncignore",
    }
)


def xdist_requested(arguments: Sequence[str], plugins: Iterable[str]) -> bool:
    """Detect upstream options or an explicit upstream plug-in load."""
    return bool(
        XDIST_PLUGIN_NAMES.intersection(plugins)
        or any(
            argument.startswith("-n") or argument.split("=", 1)[0] in XDIST_OPTION_NAMES
            for argument in arguments
        )
    )


def requested_worker_collection(
    workers: str | int | None,
    *,
    policy: str | None,
    worker_start: str | None,
    mutation_isolation: str | None,
) -> bool:
    """Resolve the requested collection route before pytest configuration."""
    if policy is not None:
        return policy == "1"
    width = str(workers)
    return (
        width.isdecimal()
        and int(width) >= 2
        and worker_start in {None, "exec"}
        and not mutation_isolation
    )


def worker_count(value: str | int, pending: int) -> int:
    """Resolve a requested width without starting idle worker processes."""

    if isinstance(value, int):
        capacity = value
    else:
        capacity = os.cpu_count() or 1
        if value == "auto":
            with suppress(AttributeError, OSError):
                affinity = getattr(os, "sched_getaffinity")  # noqa: B009 - optional on Windows
                capacity = len(affinity(0))
    return recommended_worker_count(pending, capacity)
