"""Extract a stock-only parallel row when public native execution is unsupported."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import statistics
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from validation.pytest_runner.check_native_xdist_adoption import junit_records

ROOT = Path(__file__).resolve().parents[1]

REASONS = {
    "fastapi": (
        "unsupported_required_plugins",
        "the native pytest engine does not support explicitly requested third-party plug-ins",
        1,
    ),
    "black": (
        "unsupported_conftest_plugins",
        "native pytest does not load pytest_plugins declared by",
        4,
    ),
}
ROUTE = "Four workers · capture disabled · JUnit"


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _junit(path: Path, root: Path, basetemp: Path) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as directory:
        xml = Path(directory) / "junit.xml"
        with gzip.open(path, "rb") as stream:
            xml.write_bytes(stream.read())
        return junit_records(xml, root, basetemp)


def _outcomes(cases: list[dict[str, Any]]) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    return sorted(
        (
            tuple(case["identity"]),
            tuple(
                child["tag"]
                for child in case["children"]
                if child["tag"] in {"failure", "error", "skipped"}
            ),
        )
        for case in cases
    )


def _command(gate: dict[str, Any], *, workers: int, selectors: list[str]) -> tuple[Path, Path]:
    command = _json(Path(gate["artifacts"]["command.json"]["path"]))
    argv = command["argv"]
    if (
        not isinstance(argv, list)
        or not all(isinstance(value, str) for value in argv)
        or argv[-len(selectors) :] != selectors
        or argv.count("-n") != 1
        or not any(
            argv[index : index + 2] == ["-n", str(workers)] for index in range(len(argv) - 1)
        )
        or "--dist=load" not in argv
        or "--capture=no" not in argv
        or not any(value.startswith("--junitxml=") for value in argv)
        or not any(
            argv[index : index + 2] == ["-o", "junit_logging=all"] for index in range(len(argv) - 1)
        )
        or not any(
            argv[index : index + 2] == ["-o", "junit_log_passing_tests=true"]
            for index in range(len(argv) - 1)
        )
        or argv[1:4] != ["-P", "-m", "pytest"]
    ):
        raise ValueError(f"stock-xdist-{workers} command changed the four-worker protocol")
    if not isinstance(command.get("cwd"), str):
        raise ValueError("stock gate is missing checkout identity")
    root = Path(command["cwd"])
    index = argv.index("--basetemp") if "--basetemp" in argv else -1
    basetemp = Path(argv[index + 1]) if index >= 0 else root.parent / "pytest-tmp"
    return root, basetemp


def _identity(path: Path) -> dict[str, Any]:
    identity = _json(path)
    if not isinstance(identity.get("build"), dict) or not isinstance(
        identity.get("prepared"), dict
    ):
        raise ValueError("missing pinned build or upstream identity")
    return identity


def _validate_native_blocker(blocked_preflight: Path, project: str) -> tuple[str, str]:
    status_name, reason, exit_status = REASONS[project]
    blocked = _json(blocked_preflight / "parity.json")
    for name in ("pytest-native-xdist-4", "pytest-native-j4"):
        gate = blocked.get(name)
        if not isinstance(gate, dict) or gate.get("passed") is not False:
            raise ValueError(f"{name} did not retain an unsupported-native gate")
        stderr = Path(gate["artifacts"]["stderr.txt"]["path"]).read_text(encoding="utf-8")
        status = _json(Path(gate["artifacts"]["status.json"]["path"]))
        if reason not in stderr or status.get("exit_status") != exit_status:
            raise ValueError(f"{name} blocker is not the declared public plugin incompatibility")
    return status_name, reason


def _validated_samples(
    project: dict[str, Any], sampling: dict[str, Any], gate_cases: list[dict[str, Any]]
) -> tuple[list[float], float]:
    raw = project.get("timing", {})
    summary = raw.get("summary", {}).get("stock-xdist-4", {})
    values = summary.get("samples_ms")
    count = sampling["measured_runs"]
    if (
        not isinstance(values, list)
        or len(values) != count
        or any(
            type(value) not in (float, int) or not math.isfinite(value) or value <= 0
            for value in values
        )
    ):
        raise ValueError("stock samples are incomplete or invalid")
    validated_values = [float(value) for value in values]
    median = float(statistics.median(validated_values))
    if not math.isclose(median, summary.get("median_ms", float("nan")), abs_tol=0.000001):
        raise ValueError("stock median differs from raw samples")
    samples = [sample for sample in raw.get("samples", []) if sample.get("phase") == "sample"]
    if len(samples) != count or any(sample.get("route") != "stock-xdist-4" for sample in samples):
        raise ValueError("stock-only timing contains another route or the wrong sample count")
    parity = project["parity"]["stock-xdist-4"]
    root, basetemp = _command(parity, workers=4, selectors=project["selectors"])
    for sample, milliseconds in zip(samples, validated_values, strict=True):
        if not math.isclose(sample["seconds"] * 1000, milliseconds, abs_tol=0.000001):
            raise ValueError("stock sample differs from summary")
        artifact = sample["artifacts"]["junit.xml"]
        if _junit(Path(artifact["path"]), root, basetemp) != gate_cases:
            raise ValueError("stock sample JUnit differs from full-suite preflight")
    return validated_values, median


def build_summary(stock_path: Path, blocked_preflight: Path) -> dict[str, Any]:
    stock = _json(stock_path)
    if len(stock.get("projects", [])) != 1:
        raise ValueError("stock-only record must contain one project")
    project = stock["projects"][0]
    method, sampling = stock.get("method"), stock.get("sampling")
    key = project.get("project")
    if (
        key not in REASONS
        or project.get("status") != "measured"
        or not isinstance(method, dict)
        or not isinstance(sampling, dict)
        or method.get("timing_routes") != ["stock-xdist-4"]
        or method.get("workers") != [0, 4]
        or method.get("capture") != "no"
        or method.get("distribution") != "load"
        or sampling.get("mode") != "diagnostic"
        or sampling.get("warmups") != 0
        or sampling.get("measured_runs", 0) < 2
    ):
        raise ValueError("stock-only run does not meet the chart protocol")
    if stock.get("controller", {}).get("dirty") is not False:
        raise ValueError("stock controller must be clean")
    stock_identity = _identity(stock_path.parent / key / "identity.json")
    blocked_root = blocked_preflight.parent
    blocked_identity = _identity(blocked_root / "identity.json")
    blocked_run = _json(blocked_root.parent / "result.json")
    if (
        stock_identity["build"] != blocked_identity["build"]
        or stock_identity["prepared"]["preparation_inputs"]["project"]
        != blocked_identity["prepared"]["preparation_inputs"]["project"]
        or stock.get("machine") != blocked_run.get("machine")
    ):
        raise ValueError("stock measurement and native blocker use different inputs")
    for field in (
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
    ):
        if stock_identity["installed"][field] != blocked_identity["installed"][field]:
            raise ValueError(f"stock measurement and native blocker differ in runtime {field}")
    wheel = stock_identity["build"].get("wheel_sha256")
    if not isinstance(wheel, str) or re.fullmatch(r"[0-9a-f]{64}", wheel) is None:
        raise ValueError("missing exact wheel digest")
    revision = stock_identity["prepared"]["preparation_inputs"]["project"]["revision"]
    parity = project.get("parity", {})
    cases = project.get("cases")
    selectors = project.get("selectors")
    if not isinstance(cases, int) or cases <= 0 or not isinstance(selectors, list) or not selectors:
        raise ValueError("missing full suite identity")
    gate_cases = {}
    for name, workers in (("stock-xdist-0", 0), ("stock-xdist-4", 4)):
        gate = parity.get(name)
        if (
            not isinstance(gate, dict)
            or gate.get("passed") is not True
            or gate.get("cases") != cases
        ):
            raise ValueError(f"{name} failed its full-suite gate")
        root, basetemp = _command(gate, workers=workers, selectors=selectors)
        gate_cases[name] = _junit(Path(gate["artifacts"]["junit.xml"]["path"]), root, basetemp)
        if len(gate_cases[name]) != cases:
            raise ValueError(f"{name} JUnit inventory differs from declared count")
    if _outcomes(gate_cases["stock-xdist-0"]) != _outcomes(gate_cases["stock-xdist-4"]):
        raise ValueError("serial and four-worker stock inventories or outcomes differ")
    native_status, reason = _validate_native_blocker(blocked_preflight, key)
    values, median = _validated_samples(project, sampling, gate_cases["stock-xdist-4"])
    count = sampling["measured_runs"]
    return {
        "schema": 1,
        "stock_only_rows": [
            {
                "project": key.title() if key != "fastapi" else "FastAPI",
                "scope": (
                    f"{cases:,} cases; "
                    f"{stock_identity['prepared']['preparation_inputs']['project']['scope']}"
                ),
                "route": ROUTE,
                "workers": 4,
                "capture": "no",
                "report": "JUnit",
                "reference_median_ms": median,
                "reference_samples_ms": values,
                "reference_samples": count,
                "native_status": native_status,
                "reason": reason,
                "parity": "passed",
                "controller_revision": stock["controller"]["revision"],
                "upstream_revision": revision,
                "wheel_sha256": wheel,
                "artifact": {
                    "status": "retained-local",
                    "stock_record_sha256": _sha256(stock_path),
                    "native_preflight_sha256": _sha256(blocked_preflight / "parity.json"),
                },
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_record", type=Path)
    parser.add_argument("blocked_native_preflight", type=Path)
    parser.add_argument(
        "--additional-row", nargs=2, action="append", default=[], metavar=("STOCK", "BLOCKED")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = build_summary(args.stock_record, args.blocked_native_preflight)
    for stock, blocked in args.additional_row:
        summary["stock_only_rows"].extend(
            build_summary(Path(stock), Path(blocked))["stock_only_rows"]
        )
    names = [row["project"] for row in summary["stock_only_rows"]]
    if len(names) != len(set(names)):
        raise ValueError("stock-only summary repeats a project")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
