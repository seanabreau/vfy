"""The docs chart renderer must not turn incompatible or unvalidated runs into claims."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from scripts.render_benchmark_charts import (
    ProjectChart,
    parse_memory_summary,
    parse_project_charts,
    parse_readme_resources,
    parse_readme_stock_rows,
    parse_stock_only_rows,
    render_memory_chart,
    render_project_chart,
    render_readme_overview,
    render_readme_resources,
    select_readme_projects,
)

ROOT = Path(__file__).resolve().parents[2]
SVG = "{http://www.w3.org/2000/svg}"
MIB = 1_048_576


def _memory_summary() -> dict[str, Any]:
    """Illustrative bytes used only to verify the memory chart contract."""

    def row(project: str, plain: int, vfy: int, pytest_cov: int) -> dict[str, Any]:
        return {
            "project": project,
            "scope": "Synthetic full serial suite",
            "parity": "exact",
            "warmups": 0,
            "runs": 2,
            "values": {
                name: {"samples_bytes": [value * MIB, value * MIB], "median_bytes": value * MIB}
                for name, value in (
                    ("plain pytest", plain),
                    ("vfy", vfy),
                    ("pytest-cov", pytest_cov),
                )
            },
            "provenance": {
                "wheel_sha256": "a" * 64,
                "controller_revision": "fixture-controller",
                "upstream_revision": "fixture-upstream",
            },
        }

    return {
        "schema": 1,
        "metric": "largest_process_peak_rss_bytes",
        "method": "linux-single-child-rusage",
        "route": "serial-stock-pytest-coverage",
        "rows": [row("Fixture A", 100, 80, 120), row("Fixture B", 50, 60, 40)],
    }


def _readme_resources_summary() -> dict[str, Any]:
    """Synthetic paired metrics; no values here are benchmark measurements."""

    routes = ("plain pytest", "vfy", "pytest-cov", "stock-xdist-4", "pytest-native-j4")

    def project(
        name: str, cases: int, times: tuple[int, ...], mib: tuple[int, ...]
    ) -> dict[str, Any]:
        return {
            "project": name,
            "scope": "Synthetic full suite",
            "cases": cases,
            "parity": "exact",
            "warmups": 0,
            "runs": 2,
            "gates": {"serial_coverage": "exact", "parallel_junit": "passed"},
            "routes": {
                route: {
                    "time_samples_ms": [seconds * 1000, seconds * 1000],
                    "time_median_ms": seconds * 1000,
                    "peak_tree_pss_samples_bytes": [memory * MIB, memory * MIB],
                    "peak_tree_pss_median_bytes": memory * MIB,
                }
                for route, seconds, memory in zip(routes, times, mib, strict=True)
            },
            "provenance": {
                "controller_revision": "a" * 40,
                "source_revision": "b" * 40,
                "upstream_revision": "c" * 40,
                "wheel_sha256": "d" * 64,
                "collector_sha256": "e" * 64,
                "runtime": {
                    "python": "3.12.14",
                    "implementation": "CPython",
                    "pytest": "9.1.1",
                    "pytest_cov": "7.1.0",
                    "vfy": "0.1.0",
                },
            },
        }

    return {
        "schema": 1,
        "metric": "peak_tree_pss_bytes",
        "method": "linux-process-tree-smaps-rollup",
        "sample_interval_ms": 20,
        "rows": [
            project("Packaging", 62435, (100, 80, 120, 60, 40), (200, 180, 220, 300, 250)),
            project("Rich", 981, (10, 8, 12, 6, 4), (100, 80, 120, 150, 130)),
            project("attrs", 1412, (5, 4, 6, 3, 2), (60, 50, 70, 90, 80)),
        ],
    }


def _summary() -> dict[str, object]:
    # Illustrative values used only to check chart behavior; these are not measurements.
    route = "stock pytest, serial, line coverage, full suite"
    return {
        "schema": 1,
        "charts": [
            {
                "kind": "coverage",
                "title": "Coverage comparison",
                "route": route,
                "context": "Example chart fixture",
                "reference": "pytest-cov",
                "candidate": "vfy",
                "rows": [
                    {
                        "project": "Package A",
                        "scope": "full suite",
                        "route": route,
                        "reference_median_ms": 8000,
                        "candidate_median_ms": 4000,
                        "reference_samples": 15,
                        "candidate_samples": 15,
                        "warmups": 3,
                        "status": "publication",
                        "parity": "passed",
                        "source": "fixture-only/a.json",
                    },
                    {
                        "project": "Package B",
                        "scope": "full suite",
                        "route": route,
                        "reference_median_ms": 1000,
                        "candidate_median_ms": 2000,
                        "reference_samples": 15,
                        "candidate_samples": 15,
                        "warmups": 3,
                        "status": "publication",
                        "parity": "passed",
                        "source": "fixture-only/b.json",
                    },
                ],
            }
        ],
    }


def test_project_chart_cli_shows_faster_and_slower_results(tmp_path: Path) -> None:
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(_summary()), encoding="utf-8")
    output = tmp_path / "assets"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/render_benchmark_charts.py"),
            "--summary-json",
            str(summary),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    root = ET.parse(output / "coverage-projects.svg").getroot()
    description = root.find(f"{SVG}desc")
    assert root.attrib["role"] == "img"
    assert description is not None
    description_text = description.text
    assert description_text is not None
    assert "Package A" in description_text
    assert "Package B" in description_text
    assert "ratio 2.000 times" in description_text
    assert "ratio 0.500 times" in description_text
    text_values = [element.text for element in root.iter(f"{SVG}text")]
    assert "2.00\u00d7 faster" in text_values
    assert "2.00\u00d7 slower" in text_values
    assert "vfy 4.00 s · pytest-cov 8.00 s" in text_values
    assert "vfy 2.00 s · pytest-cov 1.00 s" in text_values
    serialized = (output / "coverage-projects.svg").read_text()
    assert "2.00\u00d7 faster" in serialized
    assert "2.00\u00d7 slower" in serialized
    assert r"\u00d7" not in serialized
    assert root.attrib["viewBox"].split()[2] == "640"
    assert "prefers-color-scheme:dark" in (output / "coverage-projects.svg").read_text()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("route", "native execution", "route differs"),
        ("parity", "failed", "passing parity"),
        ("status", "unverified", "qualified status"),
        ("candidate_median_ms", 0, "positive finite number"),
        ("reference_samples", 14, "at least 15"),
        ("warmups", 2, "at least 3"),
    ],
)
def test_project_chart_rejects_unpublishable_row(field: str, value: object, message: str) -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    first = charts[0]
    assert isinstance(first, dict)
    rows = first["rows"]
    assert isinstance(rows, list)
    first_row = rows[0]
    assert isinstance(first_row, dict)
    first_row[field] = value

    with pytest.raises(ValueError, match=message):
        parse_project_charts(summary)


def test_mutation_chart_requires_thirty_samples() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    chart["kind"] = "mutation"
    rows = chart["rows"]
    assert isinstance(rows, list)

    with pytest.raises(ValueError, match="at least 30"):
        parse_project_charts(summary)


def test_mutation_chart_accepts_parity_gated_two_run_diagnostic() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    chart["kind"] = "mutation"
    rows = chart["rows"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        row["status"] = "diagnostic"
        row["reference_samples"] = 2
        row["candidate_samples"] = 2
        row["warmups"] = 0
    first = rows[0]
    assert isinstance(first, dict)
    first["scope"] = "package/module.py::function · 7 exact mutations"
    first["display_scope"] = "78 exact body mutants · Requests utility module"

    root = ET.fromstring(render_project_chart(parse_project_charts(summary)[0]))
    labels = [element.text for element in root.iter(f"{SVG}text")]
    assert "78 exact body mutants" in labels
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert description.text is not None
    assert "78 exact body mutants · Requests utility module" in description.text
    assert "2.00\u00d7 faster" in labels
    assert "2.00\u00d7 slower" in labels
    assert "4.00 s" in labels
    assert "8.00 s" in labels
    assert "Median seconds · each project has its own zero-origin scale" in labels
    assert all("diagnostic" not in (label or "") for label in labels)
    rects = [element for element in root.iter(f"{SVG}rect") if element.get("class")]
    reference = next(element for element in rects if element.get("class") == "reference")
    vfy = next(element for element in rects if element.get("class") == "vfy")
    assert float(reference.attrib["width"]) > float(vfy.attrib["width"])


def test_selection_chart_displays_elapsed_time_and_selected_full_counts() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    chart["kind"] = "selection"
    chart["reference"] = "pytest-testmon"
    rows = chart["rows"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        row.update(
            status="diagnostic",
            reference_samples=2,
            candidate_samples=2,
            warmups=0,
            full_cases=2123,
            reference_cases=7,
            candidate_cases=9,
            context_status="differs",
        )
    root = ET.fromstring(render_project_chart(parse_project_charts(summary)[0]))
    labels = [element.text for element in root.iter(f"{SVG}text")]
    assert "Selected 9 vfy / 7 testmon of 2,123 tests" in labels
    assert "vfy JSON + aggregate coverage match oracle; contexts may differ" in labels
    assert "2.00\u00d7 faster" in labels
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert description.text is not None
    assert (
        "selected 7 by pytest-testmon, 9 by vfy, of 2123 full-suite tests, "
        "per-test context status: differs"
    ) in description.text
    first = rows[0]
    assert isinstance(first, dict)
    first["candidate_cases"] = 5
    with pytest.raises(ValueError, match="selected cases must satisfy"):
        parse_project_charts(summary)


def test_coverage_chart_accepts_validated_two_sample_rows_without_status_badges() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    rows = chart["rows"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        row["status"] = "diagnostic"
        row["reference_samples"] = 2
        row["candidate_samples"] = 2
        row["baseline_samples"] = 2
        row["baseline_median_ms"] = 3000
        row["warmups"] = 1

    root = ET.fromstring(render_project_chart(parse_project_charts(summary)[0]))
    text_values = [element.text for element in root.iter(f"{SVG}text")]
    assert "2.00\u00d7 faster" in text_values
    assert all("diagnostic" not in (value or "") for value in text_values)


def test_diagnostic_row_rejects_one_sample_and_mismatched_counts() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    rows = chart["rows"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    first["status"] = "diagnostic"
    first["reference_samples"] = 1
    with pytest.raises(ValueError, match="at least 2"):
        parse_project_charts(summary)
    first["reference_samples"] = 2
    with pytest.raises(ValueError, match="sample counts must match"):
        parse_project_charts(summary)


def test_parallel_chart_uses_file_scheduler_times_for_native_xdist() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    chart["kind"] = "parallel"
    chart["reference"] = "pytest-xdist"
    chart["candidate"] = "Native xdist"
    rows = chart["rows"]
    assert isinstance(rows, list)
    for row, filegroups in zip(rows, (3500, 1500), strict=True):
        assert isinstance(row, dict)
        row["status"] = "diagnostic"
        row["reference_samples"] = 2
        row["candidate_samples"] = 2
        row["filegroups_samples"] = 2
        row["filegroups_median_ms"] = filegroups
        row["warmups"] = 0

    root = ET.fromstring(render_project_chart(parse_project_charts(summary)[0]))
    labels = [element.text for element in root.iter(f"{SVG}text")]
    assert labels.count("pytest-xdist") == 2
    assert labels.count("vfy native workers") == 2
    assert "Native file groups" not in labels
    assert "4.00 s" not in labels
    assert "2.00 s" not in labels
    assert "3.50 s" in labels
    assert "1.50 s" in labels
    assert ("vs pytest-xdist: vfy native workers 2.29\u00d7 faster") in labels
    assert ("vs pytest-xdist: vfy native workers 1.50\u00d7 slower") in labels
    assert sum(element.get("class") == "vfy" for element in root.iter(f"{SVG}rect")) == 2


def test_parallel_chart_requires_all_three_measured_routes() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    chart["kind"] = "parallel"

    with pytest.raises(ValueError, match="file-group timing belongs"):
        parse_project_charts(summary)


def test_coverage_chart_shows_plain_pytest_control_without_changing_ratio() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    rows = chart["rows"]
    assert isinstance(rows, list)
    for row, median in zip(rows, (3000, 500), strict=True):
        assert isinstance(row, dict)
        row["baseline_median_ms"] = median
        row["baseline_samples"] = 15

    svg = render_project_chart(parse_project_charts(summary)[0])
    root = ET.fromstring(svg)
    description = root.find(f"{SVG}desc")
    assert description is not None
    description_text = description.text
    assert description_text is not None
    assert "plain pytest 3.000 seconds" in description_text
    assert "plain pytest 0.500 seconds" in description_text
    text_values = [element.text for element in root.iter(f"{SVG}text")]
    assert "4.00 s" in text_values
    assert "8.00 s" in text_values
    assert "3.00 s" in text_values
    assert "2.00\u00d7 faster" in text_values
    assert "2.00\u00d7 slower" in text_values
    plain = [element for element in root.iter(f"{SVG}rect") if element.get("class") == "plain"]
    vfy = [element for element in root.iter(f"{SVG}rect") if element.get("class") == "vfy"]
    reference = [
        element for element in root.iter(f"{SVG}rect") if element.get("class") == "reference"
    ]
    assert len(plain) == len(vfy) == len(reference) == 2
    assert plain[0].get("x") == vfy[0].get("x") == reference[0].get("x") == "155"
    assert float(plain[0].get("width", "0")) < float(vfy[0].get("width", "0"))
    assert float(vfy[0].get("width", "0")) < float(reference[0].get("width", "0"))
    assert "own zero-origin elapsed-seconds scale" in description_text
    assert reference[0].get("width") == vfy[1].get("width") == "355.0"


@pytest.mark.parametrize(
    ("baseline_median", "baseline_samples", "message"),
    [
        (3000, None, "requires both plain-pytest median and samples"),
        (3000, 14, "baseline_samples must be at least 15"),
    ],
)
def test_coverage_control_requires_complete_publishable_samples(
    baseline_median: int, baseline_samples: int | None, message: str
) -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    rows = chart["rows"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    first["baseline_median_ms"] = baseline_median
    first["baseline_samples"] = baseline_samples

    with pytest.raises(ValueError, match=message):
        parse_project_charts(summary)


def test_coverage_chart_requires_controls_for_each_project() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    rows = chart["rows"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    first["baseline_median_ms"] = 3000
    first["baseline_samples"] = 15

    with pytest.raises(ValueError, match="controls for every row"):
        parse_project_charts(summary)


def test_project_chart_rejects_label_that_would_clip() -> None:
    summary = _summary()
    charts = summary["charts"]
    assert isinstance(charts, list)
    chart = charts[0]
    assert isinstance(chart, dict)
    chart["title"] = "A very long benchmark chart title that exceeds the available width"

    with pytest.raises(ValueError, match="exceeds chart label width"):
        parse_project_charts(summary)


def test_memory_chart_cli_preserves_bytes_and_per_project_zero_origin_scales(
    tmp_path: Path,
) -> None:
    summary = tmp_path / "memory-summary.json"
    summary.write_text(json.dumps(_memory_summary()), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/render_benchmark_charts.py"),
            "--memory-summary-json",
            str(summary),
            "--output-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    root = ET.parse(tmp_path / "memory-projects.svg").getroot()
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert "largest individual process" in (description.text or "")
    assert "not aggregate concurrent process-tree memory" in (description.text or "")
    text = [node.text for node in root.iter(f"{SVG}text")]
    assert "Fixture A" in text
    assert "Fixture B" in text
    assert "80.0 MiB" in text
    assert "120.0 MiB" in text
    bars = [
        node
        for node in root.iter(f"{SVG}rect")
        if node.get("class") in {"reference", "vfy", "plain"}
    ]
    assert [node.get("class") for node in bars] == [
        "plain",
        "vfy",
        "reference",
        "plain",
        "vfy",
        "reference",
    ]
    assert [float(node.attrib["width"]) for node in bars] == pytest.approx(
        [200, 160, 240, 250, 300, 200], abs=0.001
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda item: item.update({"metric": "elapsed_ms"}), "largest-process Linux RSS"),
        (lambda item: item.update({"method": "sampled-tree-rss"}), "largest-process Linux RSS"),
        (lambda item: item.update({"route": "pytest-native-xdist-4"}), "serial stock pytest"),
        (lambda item: item["rows"][0].update({"parity": "aggregate"}), "exact parity"),
        (lambda item: item["rows"][0].update({"runs": 1}), "two measured runs"),
        (
            lambda item: item["rows"][0]["values"]["vfy"].update({"samples_bytes": [80 * MIB]}),
            "two RSS samples",
        ),
        (
            lambda item: item["rows"][0]["values"]["vfy"].update({"median_bytes": 79 * MIB}),
            "median differs",
        ),
        (
            lambda item: item["rows"][0]["values"]["vfy"].update({"samples_bytes": [8000, 8000]}),
            "plausible positive memory bytes",
        ),
        (
            lambda item: item["rows"][0]["values"].update({"native-xdist": {}}),
            "three serial routes",
        ),
        (
            lambda item: item["rows"][0]["provenance"].update({"wheel_sha256": "a" * 63}),
            "release wheel SHA-256",
        ),
    ],
)
def test_memory_summary_rejects_incomparable_or_invented_values(
    change: Callable[[dict[str, Any]], None], message: str
) -> None:
    summary = deepcopy(_memory_summary())
    change(summary)
    with pytest.raises(ValueError, match=message):
        parse_memory_summary(summary)


def test_memory_chart_requires_multiple_qualified_projects() -> None:
    rows = parse_memory_summary(_memory_summary())
    with pytest.raises(ValueError, match="at least two"):
        render_memory_chart(rows[:1])


def test_single_readme_chart_separates_time_and_tree_pss_scales(tmp_path: Path) -> None:
    summary = tmp_path / "readme-resources.json"
    summary.write_text(json.dumps(_readme_resources_summary()), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/render_benchmark_charts.py"),
            "--readme-resources-json",
            str(summary),
            "--output-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    root = ET.parse(tmp_path / "readme-benchmarks.svg").getroot()
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert "simultaneously live child and descendant processes" in (description.text or "")
    assert "target 20 ms intervals" in (description.text or "")
    assert "sampling can add overhead" in (description.text or "")
    metadata = root.find(f"{SVG}metadata[@id='benchmark-provenance']")
    assert metadata is not None
    assert metadata.text is not None
    retained = json.loads(metadata.text)
    assert retained["metric"] == "peak_tree_pss_bytes"
    assert retained["method"] == "linux-process-tree-smaps-rollup"
    assert retained["sample_interval_ms"] == 20
    assert [project["project"] for project in retained["projects"]] == [
        "Packaging",
        "Rich",
        "attrs",
    ]
    for project in retained["projects"]:
        assert project["controller_revision"] == "a" * 40
        assert project["source_revision"] == "b" * 40
        assert project["upstream_revision"] == "c" * 40
        assert project["wheel_sha256"] == "d" * 64
        assert project["collector_sha256"] == "e" * 64
        assert project["runtime"] == {
            "python": "3.12.14",
            "implementation": "CPython",
            "pytest": "9.1.1",
            "pytest_cov": "7.1.0",
            "vfy": "0.1.0",
        }
    assert "samples" not in metadata.text
    assert "artifact" not in metadata.text
    assert "path" not in metadata.text
    labels = [node.text for node in root.iter(f"{SVG}text")]
    assert "Time with sampling · s" in labels
    assert "Peak memory · MiB" in labels
    assert "vfy native workers" in labels
    assert "Serial · coverage on except plain control    |    4 workers · no coverage" in labels
    assert "Peak memory: sampled tree PSS (shared pages apportioned)" in labels
    assert "Time includes memory sampling · scales reset per project" in labels
    assert "Packaging" in labels
    assert "Rich" in labels
    assert "attrs" in labels
    dividers = [
        float(node.attrib["y1"])
        for node in root.iter(f"{SVG}line")
        if node.get("class") == "rule" and node.get("x1") == "24" and node.get("x2") == "616"
    ]
    for name in ("Rich", "attrs"):
        header = next(node for node in root.iter(f"{SVG}text") if node.text == name)
        before = max(y for y in dividers if y < float(header.attrib["y"]))
        assert float(header.attrib["y"]) - before >= 16
    elapsed_bars = [
        node
        for node in root.iter(f"{SVG}rect")
        if node.get("class") in {"reference", "vfy", "plain"}
    ]
    memory_bars = [
        node for node in root.iter(f"{SVG}rect") if "memory" in (node.get("class") or "")
    ]
    assert len(elapsed_bars) == 15
    assert len(memory_bars) == 15
    # Separate project-local zero-origin scales: 120 s maps to 137.6 px,
    # while the 300 MiB peak maps to the full 75 px memory column.
    assert float(elapsed_bars[0].get("width", "0")) == pytest.approx(137.6, abs=0.001)
    assert float(memory_bars[3].get("width", "0")) == pytest.approx(75, abs=0.001)
    assert all(float(node.attrib["width"]) <= 172 for node in elapsed_bars)
    assert all(float(node.attrib["width"]) <= 75 for node in memory_bars)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda item: item.update({"metric": "largest_process_peak_rss_bytes"}), "whole-tree PSS"),
        (lambda item: item.update({"sample_interval_ms": 50}), "whole-tree PSS"),
        (lambda item: item["rows"][0].update({"project": "Wrong"}), "distinct Packaging"),
        (lambda item: item["rows"][0].update({"runs": 1}), "two measured runs"),
        (
            lambda item: item["rows"][0]["gates"].update({"parallel_junit": "failed"}),
            "parallel JUnit gates",
        ),
        (
            lambda item: item["rows"][0]["routes"].pop("pytest-native-j4"),
            "five complete command routes",
        ),
        (
            lambda item: item["rows"][0]["routes"]["vfy"].update({"time_median_ms": 90_000}),
            "time median differs",
        ),
        (
            lambda item: item["rows"][0]["routes"]["vfy"].update(
                {"peak_tree_pss_median_bytes": 10 * MIB}
            ),
            "PSS median differs",
        ),
        (
            lambda item: item["rows"][0]["routes"]["vfy"].update(
                {"peak_tree_pss_samples_bytes": [80, 80]}
            ),
            "plausible positive memory bytes",
        ),
        (
            lambda item: item["rows"][0]["provenance"].update({"wheel_sha256": "d" * 63}),
            "wheel_sha256 as SHA-256",
        ),
        (
            lambda item: item["rows"][1]["provenance"].update({"wheel_sha256": "f" * 64}),
            "mixed vfy wheels",
        ),
        (
            lambda item: item["rows"][1]["provenance"]["runtime"].update({"pytest": "8.4.0"}),
            "mixed vfy wheels, PSS collectors, or runtimes",
        ),
    ],
)
def test_single_readme_chart_rejects_incomplete_or_unmatched_resources(
    change: Callable[[dict[str, Any]], None], message: str
) -> None:
    summary = deepcopy(_readme_resources_summary())
    change(summary)
    with pytest.raises(ValueError, match=message):
        parse_readme_resources(summary)


def test_single_readme_chart_requires_fixed_three_project_groups() -> None:
    rows = parse_readme_resources(_readme_resources_summary())
    with pytest.raises(ValueError, match="Packaging, Rich, and attrs"):
        render_readme_resources(rows[:2])


def test_readme_overview_groups_all_commands_on_one_scale_per_project(tmp_path: Path) -> None:
    parallel = parse_project_charts(
        json.loads(
            (ROOT / "tests/fixtures/benchmark-charts/parallel/chart-summary.json").read_text()
        )
    )[0]
    coverage = parse_project_charts(
        json.loads((ROOT / "tests/fixtures/benchmark-charts/coverage/summary.json").read_text())
    )[0]
    _, parallel, _ = select_readme_projects(coverage, parallel, (), None)
    resource_asset = tmp_path / "readme-benchmarks.svg"
    resource_asset.write_text("retained time-and-memory chart", encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/render_benchmark_charts.py"),
            "--readme-overview",
            str(ROOT / "tests/fixtures/benchmark-charts"),
            *(argument for row in parallel.rows for argument in ("--readme-project", row.project)),
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert resource_asset.read_text(encoding="utf-8") == "retained time-and-memory chart"
    root = ET.parse(tmp_path / "readme-overview.svg").getroot()
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert (description.text or "").endswith("file scheduler when supported.")
    text = " ".join(node.text or "" for node in root.iter(f"{SVG}text"))
    for row in parallel.rows:
        assert row.filegroups_median_ms is not None
        assert f"{row.filegroups_median_ms / 1000:.2f} s" in text
    assert "serial · coverage on" in text
    assert "serial · coverage off" in text
    assert "4 workers · coverage off" in text
    assert "vfy native workers" in text
    assert "pytest-cov" in text
    assert "pytest-xdist" in text
    assert "vfy file groups" not in text
    bars = [
        node
        for node in root.iter(f"{SVG}rect")
        if node.get("class") in {"reference", "vfy", "plain"}
    ]
    assert len(bars) == len(parallel.rows) * 5
    assert all(0 < float(node.attrib["width"]) <= 300 for node in bars)

    by_project = {row.project: row for row in coverage.rows}
    for index, row in enumerate(parallel.rows):
        coverage_row = by_project[row.project]
        values = (
            coverage_row.reference_median_ms,
            coverage_row.candidate_median_ms,
            coverage_row.baseline_median_ms,
            row.reference_median_ms,
            row.filegroups_median_ms,
        )
        widths = [float(bar.attrib["width"]) for bar in bars[index * 5 : index * 5 + 5]]
        for value, width in zip(values, widths, strict=True):
            assert value is not None
            assert width / widths[0] == pytest.approx(
                value / coverage_row.reference_median_ms, abs=0.00001
            )


def _stock_only_fixture() -> tuple[dict[str, object], ProjectChart, ProjectChart]:
    """Synthetic extra project; its numbers must never enter published assets."""
    results = ROOT / "tests/fixtures/benchmark-charts"
    coverage = parse_project_charts(json.loads((results / "coverage/summary.json").read_text()))[0]
    parallel = parse_project_charts(
        json.loads((results / "parallel/chart-summary.json").read_text())
    )[0]
    template = coverage.rows[0]
    extra = replace(
        template,
        project="FixtureAPI",
        scope="Synthetic suite",
        reference_median_ms=12000,
        candidate_median_ms=8000,
        baseline_median_ms=4000,
    )
    coverage = replace(coverage, rows=(*coverage.rows, extra))
    _, parallel, _ = select_readme_projects(coverage, parallel, (), None)
    payload: dict[str, object] = {
        "schema": 1,
        "stock_only_rows": [
            {
                "project": "FixtureAPI",
                "scope": "Synthetic suite",
                "route": parallel.route,
                "workers": 4,
                "capture": "no",
                "report": "JUnit",
                "parity": "passed",
                "native_status": "unsupported_required_plugins",
                "reason": "required runtime plug-ins are unsupported",
                "reference_samples_ms": [6000, 10000],
                "reference_samples": 2,
                "reference_median_ms": 8000,
                "wheel_sha256": "a" * 64,
                "controller_revision": "fixture-controller",
                "upstream_revision": "fixture-upstream",
            }
        ],
    }
    return payload, coverage, parallel


def test_readme_stock_only_row_has_measured_bars_and_explicit_missing_routes() -> None:
    payload, coverage, parallel = _stock_only_fixture()
    stock_only = parse_stock_only_rows(payload, coverage, parallel)
    root = ET.fromstring(render_readme_overview(coverage, parallel, stock_only))
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert "required runtime plug-ins" in (description.text or "")
    text = [node.text for node in root.iter(f"{SVG}text")]
    assert text.count("Unsupported plug-ins") == 1
    assert "FixtureAPI" in text
    bars = [
        node
        for node in root.iter(f"{SVG}rect")
        if node.get("class") in {"reference", "vfy", "plain"}
    ]
    assert len(bars) == len(parallel.rows) * 5 + 4
    fixture_bars = bars[-4:]
    widths = [float(bar.attrib["width"]) for bar in fixture_bars]
    # All four commands in this project share one zero-origin scale.
    assert widths == pytest.approx([240, 160, 80, 160], abs=0.001)


def test_readme_stock_only_accepts_verified_conftest_plugin_blocker() -> None:
    payload, coverage, parallel = _stock_only_fixture()
    rows = payload["stock_only_rows"]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    row["native_status"] = "unsupported_conftest_plugins"
    row["reason"] = "native pytest does not load pytest_plugins declared by tests/conftest.py"

    qualified = parse_stock_only_rows(payload, coverage, parallel)

    assert qualified[0].native_status == "unsupported_conftest_plugins"
    root = ET.fromstring(render_readme_overview(coverage, parallel, qualified))
    description = root.find(f"{SVG}desc")
    assert description is not None
    assert "tests/conftest.py" in (description.text or "")


def test_readme_project_filter_keeps_only_qualified_selected_groups() -> None:
    payload, coverage, parallel = _stock_only_fixture()
    stock_only = parse_stock_only_rows(payload, coverage, parallel)
    selected_name = parallel.rows[0].project

    narrowed_coverage, narrowed_parallel, narrowed_stock = select_readme_projects(
        coverage, parallel, stock_only, (selected_name, "FixtureAPI")
    )

    assert {row.project for row in narrowed_coverage.rows} == {selected_name, "FixtureAPI"}
    assert [row.project for row in narrowed_parallel.rows] == [selected_name]
    assert [row.project for row in narrowed_stock] == ["FixtureAPI"]
    svg = render_readme_overview(narrowed_coverage, narrowed_parallel, narrowed_stock)
    root = ET.fromstring(svg)
    assert root.attrib["height"] == str(110 + 2 * 244)
    assert root.find(f"{SVG}desc") is not None
    text = {node.text for node in root.iter(f"{SVG}text")}
    assert selected_name in text
    assert "FixtureAPI" in text
    assert parallel.rows[1].project not in text


def test_readme_project_filter_does_not_require_unselected_pending_stock_row() -> None:
    payload, coverage, parallel = _stock_only_fixture()
    pending_coverage = replace(coverage, rows=coverage.rows[:-1])
    selected = (parallel.rows[0].project, parallel.rows[1].project)

    assert parse_readme_stock_rows(payload, pending_coverage, parallel, selected) == ()
    with pytest.raises(ValueError, match="paired coverage"):
        parse_readme_stock_rows(payload, pending_coverage, parallel, None)
    with pytest.raises(ValueError, match="paired coverage"):
        parse_readme_stock_rows(payload, pending_coverage, parallel, (selected[0], "FixtureAPI"))


def test_readme_default_omits_parallel_row_without_coverage_and_explicit_choice_fails() -> None:
    payload, coverage, parallel = _stock_only_fixture()
    stock_only = parse_stock_only_rows(payload, coverage, parallel)
    native_only = replace(parallel.rows[0], project="FixtureNativeOnly")
    parallel = replace(parallel, rows=(*parallel.rows, native_only))

    default_coverage, default_parallel, default_stock = select_readme_projects(
        coverage, parallel, stock_only, None
    )
    assert "FixtureNativeOnly" not in [row.project for row in default_parallel.rows]
    assert "FixtureNativeOnly" not in render_readme_overview(
        default_coverage, default_parallel, default_stock
    )

    with pytest.raises(ValueError, match=r"incomplete README group.*FixtureNativeOnly"):
        select_readme_projects(
            coverage, parallel, stock_only, (parallel.rows[0].project, "FixtureNativeOnly")
        )


@pytest.mark.parametrize(
    ("choice", "message"),
    [
        (("FixtureAPI",), "at least two"),
        (("FixtureAPI", "FixtureAPI"), "duplicates"),
        (("FixtureAPI", "UnknownPackage"), "unknown qualified"),
    ],
)
def test_readme_project_filter_rejects_invalid_choices(
    choice: tuple[str, ...], message: str
) -> None:
    payload, coverage, parallel = _stock_only_fixture()
    stock_only = parse_stock_only_rows(payload, coverage, parallel)
    with pytest.raises(ValueError, match=message):
        select_readme_projects(coverage, parallel, stock_only, choice)


def test_readme_project_option_requires_overview_and_qualified_selection(tmp_path: Path) -> None:
    renderer = str(ROOT / "scripts/render_benchmark_charts.py")
    for options, diagnostic in (
        (["--readme-project", "Rich"], "requires --readme-overview"),
        (["--readme-overview", "--readme-project", "Rich"], "at least two"),
        (
            ["--readme-overview", "--readme-project", "Rich", "--readme-project", "Unknown"],
            "unknown qualified",
        ),
        (
            ["--readme-overview", "--readme-project", "Rich", "--readme-project", "Click"],
            "incomplete README group lacks matching coverage: Click",
        ),
    ):
        expanded = []
        for option in options:
            expanded.append(option)
            if option == "--readme-overview":
                expanded.append(str(ROOT / "tests/fixtures/benchmark-charts"))
        completed = subprocess.run(
            [sys.executable, renderer, *expanded, "--output-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2
        assert diagnostic in completed.stderr
        assert not (tmp_path / "readme-overview.svg").exists()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"reference_median_ms": 9000}, "median differs"),
        ({"reference_samples_ms": [8000]}, "at least two"),
        ({"reference_samples": 3}, "sample count differs"),
        ({"reference_samples_ms": [float("nan"), 8000]}, "positive finite"),
        ({"parity": "failed"}, "unqualified"),
        ({"native_status": "unsupported_unknown"}, "unqualified"),
        ({"route": "other route"}, "unqualified"),
        ({"wheel_sha256": "a" * 63}, "SHA-256"),
        ({"candidate_median_ms": 1}, "cannot have timing"),
    ],
)
def test_readme_stock_only_rejects_unqualified_data(
    change: dict[str, object], message: str
) -> None:
    payload, coverage, parallel = _stock_only_fixture()
    rows = payload["stock_only_rows"]
    assert isinstance(rows, list)
    row = rows[0]
    assert isinstance(row, dict)
    row.update(change)
    with pytest.raises(ValueError, match=message):
        parse_stock_only_rows(payload, coverage, parallel)
