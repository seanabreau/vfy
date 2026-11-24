"""Run the controlled developer sequence against a prepared real project.

Keep one checkout through local and shared edits, a failing test, and a repair
that restores full-suite parity. Record every command's output. Preparation
and semantic checks run outside publication samples.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

# Direct execution starts with benchmarks/ on sys.path rather than the checkout
# root. Use one package-qualified import path in both direct and module execution.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.process_tree import run_captured

from benchmarks.benchmark_harness import (
    BenchmarkMode,
    SamplingConfig,
    benchmark_environment,
    rotated_orders,
    run_checked,
    summarize,
    timed_process,
)
from benchmarks.benchmark_projects import (
    command,
    load_json,
    normalized_data,
    outcome_records,
    run,
    selected_interpreter_metadata,
)
from benchmarks.benchmark_provenance import prepared_project_provenance
from benchmarks.observer_plugins import BENCHMARK_PLUGIN, OUTCOME_PLUGIN, stage_plugins
from benchmarks.project_manifest import EditPatch, ProjectManifest, manifest
from benchmarks.rich_workload import prepare_rich_workload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=("rich", "datasette"), required=True)
    parser.add_argument("--root", type=Path, required=True, help="prepared project checkout")
    parser.add_argument("--python", type=Path, required=True, help="prepared interpreter")
    parser.add_argument("--preparation-manifest", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cov-config", type=Path)
    parser.add_argument("--vfy-revision")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--runs", type=int, default=15)
    parser.add_argument(
        "--mode",
        choices=tuple(mode.value for mode in BenchmarkMode),
        default=BenchmarkMode.PUBLICATION.value,
    )
    return parser.parse_args()


def apply_patch(root: Path, patch: EditPatch) -> None:
    """Apply the source replacement only if its original text occurs exactly once."""

    path = root / patch.path
    source = path.read_text(encoding="utf-8")
    count = source.count(patch.before)
    if count != 1:
        raise RuntimeError(f"{patch.name}: expected one match in {path}, found {count}")
    path.write_text(source.replace(patch.before, patch.after, 1), encoding="utf-8")
    # Reviewed edits can preserve byte length; remove stale timestamp-based
    # bytecode so every phase imports the source just written.
    for cache in root.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache)


def clear_measured_caches(root: Path) -> None:
    """Keep reference-process caches out of the next timed vfy command."""

    for cache in root.rglob("__pycache__"):
        if cache.is_dir():
            shutil.rmtree(cache)
    pytest_cache = root / ".pytest_cache"
    if pytest_cache.is_dir():
        shutil.rmtree(pytest_cache)


def reset_checkout(root: Path, revision: str) -> None:
    """Reset one repetition while keeping all state changes inside a sequence."""

    environment = benchmark_environment()
    run_checked(["git", "reset", "--hard", revision], cwd=root, env=environment, timeout=120)
    run_checked(["git", "clean", "-fdx"], cwd=root, env=environment, timeout=120)


def _lexical_executable_path(path: Path) -> Path:
    """Normalize parent aliases without resolving the venv executable symlink."""

    return path.parent.resolve() / path.name


# Upstream files that hold each pinned project's pytest configuration. Other
# workloads write their own pytest.ini.
_UPSTREAM_PYTEST_CONFIG = {
    "colorama": "pyproject.toml",
    "dotenv": "pyproject.toml",
    "platformdirs": "pyproject.toml",
    "rich": "pyproject.toml",
    "sortedcontainers": "tox.ini",
}


def pytest_config_for(project: ProjectManifest) -> str:
    """Select the upstream config that defines this project's test contract."""

    return _UPSTREAM_PYTEST_CONFIG.get(project.key, "pytest.ini")


def validate_prepared_root(  # noqa: C901 - all checks precede reset/clean by design
    project: ProjectManifest,
    root: Path,
    python: Path,
    preparation_manifest: Path,
    artifacts: Path,
) -> dict[str, object]:
    """Validate the disposable prepared checkout before any reset or clean."""

    root = root.absolute()
    python = python.absolute()
    preparation_manifest = preparation_manifest.absolute()
    artifacts = artifacts.absolute()
    if not preparation_manifest.is_file():
        raise RuntimeError(f"preparation manifest is missing: {preparation_manifest}")
    expected_root = preparation_manifest.parent.parent / "project"
    if root.resolve() != expected_root.resolve():
        raise RuntimeError(
            f"workflow root must be the prepared project {expected_root}, got {root}"
        )
    if artifacts == root or artifacts.is_relative_to(root):
        raise RuntimeError("workflow artifacts must be outside the prepared checkout")
    metadata = json.loads(preparation_manifest.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise RuntimeError("preparation manifest must contain an object")
    if metadata.get("project") != project.key:
        raise RuntimeError("preparation manifest project does not match --project")
    if metadata.get("revision") != project.revision:
        raise RuntimeError("preparation manifest revision is not the manifest revision")
    checked_out = run_checked(
        ["git", "rev-parse", "HEAD"], cwd=root, env=benchmark_environment(), timeout=30
    ).stdout.strip()
    if checked_out != project.revision or metadata.get("checked_out_revision") != checked_out:
        raise RuntimeError("prepared checkout is not at the immutable project revision")
    status = run_checked(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        env=benchmark_environment(),
        timeout=30,
    ).stdout.strip()
    if status:
        raise RuntimeError("prepared checkout has pre-existing changes; refusing reset/clean")
    recorded_python = metadata.get("python")
    if not isinstance(recorded_python, str) or _lexical_executable_path(
        Path(recorded_python)
    ) != _lexical_executable_path(python):
        raise RuntimeError("selected interpreter does not match the preparation manifest")
    build_path = preparation_manifest.parent.parent / "vfy-build.json"
    if not build_path.is_file():
        raise RuntimeError(f"vfy build manifest is missing: {build_path}")
    wheel = json.loads(build_path.read_text(encoding="utf-8"))
    if not isinstance(wheel, dict):
        raise RuntimeError("vfy build manifest is not an object")
    required_build_fields = ("wheel_path", "wheel_sha256", "source_revision", "native_profile")
    if any(not isinstance(wheel.get(field), str) for field in required_build_fields):
        raise RuntimeError("vfy build manifest is incomplete")
    if wheel["native_profile"] != "release":
        raise RuntimeError("prepared vfy build is not the requested release revision")
    source_root_value = wheel.get("source_root")
    source_inputs = wheel.get("source_inputs")
    if not isinstance(source_root_value, str) or not isinstance(source_inputs, dict):
        raise RuntimeError("vfy build manifest has no source input bindings")
    source_root = Path(source_root_value).resolve()
    for relative, digest in source_inputs.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise RuntimeError("vfy build manifest has malformed source input bindings")
        source_file = (source_root / relative).resolve()
        if source_file.parent != source_root and source_root not in source_file.parents:
            raise RuntimeError("vfy build manifest source input escapes its source root")
        if not source_file.is_file() or (
            hashlib.sha256(source_file.read_bytes()).hexdigest() != digest
        ):
            raise RuntimeError(f"vfy build input changed after preparation: {relative}")
    wheel_file = Path(str(wheel["wheel_path"]))
    if (
        not wheel_file.is_file()
        or hashlib.sha256(wheel_file.read_bytes()).hexdigest() != wheel["wheel_sha256"]
    ):
        raise RuntimeError("prepared vfy wheel does not match its recorded digest")
    selected = selected_interpreter_metadata(
        python, root=root, environment=workflow_environment(project, python, root=root)
    )
    if selected.get("extension_sha256") != wheel.get("extension_sha256"):
        raise RuntimeError("selected interpreter native artifact does not match build manifest")
    if selected.get("build_profile") != wheel.get("native_profile"):
        raise RuntimeError("selected interpreter build profile does not match build manifest")
    return {
        "manifest": str(preparation_manifest),
        "metadata": metadata,
        "interpreter": selected,
        "vfy_build": wheel,
    }


def workflow_environment(
    project: ProjectManifest,
    python: Path,
    *,
    root: Path | None = None,
    observer_root: Path | None = None,
) -> dict[str, str]:
    environment = benchmark_environment()
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    environment.pop("VIRTUAL_ENV", None)
    for variable in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE"):
        environment.pop(variable, None)
    environment.update(project.environment)
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
    environment["VIRTUAL_ENV"] = str(python.parent.parent)
    # Child processes in real project tests inherit this controlled plugin
    # contract when autoload is disabled; the timed vfy command appends its
    # observer below.
    inherited_plugins = environment.get("PYTEST_PLUGINS")
    if not inherited_plugins:
        inherited_plugins = ",".join(project.plugins)
    environment["PYTEST_PLUGINS"] = inherited_plugins
    config = Path(pytest_config_for(project))
    environment["PYTEST_ADDOPTS"] = f"-c {config if root is None else root / config}"
    if observer_root is not None:
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(observer_root), environment.get("PYTHONPATH")) if value
        )
    return environment


def _vfy_command(
    root: Path,
    project: ProjectManifest,
    python: Path,
    *,
    phase: str,
    artifacts: Path,
    label: str,
    all_tests: bool,
    cov_config: Path | None,
) -> tuple[list[str], dict[str, str], Path, Path, Path]:
    outcome_path = artifacts / f"{label}-{phase}-outcomes.jsonl"
    coverage_file = artifacts / "state.coverage"
    # Keep the report output stable for replay while separating Datasette's
    # parallel and serial phases, whose coverage totals are intentionally
    # independent.
    report_path = artifacts / f"vfy-{phase}-report.json"
    outcome_path.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)
    if label == "baseline" and phase == "parallel":
        coverage_file.unlink(missing_ok=True)
    environment = workflow_environment(
        project, python, root=root, observer_root=stage_plugins(artifacts / "observer-plugins")
    )
    plugins = tuple(plugin for plugin in project.plugins if plugin != "pytest_asyncio.plugin")
    inherited_plugins = tuple(
        plugin for plugin in environment.get("PYTEST_PLUGINS", "").split(",") if plugin
    )
    pytest_plugins = tuple(part for plugin in plugins for part in ("-p", plugin))
    environment.update(
        {
            "COVERAGE_FILE": str(coverage_file),
            "VFY_BENCHMARK_OUTCOMES": str(outcome_path),
            "PYTEST_PLUGINS": ",".join((OUTCOME_PLUGIN, BENCHMARK_PLUGIN, *inherited_plugins)),
        }
    )
    return (
        [
            str(python),
            "-m",
            "vfy",
            "test",
            # The stock executor loads the outcome observer while vfy manages replay.
            "--engine=pytest",
            "-j",
            str(project.workers if phase == "parallel" else 1),
            "--source",
            project.package_path.rstrip("/"),
            "--history",
            ".vfy/test-history.json",
            "--data-file",
            str(coverage_file),
            *pytest_plugins,
            *(("--all",) if all_tests else ()),
            # Targets belong to vfy's parser and must precede forwarded pytest options.
            *project.tests,
            *project.coverage_args,
            *((f"--cov-config={cov_config}",) if cov_config is not None else ()),
            f"--cov-report=json:{report_path}",
            *(project.parallel_phase if phase == "parallel" else project.serial_phase),
        ],
        environment,
        outcome_path,
        report_path,
        coverage_file,
    )


def _vfy_failure(log_path: Path, reason: str) -> RuntimeError:
    with log_path.open("rb") as log:
        tail_size = min(log.seek(0, os.SEEK_END), 4096)
        log.seek(-tail_size, os.SEEK_END)
        tail = log.read().decode("utf-8", errors="replace").strip().splitlines()[-40:]
    message = f"{reason}\nFull child output: {log_path}"
    if tail:
        message += "\nChild output tail:\n" + "\n".join(f"  | {line}" for line in tail)
    return RuntimeError(message)


def _run_vfy(
    root: Path,
    project: ProjectManifest,
    python: Path,
    *,
    phases: tuple[str, ...],
    artifacts: Path,
    label: str,
    cov_config: Path | None,
    measure: bool,
    expected_returncode: int = 0,
) -> tuple[float, list[tuple[str, str, str, str | None]], list[Path], list[Path], int]:
    records: list[tuple[str, str, str, str | None]] = []
    reports: list[Path] = []
    data_files: list[Path] = []
    elapsed = 0.0
    returncode = 0
    clear_measured_caches(root)
    for phase in phases:
        command_args, environment, outcome_path, report_path, data_file = _vfy_command(
            root,
            project,
            python,
            phase=phase,
            artifacts=artifacts,
            label=label,
            # Reuse the command for unchanged runs so --all cannot bypass replay.
            # Only the final verification runs the full suite unconditionally.
            all_tests=label == "full-verification",
            cov_config=cov_config,
        )
        phase_elapsed, completed = _run_child(
            command_args,
            cwd=root,
            env=environment,
            measure=measure,
        )
        elapsed += phase_elapsed
        log_path = artifacts / f"{label}-{phase}.log"
        log_path.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        returncode = completed.returncode
        if outcome_path.is_file():
            records.extend(outcome_records(outcome_path))
        report_snapshot = artifacts / f"{label}-{phase}.json"
        if report_path.is_file():
            shutil.copy2(report_path, report_snapshot)
        reports.append(report_snapshot)
        snapshot = artifacts / f"{label}-{phase}.coverage"
        if data_file.is_file():
            shutil.copy2(data_file, snapshot)
        data_files.append(snapshot)
        if returncode:
            if returncode != expected_returncode:
                raise _vfy_failure(
                    log_path, f"{label} ({phase}): vfy command failed with exit {returncode}"
                )
            break
    return elapsed, records, reports, data_files, returncode


def _oracle(
    root: Path,
    project: ProjectManifest,
    python: Path,
    tests: tuple[str, ...],
    phases: tuple[str, ...],
    artifacts: Path,
    label: str,
    cov_config: Path | None,
) -> tuple[list[tuple[str, str, str, str | None]], list[Path], list[Path]]:
    destination = artifacts / label
    destination.mkdir(parents=True, exist_ok=True)
    environment = workflow_environment(
        project, python, root=root, observer_root=stage_plugins(artifacts / "observer-plugins")
    )
    records: list[tuple[str, str, str, str | None]] = []
    reports: list[Path] = []
    data_files: list[Path] = []
    oracle_project = replace(
        project,
        coverage_args=(
            project.coverage_args
            if "--cov-context=test" in project.coverage_args
            else (*project.coverage_args, "--cov-context=test")
        ),
    )
    for index, phase in enumerate(phases):
        outcome_path = destination / f"pytest-cov-{phase}-outcomes.jsonl"
        data_file = destination / f"pytest-cov-{phase}.coverage"
        report = destination / f"pytest-cov-{phase}.json"
        outcome_path.unlink(missing_ok=True)
        data_file.unlink(missing_ok=True)
        report.unlink(missing_ok=True)
        env = {
            **environment,
            "COVERAGE_FILE": str(data_file),
            "VFY_BENCHMARK_OUTCOMES": str(outcome_path),
        }
        run(
            command(
                str(python),
                "pytest_cov.plugin",
                oracle_project,
                reports=(f"json:{report}",),
                cov_config=cov_config,
                extra_args=(),
                tests=tests,
                phase=phase,
                outcome_path=outcome_path,
                append_coverage=index > 0,
            ),
            root=root,
            environment=env,
            log_path=destination / f"pytest-cov-{phase}.log",
        )
        records.extend(outcome_records(outcome_path))
        reports.append(report)
        data_files.append(data_file)
    return records, reports, data_files


def _run_child(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    measure: bool,
) -> tuple[float, subprocess.CompletedProcess[str]]:
    """Run one vfy command and optionally retain its elapsed time."""

    if measure:
        return timed_process(argv, cwd=cwd, env=env)
    return 0.0, run_captured(argv, cwd=cwd, env=env, timeout=300.0)


def _canonical_nodeid(nodeid: str) -> str:
    """Compare identities at the project test root, despite report path roots."""

    return nodeid.removeprefix("tests/")


def _canonical_outcomes(
    records: list[tuple[str, str, str, str | None]],
) -> list[tuple[str, str, str, str | None]]:
    return sorted(
        (_canonical_nodeid(nodeid), when, outcome, wasxfail)
        for nodeid, when, outcome, wasxfail in records
    )


def _check_reports_and_data(
    root: Path,
    vfy_reports: list[Path],
    vfy_data: list[Path],
    oracle_reports: list[Path],
    oracle_data: list[Path],
) -> None:
    if len(vfy_reports) != len(oracle_reports) or len(vfy_data) != len(oracle_data):
        raise RuntimeError("vfy and pytest-cov phase counts differ")
    for actual_report, expected_report, actual_data, expected_data in zip(
        vfy_reports, oracle_reports, vfy_data, oracle_data, strict=True
    ):
        if not actual_report.is_file():
            raise RuntimeError(f"vfy did not produce the required report: {actual_report}")
        if not expected_report.is_file():
            raise RuntimeError(f"pytest-cov did not produce the required report: {expected_report}")
        if not actual_data.is_file():
            raise RuntimeError(f"vfy did not produce the required data file: {actual_data}")
        if not expected_data.is_file():
            raise RuntimeError(
                f"pytest-cov did not produce the required data file: {expected_data}"
            )
        if load_json(actual_report, root) != load_json(expected_report, root):
            raise RuntimeError("vfy selected report differs from the independent full report")
        if normalized_data(actual_data, root) != normalized_data(expected_data, root):
            raise RuntimeError(
                "vfy schema-7 lines, arcs, contexts or tracers differ from the independent full run"
            )


def _check_outcomes(
    vfy_records: list[tuple[str, str, str, str | None]],
    oracle_records: list[tuple[str, str, str, str | None]],
    *,
    affected: bool,
    required_targets: tuple[str, ...] | None,
) -> tuple[set[str], set[str]]:
    vfy_nodes = {_canonical_nodeid(node) for node, *_rest in vfy_records}
    oracle_nodes = {_canonical_nodeid(node) for node, *_rest in oracle_records}
    if not vfy_nodes or not oracle_nodes:
        raise RuntimeError("workflow produced no public-boundary test identities")
    if affected:
        if not vfy_nodes < oracle_nodes:
            raise RuntimeError(
                f"affected selection is not a strict subset: selected={len(vfy_nodes)} "
                f"full={len(oracle_nodes)}"
            )
    elif vfy_nodes != oracle_nodes:
        raise RuntimeError("full workflow and independent oracle executed different tests")
    required_nodes: set[str] = set()
    if required_targets is None:
        required_nodes = set(oracle_nodes)
    else:
        for target in required_targets:
            canonical_target = _canonical_nodeid(target).removesuffix("/")
            matches = {
                node
                for node in oracle_nodes
                if node == canonical_target or node.startswith(f"{canonical_target}::")
            }
            if not matches:
                raise RuntimeError(
                    f"required selection target did not collect any oracle tests: {target}"
                )
            required_nodes.update(matches)
        if not required_nodes <= vfy_nodes:
            raise RuntimeError(
                f"vfy selection omitted required tests: {sorted(required_nodes - vfy_nodes)[:10]!r}"
            )
    actual_outcomes = _canonical_outcomes(vfy_records)
    expected_outcomes = _canonical_outcomes(oracle_records)
    if affected:
        expected_outcomes = [record for record in expected_outcomes if record[0] in vfy_nodes]
    if actual_outcomes != expected_outcomes:
        raise RuntimeError(
            "vfy test phase outcomes or xfail identities differ from the independent "
            f"oracle: vfy_only={sorted(set(actual_outcomes) - set(expected_outcomes))[:5]!r}, "
            f"oracle_only={sorted(set(expected_outcomes) - set(actual_outcomes))[:5]!r}"
        )
    if any(outcome == "failed" for _node, _when, outcome, _xfail in oracle_records):
        raise RuntimeError("independent full-suite oracle contains a failing test")
    if any(outcome == "failed" for _node, _when, outcome, _xfail in vfy_records):
        raise RuntimeError("vfy workflow contains a failing test outside the breaking step")
    return vfy_nodes, required_nodes


def _gate_against_oracle(
    root: Path,
    vfy_records: list[tuple[str, str, str, str | None]],
    vfy_reports: list[Path],
    vfy_data: list[Path],
    oracle_records: list[tuple[str, str, str, str | None]],
    oracle_reports: list[Path],
    oracle_data: list[Path],
    *,
    affected: bool,
    required_targets: tuple[str, ...] | None,
) -> tuple[list[str], list[str]]:
    """Check selected identities and full coverage outside the timed vfy call."""

    _check_reports_and_data(root, vfy_reports, vfy_data, oracle_reports, oracle_data)
    vfy_nodes, required_nodes = _check_outcomes(
        vfy_records,
        oracle_records,
        affected=affected,
        required_targets=required_targets,
    )
    return sorted(vfy_nodes), sorted(required_nodes)


def _selection_step(
    root: Path,
    project: ProjectManifest,
    python: Path,
    *,
    artifacts: Path,
    label: str,
    phases: tuple[str, ...],
    cov_config: Path | None,
    affected: bool,
    measure: bool,
    required_targets: tuple[str, ...] | None = None,
) -> dict[str, object]:
    elapsed, vfy_records, vfy_reports, vfy_data, returncode = _run_vfy(
        root,
        project,
        python,
        phases=phases,
        artifacts=artifacts,
        label=label,
        cov_config=cov_config,
        measure=measure,
    )
    oracle_records, oracle_reports, oracle_data = _oracle(
        root, project, python, project.tests, phases, artifacts, f"{label}-oracle", cov_config
    )
    selected, required_nodeids = _gate_against_oracle(
        root,
        vfy_records,
        vfy_reports,
        vfy_data,
        oracle_records,
        oracle_reports,
        oracle_data,
        affected=affected,
        required_targets=required_targets,
    )
    required = list(required_targets or ())
    return {
        "name": label,
        "elapsed_ms": elapsed,
        "returncode": returncode,
        "selected_tests": selected,
        "required_tests": required,
        "required_nodeids": required_nodeids,
        "selection_scope": "required targets plus conservative affected tests"
        if affected
        else "full suite",
        "full_tests": sorted({node for node, _when, _outcome, _xfail in oracle_records}),
        "phases": list(phases),
        "parity": "exact",
        "oracle": "pytest-cov full suite",
    }


def _verify_unchanged_cache(root: Path, artifacts: Path, phases: tuple[str, ...]) -> None:
    """Require replay output to preserve the baseline report without tests."""

    for phase in phases:
        baseline = artifacts / f"baseline-{phase}.json"
        unchanged = artifacts / f"unchanged-{phase}.json"
        if not baseline.is_file() or not unchanged.is_file():
            raise RuntimeError(f"unchanged cache did not produce a report for {phase}")
        if load_json(baseline, root) != load_json(unchanged, root):
            raise RuntimeError(f"unchanged cache report differs from baseline for {phase}")
        baseline_data = artifacts / f"baseline-{phase}.coverage"
        unchanged_data = artifacts / f"unchanged-{phase}.coverage"
        if not baseline_data.is_file() or not unchanged_data.is_file():
            raise RuntimeError(f"unchanged cache did not produce coverage data for {phase}")
        if normalized_data(baseline_data, root) != normalized_data(unchanged_data, root):
            raise RuntimeError(f"unchanged cache data differs from baseline for {phase}")


def run_sequence(
    root: Path,
    project: ProjectManifest,
    python: Path,
    artifacts: Path,
    *,
    reset: bool = True,
    cov_config: Path | None = None,
    measure: bool = True,
) -> dict[str, object]:
    """Run the edit sequence and return report paths and step durations."""

    artifacts.mkdir(parents=True, exist_ok=True)
    revision = (
        project.revision
        if reset and project.key == "rich"
        else run_checked(
            ["git", "rev-parse", "HEAD"], cwd=root, env=benchmark_environment(), timeout=30
        ).stdout.strip()
    )
    if reset:
        reset_checkout(root, revision)
        if project.key == "rich":
            stabilization, clock = prepare_rich_workload(root)
            (artifacts / "rich-test-adaptations.json").write_text(
                json.dumps({"stabilization": stabilization, "clock": clock}, indent=2) + "\n",
                encoding="utf-8",
            )
    steps: list[dict[str, object]] = []
    phases = project.phases
    steps.append(
        _selection_step(
            root,
            project,
            python,
            artifacts=artifacts,
            label="baseline",
            phases=phases,
            cov_config=cov_config,
            affected=False,
            measure=measure,
        )
    )
    unchanged_elapsed, unchanged_records, _reports, _data, unchanged_code = _run_vfy(
        root,
        project,
        python,
        phases=phases,
        artifacts=artifacts,
        label="unchanged",
        cov_config=cov_config,
        measure=measure,
    )
    if unchanged_records:
        raise RuntimeError("unchanged workflow unexpectedly executed tests")
    _verify_unchanged_cache(root, artifacts, phases)
    steps.append(
        {
            "name": "unchanged",
            "elapsed_ms": unchanged_elapsed,
            "returncode": unchanged_code,
            "selected_tests": [],
            "cache": "unchanged source restored baseline",
        }
    )

    patches = {patch.kind: patch for patch in project.patches}
    for kind in ("local", "shared"):
        patch = patches.get(kind)
        if patch is None:
            raise RuntimeError(f"{project.key}: missing reviewed {kind} patch")
        apply_patch(root, patch)
        steps.append(
            _selection_step(
                root,
                project,
                python,
                artifacts=artifacts,
                label=f"{kind}-edit",
                phases=("parallel",),
                cov_config=cov_config,
                affected=True,
                measure=measure,
                required_targets=patch.tests,
            )
        )

    breaking = patches.get("breaking")
    repair = patches.get("repair")
    if breaking is None or repair is None:
        raise RuntimeError(f"{project.key}: missing reviewed breaking/repair patch")
    apply_patch(root, breaking)
    breaking_elapsed, breaking_records, _reports, _data, breaking_code = _run_vfy(
        root,
        project,
        python,
        phases=("parallel",),
        artifacts=artifacts,
        label="breaking-edit",
        cov_config=cov_config,
        measure=measure,
        expected_returncode=1,
    )
    calls = [record for record in breaking_records if record[1] == "call"]
    if breaking_code != 1 or not any(record[2] == "failed" for record in calls):
        raise _vfy_failure(
            artifacts / "breaking-edit-parallel.log",
            f"breaking edit did not produce a real call failure: exit={breaking_code}, "
            f"outcomes={breaking_records[:8]!r}",
        )
    if any(
        record[2] == "failed" and record[1] in {"setup", "teardown"} for record in breaking_records
    ):
        raise RuntimeError("breaking edit produced a fixture failure instead of a test failure")
    steps.append(
        {
            "name": "breaking-edit",
            "elapsed_ms": breaking_elapsed,
            "returncode": breaking_code,
            "failure": "test call",
        }
    )
    apply_patch(root, repair)
    steps.append(
        _selection_step(
            root,
            project,
            python,
            artifacts=artifacts,
            label="repair",
            phases=("parallel",),
            cov_config=cov_config,
            affected=True,
            measure=measure,
            required_targets=repair.tests,
        )
    )
    steps.append(
        _selection_step(
            root,
            project,
            python,
            artifacts=artifacts,
            label="full-verification",
            phases=phases,
            cov_config=cov_config,
            affected=False,
            measure=measure,
        )
    )
    elapsed = sum(cast(float, step.get("elapsed_ms", 0.0)) for step in steps)
    return {
        "schema": 1,
        "project": project.name,
        "revision": revision,
        "workflow": [step["name"] for step in steps],
        "steps": steps,
        "sequence_elapsed_ms": elapsed,
        "stateful": True,
        "reset_between_repetitions": True,
        "raw_artifacts": str(artifacts),
    }


def main() -> int:
    args = parse_args()
    sampling = SamplingConfig(
        warmups=args.warmups,
        runs=args.runs,
        mode=BenchmarkMode(args.mode),
    )
    if not args.validate_only and args.output is None:
        raise SystemExit("published workflow benchmarks require --output")
    project = manifest(args.project)
    root = args.root.resolve()
    python = args.python.absolute()
    artifacts = args.artifacts.absolute()
    prepared = validate_prepared_root(
        project,
        root,
        python,
        args.preparation_manifest,
        artifacts,
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    cov_config = args.cov_config
    if cov_config is None and args.project == "rich":
        cov_config = Path(__file__).resolve().parent / "configs/rich.coveragerc"
    wheel = prepared["vfy_build"]
    if not isinstance(wheel, dict):
        raise RuntimeError("prepared wheel evidence is malformed")
    recorded_revision = wheel.get("source_revision")
    if not isinstance(recorded_revision, str):
        raise RuntimeError("prepared build has no vfy source revision")
    if args.vfy_revision is not None and args.vfy_revision != recorded_revision:
        raise RuntimeError("--vfy-revision does not match the prepared wheel")
    methodology = {
        "mode": sampling.mode.value,
        "warmups": sampling.warmups,
        "measured_runs": sampling.runs,
        "sampling": sampling.as_dict(),
        "validation": (
            "independent pytest-cov full-suite outcomes and schema-7 reports/data are "
            "checked before publication samples"
        ),
        "timed": "child vfy commands only; oracle, parsing, copies and gates are outside timers",
        "cache_state": (
            "upstream __pycache__ and .pytest_cache are cleared before every timed vfy "
            "sequence; OS page cache is untouched; vfy history persists within a "
            "sequence and resets between repetitions"
        ),
        "sequence_total": "sum of per-step child command times in one stateful sequence",
        "state": (
            "baseline -> unchanged cache -> local edit -> shared edit -> breaking edit -> "
            "repair -> full verification"
        ),
        "selection": (
            "reviewed required targets plus conservative extra identities from the "
            "independent full oracle"
        ),
        "coverage_gate": (
            "full successful reports and schema-7 lines/arcs/contexts/tracers match pytest-cov"
        ),
        "outcome_gate": (
            "setup/call/teardown identities, outcomes and xfail markers match the "
            "independent oracle"
        ),
        "reset": "prepared checkout reset and cleaned between repetitions after input validation",
    }
    selected = prepared.get("interpreter")
    if not isinstance(selected, dict):
        raise RuntimeError("prepared interpreter evidence is malformed")
    provenance = prepared_project_provenance(
        Path(__file__).resolve().parents[1],
        workload={"project": project.key, "workflow": "stateful-edit-sequence"},
        workers=project.workers if isinstance(project.workers, int) else None,
        cache="prepared checkout; history reset between repetitions",
        packages=("pytest", "pytest-cov"),
        selected=selected,
        prepared=prepared,
        source_revision=recorded_revision,
        upstream_revision=project.revision,
        sampling=sampling.as_dict(),
        publication=sampling.publishable,
        validation="full stateful protocol and independent parity gates passed",
    )
    if args.validate_only:
        result = run_sequence(
            root, project, python, artifacts, cov_config=cov_config, measure=False
        )
        result["benchmark"] = "workflows"
        result["workload"] = {"project": project.key, "workflow": "stateful-edit-sequence"}
        result["vfy_revision"] = recorded_revision
        result["sampling"] = sampling.as_dict()
        result["methodology"] = methodology
        result["provenance"] = provenance
    else:
        validation_artifacts = artifacts / "validation"
        validation = run_sequence(
            root, project, python, validation_artifacts, cov_config=cov_config, measure=False
        )
        validation_path = validation_artifacts / "sequence.json"
        validation_path.write_text(
            json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        warmups: list[dict[str, object]] = []
        for index, order in enumerate(rotated_orders(("sequence",), sampling.warmups)):
            sample_artifacts = artifacts / f"warmup-{index:02d}"
            sample = run_sequence(root, project, python, sample_artifacts, cov_config=cov_config)
            sample_path = sample_artifacts / "sequence.json"
            sample_path.write_text(
                json.dumps(sample, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            warmups.append(
                {
                    "index": index,
                    "order": list(order),
                    "sequence_elapsed_ms": sample["sequence_elapsed_ms"],
                    "raw_artifacts": str(sample_artifacts),
                    "sequence": str(sample_path),
                }
            )
        measured: list[dict[str, object]] = []
        samples: list[float] = []
        for index, order in enumerate(rotated_orders(("sequence",), sampling.runs)):
            sample_artifacts = artifacts / f"run-{index:02d}"
            sample = run_sequence(root, project, python, sample_artifacts, cov_config=cov_config)
            sample_path = sample_artifacts / "sequence.json"
            sample_path.write_text(
                json.dumps(sample, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            elapsed = cast(float, sample["sequence_elapsed_ms"])
            samples.append(elapsed)
            measured.append(
                {
                    "index": index,
                    "order": list(order),
                    "sequence_elapsed_ms": elapsed,
                    "raw_artifacts": str(sample_artifacts),
                    "sequence": str(sample_path),
                }
            )
        result = {
            "schema": 2,
            "benchmark": "workflows",
            "project": project.name,
            "workload": {"project": project.key, "workflow": "stateful-edit-sequence"},
            "revision": project.revision,
            "vfy_revision": recorded_revision,
            "mode": sampling.mode.value,
            "sampling": sampling.as_dict(),
            "methodology": methodology,
            "provenance": provenance,
            "validation": {
                "sequence": str(validation_path),
                "raw_artifacts": str(validation_artifacts),
            },
            "warmups": sampling.warmups,
            "warmup_records": warmups,
            "runs": sampling.runs,
            "samples": measured,
            "sequence": summarize(samples),
            "raw_artifacts": str(artifacts),
            "stateful": True,
            "reset_between_repetitions": True,
        }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
