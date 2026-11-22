"""Parity-gated full-project latency for explicit vfy engines and stock pytest.

Preflights compare complete JUnit testcase multisets and verify native engine,
phase, and execution inventories. They do not compare stock session warnings or
all stock phase records. Samples omit requested native JSON and the stock
observer, retaining identical JUnit capture flags and ordinary native CLI history.
The optional native-direct serial diagnostic uses pytest's public native plug-in
and result bridge without CLI selection/history; all vfy routes disable coverage.
Independent routes additionally compare complete phase records and warnings with
serial stock before timing. Only pytest 9.1.1's framework-owned marker-iterator
deprecation compares its public API origin and exact structured payload; actual
origins, rendered diagnostics, raw differences and normalization receipts remain
in the evidence. Project-origin warnings retain exact diagnostic comparison.
The default full evidence mode retains independent
result JSON inside every timed command; junit mode keeps full preflight proof
and checks JUnit/status in each sample while retaining ordinary CLI defaults.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import json
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.benchmark_harness import (  # noqa: E402
    SamplingConfig,
    add_sampling_arguments,
    paired_uncertainty,
    rotated_orders,
    sampling_config,
    summarize,
)
from benchmarks.benchmark_provenance import hardware_details, require_clean  # noqa: E402
from scripts.pytest_execution_record import (  # noqa: E402
    file_sha256,
    interpreter_metadata,
    native_cases,
    native_session_warnings,
    write_atomic,
)
from validation.pytest_runner import (  # noqa: E402
    benchmark_native_projects as serial,
    check_native_adoption_matrix as matrix,
    check_native_xdist_adoption as adoption,
)

SOURCE_INPUTS = {
    **serial.SOURCE_INPUT_SHA256,
    **{
        name: file_sha256(ROOT / name)
        for name in (
            "validation/pytest_runner/benchmark_native_xdist.py",
            "validation/pytest_runner/check_native_xdist_adoption.py",
            "benchmarks/rich_clock_plugin.py",
        )
    },
}

_ITERATOR_WARNING = re.compile(
    r"Passing a non-Collection iterable to parametrize is deprecated\.\n"
    r"Test: [^\n]+, argvalues type: [^\n]+\n"
    r"Please convert to a list or tuple\.\n"
    r"See https://docs\.pytest\.org/en/stable/deprecations\.html#parametrize-iterators\Z"
)


def framework_warning_projection(
    warnings: list[dict[str, Any]],
    metadata: list[dict[str, Any]],
    root: Path,
    python: Path,
    basetemp: Path,
    *,
    engine: Literal["stock", "native"],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project one verified framework emitter; keep every other warning literal."""
    projected, receipts = [], []
    available = list(metadata)
    for index, warning in enumerate(warnings):
        match = None
        for position, raw in enumerate(available):
            normalized = matrix.normalize_session_warning(raw, root, python, basetemp)
            if (
                all(
                    normalized.get(key) == warning.get(key)
                    for key in ("nodeid", "filename", "lineno")
                )
                and normalized.get("message", "").rstrip() == warning["message"].rstrip()
            ):
                match = available.pop(position)
                break
        if match is None:
            projected.append(warning)
            continue
        payload = match.get("raw_message")
        recognized = (
            warning.get("nodeid") == ""
            and match.get("category") == "PytestRemovedIn10Warning"
            and isinstance(payload, str)
            and _ITERATOR_WARNING.fullmatch(payload) is not None
        )
        source_line = match.get("source_line")
        if recognized and engine == "stock":
            recognized = (
                match.get("emitter_route") == "builtin-marker-parametrize"
                and warning["filename"].endswith("/site-packages/_pytest/python.py")
                and warning["lineno"] == 124
                and source_line
                == "metafunc.parametrize(*marker.args, **marker.kwargs, _param_mark=marker)"
            )
        elif recognized:
            filename = Path(match["filename"])
            recognized = (
                filename.name == "protocol.py"
                and filename.parent.name == "independent"
                and filename.parent.parent.name == "vfy"
                and filename.resolve().is_relative_to(python.parent.parent.resolve())
            )
            if recognized:
                try:
                    source = filename.read_text(encoding="utf-8")
                    line = match["lineno"]
                    source_line = source.splitlines()[line - 1].strip()
                    recognized = source_line == "items = operation.collect()" and any(
                        isinstance(node, ast.FunctionDef)
                        and node.name == "perform_collect"
                        and node.lineno <= line <= (node.end_lineno or node.lineno)
                        for node in ast.walk(ast.parse(source))
                    )
                except (OSError, IndexError, SyntaxError):
                    recognized = False
        if recognized:
            standard = (
                f"{warning['filename']}:{warning['lineno']}: PytestRemovedIn10Warning: {payload}\n"
                f"  {source_line}\n"
            )
            recognized = warning["message"].rstrip() == standard.rstrip()
        if not recognized:
            projected.append(warning)
            continue
        canonical = {
            "nodeid": warning["nodeid"],
            "category": match["category"],
            "message": payload,
            "logical_origin": "pytest.Metafunc.parametrize/builtin-marker-iterator",
        }
        projected.append(canonical)
        receipts.append(
            {
                "index": index,
                "rule": "framework-marker-iterator-origin-v1",
                "raw_warning": warning,
                "source_line": source_line,
                "projection": canonical,
            }
        )
    return projected, receipts


@dataclass(frozen=True)
class Route:
    engine: Literal["stock", "pytest-native", "native"]
    workers: int
    xdist: bool = True
    transport: Literal["cli", "direct"] = "cli"

    def __post_init__(self) -> None:
        if self.engine == "native" and (self.xdist or self.workers < 1):
            raise ValueError("independent routes require positive vfy worker counts without xdist")
        if self.transport == "direct" and (
            self.engine not in {"pytest-native", "native"} or self.workers != 1 or self.xdist
        ):
            raise ValueError("direct routes support only serial vfy execution")

    @property
    def name(self) -> str:
        if self.transport == "direct":
            return f"{self.engine}-direct-j{self.workers}"
        return (
            f"{self.engine}-xdist-{self.workers}"
            if self.xdist
            else f"{self.engine}-j{self.workers}"
        )

    @property
    def reference(self) -> str:
        workers = self.workers if self.xdist or self.engine == "native" else 0
        return f"stock-xdist-{0 if self.engine == 'native' and workers == 1 else workers}"


def native_experiments(
    scheduling: str | None,
    worker_collection: bool = False,
    worker_start: str | None = None,
    startup_grace_ms: int | None = None,
) -> dict[str, str]:
    environment = {}
    if scheduling is not None:
        environment["VFY_NATIVE_SCHEDULING"] = scheduling
    if worker_collection:
        environment["VFY_WORKER_COLLECTION"] = "1"
    if worker_start is not None:
        environment["VFY_WORKER_START"] = worker_start
    if startup_grace_ms is not None:
        environment["VFY_NATIVE_STARTUP_GRACE_MS"] = str(startup_grace_ms)
    return environment


def route_environment(
    project: matrix.Project,
    root: Path,
    python: Path,
    route: Route,
    native_scheduling: str | None,
    worker_collection: bool = False,
    worker_start: str | None = None,
    startup_grace_ms: int | None = None,
    *,
    output: Path | None = None,
) -> dict[str, str]:
    environment = project_environment(project, root, python)
    if route.engine == "native" and native_scheduling is not None:
        environment["VFY_NATIVE_SCHEDULING"] = native_scheduling
    if route.engine == "pytest-native":
        environment.update(
            native_experiments(
                native_scheduling,
                worker_collection,
                worker_start,
                startup_grace_ms,
            )
        )
        if route.transport == "direct":
            if output is None:
                raise ValueError("native-direct requires its route output directory")
            environment["VFY_NATIVE_COVERAGE_BRIDGE"] = "1"
            environment["VFY_NATIVE_WORKERS"] = "1"
            environment["VFY_TEST_RESULTS"] = str((output / "direct-results.json").resolve())
    return environment


def project_environment(project: matrix.Project, root: Path, python: Path) -> dict[str, str]:
    """Expose the optional clock fixture equally to stock and native routes."""
    environment = matrix.project_environment(project, root, python)
    if "rich_clock_plugin" in project.plugins:
        environment["PYTHONPATH"] += f":{ROOT / 'benchmarks'}"
    return environment


def command(
    project: matrix.Project,
    root: Path,
    python: Path,
    output: Path,
    basetemp: Path | None,
    route: Route,
    *,
    observer: bool = False,
    capture: str | None = None,
    timed_evidence: Literal["full", "junit"] = "full",
    junit: bool = True,
    history_path: Path | None = None,
) -> list[str]:
    """Keep test selectors and JUnit evidence identical across worker transports."""
    if route.engine == "stock":
        prefix = [str(python), "-P", "-m", "pytest"]
    elif route.transport == "direct" and route.engine == "native":
        prefix = [
            str(python),
            "-P",
            "-m",
            "vfy.independent.entry",
            "-p",
            "vfy.independent.coverage_support",
        ]
        if timed_evidence == "full":
            prefix += [f"--native-result-json={output / 'independent.json'}"]
    elif route.transport == "direct":
        prefix = [
            str(python),
            "-P",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:asyncio",
            "-p",
            "vfy.pytest_plugin",
            "-p",
            "vfy._pytest.native_pytest",
        ]
    else:
        prefix = [
            str(python),
            "-m",
            "vfy",
            "test",
            f"--engine={route.engine}",
        ]
        if history_path is not None or route.engine != "native" or timed_evidence == "full":
            prefix += ["--history", str(history_path or output / "history.json")]
        if route.engine == "native" and timed_evidence == "full":
            prefix += [f"--native-result-json={output / 'independent.json'}"]
    plugins = tuple(plugin for plugin in project.plugins if plugin != "xdist.plugin")
    if route.xdist:
        plugins = (*plugins, "xdist.plugin")
    descriptor = replace(project, plugins=plugins)
    project_options = _without_worker_options(
        (
            *project.extra_args,
            *project.outcome_args,
            *(project.stock_extra_args if route.engine == "stock" else ()),
        )
    )
    return [
        *prefix,
        *(
            [
                "--numprocesses" if route.engine == "pytest-native" else "-n",
                str(route.workers),
                "--dist=load",
            ]
            if route.xdist
            else ([f"--workers={route.workers}"] if route.transport == "cli" else [])
        ),
        *(["-p", "pytest_execution_probe"] if observer else []),
        *matrix.plugin_args(descriptor, stock=route.engine == "stock"),
        *(["--basetemp", str(basetemp)] if basetemp is not None else []),
        *(
            [
                f"--junitxml={output / 'junit.xml'}",
                "-o",
                "junit_logging=all",
                "-o",
                "junit_log_passing_tests=true",
            ]
            if junit
            else []
        ),
        "-q",
        *([f"--capture={capture}"] if capture is not None else []),
        *(["--no-cov"] if route.engine != "stock" else []),
        *project_options,
        *matrix.expanded_tests(root, project.tests),
    ]


def _without_worker_options(arguments: Sequence[str]) -> list[str]:
    """Let the benchmark route choose workers over upstream serial defaults."""

    selected: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-n", "--numprocesses"}:
            if index + 1 >= len(arguments):
                raise ValueError(f"{argument} has no worker count")
            index += 2
            continue
        if argument.startswith("--numprocesses=") or (
            argument.startswith("-n") and len(argument) > 2
        ):
            index += 1
            continue
        selected.append(argument)
        index += 1
    return selected


def _explicit_capture(arguments: Sequence[str]) -> str | None:
    capture = None
    iterator = iter(arguments)
    for argument in iterator:
        if argument == "--":
            break
        if (
            argument.startswith("-")
            and not argument.startswith("--")
            and ("s" in argument[1:] and set(argument[1:]) <= set("svqxl"))
        ):
            capture = "no"
        elif argument == "--capture":
            capture = next(iterator, None)
        elif argument.startswith("--capture="):
            capture = argument.partition("=")[2]
    return capture


def resolve_basetemp(
    project: matrix.Project,
    root: Path,
    python: Path,
    output: Path,
    timeout: float,
    *,
    capture: str | None = None,
    override_upstream_capture: bool = False,
) -> Path | None:
    """Respect projects that disable pytest's temporary-directory plug-in."""
    arguments = [*project.extra_args, *project.outcome_args, *project.tests]
    program = (
        "import json, sys; from vfy.run.engine import _resolve_pytest_configuration; "
        "c = _resolve_pytest_configuration(json.loads(sys.argv[1])); "
        "print(json.dumps({'plugins': c.plugins, 'effective_args': c.effective_args}))"
    )
    run = matrix.invoke(
        [str(python), "-c", program, json.dumps(arguments)],
        root,
        project_environment(project, root, python),
        output / "configuration",
        timeout,
    )
    if run["exit_status"] != 0:
        raise ValueError("could not resolve the upstream temporary-directory configuration")
    configuration = json.loads(run["stdout"])
    write_atomic(output / "configuration.json", configuration)
    if capture is not None:
        for arguments in (
            configuration["effective_args"],
            [*configuration["effective_args"], *project.stock_extra_args],
        ):
            configured = _explicit_capture(arguments)
            if configured is not None and configured != capture and not override_upstream_capture:
                raise ValueError(
                    f"requested --capture={capture} conflicts with the project's "
                    f"explicit --capture={configured}"
                )
    return None if "no:tmpdir" in configuration["plugins"] else root.parent / "pytest-tmp"


def native_proof(
    output: Path,
    root: Path,
    python: Path,
    basetemp: Path,
    route: Route,
    cases: list[dict[str, Any]],
    inventory: list[str],
) -> list[dict[str, Any]]:
    """Bind every executed native case to the independently observed stock inventory."""
    records = adoption.native_execution_records(
        output,
        root,
        python,
        basetemp,
        workers=route.workers if route.xdist else 0,
        junit_cases=cases,
    )
    executed = []
    for record in records:
        raw = json.loads(Path(record["path"]).read_text())
        native_cases, _ = matrix.normalize_native(raw, root, python, basetemp)
        executed.extend(matrix.normalize_nodeid(case["nodeid"], root) for case in native_cases)
    if sorted(executed) != sorted(inventory):
        raise ValueError("native executed node IDs differ from the full stock inventory")
    return records


def execution_observations(
    cases: list[dict[str, Any]], project: matrix.Project | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply reviewed, revision-bound Click and attrs allocation-address rules."""
    if project is not None and project.key in {"click", "attrs"}:
        return matrix.normalize_adoption_observations(
            cases, project=project.key, revision=project.revision
        )
    return cases, []


def independent_execution(
    output: Path,
    root: Path,
    python: Path,
    basetemp: Path,
    route: Route,
    *,
    project: matrix.Project | None = None,
    require_active_workers: bool = False,
) -> dict[str, Any]:
    """Require the independent engine and normalize its complete phase records."""
    raw = json.loads((output / "independent.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("independent execution record is not an object")
    if require_active_workers and route.workers > 1:
        topology = raw.get("execution_topology")
        if not isinstance(topology, dict):
            raise ValueError("distributed independent execution topology is missing")
        pids = topology.get("worker_pids")
        if (
            topology.get("requested_workers") != route.workers
            or topology.get("active_workers") != route.workers
            or not isinstance(pids, list)
            or len(pids) != route.workers
            or any(type(pid) is not int or pid <= 0 for pid in pids)
            or len(set(pids)) != route.workers
        ):
            raise ValueError(
                "distributed independent execution did not activate all requested workers"
            )
    cases = native_cases(
        raw,
        root,
        tempfile.gettempdir(),
        str(python.parent.parent.resolve()),
        str(basetemp.resolve()),
        expected_engine="vfy-independent-v1",
    )
    session = native_session_warnings(
        raw,
        cases,
        root,
        tempfile.gettempdir(),
        str(python.parent.parent.resolve()),
        str(basetemp.resolve()),
    )
    matrix.validate_cases(cases, session)
    if type(raw.get("collected")) is not int or raw["collected"] != len(cases) or not cases:
        raise ValueError("independent execution inventory is incomplete")
    if raw.get("exitstatus") != 0:
        raise ValueError("independent execution record has a nonzero or missing session status")
    cases = matrix.normalize_cases_paths(cases, root, python, basetemp)
    session = [
        matrix.normalize_session_warning(warning, root, python, basetemp) for warning in session
    ]
    raw_session = session
    session, warning_origins = framework_warning_projection(
        session, raw["warnings"], root, python, basetemp, engine="native"
    )
    # Worker completion order is nondeterministic; retain each case's ordered
    # phases and duplicate multiplicity while comparing the complete inventory.
    if route.workers > 1:
        cases.sort(key=lambda case: case["nodeid"])
    cases, normalizations = execution_observations(cases, project)
    result = {
        "cases": cases,
        "session_warnings": session,
        "raw_session_warnings": raw_session,
        "warning_origin_normalizations": warning_origins,
        "observation_normalizations": normalizations,
    }
    if require_active_workers and route.workers > 1:
        result["execution_topology"] = raw["execution_topology"]
    return result


def independent_proof(
    output: Path,
    root: Path,
    python: Path,
    basetemp: Path,
    route: Route,
    stock_execution: dict[str, Any],
    *,
    project: matrix.Project | None = None,
    require_active_workers: bool = False,
) -> dict[str, Any]:
    """Retain phase parity and the declared framework-warning semantic projection."""
    execution = independent_execution(
        output,
        root,
        python,
        basetemp,
        route,
        project=project,
        require_active_workers=require_active_workers,
    )
    topology = execution.pop("execution_topology", None)
    stock_cases = stock_execution["cases"]
    if route.workers > 1:
        stock_cases = sorted(stock_cases, key=lambda case: case["nodeid"])
    compared = ("cases", "session_warnings", "observation_normalizations")
    stock_view = {key: stock_execution[key] for key in compared}
    stock_view["cases"] = stock_cases
    actual_view = {key: execution[key] for key in compared}
    difference = matrix.first_difference(stock_view, actual_view)
    raw_difference = matrix.first_difference(
        {**stock_view, "session_warnings": stock_execution["raw_session_warnings"]},
        {**actual_view, "session_warnings": execution["raw_session_warnings"]},
    )
    execution_path = output / "execution-parity.json"
    write_atomic(execution_path, execution)
    write_atomic(
        output / "execution-difference.json",
        {
            "comparison": "phase-records-and-framework-warning-semantics-v1",
            "difference": difference,
            "raw_diagnostic_difference": raw_difference,
            "warning_origin_normalizations": {
                "stock": stock_execution["warning_origin_normalizations"],
                "native": execution["warning_origin_normalizations"],
            },
        },
    )
    if difference is not None:
        raise ValueError("independent phase records or warnings differ from full stock")
    return {
        "engine": "vfy-independent-v1",
        "execution_topology": topology,
        "path": str(execution_path),
        "sha256": file_sha256(execution_path),
    }


def runtest_junit_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude pytest's JUnit entries for modules skipped before collection."""
    return [
        case
        for case in cases
        if not (
            case["identity"][0] == ""
            and len(case["children"]) == 1
            and case["children"][0]["tag"] == "skipped"
            and case["children"][0]["attributes"].get("message") == "collection skipped"
        )
    ]


def restore_project_history(
    root: Path,
    snapshot: Path,
    history_path: Path | None = None,
    history_seed: Path | None = None,
) -> None:
    """Restore source and an optional immutable history seed outside timing."""
    serial.restore_project(root, snapshot)
    if history_seed is not None:
        if history_path is None:
            raise ValueError("a history seed requires an explicit history path")
        history_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(history_seed, history_path)


def preflight_junit_cases(
    output: Path, root: Path, temporary: Path, *, junit: bool
) -> list[dict[str, Any]]:
    """Require complete requested JUnit evidence; disabled mode writes none."""
    if not junit:
        return []
    cases = adoption.junit_records(output / "junit.xml", root, temporary)
    if not cases:
        raise ValueError("preflight has no JUnit testcases")
    return cases


def preflight(
    project: matrix.Project,
    root: Path,
    snapshot: Path,
    python: Path,
    output: Path,
    basetemp: Path | None,
    routes: list[Route],
    wheel: Path,
    timeout: float,
    *,
    native_scheduling: str | None = None,
    worker_collection: bool = False,
    worker_start: str | None = None,
    startup_grace_ms: int | None = None,
    capture: str | None = None,
    junit: bool = True,
    history_path: Path | None = None,
    history_seed: Path | None = None,
    require_active_workers: bool = False,
) -> tuple[list[Route], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    if not junit and any(route.engine == "pytest-native" for route in routes):
        raise ValueError("preflights without JUnit require stock or independent routes")
    expected: dict[str, list[dict[str, Any]]] = {}
    proof: dict[str, Any] = {}
    inventory: list[str] = []
    stock_execution: dict[str, Any] = {}
    accepted = []
    normalizer_temp = basetemp or root.parent / "pytest-tmp"
    purelib = interpreter_metadata(python, "vfy")["purelib"]
    for route in routes:
        restore_project_history(root, snapshot, history_path, history_seed)
        destination = output / route.name
        environment = route_environment(
            project,
            root,
            python,
            route,
            native_scheduling,
            worker_collection,
            worker_start,
            startup_grace_ms,
            output=destination,
        )
        observer = route == Route("stock", 0)
        if observer:
            environment = matrix.observer_environment(environment, root, python, destination)
        elif route.engine == "pytest-native":
            environment["PYTEST_NATIVE_RESULT"] = str(destination / "native.json")
        run = matrix.invoke(
            command(
                project,
                root,
                python,
                destination,
                basetemp,
                route,
                observer=observer,
                capture=capture,
                junit=junit,
                history_path=history_path,
            ),
            root,
            environment,
            destination,
            timeout,
        )
        errors = []
        records = []
        execution_parity: dict[str, Any] | None = None
        cases: list[dict[str, Any]] = []
        try:
            if run["exit_status"] != 0:
                raise ValueError(f"preflight exited {run['exit_status']}")
            cases = preflight_junit_cases(destination, root, normalizer_temp, junit=junit)
            if observer:
                raw = matrix.load_outcome_record(
                    run, destination, environment, "execution.json", root
                )
                matrix.verify_outcome_artifacts(run, purelib, wheel, "stock")
                stock_cases, stock_warnings, inventory = matrix.normalize_outcome_record(
                    "stock", raw, run, root, python, normalizer_temp
                )
                assert raw is not None
                stock_cases, normalizations = execution_observations(stock_cases, project)
                raw_stock_warnings = stock_warnings
                stock_warnings, warning_origins = framework_warning_projection(
                    stock_warnings,
                    raw.get("warning_metadata", []),
                    root,
                    python,
                    normalizer_temp,
                    engine="stock",
                )
                stock_execution = {
                    "cases": stock_cases,
                    "session_warnings": stock_warnings,
                    "raw_session_warnings": raw_stock_warnings,
                    "warning_origin_normalizations": warning_origins,
                    "observation_normalizations": normalizations,
                }
                runtest_cases = runtest_junit_cases(cases)
                if run["record_errors"] or (junit and len(inventory) != len(runtest_cases)):
                    raise ValueError("full stock observer inventory is incomplete or invalid")
                write_atomic(output / "inventory.json", {"nodeids": inventory})
            elif route.engine == "stock":
                baseline = expected[Route("stock", 0).name]
                comparison = adoption.compare_phase_outcomes(
                    {"junit_cases": baseline}, {"junit_cases": cases}
                )
                if not comparison["equal"]:
                    raise ValueError(
                        "distributed stock inventory or outcomes differ from serial stock"
                    )
            elif route.engine == "pytest-native":
                records = native_proof(
                    destination, root, python, normalizer_temp, route, cases, inventory
                )
                if cases != expected.get(route.reference):
                    raise ValueError(
                        "native JUnit captures, outcomes or properties differ from stock"
                    )
            else:
                execution_parity = independent_proof(
                    destination,
                    root,
                    python,
                    normalizer_temp,
                    route,
                    stock_execution,
                    project=project,
                    require_active_workers=require_active_workers,
                )
                if junit and cases != expected.get(route.reference):
                    raise ValueError("independent JUnit evidence differs from stock")
            expected[route.name] = cases
            accepted.append(route)
        except (OSError, KeyError, TypeError, ValueError) as error:
            errors.append(str(error))
        artifacts = serial.archive_run(destination)
        proof[route.name] = {
            "passed": not errors,
            "errors": errors,
            "cases": len(cases) if junit else len(inventory),
            "native_execution": records,
            "independent_execution": (
                {**execution_parity, **artifacts["execution-parity.json"]}
                if execution_parity is not None
                else None
            ),
            "native_environment": {
                key: value for key, value in environment.items() if key.startswith("VFY_")
            },
            "artifacts": artifacts,
        }
        write_atomic(output / "parity.json", proof)
        if observer and errors:
            raise ValueError("full serial stock preflight failed; see retained parity.json")
    native = [route for route in accepted if route.engine != "stock"]
    references = {route.reference for route in native}
    allowed = [route for route in accepted if route in native or route.name in references]
    return allowed, expected, proof


def sample(
    project: matrix.Project,
    root: Path,
    snapshot: Path,
    python: Path,
    output: Path,
    basetemp: Path | None,
    routes: list[Route],
    expected: dict[str, list[dict[str, Any]]],
    sampling: SamplingConfig,
    timeout: float,
    *,
    resources: bool,
    tree_resources: bool = False,
    native_scheduling: str | None = None,
    worker_collection: bool = False,
    worker_start: str | None = None,
    startup_grace_ms: int | None = None,
    capture: str | None = None,
    execution_expected: dict[str, dict[str, Any]] | None = None,
    timed_evidence: Literal["full", "junit"] = "full",
) -> dict[str, Any]:
    names = {route.name: route for route in routes}
    values: dict[str, list[float]] = {name: [] for name in names}
    expected_execution = {}
    for route in routes:
        if route.engine == "native":
            metadata = (execution_expected or {}).get(route.name)
            if metadata is None or metadata.get("preflight_passed") is not True:
                raise ValueError(
                    "independent samples require successful full preflight phase parity"
                )
            path = Path(metadata["path"])
            if file_sha256(path) != metadata["retained_sha256"]:
                raise ValueError("independent retained preflight phase parity changed")
            with (
                gzip.open(path, "rt", encoding="utf-8")
                if path.suffix == ".gz"
                else path.open(encoding="utf-8")
            ) as stream:
                expected_execution[route.name] = json.load(stream)
    records = []
    for phase, count in (("warmup", sampling.warmups), ("sample", sampling.runs)):
        for index, order in enumerate(rotated_orders(list(names), count)):
            for name in order:
                serial.restore_project(root, snapshot)
                destination = output / f"{phase}-{index:02d}-{name}"
                environment = route_environment(
                    project,
                    root,
                    python,
                    names[name],
                    native_scheduling,
                    worker_collection,
                    worker_start,
                    startup_grace_ms,
                    output=destination,
                )
                argv = command(
                    project,
                    root,
                    python,
                    destination,
                    basetemp,
                    names[name],
                    capture=capture,
                    timed_evidence=timed_evidence,
                )
                if tree_resources:
                    run = serial.invoke_with_resources(
                        argv, root, environment, destination, timeout, tree=True
                    )
                elif resources:
                    run = serial.invoke_with_resources(
                        argv, root, environment, destination, timeout
                    )
                else:
                    run = matrix.invoke(argv, root, environment, destination, timeout)
                cases = adoption.junit_records(
                    destination / "junit.xml", root, basetemp or root.parent / "pytest-tmp"
                )
                if run["exit_status"] != 0 or cases != expected[name]:
                    raise ValueError(
                        f"sample status or JUnit testcase multiset changed: {destination}"
                    )
                if names[name].engine == "native" and timed_evidence == "full":
                    execution = independent_execution(
                        destination,
                        root,
                        python,
                        basetemp or root.parent / "pytest-tmp",
                        names[name],
                        project=project,
                    )
                    if execution != expected_execution[name]:
                        raise ValueError(f"sample independent phase records changed: {destination}")
                records.append(
                    {
                        "phase": phase,
                        "index": index,
                        "route": name,
                        "timed_evidence": timed_evidence,
                        "per_sample_phase_validation": (
                            names[name].engine == "native" and timed_evidence == "full"
                        ),
                        "seconds": run["seconds"],
                        "native_environment": {
                            key: value
                            for key, value in environment.items()
                            if key.startswith("VFY_")
                        },
                        "artifacts": serial.archive_run(destination),
                        **{
                            field: run[field]
                            for field in (
                                "resources",
                                "outer_wrapper_seconds",
                                "collector_artifacts",
                            )
                            if field in run
                        },
                    }
                )
                if phase == "sample":
                    values[name].append(run["seconds"] * 1000)
                write_atomic(output / "samples.json", {"samples": records})
    return {
        "timed_evidence": timed_evidence,
        "independent_per_sample_phase_validation": (
            any(route.engine == "native" for route in routes) and timed_evidence == "full"
        ),
        "samples": records,
        "summary": {name: summarize(v) for name, v in values.items()},
        "comparisons": {
            route.name: {
                "reference": route.reference,
                **paired_uncertainty(values[route.name], values[route.reference]),
            }
            for route in routes
            if route.engine != "stock" and route.reference in values
        },
    }


def run_project(args: argparse.Namespace, key: str, sampling: SamplingConfig) -> dict[str, Any]:
    project = matrix.PROJECTS_BY_KEY[key]
    output = args.output / key
    root, snapshot, python, identity = serial.prepare_runtime(
        project, args.work_root / key, output, args
    )
    write_atomic(output / "identity.json", identity)
    basetemp = resolve_basetemp(
        project,
        root,
        python,
        output,
        args.timeout,
        capture=args.capture,
        override_upstream_capture=getattr(args, "override_upstream_capture", False),
    )
    workers = sorted({0, *args.workers})
    routes = [Route(engine, count) for count in workers for engine in ("stock", "pytest-native")]
    routes.extend(Route("pytest-native", count, False) for count in args.native_workers)
    routes.extend(
        Route("pytest-native", count, False, "direct")
        for count in getattr(args, "native_direct_workers", [])
    )
    for count in args.independent_workers:
        reference = Route("stock", 0 if count == 1 else count)
        if reference not in routes:
            routes.append(reference)
        routes.append(Route("native", count, False))
    selected = [route for route in routes if route.name in args.timing_route]
    if args.timing_route:
        required = {Route("stock", 0).name, *(route.reference for route in selected)}
        required.update(args.timing_route)
        routes = [route for route in routes if route.name in required]
    allowed, expected, parity = preflight(
        project,
        root,
        snapshot,
        python,
        output / "preflight",
        basetemp,
        routes,
        args.wheel,
        args.timeout,
        native_scheduling=args.native_scheduling,
        worker_collection=args.native_worker_collection,
        worker_start=args.native_worker_start,
        startup_grace_ms=args.native_startup_grace_ms,
        capture=args.capture,
    )
    rejected_independent = [
        route.name
        for route in routes
        if route.engine == "native" and not parity[route.name]["passed"]
    ]
    if rejected_independent:
        raise ValueError(
            "requested independent routes failed the full-project parity gate: "
            + ", ".join(rejected_independent)
        )
    if args.timing_route:
        if any(not parity[route.name]["passed"] for route in selected):
            raise ValueError("requested timing route failed the full-project parity gate")
        baseline = expected[selected[0].name]
        if any(expected[route.name] != baseline for route in selected[1:]):
            raise ValueError("requested timing routes differ in complete JUnit testcase multisets")
        allowed = selected
    result: dict[str, Any] = {
        "project": key,
        "cases": len(expected.get(Route("stock", 0).name, [])),
        "selectors": project.tests,
        "parity": parity,
        "status": "validated" if allowed else "blocked",
    }
    if allowed and not args.validate_only:
        result["timing"] = sample(
            project,
            root,
            snapshot,
            python,
            output / "timing",
            basetemp,
            allowed,
            expected,
            sampling,
            args.timeout,
            resources=args.resources,
            tree_resources=args.tree_resources,
            native_scheduling=args.native_scheduling,
            worker_collection=args.native_worker_collection,
            worker_start=args.native_worker_start,
            startup_grace_ms=args.native_startup_grace_ms,
            capture=args.capture,
            execution_expected={
                name: {**row["independent_execution"], "preflight_passed": row["passed"]}
                for name, row in parity.items()
                if row["independent_execution"] is not None
            },
            timed_evidence=args.timed_evidence,
        )
        result["status"] = "measured"
    serial.restore_project(root, snapshot)
    final = serial.interpreter_details(python, root, project_environment(project, root, python))
    if final != identity["installed"]:
        raise ValueError("installed runtime changed during benchmark execution")
    return result


def validate_startup_grace(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.override_upstream_capture and args.capture is None:
        parser.error("--override-upstream-capture requires --capture")
    if args.native_startup_grace_ms is not None:
        if not 0 <= args.native_startup_grace_ms <= 60_000:
            parser.error("--native-startup-grace-ms must be from 0 to 60000")
        if args.native_startup_grace_ms > 0 and not (
            args.native_scheduling == "demand"
            and args.native_worker_start == "fork"
            and not args.native_worker_collection
        ):
            parser.error("positive --native-startup-grace-ms requires demand and fork")


def parse_local_checkouts(
    parser: argparse.ArgumentParser, selected: list[str], specifications: list[str]
) -> dict[str, Path]:
    checkouts: dict[str, Path] = {}
    for specification in specifications:
        key, separator, path = specification.partition("=")
        if not separator or key not in selected or not path or key in checkouts:
            parser.error("--local-checkout requires one unique selected PROJECT=PATH")
        checkouts[key] = Path(path).absolute()
    return checkouts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--local-checkout",
        action="append",
        default=[],
        metavar="PROJECT=PATH",
        help="create a clean detached worktree from a pinned local project checkout",
    )
    parser.add_argument(
        "--project", choices=sorted(matrix.PROJECTS_BY_KEY), action="append", required=True
    )
    parser.add_argument("--workers", type=int, nargs="+", default=[0, 2, 4])
    parser.add_argument("--native-workers", type=int, nargs="*", default=[])
    parser.add_argument(
        "--independent-workers",
        type=int,
        nargs="+",
        default=[],
        help="enable independent-jN routes; compare j1 with serial pytest and jN with xdist N",
    )
    parser.add_argument(
        "--native-direct-workers",
        type=int,
        choices=(1,),
        nargs="*",
        default=[],
        help="enable native-direct-j1: pytest/result bridge without coverage or CLI history",
    )
    parser.add_argument(
        "--native-scheduling", choices=("file", "scope", "cases", "striped", "demand")
    )
    parser.add_argument("--capture", choices=("sys", "fd", "no"))
    parser.add_argument(
        "--override-upstream-capture",
        action="store_true",
        help="allow the explicit final --capture option to supersede project addopts",
    )
    parser.add_argument(
        "--rich-clock",
        action="store_true",
        help="append Rich logging-clock and FileProxy cleanup fixtures to tests/conftest.py",
    )
    parser.add_argument(
        "--timing-route",
        action="append",
        default=[],
        help="time only named routes, retaining their complete stock inventory preflights",
    )
    parser.add_argument("--python-version", default="3.12")
    parser.add_argument("--timeout", type=float, default=600)
    resource_options = parser.add_mutually_exclusive_group()
    resource_options.add_argument("--resources", action="store_true")
    resource_options.add_argument("--tree-resources", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--timed-evidence",
        choices=("full", "junit"),
        default="full",
        help="independent sample evidence; both modes require successful full preflights",
    )
    add_sampling_arguments(parser)
    parser.add_argument("--native-worker-collection", action="store_true")
    parser.add_argument("--native-worker-start", choices=("exec", "fork"))
    parser.add_argument("--native-startup-grace-ms", type=int)
    args = parser.parse_args()
    args.local_checkouts = parse_local_checkouts(parser, args.project, args.local_checkout)
    validate_startup_grace(parser, args)
    if args.native_worker_start == "fork" and args.native_worker_collection:
        parser.error("--native-worker-start=fork cannot use --native-worker-collection")
    if any(n < 0 for n in args.workers) or any(
        n < 1 for n in (*args.native_workers, *args.independent_workers)
    ):
        parser.error("xdist workers must be nonnegative; native workers must be positive")
    if any(
        len(values) != len(set(values))
        for values in (
            args.project,
            args.workers,
            args.native_workers,
            args.native_direct_workers,
            args.independent_workers,
            args.timing_route,
        )
    ):
        parser.error("project, worker and timing-route selections must be unique")
    available_routes = {
        Route(engine, count).name
        for count in {0, *args.workers}
        for engine in ("stock", "pytest-native")
    }
    available_routes.update(
        Route("pytest-native", count, False).name for count in args.native_workers
    )
    available_routes.update(
        Route("pytest-native", count, False, "direct").name for count in args.native_direct_workers
    )
    available_routes.update(
        Route("native", count, False).name for count in args.independent_workers
    )
    available_routes.update(
        Route("stock", 0 if count == 1 else count).name for count in args.independent_workers
    )
    if set(args.timing_route) - available_routes:
        parser.error(
            "enable timing routes with --workers, --native-workers, --native-direct-workers "
            "or --independent-workers"
        )
    if (args.resources or args.tree_resources) and sys.platform != "linux":
        parser.error("resource collection requires Linux")
    sampling = sampling_config(args)
    controller = require_clean(ROOT)
    for name in ("wheel", "build_manifest", "work_root", "output"):
        setattr(args, name, getattr(args, name).absolute())
    args.output.mkdir(parents=True, exist_ok=False)
    sources = {
        **SOURCE_INPUTS,
        **{
            str(lock.relative_to(ROOT)): file_sha256(lock)
            for key in args.project
            for lock in (
                matrix.PROJECTS_BY_KEY[key].dependency_lock
                or ROOT / "validation/pytest_runner/native-adoption-requirements.txt",
            )
        },
    }
    quota = Path("/sys/fs/cgroup/cpu.max")
    result: dict[str, Any] = {
        "controller": controller.as_dict(),
        "machine": hardware_details(),
        "cpu_max": quota.read_text().strip() if quota.is_file() else None,
        "sampling": sampling.as_dict(),
        "source_input_sha256": sources,
        "method": {
            "scope": "complete configured stock inventory; no test subsetting",
            "distribution": "load",
            "workers": sorted({0, *args.workers}),
            "native_workers": args.native_workers,
            "independent_workers": args.independent_workers,
            "timed_evidence": args.timed_evidence,
            "independent_per_sample_phase_validation": (
                bool(args.independent_workers) and args.timed_evidence == "full"
            ),
            "independent_results": (
                (
                    "requested independent.json inside every timed command; engine, complete "
                    "phase records, warnings and inventory rechecked against full preflight"
                    if args.timed_evidence == "full"
                    else "full phase/warning preflight required; timed commands retain "
                    "JUnit/status "
                    "parity with no explicit history or native-result JSON request; ordinary CLI "
                    "history and internal result behavior remain enabled; default project "
                    "history is transient and restored before each sample"
                )
                if args.independent_workers
                else None
            ),
            "native_direct_workers": args.native_direct_workers,
            "native_direct_command_contract": (
                (
                    "no-coverage pytest native plug-in and result bridge; identical configured "
                    "selectors/plugins/defaults/JUnit; no CLI selection/history control plane"
                )
                if args.native_direct_workers
                else None
            ),
            "native_direct_results": (
                (
                    "per-route direct-results.json from the existing native result observer; "
                    "retained in preflight and samples; no controller history publication"
                )
                if args.native_direct_workers
                else None
            ),
            "timing_routes": args.timing_route or None,
            "capture": args.capture,
            "override_upstream_capture": args.override_upstream_capture,
            "rich_clock": args.rich_clock,
            "local_checkouts": {key: str(path) for key, path in args.local_checkouts.items()},
            "native_environment": native_experiments(
                args.native_scheduling,
                args.native_worker_collection,
                args.native_worker_start,
                args.native_startup_grace_ms,
            ),
            "gate": (
                "complete JUnit testcase multisets including captures, diagnostics and "
                "properties; native engine/phase records and executed node IDs; not complete "
                "stock phase/session-warning parity"
                "; independent preflights additionally require exact stock phase records "
                "and project warnings; the verified framework marker-iterator deprecation "
                "compares its exact category/payload and public API origin, retaining raw "
                "diagnostic differences and receipts (case order compared as a multiset "
                "with multiple workers)"
            ),
            "timer": (
                "fresh child spawn/capture/drain/reap; excludes restoration, validation and "
                "resource collector startup"
            ),
            "rss": (
                "simultaneous child/descendant PSS sampled every 20 ms; collector excluded"
                if args.tree_resources
                else "largest process RSS, not aggregate simultaneous worker memory"
            ),
            "resource_method": (
                "linux-process-tree-smaps-rollup"
                if args.tree_resources
                else "linux-single-child-rusage"
                if args.resources
                else None
            ),
            "evidence": (
                "samples omit stock observer and requested native JSON; identical JUnit "
                "flags retained; native CLI retains ordinary history/results, native-direct "
                "retains the result observer and omits CLI selection/history; independent "
                "timed evidence follows the explicit timed_evidence mode"
            ),
            "cache": (
                "prepared snapshot restored before every child; source bytecode and pytest "
                "caches cold, environment and OS caches warmed"
            ),
        },
        "projects": [],
    }
    write_atomic(args.output / "inputs.json", result)
    for key in args.project:
        try:
            row = run_project(args, key, sampling)
        except Exception as error:  # noqa: BLE001 - retain blocked routes and partial raw evidence.
            row = {
                "project": key,
                "status": "blocked",
                "error": f"{type(error).__name__}: {error}",
                "artifacts": str(args.output / key),
            }
        result["projects"].append(row)
        for name, digest in sources.items():
            if file_sha256(ROOT / name) != digest:
                raise ValueError(f"source input changed during execution: {name}")
        write_atomic(args.output / "result.json", result)
        sys.stdout.write(f"{key}: {row['status']}\n")
        sys.stdout.flush()
    return 1 if any(row["status"] == "blocked" for row in result["projects"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
