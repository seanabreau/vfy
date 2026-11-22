"""Shared benchmark sampling, subprocess, and statistics helpers."""

from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from scripts.process_tree import run_captured

if TYPE_CHECKING:
    from benchmarks.benchmark_types import ComparisonUncertainty, TimingSummary


ValueT = TypeVar("ValueT")


class BenchmarkMode(StrEnum):
    PUBLICATION = "publication"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class SamplingConfig:
    """Validated warmup and sample counts for one benchmark run."""

    warmups: int = 3
    runs: int = 15
    mode: BenchmarkMode = BenchmarkMode.PUBLICATION

    def __post_init__(self) -> None:
        if self.warmups < 0:
            raise ValueError("--warmups must be nonnegative")
        if self.runs < 1:
            raise ValueError("--runs must be positive")
        if self.mode is BenchmarkMode.PUBLICATION and (self.warmups < 3 or self.runs < 15):
            raise ValueError("publication benchmarks require at least 3 warmups and 15 runs")

    @property
    def publishable(self) -> bool:
        return self.mode is BenchmarkMode.PUBLICATION

    def as_dict(self) -> dict[str, int | str]:
        return {"mode": self.mode.value, "warmups": self.warmups, "measured_runs": self.runs}


def add_sampling_arguments(
    parser: argparse.ArgumentParser,
    *,
    warmups: int = 3,
    runs: int = 15,
    mode: BenchmarkMode = BenchmarkMode.PUBLICATION,
) -> None:
    parser.add_argument("--warmups", type=int, default=warmups)
    parser.add_argument("--runs", type=int, default=runs)
    parser.add_argument(
        "--mode",
        choices=tuple(item.value for item in BenchmarkMode),
        default=mode.value,
        help=(
            "publication requires at least 3 warmups and 15 runs; diagnostic permits shorter runs"
        ),
    )


def sampling_config(args: argparse.Namespace) -> SamplingConfig:
    """Default to publication mode when the namespace omits ``mode``."""

    raw_mode = getattr(args, "mode", BenchmarkMode.PUBLICATION)
    mode = raw_mode if isinstance(raw_mode, BenchmarkMode) else BenchmarkMode(str(raw_mode))
    return SamplingConfig(int(args.warmups), int(args.runs), mode)


def benchmark_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a controlled child-process environment for benchmark commands."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("GIT_", "COVERAGE_", "PYTEST_"))
        and name
        not in {
            "PYTHONHOME",
            "PYTHONPATH",
            "UV_PROJECT_ENVIRONMENT",
            "VIRTUAL_ENV",
            "NO_COLOR",
            "FORCE_COLOR",
            "CLICOLOR",
            "CLICOLOR_FORCE",
        }
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    # Reference measurements use Coverage.py's C tracer. Python 3.14 defaults to
    # sysmon, which records no dynamic contexts; vfy follows the same core choice.
    environment["COVERAGE_CORE"] = "ctrace"
    if overrides:
        environment.update(overrides)
    return environment


def git_value(root: Path, *arguments: str) -> str | None:
    """Read one Git value with the same sanitized environment as workloads."""

    try:
        completed = run_captured(
            ["git", *arguments],
            cwd=root,
            env=benchmark_environment(),
            timeout=30.0,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    return completed.stdout.strip()


def run_checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one benchmark child with an argument list and captured diagnostics."""

    command = [str(part) for part in argv]
    child_environment = benchmark_environment() if env is None else dict(env)
    completed = run_captured(
        command,
        cwd=cwd,
        env=child_environment,
        timeout=timeout if timeout is not None else 300.0,
    )
    if completed.returncode:
        rendered = " ".join(command)
        detail = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        raise RuntimeError(
            f"benchmark command failed ({completed.returncode}): {rendered}\n{detail}"
        )
    return completed


def timed(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> float:
    """Return process wall time in milliseconds; reject a nonzero exit status."""

    elapsed, completed = timed_process(argv, cwd=cwd, env=env, timeout=timeout)
    if completed.returncode:
        rendered = " ".join(str(part) for part in argv)
        detail = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        raise RuntimeError(
            f"benchmark command failed ({completed.returncode}): {rendered}\n{detail}"
        )
    return elapsed


def timed_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[float, subprocess.CompletedProcess[str]]:
    """Measure one process and return its result for protocols with expected failures."""

    started = time.perf_counter_ns()
    command = [str(part) for part in argv]
    completed = run_captured(
        command,
        cwd=cwd,
        env=benchmark_environment() if env is None else dict(env),
        timeout=timeout if timeout is not None else 300.0,
    )
    return (time.perf_counter_ns() - started) / 1_000_000, completed


def timed_call[ValueT](function: Callable[[], ValueT]) -> tuple[float, ValueT]:
    """Time a local operation and return its value for validation after timing."""

    started = time.perf_counter_ns()
    value = function()
    return (time.perf_counter_ns() - started) / 1_000_000, value


def summarize(samples: Sequence[float]) -> TimingSummary:
    values = [float(sample) for sample in samples]
    if not values:
        raise ValueError("cannot summarize an empty sample set")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("timing samples must be finite and nonnegative")
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    return {
        "median_ms": median,
        "mean_ms": statistics.mean(values),
        "stdev_ms": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min_ms": min(values),
        "max_ms": max(values),
        "samples_ms": values,
        "mad_ms": statistics.median(deviations),
    }


def rotated_orders(names: Sequence[str], count: int) -> Iterator[tuple[str, ...]]:
    """Rotate tool order between samples to reduce ordering bias."""

    values = tuple(names)
    if not values:
        raise ValueError("at least one benchmark name is required")
    if count < 0:
        raise ValueError("rotation count must be nonnegative")
    for index in range(count):
        offset = index % len(values)
        yield values[offset:] + values[:offset]


def _bootstrap_uncertainty(
    left: Sequence[float], right: Sequence[float], *, paired: bool
) -> ComparisonUncertainty:
    if not left or not right:
        raise ValueError("timing samples must be nonempty")
    if paired and len(left) != len(right):
        raise ValueError("paired samples must have equal lengths")
    if any(not math.isfinite(value) or value < 0 for value in (*left, *right)):
        raise ValueError("timing samples must be finite and nonnegative")
    estimate = statistics.median(left) - statistics.median(right)
    distribution: list[float] = []
    seed = 0
    resamples = 10_000 if min(len(left), len(right)) > 1 else 0
    # A local seed makes retained comparisons reproducible without changing global RNG state.
    generator = random.Random(seed)
    for _ in range(resamples):
        if paired:
            indices = generator.choices(range(len(left)), k=len(left))
            left_sample = [left[index] for index in indices]
            right_sample = [right[index] for index in indices]
        else:
            left_sample = generator.choices(left, k=len(left))
            right_sample = generator.choices(right, k=len(right))
        distribution.append(statistics.median(left_sample) - statistics.median(right_sample))
    low = high = standard_error = None
    if distribution:
        percentiles = statistics.quantiles(distribution, n=40, method="inclusive")
        low, high = percentiles[0], percentiles[-1]
        standard_error = statistics.stdev(distribution)
    return {
        "candidate_samples": len(left),
        "baseline_samples": len(right),
        "mean_difference_ms": statistics.mean(left) - statistics.mean(right),
        "median_difference_ms": estimate,
        "standard_error_ms": standard_error,
        "ci95_low_ms": low,
        "ci95_high_ms": high,
        "method": "percentile-bootstrap",
        "statistic": "difference-of-medians",
        "paired": paired,
        "resamples": resamples,
        "seed": seed,
    }


def paired_uncertainty(left: Sequence[float], right: Sequence[float]) -> ComparisonUncertainty:
    """Bootstrap the median difference using matching observation pairs.

    Pairs must represent the same experimental block; matching ordinal sample
    numbers from separate runs do not establish pairing.
    """

    return _bootstrap_uncertainty(left, right, paired=True)


def independent_uncertainty(left: Sequence[float], right: Sequence[float]) -> ComparisonUncertainty:
    """Bootstrap independently sampled medians, allowing different sample counts.

    Intervals describe observed sample variability, not variation across machines
    or cache states. A single observation cannot estimate uncertainty.
    """

    return _bootstrap_uncertainty(left, right, paired=False)


def publishable_command(argv: Sequence[str], *, cwd: Path) -> list[str]:
    """Replace temporary fixture paths in a retained command record."""

    return [
        "python" if index == 0 else str(part).replace(str(cwd), "$FIXTURE")
        for index, part in enumerate(argv)
    ]
