#!/usr/bin/env python3
"""Summarize retained CI duration and runner-consumption samples.

Compare matching lane definitions from checked-in or archived JSON records so
results remain reproducible when GitHub run history changes.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CiSample:
    """One completed workflow measurement."""

    revision: str
    lane: str
    duration_seconds: float
    runner_minutes: float


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return converted


def load_samples(path: Path) -> list[CiSample]:
    """Read a retained JSON file containing ``samples`` records."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("samples") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not records:
        raise ValueError("metrics input must contain a non-empty samples list")
    samples = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"sample {index} must be an object")
        revision = record.get("revision")
        lane = record.get("lane")
        if not isinstance(revision, str) or not revision:
            raise ValueError(f"sample {index} has no revision")
        if not isinstance(lane, str) or not lane:
            raise ValueError(f"sample {index} has no lane")
        samples.append(
            CiSample(
                revision,
                lane,
                _number(record.get("duration_seconds"), "duration_seconds"),
                _number(record.get("runner_minutes"), "runner_minutes"),
            )
        )
    return samples


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    # Nearest-rank percentiles select an observed duration even for small samples.
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: list[CiSample]) -> dict[str, float | int]:
    """Return duration and runner-minute totals and nearest-rank percentiles."""

    if not samples:
        raise ValueError("at least one CI sample is required")
    durations = [sample.duration_seconds for sample in samples]
    runner_minutes = [sample.runner_minutes for sample in samples]
    return {
        "samples": len(samples),
        "duration_seconds_total": sum(durations),
        "duration_seconds_p50": _percentile(durations, 0.50),
        "duration_seconds_p95": _percentile(durations, 0.95),
        "runner_minutes_total": sum(runner_minutes),
        "runner_minutes_p50": _percentile(runner_minutes, 0.50),
        "runner_minutes_p95": _percentile(runner_minutes, 0.95),
    }


def _report(path: Path, label: str) -> dict[str, object]:
    samples = load_samples(path)
    return {
        "label": label,
        "summary": summarize(samples),
        "samples": [asdict(sample) for sample in samples],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="retained JSON sample file")
    parser.add_argument("--label", default="sample", help="label for the output record")
    parser.add_argument("--output", type=Path, help="write the report to this JSON file")
    args = parser.parse_args()
    report = _report(args.input, args.label)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
