"""Stock-only chart extraction keeps its full-suite and unsupported-native proof."""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from scripts.build_stock_parallel_chart_summary import REASONS, build_summary


def _write(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _fixture(tmp_path: Path, project: str = "fastapi") -> tuple[Path, Path]:
    stock = tmp_path / "stock"
    blocked = tmp_path / "blocked" / project
    checkout = tmp_path / "checkout"
    wheel = "a" * 64
    identity = {
        "build": {"wheel_sha256": wheel},
        "prepared": {
            "preparation_inputs": {
                "project": {"revision": "pinned-revision", "scope": "official suite"}
            }
        },
        "installed": dict.fromkeys(
            (
                "dependencies",
                "implementation",
                "vfy",
                "vfy_extension_sha256",
                "vfy_files",
                "native_profile",
                "native_shell_sha256",
                "pytest",
                "pytest_files",
                "pytest_runner_sha256",
                "python",
            ),
            "same-runtime",
        ),
    }
    _write(stock / project / "identity.json", identity)
    _write(blocked / "identity.json", identity)
    machine = {"python": "3.12.14", "hostname": "same-machine"}
    _write(blocked.parent / "result.json", {"machine": machine})
    xml = (
        '<testsuite><testcase classname="tests.test_sample" name="test_ok" time="0.01">'
        "<system-out>same output</system-out></testcase></testsuite>"
    )

    def compressed(path: Path, content: str = xml) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb") as stream:
            stream.write(content.encode())
        return path

    gates = {}
    for workers in (0, 4):
        name = f"stock-xdist-{workers}"
        gate_dir = stock / project / "preflight" / name
        command = _write(
            gate_dir / "command.json",
            {
                "argv": [
                    "/env/python",
                    "-P",
                    "-m",
                    "pytest",
                    "-n",
                    str(workers),
                    "--dist=load",
                    "--capture=no",
                    "--junitxml=results.xml",
                    "-o",
                    "junit_logging=all",
                    "-o",
                    "junit_log_passing_tests=true",
                    "tests",
                ],
                "cwd": str(checkout),
            },
        )
        junit = compressed(gate_dir / "junit.xml.gz")
        gates[name] = {
            "passed": True,
            "cases": 1,
            "errors": [],
            "artifacts": {
                "command.json": {"path": str(command)},
                "junit.xml": {"path": str(junit)},
            },
        }
    for name in ("pytest-native-xdist-4", "pytest-native-j4"):
        gate_dir = blocked / "preflight" / name
        stderr = gate_dir / "stderr.txt"
        stderr.parent.mkdir(parents=True, exist_ok=True)
        stderr.write_text(REASONS[project][1], encoding="utf-8")
        status = _write(gate_dir / "status.json", {"exit_status": REASONS[project][2]})
        gates[name] = {
            "passed": False,
            "artifacts": {
                "stderr.txt": {"path": str(stderr)},
                "status.json": {"path": str(status)},
            },
        }
    _write(
        blocked / "preflight" / "parity.json",
        {key: gates[key] for key in ("pytest-native-xdist-4", "pytest-native-j4")},
    )
    samples = []
    for index, seconds in enumerate((1.0, 2.0)):
        junit = compressed(stock / project / "timing" / f"sample-{index}" / "junit.xml.gz")
        samples.append(
            {
                "phase": "sample",
                "route": "stock-xdist-4",
                "seconds": seconds,
                "artifacts": {"junit.xml": {"path": str(junit)}},
            }
        )
    record = {
        "machine": machine,
        "controller": {"dirty": False, "revision": "clean-controller"},
        "method": {
            "timing_routes": ["stock-xdist-4"],
            "workers": [0, 4],
            "capture": "no",
            "distribution": "load",
            "scope": "complete suite",
        },
        "sampling": {"mode": "diagnostic", "warmups": 0, "measured_runs": 2},
        "projects": [
            {
                "project": project,
                "status": "measured",
                "cases": 1,
                "selectors": ["tests"],
                "parity": {key: gates[key] for key in ("stock-xdist-0", "stock-xdist-4")},
                "timing": {
                    "summary": {
                        "stock-xdist-4": {"samples_ms": [1000.0, 2000.0], "median_ms": 1500.0}
                    },
                    "samples": samples,
                },
            }
        ],
    }
    return _write(stock / "result.json", record), blocked / "preflight"


def test_stock_only_chart_requires_full_sample_junit_and_native_blocker(tmp_path: Path) -> None:
    stock, blocked = _fixture(tmp_path)
    row = build_summary(stock, blocked)["stock_only_rows"][0]
    assert row["reference_samples_ms"] == [1000.0, 2000.0]
    assert row["reference_median_ms"] == 1500.0
    assert row["native_status"] == "unsupported_required_plugins"

    altered = copy.deepcopy(json.loads(stock.read_text()))
    altered["projects"][0]["timing"]["summary"]["stock-xdist-4"]["median_ms"] = 1000.0
    _write(stock, altered)
    with pytest.raises(ValueError, match="median differs"):
        build_summary(stock, blocked)


def test_stock_only_chart_rejects_changed_sample_capture(tmp_path: Path) -> None:
    stock, blocked = _fixture(tmp_path)
    sample = json.loads(stock.read_text())["projects"][0]["timing"]["samples"][0]
    path = Path(sample["artifacts"]["junit.xml"]["path"])
    with gzip.open(path, "wb") as stream:
        stream.write(
            b'<testsuite><testcase classname="tests.test_sample" name="test_ok">'
            b"<system-out>different</system-out></testcase></testsuite>"
        )
    with pytest.raises(ValueError, match="sample JUnit differs"):
        build_summary(stock, blocked)


def test_black_stock_only_chart_requires_conftest_plugin_blocker(tmp_path: Path) -> None:
    stock, blocked = _fixture(tmp_path, "black")
    row = build_summary(stock, blocked)["stock_only_rows"][0]
    assert row["project"] == "Black"
    assert row["native_status"] == "unsupported_conftest_plugins"
    assert "official suite" in row["scope"]
    gate = blocked / "pytest-native-xdist-4" / "status.json"
    _write(gate, {"exit_status": 1})
    with pytest.raises(ValueError, match="plugin incompatibility"):
        build_summary(stock, blocked)
