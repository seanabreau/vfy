"""Parity-gated public-command timings for plain pytest and vfy without coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email import message_from_bytes
from pathlib import Path
from typing import Literal, TypedDict
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    benchmark_environment,
    paired_uncertainty,
    run_checked,
    sampling_config,
    summarize,
    timed_process,
)
from benchmarks.benchmark_provenance import (
    hardware_details,
    require_clean,
    require_revision,
)
from scripts.process_tree import run_captured
from scripts.pytest_execution_record import (
    file_sha256,
    resolve_executable,
    resolved_environment,
    verify_installed_artifacts,
)

ROOT = Path(__file__).resolve().parents[2]
PYTEST_VERSION = "9.1.1"
ROUTES = ("pytest", "vfy")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stratum:
    """One generated family, optional explicit selection, and bytecode-cache state."""

    name: str
    cases: int
    bytecode: Literal["cold-bytecode", "warm-bytecode"]
    selected_cases: tuple[int, ...] | None = None


class PreflightProof(TypedDict):
    route: Literal["pytest", "vfy"]
    exit_status: int
    outcomes: list[dict[str, str]]
    outcomes_sha256: str
    command: list[str]
    stdout: str
    stderr: str
    junit: str


class StratumGate(TypedDict):
    project: str
    fixture_sha256: str
    preflight: dict[str, PreflightProof]


class CandidateIdentity(TypedDict):
    python: str
    environment: dict[str, object]
    imported_vfy: dict[str, str]
    wheel: dict[str, str]
    build_manifest: dict[str, str]


STRATA = (
    Stratum("one-test-cold-bytecode", 1, "cold-bytecode"),
    Stratum("one-test-warm-bytecode", 1, "warm-bytecode"),
    Stratum("one-file-1000-warm-bytecode", 1_000, "warm-bytecode"),
    Stratum("one-selected-from-10000-warm-bytecode", 10_000, "warm-bytecode", (9_999,)),
    Stratum(
        "ten-selected-from-10000-warm-bytecode",
        10_000,
        "warm-bytecode",
        (0, 1_111, 2_222, 3_333, 4_444, 5_555, 6_666, 7_777, 8_888, 9_999),
    ),
)


def create_project(root: Path, cases: int) -> str:
    """Write the same single-module parametrized suite for both public routes."""

    if cases < 1:
        raise ValueError("cases must be positive")
    if root.exists():
        raise FileExistsError(f"benchmark project already exists: {root}")
    root.mkdir(parents=True)
    files = {
        "pytest.ini": "[pytest]\n",
        "test_workload.py": (
            "from __future__ import annotations\n\n"
            "import pytest\n\n"
            "@pytest.mark.parametrize(\n"
            "    'value', range(" + str(cases) + "),\n"
            "    ids=[f'case-{value:04d}' for value in range(" + str(cases) + ")],\n"
            ")\n"
            "def test_case(value: int) -> None:\n"
            "    assert value >= 0\n"
        ),
    }
    digest = hashlib.sha256()
    for relative, contents in files.items():
        payload = contents.encode("utf-8")
        (root / relative).write_bytes(payload)
        digest.update(relative.encode("utf-8") + b"\0" + payload + b"\0")
    return digest.hexdigest()


def expected_case_names(cases: int, selected_cases: tuple[int, ...] | None = None) -> list[str]:
    indices = range(cases) if selected_cases is None else selected_cases
    return [f"test_case[case-{value:04d}]" for value in indices]


def command(
    route: Literal["pytest", "vfy"],
    python: Path,
    root: Path,
    *,
    junit: Path | None = None,
    selected_cases: tuple[int, ...] | None = None,
) -> list[str]:
    """Build symmetric, public commands; only vfy-specific flags differ."""

    prefix = [str(python), "-P", "-m"]
    if route == "pytest":
        result = [*prefix, "pytest"]
    else:
        result = [
            *prefix,
            "vfy",
            "test",
            "--workers=1",
            "--no-cov",
        ]
    result.extend(("-p", "no:cacheprovider", "-q"))
    if junit is not None:
        result.extend(("--junitxml", str(junit)))
    if selected_cases is None:
        result.append("test_workload.py")
    else:
        result.extend(f"test_workload.py::test_case[case-{value:04d}]" for value in selected_cases)
    return result


def junit_outcomes(
    path: Path,
    cases: int,
    selected_cases: tuple[int, ...] | None = None,
    *,
    expected_names: list[str] | None = None,
) -> list[dict[str, str]]:
    """Return ordered test names and outcomes, excluding volatile durations."""

    if expected_names is not None and (
        selected_cases is not None
        or len(expected_names) != cases
        or len(set(expected_names)) != cases
    ):
        raise ValueError(
            "explicit full-inventory names must be unique and match the complete case count"
        )
    if not path.is_file():
        raise RuntimeError(f"JUnit report is missing: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise RuntimeError(f"JUnit report is malformed: {path}") from error
    records: list[dict[str, str]] = []
    for testcase in root.iter("testcase"):
        statuses = [
            status
            for status in ("failure", "error", "skipped")
            if testcase.find(status) is not None
        ]
        if len(statuses) > 1:
            raise RuntimeError(f"JUnit case has conflicting statuses: {testcase.attrib}")
        name = testcase.get("name")
        classname = testcase.get("classname")
        if not name or not classname:
            raise RuntimeError(f"JUnit case is missing its name or class: {testcase.attrib}")
        records.append(
            {"classname": classname, "name": name, "outcome": statuses[0] if statuses else "passed"}
        )
    expected = (
        expected_case_names(cases, selected_cases) if expected_names is None else expected_names
    )
    if [record["name"] for record in records] != expected:
        raise RuntimeError(
            f"JUnit node IDs differ: found {len(records)} cases, "
            f"expected {len(expected)} ordered cases"
        )
    return records


def run_preflight(
    route: Literal["pytest", "vfy"],
    python: Path,
    root: Path,
    evidence: Path,
    cases: int,
    env: dict[str, str],
    timeout: float,
    *,
    selected_cases: tuple[int, ...] | None = None,
    expected_names: list[str] | None = None,
) -> PreflightProof:
    """Run the observer-enabled gate and retain its command and streams."""

    evidence.mkdir(parents=True, exist_ok=False)
    junit = evidence / "junit.xml"
    argv = command(route, python, root, junit=junit, selected_cases=selected_cases)
    result = run_captured(argv, cwd=root, env=env, timeout=timeout)
    (evidence / "stdout.txt").write_text(result.stdout, encoding="utf-8")
    (evidence / "stderr.txt").write_text(result.stderr, encoding="utf-8")
    (evidence / "command.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "cwd": str(root),
                "exit_status": result.returncode,
                "observer": "pytest built-in JUnit XML",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{route} parity preflight exited {result.returncode}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    outcomes = junit_outcomes(junit, cases, selected_cases, expected_names=expected_names)
    if any(outcome["outcome"] != "passed" for outcome in outcomes):
        raise RuntimeError(f"{route} parity preflight did not pass every requested case")
    return {
        "route": route,
        "exit_status": result.returncode,
        "outcomes": outcomes,
        "outcomes_sha256": hashlib.sha256(
            json.dumps(outcomes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "command": argv,
        "stdout": str(evidence / "stdout.txt"),
        "stderr": str(evidence / "stderr.txt"),
        "junit": str(junit),
    }


def clear_fixture_bytecode(root: Path) -> None:
    """Clear only generated project bytecode, outside the timed process."""

    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache)


def validate_stratum(
    stratum: Stratum,
    python: Path,
    output: Path,
    env: dict[str, str],
    timeout: float,
) -> StratumGate:
    logger.info("[%s] parity preflight", stratum.name)
    stratum_root = output / "strata" / stratum.name
    project = stratum_root / "project"
    fixture_sha256 = create_project(project, stratum.cases)
    (stratum_root / "runs").mkdir(parents=True)
    proofs = {
        route: run_preflight(
            route,
            python,
            project,
            stratum_root / "preflight" / route,
            stratum.cases,
            env,
            timeout,
            selected_cases=stratum.selected_cases,
        )
        for route in ROUTES
    }
    if proofs["pytest"]["outcomes"] != proofs["vfy"]["outcomes"]:
        raise RuntimeError(f"{stratum.name}: stock and vfy outcomes differ")
    return {
        "project": str(project),
        "fixture_sha256": fixture_sha256,
        "preflight": proofs,
    }


def run_stratum(
    stratum: Stratum,
    python: Path,
    output: Path,
    env: dict[str, str],
    gate: StratumGate,
    *,
    warmups: int,
    runs: int,
    timeout: float,
) -> dict[str, object]:
    stratum_root = output / "strata" / stratum.name
    project = Path(gate["project"])
    fixture_sha256 = gate["fixture_sha256"]
    proofs = gate["preflight"]
    selected_count = (
        stratum.cases if stratum.selected_cases is None else len(stratum.selected_cases)
    )
    selected_nodeids = (
        None
        if stratum.selected_cases is None
        else [
            f"test_workload.py::{name}"
            for name in expected_case_names(stratum.cases, stratum.selected_cases)
        ]
    )
    samples: dict[str, list[float]] = {route: [] for route in ROUTES}
    records: list[dict[str, object]] = []
    for phase, count in (("warmup", warmups), ("measured", runs)):
        for pair in range(count):
            order = ROUTES if pair % 2 == 0 else tuple(reversed(ROUTES))
            for route in order:
                if stratum.bytecode == "cold-bytecode":
                    clear_fixture_bytecode(project)
                command_path = command(
                    route, python, project, selected_cases=stratum.selected_cases
                )
                elapsed_ms, result = timed_process(
                    command_path,
                    cwd=project,
                    env=env,
                    timeout=timeout,
                )
                label = f"{phase}-{pair:02d}-{route}"
                evidence = stratum_root / "runs" / label
                evidence.mkdir()
                (evidence / "stdout.txt").write_text(result.stdout, encoding="utf-8")
                (evidence / "stderr.txt").write_text(result.stderr, encoding="utf-8")
                sample: dict[str, object] = {
                    "stratum": stratum.name,
                    "phase": phase,
                    "pair": pair,
                    "route": route,
                    "order": list(order),
                    "argv": command_path,
                    "cwd": str(project),
                    "bytecode_state": stratum.bytecode,
                    "generated_cases": stratum.cases,
                    "selected_cases": selected_count,
                    "elapsed_ms": elapsed_ms,
                    "exit_status": result.returncode,
                    "stdout_path": str(evidence / "stdout.txt"),
                    "stderr_path": str(evidence / "stderr.txt"),
                }
                (evidence / "sample.json").write_text(
                    json.dumps(sample, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                records.append(sample)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"{stratum.name} {phase} {route} run exited {result.returncode}:\n"
                        f"{result.stdout}\n{result.stderr}"
                    )
                if phase == "measured":
                    samples[route].append(elapsed_ms)

    (stratum_root / "fixture.json").write_text(
        json.dumps(
            {
                "cases": stratum.cases,
                "generated_cases": stratum.cases,
                "selected_cases": selected_count,
                "selected_nodeids": selected_nodeids,
                "test_file": "test_workload.py",
                "fixture_sha256": fixture_sha256,
                "bytecode_state": stratum.bytecode,
                "cold_policy": (
                    "remove every project __pycache__ directory before each process; "
                    "OS and installed-environment caches remain warm"
                    if stratum.bytecode == "cold-bytecode"
                    else "preserve project __pycache__ created by preflight and warmups"
                ),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "name": stratum.name,
        "cases": stratum.cases,
        "generated_cases": stratum.cases,
        "selected_cases": selected_count,
        "selected_nodeids": selected_nodeids,
        "bytecode_state": stratum.bytecode,
        "fixture_sha256": fixture_sha256,
        "preflight": proofs,
        "methodology": {
            "timed_processes_per_route": runs,
            "warmup_processes_per_route": warmups,
            "order": "alternating paired route order, starting with pytest",
            "timer": (
                "child process wall time, including startup, collection, test execution "
                "and output capture"
            ),
            "coverage": "disabled for both routes",
            "cacheprovider": "disabled for both routes",
            "plugin_autoload": "disabled through PYTEST_DISABLE_PLUGIN_AUTOLOAD for both routes",
            "timed_samples": {route: summarize(samples[route]) for route in ROUTES},
            "vfy_minus_pytest": paired_uncertainty(samples["vfy"], samples["pytest"]),
            "vfy_over_pytest_median": (
                summarize(samples["vfy"])["median_ms"] / summarize(samples["pytest"])["median_ms"]
            ),
        },
        "samples": records,
    }


def verify_candidate(
    python: Path, wheel: Path, manifest_path: Path, revision: str
) -> CandidateIdentity:
    """Bind the installed payload and imported extension to the release wheel."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    wheel_hash = file_sha256(wheel)
    if manifest.get("source_revision") != revision:
        raise RuntimeError("build manifest source revision differs from the clean checkout")
    if manifest.get("source_root") != str(ROOT.resolve()):
        raise RuntimeError("build manifest source root differs from the benchmark checkout")
    if manifest.get("native_profile") != "release":
        raise RuntimeError("benchmark requires a release wheel build manifest")
    if manifest.get("wheel_sha256") != wheel_hash:
        raise RuntimeError("candidate wheel hash differs from its build manifest")
    with ZipFile(wheel) as archive:
        metadata_paths = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise RuntimeError("candidate wheel must contain exactly one METADATA file")
        metadata = message_from_bytes(archive.read(metadata_paths[0]))
        if (metadata.get("Name") or "").lower().replace("_", "-") != "vfy":
            raise RuntimeError("candidate wheel is not the vfy distribution")
        wheel_extensions = {
            hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.endswith((".so", ".pyd", ".dylib"))
        }
    if manifest.get("extension_sha256") not in wheel_extensions:
        raise RuntimeError("candidate wheel does not contain its manifested native extension")
    verify_installed_artifacts(python, [wheel], "vfy")
    installed = resolved_environment(python)
    pytest = next(
        (
            item
            for item in installed["packages"]
            if item["name"].lower().replace("_", "-") == "pytest"
        ),
        None,
    )
    if pytest is None or pytest["version"] != PYTEST_VERSION:
        raise RuntimeError(f"benchmark requires pytest {PYTEST_VERSION}; found {pytest}")
    program = """\
import hashlib
import importlib.metadata
import json
import vfy
import vfy._native
from pathlib import Path

extension = Path(vfy._native.__file__).resolve()
print(json.dumps({
    "vfy_version": importlib.metadata.version("vfy"),
    "vfy_path": str(Path(vfy.__file__).resolve()),
    "extension_path": str(extension),
    "extension_sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
}))
"""
    completed = run_checked(
        [str(python), "-P", "-c", program], cwd=ROOT, env=benchmark_environment()
    )
    imported = json.loads(completed.stdout)
    if imported.get("extension_sha256") != manifest.get("extension_sha256"):
        raise RuntimeError("installed native extension differs from the build manifest")
    if not Path(imported["extension_path"]).is_relative_to(python.parent.parent.resolve()):
        raise RuntimeError("benchmark interpreter imported vfy outside its environment")
    return {
        "python": str(python),
        "environment": installed,
        "imported_vfy": imported,
        "wheel": {"path": str(wheel), "sha256": wheel_hash},
        "build_manifest": {
            "path": str(manifest_path),
            "sha256": file_sha256(manifest_path),
            "source_revision": revision,
            "native_profile": str(manifest["native_profile"]),
            "extension_sha256": str(manifest["extension_sha256"]),
        },
    }


def write_checksums(root: Path) -> None:
    paths = sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (root / "SHA256SUMS").write_text(
        "".join(f"{file_sha256(path)}  {path.relative_to(root).as_posix()}\n" for path in paths),
        encoding="utf-8",
    )


def controlled_environment(home: Path, temporary: Path) -> dict[str, str]:
    """Pass only reproducibility inputs and essential host runtime variables."""

    allowed_host_values = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SYSTEMROOT")
    environment = {
        key: value for key in allowed_host_values if (value := os.environ.get(key)) is not None
    }
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python", type=Path, required=True, help="installed candidate venv Python"
    )
    parser.add_argument("--wheel", type=Path, required=True, help="exact installed vfy wheel")
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    add_sampling_arguments(parser)
    args = parser.parse_args()
    sampling = sampling_config(args)
    source = require_clean(ROOT)
    python = resolve_executable(args.python)
    wheel = args.wheel.resolve()
    manifest = args.build_manifest.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"benchmark output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    identity = verify_candidate(python, wheel, manifest, source.revision)
    home = output / "home"
    temporary = output / "tmp"
    home.mkdir()
    temporary.mkdir()
    env = controlled_environment(home, temporary)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    artifacts = output / "artifacts"
    artifacts.mkdir()
    archived_wheel = artifacts / wheel.name
    archived_manifest = artifacts / "build-manifest.json"
    shutil.copyfile(wheel, archived_wheel)
    shutil.copyfile(manifest, archived_manifest)
    identity["wheel"] = {
        **identity["wheel"],
        "path": str(wheel),
        "archive_path": str(archived_wheel.relative_to(output)),
    }
    identity["build_manifest"] = {
        **identity["build_manifest"],
        "path": str(manifest),
        "archive_path": str(archived_manifest.relative_to(output)),
    }
    result = {
        "schema_version": 1,
        "benchmark": "plain-pytest-vs-vfy-test-no-cov",
        "source": {"revision": source.revision, "dirty": source.dirty},
        "harness": {
            "path": str(Path(__file__).resolve()),
            "sha256": file_sha256(Path(__file__).resolve()),
        },
        "machine": hardware_details(),
        "candidate": identity,
        "methodology": {
            **sampling.as_dict(),
            "pytest_version": PYTEST_VERSION,
            "interpreter_flags": ["-P"],
            "environment_overrides": {
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTEST_ADDOPTS": None,
                "PYTHONHASHSEED": "0",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
            },
            "execution_environment": env,
            "strata_are_separate": True,
        },
        "strata": [],
    }
    gates = {
        stratum.name: validate_stratum(stratum, python, output, env, args.timeout)
        for stratum in STRATA
    }
    require_revision(ROOT, source.revision)
    for stratum in STRATA:
        logger.info("[%s] timing after all parity gates", stratum.name)
        result["strata"].append(
            run_stratum(
                stratum,
                python,
                output,
                env,
                gates[stratum.name],
                warmups=sampling.warmups,
                runs=sampling.runs,
                timeout=args.timeout,
            )
        )
        require_revision(ROOT, source.revision)
    result_path = output / "result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_checksums(output)
    logger.info("%s", result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
