from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.ci_metrics import CiSample, load_samples, summarize


def test_ci_metrics_are_deterministic_and_retain_sample_count() -> None:
    samples = [
        CiSample("a", "quality", 10.0, 1.5),
        CiSample("b", "quality", 20.0, 2.5),
        CiSample("c", "quality", 30.0, 3.5),
    ]

    assert summarize(samples) == {
        "samples": 3,
        "duration_seconds_total": 60.0,
        "duration_seconds_p50": 20.0,
        "duration_seconds_p95": 30.0,
        "runner_minutes_total": 7.5,
        "runner_minutes_p50": 2.5,
        "runner_minutes_p95": 3.5,
    }


def test_ci_metrics_loads_retained_records_and_rejects_unmeasurable_values(tmp_path: Path) -> None:
    path = tmp_path / "before.json"
    path.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "revision": "abc123",
                        "lane": "full",
                        "duration_seconds": 42,
                        "runner_minutes": 6.25,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_samples(path)[0].runner_minutes == 6.25

    path.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "revision": "abc123",
                        "lane": "full",
                        "duration_seconds": "unknown",
                        "runner_minutes": 6.25,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duration_seconds"):
        load_samples(path)
