"""Compare complete common mutation catalogs on unchanged mutmut adopters.

Run with the prepared environment's interpreter. Its resolved hash lock,
original project configuration, and source-bound release wheel are required.
Incomplete or differing outcomes stop the comparison before sampling.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Literal

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from benchmarks.benchmark_harness import summarize
from benchmarks.benchmark_mutation import (
    REPO_ROOT,
    MutmutSample,
    MutmutSignature,
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
from benchmarks.benchmark_provenance import artifact_provenance, build_provenance
from benchmarks.benchmark_real_mutation import _check_vfy_round, _retain_json
from benchmarks.benchmark_resources import tree_resource_methodology
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins
from vfy import native

type VfyEngine = Literal["pytest-native", "native"]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--source", action="append", help="verify the original mutation source scope"
    )
    parser.add_argument(
        "--pytest-arg", action="append", help="verify the original pytest arguments"
    )
    parser.add_argument("--expected-tests", type=int, required=True)
    parser.add_argument("--mutmut", required=True)
    parser.add_argument("--mutmut-repository", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--vfy-engine",
        dest="vfy_route",
        choices=("pytest-native", "native"),
        default="pytest-native",
    )
    parser.add_argument("--workers", type=int, help="concurrent mutations: native 4, independent 1")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--timeout-policy", choices=("adaptive", "fixed"), default="adaptive")
    parser.add_argument("--test-isolation", choices=("serial", "file"), default="serial")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--random-order-seed", type=int, help="match pytest-random-order input in both tools"
    )
    args = parser.parse_args()
    if args.workers is None:
        args.workers = 1 if args.vfy_route == "native" else 4
    if args.workers < 1 or args.expected_tests < 1 or args.timeout <= 0:
        parser.error("workers, expected-tests, and timeout must be positive")
    if args.warmups < 3 or args.runs < 30:
        parser.error("mutation comparisons require three warmups and thirty measured pairs")
    if args.random_order_seed is not None and args.random_order_seed < 0:
        parser.error("random-order-seed must be nonnegative")
    if args.vfy_route == "native" and (args.workers != 1 or args.test_isolation != "serial"):
        parser.error("independent mutation comparisons require one worker and serial isolation")
    return args


def environment(root: Path, *, random_order_seed: int | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith(("COVERAGE_", "VFY_")) or key in {
            "PYTHONHOME",
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "PYTEST_NATIVE_RESULT",
        }:
            env.pop(key)
    env["PYTHONPATH"] = os.pathsep.join((str(root / "src"), str(root)))
    env["PYTHONHASHSEED"] = "0"
    if random_order_seed is not None:
        env["PYTEST_ADDOPTS"] = f"--random-order-seed={random_order_seed}"
    return env


def source_files(root: Path, sources: list[str]) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for value in sources:
        relative = Path(value)
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or not path.exists():
            raise ValueError(f"source must exist inside the project: {value}")
        paths.update(path.rglob("*.py") if path.is_dir() else (path,))
    if not paths or any(path.suffix != ".py" for path in paths):
        raise ValueError("mutation sources must contain Python files")
    return tuple(sorted(paths))


def locked_versions(path: Path) -> dict[str, str]:
    """Verify the installed pins in a resolved pip/uv requirements hash lock."""
    contents = path.read_text(encoding="utf-8")
    result: dict[str, str] = {}
    for logical in contents.replace("\\\n", " ").splitlines():
        line = logical.partition(" #")[0].strip()
        if not line or line.startswith(("#", "--", "-e ")):
            continue
        requirement = Requirement(line.split(" --hash=", 1)[0].strip())
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
            raise ValueError(f"dependency is not exactly pinned: {requirement.name}")
        if not re.search(r"--hash=sha256:[a-f0-9]{64}(?:\s|$)", line):
            raise ValueError(f"dependency has no SHA-256 hash: {requirement.name}")
        version = specifiers[0].version
        name = canonicalize_name(requirement.name)
        if name in result and result[name] != version:
            raise ValueError(f"dependency has conflicting pins: {requirement.name}")
        if importlib.metadata.version(requirement.name) != version:
            raise ValueError(f"installed dependency differs from lock: {requirement.name}")
        result[name] = version
    if not result:
        raise ValueError("dependency lock contains no applicable hashed pins")
    return result


def dependency_closure(versions: dict[str, str]) -> None:
    """Reject an installed transitive requirement omitted from the supplied lock."""
    for name in (*versions, "vfy", "mutmut"):
        for value in importlib.metadata.requires(name) or ():
            requirement = Requirement(value)
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            dependency = canonicalize_name(requirement.name)
            if dependency not in versions and dependency not in {"vfy", "mutmut"}:
                raise ValueError(f"dependency lock omits {name}'s requirement {requirement.name}")
            observed = importlib.metadata.version(requirement.name)
            if observed not in requirement.specifier:
                raise ValueError(
                    f"installed {requirement.name} does not satisfy {name}'s requirement"
                )


def declared_scope(root: Path, args: argparse.Namespace, out: Path) -> dict[str, object]:
    """Bind explicit scope arguments to mutmut's original configuration reader."""
    script = """
import configparser,json
from pathlib import Path
import tomllib
import mutmut.configuration as configuration
pyproject=Path('pyproject.toml')
data=tomllib.loads(pyproject.read_text()) if pyproject.exists() else {}
setup=configparser.ConfigParser(); setup.read('setup.cfg')
if 'mutmut' not in data.get('tool',{}) and not setup.has_section('mutmut'):
    raise ValueError('project has no original mutmut configuration')
cfg=configuration.config() if hasattr(configuration,'config') else configuration.Config.get()
files=[]
for path in cfg.source_paths:
    files.extend(p for p in (path.rglob('*.py') if path.is_dir() else [path])
                 if cfg.should_mutate(p))
print(json.dumps({'files':sorted(str(p) for p in set(files)),
    'pytest_args':cfg.pytest_add_cli_args_test_selection+cfg.pytest_add_cli_args,
    'execution_args':cfg.pytest_add_cli_args,
    'selection_args':cfg.pytest_add_cli_args_test_selection,
    'mutate_only_covered_lines':cfg.mutate_only_covered_lines}))
"""
    command = [sys.executable, "-c", script]
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment(root, random_order_seed=getattr(args, "random_order_seed", None)),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _retain_json(
        out,
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode:
        raise RuntimeError("could not read original mutmut source and test scope")
    declaration = json.loads(completed.stdout)
    if not isinstance(declaration, dict):
        raise RuntimeError("invalid mutmut configuration response")
    # Coverage filtering changes per-function mutant numbering in the reference.
    # The unfiltered CST catalog cannot bind those names to exact replacements.
    if declaration.get("mutate_only_covered_lines"):
        raise RuntimeError("covered-line mutation catalogs require reference coverage mapping")
    if args.source is None:
        args.source = declaration["files"]
    elif source_files(root, declaration["files"]) != source_files(root, args.source):
        raise RuntimeError("requested source scope differs from original mutmut configuration")
    if args.pytest_arg is None:
        args.pytest_arg = declaration["pytest_args"]
    elif declaration["pytest_args"] != args.pytest_arg:
        raise RuntimeError("pytest arguments differ from original mutmut configuration")
    return {key: value for key, value in declaration.items() if isinstance(key, str)}


def retain_baseline_data(root: Path, out: Path) -> None:
    """Retain consistent copies of the completed mutation baseline databases."""
    out.mkdir()
    for relative in (".coverage-vfy", ".vfy/test-history.sqlite3"):
        source = root / relative
        if source.is_file():
            with (
                closing(sqlite3.connect(source)) as current,
                closing(sqlite3.connect(out / source.name)) as saved,
            ):
                current.backup(saved)


def wheel_payload() -> dict[str, str | int | None]:
    artifact = artifact_provenance(strict=True)
    manifest = json.loads(Path(os.environ["VFY_BENCHMARK_BUILD_MANIFEST"]).read_text())
    extension = Path(str(artifact["extension_path"]))
    count = 0
    with zipfile.ZipFile(manifest["wheel_path"]) as wheel:
        for member in wheel.namelist():
            if member.startswith("vfy/") and not member.endswith("/"):
                installed = extension.parent / member.removeprefix("vfy/")
                if not installed.is_file() or installed.read_bytes() != wheel.read(member):
                    raise RuntimeError(f"installed wheel payload mismatch: {member}")
                count += 1
    return {**artifact, "wheel_payload_files_verified": count}


def junit_outcomes(path: Path, *, root: Path | None = None) -> collections.Counter[tuple[str, ...]]:
    rows: collections.Counter[tuple[str, ...]] = collections.Counter()
    for case in ET.parse(path).getroot().iter("testcase"):
        outcomes = [child for child in case if child.tag in {"failure", "error", "skipped"}]
        outcome = outcomes[0] if outcomes else None
        values = (
            case.get("classname", ""),
            case.get("name", ""),
            outcome.tag if outcome is not None else "passed",
            outcome.get("type", "") if outcome is not None else "",
            outcome.get("message", "") if outcome is not None else "",
            outcome.text or "" if outcome is not None else "",
        )
        if root is not None:
            values = tuple(value.replace(str(root), "<project>") for value in values)
        rows[values] += 1
    return rows


def phase_row(
    nodeid: object, phase: object, outcome: object, wasxfail: object
) -> tuple[str, str, str, str | None]:
    if (
        not isinstance(nodeid, str)
        or not isinstance(phase, str)
        or not isinstance(outcome, str)
        or not isinstance(wasxfail, str | None)
    ):
        raise RuntimeError("baseline contains an invalid phase record")
    return nodeid, phase, outcome, wasxfail


def baseline_coverage_policy(
    root: Path, args: argparse.Namespace, out: Path
) -> Literal["implicit", "requested", "disabled"]:
    script = """
import json,sys
from vfy.native import NativeTestInvocation
from vfy.run.engine import (
    _resolve_pytest_configuration,
)
configuration=_resolve_pytest_configuration(json.loads(sys.argv[1])).projection
arguments=configuration['effective_args']
print(json.dumps({'effective_args':arguments,
    'policy':NativeTestInvocation.mutation_baseline_coverage_policy(arguments)}))
"""
    command = [sys.executable, "-c", script, json.dumps(args.pytest_arg)]
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment(root, random_order_seed=getattr(args, "random_order_seed", None)),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _retain_json(
        out,
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode:
        raise RuntimeError("could not determine original coverage request policy")
    policy = json.loads(completed.stdout).get("policy")
    if policy == "implicit":
        return "implicit"
    if policy == "requested":
        return "requested"
    if policy == "disabled":
        return "disabled"
    raise RuntimeError("invalid mutation baseline coverage policy")


def runner_outcomes(
    path: Path, expected_tests: int, *, engine: VfyEngine = "pytest-native"
) -> list[tuple[str, str, str, str | None]]:
    snapshot = path.read_text(encoding="utf-8")
    result = json.loads(snapshot)
    expected_engine = {"pytest-native": "pytest-runner-native-v1", "native": "vfy-independent-v1"}[
        engine
    ]
    if result.get("engine") != expected_engine or result.get("collected") != expected_tests:
        raise RuntimeError(f"baseline did not activate complete {engine} execution: {path}")
    projected = native._pytest.NativeRunResult.merge_json_results([snapshot])
    return [phase_row(row[0], row[1], row[2], row[5]) for row in projected.pytest_reports()]


def validate_mutation_baseline(
    path: Path,
    expected_tests: int,
    stock: list[tuple[str, str, str, str | None]],
    *,
    engine: VfyEngine = "pytest-native",
) -> None:
    if runner_outcomes(path, expected_tests, engine=engine) != stock:
        raise RuntimeError("instrumented mutation baseline outcomes differ from stock pytest")


def validated_reference_signature(
    sample: MutmutSample, *, expected_names: Sequence[str], label: str
) -> MutmutSignature:
    """Reject reference infrastructure exits before normalizing mutation outcomes."""
    process = sample.get("process")
    raw = process.get("raw_exit_codes") if isinstance(process, dict) else None
    if not isinstance(raw, dict) or len(set(expected_names)) != len(expected_names):
        raise RuntimeError(f"{label} has no exact raw mutmut exit-code evidence")
    for name in expected_names:
        code = raw.get(name)
        # mutmut labels pytest's internal-error exit 3 as killed. Publication
        # requires completed test outcomes, while diagnostics retain that mapping.
        if type(code) is not int or code not in {0, 1}:
            raise RuntimeError(f"{label} has a non-test mutmut exit status for {name}: {code!r}")
    return _validated_mutmut_signature(sample, expected_names=expected_names, label=label)


def baseline(
    root: Path,
    args: argparse.Namespace,
    out: Path,
    *,
    engine: VfyEngine | None,
    coverage_policy: Literal["implicit", "requested", "disabled"] = "requested",
) -> list[tuple[str, str, str, str | None]]:
    out.mkdir(parents=True)
    env = environment(root, random_order_seed=getattr(args, "random_order_seed", None))
    result_path = out / f"{engine}.json"
    if engine == "pytest-native":
        env["PYTEST_NATIVE_RESULT"] = str(result_path)
    if engine != "pytest-native":
        env["PYTHONPATH"] = os.pathsep.join(
            (str(stage_plugins(out / "plugins")), env["PYTHONPATH"])
        )
        env["VFY_BENCHMARK_OUTCOMES"] = str(out / "phases.jsonl")
    prefix = ["vfy", "test", f"--engine={engine}", "-j", "1"] if engine else ["pytest"]
    command = [
        sys.executable,
        "-m",
        *prefix,
        *(["--no-cov"] if engine and coverage_policy == "implicit" else []),
        *(["-p", OUTCOME_PLUGIN] if engine != "pytest-native" else []),
        *([f"--native-result-json={result_path}"] if engine == "native" else []),
        f"--junitxml={out / 'junit.xml'}",
        *args.pytest_arg,
    ]
    completed = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, timeout=600, check=False
    )
    _retain_json(
        out / "command.json",
        {
            "command": command,
            "cwd": str(root),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode:
        raise RuntimeError(f"baseline failed: {out}")
    rows = runner_outcomes(result_path, args.expected_tests, engine=engine) if engine else []
    if engine != "pytest-native":
        recorded = [json.loads(line) for line in (out / "phases.jsonl").read_text().splitlines()]
        observed = [
            phase_row(row["nodeid"], row["when"], row["outcome"], row.get("wasxfail"))
            for row in recorded
        ]
        if engine == "native" and rows != observed:
            raise RuntimeError("independent result phases differ from observed reports")
        rows = observed
    if len({row[0] for row in rows}) != args.expected_tests or not any(
        row[1] == "call" for row in rows
    ):
        raise RuntimeError(f"baseline did not run the complete expected inventory: {out}")
    if any(row[2] not in {"passed", "skipped"} for row in rows):
        raise RuntimeError(f"baseline has a failing phase: {out}")
    return rows


def full_suite_keys(
    root: Path,
    report: object,
    env: dict[str, str],
    out: Path,
    *,
    engine: VfyEngine = "pytest-native",
) -> frozenset[tuple[str, int, int, str, str]]:
    """Verify full-suite execution through the selected engine's Rust planners."""
    if not isinstance(report, dict) or not isinstance(report.get("mutations"), list):
        raise RuntimeError("missing vfy mutation rows")
    candidates = [
        row
        for row in report["mutations"]
        if row.get("status") == "protected"
        and (not row.get("tests") or row.get("owner", {}).get("phase") == "definition")
    ]
    if not candidates:
        return frozenset[tuple[str, int, int, str, str]]()
    script = """
import json,sys
from coverage import Coverage
from vfy.data import CoverageStore
from vfy.native import _pytest,mutation_contexts,mutation_executed_paths,plan_mutation_tests
coverage=Coverage(config_file=True)
data=CoverageStore.read('.coverage-vfy').to_rust()
measured=set(data.measured_files())
locations,_=mutation_contexts(data)
executed=mutation_executed_paths(data)
omit=coverage.get_option('run:omit') or []
inputs=json.loads(sys.argv[1])
initial=plan_mutation_tests(
    [(row['id'],row['path'],row['line'],row['owner']['phase']) for row in inputs],
    locations,executed,'.vfy/test-history.sqlite3',omit,
    collection_data=data,mutation_inputs=json.dumps(inputs))
plans=dict(initial)
if sys.argv[2]=='native':
    pending=[row for row in inputs if initial[row['id']][0]!='unreached']
    requests=[{'mutation':row,'scope':initial[row['id']][0],
               'tests':initial[row['id']][1]} for row in pending]
    fresh=_pytest.fresh_mutation_test_plans(json.dumps(requests),'.coverage-vfy')
    for row,tests in zip(pending,fresh,strict=True):
        plans[row['id']]=('selected' if tests else 'full_suite',tests)
print(json.dumps({'initial_plans':initial,'plans':plans,
                  'measured_files':sorted(measured),'executed_paths':sorted(executed),
                  'omit':omit,'engine':sys.argv[2]}))
"""
    command = [sys.executable, "-c", script, json.dumps(candidates), engine]
    completed = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, timeout=30, check=False
    )
    _retain_json(
        out,
        {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )
    if completed.returncode:
        raise RuntimeError("could not verify full-suite mutation plans")
    plans = json.loads(completed.stdout)["plans"]
    if set(plans) != {row["id"] for row in candidates} or any(
        value != ["full_suite", []] for value in plans.values()
    ):
        raise RuntimeError("protected unselected mutation does not have a full-suite plan")
    return frozenset(_mutation_catalog_key(row, root) for row in candidates)


def verify_inputs(
    root: Path,
    args: argparse.Namespace,
    state: tuple[str, str, tuple[str, ...]],
    mutmut_state: tuple[str, str, tuple[str, ...]],
    controller_state: tuple[str, str, tuple[str, ...]],
    artifact: dict[str, str | int | None],
    locked: dict[str, str],
) -> None:
    final = wheel_payload()
    if (
        any(
            final[key] != artifact[key]
            for key in (
                "extension_sha256",
                "source_revision",
                "wheel_sha256",
                "wheel_payload_files_verified",
            )
        )
        or locked_versions(args.lock) != locked
    ):
        raise RuntimeError("installed artifact or dependencies changed during validation")
    if (
        repository_state(root) != state
        or repository_state(args.mutmut_repository.resolve()) != mutmut_state
        or repository_state(REPO_ROOT) != controller_state
    ):
        raise RuntimeError("source revision changed during validation")


def main() -> None:  # noqa: C901 - retain preflight and rotating measurements in execution order
    args = arguments()
    child_environment = partial(environment, random_order_seed=args.random_order_seed)
    root, out = args.root.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    state = repository_state(root)
    controller_state = repository_state(REPO_ROOT)
    if state[0] != args.revision or state[2]:
        raise RuntimeError("upstream revision or cleanliness mismatch")
    mutmut_state = repository_state(args.mutmut_repository.resolve())
    if mutmut_state[2]:
        raise RuntimeError("mutmut checkout is not clean")
    args.mutmut = str(Path(args.mutmut).absolute())
    if Path(args.mutmut).parent != Path(sys.executable).absolute().parent:
        raise RuntimeError("mutmut and vfy must share the prepared environment")
    mutmut = importlib.import_module("mutmut")

    if not Path(str(mutmut.__file__)).resolve().is_relative_to(args.mutmut_repository.resolve()):
        raise RuntimeError("installed mutmut does not come from the recorded checkout")
    locked = locked_versions(args.lock)
    dependency_closure(locked)
    shutil.copy2(args.lock, out / "requirements.lock")
    declaration = declared_scope(root, args, out / "declared-scope.json")
    inventory = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source_files(root, args.source)
    }
    _retain_json(out / "source-inventory.json", inventory)
    artifact = wheel_payload()
    provenance = build_provenance(
        REPO_ROOT,
        workload={
            "source": args.source,
            "pytest_args": args.pytest_arg,
            "vfy_engine": args.vfy_route,
        },
        workers=args.workers,
        packages=("pytest", "coverage", "mutmut"),
        publication=not args.validate_only,
    )
    result: dict[str, object] = {
        "schema": "vfy.adopter-mutation.v1",
        "status": "preflight",
        "project_revision": state[0],
        "project_tree": state[1],
        "mutmut_revision": mutmut_state[0],
        "mutmut_tree": mutmut_state[1],
        "provenance": provenance,
        "artifact": artifact,
        "dependency_lock_sha256": hashlib.sha256(args.lock.read_bytes()).hexdigest(),
        "locked_versions": locked,
        "configuration_adaptations": [],
        "declared_scope": declaration,
        "environment_overrides": {
            key: os.environ.get(key)
            for key in (
                "RES_OPTIONS",
                "PYTHONPYCACHEPREFIX",
                "PYTHONDONTWRITEBYTECODE",
                "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
            )
        },
        "matching_random_inputs": {
            "PYTHONHASHSEED": "0",
            "random_order_seed": args.random_order_seed,
            "PYTEST_ADDOPTS": child_environment(root).get("PYTEST_ADDOPTS"),
        },
        "baseline_contract": (
            "ordered node IDs, phase names, phase outcomes, xfail annotations and JUnit "
            "outcome multiplicities; warning and capture parity is not asserted"
        ),
        "workers": args.workers,
        "vfy_engine": args.vfy_route,
        "test_isolation": args.test_isolation,
        "timeout_policy": args.timeout_policy,
        "timeout_seconds": args.timeout,
    }
    _retain_json(out / "result.json", result)
    with tempfile.TemporaryDirectory(prefix="vfy-adopter-preflight-") as temporary:
        qroot, mroot = Path(temporary) / "vfy", Path(temporary) / "mutmut"
        copy_project(root, qroot)
        copy_project(root, mroot)
        policy = baseline_coverage_policy(qroot, args, out / "baseline-coverage-policy.json")
        result["baseline_coverage_policy"] = policy
        stock = baseline(mroot, args, out / "stock", engine=None)
        executed = baseline(
            qroot, args, out / args.vfy_route, engine=args.vfy_route, coverage_policy=policy
        )
        if stock != executed or junit_outcomes(
            out / "stock/junit.xml", root=mroot
        ) != junit_outcomes(out / args.vfy_route / "junit.xml", root=qroot):
            raise RuntimeError("stock/vfy complete baseline outcomes differ")
        baseline_ids = frozenset(row[0] for row in stock)
        qcatalog, _, qids = vfy_catalog(qroot, source_files(qroot, args.source))
        mcatalog, _, mnames = mutmut_catalog(
            mroot, sys.executable, source_files(mroot, args.source)
        )
        common = qcatalog & mcatalog
        if not common:
            raise RuntimeError("project has no exact common mutation catalog")
        names = [mnames[key] for key in sorted(common)]
        _retain_json(
            out / "catalog.json",
            {
                "vfy": len(qcatalog),
                "mutmut": len(mcatalog),
                "common": [
                    {"key": key, "vfy_id": qids[key], "mutmut_name": mnames[key]}
                    for key in sorted(common)
                ],
            },
        )
        mutation_environment = child_environment(qroot)
        mutation_environment["PYTEST_NATIVE_RESULT"] = str(out / "mutation-baseline.json")
        report = measured_vfy(
            qroot,
            args,
            common,
            source_paths=source_files(qroot, args.source),
            pytest_args=args.pytest_arg,
            child_environment=mutation_environment,
            retain_process=True,
        )
        _retain_json(out / "vfy-preflight.json", report)
        retain_baseline_data(qroot, out / "baseline-data")
        validate_mutation_baseline(
            out / "mutation-baseline.json", args.expected_tests, stock, engine=args.vfy_route
        )
        full = full_suite_keys(
            qroot,
            report,
            child_environment(qroot),
            out / "full-suite-plans.json",
            engine=args.vfy_route,
        )
        qsignature = _validated_vfy_signature(
            report,
            root=qroot,
            expected_catalog=common,
            label="adopter preflight",
            full_suite_keys=full,
            baseline_test_ids=baseline_ids,
        )
        reference = measured_mutmut(
            mroot, args, names, child_environment=child_environment(mroot), retain_process=True
        )
        _retain_json(out / "mutmut-preflight.json", reference)
        msignature = validated_reference_signature(
            reference, expected_names=names, label="adopter preflight"
        )
        parity = compare_fixture_outcomes(
            report, reference, common_catalog=common, vfy_ids=qids, mutmut_names=mnames
        )
        execution_args = declaration["execution_args"]
        if not isinstance(execution_args, list) or any(
            not isinstance(argument, str) for argument in execution_args
        ):
            raise RuntimeError("original mutmut execution arguments are not strings")
        proofs = _KillerProofs(
            out / "proofs",
            python=sys.executable,
            timeout=600,
            test_environment=child_environment,
            pytest_args=[argument for argument in execution_args if isinstance(argument, str)],
        )
        proofs.set_reference_report(report, qroot)
        for mutation in report["mutations"]:
            if mutation["status"] == "protected":
                killer = mutation["killer"]
                if not isinstance(killer, str):
                    raise RuntimeError("protected mutation has no detecting test")
                proofs.prove(
                    qroot,
                    mutation,
                    [killer],
                    label="adopter preflight",
                    full_suite_tests=baseline_ids
                    if _mutation_catalog_key(mutation, qroot) in full
                    else frozenset(),
                )
        verify_inputs(root, args, state, mutmut_state, controller_state, artifact, locked)
        result.update(
            {
                "status": "validated",
                "catalog": {"vfy": len(qcatalog), "mutmut": len(mcatalog), "common": len(common)},
                "baseline_cases": len(baseline_ids),
                "parity": parity,
                "proofs": proofs.records(),
            }
        )
        _retain_json(out / "result.json", result)
        if args.validate_only:
            return
        times: dict[str, list[float]] = {"vfy": [], "mutmut": []}
        memory: dict[str, list[float]] = {"vfy": [], "mutmut": []}
        samples: list[dict[str, object]] = []
        for index in range(args.warmups + args.runs):
            order = ("vfy", "mutmut") if index % 2 == 0 else ("mutmut", "vfy")
            row: dict[str, object] = {
                "round": index + 1,
                "phase": "warmup" if index < args.warmups else "measured",
                "order": order,
            }
            for role in order:
                if (
                    repository_state(root) != state
                    or repository_state(args.mutmut_repository.resolve()) != mutmut_state
                    or repository_state(REPO_ROOT) != controller_state
                ):
                    raise RuntimeError("source revision changed during sampling")
                print(f"round {index + 1}: {role}", flush=True)
                with tempfile.TemporaryDirectory(prefix=f"vfy-adopter-{role}-") as sample_temp:
                    sample_root = Path(sample_temp) / "project"
                    copy_project(root, sample_root)
                    resource = out / "resources" / f"{index + 1:03d}-{role}.json"
                    if role == "vfy":
                        sample = measured_vfy(
                            sample_root,
                            args,
                            common,
                            source_paths=source_files(sample_root, args.source),
                            pytest_args=args.pytest_arg,
                            child_environment=child_environment(sample_root),
                            retain_process=True,
                            resource_path=resource,
                        )
                        _retain_json(out / "samples" / f"{index + 1:03d}-{role}.json", sample)
                        signature = _validated_vfy_signature(
                            sample,
                            root=sample_root,
                            expected_catalog=common,
                            label=f"round {index + 1}",
                            full_suite_keys=full,
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
                            child_environment=child_environment(sample_root),
                            retain_process=True,
                            resource_path=resource,
                        )
                        _retain_json(out / "samples" / f"{index + 1:03d}-{role}.json", sample)
                        if (
                            validated_reference_signature(
                                sample, expected_names=names, label=f"round {index + 1}"
                            )
                            != msignature
                        ):
                            raise RuntimeError("mutmut outcomes changed during sampling")
                    resources = sample.get("resources")
                    peak = (
                        resources.get("peak_tree_pss_bytes")
                        if isinstance(resources, dict)
                        else None
                    )
                    if not isinstance(peak, int):
                        raise RuntimeError("sample has no process-tree memory measurement")
                    row[role] = {"wall_ms": sample["wall_ms"], "resources": resources}
                    if index >= args.warmups:
                        times[role].append(sample["wall_ms"])
                        memory[role].append(float(peak))
                    _retain_json(out / "partial.json", {"samples": [*samples, row]})
            samples.append(row)
        verify_inputs(root, args, state, mutmut_state, controller_state, artifact, locked)
        result.update(
            {
                "status": "complete",
                "warmups": args.warmups,
                "runs": args.runs,
                "samples": samples,
                "resources_methodology": tree_resource_methodology(),
                "timed_boundary": (
                    "public mutation commands including startup, baseline and mutation execution; "
                    "source copy, catalogs, parsing and validation excluded"
                ),
                "statistic": "median",
                "vfy_wall_ms": summarize(times["vfy"]),
                "mutmut_wall_ms": summarize(times["mutmut"]),
                "peak_tree_pss_bytes": {
                    role: {"median": statistics.median(values), "samples": values}
                    for role, values in memory.items()
                },
                "proofs": proofs.records(),
                "recorded_at_utc": dt.datetime.now(dt.UTC).isoformat(),
            }
        )
        _retain_json(out / "result.json", result)


if __name__ == "__main__":
    main()
