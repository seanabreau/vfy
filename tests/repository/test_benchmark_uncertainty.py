"""Uncertainty describes the measured statistic and experimental pairing."""

from __future__ import annotations

import math

import pytest
from benchmarks.benchmark_harness import independent_uncertainty, paired_uncertainty


def test_paired_shared_noise_cancels_only_when_observations_are_paired() -> None:
    baseline = [1.0, 10.0, 100.0, 1000.0]
    candidate = [value + 5.0 for value in baseline]
    paired = paired_uncertainty(candidate, baseline)
    independent = independent_uncertainty(candidate, baseline)
    assert paired["median_difference_ms"] == 5.0
    assert (paired["ci95_low_ms"], paired["ci95_high_ms"]) == (5.0, 5.0)
    low, high = independent["ci95_low_ms"], independent["ci95_high_ms"]
    assert low is not None
    assert high is not None
    assert low < 0 < high


def test_bootstrap_reports_median_uncertainty_with_unequal_samples() -> None:
    result = independent_uncertainty([1.0, 3.0], [0.0, 0.0, 0.0])
    # The possible resampled candidate medians are 1, 2, and 3. Both tails
    # carry 25% probability, so the 95% percentile interval includes 1 and 3.
    assert result["median_difference_ms"] == 2.0
    assert (result["ci95_low_ms"], result["ci95_high_ms"]) == (1.0, 3.0)
    assert result["candidate_samples"] == 2
    assert result["baseline_samples"] == 3
    assert result == independent_uncertainty([1.0, 3.0], [0.0, 0.0, 0.0])


def test_outlier_does_not_turn_median_interval_into_mean_interval() -> None:
    result = independent_uncertainty([1.0] * 14 + [100.0], [0.0] * 15)
    assert result["mean_difference_ms"] > 1.0
    assert result["median_difference_ms"] == 1.0
    assert (result["ci95_low_ms"], result["ci95_high_ms"]) == (1.0, 1.0)


@pytest.mark.parametrize("samples", [[], [-1.0], [math.nan], [math.inf]])
def test_invalid_observations_cannot_produce_uncertainty(samples: list[float]) -> None:
    with pytest.raises(ValueError, match="timing samples"):
        independent_uncertainty(samples, [1.0, 2.0])


def test_one_observation_has_no_estimated_interval() -> None:
    result = independent_uncertainty([1.0], [2.0, 3.0])
    assert result["ci95_low_ms"] is None
    assert result["ci95_high_ms"] is None
    assert result["standard_error_ms"] is None


def test_pairing_rejects_unequal_observation_counts() -> None:
    with pytest.raises(ValueError, match="equal lengths"):
        paired_uncertainty([1.0], [2.0, 3.0])
