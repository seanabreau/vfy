"""Publication chart summaries must come from validated raw samples."""

from __future__ import annotations

import json
import shutil
import statistics
from pathlib import Path

import pytest
from scripts.build_coverage_chart_summary import build_summary

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "tests/fixtures/benchmark-charts/coverage"


def _copies(tmp_path: Path) -> list[Path]:
    paths = []
    for name in ("attrs", "platformdirs"):
        for suffix in ("", "-parity"):
            shutil.copy2(RESULTS / f"{name}{suffix}.json", tmp_path / f"{name}{suffix}.json")
        paths.append(tmp_path / f"{name}.json")
    return paths


def test_summary_recomputes_three_medians_and_links_sources(tmp_path: Path) -> None:
    paths = _copies(tmp_path)
    summary = build_summary(paths, root=tmp_path)
    rows = summary["charts"][0]["rows"]

    assert [row["source"] for row in rows] == ["attrs.json", "platformdirs.json"]
    assert all(
        row["reference_samples"] == row["candidate_samples"] == row["baseline_samples"] == 15
        for row in rows
    )
    assert rows[0]["reference_median_ms"] == 37
    assert rows[0]["candidate_median_ms"] == 27
    assert rows[0]["baseline_median_ms"] == 17
    assert rows[1]["reference_median_ms"] == 67
    assert rows[1]["candidate_median_ms"] == 47
    assert rows[1]["baseline_median_ms"] == 27


def test_summary_preserves_two_run_diagnostic_mode_and_actual_counts(tmp_path: Path) -> None:
    paths = _copies(tmp_path)
    record = json.loads(paths[0].read_text(encoding="utf-8"))
    record["mode"] = "diagnostic"
    record["methodology"]["mode"] = "diagnostic"
    record["warmups"] = 1
    record["methodology"]["warmups"] = 1
    record["runs"] = 2
    record["methodology"]["measured_runs"] = 2
    record["methodology"]["sampling"] = {"warmups": 1, "measured_runs": 2}
    for tool in ("pytest-cov", "vfy", "plain pytest"):
        series = record[tool]
        series["samples_ms"] = series["samples_ms"][:2]
        series["median_ms"] = statistics.median(series["samples_ms"])
    paths[0].write_text(json.dumps(record), encoding="utf-8")
    parity_path = tmp_path / "attrs-parity.json"
    parity = json.loads(parity_path.read_text(encoding="utf-8"))
    parity["mode"] = "diagnostic"
    parity["methodology"] = record["methodology"]
    parity_path.write_text(json.dumps(parity), encoding="utf-8")

    row = build_summary(paths, root=tmp_path)["charts"][0]["rows"][0]
    assert row["status"] == "diagnostic"
    assert row["reference_samples"] == row["candidate_samples"] == row["baseline_samples"] == 2
    assert row["warmups"] == 1


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("reported_median", "reported median differs"),
        ("nan_median", "reported median differs"),
        ("infinite_median", "reported median differs"),
        ("boolean_median", "reported median differs"),
        ("short_samples", "requires 15"),
        ("declared_runs", "sample counts differ"),
        ("parity", "exact matching parity"),
        ("route", "incompatible or unpublishable"),
        ("command", "coverage commands differ"),
    ],
)
def test_summary_rejects_unqualified_source(tmp_path: Path, tamper: str, message: str) -> None:
    paths = _copies(tmp_path)
    record_path = paths[0] if tamper != "parity" else tmp_path / "attrs-parity.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if tamper == "reported_median":
        record["vfy"]["median_ms"] += 1
    elif tamper == "nan_median":
        record["vfy"]["median_ms"] = float("nan")
    elif tamper == "infinite_median":
        record["vfy"]["median_ms"] = float("inf")
    elif tamper == "boolean_median":
        record["vfy"]["median_ms"] = True
    elif tamper == "short_samples":
        record["vfy"]["samples_ms"] = record["vfy"]["samples_ms"][:14]
    elif tamper == "declared_runs":
        record["runs"] = 16
        record["methodology"]["measured_runs"] = 16
        record["methodology"]["sampling"]["measured_runs"] = 16
    elif tamper == "parity":
        record["parity"] = "failed"
    elif tamper == "command":
        record["commands"]["vfy"][0]["argv"].remove("--cov-context=test")
    else:
        record["methodology"]["coverage_context"] = None
    record_path.write_text(json.dumps(record), encoding="utf-8")
    if tamper == "declared_runs":
        parity_path = tmp_path / "attrs-parity.json"
        parity = json.loads(parity_path.read_text(encoding="utf-8"))
        parity["methodology"] = record["methodology"]
        parity_path.write_text(json.dumps(parity), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        build_summary(paths, root=tmp_path)


def test_summary_accepts_only_documented_fastapi_source_override(tmp_path: Path) -> None:
    paths = _copies(tmp_path)
    record = json.loads(paths[0].read_text())
    parity_path = paths[0].with_name("attrs-parity.json")
    parity = json.loads(parity_path.read_text())
    record["project"]["key"] = "fastapi"
    record["project"]["name"] = "FastAPI"
    record["methodology"]["source_override"] = "fastapi"
    for tool in ("pytest-cov", "vfy"):
        record["commands"][tool][0]["argv"].extend(["--cov-reset", "--cov=fastapi"])
    parity["project"] = "FastAPI"
    parity["methodology"] = record["methodology"]
    paths[0].write_text(json.dumps(record))
    parity_path.write_text(json.dumps(parity))
    assert build_summary(paths, root=tmp_path)["charts"][0]["rows"][0]["project"] == "FastAPI"
    record["commands"]["pytest-cov"][0]["argv"][-1] = "--cov=tests"
    record["commands"]["vfy"][0]["argv"][-1] = "--cov=tests"
    paths[0].write_text(json.dumps(record))
    with pytest.raises(ValueError, match="source override differs"):
        build_summary(paths, root=tmp_path)
