"""Structured timing records shared by the benchmark harnesses."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class TimingSummary(TypedDict):
    median_ms: float
    mean_ms: float
    stdev_ms: float
    min_ms: float
    max_ms: float
    samples_ms: list[float]
    mad_ms: NotRequired[float]


class ComparisonUncertainty(TypedDict):
    candidate_samples: int
    baseline_samples: int
    mean_difference_ms: float
    median_difference_ms: float
    standard_error_ms: float | None
    ci95_low_ms: float | None
    ci95_high_ms: float | None
    method: str
    statistic: str
    paired: bool
    resamples: int
    seed: int
