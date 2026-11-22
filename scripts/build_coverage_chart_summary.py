"""Build a cross-project coverage chart from validated retained records.

Usage: python scripts/build_coverage_chart_summary.py RESULTS/attrs.json \
    RESULTS/platformdirs.json --output RESULTS/summary.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

ROUTE = "Serial stock pytest · test contexts · terminal and JSON reports"


def _read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected JSON object")
    return data


def _median(record: dict[str, Any], tool: str, source: Path, *, minimum: int) -> tuple[float, int]:
    series = record.get(tool)
    if not isinstance(series, dict):
        raise ValueError(f"{source}: missing {tool} measurements")
    samples = series.get("samples_ms")
    if (
        not isinstance(samples, list)
        or len(samples) < minimum
        or any(
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value <= 0
            for value in samples
        )
    ):
        raise ValueError(f"{source}: {tool} requires {minimum} finite positive samples")
    median = float(statistics.median(float(value) for value in samples))
    reported = series.get("median_ms")
    if (
        isinstance(reported, bool)
        or not isinstance(reported, (float, int))
        or not math.isclose(median, reported, rel_tol=0, abs_tol=0.000001)
    ):
        raise ValueError(f"{source}: {tool} reported median differs from samples")
    return median, len(samples)


def _check_commands(record: dict[str, Any], source: Path) -> None:
    commands = record.get("commands")
    if not isinstance(commands, dict):
        raise ValueError(f"{source}: missing timed commands")
    argvs: dict[str, list[str]] = {}
    for tool in ("pytest-cov", "vfy", "plain pytest"):
        phases = commands.get(tool)
        if not isinstance(phases, list) or len(phases) != 1 or phases[0].get("phase") != "serial":
            raise ValueError(f"{source}: expected one serial command per tool")
        argv = phases[0].get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError(f"{source}: malformed {tool} command")
        argvs[tool] = argv
    reference = argvs["pytest-cov"]
    candidate = argvs["vfy"]
    required = {"--cov-context=test", "--cov-report=term"}
    if (
        not required.issubset(reference)
        or not any(
            arg in {"--cov-report=json", "--cov-report=json:coverage.json"} for arg in reference
        )
        or not any(
            arg.endswith("/benchmarks/configs/project-coverage.coveragerc") for arg in reference
        )
        or reference.count("pytest_cov.plugin") != 1
        or candidate
        != ["vfy.pytest_plugin" if arg == "pytest_cov.plugin" else arg for arg in reference]
    ):
        raise ValueError(f"{source}: coverage commands differ beyond plug-in swap")
    coverage_source = record["methodology"].get("source_override")
    if coverage_source is not None and (
        "--cov-reset" not in reference
        or [arg for arg in reference if arg.startswith("--cov=")][-1:]
        != [f"--cov={coverage_source}"]
    ):
        raise ValueError(f"{source}: coverage source override differs from command")
    if "--cov-context=test" in argvs["plain pytest"] or any(
        arg.startswith("--cov") for arg in argvs["plain pytest"]
    ):
        raise ValueError(f"{source}: plain pytest control must disable coverage")


def build_summary(paths: list[Path], *, root: Path) -> dict[str, Any]:
    if len(paths) < 2:
        raise ValueError("cross-project chart needs at least two results")
    rows = []
    common_environment: tuple[Any, ...] | None = None
    seen: set[str] = set()
    for source in paths:
        record = _read(source)
        parity = _read(source.with_name(f"{source.stem}-parity.json"))
        project = record.get("project")
        methodology = record.get("methodology")
        if not isinstance(project, dict) or not isinstance(methodology, dict):
            raise ValueError(f"{source}: missing project or methodology")
        key = project.get("key")
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError(f"{source}: missing or duplicate project key")
        seen.add(key)
        mode = record.get("mode")
        minimum = 15 if mode == "publication" else 2 if mode == "diagnostic" else None
        if minimum is None:
            raise ValueError(f"{source}: unsupported coverage evidence mode")
        minimum_warmups = 3 if mode == "publication" else 0
        if (
            record.get("schema") != 3
            or record.get("benchmark") != "projects"
            or methodology.get("mode") != mode
            or methodology.get("warmups") != record.get("warmups")
            or methodology.get("measured_runs") != record.get("runs")
            or methodology.get("sampling")
            != {"warmups": record.get("warmups"), "measured_runs": record.get("runs")}
            or record.get("warmups", -1) < minimum_warmups
            or record.get("runs", 0) < minimum
            or methodology.get("phases") != ["serial"]
            or methodology.get("coverage_context") != "test"
            or methodology.get("reports") not in (["term", "json:coverage.json"], ["term", "json"])
            or methodology.get("source_override")
            not in ((None, "fastapi") if key == "fastapi" else (None,))
        ):
            raise ValueError(f"{source}: incompatible or unpublishable coverage protocol")
        if (
            parity.get("parity") != "exact"
            or parity.get("mode") != mode
            or parity.get("project") != project.get("name")
            or parity.get("commit") != project.get("commit")
            or parity.get("vfy_commit") != project.get("vfy_commit")
            or parity.get("methodology") != methodology
        ):
            raise ValueError(f"{source}: exact matching parity record required")
        env = record.get("environment")
        if not isinstance(env, dict):
            raise ValueError(f"{source}: missing environment")
        current_environment = tuple(
            env.get(field)
            for field in ("python", "implementation", "platform", "machine", "vfy", "pytest_cov")
        )
        if common_environment is not None and current_environment != common_environment:
            raise ValueError(f"{source}: environment differs between project rows")
        common_environment = current_environment
        _check_commands(record, source)
        reference, reference_count = _median(record, "pytest-cov", source, minimum=minimum)
        candidate, candidate_count = _median(record, "vfy", source, minimum=minimum)
        baseline, baseline_count = _median(record, "plain pytest", source, minimum=minimum)
        if {reference_count, candidate_count, baseline_count} != {record["runs"]}:
            raise ValueError(f"{source}: measured sample counts differ from declared runs")
        rows.append(
            {
                "project": project["name"],
                "scope": project["scope"],
                "display_scope": "Configured suite"
                if key in {"packaging", "black", "click"}
                else "Full suite",
                "route": ROUTE,
                "reference_median_ms": reference,
                "candidate_median_ms": candidate,
                "baseline_median_ms": baseline,
                "reference_samples": reference_count,
                "candidate_samples": candidate_count,
                "baseline_samples": baseline_count,
                "warmups": record["warmups"],
                "status": mode,
                "parity": "passed",
                "source": source.resolve().relative_to(root.resolve()).as_posix(),
                "upstream_revision": project["commit"],
                "source_revision": project["vfy_commit"],
                "wheel_sha256": record.get("provenance", {})
                .get("vfy_build", {})
                .get("wheel_sha256"),
            }
        )
    assert common_environment is not None
    return {
        "schema": 1,
        "charts": [
            {
                "kind": "coverage",
                "title": "Coverage across real project suites",
                "route": ROUTE,
                "context": (
                    f"{len(rows)} configured suites · {common_environment[1]} "
                    f"{'.'.join(str(common_environment[0]).split('.')[:2])} · "
                    f"{str(common_environment[2]).split('-')[0]}"
                ),
                "reference": "pytest-cov",
                "candidate": "vfy",
                "rows": rows,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    summary = build_summary(arguments.results, root=root)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
