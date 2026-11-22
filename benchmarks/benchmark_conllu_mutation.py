"""Parity-gated full-package conllu mutation comparison with pinned mutmut."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from pathlib import Path

from coverage import Coverage, CoverageData

from benchmarks.benchmark_harness import summarize
from benchmarks.benchmark_mutation import (
    _KillerProofs,
    _mutation_catalog_key,
    _validated_mutmut_signature,
    _validated_vfy_signature,
    compare_fixture_outcomes,
    copy_project,
    measured_mutmut,
    measured_vfy,
    mutmut_catalog,
    repository_state,
    vfy_catalog,
)
from benchmarks.benchmark_projects import outcome_records
from benchmarks.benchmark_provenance import artifact_provenance, build_provenance
from benchmarks.benchmark_real_mutation import _check_vfy_round, _retain_json
from benchmarks.benchmark_resources import tree_resource_methodology
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins

PROJECT_REVISION = "3c48b422421012223587806b8476ddb1be0acd78"
MUTMUT_REVISION = "cd2f73da310c3fc90cffcb3e6c768cdeac14e18c"
CONFIG = (
    "\n[tool.mutmut]\n"
    'source_paths = ["conllu/"]\n'
    'pytest_add_cli_args_test_selection = ["tests/"]\n'
    'also_copy = ["README.md"]\n'
)


def environment(root: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("COVERAGE_"):
            env.pop(key)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(root)
    env["PYTHONHASHSEED"] = "0"
    for key in (
        "PYTHONHOME",
        "PYTEST_PLUGINS",
        "PYTEST_ADDOPTS",
        "PYTEST_NATIVE_RESULT",
        "VFY_BENCHMARK_OUTCOMES",
        "VFY_BENCHMARK_OWNER_PID",
        "VFY_SELECTED_TESTS",
        "VFY_MUTATION_KEYS",
    ):
        env.pop(key, None)
    return env


def prepare(source: Path, destination: Path) -> None:
    copy_project(source, destination)
    config = destination / "pyproject.toml"
    original = config.read_text(encoding="utf-8")
    if "[tool.mutmut]" in original:
        raise RuntimeError("upstream unexpectedly contains a mutmut configuration")
    config.write_text(original + CONFIG, encoding="utf-8")


def stock_outcomes(
    root: Path, python: Path, evidence: Path
) -> list[tuple[str, str, str, str | None]]:
    evidence.mkdir(parents=True, exist_ok=True)
    env = environment(root)
    env["VFY_BENCHMARK_OUTCOMES"] = str(evidence / "phases.jsonl")
    env["PYTHONPATH"] = os.pathsep.join((str(stage_plugins(evidence / "plugins")), str(root)))
    command = [
        str(python),
        "-m",
        "pytest",
        "-p",
        OUTCOME_PLUGIN,
        "-q",
        f"--junitxml={evidence / 'stock.xml'}",
        "tests",
    ]
    result = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    (evidence / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (evidence / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    _retain_json(evidence / "command.json", {"command": command, "exit": result.returncode})
    if result.returncode:
        raise RuntimeError(f"pytest baseline failed: {evidence}")
    rows = outcome_records(evidence / "phases.jsonl")
    normalized = sorted(rows, key=lambda row: (row[0], row[1], row[2], row[3] or ""))
    calls = [row for row in normalized if row[1] == "call"]
    phases = {(node, phase): outcome for node, phase, outcome, _ in normalized}
    nodes = {node for node, _, _, _ in normalized}
    if (
        len(calls) != 139
        or len(nodes) != 139
        or len(normalized) != 417
        or any(
            phases.get((node, phase)) != "passed"
            for node in nodes
            for phase in ("setup", "call", "teardown")
        )
    ):
        raise RuntimeError(f"expected all 139 conllu test calls: {evidence}")
    return normalized


def parse_junit(xml: Path) -> set[tuple[str, str, str]]:
    rows = {
        (
            c.get("classname", ""),
            c.get("name", ""),
            "failed"
            if c.find("failure") is not None or c.find("error") is not None
            else "skipped"
            if c.find("skipped") is not None
            else "passed",
        )
        for c in ET.parse(xml).getroot().iter("testcase")
    }
    if len(rows) != 139 or any(row[2] != "passed" for row in rows):
        raise RuntimeError("vfy native baseline lost or failed test cases")
    return rows


def native_junit(
    root: Path, python: Path, evidence: Path, workers: int
) -> set[tuple[str, str, str]]:
    evidence.mkdir(parents=True, exist_ok=True)
    xml = evidence / "vfy.xml"
    command = [
        str(python),
        "-m",
        "vfy",
        "test",
        "--engine=pytest-native",
        "-j",
        str(workers),
        "--no-cov",
        "-q",
        f"--junitxml={xml}",
        "tests",
    ]
    result = subprocess.run(
        command,
        cwd=root,
        env=environment(root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    (evidence / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (evidence / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    _retain_json(evidence / "command.json", {"command": command, "exit": result.returncode})
    if result.returncode:
        raise RuntimeError("vfy native baseline failed")
    rows = parse_junit(xml)
    if len(rows) != 139 or any(row[2] != "passed" for row in rows):
        raise RuntimeError("vfy native baseline lost or failed test cases")
    return rows


def verified_full_suite_keys(
    root: Path, python: Path, report: Mapping[str, object], evidence: Path
) -> frozenset[tuple[str, int, int, str, str]]:
    """Verify omitted and definition-time mutations use the Rust full-suite plan."""
    coverage = Coverage(config_file=str(root / "pyproject.toml"))
    omitted = coverage.get_option("run:omit")
    if omitted != ["conllu/__init__.py"]:
        raise RuntimeError(f"unexpected conllu coverage omit: {omitted!r}")
    data = CoverageData(basename=str(root / ".coverage-vfy"))
    data.read()
    measured = set(data.measured_files())
    if str((root / "conllu/__init__.py").resolve()) in measured:
        raise RuntimeError("coverage-omitted source unexpectedly has baseline data")
    expected_files = {
        str(path.resolve()) for path in (root / "conllu").glob("*.py") if path.name != "__init__.py"
    }
    if not expected_files or not expected_files.issubset(measured):
        raise RuntimeError("mutation baseline is missing other conllu sources")
    values = report.get("mutations")
    if not isinstance(values, list):
        raise RuntimeError("missing mutation records")
    omitted_records = [
        item
        for item in values
        if isinstance(item, dict) and _mutation_catalog_key(item, root)[0] == "conllu/__init__.py"
    ]
    if not omitted_records:
        raise RuntimeError("common catalog has no coverage-omitted mutations")
    definition_records = [
        item
        for item in values
        if isinstance(item, dict)
        and isinstance(item.get("owner"), dict)
        and item["owner"].get("phase") == "definition"
    ]
    selected = list({item["id"]: item for item in [*omitted_records, *definition_records]}.values())
    inputs = [(item["id"], item["path"], item["line"], item["owner"]["phase"]) for item in selected]
    script = (
        "import json,sys; from vfy.native import plan_mutation_tests; "
        "print(json.dumps(plan_mutation_tests("
        "[tuple(row) for row in json.loads(sys.argv[1])], {}, set(json.loads(sys.argv[4])), "
        "sys.argv[2], json.loads(sys.argv[3]))))"
    )
    command = [
        str(python),
        "-c",
        script,
        json.dumps(inputs),
        str(root / ".vfy/test-history.sqlite3"),
        json.dumps(omitted),
        json.dumps(sorted(measured)),
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment(root),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _retain_json(
        evidence,
        {
            "command": command,
            "exit": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "omit": omitted,
            "omitted_keys": [_mutation_catalog_key(item, root) for item in omitted_records],
            "definition_keys": [_mutation_catalog_key(item, root) for item in definition_records],
            "measured_files": sorted(data.measured_files()),
        },
    )
    if completed.returncode:
        raise RuntimeError("could not verify native mutation plan")
    plan = json.loads(completed.stdout)
    if set(plan) != {item["id"] for item in selected} or any(
        value != ["full_suite", []] for value in plan.values()
    ):
        raise RuntimeError("coverage-omitted mutations do not use the full suite")
    return frozenset(_mutation_catalog_key(item, root) for item in selected)


def args_parser() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", type=Path, required=True)
    p.add_argument("--python", type=Path, required=True)
    p.add_argument("--mutmut", required=True)
    p.add_argument("--mutmut-repository", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--warmups", type=int, default=3)
    p.add_argument("--runs", type=int, default=30)
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--test-isolation", choices=("default", "serial", "file"), default="default")
    return p.parse_args()


# Keep parity gates and the sampling sequence in their execution order.
def main() -> None:  # noqa: C901
    args = args_parser()
    root = args.project.resolve()
    python = args.python.absolute()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    args.vfy_route = "pytest-native"
    if (
        args.workers < 1
        or args.timeout <= 0
        or (not args.validate_only and (args.warmups < 3 or args.runs < 30))
    ):
        raise ValueError("publication requires positive workers/timeout, three warmups and 30 runs")
    revision, tree, dirty = repository_state(root)
    mutmut_revision, mutmut_tree, mutmut_dirty = repository_state(args.mutmut_repository.resolve())
    if revision != PROJECT_REVISION or dirty or mutmut_revision != MUTMUT_REVISION or mutmut_dirty:
        raise RuntimeError("project or mutmut revision/cleanliness mismatch")
    if python != Path(sys.executable).absolute():
        raise RuntimeError("controller and measured interpreter differ")
    mutmut_executable = Path(args.mutmut).absolute()
    if mutmut_executable.parent != python.parent:
        raise RuntimeError("mutmut executable belongs to a different environment")
    mutmut_module = subprocess.check_output(
        [str(python), "-c", "import mutmut; print(mutmut.__file__)"], text=True
    ).strip()
    if not Path(mutmut_module).resolve().is_relative_to(args.mutmut_repository.resolve()):
        raise RuntimeError("installed mutmut is not the pinned checkout")
    artifact = artifact_provenance(strict=True)
    manifest_path = Path(os.environ["VFY_BENCHMARK_BUILD_MANIFEST"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    wheel_path = Path(manifest["wheel_path"])
    extension = Path(str(artifact["extension_path"]))
    with zipfile.ZipFile(wheel_path) as wheel:
        members = [
            name for name in wheel.namelist() if name.startswith("vfy/") and not name.endswith("/")
        ]
        for name in members:
            installed = extension.parent / name.removeprefix("vfy/")
            if not installed.is_file() or installed.read_bytes() != wheel.read(name):
                raise RuntimeError(f"installed wheel payload mismatch: {name}")
    lock_files = ["packaging.txt", "real-mutation-mutmut.txt", "packaging-mutation-click.txt"]
    lock_root = Path(__file__).resolve().parent / "locks"
    locks = {
        name: hashlib.sha256((lock_root / name).read_bytes()).hexdigest() for name in lock_files
    }
    pinned: dict[str, str] = {}
    for name in lock_files:
        for line in (lock_root / name).read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", line)
            if not match or "sys_platform == 'win32'" in line:
                continue
            distribution, expected = match.groups()
            observed = importlib.metadata.version(distribution)
            if observed != expected:
                raise RuntimeError(f"dependency mismatch: {distribution} {observed} != {expected}")
            pinned[distribution] = observed
    provenance = build_provenance(
        Path(__file__).resolve().parents[1],
        workload={"project": "conllu", "source": "all Python package files", "tests": "tests/"},
        workers=args.workers,
        packages=("pytest", "coverage", "mutmut"),
        publication=not args.validate_only,
    )
    result: dict[str, object] = {
        "project_revision": revision,
        "project_tree": tree,
        "mutmut_revision": mutmut_revision,
        "mutmut_tree": mutmut_tree,
        "provenance": provenance,
        "strict_artifact": artifact,
        "wheel_payload_files_verified": len(members),
        "mutmut_import": mutmut_module,
        "dependency_lock_sha256": locks,
        "verified_dependency_versions": pinned,
        "appended_mutmut_configuration": CONFIG,
        "status": "preflight",
    }
    _retain_json(out / "result.json", result)
    original = stock_outcomes(root, python, out / "original-stock")
    with tempfile.TemporaryDirectory(prefix="vfy-conllu-preflight-") as temporary:
        base = Path(temporary)
        qroot, mroot = base / "vfy", base / "mutmut"
        prepare(root, qroot)
        prepare(root, mroot)
        if stock_outcomes(qroot, python, out / "prepared-stock") != original:
            raise RuntimeError("copied fixture changed phase outcomes")
        native_cases = native_junit(qroot, python, out / "native-baseline", args.workers)
        if native_cases != parse_junit(out / "original-stock" / "stock.xml"):
            raise RuntimeError("stock and native JUnit inventories/outcomes differ")
        paths = sorted((qroot / "conllu").glob("*.py"))
        qcatalog, _, qids = vfy_catalog(qroot, paths)
        mcatalog, _, mnames = mutmut_catalog(qroot, str(python), paths)
        common = qcatalog & mcatalog
        if not common:
            raise RuntimeError("empty exact common catalog")
        names = [mnames[key] for key in sorted(common)]
        result["catalog"] = {
            "vfy": len(qcatalog),
            "mutmut": len(mcatalog),
            "common": len(common),
            "vfy_keys": sorted(qcatalog),
            "mutmut_keys": sorted(mcatalog),
            "common_keys": sorted(common),
            "common_mapping": [
                {"identity": key, "vfy_id": qids[key], "mutmut_name": mnames[key]}
                for key in sorted(common)
            ],
        }
        _retain_json(out / "result.json", result)
        baseline = measured_vfy(
            qroot,
            args,
            common,
            source_paths=paths,
            pytest_args=("tests",),
            python=str(python),
            child_environment=environment(qroot),
            retain_process=True,
        )
        _retain_json(out / "vfy-parity.json", baseline)
        shutil.copy2(qroot / ".coverage-vfy", out / "vfy-baseline.coverage")
        full_suite_keys = verified_full_suite_keys(
            qroot, python, baseline, out / "omitted-full-suite-plan.json"
        )
        result["verified_full_suite_keys"] = sorted(full_suite_keys)
        _retain_json(out / "result.json", result)
        baseline_ids = frozenset(node for node, phase, _, _ in original if phase == "call")
        qsignature = _validated_vfy_signature(
            baseline,
            root=qroot,
            expected_catalog=common,
            label="conllu vfy parity",
            full_suite_keys=full_suite_keys,
            baseline_test_ids=baseline_ids,
        )
        reference = measured_mutmut(
            mroot, args, names, child_environment=environment(mroot), retain_process=True
        )
        _retain_json(out / "mutmut-parity.json", reference)
        msignature = _validated_mutmut_signature(
            reference, expected_names=names, label="conllu mutmut parity"
        )
        sandbox = mroot / "mutants"
        if stock_outcomes(sandbox, python, out / "mutmut-sandbox-stock") != original:
            raise RuntimeError("mutmut sandbox changed full pytest phase inventory/outcomes")
        parity = compare_fixture_outcomes(
            baseline, reference, common_catalog=common, vfy_ids=qids, mutmut_names=mnames
        )
        _retain_json(out / "parity.json", parity)
        proofs = _KillerProofs(
            out / "killer-proofs",
            python=str(python),
            timeout=args.timeout,
            test_environment=environment,
        )
        proofs.set_reference_report(baseline, qroot)
        for mutation in baseline["mutations"]:
            if mutation.get("status") != "protected":
                continue
            killer = mutation.get("killer")
            if not isinstance(killer, str) or killer not in baseline_ids:
                raise RuntimeError("protected mutation killer is not in the baseline inventory")
            key = _mutation_catalog_key(mutation, qroot)
            proofs.prove(
                qroot,
                mutation,
                [killer],
                label="conllu parity",
                full_suite_tests=baseline_ids if key in full_suite_keys else frozenset(),
            )
        result.update(
            {
                "status": "preflight-passed",
                "vfy_summary": baseline["summary"],
                "mutmut_summary": reference["summary"],
                "parity": parity,
                "proofs": proofs.records(),
            }
        )
        _retain_json(out / "result.json", result)
        if args.validate_only:
            return
        samples = []
        timings: dict[str, list[float]] = {"vfy": [], "mutmut": []}
        pss: dict[str, list[float]] = {"vfy": [], "mutmut": []}
        for index in range(args.warmups + args.runs):
            row: dict[str, object] = {
                "round": index + 1,
                "phase": "warmup" if index < args.warmups else "measured",
            }
            order = ("vfy", "mutmut") if index % 2 == 0 else ("mutmut", "vfy")
            row["order"] = order
            for role in order:
                if repository_state(root) != (revision, tree, ()):
                    raise RuntimeError("conllu checkout changed during sampling")
                if repository_state(args.mutmut_repository.resolve()) != (
                    mutmut_revision,
                    mutmut_tree,
                    (),
                ):
                    raise RuntimeError("mutmut checkout changed during sampling")
                with tempfile.TemporaryDirectory(prefix=f"vfy-conllu-{role}-") as sample_temp:
                    sample_root = Path(sample_temp) / "project"
                    prepare(root, sample_root)
                    resource = out / "resources" / f"{index + 1:03d}-{role}.json"
                    if role == "vfy":
                        sample = measured_vfy(
                            sample_root,
                            args,
                            common,
                            source_paths=sorted((sample_root / "conllu").glob("*.py")),
                            pytest_args=("tests",),
                            python=str(python),
                            child_environment=environment(sample_root),
                            retain_process=True,
                            resource_path=resource,
                        )
                        _retain_json(out / "samples" / f"{index + 1:03d}-{role}.json", sample)
                        signature = _validated_vfy_signature(
                            sample,
                            root=sample_root,
                            expected_catalog=common,
                            label=f"round {index + 1}",
                            full_suite_keys=full_suite_keys,
                            baseline_test_ids=baseline_ids,
                        )
                        _check_vfy_round(
                            qsignature,
                            signature,
                            sample,
                            root=sample_root,
                            label=f"round {index + 1}",
                            proofs=proofs,
                        )
                    else:
                        sample = measured_mutmut(
                            sample_root,
                            args,
                            names,
                            child_environment=environment(sample_root),
                            retain_process=True,
                            resource_path=resource,
                        )
                        _retain_json(out / "samples" / f"{index + 1:03d}-{role}.json", sample)
                        if (
                            _validated_mutmut_signature(
                                sample, expected_names=names, label=f"round {index + 1}"
                            )
                            != msignature
                        ):
                            raise RuntimeError("mutmut outcomes changed")
                    resources = sample.get("resources")
                    if not isinstance(resources, dict):
                        raise RuntimeError("sample has no process-tree resources")
                    peak = resources.get("peak_tree_pss_bytes")
                    if not isinstance(peak, int):
                        raise RuntimeError("sample has no peak process-tree PSS")
                    if index >= args.warmups:
                        timings[role].append(sample["wall_ms"])
                        pss[role].append(float(peak))
                    row[role] = {"wall_ms": sample["wall_ms"], "resources": sample.get("resources")}
                    _retain_json(out / "partial.json", {"samples": [*samples, row]})
            samples.append(row)
        if repository_state(root) != (revision, tree, ()) or repository_state(
            args.mutmut_repository.resolve()
        ) != (mutmut_revision, mutmut_tree, ()):
            raise RuntimeError("source changed after sampling")
        if artifact_provenance(strict=True)["extension_sha256"] != artifact["extension_sha256"]:
            raise RuntimeError("vfy extension changed during sampling")
        result.update(
            {
                "status": "complete",
                "samples": samples,
                "warmups": args.warmups,
                "runs": args.runs,
                "methodology": tree_resource_methodology(),
                "statistic": "median",
                "vfy_peak_tree_pss_bytes": {
                    "median": statistics.median(pss["vfy"]),
                    "samples": pss["vfy"],
                },
                "mutmut_peak_tree_pss_bytes": {
                    "median": statistics.median(pss["mutmut"]),
                    "samples": pss["mutmut"],
                },
                "vfy_wall_ms": summarize(timings["vfy"]),
                "mutmut_wall_ms": summarize(timings["mutmut"]),
                "proofs": proofs.records(),
                "recorded_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            }
        )
        _retain_json(out / "result.json", result)


if __name__ == "__main__":
    main()
