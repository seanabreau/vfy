#!/usr/bin/env python3
"""Parity-gated end-to-end benchmarks for Python coverage tools."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from importlib.metadata import version as distribution_version
from pathlib import Path

# Direct script execution needs the checkout root for package-qualified imports.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import coverage
import pytest_cov
from coverage import CoverageData

import vfy
from benchmarks.benchmark_harness import (
    add_sampling_arguments,
    benchmark_environment,
    publishable_command,
    rotated_orders,
    run_checked,
    sampling_config,
    summarize,
    timed as timed_command,
    timed_process as timed_process_command,
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
from vfy.native import build_profile, parser_backend

SLIPCOVER_SUPPORTED = sys.version_info < (3, 15)

SOURCE_TEMPLATE = """\
def compute(value: int) -> int:
    total = 0
    for index in range(10):
        if index % 2:
            total += value + index
        else:
            total -= value - index
    return total


RESULT = compute({value})
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=500)
    parser.add_argument("--shards", type=int, default=16)
    parser.add_argument(
        "--report-formats",
        nargs="+",
        choices=("terminal", "json", "xml"),
        default=["terminal", "json", "xml"],
    )
    add_sampling_arguments(parser)
    parser.add_argument("--diagnostic", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--validate-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if len(set(args.report_formats)) != len(args.report_formats):
        parser.error("--report-formats cannot contain duplicates")
    if args.diagnostic:
        args.mode = "diagnostic"
    return args


def create_fixture(root: Path, file_count: int) -> tuple[Path, Path, Path, Path]:
    package = root / "package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    measured: dict[str, set[int]] = {}
    for index in range(file_count):
        source = package / f"module_{index:05d}.py"
        source.write_text(SOURCE_TEMPLATE.format(value=index), encoding="utf-8")
        measured[str(source)] = {1, 2, 3, 4, 5, 7, 8, 11}
    data_file = root / ".coverage"
    data = CoverageData(str(data_file))
    data.add_lines(measured)
    data.write()
    driver = root / "run_fixture.py"
    driver.write_text(
        "import importlib\n"
        f"for index in range({file_count}):\n"
        "    importlib.import_module(f'package.module_{index:05d}')\n",
        encoding="utf-8",
    )
    pytest_driver = root / "test_fixture.py"
    pytest_driver.write_text(
        "import importlib\n\n"
        "def test_fixture():\n"
        f"    for index in range({file_count}):\n"
        "        importlib.import_module(f'package.module_{index:05d}')\n",
        encoding="utf-8",
    )
    return data_file, package, driver, pytest_driver


def create_shards(root: Path, data_file: Path, shard_count: int = 16) -> Path:
    source_data = CoverageData(str(data_file))
    source_data.read()
    shard_directory = root / "shards"
    shard_directory.mkdir()
    for index in range(shard_count):
        shard = CoverageData(str(shard_directory / f".coverage.{index:03d}"))
        shard.set_context(f"worker-{index:03d}")
        shard.add_lines(
            {
                filename: {lines[index % len(lines)]}
                for filename in source_data.measured_files()
                if (lines := source_data.lines(filename))
            }
        )
        shard.write()
    return shard_directory


def run(
    command: list[str], *, cwd: Path, environment_overrides: dict[str, str] | None = None
) -> str:
    return run_checked(command, cwd=cwd, env=benchmark_environment(environment_overrides)).stdout


def timed_with_stdout(
    command: list[str], *, cwd: Path, environment_overrides: dict[str, str] | None = None
) -> tuple[float, str]:
    elapsed, completed = timed_process_command(
        command, cwd=cwd, env=benchmark_environment(environment_overrides)
    )
    if completed.returncode:
        rendered = " ".join(command)
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"benchmark command failed ({completed.returncode}): {rendered}\n{detail}"
        )
    return elapsed, completed.stdout


def timed(
    command: list[str], *, cwd: Path, environment_overrides: dict[str, str] | None = None
) -> float:
    return timed_command(
        command,
        cwd=cwd,
        env=benchmark_environment(environment_overrides),
    )


def remove_tool_output(tool: str, outputs: dict[str, Path]) -> None:
    output = outputs.get(tool)
    if output is not None:
        output.unlink(missing_ok=True)


def _record_report_output(
    tool: str,
    stdout: str,
    pending: dict[str, object],
    *,
    output_reader: Callable[[str, str], object] | None,
    output_validator: Callable[[object, object], None] | None,
) -> None:
    if output_reader is None or tool not in {"coverage.py", "vfy"}:
        return
    if output_validator is None:
        raise AssertionError("report output validator is missing")
    pending[tool] = output_reader(tool, stdout)
    if "coverage.py" in pending and "vfy" in pending:
        output_validator(pending["coverage.py"], pending["vfy"])
        pending.clear()


def compare(
    name: str,
    coverage_command: list[str],
    vfy_command: list[str],
    *,
    cwd: Path,
    warmups: int,
    runs: int,
    before_each: Callable[[str], None] | None = None,
    after_each: Callable[[str], None] | None = None,
    baseline_command: list[str] | None = None,
    output_reader: Callable[[str, str], object] | None = None,
    output_validator: Callable[[object, object], None] | None = None,
) -> dict[str, object]:
    if (output_reader is None) != (output_validator is None):
        raise ValueError("report output reader and validator must be supplied together")

    commands = [("coverage.py", coverage_command), ("vfy", vfy_command)]
    if baseline_command is not None:
        commands.append(("python", baseline_command))
    for order in rotated_orders([tool for tool, _ in commands], warmups):
        commands_by_tool = dict(commands)
        warmup_outputs: dict[str, object] = {}
        for tool in order:
            command = commands_by_tool[tool]
            if before_each:
                before_each(tool)
            stdout = run(command, cwd=cwd)
            _record_report_output(
                tool,
                stdout,
                warmup_outputs,
                output_reader=output_reader,
                output_validator=output_validator,
            )

    samples = {tool: [] for tool, _ in commands}
    commands_by_tool = dict(commands)
    for order in rotated_orders([tool for tool, _ in commands], runs):
        sample_outputs: dict[str, object] = {}
        for tool in order:
            command = commands_by_tool[tool]
            if before_each:
                before_each(tool)
            if output_reader is None:
                elapsed = timed(command, cwd=cwd)
            else:
                elapsed, stdout = timed_with_stdout(command, cwd=cwd)
                _record_report_output(
                    tool,
                    stdout,
                    sample_outputs,
                    output_reader=output_reader,
                    output_validator=output_validator,
                )
            samples[tool].append(elapsed)
            if after_each:
                after_each(tool)

    coverage_result = summarize(samples["coverage.py"])
    vfy_result = summarize(samples["vfy"])

    result = {
        "name": name,
        "commands": {
            "coverage.py": publishable_command(coverage_command, cwd=cwd),
            "vfy": publishable_command(vfy_command, cwd=cwd),
            **(
                {"python": publishable_command(baseline_command, cwd=cwd)}
                if baseline_command
                else {}
            ),
        },
        "coverage.py": coverage_result,
        "vfy": vfy_result,
        "speedup": coverage_result["median_ms"] / vfy_result["median_ms"],
    }
    if baseline_command is not None:
        baseline = summarize(samples["python"])
        result["python"] = baseline
        result["overhead_percent"] = {
            "coverage.py": (coverage_result["median_ms"] / baseline["median_ms"] - 1) * 100,
            "vfy": (vfy_result["median_ms"] / baseline["median_ms"] - 1) * 100,
        }
    return result


def compare_tools(
    name: str,
    commands: list[tuple[str, list[str]]],
    *,
    cwd: Path,
    warmups: int,
    runs: int,
    before_each: Callable[[str], None] | None = None,
    output_validator: Callable[[str, str], None] | None = None,
    environments: dict[str, dict[str, str]] | None = None,
    baseline: str | None = None,
) -> dict[str, object]:
    environments = environments or {}
    for order in rotated_orders([tool for tool, _ in commands], warmups):
        commands_by_tool = dict(commands)
        for tool in order:
            command = commands_by_tool[tool]
            if before_each:
                before_each(tool)
            stdout = run(command, cwd=cwd, environment_overrides=environments.get(tool))
            if output_validator:
                output_validator(tool, stdout)

    samples = {tool: [] for tool, _ in commands}
    commands_by_tool = dict(commands)
    for order in rotated_orders([tool for tool, _ in commands], runs):
        for tool in order:
            command = commands_by_tool[tool]
            if before_each:
                before_each(tool)
            elapsed, stdout = timed_with_stdout(
                command, cwd=cwd, environment_overrides=environments.get(tool)
            )
            if output_validator:
                output_validator(tool, stdout)
            samples[tool].append(elapsed)

    summaries = {tool: summarize(tool_samples) for tool, tool_samples in samples.items()}
    vfy_median = summaries["vfy pytest"]["median_ms"]
    result: dict[str, object] = {
        "name": name,
        "commands": {tool: publishable_command(command, cwd=cwd) for tool, command in commands},
        **summaries,
        "vfy_speedup": {
            tool: summary["median_ms"] / vfy_median
            for tool, summary in summaries.items()
            if tool not in {"vfy pytest", baseline}
        },
    }
    if baseline:
        baseline_median = summaries[baseline]["median_ms"]
        result["overhead_percent"] = {
            tool: (summary["median_ms"] / baseline_median - 1) * 100
            for tool, summary in summaries.items()
            if tool != baseline
        }
    return result


def validate_json_outputs(root: Path, data_file: Path) -> None:
    coverage_output = root / "coverage-reference.json"
    vfy_output = root / "coverage-vfy.json"
    run(
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "--data-file",
            str(data_file),
            "-o",
            str(coverage_output),
        ],
        cwd=root,
    )
    run(
        [
            sys.executable,
            "-m",
            "vfy",
            "coverage",
            "json",
            "--data-file",
            str(data_file),
            "-o",
            str(vfy_output),
        ],
        cwd=root,
    )
    assert_json_report_parity(
        json.loads(coverage_output.read_text(encoding="utf-8")),
        json.loads(vfy_output.read_text(encoding="utf-8")),
        root,
    )


def validate_terminal_outputs(root: Path, data_file: Path) -> None:
    common = ["--data-file", str(data_file)]
    reference = run([sys.executable, "-m", "coverage", "report", *common], cwd=root)
    actual = run([sys.executable, "-m", "vfy", "coverage", "report", *common], cwd=root)
    assert_terminal_report_parity(reference, actual, root)


def validate_xml_outputs(root: Path, data_file: Path) -> None:
    reference_path = root / "coverage-reference.xml"
    actual_path = root / "coverage-vfy.xml"
    common = ["--data-file", str(data_file)]
    run(
        [sys.executable, "-m", "coverage", "xml", *common, "-o", str(reference_path)],
        cwd=root,
    )
    run(
        [sys.executable, "-m", "vfy", "coverage", "xml", *common, "-o", str(actual_path)],
        cwd=root,
    )
    assert_xml_report_parity(
        reference_path.read_text(encoding="utf-8"),
        actual_path.read_text(encoding="utf-8"),
    )


def validate_combine_outputs(root: Path, shard_directory: Path) -> tuple[Path, Path]:
    outputs = {
        "coverage.py": root / "combined-coverage" / ".coverage",
        "vfy": root / "combined-vfy" / ".coverage",
    }
    for output in outputs.values():
        output.parent.mkdir()
    for tool, module in (("coverage.py", "coverage"), ("vfy", "vfy")):
        command = [sys.executable, "-m", module]
        if tool == "vfy":
            command.append("coverage")
        command.extend(
            [
                "combine",
                "--keep",
                "--data-file",
                str(outputs[tool]),
                str(shard_directory),
            ]
        )
        run(command, cwd=root)
    reference = CoverageData(str(outputs["coverage.py"]))
    actual = CoverageData(str(outputs["vfy"]))
    reference.read()
    actual.read()
    if reference.measured_files() != actual.measured_files() or any(
        reference.lines(filename) != actual.lines(filename)
        for filename in reference.measured_files()
    ):
        raise RuntimeError("combined line data differs from Coverage.py")
    for output in outputs.values():
        output.unlink()
    return outputs["coverage.py"], outputs["vfy"]


def collection_commands(
    root: Path, package: Path, driver: Path, *, branch: bool
) -> tuple[list[str], list[str], Path, Path]:
    reference_data = root / (
        ".coverage-collection-reference-branch" if branch else ".coverage-collection-reference-line"
    )
    vfy_data = root / (
        ".coverage-collection-vfy-branch" if branch else ".coverage-collection-vfy-line"
    )
    branch_argument: list[str] = ["--branch"] if branch else []
    reference = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        *branch_argument,
        "--data-file",
        str(reference_data),
        "--source",
        str(package),
        str(driver),
    ]
    actual = [
        sys.executable,
        "-m",
        "vfy",
        "coverage",
        "run",
        *branch_argument,
        "--data-file",
        str(vfy_data),
        "--source",
        str(package),
        str(driver),
    ]
    return reference, actual, reference_data, vfy_data


def validate_collection_outputs(reference_path: Path, actual_path: Path) -> None:
    reference = CoverageData(str(reference_path))
    actual = CoverageData(str(actual_path))
    reference.read()
    actual.read()
    if reference.has_arcs() != actual.has_arcs():
        raise RuntimeError("collection modes differ")
    if reference.measured_files() != actual.measured_files():
        raise RuntimeError("collection measured-file sets differ")
    for filename in reference.measured_files():
        if reference.lines(filename) != actual.lines(filename):
            raise RuntimeError(
                f"collected lines differ for {filename}: "
                f"Coverage.py={reference.lines(filename)!r}, vfy={actual.lines(filename)!r}"
            )
        if reference.has_arcs() and reference.arcs(filename) != actual.arcs(filename):
            raise RuntimeError(
                f"collected arcs differ for {filename}: "
                f"Coverage.py={reference.arcs(filename)!r}, vfy={actual.arcs(filename)!r}"
            )


def pytest_commands(
    root: Path, package: Path, pytest_driver: Path, *, branch: bool, json_output: bool
) -> tuple[list[tuple[str, list[str]]], dict[str, dict[str, str]], dict[str, Path]]:
    branch_argument: list[str] = ["--cov-branch"] if branch else []
    outputs = {
        "pytest-cov": root / f"pytest-cov-{'branch' if branch else 'line'}.json",
        "vfy pytest": root / f"vfy-pytest-{'branch' if branch else 'line'}.json",
        "slipcover": root / f"slipcover-{'branch' if branch else 'line'}.json",
    }
    pytest_report: dict[str, list[str]] = {
        tool: [f"--cov-report=json:{outputs[tool]}"] if json_output else ["--cov-report=term"]
        for tool in ("pytest-cov", "vfy pytest")
    }
    commands: list[tuple[str, list[str]]] = [
        (
            "pytest-cov",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "pytest_cov.plugin",
                f"--cov={package}",
                *branch_argument,
                *pytest_report["pytest-cov"],
                str(pytest_driver),
            ],
        ),
        (
            "vfy pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "vfy.pytest_plugin",
                f"--cov={package}",
                *branch_argument,
                *pytest_report["vfy pytest"],
                str(pytest_driver),
            ],
        ),
        (
            "slipcover",
            [
                sys.executable,
                "-m",
                "slipcover",
                *(["--branch"] if branch else []),
                "--source",
                str(package),
                *(["--json", "--out", str(outputs["slipcover"])] if json_output else []),
                "-m",
                "pytest",
                "-q",
                str(pytest_driver),
            ],
        ),
        (
            "plain pytest",
            [sys.executable, "-m", "pytest", "-q", str(pytest_driver)],
        ),
    ]
    if not SLIPCOVER_SUPPORTED:
        commands = [(tool, command) for tool, command in commands if tool != "slipcover"]
        del outputs["slipcover"]
    environments = {
        "pytest-cov": {"COVERAGE_FILE": str(root / ".coverage-pytest-cov")},
        "vfy pytest": {"COVERAGE_FILE": str(root / ".coverage-vfy-pytest")},
    }
    return commands, environments, outputs


def normalized_json_files(payload: dict[str, object], root: Path) -> dict[str, dict[str, object]]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, dict):
        raise RuntimeError("benchmark JSON has no file mapping")
    normalized: dict[str, dict[str, object]] = {}
    for filename, raw_data in raw_files.items():
        if not isinstance(filename, str) or not isinstance(raw_data, dict):
            raise RuntimeError("benchmark JSON contains an invalid file entry")
        path = Path(filename)
        if path.is_absolute():
            path = path.relative_to(root)
        normalized[path.as_posix()] = raw_data
    return normalized


def assert_json_report_parity(reference: object, actual: object, root: Path) -> None:
    if not isinstance(reference, dict) or not isinstance(actual, dict):
        raise RuntimeError("JSON report output is not an object")
    reference_totals = reference.get("totals")
    actual_totals = actual.get("totals")
    if not isinstance(reference_totals, dict) or not isinstance(actual_totals, dict):
        raise RuntimeError("JSON report has no totals object")
    if reference_totals != actual_totals:
        raise RuntimeError("JSON report totals differ")
    if normalized_json_files(reference, root) != normalized_json_files(actual, root):
        raise RuntimeError("JSON report per-file output differs")


def _terminal_report_rows(report: str, root: Path) -> list[tuple[str, int, int, Decimal]]:
    rows: list[tuple[str, int, int, Decimal]] = []
    found_header = False
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) == {"-"}:
            continue
        tokens = stripped.split()
        if tokens[0] == "Name":
            if tokens[:4] != ["Name", "Stmts", "Miss", "Cover"] or set(tokens[4:]) - {"Missing"}:
                raise RuntimeError("terminal report has an unsupported header")
            found_header = True
            continue

        row = None
        for percent_index in range(2, len(tokens)):
            if not tokens[percent_index].endswith("%"):
                continue
            statements, missing = tokens[percent_index - 2 : percent_index]
            if not statements.isdecimal() or not missing.isdecimal():
                continue
            filename = " ".join(tokens[: percent_index - 2])
            if not filename:
                continue
            try:
                coverage = Decimal(tokens[percent_index][:-1])
            except InvalidOperation as error:
                raise RuntimeError("terminal report has an invalid coverage percentage") from error
            path = Path(filename)
            if path.is_absolute():
                try:
                    filename = path.relative_to(root).as_posix()
                except ValueError:
                    filename = path.as_posix()
            else:
                filename = path.as_posix()
            row = (filename, int(statements), int(missing), coverage)
            break
        if row is None:
            raise RuntimeError("terminal report contains an invalid row")
        rows.append(row)
    if not found_header or not rows or rows[-1][0] != "TOTAL":
        raise RuntimeError("terminal report is missing its header or total row")
    return rows


def assert_terminal_report_parity(reference: object, actual: object, root: Path) -> None:
    if not isinstance(reference, str) or not isinstance(actual, str):
        raise RuntimeError("terminal report output is not text")
    if _terminal_report_rows(reference, root) != _terminal_report_rows(actual, root):
        raise RuntimeError("terminal report output differs")


_XML_NUMERIC_ATTRIBUTES = frozenset(
    {
        "line-rate",
        "branch-rate",
        "lines-valid",
        "lines-covered",
        "branches-valid",
        "branches-covered",
        "complexity",
        "number",
        "hits",
    }
)


def _xml_report_signature(element: ET.Element, *, is_root: bool = False) -> tuple[object, ...]:
    attributes: list[tuple[str, object]] = []
    for name, value in element.attrib.items():
        if is_root and name in {"timestamp", "version"}:
            continue
        if name in _XML_NUMERIC_ATTRIBUTES:
            try:
                normalized_value: object = Decimal(value)
            except InvalidOperation as error:
                raise RuntimeError(f"XML report has invalid numeric attribute {name!r}") from error
        else:
            normalized_value = value
        attributes.append((name, normalized_value))
    text = element.text.strip() if element.text and element.text.strip() else ""
    children = tuple(_xml_report_signature(child) for child in element)
    return element.tag, tuple(sorted(attributes)), text, children


def assert_xml_report_parity(reference: object, actual: object) -> None:
    if not isinstance(reference, str) or not isinstance(actual, str):
        raise RuntimeError("XML report output is not text")
    try:
        reference_signature = _xml_report_signature(ET.fromstring(reference), is_root=True)
        actual_signature = _xml_report_signature(ET.fromstring(actual), is_root=True)
    except ET.ParseError as error:
        raise RuntimeError("XML report output is invalid") from error
    if reference_signature != actual_signature:
        raise RuntimeError("XML report output differs")


def _read_report_file(paths: dict[str, Path], tool: str) -> str:
    try:
        return paths[tool].read_text(encoding="utf-8")
    except (KeyError, OSError) as error:
        raise RuntimeError(f"missing report output for {tool}") from error


def _read_json_report_file(paths: dict[str, Path], tool: str) -> object:
    try:
        payload: object = json.loads(paths[tool].read_text(encoding="utf-8"))
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid JSON report output for {tool}") from error
    return payload


def validate_pytest_outputs(
    root: Path, package: Path, pytest_driver: Path, *, branch: bool
) -> tuple[list[tuple[str, list[str]]], dict[str, dict[str, str]]]:
    commands, environments, outputs = pytest_commands(
        root, package, pytest_driver, branch=branch, json_output=True
    )
    for tool, command in commands:
        if tool == "plain pytest":
            continue
        outputs[tool].unlink(missing_ok=True)
        run(command, cwd=root, environment_overrides=environments.get(tool))

    payloads = {
        tool: json.loads(output.read_text(encoding="utf-8")) for tool, output in outputs.items()
    }
    files = {tool: normalized_json_files(payload, root) for tool, payload in payloads.items()}
    reference_files = files["pytest-cov"]
    for tool, tool_files in files.items():
        if tool_files.keys() != reference_files.keys():
            raise RuntimeError(
                f"pytest measured-file sets differ: pytest-cov={sorted(reference_files)!r}, "
                f"{tool}={sorted(tool_files)!r}"
            )
        for filename, reference in reference_files.items():
            actual = tool_files[filename]
            for field in ("executed_lines", "missing_lines"):
                if actual[field] != reference[field]:
                    raise RuntimeError(f"pytest {field} differs for {tool} in {filename}")
            if branch:
                for field in ("executed_branches", "missing_branches"):
                    if actual[field] != reference[field]:
                        raise RuntimeError(f"pytest {field} differs for {tool} in {filename}")

    terminal_commands, terminal_environments, _ = pytest_commands(
        root, package, pytest_driver, branch=branch, json_output=False
    )
    return terminal_commands, terminal_environments


def remove_pytest_output(tool: str, root: Path) -> None:
    outputs = {
        "pytest-cov": root / ".coverage-pytest-cov",
        "vfy pytest": root / ".coverage-vfy-pytest",
    }
    remove_tool_output(tool, outputs)


def _pytest_terminal_rows(stdout: str, tool: str) -> tuple[tuple[str, ...], ...]:
    header, total = ("File", "(summary)") if tool == "slipcover" else ("Name", "TOTAL")
    rows: list[tuple[str, ...]] = []
    for line in stdout.splitlines():
        tokens = tuple(line.split())
        if not tokens:
            continue
        if not rows:
            if tokens[0] == header:
                rows.append(tokens)
            continue
        if all(set(token) == {"-"} for token in tokens):
            continue
        rows.append(tokens)
        if tokens[0] == total:
            return tuple(rows)
    raise RuntimeError(f"missing pytest terminal coverage report for {tool}")


def prepare_pytest_sample_validator(
    root: Path,
    commands: list[tuple[str, list[str]]],
    environments: dict[str, dict[str, str]],
    *,
    branch: bool,
) -> Callable[[str, str], None]:
    reference = root / f".coverage-pytest-reference-{'branch' if branch else 'line'}"
    terminal_reports: dict[str, tuple[tuple[str, ...], ...]] = {}
    for tool, command in commands:
        if tool == "plain pytest":
            continue
        remove_pytest_output(tool, root)
        stdout = run(command, cwd=root, environment_overrides=environments.get(tool))
        terminal_reports[tool] = _pytest_terminal_rows(stdout, tool)
        if tool == "pytest-cov":
            # Each mode keeps an immutable oracle; measured commands reuse their data paths.
            shutil.copyfile(environments[tool]["COVERAGE_FILE"], reference)
        elif tool == "vfy pytest":
            validate_collection_outputs(reference, Path(environments[tool]["COVERAGE_FILE"]))

    def validate(tool: str, stdout: str) -> None:
        if tool == "plain pytest":
            return
        if tool in environments:
            data_file = Path(environments[tool]["COVERAGE_FILE"])
            if not data_file.is_file():
                raise RuntimeError(f"missing measured pytest coverage data for {tool}")
            validate_collection_outputs(reference, data_file)
        if _pytest_terminal_rows(stdout, tool) != terminal_reports[tool]:
            raise RuntimeError(f"measured pytest terminal coverage report differs for {tool}")

    return validate


def main() -> int:
    args = parse_args()
    sampling = sampling_config(args)
    artifact_before = (
        artifact_provenance(strict=True)
        if sampling.publishable and not args.validate_only
        else None
    )
    repository = Path(__file__).resolve().parents[1]
    starting_state = require_clean(repository) if sampling.publishable else source_state(repository)
    if sampling.publishable and build_profile() != "release":
        raise SystemExit("publication benchmarks require a release-mode vfy build")
    with tempfile.TemporaryDirectory(prefix="vfy-benchmark-") as temporary:
        # Resolve platform aliases (notably macOS /var -> /private/var) so the
        # paths written to Coverage.py's database match its source canonicalization.
        root = Path(temporary).resolve()
        data_file, package, driver, pytest_driver = create_fixture(root, args.files)
        report_validators = {
            "terminal": validate_terminal_outputs,
            "json": validate_json_outputs,
            "xml": validate_xml_outputs,
        }
        for report_format in args.report_formats:
            report_validators[report_format](root, data_file)
        shard_directory = create_shards(root, data_file, args.shards)
        coverage_combined, vfy_combined = validate_combine_outputs(root, shard_directory)
        line_reference, line_vfy, line_reference_data, line_vfy_data = collection_commands(
            root, package, driver, branch=False
        )
        branch_reference, branch_vfy, branch_reference_data, branch_vfy_data = collection_commands(
            root, package, driver, branch=True
        )
        for command, output in (
            (line_reference, line_reference_data),
            (line_vfy, line_vfy_data),
            (branch_reference, branch_reference_data),
            (branch_vfy, branch_vfy_data),
        ):
            output.unlink(missing_ok=True)
            run(command, cwd=root)
        validate_collection_outputs(line_reference_data, line_vfy_data)
        validate_collection_outputs(branch_reference_data, branch_vfy_data)
        pytest_line_commands, pytest_line_environments = validate_pytest_outputs(
            root, package, pytest_driver, branch=False
        )
        pytest_branch_commands, pytest_branch_environments = validate_pytest_outputs(
            root, package, pytest_driver, branch=True
        )
        validate_pytest_line_sample = prepare_pytest_sample_validator(
            root, pytest_line_commands, pytest_line_environments, branch=False
        )
        validate_pytest_branch_sample = prepare_pytest_sample_validator(
            root, pytest_branch_commands, pytest_branch_environments, branch=True
        )
        if args.validate_only:
            return 0

        def validate_collection_sample(tool: str) -> None:
            del tool
            if line_reference_data.is_file() and line_vfy_data.is_file():
                validate_collection_outputs(line_reference_data, line_vfy_data)
            if branch_reference_data.is_file() and branch_vfy_data.is_file():
                validate_collection_outputs(branch_reference_data, branch_vfy_data)

        common = ["--data-file", str(data_file)]
        json_outputs = {
            "coverage.py": root / "coverage-reference.json",
            "vfy": root / "coverage-vfy.json",
        }
        xml_outputs = {
            "coverage.py": root / "coverage-reference.xml",
            "vfy": root / "coverage-vfy.xml",
        }
        benchmarks = [
            compare(
                "line collection",
                line_reference,
                line_vfy,
                cwd=root,
                warmups=sampling.warmups,
                runs=sampling.runs,
                before_each=lambda tool: remove_tool_output(
                    tool,
                    {"coverage.py": line_reference_data, "vfy": line_vfy_data},
                ),
                after_each=validate_collection_sample,
                baseline_command=[sys.executable, str(driver)],
            ),
            compare(
                "branch collection",
                branch_reference,
                branch_vfy,
                cwd=root,
                warmups=sampling.warmups,
                runs=sampling.runs,
                before_each=lambda tool: remove_tool_output(
                    tool,
                    {"coverage.py": branch_reference_data, "vfy": branch_vfy_data},
                ),
                after_each=validate_collection_sample,
                baseline_command=[sys.executable, str(driver)],
            ),
        ]
        report_benchmarks: dict[str, Callable[[], dict[str, object]]] = {
            "terminal": lambda: compare(
                "terminal report",
                [sys.executable, "-m", "coverage", "report", *common],
                [sys.executable, "-m", "vfy", "coverage", "report", *common],
                cwd=root,
                warmups=sampling.warmups,
                runs=sampling.runs,
                output_reader=lambda _tool, stdout: stdout,
                output_validator=lambda reference, actual: assert_terminal_report_parity(
                    reference, actual, root
                ),
            ),
            "json": lambda: compare(
                "JSON report",
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "json",
                    *common,
                    "-o",
                    str(root / "coverage-reference.json"),
                ],
                [
                    sys.executable,
                    "-m",
                    "vfy",
                    "coverage",
                    "json",
                    *common,
                    "-o",
                    str(root / "coverage-vfy.json"),
                ],
                cwd=root,
                warmups=sampling.warmups,
                runs=sampling.runs,
                before_each=lambda tool: remove_tool_output(tool, json_outputs),
                output_reader=lambda tool, _stdout: _read_json_report_file(json_outputs, tool),
                output_validator=lambda reference, actual: assert_json_report_parity(
                    reference, actual, root
                ),
            ),
            "xml": lambda: compare(
                "XML report",
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "xml",
                    *common,
                    "-o",
                    str(root / "coverage-reference.xml"),
                ],
                [
                    sys.executable,
                    "-m",
                    "vfy",
                    "coverage",
                    "xml",
                    *common,
                    "-o",
                    str(root / "coverage-vfy.xml"),
                ],
                cwd=root,
                warmups=sampling.warmups,
                runs=sampling.runs,
                before_each=lambda tool: remove_tool_output(tool, xml_outputs),
                output_reader=lambda tool, _stdout: _read_report_file(xml_outputs, tool),
                output_validator=assert_xml_report_parity,
            ),
        }
        benchmarks.extend(
            report_benchmarks[report_format]() for report_format in args.report_formats
        )
        benchmarks.extend(
            [
                compare(
                    f"combine {args.shards} shards",
                    [
                        sys.executable,
                        "-m",
                        "coverage",
                        "combine",
                        "--keep",
                        "--data-file",
                        str(coverage_combined),
                        str(shard_directory),
                    ],
                    [
                        sys.executable,
                        "-m",
                        "vfy",
                        "coverage",
                        "combine",
                        "--keep",
                        "--data-file",
                        str(vfy_combined),
                        str(shard_directory),
                    ],
                    cwd=root,
                    warmups=sampling.warmups,
                    runs=sampling.runs,
                    before_each=lambda tool: (
                        coverage_combined if tool == "coverage.py" else vfy_combined
                    ).unlink(missing_ok=True),
                ),
                compare_tools(
                    "pytest line collection + terminal report",
                    pytest_line_commands,
                    cwd=root,
                    warmups=sampling.warmups,
                    runs=sampling.runs,
                    before_each=lambda tool: remove_pytest_output(tool, root),
                    output_validator=validate_pytest_line_sample,
                    environments=pytest_line_environments,
                    baseline="plain pytest",
                ),
                compare_tools(
                    "pytest branch collection + terminal report",
                    pytest_branch_commands,
                    cwd=root,
                    warmups=sampling.warmups,
                    runs=sampling.runs,
                    before_each=lambda tool: remove_pytest_output(tool, root),
                    output_validator=validate_pytest_branch_sample,
                    environments=pytest_branch_environments,
                    baseline="plain pytest",
                ),
            ]
        )

    report_validation = {
        "terminal": "shared per-file Stmts/Miss/Cover rows and totals",
        "json": "totals and complete per-file report objects",
        "xml": "normalized semantic XML report tree",
    }
    result = {
        "benchmark": "report",
        "methodology": {
            "fixture_files": args.files,
            **sampling.as_dict(),
            "order": "rotating to counter ordering and thermal bias",
            "cache": "warm filesystem; empty process-local analysis cache per command",
            "report_formats": args.report_formats,
            "report_validation": {
                report_format: report_validation[report_format]
                for report_format in args.report_formats
            },
            "collection_validation": "measured files, lines, mode, and exact arcs must agree",
            "merge_fixture_shards": args.shards,
            "merge_validation": "measured files and merged line sets must agree",
            "pytest_validation": (
                "JSON preflight: measured files, executed and missing lines, and branch edges "
                "agree across tools; every warmup and measured command: pytest-cov and vfy "
                "saved files, lines, mode, and exact arcs agree with the frozen Coverage.py "
                "reference, and each tool's terminal coverage rows agree with its preflight; "
                "when available, SlipCover validation covers terminal counts, percentages, "
                "and missing entries"
            ),
            "pytest_plugin_loading": "autoload disabled; compared plug-ins loaded explicitly",
        },
        "environment": {
            **hardware_details(),
            "coverage.py": coverage.__version__,
            "pytest-cov": pytest_cov.__version__,
            "slipcover": distribution_version("slipcover") if SLIPCOVER_SUPPORTED else None,
            "vfy": vfy.__version__,
            "vfy_rust_profile": build_profile(),
            "vfy_parser": parser_backend(),
        },
        "benchmarks": benchmarks,
    }
    if artifact_before is not None:
        require_unchanged_artifact(artifact_before)
    require_revision(repository, starting_state.revision)
    result["provenance"] = {
        **build_provenance(
            repository,
            workload={
                "files": args.files,
                "shards": args.shards,
                "report_formats": args.report_formats,
            },
            workers=1,
            cache="warm-filesystem",
            packages=("coverage", "pytest-cov", "slipcover"),
            publication=sampling.publishable,
        ),
        "starting_source": starting_state.as_dict(),
        "ending_source": source_state(repository).as_dict(),
    }
    rendered = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
