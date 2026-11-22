"""Compact native evidence rejects changed artifacts and mixed builds."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from scripts.build_native_parallel_chart_summary import build_native_summary
from scripts.build_parallel_evidence_record import build_record

ROUTES = ("stock-xdist-4", "pytest-native-xdist-4", "pytest-native-j4")
JUNIT = gzip.compress(
    b'<testsuite><testcase classname="tests.test_case" name="test_ok"/></testsuite>'
)


def _write(path: Path, value: bytes | dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
    return path


def _artifact(path: Path) -> dict[str, str]:
    return {"path": str(path), "retained_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _fixture(tmp_path: Path, name: str, wheel: str) -> Path:
    root = tmp_path / name
    build_manifest = _write(tmp_path / "build.json", {"wheel_sha256": wheel})
    identity = {
        "build": {
            "path": str(build_manifest),
            "wheel_sha256": wheel,
            "source_revision": "pinned-engine",
        },
        "prepared": {
            "wheel_sha256": wheel,
            "revision": "pinned-upstream",
            "preparation_inputs": {"project": name},
            "benchmark_inputs": {},
            "setup_diff": "",
        },
    }
    _write(root / name / "identity.json", identity)
    parity = {}
    for route in ("stock-xdist-0", *ROUTES):
        destination = root / name / "preflight" / route
        if route.startswith("stock"):
            count = route.rsplit("-", 1)[1]
            argv = ["/env/python", "-P", "-m", "pytest", "-n", count, "--dist=load"]
        elif route == "pytest-native-xdist-4":
            argv = [
                "/env/python",
                "-m",
                "vfy",
                "test",
                "-n",
                "4",
                "--dist=load",
                "--no-cov",
            ]
        else:
            argv = ["/env/python", "-m", "vfy", "test", "--workers=4", "--no-cov"]
        argv.extend(
            [
                "--capture=no",
                "--junitxml=results.xml",
                "-o",
                "junit_logging=all",
                "-o",
                "junit_log_passing_tests=true",
                "tests",
            ]
        )
        command = _write(destination / "command.json", {"argv": argv, "cwd": "/checkout"})
        junit = _write(destination / "junit.xml.gz", JUNIT)
        parity[route] = {
            "passed": True,
            "cases": 1,
            "errors": [],
            "artifacts": {"command.json": _artifact(command), "junit.xml": _artifact(junit)},
        }
    _write(root / name / "preflight" / "parity.json", parity)
    samples = []
    summary = {}
    for route in ROUTES:
        milliseconds = []
        for index, seconds in enumerate((1.0, 2.0)):
            junit = _write(root / name / "timing" / f"{route}-{index}.gz", JUNIT)
            samples.append(
                {
                    "phase": "sample",
                    "route": route,
                    "index": index,
                    "seconds": seconds,
                    "artifacts": {"junit.xml": _artifact(junit)},
                }
            )
            milliseconds.append(seconds * 1000)
        summary[route] = {"samples_ms": milliseconds, "median_ms": 1500.0}
    timing = {"samples": samples, "summary": summary}
    _write(root / name / "timing" / "samples.json", timing)
    return _write(
        root / "result.json",
        {
            "controller": {"dirty": False, "revision": "clean-controller"},
            "machine": {"python": "3.12.14", "python_implementation": "CPython"},
            "sampling": {"mode": "diagnostic", "warmups": 0, "measured_runs": 2},
            "method": {
                "timing_routes": list(ROUTES),
                "workers": [0, 4],
                "native_workers": [4],
                "capture": "no",
                "distribution": "load",
                "scope": "complete configured stock inventory; no test subsetting",
            },
            "source_input_sha256": {"harness": "b" * 64},
            "projects": [
                {
                    "project": name,
                    "status": "measured",
                    "cases": 1,
                    "selectors": ["tests"],
                    "parity": parity,
                    "timing": timing,
                }
            ],
        },
    )


def test_parallel_record_preserves_three_complete_routes(tmp_path: Path) -> None:
    wheel = "a" * 64
    first = _fixture(tmp_path, "attrs", wheel)
    second = _fixture(tmp_path, "rich", wheel)
    record = build_record([first, second])
    assert record["wheel_sha256"] == wheel
    assert [row["project"] for row in record["projects"]] == ["attrs", "rich"]
    assert all(
        set(row["route_commands"]) == {"stock-xdist-0", *ROUTES} for row in record["projects"]
    )
    summary = build_native_summary([first, second])
    assert [row["project"] for row in summary["charts"][0]["rows"]] == ["attrs", "Rich"]
    assert all(row["wheel_sha256"] == wheel for row in summary["charts"][0]["rows"])
    assert all(
        row["engine_source_revision"] == "pinned-engine" for row in summary["charts"][0]["rows"]
    )

    proof = Path(record["projects"][0]["route_commands"]["stock-xdist-4"]["cwd"])
    assert proof == Path("/checkout")
    changed = first.parent / "attrs" / "timing" / "stock-xdist-4-0.gz"
    changed.write_bytes(b"altered")
    with pytest.raises(ValueError, match="modified retained artifact"):
        build_record([first, second])


def test_parallel_record_rejects_mixed_wheels(tmp_path: Path) -> None:
    first = _fixture(tmp_path, "attrs", "a" * 64)
    second = _fixture(tmp_path, "rich", "b" * 64)
    with pytest.raises(ValueError, match="different wheel builds"):
        build_record([first, second])


def test_parallel_record_rejects_changed_capture_with_updated_artifact_hash(
    tmp_path: Path,
) -> None:
    first = _fixture(tmp_path, "attrs", "a" * 64)
    second = _fixture(tmp_path, "rich", "a" * 64)
    raw = json.loads(first.read_text())
    sample = raw["projects"][0]["timing"]["samples"][0]
    junit = Path(sample["artifacts"]["junit.xml"]["path"])
    junit.write_bytes(
        gzip.compress(
            b'<testsuite><testcase classname="tests.test_case" name="test_ok">'
            b"<system-out>changed capture</system-out></testcase></testsuite>"
        )
    )
    sample["artifacts"]["junit.xml"] = _artifact(junit)
    _write(first, raw)
    _write(first.parent / "attrs" / "timing" / "samples.json", raw["projects"][0]["timing"])
    with pytest.raises(ValueError, match="timed JUnit changed"):
        build_record([first, second])
