r"""Validate and measure mutation execution against pinned mutmut ``benchmark_1k``.

This harness selects ``vfy mutate --engine=pytest-native``; the real-project harness
defaults to the independent engine. Publication mode compares the common
mutation catalog and statuses, verifies reported failing tests independently,
exercises cache states, and checks build identity before collecting three
warmups and thirty measured samples. ``--validate-only`` and
``--datasette-only`` run the semantic checks without timing and write
diagnostic evidence that cannot be used as a publication result.

mutmut maintains the benchmark project. Obtain the pinned revision before
running this script, for example::

    git clone https://github.com/boxed/mutmut.git /tmp/mutmut
    git -C /tmp/mutmut checkout cd2f73da310c3fc90cffcb3e6c768cdeac14e18c
    uv pip install --python .venv/bin/python -e /tmp/mutmut
    uv run python -m benchmarks run mutation \
      --project /tmp/mutmut/e2e_projects/benchmark_1k \
      --output target/benchmarks/mutation.json

    uv run python -m benchmarks validate mutation --datasette-only \
      --datasette-root /tmp/datasette \
      --datasette-python /tmp/datasette-environment/bin/python \
      --datasette-preparation-manifest /tmp/datasette-artifacts/manifest.json \
      --datasette-artifacts /tmp/datasette-artifacts \
      --output target/benchmarks/datasette-mutation.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from benchmarks.benchmark_harness import (
    BenchmarkMode,
    add_sampling_arguments,
    benchmark_environment,
    git_value,
    independent_uncertainty,
    run_checked,
    sampling_config,
    summarize,
    timed_call,
    timed_process,
)
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    require_unchanged_artifact,
)
from benchmarks.benchmark_resources import tree_resource_methodology, tree_resource_process
from benchmarks.benchmark_types import TimingSummary
from benchmarks.benchmark_workflows import workflow_environment
from benchmarks.datasette_mutation import (
    DATASETTE_SOURCE,
    DatasetteMutationFixture,
    prepare_datasette_fixture,
)
from benchmarks.observer_plugins import OUTCOME_PLUGIN, stage_plugins
from vfy.mutate import MutationReportRecord
from vfy.native import analyze_mutation_files, build_profile

MUTMUT_REPOSITORY = "https://github.com/boxed/mutmut"
MUTMUT_REVISION = "cd2f73da310c3fc90cffcb3e6c768cdeac14e18c"
REPO_ROOT = Path(__file__).resolve().parents[1]
MUTMUT_STATUS_BY_EXIT = {
    0: "survived",
    1: "killed",
    3: "killed",
    5: "no_tests",
    33: "no_tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    -24: "timeout",
    24: "timeout",
    152: "timeout",
    255: "timeout",
    37: "caught_by_type_check",
    2: "check_was_interrupted_by_user",
    -9: "segfault",
    -11: "segfault",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", type=Path, help="path to mutmut's pinned benchmark_1k project")
    parser.add_argument(
        "--datasette-only",
        action="store_true",
        help="validate the prepared scoped Datasette fixture without mutmut",
    )
    parser.add_argument(
        "--datasette-root", type=Path, help="path to the prepared Datasette project"
    )
    parser.add_argument(
        "--datasette-python",
        type=Path,
        help="Python interpreter for the prepared Datasette environment",
    )
    parser.add_argument(
        "--datasette-preparation-manifest",
        type=Path,
        help="manifest describing the prepared Datasette fixture",
    )
    parser.add_argument(
        "--datasette-artifacts",
        type=Path,
        help="directory containing prepared Datasette artifacts",
    )
    parser.add_argument(
        "--mutmut", default="mutmut", help="mutmut executable for the pinned benchmark"
    )
    parser.add_argument(
        "--mutmut-python",
        default=sys.executable,
        help="Python interpreter that can import the pinned mutmut revision",
    )
    parser.add_argument("--workers", type=int, default=10, help="workers for mutation commands")
    parser.add_argument(
        "--test-isolation",
        choices=("default", "serial", "file"),
        default="default",
        help="native vfy isolation selection; default omits the option and uses the native default",
    )
    add_sampling_arguments(parser, warmups=3, runs=30)
    parser.add_argument(
        "--tree-resources", action="store_true", help="sample Linux process-tree peak PSS"
    )
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--cold-only",
        action="store_true",
        help="diagnose cold common-catalog execution after parity, without cache/full-catalog runs",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="run parity and cache-state validation without timing samples",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="timeout in seconds for each child command"
    )
    parser.add_argument(
        "--timeout-policy",
        choices=("adaptive", "fixed"),
        help="override vfy's mutation timeout policy",
    )
    parser.add_argument(
        "--parity-runs", type=int, default=1, help="semantic parity repetitions before timing"
    )
    parser.add_argument("--output", type=Path, required=True, help="path for the JSON result")
    args = parser.parse_args()
    if args.tree_resources and sys.platform != "linux":
        parser.error("--tree-resources requires Linux")
    if not args.datasette_only and (
        args.project is None or not args.project.is_dir() or not (args.project / "src").is_dir()
    ):
        parser.error("set --project to mutmut's e2e_projects/benchmark_1k directory")
    if args.workers < 1 or args.warmups < 0 or args.runs < 1 or args.parity_runs < 1:
        parser.error("workers/runs must be positive; parity-runs must be at least one")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    datasette_values = (
        args.datasette_root,
        args.datasette_python,
        args.datasette_preparation_manifest,
        args.datasette_artifacts,
    )
    if any(value is not None for value in datasette_values) and not all(
        value is not None for value in datasette_values
    ):
        parser.error(
            "--datasette-root, --datasette-python, --datasette-preparation-manifest, "
            "and --datasette-artifacts must be supplied together"
        )
    if args.datasette_only and not all(value is not None for value in datasette_values):
        parser.error(
            "--datasette-only requires --datasette-root, --datasette-python, "
            "--datasette-preparation-manifest, and --datasette-artifacts"
        )
    if args.cold_only and (args.validate_only or args.datasette_only):
        parser.error("--cold-only cannot be combined with validation-only routes")
    if args.validate_only or args.diagnostic or args.datasette_only or args.cold_only:
        args.mode = BenchmarkMode.DIAGNOSTIC.value
    sampling_config(args)
    if args.mode == BenchmarkMode.PUBLICATION.value and args.runs < 30:
        parser.error("mutation publication benchmarks require at least 30 measured runs")
    return args


def _ignore(path: str, names: list[str]) -> set[str]:
    ignored = {
        ".git",
        ".venv",
        ".vfy",
        ".pytest_cache",
        "__pycache__",
        "mutants",
        ".mutmut-cache",
        ".coverage",
        ".coverage-vfy",
        ".coverage-vfy-wal",
        ".coverage-vfy-shm",
        ".coverage-vfy-journal",
        "benchmark_results.json",
    }
    # The benchmark baseline contains run-specific paths. Stock pytest killer
    # proofs consume source, tests, and configuration, independently of this data.
    return {
        name
        for name in names
        if name in ignored or name.startswith((".coverage.", ".coverage-vfy."))
    }


def copy_project(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=_ignore, symlinks=True)


def _prepare_test_selection(
    root: Path,
    pytest_args: Sequence[str],
    *,
    python: str,
    env: dict[str, str],
) -> tuple[Path, tuple[str, ...]]:
    """Expand a scoped pytest target for the cache-state validation oracle."""

    selection_value = env.get("VFY_SELECTED_TESTS")
    selection_path = (
        Path(selection_value)
        if selection_value
        else root / ".vfy" / "benchmark-selected-tests.json"
    )
    if selection_path.is_file():
        try:
            payload = json.loads(selection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"could not read selected pytest node ids: {selection_path}"
            ) from error
        if isinstance(payload, list):
            nodeids = tuple(item for item in payload if isinstance(item, str) and item)
            if len(nodeids) == len(payload):
                return selection_path, nodeids
        raise RuntimeError(f"selected pytest node ids are invalid: {selection_path}")

    if not pytest_args:
        raise RuntimeError("a scoped pytest target is required to prepare test selection")
    collection_env = dict(env)
    collection_env.pop("VFY_SELECTED_TESTS", None)
    collected = run_checked(
        [python, "-m", "pytest", "--collect-only", "-q", *pytest_args],
        cwd=root,
        env=collection_env,
        timeout=120,
    )
    prefix = next(
        (
            argument.split("::", 1)[0]
            for argument in pytest_args
            if "::" in argument and not argument.startswith("-")
        ),
        None,
    )
    nodeids: list[str] = []
    for line in collected.stdout.splitlines():
        value = line.strip()
        if value.startswith("::") and prefix is not None:
            value = prefix + value
        if "::" in value and not value.startswith(("=", "-", " ")):
            nodeids.append(value)
    if not nodeids:
        raise RuntimeError(f"scoped pytest target collected no node ids: {' '.join(pytest_args)}")
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_path.write_text(json.dumps(nodeids, sort_keys=True) + "\n", encoding="utf-8")
    return selection_path, tuple(nodeids)


def environment(root: Path, *, mutmut: bool = False) -> dict[str, str]:
    paths = [str(root / "src")]
    overrides = {
        "BENCHMARK_IMPORT_DELAY": "0.1",
        "BENCHMARK_CONFTEST_DELAY": "0.1",
        "BENCHMARK_TEST_DELAY": "0.05",
        "PYTHONPATH": os.pathsep.join(paths),
    }
    if mutmut:
        overrides["MUTMUT_BENCHMARK"] = "1"
    return benchmark_environment(overrides)


def resolve_command(value: str) -> str:
    """Resolve a command before subprocesses change their working directory."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        # Keep virtual-environment interpreter symlinks intact.  ``resolve``
        # would turn ``.venv/bin/python`` into the system Python and lose the
        # packages installed for the benchmark.
        return str(candidate.absolute()) if candidate.exists() else value
    found = shutil.which(value)
    if found:
        return found
    local_bin = "Scripts" if os.name == "nt" else "bin"
    bundled = REPO_ROOT / ".venv" / local_bin / value
    if bundled.is_file():
        return str(bundled)
    return value


def repository_state(path: Path) -> tuple[str, str, tuple[str, ...]]:
    """Return the enclosing Git revision, tree, and dirty status entries."""
    candidate = path if path.is_dir() else path.parent
    root: Path | None = None
    for parent in (candidate, *candidate.parents):
        if (parent / ".git").exists():
            root = parent
            break
    if root is None:
        raise RuntimeError(f"could not find a Git checkout containing {path}")

    revision = git_value(root, "rev-parse", "HEAD")
    tree = git_value(root, "rev-parse", "HEAD^{tree}")
    if revision is None or tree is None:
        raise RuntimeError(f"could not read Git revision for {path}")
    status = run_checked(["git", "status", "--porcelain"], cwd=root).stdout.rstrip("\n")
    return revision, tree, tuple(status.splitlines())


def package_versions(names: Sequence[str]) -> dict[str, str]:
    result = {}
    for name in names:
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "not installed"
    return result


def external_package_versions(python: str, names: Sequence[str]) -> dict[str, str]:
    script = r"""
import json
import sys
from importlib.metadata import PackageNotFoundError, version

result = {}
for name in json.loads(sys.argv[1]):
    try:
        result[name] = version(name)
    except PackageNotFoundError:
        result[name] = "not installed"
print(json.dumps(result, sort_keys=True))
"""
    completed = run_checked(
        [python, "-c", script, json.dumps(list(names))], cwd=REPO_ROOT, timeout=30
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or not all(
        isinstance(name, str) and isinstance(package_version, str)
        for name, package_version in value.items()
    ):
        raise RuntimeError("package version probe returned an invalid result")
    return {
        name: package_version
        for name, package_version in value.items()
        if isinstance(name, str) and isinstance(package_version, str)
    }


def run_timed(
    command: list[str],
    *,
    root: Path,
    env: dict[str, str],
    timeout: float | None = None,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> tuple[float, subprocess.CompletedProcess[str]]:
    elapsed, completed = timed_process(command, cwd=root, env=env, timeout=timeout)
    if completed.returncode not in allowed_returncodes:
        rendered = " ".join(command)
        detail = (completed.stderr or completed.stdout)[-8000:]
        raise RuntimeError(f"command failed ({completed.returncode}): {rendered}\n{detail}")
    return elapsed, completed


type CatalogKey = tuple[str, int, int, str, str]


class VfySample(MutationReportRecord):
    wall_ms: NotRequired[float]
    process: NotRequired[dict[str, object]]
    resources: NotRequired[dict[str, object]]


class CompactSample(TypedDict):
    wall_ms: float
    summary: dict[str, int]
    metrics: dict[str, float]
    resources: NotRequired[dict[str, object]]


class MutmutSummary(TypedDict):
    stats: dict[str, int]
    phase_timings: dict[str, float]
    outcomes: dict[str, str]


class MutmutSample(TypedDict):
    wall_ms: float
    summary: MutmutSummary
    process: NotRequired[dict[str, object]]
    resources: NotRequired[dict[str, object]]


class RatioSummary(TypedDict):
    samples: list[float]
    median: float
    mean: float
    lower_95_percent_bound: float | None
    upper_95_percent_bound: float | None
    uncertainty: dict[str, object]


_SEMANTIC_MUTATION_STATUSES = frozenset({"protected", "survived", "unreached"})
_SEMANTIC_MUTMUT_STATUSES = frozenset({"killed", "survived"})


class _CatalogScope(Enum):
    COMMON = "common"
    COMPLETE = "complete"


type VfySignature = tuple[tuple[CatalogKey, str, str | None], ...]
type MutmutSignature = tuple[tuple[str, str], ...]


def _mutmut_cached_scope_is_exact(
    mutmut_catalog_keys: set[CatalogKey], common_catalog: set[CatalogKey]
) -> bool:
    """Whether an unnamed mutmut run would cover exactly the compared catalog."""

    return mutmut_catalog_keys == common_catalog


def _mutation_catalog_key(item: Mapping[str, object], root: Path) -> CatalogKey:
    path = item.get("path")
    start = item.get("start")
    end = item.get("end")
    original = item.get("original")
    replacement = item.get("replacement")
    if (
        not isinstance(path, str)
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not isinstance(original, str)
        or not isinstance(replacement, str)
    ):
        raise RuntimeError("mutation report has an invalid identity record")
    try:
        relative = Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise RuntimeError(f"mutation report path escapes fixture root: {path}") from error
    return relative, start, end, original, replacement


def _validated_vfy_signature(
    report: VfySample,
    *,
    root: Path,
    expected_catalog: set[CatalogKey],
    label: str,
    scope: _CatalogScope = _CatalogScope.COMMON,
    full_suite_keys: frozenset[CatalogKey] = frozenset(),
    baseline_test_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[CatalogKey, str, str | None], ...]:
    """Validate one vfy result and return its stable semantic signature."""

    # The unmatched complete catalog can include nonterminating counterfactuals.
    # Keep these as explicit timeouts; they cannot enter the timed parity set.
    statuses = _SEMANTIC_MUTATION_STATUSES
    if scope is _CatalogScope.COMPLETE:
        statuses = statuses | {"timeout"}
    raw = report.get("mutations")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"{label} returned no mutation outcomes")
    seen: set[CatalogKey] = set()
    signature: list[tuple[CatalogKey, str, str | None]] = []
    counts: dict[str, int] = {}
    for value in raw:
        if not isinstance(value, dict):
            raise RuntimeError(f"{label} returned an invalid mutation outcome")
        key = _mutation_catalog_key(value, root)
        if key in seen:
            raise RuntimeError(f"{label} returned a duplicate mutation identity: {key!r}")
        seen.add(key)
        status = value.get("status")
        killer = value.get("killer")
        if not isinstance(status, str) or status not in statuses:
            raise RuntimeError(f"{label} returned an infrastructure mutation outcome")
        if status == "timeout" and value.get("returncode") is not None:
            raise RuntimeError(f"{label} timed-out mutation has a completed process exit status")
        if status == "protected":
            if not isinstance(killer, str) or not killer:
                raise RuntimeError(f"{label} protected mutation has no killer")
            tests = value.get("tests")
            if not isinstance(tests, list) or any(not isinstance(test, str) for test in tests):
                raise RuntimeError(f"{label} protected mutation has invalid selected tests")
            owner = value.get("owner")
            definition_scope = isinstance(owner, dict) and owner.get("phase") == "definition"
            # Definition-time mutations use the full requested scope. Callers
            # may also supply independently verified full-suite keys and the
            # complete baseline inventory for coverage-omitted sources.
            verified_full_suite = key in full_suite_keys and killer in baseline_test_ids
            if (tests and killer not in tests) or (
                not tests and not definition_scope and not verified_full_suite
            ):
                raise RuntimeError(
                    f"{label} protected mutation killer is not among its selected tests"
                )
            normalized_killer: str | None = killer
        else:
            if killer not in (None, ""):
                raise RuntimeError(f"{label} non-protected mutation has a killer")
            normalized_killer = None
        counts[status] = counts.get(status, 0) + 1
        signature.append((key, status, normalized_killer))
    if seen != expected_catalog:
        missing = sorted(expected_catalog - seen)
        extra = sorted(seen - expected_catalog)
        raise RuntimeError(
            f"{label} mutation catalog differs from the expected scope: "
            f"missing={missing[:3]!r}, extra={extra[:3]!r}"
        )
    _validate_mutation_summary(report.get("summary"), counts, statuses, label=label)
    return tuple(sorted(signature))


def _validate_mutation_summary(
    summary: object, counts: Mapping[str, int], statuses: frozenset[str], *, label: str
) -> None:
    if not isinstance(summary, dict):
        raise RuntimeError(f"{label} has no mutation summary")
    if any(key not in statuses for key in summary):
        raise RuntimeError(f"{label} summary contains an infrastructure outcome")
    normalized_summary = {status: summary.get(status, 0) for status in statuses}
    expected_summary = {status: counts.get(status, 0) for status in statuses}
    if normalized_summary != expected_summary or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in normalized_summary.values()
    ):
        raise RuntimeError(f"{label} summary disagrees with mutation outcomes")


def _validated_mutmut_signature(
    sample: MutmutSample, *, expected_names: Sequence[str], label: str
) -> MutmutSignature:
    summary = sample.get("summary")
    if not isinstance(summary, dict):
        raise RuntimeError(f"{label} has no mutmut summary")
    raw = summary.get("outcomes")
    if not isinstance(raw, dict) or set(raw) != set(expected_names):
        raise RuntimeError(f"{label} mutmut identities differ from the expected scope")
    signature: list[tuple[str, str]] = []
    for name in expected_names:
        status = raw.get(name)
        if not isinstance(status, str) or status not in _SEMANTIC_MUTMUT_STATUSES:
            raise RuntimeError(f"{label} returned an infrastructure mutmut outcome")
        signature.append((name, status))
    stats = summary.get("stats")
    if not isinstance(stats, dict) or stats.get("total") != len(expected_names):
        raise RuntimeError(f"{label} mutmut summary has an invalid count")
    return tuple(signature)


def _validate_state_signatures(
    reports: Sequence[tuple[str, VfySample]],
    *,
    root: Path,
    expected_catalog: set[CatalogKey],
    killer_proofs: _KillerProofs | None = None,
) -> VfySignature:
    signatures = [
        (
            label,
            _validated_vfy_signature(
                report, root=root, expected_catalog=expected_catalog, label=label
            ),
        )
        for label, report in reports
    ]
    first = signatures[0][1]
    if killer_proofs is not None:
        killer_proofs.set_reference_report(reports[0][1], root)
    for (label, report), (_, signature) in zip(reports[1:], signatures[1:], strict=True):
        if signature == first:
            continue
        if label != "invalidated":
            raise RuntimeError(f"{label} semantic mutation result differs from baseline state")
        _require_signature_match(
            first,
            signature,
            report,
            root=root,
            label=label,
            killer_proofs=killer_proofs,
        )
    return first


def _require_signature_match(
    expected: VfySignature,
    actual: VfySignature,
    report: VfySample,
    *,
    root: Path,
    label: str,
    killer_proofs: _KillerProofs | None,
) -> None:
    """Require matching mutation identities and statuses; verify changes in failing tests."""

    if len(actual) != len(expected):
        raise RuntimeError(f"{label} semantic mutation result differs from its reference")
    rerankable = _selected_body_mutations(report, root)
    for reference, current in zip(expected, actual, strict=True):
        reference_key, reference_status, reference_killer = reference
        key, status, killer = current
        if (key, status) != (reference_key, reference_status):
            raise RuntimeError(f"{label} semantic mutation result differs from its reference")
        if killer == reference_killer:
            continue
        mutation = rerankable.get(key)
        if (
            status != "protected"
            or mutation is None
            or not isinstance(reference_killer, str)
            or not isinstance(killer, str)
            or killer_proofs is None
            or not killer_proofs.is_reference_body_mutation(key, mutation)
        ):
            raise RuntimeError(f"{label} semantic mutation result differs from its reference")
        killer_proofs.prove(
            root,
            mutation,
            (reference_killer, killer),
            label=label,
        )


def _selected_body_mutations(
    report: VfySample, root: Path
) -> dict[CatalogKey, Mapping[str, object]]:
    """Return protected context-selected body mutation records."""

    raw = report.get("mutations")
    if not isinstance(raw, list):
        return {}
    selected: dict[CatalogKey, Mapping[str, object]] = {}
    for value in raw:
        if not isinstance(value, dict) or value.get("status") != "protected":
            continue
        owner = value.get("owner")
        tests = value.get("tests")
        if (
            isinstance(owner, dict)
            and owner.get("phase") == "body"
            and isinstance(tests, list)
            and tests
        ):
            selected[_mutation_catalog_key(value, root)] = value
    return selected


class _KillerProofs:
    """Verify that each reported failing test detects the mutation, outside timing."""

    def __init__(
        self,
        evidence_dir: Path,
        *,
        python: str,
        timeout: float,
        test_environment: Callable[[Path], dict[str, str]] | None = None,
        pytest_args: Sequence[str] = (),
    ) -> None:
        self.evidence_dir = evidence_dir.resolve()
        self.python = python
        self.timeout = timeout
        self.test_environment = test_environment
        self.pytest_args = tuple(pytest_args)
        self._cache: dict[tuple[CatalogKey, str, str, str], dict[str, object]] = {}
        self._reference_body_sources: dict[CatalogKey, str] = {}

    def set_reference_report(self, report: VfySample, root: Path) -> None:
        """Save source hashes for the reference report's function-body mutations."""

        self._reference_body_sources = {}
        for key, mutation in _selected_body_mutations(report, root).items():
            source_hash = mutation.get("source_hash")
            if isinstance(source_hash, str):
                self._reference_body_sources[key] = source_hash

    def is_reference_body_mutation(self, key: CatalogKey, current: Mapping[str, object]) -> bool:
        """Return whether this mutation retains its reference function-body source hash."""

        return self._reference_body_sources.get(key) == current.get("source_hash")

    def prove(
        self,
        root: Path,
        mutation: Mapping[str, object],
        killers: Sequence[str],
        *,
        label: str,
        full_suite_tests: frozenset[str] = frozenset(),
    ) -> None:
        """Require every distinct claimed killer to fail this exact mutant."""

        if self.evidence_dir.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("killer proof evidence must be outside the fixture project")
        key = _mutation_catalog_key(mutation, root)
        owner = mutation.get("owner")
        tests = mutation.get("tests")
        source_hash = mutation.get("source_hash")
        if (
            not isinstance(owner, dict)
            or owner.get("phase") not in {"body", "definition"}
            or (owner.get("phase") == "definition" and not full_suite_tests)
            or not isinstance(tests, list)
            or (not tests and not full_suite_tests)
            or any(not isinstance(test, str) for test in tests)
            or not isinstance(source_hash, str)
            or len(source_hash) != 64
        ):
            raise RuntimeError("killer proof requires a selected or verified full-suite mutation")
        unresolved_source = root / key[0]
        if unresolved_source.is_symlink():
            raise RuntimeError(f"killer proof source is a symlink: {key[0]}")
        source_path = unresolved_source.resolve()
        if not source_path.is_relative_to(root.resolve()):
            raise RuntimeError(f"killer proof source escapes fixture root: {key[0]}")
        source_bytes = source_path.read_bytes()
        actual_hash = hashlib.sha256(source_bytes).hexdigest()
        if actual_hash != source_hash:
            raise RuntimeError(
                f"killer proof source hash changed for {key[0]}: "
                f"report={source_hash}, current={actual_hash}"
            )
        start, end, original, replacement = key[1:]
        original_bytes = original.encode("utf-8")
        if source_bytes[start:end] != original_bytes:
            raise RuntimeError(f"killer proof mutation span changed for {key[0]}:{start}:{end}")
        mutated_bytes = source_bytes[:start] + replacement.encode("utf-8") + source_bytes[end:]
        fixture_digest = _fixture_proof_digest(root)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        for killer in dict.fromkeys(killers):
            if not isinstance(killer, str) or not killer:
                raise RuntimeError("killer proof received an empty pytest node id")
            if not tests and killer not in full_suite_tests:
                raise RuntimeError("full-suite killer is not in the verified baseline inventory")
            cache_key = (key, source_hash, fixture_digest, killer)
            if cache_key in self._cache:
                continue
            self._prove_one(
                root,
                key,
                source_hash,
                fixture_digest,
                tests,
                source_bytes,
                mutated_bytes,
                killer,
                label=label,
            )

    def _prove_one(
        self,
        source_root: Path,
        key: CatalogKey,
        source_hash: str,
        fixture_digest: str,
        selected_tests: Sequence[str],
        source_bytes: bytes,
        mutated_bytes: bytes,
        killer: str,
        *,
        label: str,
    ) -> None:
        cache_key = (key, source_hash, fixture_digest, killer)
        proof_id = hashlib.sha256(
            json.dumps([key, source_hash, fixture_digest, killer], sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        proof_dir = self.evidence_dir / proof_id
        proof_dir.mkdir(parents=True, exist_ok=True)
        (proof_dir / "original-source.py").write_bytes(source_bytes)
        (proof_dir / "mutated-source.py").write_bytes(mutated_bytes)
        manifest: dict[str, object] = {
            "identity": list(key),
            "source_hash": source_hash,
            "fixture_digest": fixture_digest,
            "killer": killer,
            "selected_tests": list(selected_tests),
            "first_observed_during": label,
            "python": self.python,
            "pytest_args": list(self.pytest_args),
        }
        try:
            with tempfile.TemporaryDirectory(prefix="vfy-killer-proof-") as temporary:
                temporary_root = Path(temporary)
                clean_root = temporary_root / "clean"
                mutant_root = temporary_root / "mutant"
                copy_project(source_root, clean_root)
                copy_project(source_root, mutant_root)
                relative_source = Path(key[0])
                clean_source = clean_root / relative_source
                mutant_source = mutant_root / relative_source
                if clean_source.read_bytes() != source_bytes:
                    raise RuntimeError("clean killer proof copy changed source bytes")
                if mutant_source.is_symlink():
                    raise RuntimeError("mutant killer proof source is a symlink")
                mutant_source.write_bytes(mutated_bytes)
                clean = self._run_test(clean_root, killer, proof_dir / "clean")
                mutant = self._run_test(mutant_root, killer, proof_dir / "mutant")
            manifest["clean"] = clean
            manifest["mutant"] = mutant
            clean_ok = _killer_proof_outcomes(clean, killer, mutant=False)
            mutant_ok = _killer_proof_outcomes(mutant, killer, mutant=True)
            manifest["valid"] = clean_ok and mutant_ok
            if not clean_ok or not mutant_ok:
                raise RuntimeError(f"killer proof failed for {killer!r}; evidence: {proof_dir}")
            self._cache[cache_key] = manifest
        except Exception as error:
            manifest["valid"] = False
            manifest["error"] = str(error)
            raise
        finally:
            (proof_dir / "proof.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    def _run_test(self, root: Path, nodeid: str, evidence_dir: Path) -> dict[str, object]:
        root = root.resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        outcomes_path = evidence_dir / "outcomes.jsonl"
        outcomes_path.unlink(missing_ok=True)
        env = environment(root) if self.test_environment is None else self.test_environment(root)
        plugin_path = str(stage_plugins(evidence_dir / "observer-plugins"))
        python_paths = [plugin_path, str(root), str(root / "src")]
        existing_path = env.get("PYTHONPATH")
        if existing_path:
            python_paths.extend(existing_path.split(os.pathsep))
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("VFY_BENCHMARK_OWNER_PID", None)
        env["VFY_BENCHMARK_OUTCOMES"] = str(outcomes_path)
        command = [
            self.python,
            "-m",
            "pytest",
            "--rootdir",
            str(root),
            "-p",
            OUTCOME_PLUGIN,
            "-q",
            *self.pytest_args,
            nodeid,
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
            returncode: int | None = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            returncode = None
            stdout = _subprocess_output_text(error.stdout)
            stderr = _subprocess_output_text(error.stderr)
        try:
            outcomes = [
                json.loads(line)
                for line in outcomes_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as error:
            outcomes = []
            stderr += f"\nCould not read outcome evidence: {error}"
        (evidence_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (evidence_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
        (evidence_dir / "parsed-outcomes.json").write_text(
            json.dumps(outcomes, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {
            "returncode": returncode,
            "command": command,
            "cwd": str(root),
            "outcomes": outcomes,
            "stdout": "stdout.txt",
            "stderr": "stderr.txt",
            "outcomes_file": "parsed-outcomes.json",
        }

    def records(self) -> list[dict[str, object]]:
        """Return proof summaries with paths to retained evidence."""

        records: list[dict[str, object]] = []
        for (key, source_hash, fixture_digest, killer), manifest in sorted(self._cache.items()):
            proof_id = hashlib.sha256(
                json.dumps([key, source_hash, fixture_digest, killer], sort_keys=True).encode(
                    "utf-8"
                )
            ).hexdigest()[:20]
            records.append(
                {
                    "identity": list(key),
                    "source_hash": source_hash,
                    "fixture_digest": fixture_digest,
                    "killer": killer,
                    "proof": str(self.evidence_dir.resolve() / proof_id / "proof.json"),
                    "valid": manifest.get("valid") is True,
                }
            )
        return records


def _subprocess_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _fixture_proof_digest(root: Path) -> str:
    """Fingerprint fixture inputs while excluding generated benchmark state."""

    digest = hashlib.sha256()
    resolved_root = root.resolve()

    def record_symlink(path: Path) -> None:
        target = path.resolve(strict=True)
        if not target.is_relative_to(resolved_root):
            raise RuntimeError(f"killer proof fixture contains an escaping symlink: {path}")
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0symlink\0")
        digest.update(os.readlink(path).encode("utf-8"))
        digest.update(b"\0")

    for directory, names, filenames in os.walk(root, followlinks=False):
        ignored = _ignore(directory, [*names, *filenames])
        names[:] = sorted(name for name in names if name not in ignored)
        base = Path(directory)
        for name in sorted(filenames):
            if name in ignored:
                continue
            path = base / name
            if path.is_symlink():
                record_symlink(path)
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            if path.is_file():
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
            digest.update(b"\0")
        for name in names:
            if (base / name).is_symlink():
                record_symlink(base / name)
    return digest.hexdigest()


def _killer_proof_outcomes(result: Mapping[str, object], nodeid: str, *, mutant: bool) -> bool:
    outcomes = result.get("outcomes")
    if not isinstance(outcomes, list) or any(not isinstance(row, dict) for row in outcomes):
        return False
    if any(row.get("nodeid") != nodeid for row in outcomes):
        return False
    by_phase = {row.get("when"): row.get("outcome") for row in outcomes}
    if len(outcomes) != 3 or set(by_phase) != {"setup", "call", "teardown"}:
        return False
    if result.get("returncode") != (1 if mutant else 0):
        return False
    expected = {"setup": "passed", "call": "failed" if mutant else "passed", "teardown": "passed"}
    return by_phase == expected


def _validate_parity_repeat(
    expected_fixture: Mapping[str, object],
    expected_vfy: VfySignature,
    expected_mutmut: MutmutSignature,
    fixture: Mapping[str, object],
    vfy: VfySignature,
    mutmut: MutmutSignature,
    *,
    label: str,
    root: Path,
    report: VfySample,
    killer_proofs: _KillerProofs | None,
) -> None:
    """Require every parity repetition to match the first independent oracle."""

    if fixture != expected_fixture:
        raise RuntimeError(f"{label} fixture parity differs from the first parity run")
    if vfy != expected_vfy:
        _require_signature_match(
            expected_vfy,
            vfy,
            report,
            root=root,
            label=label,
            killer_proofs=killer_proofs,
        )
    if mutmut != expected_mutmut:
        raise RuntimeError(f"{label} mutmut parity differs from the first parity run")


def _write_sample_evidence(
    evidence_dir: Path | None, label: str, value: object, *, error: str | None = None
) -> None:
    if evidence_dir is None:
        return
    evidence_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"label": label, "value": value}
    if error is not None:
        payload["error"] = error
    (evidence_dir / f"{label}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sample_vfy(
    root: Path,
    args: argparse.Namespace,
    common_catalog: set[CatalogKey],
    *,
    label: str,
    expected_signature: VfySignature | None,
    evidence_dir: Path | None,
    killer_proofs: _KillerProofs | None = None,
    allow_body_rerank: bool = False,
    clear_cache: bool = True,
) -> tuple[VfySample, VfySignature]:
    try:
        report = measured_vfy(
            root,
            args,
            common_catalog,
            clear_cache=clear_cache,
            resource_path=(
                args.output.with_suffix(".artifacts") / "resources" / f"{label}.json"
                if getattr(args, "tree_resources", False)
                else None
            ),
        )
    except Exception as error:
        _write_sample_evidence(evidence_dir, label, {}, error=str(error))
        raise
    try:
        signature = _validated_vfy_signature(
            report, root=root, expected_catalog=common_catalog, label=label
        )
        if expected_signature is not None and signature != expected_signature:
            if not allow_body_rerank:
                raise RuntimeError(f"{label} semantic result differs from the pre-timing parity")
            _require_signature_match(
                expected_signature,
                signature,
                report,
                root=root,
                label=label,
                killer_proofs=killer_proofs,
            )
    except Exception as error:
        _write_sample_evidence(evidence_dir, label + ".failure", report, error=str(error))
        raise
    _write_sample_evidence(
        evidence_dir,
        label,
        {
            "wall_ms": float(report.get("wall_ms", 0.0)),
            "summary": report.get("summary", {}),
            "metrics": report.get("metrics", {}),
            **({"resources": report["resources"]} if "resources" in report else {}),
            "mutation_status_counts": {
                status: sum(1 for _key, current, _killer in signature if current == status)
                for status in sorted(_SEMANTIC_MUTATION_STATUSES)
            },
        },
    )
    return report, signature


def _sample_mutmut(
    root: Path,
    args: argparse.Namespace,
    mutmut_names: Sequence[str],
    *,
    label: str,
    expected_signature: MutmutSignature | None,
    evidence_dir: Path | None,
) -> tuple[MutmutSample, MutmutSignature]:
    try:
        sample = measured_mutmut(
            root,
            args,
            mutmut_names,
            resource_path=(
                args.output.with_suffix(".artifacts") / "resources" / f"{label}.json"
                if getattr(args, "tree_resources", False)
                else None
            ),
        )
    except Exception as error:
        _write_sample_evidence(evidence_dir, label, {}, error=str(error))
        raise
    try:
        signature = _validated_mutmut_signature(sample, expected_names=mutmut_names, label=label)
        if expected_signature is not None and signature != expected_signature:
            raise RuntimeError(f"{label} semantic result differs from the pre-timing parity")
    except Exception as error:
        _write_sample_evidence(evidence_dir, label + ".failure", sample, error=str(error))
        raise
    statuses = [status for _name, status in signature]
    _write_sample_evidence(
        evidence_dir,
        label,
        {
            "wall_ms": float(sample.get("wall_ms", 0.0)),
            **({"resources": sample["resources"]} if "resources" in sample else {}),
            "summary": {
                "stats": sample.get("summary", {}).get("stats", {}),
                "phase_timings": sample.get("summary", {}).get("phase_timings", {}),
            },
            "mutation_status_counts": {
                status: statuses.count(status) for status in sorted(set(statuses))
            },
        },
    )
    return sample, signature


def json_report(completed: subprocess.CompletedProcess[str], label: str) -> VfySample:
    """Decode the mutation JSON object; include stderr when decoding fails."""
    payload = completed.stdout.strip()
    if not payload:
        detail = (completed.stderr or "no stderr output").strip()
        raise RuntimeError(f"{label} returned no JSON report: {detail[-8000:]}")
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        detail = (completed.stderr or "").strip()
        suffix = f"; stderr: {detail[-2000:]}" if detail else ""
        raise RuntimeError(f"{label} returned invalid JSON: {error}{suffix}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned JSON {type(value).__name__}, expected an object")
    return cast(VfySample, value)


def vfy_command(*arguments: str, python: str | None = None) -> list[str]:
    return [python or sys.executable, "-m", "vfy", *arguments]


def _test_isolation_arguments(args: argparse.Namespace) -> tuple[str, ...]:
    isolation = str(getattr(args, "test_isolation", "default"))
    if isolation == "default":
        return ()
    if isolation not in {"serial", "file"}:
        raise ValueError(f"unsupported benchmark test isolation: {isolation}")
    return "--test-isolation", isolation


def _timeout_policy_arguments(args: argparse.Namespace) -> tuple[str, ...]:
    policy = getattr(args, "timeout_policy", None)
    return () if policy is None else ("--timeout-policy", str(policy))


def vfy_catalog(
    root: Path,
    source_paths: Sequence[Path] | None = None,
) -> tuple[set[CatalogKey], float, dict[CatalogKey, str]]:
    # Time catalog generation separately from the mutation command, using the
    # loaded release extension without another interpreter startup.

    paths = sorted(
        str(path.resolve()) for path in (source_paths or tuple((root / "src").rglob("*.py")))
    )
    elapsed, records = timed_call(lambda: analyze_mutation_files(paths))
    catalog: set[CatalogKey] = set()
    ids: dict[CatalogKey, str] = {}
    for record in records:
        key = (
            Path(record[1]).resolve().relative_to(root.resolve()).as_posix(),
            int(record[7]),
            int(record[8]),
            str(record[5]),
            str(record[6]),
        )
        catalog.add(key)
        ids[key] = str(record[0])
    return catalog, elapsed, ids


def mutmut_catalog(
    root: Path, python: str, source_paths: Sequence[Path] | None = None
) -> tuple[set[CatalogKey], float, dict[CatalogKey, str]]:
    """Ask mutmut's own CST visitor for exact source spans and replacements."""
    script = r"""
import json
import sys
from pathlib import Path
import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider
from mutmut.mutation.file_mutation import create_mutations
from mutmut.mutation.file_mutation import group_by_top_level_node
from mutmut.mutation.trampoline_templates import mangle_function_name
from mutmut.utils.format_utils import get_mutant_name

def offsets(text):
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line.encode("utf-8")))
    return starts

def byte_offset(starts, line, column, text):
    prefix = text.splitlines(keepends=True)[line - 1][:column]
    return starts[line - 1] + len(prefix.encode("utf-8"))

result = {}
paths = [Path(path) for path in json.loads(sys.argv[1])]
codegen = cst.Module([])
for path in sorted(paths):
    source = path.read_text(encoding="utf-8")
    module, mutations, _classes, _functions = create_mutations(str(path), source)
    positions = MetadataWrapper(module, unsafe_skip_copy=True).resolve(PositionProvider)
    starts = offsets(source)
    groups = group_by_top_level_node(mutations)

    def record(body, class_name=None):
        for statement in body:
            if isinstance(statement, cst.FunctionDef):
                grouped = groups.get(statement, ())
                if not grouped:
                    continue
                base = mangle_function_name(name=statement.name.value, class_name=class_name)
                for index, mutation in enumerate(grouped, 1):
                    original = mutation.original_node
                    replacement = mutation.mutated_node
                    original_position = positions[original]
                    start = byte_offset(
                        starts, original_position.start.line, original_position.start.column, source
                    )
                    end = byte_offset(
                        starts, original_position.end.line, original_position.end.column, source
                    )
                    key = (
                        path.as_posix(),
                        start,
                        end,
                        codegen.code_for_node(original),
                        codegen.code_for_node(replacement),
                    )
                    result[key] = get_mutant_name(path, f"{base}__mutmut_{index}")
            elif isinstance(statement, cst.ClassDef) and isinstance(
                statement.body, cst.IndentedBlock
            ):
                nested = (
                    statement.name.value
                    if not class_name
                    else f"{class_name}.{statement.name.value}"
                )
                record(statement.body.body, nested)

    record(module.body)
print(
    json.dumps(
        {"catalog": sorted(result), "names": [[list(key), name] for key, name in result.items()]}
    )
)
"""
    elapsed, completed = run_timed(
        [
            python,
            "-c",
            script,
            json.dumps(
                [
                    path.resolve().relative_to(root.resolve()).as_posix()
                    for path in (source_paths or tuple((root / "src").rglob("*.py")))
                ]
            ),
        ],
        root=root,
        env=environment(root, mutmut=True),
        timeout=120,
    )
    value = json.loads(completed.stdout)
    # The pinned visitor emits exact five-field source-span keys.
    catalog = {cast(CatalogKey, tuple(item)) for item in value["catalog"]}
    names = {cast(CatalogKey, tuple(item)): str(name) for item, name in value["names"]}
    return catalog, elapsed, names


def measured_vfy(
    root: Path,
    args: argparse.Namespace,
    common_catalog: set[CatalogKey] | None = None,
    *,
    clear_cache: bool = True,
    source_paths: Sequence[Path] | None = None,
    pytest_args: Sequence[str] = (),
    python: str | None = None,
    child_environment: dict[str, str] | None = None,
    retain_process: bool = False,
    resource_path: Path | None = None,
) -> VfySample:
    # mutmut copies tests into mutants/. Remove its generated artifacts before
    # vfy collects both conftests and raises ImportPathMismatchError.
    for name in ("mutants", ".mutmut-cache"):
        artifact = root / name
        if artifact.is_dir():
            shutil.rmtree(artifact)
        elif artifact.exists():
            artifact.unlink()
    cache = root / ".vfy" / "mutations.json"
    if clear_cache:
        cache.unlink(missing_ok=True)
    env = environment(root) if child_environment is None else dict(child_environment)
    source_arguments = source_paths
    source_cli: list[str] = []
    if source_arguments is None:
        source_cli.extend(("--source", "src"))
    else:
        for source in source_arguments:
            source_cli.extend(("--source", str(source)))
    if common_catalog is not None:
        _catalog, _elapsed, ids = vfy_catalog(root, source_arguments)
        missing = common_catalog.difference(ids)
        if missing:
            raise RuntimeError(f"copied project lost common catalog entries: {sorted(missing)[:3]}")
        env["VFY_MUTATION_KEYS"] = json.dumps(sorted(ids[key] for key in common_catalog))
    command = vfy_command(
        "mutate",
        f"--engine={getattr(args, 'vfy_route', 'pytest-native')}",
        "--all",
        *source_cli,
        "--data-file",
        ".coverage-vfy",
        "--format",
        "json",
        "-j",
        str(args.workers),
        *_test_isolation_arguments(args),
        *_timeout_policy_arguments(args),
        "--timeout",
        str(args.timeout),
        "-x",
        "-q",
        *pytest_args,
        python=python or sys.executable,
    )
    resources = None
    # Mutation may return a nonzero status with a valid structured outcome;
    # the semantic gate below still rejects infrastructure outcomes as kills.
    if resource_path is None:
        elapsed, completed = run_timed(
            command, root=root, env=env, timeout=600, allowed_returncodes=frozenset({0, 1, 2})
        )
    else:
        elapsed, completed, resources = tree_resource_process(
            command, cwd=root, env=env, timeout=600, record_path=resource_path
        )
        if completed.returncode not in {0, 1, 2}:
            raise RuntimeError(f"vfy mutate failed ({completed.returncode}): {completed.stderr}")
    report = json_report(completed, "vfy mutate")
    report["wall_ms"] = elapsed
    if resources is not None:
        report["resources"] = resources
    if retain_process:
        report["process"] = {
            "command": command,
            "cwd": str(root),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "environment": {
                key: env.get(key)
                for key in (
                    "PYTHONPATH",
                    "PYTEST_PLUGINS",
                    "PYTEST_ADDOPTS",
                    "VFY_SELECTED_TESTS",
                    "VFY_MUTATION_KEYS",
                )
            },
        }
    return report


def parity(
    root: Path,
    args: argparse.Namespace,
    common_catalog: set[CatalogKey],
    mutmut_names_by_key: dict[CatalogKey, str],
) -> tuple[dict[str, object], VfySignature, MutmutSignature, VfySample]:
    fast = measured_vfy(root, args, common_catalog)
    fast_signature = _validated_vfy_signature(
        fast, root=root, expected_catalog=common_catalog, label="fast parity"
    )
    # A portable run gets its own data/cache and cannot reuse fast results.
    (root / ".vfy" / "mutations.json").unlink(missing_ok=True)
    env = environment(root)
    env["VFY_MUTATION_ENGINE"] = "portable"
    _catalog, _elapsed, ids = vfy_catalog(root)
    env["VFY_MUTATION_KEYS"] = json.dumps(sorted(ids[key] for key in common_catalog))
    _, completed = run_timed(
        vfy_command(
            "mutate",
            "--engine=pytest-native",
            "--all",
            "--source",
            "src",
            "--data-file",
            ".coverage-vfy",
            "--format",
            "json",
            "--no-cache",
            "-j",
            str(args.workers),
            *_test_isolation_arguments(args),
            *_timeout_policy_arguments(args),
            "--timeout",
            str(args.timeout),
            "-x",
            "-q",
        ),
        root=root,
        env=env,
        timeout=1200,
        allowed_returncodes=frozenset({0, 1, 2}),
    )
    portable = json_report(completed, "portable vfy mutate")
    portable_signature = _validated_vfy_signature(
        portable, root=root, expected_catalog=common_catalog, label="portable parity"
    )
    if portable_signature != fast_signature:
        raise RuntimeError("portable parity semantic result differs from fast parity")
    fast_keys = {
        (item.get("id"), item.get("status"), item.get("killer"))
        for item in fast.get("mutations", [])
    }
    portable_keys = {
        (item.get("id"), item.get("status"), item.get("killer"))
        for item in portable.get("mutations", [])
    }
    if fast_keys != portable_keys:
        only_fast = sorted(fast_keys - portable_keys)[:3]
        only_portable = sorted(portable_keys - fast_keys)[:3]
        raise RuntimeError(
            f"fast/portable parity failed: only-fast={only_fast!r}, only-portable={only_portable!r}"
        )
    _catalog, _elapsed, vfy_ids = vfy_catalog(root)
    mutmut = measured_mutmut(
        root,
        args,
        [mutmut_names_by_key[key] for key in sorted(common_catalog)],
    )
    mutmut_signature = _validated_mutmut_signature(
        mutmut,
        expected_names=[mutmut_names_by_key[key] for key in sorted(common_catalog)],
        label="parity mutmut",
    )
    return (
        compare_fixture_outcomes(
            fast,
            mutmut,
            common_catalog=common_catalog,
            vfy_ids=vfy_ids,
            mutmut_names=mutmut_names_by_key,
        ),
        fast_signature,
        mutmut_signature,
        fast,
    )


def run_fixture_parity(
    project: Path,
    args: argparse.Namespace,
    common_catalog: set[CatalogKey],
    mutmut_names_by_key: dict[CatalogKey, str],
    *,
    killer_proofs: _KillerProofs | None = None,
) -> tuple[dict[str, object], VfySignature, MutmutSignature]:
    """Run parity repetitions against one stable pre-timing oracle."""

    fixture_parity: dict[str, object] | None = None
    vfy_signature: VfySignature | None = None
    mutmut_signature: MutmutSignature | None = None
    for parity_index in range(args.parity_runs):
        with tempfile.TemporaryDirectory(prefix="vfy-mutation-parity-") as temporary:
            parity_root = Path(temporary) / "project"
            copy_project(project, parity_root)
            current_fixture, current_vfy, current_mutmut, current_report = parity(
                parity_root,
                args,
                common_catalog,
                mutmut_names_by_key,
            )
            if fixture_parity is None:
                fixture_parity = current_fixture
                vfy_signature = current_vfy
                mutmut_signature = current_mutmut
                if killer_proofs is not None:
                    killer_proofs.set_reference_report(current_report, parity_root)
            else:
                if vfy_signature is None or mutmut_signature is None:
                    raise RuntimeError("mutation parity lost its first-run signatures")
                _validate_parity_repeat(
                    fixture_parity,
                    vfy_signature,
                    mutmut_signature,
                    current_fixture,
                    current_vfy,
                    current_mutmut,
                    label=f"parity run {parity_index + 1}",
                    root=parity_root,
                    report=current_report,
                    killer_proofs=killer_proofs,
                )
    if fixture_parity is None or vfy_signature is None or mutmut_signature is None:
        raise RuntimeError("mutation parity did not run")
    return fixture_parity, vfy_signature, mutmut_signature


def measured_mutmut(
    root: Path,
    args: argparse.Namespace,
    mutmut_names: Sequence[str],
    *,
    child_environment: dict[str, str] | None = None,
    retain_process: bool = False,
    resource_path: Path | None = None,
) -> MutmutSample:
    for name in ("mutants", ".mutmut-cache"):
        path = root / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    command = [args.mutmut, "run", "--max-children", str(args.workers), *mutmut_names]
    run_environment = (
        environment(root, mutmut=True) if child_environment is None else child_environment
    )
    resources = None
    if resource_path is None:
        elapsed, completed = run_timed(command, root=root, env=run_environment, timeout=1200)
    else:
        elapsed, completed, resources = tree_resource_process(
            command, cwd=root, env=run_environment, timeout=1200, record_path=resource_path
        )
        if completed.returncode:
            raise RuntimeError(f"mutmut failed ({completed.returncode}): {completed.stderr}")
    summary_path = root / "mutants" / "mutmut-stats.json"
    if not summary_path.is_file():
        summary_path = root / "mutants" / "summary.json"
    raw_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    exit_codes: dict[str, int | None] = {}
    for path in (root / "mutants").rglob("*.meta"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            entries = value.get("exit_code_by_key", {})
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if isinstance(entries, dict):
            exit_codes.update(
                {
                    str(key): value if isinstance(value, int) else None
                    for key, value in entries.items()
                }
            )
    statuses = [
        "not_checked"
        if exit_codes.get(name) is None
        else MUTMUT_STATUS_BY_EXIT.get(exit_codes[name], "suspicious")
        for name in mutmut_names
    ]
    selected_stats = {
        "total": len(mutmut_names),
        **{status: statuses.count(status) for status in sorted(set(statuses))},
    }
    phase_timings = raw_summary.get("phase_timings", {})
    sample: MutmutSample = {
        "wall_ms": elapsed,
        "summary": {
            "stats": selected_stats,
            "phase_timings": phase_timings,
            "outcomes": dict(zip(mutmut_names, statuses, strict=True)),
        },
    }
    if resources is not None:
        sample["resources"] = resources
    if retain_process:
        sample["process"] = {
            "command": command,
            "cwd": str(root),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "raw_summary": raw_summary,
            "raw_exit_codes": exit_codes,
            "environment": {
                key: run_environment.get(key)
                for key in ("PYTHONPATH", "PYTEST_PLUGINS", "PYTEST_ADDOPTS")
            },
        }
    return sample


def _vfy_outcomes(report: VfySample) -> dict[str, dict[str, object]]:
    raw = report.get("mutations")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("vfy returned no mutation outcomes")
    outcomes: dict[str, dict[str, object]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise RuntimeError("vfy returned an invalid mutation outcome")
        identifier = item.get("id")
        status = item.get("status")
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError("vfy returned a mutation without an ID")
        if not isinstance(status, str):
            raise RuntimeError(f"vfy returned an invalid status for {identifier}")
        if identifier in outcomes:
            raise RuntimeError(f"vfy returned duplicate mutation ID {identifier}")
        outcomes[identifier] = cast(dict[str, object], item)
    return outcomes


_VFY_TO_MUTMUT_STATUS = {
    "protected": "killed",
    "survived": "survived",
    # vfy records a mutation as unreached when its selected tests never
    # import the changed operation.  mutmut reports the same observable
    # result as a survivor because no test failed; retain the source status in
    # the evidence while comparing this normalized outcome.
    "unreached": "survived",
    "unstable": "suspicious",
    "timeout": "timeout",
    "error": "suspicious",
}


def compare_fixture_outcomes(
    vfy: VfySample,
    mutmut: MutmutSample,
    *,
    common_catalog: set[CatalogKey],
    vfy_ids: dict[CatalogKey, str],
    mutmut_names: dict[CatalogKey, str],
) -> dict[str, object]:
    """Compare exact common mutation outcomes, including a real call failure."""

    vfy_outcomes = _vfy_outcomes(vfy)
    raw_mutmut = mutmut.get("summary", {}).get("outcomes", {})
    if not isinstance(raw_mutmut, dict):
        raise RuntimeError("mutmut returned no per-mutant outcomes")
    comparisons: dict[str, dict[str, str]] = {}
    for key in sorted(common_catalog):
        vfy_id = vfy_ids.get(key)
        mutmut_name = mutmut_names.get(key)
        if vfy_id is None or mutmut_name is None:
            raise RuntimeError("common mutation is missing an engine-specific ID")
        vfy_record = vfy_outcomes.get(vfy_id)
        mutmut_status = raw_mutmut.get(mutmut_name)
        if vfy_record is None or not isinstance(mutmut_status, str):
            raise RuntimeError(f"missing outcome for common mutation {key!r}")
        vfy_status = vfy_record["status"]
        if not isinstance(vfy_status, str):
            raise RuntimeError(f"invalid vfy outcome for common mutation {key!r}")
        expected = _VFY_TO_MUTMUT_STATUS.get(vfy_status)
        if expected is None:
            raise RuntimeError(f"unmapped vfy status {vfy_status!r}")
        if expected != mutmut_status:
            raise RuntimeError(
                f"mutmut fixture parity failed for {key!r}: "
                f"vfy={vfy_status!r}, mutmut={mutmut_status!r}"
            )
        comparisons["|".join(map(str, key))] = {
            "vfy": vfy_status,
            "mutmut": mutmut_status,
        }
    call_failure = any(
        item["vfy"] == "protected" and bool(vfy_outcomes[vfy_ids[key]].get("killer"))
        for key, item in (
            (key, comparisons["|".join(map(str, key))]) for key in sorted(common_catalog)
        )
    )
    if not call_failure:
        raise RuntimeError("mutation fixture did not observe a real failing test call")
    return {
        "common_mutations": len(comparisons),
        "outcomes": comparisons,
        "real_call_failure_observed": True,
    }


def _append_behavior_preserving_edit(root: Path) -> tuple[Path, str]:
    candidates = sorted((root / "tests").rglob("*.py"))
    if not candidates:
        raise RuntimeError("mutation fixture has no test source for invalidation validation")
    path = candidates[0]
    original = path.read_text(encoding="utf-8")
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n# benchmark cache invalidation probe\n")
    return path, original


# C901: this ordered four-state protocol keeps cache and semantic gates together.
def validate_stateful_flow(  # noqa: C901
    project: Path,
    args: argparse.Namespace,
    common_catalog: set[CatalogKey],
    *,
    source_paths: Sequence[Path] | None = None,
    pytest_args: Sequence[str] = (),
    python: str | None = None,
    child_environment: dict[str, str] | None = None,
    copy_fixture: bool = True,
    evidence_dir: Path | None = None,
    killer_proofs: _KillerProofs | None = None,
) -> dict[str, object]:
    """Prove baseline, verified, unchanged-cache, and invalidation states."""

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if copy_fixture:
        temporary = tempfile.TemporaryDirectory(prefix="vfy-mutation-state-")
        root = Path(temporary.name) / "project"
        copy_project(project, root)
        if source_paths is not None:
            source_paths = tuple(
                root / path.resolve().relative_to(project.resolve()) for path in source_paths
            )
        if child_environment is not None:
            child_environment = {
                key: value.replace(str(project.resolve()), str(root))
                for key, value in child_environment.items()
            }
    else:
        root = project
    selected_nodeids: tuple[str, ...] = ()
    if pytest_args:
        selection_environment = (
            environment(root) if child_environment is None else dict(child_environment)
        )
        selection_path, selected_nodeids = _prepare_test_selection(
            root,
            pytest_args,
            python=python or sys.executable,
            env=selection_environment,
        )
        selection_environment["VFY_SELECTED_TESTS"] = str(selection_path)
        child_environment = selection_environment
    edit_path: Path | None = None
    original_edit: str | None = None
    state_signature: tuple[tuple[CatalogKey, str, str | None], ...] | None = None
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)

    def retain(label: str, report: VfySample) -> None:
        if evidence_dir is not None:
            (evidence_dir / f"{label}.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    def measure(label: str, *, clear_cache: bool) -> VfySample:
        try:
            report = measured_vfy(
                root,
                args,
                common_catalog,
                clear_cache=clear_cache,
                source_paths=source_paths,
                pytest_args=pytest_args,
                python=python,
                child_environment=child_environment,
                resource_path=(
                    (evidence_dir or args.output.with_suffix(".artifacts") / "states")
                    / "resources"
                    / f"{label}.json"
                    if getattr(args, "tree_resources", False)
                    else None
                ),
            )
        except Exception as error:
            if evidence_dir is not None:
                (evidence_dir / f"{label}.failure.json").write_text(
                    json.dumps({"state": label, "error": str(error)}, indent=2) + "\n",
                    encoding="utf-8",
                )
            raise
        retain(label, report)
        return report

    try:
        baseline = measure("baseline", clear_cache=True)
        verified = measure("verified", clear_cache=False)
        unchanged = measure("unchanged", clear_cache=False)
        edit_path, original_edit = _append_behavior_preserving_edit(root)
        invalidated = measure("invalidated", clear_cache=False)
        state_reports = (
            ("baseline", baseline),
            ("verified", verified),
            ("unchanged", unchanged),
            ("invalidated", invalidated),
        )
        try:
            state_signature = _validate_state_signatures(
                state_reports,
                root=root,
                expected_catalog=common_catalog,
                killer_proofs=killer_proofs,
            )
        except RuntimeError as error:
            if evidence_dir is not None:
                signatures: dict[str, VfySignature] = {}
                for label, report in state_reports:
                    try:
                        signatures[label] = _validated_vfy_signature(
                            report,
                            root=root,
                            expected_catalog=common_catalog,
                            label=label,
                        )
                    except RuntimeError:
                        continue
                reference = signatures.get("baseline", ())
                differences = []
                for label, signature in signatures.items():
                    if signature == reference:
                        continue
                    expected_by_key = {item[0]: item[1:] for item in reference}
                    actual_by_key = {item[0]: item[1:] for item in signature}
                    changed = [
                        {
                            "identity": list(key),
                            "baseline": expected_by_key.get(key),
                            "state": actual_by_key.get(key),
                        }
                        for key in sorted(expected_by_key.keys() | actual_by_key.keys())
                        if expected_by_key.get(key) != actual_by_key.get(key)
                    ]
                    differences.append({"state": label, "first_differences": changed[:10]})
                (evidence_dir / "validation-failure.json").write_text(
                    json.dumps(
                        {"error": str(error), "differences": differences},
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            raise
    finally:
        if not copy_fixture:
            if edit_path is not None and original_edit is not None:
                edit_path.write_text(
                    original_edit,
                    encoding="utf-8",
                )
            shutil.rmtree(root / ".vfy", ignore_errors=True)
        if temporary is not None:
            temporary.cleanup()
    metrics = [item.get("metrics", {}) for item in (baseline, verified, unchanged, invalidated)]
    cache_hits = [
        float(value.get("cache_hits", 0.0)) if isinstance(value, dict) else 0.0 for value in metrics
    ]
    if cache_hits[0] != 0:
        raise RuntimeError("baseline mutation run unexpectedly reused cached results")
    if cache_hits[2] <= 0:
        raise RuntimeError("unchanged mutation run did not reuse a verified cache")
    if cache_hits[3] >= cache_hits[2]:
        raise RuntimeError("behavior-preserving test edit did not invalidate mutation cache")
    for label, report in (
        ("baseline", baseline),
        ("verified", verified),
        ("unchanged", unchanged),
        ("invalidated", invalidated),
    ):
        if not report.get("summary") or not report.get("mutations"):
            raise RuntimeError(f"{label} mutation state has no semantic outcomes")
    if edit_path is None:
        raise RuntimeError("mutation invalidation edit was not recorded")
    if state_signature is None:
        raise RuntimeError("stateful mutation signature was not recorded")
    result: dict[str, object] = {
        "states": ["baseline", "verified_baseline", "unchanged_cached", "invalidated_edit"],
        "cache_hits": cache_hits,
        "pytest_selection": {
            "requested": list(pytest_args),
            "nodeids": list(selected_nodeids),
        },
        "invalidated_path": (
            str(edit_path.relative_to(root)) if edit_path.is_relative_to(root) else "tests"
        ),
        "baseline_outcomes": baseline["summary"],
        **(
            {"resources": {label: report["resources"] for label, report in state_reports}}
            if getattr(args, "tree_resources", False)
            else {}
        ),
    }
    if evidence_dir is not None:
        if killer_proofs is not None:
            result["killer_proofs"] = killer_proofs.records()
        (evidence_dir / "state-summary.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return result


def validate_datasette_fixture(args: argparse.Namespace) -> dict[str, object] | None:
    """Run the scoped Datasette utility through the same stateful protocol."""

    if args.datasette_root is None:
        return None
    evidence_root = getattr(args, "evidence_dir", None)
    if evidence_root is not None:
        evidence_root = Path(evidence_root)
        shutil.rmtree(evidence_root, ignore_errors=True)
        evidence_root.mkdir(parents=True, exist_ok=True)
    selected_python = args.datasette_python.absolute()
    fixture: DatasetteMutationFixture = prepare_datasette_fixture(
        args.datasette_root.resolve(),
        selected_python,
        args.datasette_preparation_manifest.resolve(),
        args.datasette_artifacts.resolve(),
    )
    raw_records = analyze_mutation_files([str(path) for path in fixture.source_files])
    scoped_records = fixture.filter_records(raw_records)
    if not scoped_records:
        raise RuntimeError("Datasette escape_sqlite scope contains no mutations")
    catalog: set[CatalogKey] = set()
    for record in scoped_records:
        if not isinstance(record[7], int) or not isinstance(record[8], int):
            raise RuntimeError("Datasette mutation record has invalid byte offsets")
        catalog.add(
            (
                Path(str(record[1])).resolve().relative_to(fixture.root.resolve()).as_posix(),
                int(record[7]),
                int(record[8]),
                str(record[5]),
                str(record[6]),
            )
        )
    datasette_environment = workflow_environment(
        fixture.project, selected_python, root=fixture.root
    )
    current_pythonpath = datasette_environment.get("PYTHONPATH")
    datasette_environment["PYTHONPATH"] = (
        str(fixture.root)
        if not current_pythonpath
        else str(fixture.root) + os.pathsep + current_pythonpath
    )
    configured_plugins = tuple(
        plugin for plugin in datasette_environment.get("PYTEST_PLUGINS", "").split(",") if plugin
    )
    datasette_plugins = datasette_environment.get("PYTEST_PLUGINS")
    removed_plugins: tuple[str, ...] = ()
    if datasette_plugins:
        removed_plugins = tuple(
            plugin
            for plugin in configured_plugins
            if plugin in {"pytest_asyncio.plugin", "xdist.plugin"}
        )
        datasette_environment["PYTEST_PLUGINS"] = ",".join(
            plugin for plugin in configured_plugins if plugin not in removed_plugins
        )
    state_evidence = (
        evidence_root if evidence_root is not None else args.datasette_artifacts / "mutation-state"
    )
    try:
        state = validate_stateful_flow(
            fixture.root,
            args,
            catalog,
            source_paths=fixture.source_files,
            pytest_args=fixture.scope.test_targets,
            python=str(selected_python),
            child_environment=datasette_environment,
            copy_fixture=True,
            evidence_dir=state_evidence,
        )
    except Exception as error:  # noqa: BLE001 - retain failed state evidence
        state_records: dict[str, list[dict[str, object]]] = {}
        for label in ("baseline", "verified", "unchanged", "invalidated"):
            report_path = state_evidence / f"{label}.json"
            try:
                report_value = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            mutations = report_value.get("mutations")
            if not isinstance(mutations, list):
                continue
            state_records[label] = [
                {
                    "id": mutation.get("id"),
                    "status": mutation.get("status"),
                    "killer": mutation.get("killer"),
                }
                for mutation in mutations
                if isinstance(mutation, dict)
            ]
        failure = {"status": "failed", "error": str(error), "states": state_records}
        (state_evidence / "validation-failure.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return {
            "project": fixture.project.key,
            "revision": fixture.project.revision,
            "scope": {
                "source": str(DATASETTE_SOURCE),
                "function": fixture.scope.function,
                "byte_range": [fixture.scope.start_byte, fixture.scope.end_byte],
                "test_targets": list(fixture.scope.test_targets),
            },
            "catalog": len(catalog),
            "source_sha256": fixture.source_sha256,
            "preparation": fixture.preparation,
            "evidence_dir": str(state_evidence),
            "pytest_plugins": {
                "configured": list(configured_plugins),
                "effective": [
                    plugin
                    for plugin in datasette_environment.get("PYTEST_PLUGINS", "").split(",")
                    if plugin
                ],
                "removed": list(removed_plugins),
                "removal_reason": (
                    "vfy mutation children run serially here; explicit asyncio and xdist "
                    "plugins would change the controller boundary or introduce parallel "
                    "killer races"
                    if removed_plugins
                    else None
                ),
            },
            "state_validation": failure,
            "validation": failure,
        }
    if evidence_root is not None:
        fixture_evidence = args.datasette_artifacts / "datasette-mutation-fixture.json"
        if fixture_evidence.is_file():
            shutil.copy2(fixture_evidence, evidence_root / fixture_evidence.name)
    return {
        "project": fixture.project.key,
        "revision": fixture.project.revision,
        "scope": {
            "source": str(DATASETTE_SOURCE),
            "function": fixture.scope.function,
            "byte_range": [fixture.scope.start_byte, fixture.scope.end_byte],
            "test_targets": list(fixture.scope.test_targets),
        },
        "catalog": len(catalog),
        "source_sha256": fixture.source_sha256,
        "preparation": fixture.preparation,
        "evidence_dir": (
            str(evidence_root)
            if evidence_root is not None
            else str(args.datasette_artifacts / "mutation-state")
        ),
        "pytest_plugins": {
            "configured": list(configured_plugins),
            "effective": [
                plugin
                for plugin in datasette_environment.get("PYTEST_PLUGINS", "").split(",")
                if plugin
            ],
            "removed": list(removed_plugins),
            "removal_reason": (
                "vfy mutation children run serially here; explicit asyncio and xdist "
                "plugins would change the controller boundary or introduce parallel killer races"
                if removed_plugins
                else None
            ),
        },
        "state_validation": state,
        "validation": {"status": "passed"},
    }


def stats(samples: list[float]) -> TimingSummary:
    return summarize(samples)


def ratio_stats(numerators: list[float], denominators: list[float]) -> RatioSummary:
    samples = [
        numerator / denominator
        for numerator, denominator in zip(numerators, denominators, strict=True)
    ]
    summary = summarize(samples)
    uncertainty = independent_uncertainty(samples, [1.0] * len(samples))
    low = uncertainty["ci95_low_ms"]
    high = uncertainty["ci95_high_ms"]
    return {
        "samples": samples,
        "median": summary["median_ms"],
        "mean": summary["mean_ms"],
        "lower_95_percent_bound": None if low is None else low + 1.0,
        "upper_95_percent_bound": None if high is None else high + 1.0,
        "uncertainty": dict(uncertainty),
    }


def compact_vfy_report(report: VfySample) -> CompactSample:
    """Retain timing/correctness metadata without embedding every mutation."""
    return {
        "wall_ms": float(report.get("wall_ms", 0.0)),
        "summary": report.get("summary", {}),
        "metrics": report.get("metrics", {}),
        **({"resources": report["resources"]} if "resources" in report else {}),
    }


def _benchmark_exit_code(mode: str, gates: Mapping[str, bool]) -> int:
    """Return a nonzero status when a publication check fails."""

    return int(mode == BenchmarkMode.PUBLICATION.value and not all(gates.values()))


def _mutation_methodology(args: argparse.Namespace, *, timed: bool) -> dict[str, object]:
    """Describe the semantic and timing boundaries shared by mutation results."""

    mode = str(args.mode)
    cold_only = bool(getattr(args, "cold_only", False))
    test_isolation = str(getattr(args, "test_isolation", "default"))
    validation_only = bool(getattr(args, "validate_only", False)) or bool(
        getattr(args, "datasette_only", False)
    )
    return {
        "mode": mode,
        "warmups": args.warmups,
        "measured_runs": args.runs,
        "resources": tree_resource_methodology()
        if getattr(args, "tree_resources", False)
        else None,
        "parity": (
            "exact source identity, byte range, replacement, and status parity; any "
            "fresh-checkout selected-body killer rerank is independently proved before timing"
        ),
        "catalog_validation": ("vfy and mutmut must expose the same exact source mutation catalog"),
        "status_validation": (
            "every sample and state must preserve the complete mutation status and summary counts"
        ),
        "killer_validation": (
            "protected mutations retain nonempty killers; definition/full-suite and same-baseline "
            "warm/cache results require exact killer identity, while a fresh-checkout body rerank "
            "requires clean and mutant call-phase proofs for both claimed killers"
        ),
        "complete_catalog": (
            "not run in cold-only diagnostics"
            if cold_only
            else "untimed informational catalogs differ between tools; vfy timeouts remain "
            "explicit non-protected outcomes without a killer or process exit status; "
            "timeouts are still rejected in the timed common-catalog comparison"
        ),
        "timed_boundary": (
            "child vfy and mutmut commands only; catalog generation, semantic "
            "oracles, parsing, and gates are outside timers"
            if timed
            else "no timing samples; semantic and cache-state validation only"
        ),
        "engine": "pytest-native",
        "timeout_policy": getattr(args, "timeout_policy", None),
        "test_isolation": {
            "mode": test_isolation,
            "cli_arguments": list(_test_isolation_arguments(args)),
            "scope": (
                "vfy uses the native engine isolation default without --test-isolation in "
                "baseline, mutation, cache-state, and full-catalog commands; mutmut retains its "
                "own process model"
                if test_isolation == "default"
                else "the same file-group plug-in runs vfy baseline and mutation tests under the "
                "shared -j budget of executing test workers; mutmut retains its own process model"
                if test_isolation == "file"
                else "vfy uses its serial pytest session; mutmut retains its own process model"
            ),
        },
        "vfy_cache_state_protocol": (
            "fresh checkout and empty vfy cache for every cold sample; other states not run"
            if cold_only
            else "baseline -> verified baseline -> unchanged cached -> behavior-preserving "
            "edit invalidated cache, with one checkout per repetition"
        ),
        "mutmut_cache_scope_limitation": (
            "the pinned mutmut command reruns explicitly named mutants; an unnamed cached "
            "command is usable for this comparison only when it covers exactly the common "
            "catalog, and this workload omits incremental timing and ratio"
        ),
        "raw_sample_validation": (
            "each measured wall-time sample is finite, nonnegative, retained, and "
            "validated after its timer stops"
        ),
        "publishable": mode == BenchmarkMode.PUBLICATION.value and not validation_only,
        "validation_only": validation_only,
    }


def _run_datasette_only(args: argparse.Namespace) -> int:
    """Run the prepared scoped fixture through the public validation command."""

    evidence_dir = args.output.with_suffix(".artifacts")
    args.evidence_dir = evidence_dir
    datasette_validation = validate_datasette_fixture(args)
    if datasette_validation is None:
        raise RuntimeError("--datasette-only requires a prepared Datasette fixture")
    catalog = datasette_validation.get("catalog")
    if not isinstance(catalog, int):
        raise RuntimeError("Datasette validation returned an invalid catalog count")
    validation = datasette_validation.get("validation")
    if not isinstance(validation, dict) or validation.get("status") not in {"passed", "failed"}:
        raise RuntimeError("Datasette validation returned no status")
    output = {
        "schema": "vfy.mutation-benchmark",
        "benchmark": "mutation",
        "mode": BenchmarkMode.DIAGNOSTIC.value,
        "validation_only": True,
        "datasette_only": True,
        "methodology": _mutation_methodology(args, timed=False),
        "recorded_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "datasette_validation": datasette_validation,
        "provenance": build_provenance(
            REPO_ROOT,
            workload={
                "project": "datasette",
                "scope": "escape_sqlite",
                "catalog": catalog,
            },
            workers=args.workers,
            packages=("vfy", "pytest", "coverage"),
            publication=False,
        ),
        "validation": validation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps({"validation": validation["status"], "output": str(args.output)}, sort_keys=True)
    )
    return 0 if validation["status"] == "passed" else 1


def _run_cold_diagnostic(
    args: argparse.Namespace,
    common_catalog: set[CatalogKey],
    common_mutmut_names: list[str],
    fixture_parity: dict[str, object],
    vfy_signature: VfySignature,
    mutmut_signature: MutmutSignature,
    killer_proofs: _KillerProofs,
) -> int:
    """Retain a bounded cold comparison with the ordinary semantic gates."""
    evidence = args.output.with_suffix(".artifacts") / "samples"
    vfy_samples: list[VfySample] = []
    mutmut_samples: list[MutmutSample] = []
    for index in range(args.warmups + args.runs):
        order = ("vfy", "mutmut") if index % 2 == 0 else ("mutmut", "vfy")
        with tempfile.TemporaryDirectory(prefix="vfy-mutation-cold-") as temporary:
            root = Path(temporary) / "project"
            copy_project(args.project.resolve(), root)
            for engine in order:
                label = f"round-{index:03d}-{engine}-cold"
                print(f"mutation: {label}", file=sys.stderr, flush=True)
                if engine == "vfy":
                    report, vfy_signature = _sample_vfy(
                        root,
                        args,
                        common_catalog,
                        label=label,
                        expected_signature=vfy_signature,
                        evidence_dir=evidence,
                        killer_proofs=killer_proofs,
                        allow_body_rerank=True,
                    )
                    _write_sample_evidence(evidence, label + "-report", report)
                    if index >= args.warmups:
                        vfy_samples.append(report)
                else:
                    result, mutmut_signature = _sample_mutmut(
                        root,
                        args,
                        common_mutmut_names,
                        label=label,
                        expected_signature=mutmut_signature,
                        evidence_dir=evidence,
                    )
                    if index >= args.warmups:
                        mutmut_samples.append(result)
    vfy_times = [float(item["wall_ms"]) for item in vfy_samples]
    mutmut_times = [float(item["wall_ms"]) for item in mutmut_samples]
    result = {
        "schema": "vfy.mutation-benchmark",
        "benchmark": "mutation",
        "mode": BenchmarkMode.DIAGNOSTIC.value,
        "cold_only": True,
        "methodology": _mutation_methodology(args, timed=True),
        "mutmut_revision": MUTMUT_REVISION,
        "catalog": {"common": len(common_catalog)},
        "fixture_parity": fixture_parity,
        "killer_proofs": killer_proofs.records(),
        "provenance": build_provenance(
            REPO_ROOT,
            workload={"project": str(args.project.resolve()), "catalog": len(common_catalog)},
            workers=args.workers,
            packages=("vfy", "pytest", "coverage"),
            publication=False,
        ),
        "vfy_first_run": stats(vfy_times),
        "mutmut": stats(mutmut_times),
        "ratio_mutmut_over_vfy_first_run": ratio_stats(mutmut_times, vfy_times),
        "last_vfy_summary": vfy_samples[-1]["summary"],
        "validation": {"status": "passed", "scope": "cold common catalog"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"validation": "passed", "output": str(args.output)}, sort_keys=True))
    return 0


def main() -> int:
    args = parse_args()
    artifact_before = (
        artifact_provenance(strict=True) if args.mode == BenchmarkMode.PUBLICATION.value else None
    )
    if args.datasette_only:
        return _run_datasette_only(args)
    args.mutmut = resolve_command(args.mutmut)
    args.mutmut_python = resolve_command(args.mutmut_python)
    vfy_revision, vfy_tree, vfy_dirty = repository_state(REPO_ROOT)
    mutmut_revision, mutmut_tree, _mutmut_dirty = repository_state(Path(args.mutmut))
    if mutmut_revision != MUTMUT_REVISION:
        raise RuntimeError(
            f"mutmut checkout is {mutmut_revision}, expected pinned revision {MUTMUT_REVISION}"
        )
    try:
        output_relative = args.output.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        output_relative = ""
    benchmark_inputs_clean = all(
        entry[3:] == output_relative for entry in vfy_dirty if len(entry) > 3
    )

    rust_build = str(build_profile())
    project = args.project.resolve()
    vfy_catalog_keys, catalog_ms, _vfy_ids = vfy_catalog(project)
    mutmut_catalog_keys, mutmut_catalog_ms, mutmut_names_by_key = mutmut_catalog(
        project, args.mutmut_python
    )
    common_catalog = vfy_catalog_keys & mutmut_catalog_keys
    if not common_catalog:
        raise RuntimeError(
            "the pinned mutmut visitor did not produce an exact common catalog; "
            "refusing to compare unlike mutation sets"
        )
    common_mutmut_names = [mutmut_names_by_key[key] for key in sorted(common_catalog)]
    mutmut_cached_scope_exact = _mutmut_cached_scope_is_exact(mutmut_catalog_keys, common_catalog)
    if len(common_mutmut_names) != len(common_catalog):
        raise RuntimeError(
            "could not map every common source mutation to a runnable vfy or mutmut ID"
        )

    # Correctness parity gets its own disposable checkout so it cannot warm a
    # timed first run or seed its coverage baseline.
    killer_proofs = _KillerProofs(
        args.output.with_suffix(".artifacts") / "killer-proofs",
        python=sys.executable,
        timeout=args.timeout,
    )
    fixture_parity, vfy_signature, mutmut_signature = run_fixture_parity(
        project,
        args,
        common_catalog,
        mutmut_names_by_key,
        killer_proofs=killer_proofs,
    )
    if args.cold_only:
        return _run_cold_diagnostic(
            args,
            common_catalog,
            common_mutmut_names,
            fixture_parity,
            vfy_signature,
            mutmut_signature,
            killer_proofs,
        )
    state_evidence = (
        args.output.with_suffix(".artifacts") / "mutation-state"
        if args.validate_only or args.diagnostic
        else None
    )
    state_validation = validate_stateful_flow(
        project,
        args,
        common_catalog,
        evidence_dir=state_evidence,
        killer_proofs=killer_proofs,
    )
    datasette_validation = validate_datasette_fixture(args)

    if args.validate_only:
        validation = {
            "schema": "vfy.mutation-benchmark",
            "benchmark": "mutation",
            "mode": BenchmarkMode.DIAGNOSTIC.value,
            "validation_only": True,
            "methodology": _mutation_methodology(args, timed=False),
            "mutmut_revision": MUTMUT_REVISION,
            "vfy_revision": vfy_revision,
            "catalog": {
                "vfy": len(vfy_catalog_keys),
                "mutmut": len(mutmut_catalog_keys),
                "common": len(common_catalog),
                "mutmut_cached_scope_exact": mutmut_cached_scope_exact,
            },
            "fixture_parity": fixture_parity,
            "state_validation": state_validation,
            "killer_proofs": killer_proofs.records(),
            "datasette_validation": datasette_validation,
            "provenance": build_provenance(
                REPO_ROOT,
                workload={"project": str(project), "catalog": len(common_catalog)},
                workers=args.workers,
                packages=("vfy", "pytest", "coverage"),
                publication=False,
            ),
            "validation": {"status": "passed"},
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps({"validation": "passed", "output": str(args.output)}, sort_keys=True))
        return 0

    vfy_first_run_samples: list[VfySample] = []
    vfy_warm_samples: list[VfySample] = []
    vfy_incremental_samples: list[VfySample] = []
    mutmut_samples: list[MutmutSample] = []
    sample_evidence_dir = args.output.with_suffix(".artifacts") / "samples"
    sample_evidence_dir.mkdir(parents=True, exist_ok=True)
    if vfy_signature is None or mutmut_signature is None:
        raise RuntimeError("mutation parity did not return stable timing signatures")
    total = args.warmups + args.runs
    for index in range(total):
        order = ("vfy", "mutmut") if index % 2 == 0 else ("mutmut", "vfy")
        with tempfile.TemporaryDirectory(prefix="vfy-mutation-benchmark-") as temporary:
            root = Path(temporary) / "project"
            copy_project(project, root)
            for engine in order:
                print(
                    f"mutation: round {index + 1}/{total}: {engine}",
                    file=sys.stderr,
                    flush=True,
                )
                if engine == "vfy":
                    first_run, vfy_signature = _sample_vfy(
                        root,
                        args,
                        common_catalog,
                        label=f"round-{index:03d}-vfy-cold",
                        expected_signature=vfy_signature,
                        evidence_dir=sample_evidence_dir,
                        killer_proofs=killer_proofs,
                        allow_body_rerank=True,
                    )
                    vfy_first_run_samples.append(first_run)
                    warm_run, vfy_signature = _sample_vfy(
                        root,
                        args,
                        common_catalog,
                        label=f"round-{index:03d}-vfy-warm",
                        expected_signature=vfy_signature,
                        evidence_dir=sample_evidence_dir,
                        killer_proofs=killer_proofs,
                    )
                    vfy_warm_samples.append(warm_run)
                    incremental_run, vfy_signature = _sample_vfy(
                        root,
                        args,
                        common_catalog,
                        label=f"round-{index:03d}-vfy-cached",
                        expected_signature=vfy_signature,
                        evidence_dir=sample_evidence_dir,
                        killer_proofs=killer_proofs,
                        clear_cache=False,
                    )
                    vfy_incremental_samples.append(incremental_run)
                else:
                    result, mutmut_signature = _sample_mutmut(
                        root,
                        args,
                        common_mutmut_names,
                        label=f"round-{index:03d}-mutmut-cold",
                        expected_signature=mutmut_signature,
                        evidence_dir=sample_evidence_dir,
                    )
                    mutmut_samples.append(result)
            if index == args.warmups - 1:
                vfy_first_run_samples.clear()
                vfy_warm_samples.clear()
                vfy_incremental_samples.clear()
                mutmut_samples.clear()

    vfy_first_run_times = [float(item["wall_ms"]) for item in vfy_first_run_samples]
    vfy_warm_times = [float(item["wall_ms"]) for item in vfy_warm_samples]
    vfy_incremental_times = [float(item["wall_ms"]) for item in vfy_incremental_samples]
    mutmut_times = [float(item["wall_ms"]) for item in mutmut_samples]
    first_run_ratio = ratio_stats(mutmut_times, vfy_first_run_times)
    warm_ratio = ratio_stats(mutmut_times, vfy_warm_times)

    # Also check the complete catalog. Use a fresh checkout and baseline so
    # the default command cannot reuse state from the common-catalog samples.
    with tempfile.TemporaryDirectory(prefix="vfy-mutation-complete-") as temporary:
        complete_root = Path(temporary) / "project"
        copy_project(project, complete_root)
        complete_vfy = measured_vfy(
            complete_root,
            args,
            resource_path=(
                args.output.with_suffix(".artifacts") / "resources/complete-vfy.json"
                if args.tree_resources
                else None
            ),
        )
        _write_sample_evidence(sample_evidence_dir, "complete-vfy", complete_vfy)
        _validated_vfy_signature(
            complete_vfy,
            root=complete_root,
            expected_catalog=vfy_catalog_keys,
            label="complete vfy",
            scope=_CatalogScope.COMPLETE,
        )
        complete_mutmut = measured_mutmut(
            complete_root,
            args,
            [mutmut_names_by_key[key] for key in sorted(mutmut_catalog_keys)],
            resource_path=(
                args.output.with_suffix(".artifacts") / "resources/complete-mutmut.json"
                if args.tree_resources
                else None
            ),
        )
        _write_sample_evidence(sample_evidence_dir, "complete-mutmut", complete_mutmut)

    # Use the last measured vfy report for correctness/fast-path gates; the
    # parity check above compares the same immutable catalog in both engines.
    last_vfy = vfy_first_run_samples[-1]
    last_mutmut = mutmut_samples[-1]
    mutations = last_vfy.get("mutations", [])
    metrics = last_vfy.get("metrics", {})
    mutation_count = int(metrics.get("mutations", len(mutations)))
    fast_fraction = int(metrics.get("fast_results", 0)) / mutation_count if mutation_count else 0.0
    summary = last_vfy.get("summary", {})
    mutmut_stats = last_mutmut.get("summary", {}).get("stats", {})
    mutmut_total = mutmut_stats.get("total") if isinstance(mutmut_stats, dict) else None
    forbidden = sum(
        int(summary.get(name, 0)) for name in ("error", "invalid", "timeout", "unstable")
    )
    raw_cache_hits = state_validation.get("cache_hits")
    if not isinstance(raw_cache_hits, list) or len(raw_cache_hits) != 4:
        raise RuntimeError("state validation returned an invalid cache sequence")
    cache_hits = [float(value) for value in raw_cache_hits]
    gates = {
        "benchmark_inputs_clean": benchmark_inputs_clean
        or args.mode == BenchmarkMode.DIAGNOSTIC.value,
        "release_rust_build": rust_build == "release"
        or args.mode == BenchmarkMode.DIAGNOSTIC.value,
        "common_catalog_nonempty": bool(common_catalog),
        # Both measured commands are restricted to the exact intersection.  A
        # mismatch here means a tool ignored the filter or failed to map a
        # source mutation to its runtime identifier.
        "timed_on_common_catalog": (
            len(mutations) == len(common_catalog)
            and isinstance(mutmut_total, int)
            and mutmut_total == len(common_catalog)
        ),
        "minimum_measurement_protocol": args.mode == BenchmarkMode.DIAGNOSTIC.value
        or (args.warmups >= 3 and args.runs >= 30),
        "no_infrastructure_outcomes_protected": forbidden == 0,
        "fixture_outcomes_match": bool(fixture_parity.get("outcomes")),
        "real_call_failure_observed": fixture_parity.get("real_call_failure_observed") is True,
        "baseline_cache_contract": cache_hits[0] == 0,
        "unchanged_cache_contract": cache_hits[2] > 0,
        "invalidation_cache_contract": cache_hits[3] < cache_hits[2],
    }
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    provenance = build_provenance(
        REPO_ROOT,
        workload={
            "project": str(project),
            "mutmut_revision": MUTMUT_REVISION,
            "common_catalog": len(common_catalog),
        },
        workers=args.workers,
        cache="baseline, verified, unchanged, invalidated states are retained in protocol",
        packages=("vfy", "pytest", "coverage"),
        publication=args.mode == BenchmarkMode.PUBLICATION.value,
    )
    provenance["workload_source"] = {
        "revision": mutmut_revision,
        "tree": mutmut_tree,
        "dirty": bool(_mutmut_dirty),
    }
    provenance["validation"] = {"status": "passed" if all(gates.values()) else "failed"}
    result = {
        "schema": "vfy.mutation-benchmark",
        "benchmark": "mutation",
        "mode": args.mode,
        "methodology": _mutation_methodology(args, timed=True),
        "mutmut_repository": MUTMUT_REPOSITORY,
        "mutmut_revision": MUTMUT_REVISION,
        "mutmut_tree": mutmut_tree,
        "project": str(project),
        "recorded_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "vfy_revision": vfy_revision,
        "vfy_tree": vfy_tree,
        "vfy_worktree_clean": not vfy_dirty,
        "vfy_dirty_entries_at_start": list(vfy_dirty),
        "vfy_benchmark_inputs_clean": benchmark_inputs_clean,
        "python": sys.version,
        "packages": {
            "vfy": package_versions(("vfy", "pytest", "coverage")),
            "mutmut": external_package_versions(
                args.mutmut_python,
                ("mutmut", "click", "libcst", "pytest", "textual"),
            ),
        },
        "vfy_rust_build": rust_build,
        "workers": args.workers,
        "test_isolation": args.test_isolation,
        "warmups": args.warmups,
        "runs": args.runs,
        "environment": {
            "BENCHMARK_IMPORT_DELAY": "0.1",
            "BENCHMARK_CONFTEST_DELAY": "0.1",
            "BENCHMARK_TEST_DELAY": "0.05",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        },
        "commands": {
            "vfy": vfy_command(
                "mutate",
                "--engine=pytest-native",
                "--all",
                "--source",
                "src",
                "--data-file",
                ".coverage-vfy",
                "--format",
                "json",
                "-j",
                str(args.workers),
                *_test_isolation_arguments(args),
                *_timeout_policy_arguments(args),
                "--timeout",
                str(args.timeout),
                "-x",
                "-q",
            ),
            "mutmut": [
                args.mutmut,
                "run",
                "--max-children",
                str(args.workers),
                "<exact common mutant names>",
            ],
        },
        "catalog": {
            "vfy": len(vfy_catalog_keys),
            "mutmut": len(mutmut_catalog_keys),
            "common": len(common_catalog),
            "mutmut_cached_scope_exact": mutmut_cached_scope_exact,
            "vfy_generation_ms": catalog_ms,
            "mutmut_generation_ms": mutmut_catalog_ms,
        },
        "provenance": provenance,
        "fixture_parity": fixture_parity,
        "state_validation": state_validation,
        "killer_proofs": killer_proofs.records(),
        "datasette_validation": datasette_validation,
        "vfy_first_run": stats(vfy_first_run_times),
        "vfy_warm": stats(vfy_warm_times),
        "vfy_incremental": stats(vfy_incremental_times),
        "mutmut": stats(mutmut_times),
        "stages": {
            "vfy_baseline_collection": stats(
                [
                    float(item.get("metrics", {}).get("baseline_ms", 0.0))
                    for item in vfy_first_run_samples
                ]
            ),
            "vfy_catalog_generation": {"median_ms": catalog_ms},
            "vfy_execution": stats(
                [
                    float(item.get("metrics", {}).get("execution_ms", 0.0))
                    for item in vfy_warm_samples
                ]
            ),
            "vfy_persistence": stats(
                [
                    float(item.get("metrics", {}).get("persistence_ms", 0.0))
                    for item in vfy_warm_samples
                ]
            ),
            "vfy_incremental_cache_hits": stats(
                [
                    float(item.get("metrics", {}).get("cache_hits", 0.0))
                    for item in vfy_incremental_samples
                ]
            ),
            "vfy_complete_default": compact_vfy_report(complete_vfy),
            "mutmut_complete_default": complete_mutmut,
            "mutmut_phase_timings": [
                item.get("summary", {}).get("phase_timings", {}) for item in mutmut_samples
            ],
        },
        "ratio_mutmut_over_vfy_first_run": {
            **first_run_ratio,
        },
        "ratio_mutmut_over_vfy_warm": {
            **warm_ratio,
        },
        "last_vfy_summary": summary,
        "fast_path_fraction": fast_fraction,
        "gates": gates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"gates": gates, "output": str(args.output)}, sort_keys=True))
    return _benchmark_exit_code(args.mode, gates)


if __name__ == "__main__":
    raise SystemExit(main())
