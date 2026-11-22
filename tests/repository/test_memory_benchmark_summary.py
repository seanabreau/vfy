"""File-backed evidence checks for the compact peak RSS summary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts.summarize_memory_benchmarks import _commands, _provenance, build_summary


def _write(path: Path, value: dict[str, Any]) -> str:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> Path:
    controller, revision, upstream = ("a" * 40, "b" * 40, "c" * 40)
    wheel, extension, collector = ("d" * 64, "e" * 64, "f" * 64)
    build = {
        "source_revision": revision,
        "wheel_sha256": wheel,
        "extension_sha256": extension,
        "native_profile": "release",
    }
    provenance = {
        "source": {"revision": controller, "dirty": False},
        "engine_source": {"revision": revision},
        "upstream_revision": upstream,
        "vfy_build": build,
        "interpreter": {"extension_sha256": extension},
        "validation": {"status": "passed"},
    }
    methodology = {
        "mode": "diagnostic",
        "phases": ["serial"],
        "warmups": 0,
        "measured_runs": 2,
        "sampling": {"warmups": 0, "measured_runs": 2},
        "coverage_context": "test",
        "reports": ["term", "json:coverage.json"],
        "resources": {
            "method": "linux-single-child-rusage",
            "rss_scope": "largest individual process peak, not concurrent process-tree RSS",
        },
    }
    common = ["$PYTHON", "-m", "pytest", "-p", "outcome_plugin"]
    coverage = [
        "--cov=sample",
        "--cov-config=/benchmarks/configs/project-coverage.coveragerc",
        "--cov-context=test",
        "--cov-report=term",
        "--cov-report=json:coverage.json",
    ]
    commands = {
        "plain pytest": [{"phase": "serial", "argv": [*common, "tests"]}],
        "vfy": [
            {"phase": "serial", "argv": [*common, "-p", "vfy.pytest_plugin", *coverage, "tests"]}
        ],
        "pytest-cov": [
            {"phase": "serial", "argv": [*common, "-p", "pytest_cov.plugin", *coverage, "tests"]}
        ],
    }
    project = {
        "name": "Sample",
        "scope": "full configured suite",
        "commit": upstream,
        "vfy_commit": revision,
        "phases": ["serial"],
    }
    record: dict[str, Any] = {
        "schema": 3,
        "benchmark": "projects",
        "project": project,
        "commands": commands,
        "methodology": methodology,
        "provenance": provenance,
        "preparation": {"python": "/venv/bin/python", "revision": upstream, "vfy_wheel": build},
        "mode": "diagnostic",
        "warmups": 0,
        "runs": 2,
    }
    parity = {
        "parity": "exact",
        "project": project["name"],
        "commit": upstream,
        "vfy_commit": revision,
        "methodology": methodology,
        "provenance": provenance,
    }
    parity_path = tmp_path / "parity.json"
    record["resource_parity_artifact"] = {
        "path": str(parity_path),
        "sha256": _write(parity_path, parity),
    }
    resources = {}
    for tool, base in (("plain pytest", 1000), ("vfy", 2000), ("pytest-cov", 3000)):
        record[tool] = {"samples_ms": [10.0, 11.0]}
        samples = [base, base + 200]
        descriptors = []
        for index, peak in enumerate(samples):
            raw = {
                "schema_version": 1,
                "method": "linux-single-child-rusage",
                "rss_scope": "largest process RSS, not a sum of concurrent descendants",
                "collector_sha256": collector,
                "child_command": ["/venv/bin/python", *commands[tool][0]["argv"][1:]],
                "child_exit_status": 0,
                "child_wall_ms": 10.0 + index,
                "peak_rss_bytes": peak,
            }
            path = tmp_path / f"{tool.replace(' ', '-')}-{index}.json"
            descriptors.append(
                {
                    **raw,
                    "path": str(path),
                    "sha256": _write(path, raw),
                    "validation": {
                        "outcomes": "passed",
                        "coverage_data": "not applicable" if tool == "plain pytest" else "passed",
                    },
                }
            )
        resources[tool] = {
            "samples_peak_rss_bytes": samples,
            "median_peak_rss_bytes": base + 100,
            "records": descriptors,
        }
    record["resources"] = resources
    path = tmp_path / "result.json"
    _write(path, record)
    return path


def test_summary_recomputes_peak_rss_from_retained_child_records(tmp_path: Path) -> None:
    result = build_summary([_fixture(tmp_path)])
    assert result == {
        "schema": 1,
        "metric": "largest_process_peak_rss_bytes",
        "method": "linux-single-child-rusage",
        "route": "serial-stock-pytest-coverage",
        "rows": [
            {
                "project": "Sample",
                "scope": "full configured suite",
                "parity": "exact",
                "warmups": 0,
                "runs": 2,
                "values": {
                    "plain pytest": {"samples_bytes": [1000, 1200], "median_bytes": 1100},
                    "vfy": {"samples_bytes": [2000, 2200], "median_bytes": 2100},
                    "pytest-cov": {"samples_bytes": [3000, 3200], "median_bytes": 3100},
                },
                "provenance": {
                    "controller_revision": "a" * 40,
                    "upstream_revision": "c" * 40,
                    "source_revision": "b" * 40,
                    "wheel_sha256": "d" * 64,
                    "extension_sha256": "e" * 64,
                    "collector_sha256": "f" * 64,
                },
            }
        ],
    }


def test_project_result_uses_project_revision_shape(tmp_path: Path) -> None:
    path = _fixture(tmp_path)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert "vfy_commit" not in record
    assert record["project"]["vfy_commit"] == record["provenance"]["vfy_build"]["source_revision"]
    assert (
        _provenance(record, path)["wheel_sha256"]
        == record["provenance"]["vfy_build"]["wheel_sha256"]
    )
    assert set(_commands(record, path)) == {"plain pytest", "vfy", "pytest-cov"}


@pytest.mark.parametrize("tamper", ["gate", "raw", "sample", "receipt", "wheel", "command"])
def test_summary_rejects_missing_or_changed_evidence(tmp_path: Path, tamper: str) -> None:
    path = _fixture(tmp_path)
    record = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "gate":
        parity_path = Path(record["resource_parity_artifact"]["path"])
        gate = json.loads(parity_path.read_text(encoding="utf-8"))
        gate["parity"] = "failed"
        record["resource_parity_artifact"]["sha256"] = _write(parity_path, gate)
    elif tamper == "raw":
        Path(record["resources"]["vfy"]["records"][0]["path"]).write_text("{}\n")
    elif tamper == "sample":
        record["resources"]["vfy"]["samples_peak_rss_bytes"][0] = 9999
    elif tamper == "receipt":
        record["resources"]["vfy"]["records"][0]["validation"]["coverage_data"] = "failed"
    elif tamper == "wheel":
        record["preparation"]["vfy_wheel"]["wheel_sha256"] = "0" * 64
    else:
        record["commands"]["vfy"][0]["argv"].remove("--cov-context=test")
    _write(path, record)
    with pytest.raises(ValueError, match=r"parity|artifact|sample|record|provenance|command"):
        build_summary([path])
