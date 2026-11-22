#!/usr/bin/env python3
"""Parity-gated benchmarks for vfy's built-in asyncio plug-in."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Callable, Iterable
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

import coverage

import vfy
from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    benchmark_environment,
    rotated_orders,
    run_checked,
    sampling_config,
    summarize,
    timed as timed_command,
)
from benchmarks.benchmark_provenance import (
    artifact_provenance,
    build_provenance,
    hardware_details,
    require_clean,
    require_revision,
    require_unchanged_artifact,
    source_state,
)
from vfy.native import build_profile

if TYPE_CHECKING:
    from benchmarks.benchmark_types import TimingSummary


WORKLOADS = ("synchronous", "mixed", "parametrized", "fixture-heavy", "short-async")
_COLLECTION_SUMMARY = re.compile(r"(?:no tests|\d+ tests?) collected in \d+(?:\.\d+)?s")


CommandResults = TypedDict(
    "CommandResults",
    {"pytest-asyncio": "TimingSummary", "vfy": "TimingSummary", "vfy_speedup": float},
)


class WorkloadResults(TypedDict):
    direct_commands: dict[str, list[str]]
    collection_commands: dict[str, list[str]]
    workflow_commands: dict[str, list[str]]
    plugin_collection: NotRequired[CommandResults | None]
    direct_execution: NotRequired[CommandResults | None]
    full_workflow: NotRequired[CommandResults | None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", choices=("all", *WORKLOADS), default="all")
    parser.add_argument("--tests", type=int, default=200)
    add_sampling_arguments(parser)
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.diagnostic:
        args.mode = "diagnostic"
    if args.tests < 1 or args.runs < 1 or args.warmups < 0:
        parser.error("--tests and --runs must be positive; --warmups must be nonnegative")
    return args


def create_fixture(
    root: Path,
    workload: str,
    test_count: int,
    fixture_module: str,
    fixture_decorator: str = "fixture",
) -> Path:
    package = root / "package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "workload.py").write_text(
        "import asyncio\n"
        "\n"
        "def sync_value(value):\n"
        "    return value + 1\n"
        "\n"
        "async def async_value(value):\n"
        "    await asyncio.sleep(0)\n"
        "    return value + 1\n",
        encoding="utf-8",
    )
    lines = [
        "import asyncio\n",
        f"import {fixture_module}\n",
        "import pytest\n",
        "from package.workload import async_value, sync_value\n",
        "\n",
    ]
    if workload == "fixture-heavy":
        lines.extend(
            [
                f"@{fixture_module}.{fixture_decorator}\n",
                "async def client():\n",
                "    await asyncio.sleep(0)\n",
                "    yield object()\n",
                "\n",
                f"@{fixture_module}.{fixture_decorator}\n",
                "async def resource(client):\n",
                "    await asyncio.sleep(0)\n",
                "    return client\n",
                "\n",
            ]
        )

    def add_async_test(name: str, value: int, *, fixtures: bool = False) -> None:
        fixture_arguments = ", client, resource" if fixtures else ""
        lines.extend(
            [
                "@pytest.mark.asyncio\n",
                f"async def {name}({fixture_arguments.lstrip(', ')}):\n"
                if fixture_arguments
                else f"async def {name}():\n",
                f"    assert await async_value({value}) == {value + 1}\n",
            ]
        )
        if fixtures:
            lines.append("    assert client is resource\n")
        lines.append("\n")

    def add_sync_test(name: str, value: int) -> None:
        lines.extend(
            [
                f"def {name}():\n",
                f"    assert sync_value({value}) == {value + 1}\n",
                "\n",
            ]
        )

    if workload == "synchronous":
        for index in range(test_count):
            add_sync_test(f"test_sync_{index:04d}", index)
    elif workload == "mixed":
        for index in range(test_count):
            if index % 2:
                add_async_test(f"test_async_{index:04d}", index)
            else:
                add_sync_test(f"test_sync_{index:04d}", index)
    elif workload == "parametrized":
        for index, start in enumerate(range(0, test_count, 16)):
            size = min(16, test_count - start)
            lines.extend(
                [
                    f"@pytest.mark.parametrize('value', range({size}), "
                    "ids=lambda value: f'value-{value}')\n",
                    "@pytest.mark.asyncio\n",
                    f"async def test_parametrized_{index:04d}(value):\n",
                    "    assert await async_value(value) == value + 1\n",
                    "\n",
                ]
            )
    elif workload == "fixture-heavy":
        for index in range(test_count):
            add_async_test(f"test_fixture_{index:04d}", index, fixtures=True)
    else:
        for index in range(test_count):
            add_async_test(f"test_short_{index:04d}", index)
    test_file = root / "test_workload.py"
    test_file.write_text("".join(lines), encoding="utf-8")
    return test_file


def environment(data_file: Path) -> dict[str, str]:
    return benchmark_environment({"COVERAGE_FILE": str(data_file)})


def execution_command(test_file: Path, tool: str) -> list[str]:
    common = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--cov=package",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=",
        str(test_file),
    ]
    if tool == "pytest-asyncio":
        return [
            *common[:3],
            "-p",
            "pytest_cov.plugin",
            "-p",
            "pytest_asyncio.plugin",
            *common[3:],
        ]
    return [
        *common[:3],
        "-p",
        "vfy.pytest_plugin",
        "-p",
        "vfy.asyncio_plugin",
        *common[3:],
    ]


def workflow_command(test_file: Path, tool: str, history_file: Path) -> list[str]:
    if tool == "pytest-asyncio":
        return execution_command(test_file, tool)
    return [
        sys.executable,
        "-m",
        "vfy",
        "test",
        "--engine=pytest-native",
        "--all",
        "-j",
        "1",
        "--history",
        str(history_file),
        "--data-file",
        str(history_file.parent / "workflow.coverage"),
        "--source",
        "package",
        "-q",
        "--cov=package",
        "--cov-branch",
        "--cov-context=test",
        "--cov-report=",
    ]


def collection_command(
    test_file: Path,
    plugin: str,
    *,
    coverage_plugin: str | None = None,
) -> list[str]:
    if coverage_plugin is None:
        coverage_plugin = "pytest_cov.plugin" if plugin == "pytest-asyncio" else "vfy.pytest_plugin"
    asyncio_plugin = "pytest_asyncio.plugin" if plugin == "pytest-asyncio" else "vfy.asyncio_plugin"
    return [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        coverage_plugin,
        "-p",
        asyncio_plugin,
        str(test_file),
    ]


def run(command: Iterable[str], *, cwd: Path, env: dict[str, str], quiet: bool = True) -> str:
    del quiet
    return run_checked(list(command), cwd=cwd, env=env).stdout


def collection_ids(output: str) -> tuple[str, ...]:
    """Return pytest's collected node IDs without timing-dependent output."""
    return tuple(
        line for line in output.splitlines() if line and _COLLECTION_SUMMARY.fullmatch(line) is None
    )


def validate_collection_parity(
    roots: dict[str, Path],
    command_set: dict[str, list[str]],
    data_files: dict[str, Path],
) -> None:
    collections = {
        tool: collection_ids(run(command, cwd=roots[tool], env=environment(data_files[tool])))
        for tool, command in command_set.items()
    }
    if collections["pytest-asyncio"] != collections["vfy"]:
        raise RuntimeError(
            "asyncio collection IDs differ: "
            f"pytest-asyncio={collections['pytest-asyncio']!r}, "
            f"vfy={collections['vfy']!r}"
        )


def normalized_data(
    path: Path, root: Path
) -> dict[
    str,
    tuple[
        tuple[int, ...] | None,
        tuple[tuple[int, int], ...] | None,
        tuple[tuple[int, tuple[str, ...]], ...],
    ],
]:
    if not path.is_file():
        raise RuntimeError(f"coverage data was not produced: {path}")
    data = coverage.CoverageData(str(path))
    data.read()
    if not data.has_arcs():
        raise RuntimeError(f"branch coverage data was not produced: {path}")
    if not any(context.endswith("|run") for context in data.measured_contexts()):
        raise RuntimeError(f"per-test execution contexts were not produced: {path}")
    normalized: dict[
        str,
        tuple[
            tuple[int, ...] | None,
            tuple[tuple[int, int], ...] | None,
            tuple[tuple[int, tuple[str, ...]], ...],
        ],
    ] = {}
    for filename in data.measured_files():
        relative = Path(filename)
        if relative.is_absolute():
            relative = relative.relative_to(root)
        lines = data.lines(filename)
        arcs = data.arcs(filename)
        contexts = tuple(
            sorted(
                (line, tuple(sorted(contexts)))
                for line, contexts in data.contexts_by_lineno(filename).items()
            )
        )
        normalized[relative.as_posix()] = (
            tuple(sorted(lines)) if lines is not None else None,
            tuple(sorted(arcs)) if arcs is not None else None,
            contexts,
        )
    return normalized


def validate_parity(
    roots: dict[str, Path],
    test_files: dict[str, Path],
    data_files: dict[str, Path],
) -> None:
    validate_collection_parity(
        roots,
        {tool: collection_command(test_files[tool], tool) for tool in data_files},
        data_files,
    )
    datasets = {tool: normalized_data(path, roots[tool]) for tool, path in data_files.items()}
    for tool, dataset in datasets.items():
        workload = dataset.get("package/workload.py")
        if workload is None or not workload[0] or not workload[1]:
            raise RuntimeError(f"{tool} did not measure the workload's lines and arcs")
    if datasets["pytest-asyncio"] != datasets["vfy"]:
        raise RuntimeError("asyncio coverage data differs from pytest-asyncio")


def benchmark_commands(
    command_set: dict[str, list[str]],
    roots: dict[str, Path],
    data_files: dict[str, Path],
    args: argparse.Namespace,
    *,
    reset: Callable[[str], None] | None = None,
) -> CommandResults:
    samples: dict[str, list[float]] = {tool: [] for tool in command_set}
    for order in rotated_orders(tuple(command_set), args.warmups):
        for tool in order:
            command = command_set[tool]
            if reset is not None:
                reset(tool)
            data_files[tool].unlink(missing_ok=True)
            run(command, cwd=roots[tool], env=environment(data_files[tool]))
    for order in rotated_orders(tuple(command_set), args.runs):
        for tool in order:
            if reset is not None:
                reset(tool)
            data_files[tool].unlink(missing_ok=True)
            samples[tool].append(
                timed_command(
                    command_set[tool],
                    cwd=roots[tool],
                    env=environment(data_files[tool]),
                )
            )
    reference = summarize(samples["pytest-asyncio"])
    actual = summarize(samples["vfy"])
    return {
        "pytest-asyncio": reference,
        "vfy": actual,
        "vfy_speedup": reference["median_ms"] / actual["median_ms"],
    }


def benchmark_workload(root: Path, workload: str, args: argparse.Namespace) -> WorkloadResults:
    print(f"{workload}: validating collection and coverage parity", file=sys.stderr, flush=True)
    workload_root = root / workload
    workload_root.mkdir()
    tool_roots = {
        "pytest-asyncio": workload_root / "pytest-asyncio",
        "vfy": workload_root / "vfy",
    }
    for tool_root in tool_roots.values():
        tool_root.mkdir()
    test_files = {
        "pytest-asyncio": create_fixture(
            tool_roots["pytest-asyncio"],
            workload,
            args.tests,
            "pytest_asyncio",
        ),
        "vfy": create_fixture(tool_roots["vfy"], workload, args.tests, "vfy", "async_fixture"),
    }
    data_files = {
        "pytest-asyncio": workload_root / "coverage-pytest-asyncio",
        "vfy": workload_root / "coverage-vfy",
    }
    direct_commands = {tool: execution_command(test_files[tool], tool) for tool in test_files}
    collection_command_set = {
        tool: collection_command(
            test_files[tool],
            tool,
            coverage_plugin="vfy.pytest_plugin",
        )
        for tool in direct_commands
    }
    collection_data_files = {
        tool: workload_root / f"collection-{tool}" for tool in collection_command_set
    }
    for _ in range(2):
        for tool in direct_commands:
            data_files[tool].unlink(missing_ok=True)
            run(direct_commands[tool], cwd=tool_roots[tool], env=environment(data_files[tool]))
    validate_parity(tool_roots, test_files, data_files)
    validate_collection_parity(
        tool_roots,
        collection_command_set,
        collection_data_files,
    )
    if args.validate_only:
        return {
            "direct_commands": direct_commands,
            "collection_commands": collection_command_set,
            "workflow_commands": {},
        }

    print(f"{workload}: timing plugin collection", file=sys.stderr, flush=True)
    collection_results = benchmark_commands(
        collection_command_set, tool_roots, collection_data_files, args
    )
    print(f"{workload}: timing direct execution", file=sys.stderr, flush=True)
    direct_results = benchmark_commands(direct_commands, tool_roots, data_files, args)

    workflow_history = {
        tool: tool_roots[tool] / ".vfy" / "test-history.json" for tool in tool_roots
    }
    workflow_data = {
        "pytest-asyncio": workload_root / "workflow-pytest-asyncio",
        "vfy": tool_roots["vfy"] / ".vfy" / "workflow.coverage",
    }
    workflow_commands = {
        tool: workflow_command(test_files[tool], tool, workflow_history[tool])
        for tool in tool_roots
    }

    def reset_workflow(tool: str) -> None:
        history = workflow_history[tool]
        for path in (history, history.parent / "replay.coverage"):
            path.unlink(missing_ok=True)

    for tool, command in workflow_commands.items():
        reset_workflow(tool)
        workflow_data[tool].unlink(missing_ok=True)
        run(command, cwd=tool_roots[tool], env=environment(workflow_data[tool]))
    workflow_datasets = {
        tool: normalized_data(path, tool_roots[tool]) for tool, path in workflow_data.items()
    }
    if workflow_datasets["pytest-asyncio"] != workflow_datasets["vfy"]:
        raise RuntimeError("full-workflow coverage data differs from pytest-asyncio")
    print(f"{workload}: timing full workflow", file=sys.stderr, flush=True)
    workflow_results = benchmark_commands(
        workflow_commands,
        tool_roots,
        workflow_data,
        args,
        reset=reset_workflow,
    )
    return {
        "direct_commands": direct_commands,
        "collection_commands": collection_command_set,
        "workflow_commands": workflow_commands,
        "plugin_collection": collection_results,
        "direct_execution": direct_results,
        "full_workflow": workflow_results,
    }


def main() -> int:
    args = parse_args()
    sampling = sampling_config(args)
    artifact_before = (
        artifact_provenance(strict=True)
        if sampling.publishable and not args.validate_only
        else None
    )
    repository = Path(__file__).resolve().parents[1]
    source_before = require_clean(repository) if sampling.publishable else source_state(repository)
    if sampling.publishable and build_profile() != "release":
        raise SystemExit("publication benchmarks require a release-mode vfy build")
    workloads = WORKLOADS if args.workload == "all" else (args.workload,)
    with tempfile.TemporaryDirectory(prefix="vfy-asyncio-benchmark-") as temporary:
        root = Path(temporary).resolve()
        results = {workload: benchmark_workload(root, workload, args) for workload in workloads}
    source_after = source_state(repository)
    if args.validate_only:
        record = {
            "benchmark": "asyncio",
            "validation_only": True,
            "workloads": results,
            "provenance": build_provenance(
                repository,
                workload={"workloads": list(workloads), "tests": args.tests},
                workers=1,
                cache="validation only",
                packages=("coverage", "pytest", "pytest-asyncio", "pytest-cov"),
                publication=False,
            ),
        }
        rendered = json.dumps(record, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    record = {
        "benchmark": "asyncio",
        "methodology": {
            "workloads": list(workloads),
            "tests_per_workload": args.tests,
            **sampling.as_dict(),
            "order": "rotating by measured run",
            "coverage_settings": "--cov=package --cov-branch --cov-context=test",
            "plugin_autoload": "disabled; pytest-asyncio and vfy loaded explicitly",
            "parity": "collection IDs and schema-7 lines/arcs/contexts must match before timing",
            "collection_comparison": (
                "same vfy.pytest_plugin coverage plug-in; only asyncio plug-in varies"
            ),
            "direct_comparison": "pytest plus each tool's coverage and asyncio plug-ins",
            "workflow_comparison": (
                "vfy test --all -j 1 versus pytest-asyncio plus pytest-cov; "
                "history and replay state reset before every vfy run"
            ),
            "build": "release vfy extension",
        },
        "environment": {
            **hardware_details(),
            "coverage.py": coverage.__version__,
            "vfy": vfy.__version__,
            "vfy_native_profile": build_profile(),
            "pytest": version("pytest"),
            "pytest-asyncio": version("pytest-asyncio"),
            "pytest-cov": version("pytest-cov"),
            "revision": source_before.revision,
            "working_tree_dirty": source_before.dirty,
            "revision_after": source_after.revision,
            "working_tree_dirty_after": source_after.dirty,
        },
        "benchmarks": results,
    }
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    require_revision(repository, source_before.revision)
    record["provenance"] = {
        **build_provenance(
            repository,
            workload={"workloads": list(workloads), "tests": args.tests},
            workers=1,
            cache="warm-filesystem; workflow history reset per sample",
            packages=("coverage", "pytest", "pytest-asyncio", "pytest-cov"),
            publication=sampling.publishable,
        ),
        "starting_source": source_before.as_dict(),
        "ending_source": source_after.as_dict(),
    }
    rendered = json.dumps(record, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
