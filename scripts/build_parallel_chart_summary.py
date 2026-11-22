"""Build the three-route parallel chart from retained, parity-gated timings."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any

ROUTES = ("stock-xdist-4", "pytest-native-xdist-4", "pytest-native-j4")
ROUTE_LABEL = "Four workers · capture disabled · JUnit"


def _has_pair(argv: list[str], first: str, second: str) -> bool:
    return any(argv[index : index + 2] == [first, second] for index in range(len(argv) - 1))


def _check_route_commands(project: dict[str, Any], wheel_sha256: str) -> None:
    name = project["project"]
    if project.get("wheel_sha256") != wheel_sha256:
        raise ValueError(f"{name}: vfy wheel differs from chart build")
    commands = project.get("route_commands")
    selectors = project.get("selectors")
    if not isinstance(commands, dict) or not isinstance(selectors, list) or not selectors:
        raise ValueError(f"{name}: missing route commands or selectors")
    argvs = []
    roots = []
    for route in ROUTES:
        command = commands.get(route)
        if not isinstance(command, dict) or not isinstance(command.get("argv"), list):
            raise ValueError(f"{name}: missing {route} command")
        argv = command["argv"]
        if not all(isinstance(item, str) for item in argv) or argv[-len(selectors) :] != selectors:
            raise ValueError(f"{name}: {route} command changes selected suite")
        if not any(item.startswith("--junitxml=") for item in argv):
            raise ValueError(f"{name}: {route} command lacks JUnit output")
        if not _has_pair(argv, "-o", "junit_logging=all") or not _has_pair(
            argv, "-o", "junit_log_passing_tests=true"
        ):
            raise ValueError(f"{name}: {route} command changes JUnit settings")
        if "--capture=no" not in argv:
            raise ValueError(f"{name}: {route} command lacks shared capture setting")
        if not isinstance(command.get("cwd"), str):
            raise ValueError(f"{name}: {route} command lacks project checkout")
        argvs.append(argv)
        roots.append(command.get("cwd"))
    if len({argv[0] for argv in argvs}) != 1 or len(set(roots)) != 1:
        raise ValueError(f"{name}: routes use different project environments")
    stock, native_xdist, native_groups = argvs
    for argv in (stock, native_xdist):
        worker_options = [arg for arg in argv if arg == "-n" or arg.startswith("--numprocesses")]
        if worker_options != ["-n"]:
            raise ValueError(f"{name}: worker override changes declared four-worker modes")
    if any(arg == "-n" or arg.startswith("--numprocesses") for arg in native_groups):
        raise ValueError(f"{name}: worker override changes declared four-worker modes")
    if (
        not _has_pair(stock, "-m", "pytest")
        or not _has_pair(stock, "-n", "4")
        or "--dist=load" not in stock
        or not _has_pair(native_xdist, "-m", "vfy")
        or not _has_pair(native_xdist, "vfy", "test")
        or not _has_pair(native_xdist, "-n", "4")
        or "--dist=load" not in native_xdist
        or "--no-cov" not in native_xdist
        or not _has_pair(native_groups, "-m", "vfy")
        or not _has_pair(native_groups, "vfy", "test")
        or "--workers=4" not in native_groups
        or "--no-cov" not in native_groups
    ):
        raise ValueError(f"{name}: route commands do not use declared four-worker modes")


def _series(project: dict[str, Any], route: str, runs: int) -> tuple[float, int]:
    series = project["timing"]["summary"][route]
    samples = series.get("samples_ms")
    if (
        not isinstance(samples, list)
        or len(samples) != runs
        or any(
            isinstance(sample, bool)
            or not isinstance(sample, (int, float))
            or not math.isfinite(sample)
            or sample <= 0
            for sample in samples
        )
    ):
        raise ValueError(f"{project['project']}: {route} needs {runs} positive samples")
    median = float(statistics.median(float(sample) for sample in samples))
    reported = series.get("median_ms")
    if not isinstance(reported, (int, float)) or not math.isclose(
        median, reported, abs_tol=0.000001
    ):
        raise ValueError(f"{project['project']}: {route} median differs from raw samples")
    recorded_samples = [
        sample["seconds"] * 1000
        for sample in project["timing"]["samples"]
        if sample.get("phase") == "sample" and sample.get("route") == route
    ]
    if len(recorded_samples) != runs or any(
        not math.isclose(actual, retained, abs_tol=0.000001)
        for actual, retained in zip(recorded_samples, samples, strict=True)
    ):
        raise ValueError(f"{project['project']}: {route} timed samples differ from summary")
    return median, runs


def build_summary(record: dict[str, Any], *, source: str) -> dict[str, Any]:
    machine = record.get("machine")
    if not isinstance(machine, dict):
        raise ValueError("parallel record requires machine provenance")
    wheel_sha256 = record.get("wheel_sha256")
    if not isinstance(wheel_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", wheel_sha256) is None:
        raise ValueError("parallel record requires pinned vfy wheel digest")
    raw_projects = record.get("projects")
    if not isinstance(raw_projects, list):
        raise ValueError("parallel record requires projects")
    rows = []
    seen: set[str] = set()
    for project in raw_projects:
        if not isinstance(project, dict) or project.get("status") != "measured":
            continue
        name = project.get("project")
        if not isinstance(name, str) or not name or name in seen:
            raise ValueError("parallel result has missing or duplicate project name")
        seen.add(name)
        method = project.get("method")
        sampling = project.get("sampling")
        controller = project.get("controller")
        source_inputs = project.get("source_input_sha256")
        provenance = (method, sampling, controller, source_inputs)
        if not all(isinstance(item, dict) for item in provenance):
            raise ValueError(f"{name}: missing method or source provenance")
        if (
            controller.get("dirty") is not False
            or not controller.get("revision")
            or not source_inputs
            or sampling.get("mode") != "diagnostic"
            or sampling.get("warmups") != 0
            or sampling.get("measured_runs", 0) < 2
            or method.get("timing_routes") != list(ROUTES)
            or method.get("distribution") != "load"
            or method.get("workers") != [0, 4]
            or method.get("native_workers") != [4]
            or method.get("capture") != "no"
        ):
            raise ValueError(f"{name}: parallel comparison protocol is not qualified")
        runs = sampling["measured_runs"]
        _check_route_commands(project, wheel_sha256)
        cases = project.get("cases")
        parity = project.get("parity")
        if not isinstance(cases, int) or cases <= 0 or not isinstance(parity, dict):
            raise ValueError(f"{name}: missing cases or parity")
        for route in ("stock-xdist-0", *ROUTES):
            gate = parity.get(route)
            if (
                not isinstance(gate, dict)
                or gate.get("passed") is not True
                or gate.get("cases") != cases
                or gate.get("errors") != []
            ):
                raise ValueError(f"{name}: {route} parity gate did not pass")
        reference, count = _series(project, ROUTES[0], runs)
        candidate, candidate_count = _series(project, ROUTES[1], runs)
        filegroups, filegroups_count = _series(project, ROUTES[2], runs)
        rows.append(
            {
                "project": {
                    "attrs": "attrs",
                    "platformdirs": "Platformdirs",
                    "fastapi": "FastAPI",
                }.get(name, name.title()),
                "scope": f"{cases:,} cases; {method['scope']}",
                "display_scope": "Configured suite"
                if name in {"packaging", "black", "click"}
                else "Full suite",
                "route": ROUTE_LABEL,
                "reference_median_ms": reference,
                "candidate_median_ms": candidate,
                "filegroups_median_ms": filegroups,
                "reference_samples": count,
                "candidate_samples": candidate_count,
                "filegroups_samples": filegroups_count,
                "warmups": sampling["warmups"],
                "status": sampling["mode"],
                "parity": "passed",
                "source": source,
            }
        )
    if len(rows) < 2:
        raise ValueError("parallel chart needs two measured projects")
    return {
        "schema": 1,
        "charts": [
            {
                "kind": "parallel",
                "title": "Parallel test execution",
                "route": ROUTE_LABEL,
                "context": f"{len(rows)} full suites · {machine['python_implementation']} "
                f"{'.'.join(machine['python'].split('.')[:2])} · Linux",
                "reference": "pytest-xdist",
                "candidate": "Native xdist",
                "rows": rows,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    record = json.loads(arguments.record.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[1]
    source = arguments.record.resolve().relative_to(root).as_posix()
    summary = build_summary(record, source=source)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
