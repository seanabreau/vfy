"""Unified command line entry point for repository benchmark workloads."""

from __future__ import annotations

import argparse
import copy
import html
import importlib
import json
import math
import shutil
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from benchmarks.benchmark_harness import (
    BenchmarkMode,
    SamplingConfig,
    benchmark_environment,
    independent_uncertainty,
    paired_uncertainty,
    run_checked,
    summarize,
)
from benchmarks.benchmark_provenance import (
    BUILD_MANIFEST_ENV,
    git_value,
)


class BenchmarkModule(Protocol):
    def main(self) -> int | None: ...


class ModeForwarding(StrEnum):
    """How the controller passes diagnostic mode to a workload."""

    UNSUPPORTED = "unsupported"
    DIAGNOSTIC_FLAG = "diagnostic-flag"
    MODE_OPTION = "mode-option"


@dataclass(frozen=True)
class BenchmarkSpec:
    name: str
    module: str
    description: str
    mode_forwarding: ModeForwarding = ModeForwarding.UNSUPPORTED
    supports_validation: bool = False
    supports_preparation: bool = False


SPECS: tuple[BenchmarkSpec, ...] = (
    BenchmarkSpec(
        "ci-selection",
        "benchmarks.benchmark_ci_selection",
        "ancestor evidence and PR selection",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "report",
        "benchmarks.benchmark_report",
        "coverage collection and reports",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "asyncio",
        "benchmarks.benchmark_asyncio",
        "asyncio collection and workflow",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "pytest-html",
        "benchmarks.benchmark_pytest_html",
        "pytest HTML reports",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "rust-boundary",
        "benchmarks.benchmark_rust_boundary",
        "Rust report boundary diagnostics",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "selection-persistence",
        "benchmarks.benchmark_selection_persistence",
        "selection and schema persistence",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "sysmon-events",
        "benchmarks.benchmark_sysmon_events",
        "CPython monitoring event diagnostics",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "evidence",
        "benchmarks.benchmark_evidence",
        "Criterion evidence benchmark",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=False,
    ),
    BenchmarkSpec(
        "mutation",
        "benchmarks.benchmark_mutation",
        "mutation workflow",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
        supports_preparation=True,
    ),
    BenchmarkSpec(
        "real-mutation",
        "benchmarks.benchmark_real_mutation",
        "scoped mutation comparison on pinned real projects versus mutmut",
        mode_forwarding=ModeForwarding.MODE_OPTION,
        supports_validation=True,
        supports_preparation=True,
    ),
    BenchmarkSpec(
        "projects",
        "benchmarks.benchmark_projects",
        "real-project workflows",
        mode_forwarding=ModeForwarding.MODE_OPTION,
        supports_validation=True,
        supports_preparation=True,
    ),
    BenchmarkSpec(
        "native-coverage",
        "benchmarks.benchmark_native_coverage",
        "same independent frontend coverage workflow",
        mode_forwarding=ModeForwarding.MODE_OPTION,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "static-report",
        "benchmarks.benchmark_static_report",
        "prepared source analysis and reports",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
    ),
    BenchmarkSpec(
        "workflows",
        "benchmarks.benchmark_workflows",
        "stateful real-project edit workflows",
        mode_forwarding=ModeForwarding.MODE_OPTION,
        supports_validation=True,
        supports_preparation=True,
    ),
    BenchmarkSpec(
        "edit-loop",
        "benchmarks.benchmark_edit_loop",
        "selection edit loop",
        mode_forwarding=ModeForwarding.DIAGNOSTIC_FLAG,
        supports_validation=True,
        supports_preparation=True,
    ),
    BenchmarkSpec(
        "real-selection",
        "benchmarks.benchmark_real_selection",
        "affected tests on a pinned real project versus pytest-testmon",
        mode_forwarding=ModeForwarding.MODE_OPTION,
        supports_validation=True,
        supports_preparation=True,
    ),
)


def _spec(name: str) -> BenchmarkSpec:
    for spec in SPECS:
        if spec.name == name:
            return spec
    choices = ", ".join(item.name for item in SPECS)
    raise SystemExit(f"unknown benchmark {name!r}; choose one of: {choices}")


def _sampling_from_args(arguments: Sequence[str], *, mode: BenchmarkMode) -> SamplingConfig:
    warmups = 3
    runs = 15
    for index, argument in enumerate(arguments):
        if argument == "--warmups" and index + 1 < len(arguments):
            warmups = int(arguments[index + 1])
        elif argument == "--runs" and index + 1 < len(arguments):
            runs = int(arguments[index + 1])
    return SamplingConfig(warmups, runs, mode)


def _child_arguments(
    arguments: Sequence[str], *, mode: BenchmarkMode, forwarding: ModeForwarding
) -> list[str]:
    """Translate the common mode into the workload's declared interface."""

    result: list[str] = []
    mode_option_present = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--mode":
            mode_option_present = True
            if forwarding is ModeForwarding.MODE_OPTION:
                result.extend(arguments[index : index + 2])
            index += 2
            continue
        result.append(argument)
        index += 1
    if mode is BenchmarkMode.DIAGNOSTIC:
        if forwarding is ModeForwarding.DIAGNOSTIC_FLAG and "--diagnostic" not in result:
            result.append("--diagnostic")
        elif forwarding is ModeForwarding.MODE_OPTION and not mode_option_present:
            result.extend(("--mode", mode.value))
    return result


@contextmanager
def _temporary_argv(arguments: Sequence[str]) -> Iterator[None]:
    original = sys.argv
    sys.argv = [original[0], *arguments]
    try:
        yield
    finally:
        sys.argv = original


def _prepare_project(arguments: Sequence[str]) -> int:
    try:
        owner = importlib.import_module("benchmarks.prepare_projects")
    except ImportError as error:
        raise SystemExit("the workflow manifest is unavailable") from error
    prepare_project = getattr(owner, "prepare_project", None)
    if callable(prepare_project):
        if len(arguments) != 2:
            raise SystemExit("prepare requires PROJECT DESTINATION")
        prepare_project(Path(arguments[0]), Path(arguments[1]))
        return 0
    prepare = getattr(owner, "prepare", None)
    if not callable(prepare):
        raise SystemExit("the workflow manifest does not expose a project preparation entry point")
    project_name: str | None = None
    destination: Path | None = None
    environment: Path | None = None
    metadata: Path | None = None
    constraints: Path | None = None
    vfy_root: Path | None = None
    python = sys.executable
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--python" and index + 1 < len(arguments):
            python = arguments[index + 1]
            index += 2
            continue
        if argument == "--environment" and index + 1 < len(arguments):
            environment = Path(arguments[index + 1])
            index += 2
            continue
        if argument == "--metadata" and index + 1 < len(arguments):
            metadata = Path(arguments[index + 1])
            index += 2
            continue
        if argument == "--constraints" and index + 1 < len(arguments):
            constraints = Path(arguments[index + 1])
            index += 2
            continue
        if argument == "--vfy-root" and index + 1 < len(arguments):
            vfy_root = Path(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--"):
            raise SystemExit(f"unknown prepare option: {argument}")
        if project_name is None:
            project_name = argument
        elif destination is None:
            destination = Path(argument)
        else:
            raise SystemExit("prepare accepts PROJECT DESTINATION and optional preparation paths")
        index += 1
    if project_name is None or destination is None:
        raise SystemExit("prepare requires PROJECT DESTINATION")
    manifest = importlib.import_module("benchmarks.project_manifest").manifest
    prepare(
        manifest(project_name),
        destination,
        environment=environment,
        metadata=metadata,
        constraints=constraints,
        vfy_root=vfy_root,
        python=python,
    )
    return 0


def _invoke(spec: BenchmarkSpec, command: str, arguments: Sequence[str]) -> int:
    if command == "validate" and not spec.supports_validation:
        raise SystemExit(f"benchmark {spec.name!r} does not support validation-only runs")
    if command not in {"prepare", "run", "validate"}:
        raise SystemExit(f"unsupported benchmark command: {command}")
    module = cast(BenchmarkModule, importlib.import_module(spec.module))
    mode = BenchmarkMode.DIAGNOSTIC if command == "validate" else BenchmarkMode.PUBLICATION
    if command == "run":
        for index, argument in enumerate(arguments):
            if argument == "--mode" and index + 1 < len(arguments):
                mode = BenchmarkMode(arguments[index + 1])
                break
        if mode is BenchmarkMode.DIAGNOSTIC and spec.mode_forwarding is ModeForwarding.UNSUPPORTED:
            raise SystemExit(f"benchmark {spec.name!r} does not support diagnostic mode")
        _sampling_from_args(arguments, mode=mode)
        child_arguments = _child_arguments(
            arguments,
            mode=mode,
            forwarding=spec.mode_forwarding,
        )
    elif command == "validate":
        child_arguments = _child_arguments(
            arguments,
            mode=mode,
            forwarding=spec.mode_forwarding,
        )
        if "--validate-only" not in child_arguments and spec.supports_validation:
            child_arguments.append("--validate-only")
    else:
        child_arguments = _child_arguments(
            arguments,
            mode=mode,
            forwarding=spec.mode_forwarding,
        )

    if command == "prepare":
        return _prepare_project(child_arguments)
    if command in {"prepare", "validate"}:
        hook = getattr(module, command, None)
        if callable(hook):
            result = hook(child_arguments)
            return int(result) if result is not None else 0
    with _temporary_argv(child_arguments):
        result = module.main()
    return int(result) if result is not None else 0


def _revision(payload: dict[str, object]) -> str | None:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return None
    engine_source = provenance.get("engine_source")
    if isinstance(engine_source, dict) and isinstance(engine_source.get("revision"), str):
        value = engine_source.get("revision")
        if isinstance(value, str):
            return value
    source = provenance.get("source")
    if isinstance(source, dict) and isinstance(source.get("revision"), str):
        value = source.get("revision")
        if isinstance(value, str):
            return value
    for key in ("revision", "candidate_revision"):
        value = provenance.get(key)
        if isinstance(value, str):
            return value
    return None


def _require_full_revision(value: str | None, label: str) -> str:
    if value is None or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"incomparable benchmark results: {label} must be a full commit ID")
    return value


def _semantic_methodology(value: object) -> object:
    if isinstance(value, dict):
        ignored = {"command", "commands", "output", "path", "paths", "order"}
        return {
            str(key): _semantic_methodology(nested)
            for key, nested in value.items()
            if key not in ignored
        }
    if isinstance(value, list):
        return [_semantic_methodology(item) for item in value]
    return value


def _full_hex(value: object, length: int, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"incomparable benchmark results: invalid {label}")
    return value


def _require_publication_sampling(payload: dict[str, object], methodology: object) -> None:
    method = methodology if isinstance(methodology, dict) else {}
    payload_mode = payload.get("mode")
    method_mode = method.get("mode")
    if payload_mode is not None and method_mode is not None and payload_mode != method_mode:
        raise ValueError("incomparable publication results: sampling modes disagree")
    mode = payload_mode if payload_mode is not None else method_mode
    if mode != BenchmarkMode.PUBLICATION.value:
        raise ValueError("incomparable publication results: diagnostic sampling is not publishable")
    warmups = payload.get("warmups", method.get("warmups"))
    runs = payload.get("runs", payload.get("measured_runs", method.get("measured_runs")))
    if runs is None:
        runs = method.get("samples")
    if warmups is None:
        warmup_seconds = method.get("warm_up_seconds")
        warmups = (
            3
            if isinstance(warmup_seconds, (int, float))
            and not isinstance(warmup_seconds, bool)
            and math.isfinite(warmup_seconds)
            and warmup_seconds >= 3
            else None
        )
    if type(warmups) is not int or warmups < 3:
        raise ValueError("incomparable publication results: insufficient warmups")
    if type(runs) is not int or runs < 15:
        raise ValueError("incomparable publication results: insufficient measured samples")
    timing_records = _timing_records(payload)
    if not timing_records:
        raise ValueError("incomparable publication results: no raw timing samples")
    for path, record in timing_records.items():
        samples = record.get("samples_ms")
        if not isinstance(samples, list) or len(samples) < 15:
            raise ValueError(
                "incomparable publication results: fewer than 15 raw samples at " + path
            )


def _require_publication_hardware(identity: dict[str, object]) -> None:
    required_hardware = (
        "cpu",
        "python_implementation",
        "python_build",
        "python_abi",
        "python_gil",
    )
    if any(not isinstance(identity[key], str) or not identity[key] for key in required_hardware):
        raise ValueError("incomparable publication results: hardware identity is incomplete")
    for key in ("logical_cpus", "memory_bytes"):
        value = identity[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("incomparable publication results: hardware identity is incomplete")


def _require_publication_artifact(
    artifact: object, artifact_revision: object, engine_revision: object
) -> None:
    _full_hex(artifact_revision, 40, "artifact source revision")
    _full_hex(engine_revision, 40, "engine source revision")
    if not isinstance(artifact, dict):
        raise ValueError("incomparable publication results: missing verified build artifact")
    _full_hex(artifact.get("wheel_sha256"), 64, "wheel hash")
    _full_hex(artifact.get("extension_sha256"), 64, "extension hash")
    if (
        artifact.get("native_profile") != "release"
        or not isinstance(artifact.get("build_manifest"), str)
        or not isinstance(artifact.get("source_revision"), str)
        or len(str(artifact.get("source_revision"))) != 40
    ):
        raise ValueError("incomparable publication results: missing verified build artifact")


def _identity(payload: dict[str, object], *, require_artifact: bool) -> dict[str, object]:
    provenance = payload.get("provenance")
    provenance_map = provenance if isinstance(provenance, dict) else {}
    environment = payload.get("environment")
    environment_map = environment if isinstance(environment, dict) else {}
    provenance_environment = provenance_map.get("environment")
    if isinstance(provenance_environment, dict):
        environment_map = {**provenance_environment, **environment_map}
    tools = environment_map.get("tools", environment_map.get("versions"))
    python = environment_map.get("python")
    if python is None and isinstance(tools, dict):
        python = tools.get("python")
    hardware_fields = {
        key: environment_map.get(key)
        for key in (
            "cpu",
            "logical_cpus",
            "memory_bytes",
            "python_implementation",
            "python_build",
            "python_abi",
            "python_debug",
            "python_gil",
        )
    }
    methodology = payload.get("methodology")
    if methodology is None:
        raise ValueError("incomparable benchmark results: missing methodology")
    if isinstance(methodology, dict):
        parity_values = [
            value
            for key, value in methodology.items()
            if key in {"parity", "validation", "collection_validation", "merge_validation"}
        ]
    else:
        parity_values = [methodology] if isinstance(methodology, str) else []
    if not any(isinstance(value, str) and value.strip() for value in parity_values):
        raise ValueError("incomparable benchmark results: missing semantic parity evidence")
    validation = provenance_map.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "passed":
        raise ValueError("incomparable benchmark results: semantic validation did not pass")
    artifact = environment_map.get("artifact")
    engine_source = provenance_map.get("engine_source")
    artifact_revision = artifact.get("source_revision") if isinstance(artifact, dict) else None
    engine_revision = engine_source.get("revision") if isinstance(engine_source, dict) else None
    if artifact_revision is not None and (
        not isinstance(artifact_revision, str)
        or len(artifact_revision) != 40
        or any(char not in "0123456789abcdef" for char in artifact_revision)
    ):
        raise ValueError("incomparable benchmark results: invalid artifact revision")
    if (
        engine_revision is not None
        and artifact_revision is not None
        and engine_revision != artifact_revision
    ):
        raise ValueError("incomparable benchmark results: artifact and engine revisions differ")
    if require_artifact:
        _require_publication_artifact(artifact, artifact_revision, engine_revision)
        _require_publication_sampling(payload, methodology)
    identity = {
        "benchmark": payload.get("benchmark", payload.get("name")),
        "workload": payload.get("workload", provenance_map.get("workload")),
        "workers": provenance_map.get("workers", payload.get("workers")),
        "cache": provenance_map.get("cache", payload.get("cache")),
        "platform": environment_map.get("platform"),
        "machine": environment_map.get("machine"),
        **hardware_fields,
        "python": python,
        "tools": tools,
        "methodology": _semantic_methodology(methodology),
    }
    if any(
        identity[key] is None
        for key in ("benchmark", "workload", "platform", "machine", "python", "tools")
    ):
        raise ValueError("incomparable benchmark results: missing workload or environment identity")
    if not identity["workload"] or not isinstance(identity["tools"], dict) or not identity["tools"]:
        raise ValueError("incomparable benchmark results: workload and tools must be nonempty")
    if any(
        not isinstance(identity[key], str) or not identity[key]
        for key in ("platform", "machine", "python")
    ):
        raise ValueError("incomparable benchmark results: machine identity is incomplete")
    if require_artifact:
        _require_publication_hardware(identity)
    return identity


def _validated_samples(
    record: dict[str, object], path: str
) -> tuple[list[float], list[str] | None]:
    raw = record.get("samples_ms")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"missing raw timing samples at {path}")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw):
        raise ValueError(f"invalid raw timing samples at {path}")
    samples = [float(value) for value in raw]
    if any(not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError(f"invalid raw timing samples at {path}")
    for key in ("median_ms", "mean_ms", "stdev_ms", "min_ms", "max_ms"):
        value = record.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"missing timing summary {key} at {path}")
    sample_ids = record.get("sample_ids")
    if sample_ids is None:
        return samples, None
    if not isinstance(sample_ids, list) or len(sample_ids) != len(samples):
        raise ValueError(f"invalid sample IDs at {path}")
    ids: list[str] = [value for value in sample_ids if isinstance(value, str) and value]
    if len(ids) != len(sample_ids) or len(set(ids)) != len(ids):
        raise ValueError(f"invalid sample IDs at {path}")
    return samples, ids


def _record_median(records: dict[str, object], path: str, key: str) -> float:
    record = records.get(path)
    if not isinstance(record, dict):
        raise ValueError(f"invalid comparison record at {path}")
    summary = record.get(key)
    if not isinstance(summary, dict):
        raise ValueError(f"invalid comparison summary at {path}")
    median = summary.get("median_ms")
    if not isinstance(median, (int, float)):
        raise ValueError(f"invalid comparison median at {path}")
    return float(median)


def _timing_records(value: object, path: str = "") -> dict[str, dict[str, object]]:
    if isinstance(value, dict):
        result: dict[str, dict[str, object]] = {}
        if "samples_ms" in value and isinstance(value["samples_ms"], list):
            result[path] = cast(dict[str, object], value)
        for key, nested in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            result.update(_timing_records(nested, child_path))
        return result
    if isinstance(value, list):
        result = {}
        for index, nested in enumerate(value):
            result.update(_timing_records(nested, f"{path}[{index}]"))
        return result
    return {}


def compare_results(
    candidate_path: Path,
    baseline_path: Path,
    *,
    base_revision: str | None = None,
    candidate_revision: str | None = None,
    mode: BenchmarkMode = BenchmarkMode.PUBLICATION,
) -> dict[str, object]:
    """Compare compatible retained results with paired raw-sample uncertainty."""

    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(candidate, dict) or not isinstance(baseline, dict):
        raise ValueError("benchmark results must contain JSON objects")
    require_artifact = mode is BenchmarkMode.PUBLICATION
    candidate_identity = _identity(candidate, require_artifact=require_artifact)
    baseline_identity = _identity(baseline, require_artifact=require_artifact)
    if candidate_identity != baseline_identity:
        raise ValueError(
            f"incomparable benchmark identities: candidate={candidate_identity!r}, "
            f"baseline={baseline_identity!r}"
        )
    candidate_source = _require_full_revision(_revision(candidate), "candidate revision")
    baseline_source = _require_full_revision(_revision(baseline), "baseline revision")
    if base_revision is not None and baseline_source != base_revision:
        raise ValueError(
            f"baseline revision mismatch: expected {base_revision}, got {baseline_source}"
        )
    if candidate_revision is not None and candidate_source != candidate_revision:
        raise ValueError(
            f"candidate revision mismatch: expected {candidate_revision}, got {candidate_source}"
        )
    candidate_timings = _timing_records(candidate)
    baseline_timings = _timing_records(baseline)
    if candidate_timings.keys() != baseline_timings.keys():
        raise ValueError("incomparable benchmark results: timing records differ")
    if not candidate_timings:
        raise ValueError("benchmark results contain no raw timing samples")
    records: dict[str, object] = {}
    for path in sorted(candidate_timings):
        candidate_samples, candidate_ids = _validated_samples(candidate_timings[path], path)
        baseline_samples, baseline_ids = _validated_samples(baseline_timings[path], path)
        if (candidate_ids is None) != (baseline_ids is None):
            raise ValueError(f"sample pairing is present in only one result at {path}")
        if candidate_ids is not None and baseline_ids is not None:
            if len(candidate_samples) != len(baseline_samples):
                raise ValueError(f"paired sample counts differ at {path}")
            if candidate_ids != baseline_ids:
                raise ValueError(f"sample pairing differs at {path}")
            uncertainty = paired_uncertainty(candidate_samples, baseline_samples)
            uncertainty_kind = "paired"
        else:
            uncertainty = independent_uncertainty(candidate_samples, baseline_samples)
            uncertainty_kind = "independent"
        records[path] = {
            "candidate_summary": summarize(candidate_samples),
            "baseline_summary": summarize(baseline_samples),
            "uncertainty_kind": uncertainty_kind,
            "uncertainty": uncertainty,
        }
    return {
        "candidate_revision": candidate_source,
        "baseline_revision": baseline_source,
        "identity": candidate_identity,
        "records": records,
        "summary": {"chart_labels": sorted(records)},
    }


def write_comparison_artifacts(report: dict[str, object], destination: Path) -> dict[str, str]:
    """Write a concise review table and standalone SVG from validated records."""

    raw_records = report.get("records")
    if not isinstance(raw_records, dict):
        raise ValueError("comparison report has no records")
    rows: list[tuple[str, float, float, float, str, int]] = []
    for path in sorted(raw_records):
        record = raw_records[path]
        if not isinstance(record, dict):
            raise ValueError(f"invalid comparison record at {path}")
        candidate = _record_median({path: record}, path, "candidate_summary")
        baseline = _record_median({path: record}, path, "baseline_summary")
        uncertainty = record.get("uncertainty")
        if not isinstance(uncertainty, dict):
            raise ValueError(f"invalid comparison uncertainty at {path}")
        low = uncertainty.get("ci95_low_ms")
        high = uncertainty.get("ci95_high_ms")
        count_value = uncertainty.get("samples", uncertainty.get("candidate_samples"))
        count = int(count_value) if isinstance(count_value, (int, float)) else 0
        interval = (
            "unavailable"
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float))
            else f"[{float(low):.3f}, {float(high):.3f}]"
        )
        rows.append((path, candidate, baseline, candidate - baseline, interval, count))
    destination.mkdir(parents=True, exist_ok=True)
    markdown = [
        "| Workload | Candidate median (ms) | Baseline median (ms) | Delta (ms) | "
        "95% CI (ms) | n |",
        "| --- | ---: | ---: | ---: | --- | ---: |",
    ]
    for path, candidate, baseline, delta, interval, count in rows:
        escaped_path = path.replace("|", "\\|")
        markdown.append(
            f"| {escaped_path} | {candidate:.3f} | {baseline:.3f} | "
            f"{delta:.3f} | {interval} | {count} |"
        )
    markdown_path = destination / "summary.md"
    markdown_path.write_text("\n".join(markdown) + "\n", encoding="utf-8")

    width = 960
    row_height = 30
    height = max(60, 40 + row_height * len(rows))
    maximum = max((max(candidate, baseline) for _, candidate, baseline, *_ in rows), default=0.0)
    scale = 650 / maximum if maximum else 0.0
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<title>Benchmark comparison medians</title>",
    ]
    for index, (path, candidate, baseline, _delta, _interval, _count) in enumerate(rows):
        y = 30 + index * row_height
        label = html.escape(path)
        elements.append(f'<text x="8" y="{y + 12}" font-size="11">{label}</text>')
        elements.append(
            f'<rect x="250" y="{y}" width="{baseline * scale:.2f}" height="10" fill="#9ca3af"/>'
        )
        elements.append(
            f'<rect x="250" y="{y + 12}" width="{candidate * scale:.2f}" '
            'height="10" fill="#2563eb"/>'
        )
    elements.append('<text x="915" y="18" font-size="11">baseline / candidate</text>')
    elements.append("</svg>\n")
    svg_path = destination / "summary.svg"
    svg_path.write_text("\n".join(elements), encoding="utf-8")
    return {"markdown": str(markdown_path), "svg": str(svg_path)}


def _isolated_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    environment = benchmark_environment(overrides)
    environment.pop("CONDA_PREFIX", None)
    return environment


def _run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    log: Path,
    timeout: float,
) -> None:
    try:
        completed = run_checked(command, cwd=cwd, env=environment, timeout=timeout)
    except RuntimeError as error:
        log.write_text(str(error) + "\n", encoding="utf-8")
        raise
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")


def _merge_sampled_value(values: Sequence[object]) -> object:
    if not values:
        raise ValueError("cannot merge an empty value sequence")
    first = values[0]
    if all(isinstance(value, dict) for value in values):
        mappings = cast(list[dict[str, object]], list(values))
        keys = set(mappings[0])
        if any(set(mapping) != keys for mapping in mappings[1:]):
            raise ValueError("benchmark blocks have different record fields")
        if all(isinstance(mapping.get("samples_ms"), list) for mapping in mappings):
            samples: list[float] = []
            for mapping in mappings:
                raw_samples = mapping["samples_ms"]
                if not isinstance(raw_samples, list):
                    raise ValueError("benchmark block samples are not lists")
                for sample in raw_samples:
                    if not isinstance(sample, (int, float)):
                        raise ValueError("benchmark block sample is not numeric")
                    samples.append(float(sample))
            summary = dict(summarize(samples))
            if any("sample_ids" in mapping for mapping in mappings):
                summary.pop("sample_ids", None)
            return summary
        result = copy.deepcopy(mappings[0])
        for key in result:
            if all(key in mapping for mapping in mappings):
                result[key] = _merge_sampled_value([mapping[key] for mapping in mappings])
        return result
    if all(isinstance(value, list) for value in values):
        lists = cast(list[list[object]], list(values))
        lengths = {len(items) for items in lists}
        if len(lengths) != 1:
            raise ValueError("benchmark blocks have different list lengths")
        return [
            _merge_sampled_value([items[index] for items in lists])
            for index in range(len(lists[0]))
        ]
    return copy.deepcopy(first)


def _validate_sampled_blocks(
    payloads: Sequence[dict[str, object]],
    *,
    mode: BenchmarkMode | None,
    expected_revision: str | None,
) -> None:
    timing_maps = [_timing_records(payload) for payload in payloads]
    first_timing = timing_maps[0]
    if not first_timing:
        raise ValueError("benchmark blocks contain no raw timing records")
    if any(timings.keys() != first_timing.keys() for timings in timing_maps[1:]):
        raise ValueError("benchmark blocks have different timing records")
    for path in sorted(first_timing):
        records = [timings[path] for timings in timing_maps]
        fields = set(records[0])
        if any(set(record) != fields for record in records[1:]):
            raise ValueError(f"benchmark timing record fields differ at {path}")
        if any(not isinstance(record["samples_ms"], list) for record in records):
            raise ValueError(f"benchmark timing samples are not lists at {path}")
        counts = [len(cast(list[object], record["samples_ms"])) for record in records]
        if len(set(counts)) != 1:
            raise ValueError(f"benchmark block sample counts differ at {path}")
        for record in records:
            _validated_samples(record, path)
    if mode is None:
        return
    identities = [
        _identity(payload, require_artifact=mode is BenchmarkMode.PUBLICATION)
        for payload in payloads
    ]
    if any(identity != identities[0] for identity in identities[1:]):
        raise ValueError("benchmark blocks have different workload or environment identity")
    revisions = [
        _require_full_revision(_revision(payload), "engine revision") for payload in payloads
    ]
    if expected_revision is not None:
        expected = _require_full_revision(expected_revision, "expected engine revision")
        if any(revision != expected for revision in revisions):
            raise ValueError("benchmark block engine revisions differ from the built artifact")
    elif any(revision != revisions[0] for revision in revisions[1:]):
        raise ValueError("benchmark blocks have different engine revisions")


def _merge_sampled_payloads(
    payloads: Sequence[dict[str, object]],
    *,
    mode: BenchmarkMode | None = None,
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Merge independently measured blocks while retaining one raw sample stream."""

    if not payloads:
        raise ValueError("cannot merge empty benchmark blocks")
    _validate_sampled_blocks(payloads, mode=mode, expected_revision=expected_revision)
    merged = cast(dict[str, object], _merge_sampled_value(list(payloads)))
    for container in (merged, merged.get("methodology")):
        if not isinstance(container, dict):
            continue
        for key in ("warmups", "runs", "measured_runs", "samples"):
            value = container.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                container[key] = value * len(payloads)
    merged["sampling_blocks"] = len(payloads)
    return merged


def _measure_revision_blocks(
    records: dict[str, dict[str, object]],
    *,
    repository: Path,
    benchmark: str,
    child_arguments: Sequence[str],
    mode: BenchmarkMode,
    result_root: Path,
    log_root: Path,
) -> tuple[dict[str, Path], tuple[tuple[str, ...], ...]]:
    block_orders = (("base", "candidate"), ("candidate", "base"))
    result_paths: dict[str, list[Path]] = {label: [] for label in records}
    for block, order in enumerate(block_orders):
        for label in order:
            record = records[label]
            output = result_root / f"{label}-block-{block:02d}.json"
            environment = _isolated_environment({BUILD_MANIFEST_ENV: str(record["manifest"])})
            _run_logged(
                [
                    str(record["interpreter"]),
                    "-m",
                    "benchmarks",
                    "run",
                    benchmark,
                    "--mode",
                    mode.value,
                    *child_arguments,
                    "--output",
                    str(output),
                ],
                cwd=repository,
                environment=environment,
                log=log_root / f"{label}-benchmark-block-{block:02d}.log",
                timeout=3_600,
            )
            result_paths[label].append(output)

    merged_paths: dict[str, Path] = {}
    for label, paths in result_paths.items():
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        if not all(isinstance(payload, dict) for payload in payloads):
            raise RuntimeError(f"{label} benchmark blocks did not produce JSON objects")
        merged = _merge_sampled_payloads(
            cast(list[dict[str, object]], payloads),
            mode=mode,
            expected_revision=cast(str, records[label]["revision"]),
        )
        merged_path = result_root / f"{label}.json"
        merged_path.write_text(
            json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        merged_paths[label] = merged_path
    return merged_paths, block_orders


def compare_revisions(
    benchmark: str,
    *,
    base_revision: str,
    candidate_revision: str | None = None,
    arguments: Sequence[str] = (),
    root: Path | None = None,
    mode: BenchmarkMode = BenchmarkMode.PUBLICATION,
) -> dict[str, object]:
    """Build and measure two exact revisions in alternating retained blocks.

    Builds and dependency setup happen before any benchmark command. Each
    revision then receives one publication block in each order, and the raw
    samples are merged only after both blocks finish. The benchmark's own
    publication gates bind each installed release artifact to its recorded
    source revision.
    """

    repository = (root or Path.cwd()).resolve()
    base = git_value(repository, "rev-parse", "--verify", f"{base_revision}^{{commit}}")
    if base is None:
        raise ValueError(f"base revision is not a commit: {base_revision}")
    candidate_name = candidate_revision or "HEAD"
    candidate = git_value(repository, "rev-parse", "--verify", f"{candidate_name}^{{commit}}")
    if candidate is None:
        raise ValueError(f"candidate revision is not a commit: {candidate_name}")
    if len(base) != 40 or len(candidate) != 40:
        raise ValueError("exact revision comparison requires full commit IDs")
    child_arguments: list[str] = []
    index = 0
    while index < len(arguments):
        argument = str(arguments[index])
        if argument in {"--", "--mode", "--output"}:
            index += 2 if argument in {"--mode", "--output"} else 1
            continue
        child_arguments.append(argument)
        index += 1
    evidence_root = (
        repository
        / "target"
        / "benchmarks"
        / f"exact-compare-{base[:12]}-{candidate[:12]}-{time.time_ns()}"
    )
    wheel_root = evidence_root / "wheels"
    manifest_root = evidence_root / "manifests"
    environment_root = evidence_root / "environments"
    result_root = evidence_root / "results"
    log_root = evidence_root / "logs"
    for directory in (wheel_root, manifest_root, environment_root, result_root, log_root):
        directory.mkdir(parents=True, exist_ok=True)
    dependency_export = run_checked(
        [
            "uv",
            "export",
            "--project",
            str(repository),
            "--locked",
            "--group",
            "dev",
            "--group",
            "benchmark",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ],
        cwd=repository,
        env=_isolated_environment(),
        timeout=300,
    )
    dependency_file = evidence_root / "requirements.txt"
    dependency_file.write_text(dependency_export.stdout, encoding="utf-8")
    maturin = shutil.which("maturin")
    if maturin is None:
        candidate_maturin = Path(sys.executable).with_name("maturin")
        if not candidate_maturin.is_file():
            raise RuntimeError("maturin is required for exact revision comparisons")
        maturin = str(candidate_maturin)

    records: dict[str, dict[str, object]] = {}
    for label, revision in (("base", base), ("candidate", candidate)):
        worktree = evidence_root / "worktrees" / label
        wheel_dir = wheel_root / label
        manifest_path = manifest_root / f"{label}.json"
        build_environment = _isolated_environment()
        _run_logged(
            ["git", "worktree", "add", "--detach", str(worktree), revision],
            cwd=repository,
            environment=build_environment,
            log=log_root / f"{label}-worktree.log",
            timeout=120,
        )
        build_command = [
            maturin,
            "build",
            "--release",
            "--locked",
            "--interpreter",
            sys.executable,
            "--target-dir",
            str(evidence_root / "cargo-targets" / label),
            "--out",
            str(wheel_dir),
            "--manifest-path",
            str(worktree / "crates/vfy/Cargo.toml"),
        ]
        try:
            _run_logged(
                build_command,
                cwd=worktree,
                environment=build_environment,
                log=log_root / f"{label}-build.log",
                timeout=1_800,
            )
            wheels = sorted(wheel_dir.glob("vfy-*.whl"))
            if not wheels:
                raise RuntimeError(f"release build produced no wheel for {revision}")
            records[label] = {
                "revision": revision,
                "source_root": worktree,
                "wheel": wheels[-1],
                "manifest": manifest_path,
                "build_command": tuple(build_command),
            }
        finally:
            _run_logged(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=repository,
                environment=build_environment,
                log=log_root / f"{label}-worktree-remove.log",
                timeout=120,
            )

    manifest_script = (
        "import sys; "
        "from pathlib import Path; "
        "from benchmarks.benchmark_provenance import write_build_manifest; "
        "write_build_manifest(Path(sys.argv[1]), source_root=Path(sys.argv[2]), "
        "source_revision=sys.argv[3], wheel=Path(sys.argv[4]), "
        "build_command=tuple(sys.argv[5:]))"
    )
    for label, record in records.items():
        environment = _isolated_environment()
        wheel = cast(Path, record["wheel"])
        manifest_path = cast(Path, record["manifest"])
        venv = environment_root / label
        _run_logged(
            ["uv", "venv", "--python", sys.executable, str(venv)],
            cwd=repository,
            environment=environment,
            log=log_root / f"{label}-venv.log",
            timeout=300,
        )
        interpreter = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        _run_logged(
            [
                "uv",
                "pip",
                "sync",
                "--python",
                str(interpreter),
                "--require-hashes",
                str(dependency_file),
            ],
            cwd=repository,
            environment=environment,
            log=log_root / f"{label}-dependencies.log",
            timeout=1_800,
        )
        _run_logged(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(interpreter),
                "--force-reinstall",
                "--no-deps",
                str(wheel),
            ],
            cwd=repository,
            environment=environment,
            log=log_root / f"{label}-install.log",
            timeout=600,
        )
        _run_logged(
            [
                str(interpreter),
                "-c",
                manifest_script,
                str(manifest_path),
                str(record["source_root"]),
                cast(str, record["revision"]),
                str(wheel),
                *cast(tuple[str, ...], record["build_command"]),
            ],
            cwd=repository,
            environment=environment,
            log=log_root / f"{label}-manifest.log",
            timeout=120,
        )
        record["interpreter"] = interpreter

    merged_paths, block_orders = _measure_revision_blocks(
        records,
        repository=repository,
        benchmark=benchmark,
        child_arguments=child_arguments,
        mode=mode,
        result_root=result_root,
        log_root=log_root,
    )
    report = compare_results(
        merged_paths["candidate"],
        merged_paths["base"],
        base_revision=base,
        candidate_revision=candidate,
        mode=mode,
    )
    report["evidence_root"] = str(evidence_root)
    report["summary_artifacts"] = write_comparison_artifacts(report, evidence_root)
    report["harness_revision"] = git_value(repository, "rev-parse", "HEAD")
    report["measurement_order"] = [list(order) for order in block_orders]
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m benchmarks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list registered workloads")
    for command in ("prepare", "validate", "run"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("benchmark")
        command_parser.add_argument("args", nargs=argparse.REMAINDER)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--benchmark", choices=tuple(item.name for item in SPECS))
    compare.add_argument("--base-revision")
    compare.add_argument("--candidate-revision")
    compare.add_argument(
        "--mode",
        choices=tuple(item.value for item in BenchmarkMode),
        default=BenchmarkMode.PUBLICATION.value,
    )
    compare.add_argument("paths", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "list":
        for spec in SPECS:
            print(f"{spec.name}\t{spec.description}")
        return 0
    if args.command == "compare":
        paths = list(args.paths)
        if paths and paths[0] == "--":
            paths.pop(0)
        if args.benchmark:
            if args.base_revision is None:
                raise SystemExit("exact revision comparisons require --base-revision REV")
            report = compare_revisions(
                args.benchmark,
                base_revision=args.base_revision,
                candidate_revision=args.candidate_revision,
                arguments=paths,
                mode=BenchmarkMode(args.mode),
            )
            print(json.dumps(report, indent=2))
            return 0
        if len(paths) != 2:
            raise SystemExit(
                "compare requires CANDIDATE BASELINE or --benchmark with --base-revision"
            )
        print(
            json.dumps(
                compare_results(
                    Path(paths[0]),
                    Path(paths[1]),
                    base_revision=args.base_revision,
                    candidate_revision=args.candidate_revision,
                    mode=BenchmarkMode(args.mode),
                ),
                indent=2,
            )
        )
        return 0
    return _invoke(_spec(args.benchmark), args.command, args.args)


if __name__ == "__main__":
    raise SystemExit(main())
