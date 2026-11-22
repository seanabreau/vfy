"""Parallel charts require three timed routes and full-suite parity gates."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from scripts.build_parallel_chart_summary import build_summary
from scripts.render_benchmark_charts import parse_project_charts, render_project_chart


def _record() -> dict[str, Any]:
    # Synthetic fixtures test extraction only; they are not benchmark evidence.
    routes = ("stock-xdist-4", "pytest-native-xdist-4", "pytest-native-j4")
    projects = []
    for name, cases, values in (
        ("packaging", 100, ((8.0, 10.0), (4.0, 6.0), (3.0, 4.0))),
        ("platformdirs", 20, ((1.0, 2.0), (2.0, 3.0), (1.0, 1.5))),
    ):
        summary = {}
        samples = []
        for route, seconds in zip(routes, values, strict=True):
            milliseconds = [number * 1000 for number in seconds]
            summary[route] = {
                "samples_ms": milliseconds,
                "median_ms": sum(milliseconds) / 2,
            }
            samples.extend(
                {"phase": "sample", "route": route, "index": index, "seconds": number}
                for index, number in enumerate(seconds)
            )
        route_commands = {
            "stock-xdist-4": {
                "argv": [
                    "/env/python",
                    "-m",
                    "pytest",
                    "-n",
                    "4",
                    "--dist=load",
                    "--capture=no",
                    "--junitxml=stock.xml",
                    "-o",
                    "junit_logging=all",
                    "-o",
                    "junit_log_passing_tests=true",
                    "tests",
                ],
                "cwd": f"/{name}",
            },
            "pytest-native-xdist-4": {
                "argv": [
                    "/env/python",
                    "-m",
                    "vfy",
                    "test",
                    "-n",
                    "4",
                    "--dist=load",
                    "--no-cov",
                    "--capture=no",
                    "--junitxml=native.xml",
                    "-o",
                    "junit_logging=all",
                    "-o",
                    "junit_log_passing_tests=true",
                    "tests",
                ],
                "cwd": f"/{name}",
            },
            "pytest-native-j4": {
                "argv": [
                    "/env/python",
                    "-m",
                    "vfy",
                    "test",
                    "--workers=4",
                    "--no-cov",
                    "--capture=no",
                    "--junitxml=groups.xml",
                    "-o",
                    "junit_logging=all",
                    "-o",
                    "junit_log_passing_tests=true",
                    "tests",
                ],
                "cwd": f"/{name}",
            },
        }
        projects.append(
            {
                "project": name,
                "cases": cases,
                "selectors": ["tests"],
                "status": "measured",
                "wheel_sha256": "a" * 64,
                "route_commands": route_commands,
                "controller": {"dirty": False, "revision": "fixture"},
                "source_input_sha256": {"fixture": "a" * 64},
                "sampling": {"mode": "diagnostic", "warmups": 0, "measured_runs": 2},
                "method": {
                    "timing_routes": list(routes),
                    "distribution": "load",
                    "workers": [0, 4],
                    "native_workers": [4],
                    "capture": "no",
                    "scope": "complete configured stock inventory; no test subsetting",
                },
                "parity": {
                    route: {"passed": True, "cases": cases, "errors": []}
                    for route in ("stock-xdist-0", *routes)
                },
                "timing": {"summary": summary, "samples": samples},
            }
        )
    return {
        "wheel_sha256": "a" * 64,
        "machine": {"python_implementation": "CPython", "python": "3.12.14"},
        "projects": projects,
    }


def test_parallel_summary_uses_three_raw_sample_series() -> None:
    record = _record()
    record["projects"][1]["controller"]["revision"] = "fixture-followup"
    payload = build_summary(record, source="fixture/parallel.json")
    chart = parse_project_charts(payload)[0]
    svg = render_project_chart(chart)

    assert chart.kind == "parallel"
    assert chart.rows[0].reference_median_ms == 9000
    assert chart.rows[0].candidate_median_ms == 5000
    assert chart.rows[0].filegroups_median_ms == 3500
    assert chart.rows[1].reference_median_ms == 1500
    assert "vfy native workers (file scheduler) 3.500 seconds" in svg
    assert "2 full suites" in svg


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("parity", "parity gate did not pass"),
        ("sample", "timed samples differ"),
        ("median", "median differs"),
        ("workers", "protocol is not qualified"),
        ("wheel", "wheel differs"),
        ("command", "four-worker modes"),
        ("worker_override", "worker override"),
        ("capture", "shared capture setting"),
    ],
)
def test_parallel_summary_rejects_unqualified_evidence(tamper: str, message: str) -> None:
    record = copy.deepcopy(_record())
    project = record["projects"][0]
    if tamper == "parity":
        project["parity"]["pytest-native-j4"]["passed"] = False
    elif tamper == "sample":
        project["timing"]["samples"][0]["seconds"] += 1
    elif tamper == "median":
        project["timing"]["summary"]["pytest-native-xdist-4"]["median_ms"] += 1
    elif tamper == "wheel":
        project["wheel_sha256"] = "b" * 64
    elif tamper == "command":
        project["route_commands"]["pytest-native-j4"]["argv"].remove("--workers=4")
    elif tamper == "worker_override":
        project["route_commands"]["stock-xdist-4"]["argv"][-1:-1] = ["-n", "0"]
    elif tamper == "capture":
        project["route_commands"]["pytest-native-xdist-4"]["argv"].remove("--capture=no")
    else:
        project["method"]["native_workers"] = [8]

    with pytest.raises(ValueError, match=message):
        build_summary(record, source="fixture/parallel.json")
