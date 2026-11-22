"""Prepare the narrow Datasette mutation workload used by the benchmark.

The workload stays on Datasette's real checkout.  It scopes mutation discovery
to ``escape_sqlite`` in ``datasette/utils/__init__.py`` and proves that the
upstream parameterized test still passes before the caller starts mutation
timing.  Checkout, interpreter, wheel, and dependency validation remain owned
by ``prepare_projects`` and ``benchmark_workflows``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .benchmark_harness import benchmark_environment, run_checked
from .benchmark_projects import outcome_records
from .benchmark_workflows import validate_prepared_root, workflow_environment
from .observer_plugins import OUTCOME_PLUGIN, stage_plugins
from .project_manifest import ProjectManifest, manifest

DATASETTE_SOURCE = Path("datasette/utils/__init__.py")
DATASETTE_TEST = "tests/test_utils.py::test_escape_sqlite"


def _require_successful_target(
    records: Sequence[tuple[str, str, str, str | None]], target: str
) -> list[tuple[str, str, str, str | None]]:
    """Require a real, non-skipped successful call for the requested target."""

    target_name = target.rsplit("::", 1)[-1]
    target_records = [
        record
        for record in records
        if (
            (node_name := record[0].rsplit("::", 1)[-1]) == target_name
            or node_name.startswith(f"{target_name}[")
        )
    ]
    if not target_records:
        raise RuntimeError(f"direct mutation fixture produced no outcome for {target!r}")
    if any(record[2] == "skipped" for record in records):
        raise RuntimeError(f"direct mutation fixture contains a skipped outcome: {records!r}")
    calls = [record for record in target_records if record[1] == "call"]
    if not calls or any(record[2] != "passed" for record in calls):
        raise RuntimeError(
            f"direct mutation fixture did not pass the requested call: {target_records!r}"
        )
    if any(record[2] != "passed" for record in target_records):
        raise RuntimeError(
            f"direct mutation fixture contains a non-passing fixture phase: {target_records!r}"
        )
    return target_records


@dataclass(frozen=True)
class MutationScope:
    """The source range and direct test target for one mutation workload."""

    source_file: Path
    function: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    test_targets: tuple[str, ...]

    def contains(self, record: Sequence[object]) -> bool:
        """Return whether a native mutation record belongs to this function."""

        if len(record) < 9:
            return False
        start_value, end_value = record[7], record[8]
        if not isinstance(start_value, int) or not isinstance(end_value, int):
            return False
        path = Path(str(record[1])).resolve()
        start, end = start_value, end_value
        return path == self.source_file and self.start_byte <= start < end <= self.end_byte


@dataclass(frozen=True)
class DatasetteMutationFixture:
    """Validated real-checkout inputs for the Datasette mutation benchmark."""

    project: ProjectManifest
    root: Path
    python: Path
    preparation_manifest: Path
    scope: MutationScope
    source_sha256: str
    preparation: dict[str, object]

    @property
    def source_files(self) -> tuple[Path, ...]:
        return (self.scope.source_file,)

    def filter_records(self, records: Sequence[Sequence[object]]) -> list[Sequence[object]]:
        """Keep only mutations inside the reviewed ``escape_sqlite`` function."""

        return [record for record in records if self.scope.contains(record)]

    def environment(self) -> dict[str, str]:
        """Build the project environment while resolving imports from this checkout."""

        environment = workflow_environment(self.project, self.python, root=self.root)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(self.root) if not existing else str(self.root) + os.pathsep + existing
        )
        return environment


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _function_scope(
    source_file: Path, function: str, test_target: str = DATASETTE_TEST
) -> MutationScope:
    source = source_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_file))
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one top-level {function} definition in {source_file}, found {len(matches)}"
        )
    node = matches[0]
    if node.end_lineno is None or node.end_col_offset is None:
        raise RuntimeError(f"Python AST has no complete range for {function}: {source_file}")

    def offset(line: int, column: int) -> int:
        source_lines = source.splitlines(keepends=True)
        if not 1 <= line <= len(source_lines):
            raise RuntimeError(f"AST line is outside the source: {line}")
        prefix = source_lines[: line - 1]
        return sum(len(item.encode("utf-8")) for item in prefix) + column

    return MutationScope(
        source_file=source_file.resolve(),
        function=function,
        start_byte=offset(node.lineno, node.col_offset),
        end_byte=offset(node.end_lineno, node.end_col_offset),
        start_line=node.lineno,
        end_line=node.end_lineno,
        test_targets=(test_target,),
    )


def prepare_datasette_fixture(
    root: Path,
    python: Path,
    preparation_manifest: Path,
    artifacts: Path,
) -> DatasetteMutationFixture:
    """Validate preparation and the direct upstream ``escape_sqlite`` test.

    This performs no mutation and is intended to run before benchmark samples.
    The caller can then pass ``fixture.source_files`` to native catalog
    discovery and use ``fixture.filter_records`` to retain the scoped records.
    """

    project = manifest("datasette")
    preparation = validate_prepared_root(
        project,
        root,
        python,
        preparation_manifest,
        artifacts,
    )
    source_file = (root / DATASETTE_SOURCE).resolve()
    if not source_file.is_file():
        raise RuntimeError(f"Datasette mutation source is missing: {source_file}")
    scope = _function_scope(source_file, "escape_sqlite")
    before = _sha256(source_file)
    command = [
        str(python),
        "-m",
        "pytest",
        "-q",
        *[part for plugin in project.plugins for part in ("-p", plugin)],
        DATASETTE_TEST,
    ]
    environment = workflow_environment(project, python, root=root)
    outcome_path = artifacts / "datasette-mutation-fixture-outcomes.jsonl"
    outcome_path.unlink(missing_ok=True)
    environment["VFY_BENCHMARK_OUTCOMES"] = str(outcome_path)
    environment["PYTEST_PLUGINS"] = ",".join(dict.fromkeys((OUTCOME_PLUGIN, *project.plugins)))
    existing = environment.get("PYTHONPATH")
    plugin_root = stage_plugins(artifacts / "observer-plugins")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(plugin_root), str(root), existing) if part
    )
    completed = run_checked(command, cwd=root, env=environment, timeout=600)
    outcomes = _require_successful_target(outcome_records(outcome_path), DATASETTE_TEST)
    after = _sha256(source_file)
    if before != after:
        raise RuntimeError("Datasette source changed during the direct mutation fixture gate")
    status = run_checked(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        env=benchmark_environment(),
        timeout=30,
    ).stdout.strip()
    if status:
        raise RuntimeError("Datasette checkout changed during the direct mutation fixture gate")
    revision = run_checked(
        ["git", "rev-parse", "HEAD"], cwd=root, env=benchmark_environment(), timeout=30
    ).stdout.strip()
    if revision != project.revision:
        raise RuntimeError(
            "Datasette checkout revision changed during the direct mutation fixture gate"
        )
    evidence = {
        "project": project.key,
        "revision": revision,
        "source": str(DATASETTE_SOURCE),
        "source_sha256": after,
        "function": scope.function,
        "function_byte_range": [scope.start_byte, scope.end_byte],
        "function_line_range": [scope.start_line, scope.end_line],
        "test_targets": list(scope.test_targets),
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "outcomes": [
            {
                "nodeid": nodeid,
                "when": when,
                "outcome": outcome,
                "wasxfail": wasxfail,
            }
            for nodeid, when, outcome, wasxfail in outcomes
        ],
        "preparation": preparation,
    }
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "datasette-mutation-fixture.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return DatasetteMutationFixture(
        project=project,
        root=root.resolve(),
        python=python.resolve(),
        preparation_manifest=preparation_manifest.resolve(),
        scope=scope,
        source_sha256=after,
        preparation=preparation,
    )
