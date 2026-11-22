from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks import benchmark_harness, benchmark_report
from benchmarks.benchmark_report import (
    assert_json_report_parity,
    assert_terminal_report_parity,
    assert_xml_report_parity,
)
from coverage import CoverageData


def test_json_report_parity_rejects_changed_files_with_equal_totals(tmp_path: Path) -> None:
    root = tmp_path
    totals = {"covered_lines": 1, "num_statements": 2, "missing_lines": 1}
    reference = {
        "files": {
            "package/a.py": {
                "executed_lines": [1],
                "missing_lines": [2],
                "summary": {"covered_lines": 1, "num_statements": 2, "missing_lines": 1},
            }
        },
        "totals": totals,
    }
    changed = {
        "files": {
            "package/a.py": {
                "executed_lines": [2],
                "missing_lines": [1],
                "summary": {"covered_lines": 1, "num_statements": 2, "missing_lines": 1},
            }
        },
        "totals": totals,
    }

    with pytest.raises(RuntimeError, match="per-file"):
        assert_json_report_parity(reference, changed, root)


def test_terminal_report_parity_normalizes_path_and_optional_missing_column(
    tmp_path: Path,
) -> None:
    reference = "Name Stmts Miss Cover\npackage/a.py 2 1 50%\nTOTAL 2 1 50%\n"
    vfy = (
        "Name Stmts Miss Cover Missing\n"
        "--------------------------------------------------\n"
        f"{tmp_path / 'package/a.py'} 2 1 50% 1\n"
        "--------------------------------------------------\n"
        "TOTAL 2 1 50%\n"
    )

    assert_terminal_report_parity(reference, vfy, tmp_path)


def test_terminal_report_parity_rejects_changed_file_rows_with_equal_total(
    tmp_path: Path,
) -> None:
    reference = "Name Stmts Miss Cover\npackage/a.py 2 1 50%\nTOTAL 2 1 50%\n"
    changed = (
        f"Name Stmts Miss Cover Missing\n{tmp_path / 'package/b.py'} 2 1 50% 1\nTOTAL 2 1 50%\n"
    )

    with pytest.raises(RuntimeError, match="terminal report output differs"):
        assert_terminal_report_parity(reference, changed, tmp_path)


def test_xml_report_parity_ignores_volatile_metadata() -> None:
    reference = (
        '<coverage version="coverage.py 7.15.4" timestamp="100" lines-valid="1" '
        'lines-covered="1" line-rate="1.0"><packages/></coverage>'
    )
    vfy = (
        '<coverage version="vfy 0.0.1" timestamp="200" lines-valid="1" '
        'lines-covered="1" line-rate="1"><packages/></coverage>'
    )

    assert_xml_report_parity(reference, vfy)


def test_xml_report_parity_rejects_changed_file_lines_with_equal_totals() -> None:
    reference = (
        '<coverage timestamp="100" lines-valid="2" lines-covered="1" line-rate="0.5">'
        '<packages><package name="package" line-rate="0.5"><classes>'
        '<class name="a" filename="a.py" line-rate="1"><lines>'
        '<line number="1" hits="1"/></lines></class>'
        '<class name="b" filename="b.py" line-rate="0"><lines>'
        '<line number="1" hits="0"/></lines></class>'
        "</classes></package></packages></coverage>"
    )
    changed = (
        '<coverage timestamp="200" lines-valid="2" lines-covered="1" line-rate="0.5">'
        '<packages><package name="package" line-rate="0.5"><classes>'
        '<class name="a" filename="a.py" line-rate="0"><lines>'
        '<line number="1" hits="0"/></lines></class>'
        '<class name="b" filename="b.py" line-rate="1"><lines>'
        '<line number="1" hits="1"/></lines></class>'
        "</classes></package></packages></coverage>"
    )

    with pytest.raises(RuntimeError, match="XML report output differs"):
        assert_xml_report_parity(reference, changed)


@pytest.mark.parametrize("tool_plugin", ["pytest_cov.plugin", "vfy.pytest_plugin"])
@pytest.mark.parametrize("branch", [False, True])
def test_report_harness_rejects_and_preserves_corrupt_measured_pytest_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_plugin: str, branch: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        benchmark_report.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: contextlib.nullcontext(str(project)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_report.py",
            "--diagnostic",
            "--files",
            "1",
            "--shards",
            "1",
            "--report-formats",
            "terminal",
            "--warmups",
            "0",
            "--runs",
            "1",
            "--output",
            str(output),
        ],
    )
    real_timed_process = benchmark_harness.timed_process
    corrupted: dict[Path, bytes] = {}

    def corrupt_measured_data(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[float, subprocess.CompletedProcess[str]]:
        elapsed, completed = real_timed_process(command, cwd=cwd, env=env, timeout=timeout)
        if (
            tool_plugin in command
            and "--cov-report=term" in command
            and ("--cov-branch" in command) is branch
        ):
            assert completed.returncode == 0, completed.stderr
            assert env is not None
            data_file = Path(env["COVERAGE_FILE"])
            data = CoverageData(str(data_file))
            source = str(project / "package" / "module_00000.py")
            if branch:
                data.read()
                # Keep files and executed lines intact; change only an arc.
                data.add_arcs({source: [(-123, 2)]})
            else:
                data.erase()
                data.add_lines({source: [1]})
            data.write()
            corrupted[data_file] = data_file.read_bytes()
        return elapsed, completed

    monkeypatch.setattr(benchmark_harness, "timed_process", corrupt_measured_data)
    monkeypatch.setattr(benchmark_report, "timed_process_command", corrupt_measured_data)

    diagnostic = "collected arcs differ" if branch else "collection measured-file sets differ"
    with pytest.raises(RuntimeError, match=diagnostic):
        benchmark_report.main()

    assert corrupted, "the measured command must reach the corruption boundary"
    assert {path: path.read_bytes() for path in corrupted} == corrupted
    assert not output.exists()

    # The unchanged workload passes a fresh JSON run, which cannot validate the rejected sample.
    benchmark_report.validate_pytest_outputs(
        project, project / "package", project / "test_fixture.py", branch=branch
    )


def test_validate_only_checks_terminal_pytest_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["benchmark_report.py", "--diagnostic", "--validate-only", "--files", "1"]
    )
    real_run = benchmark_report.run
    terminal_commands: list[list[str]] = []

    def omit_terminal_report(
        command: list[str], *, cwd: Path, environment_overrides: dict[str, str] | None = None
    ) -> str:
        stdout = real_run(command, cwd=cwd, environment_overrides=environment_overrides)
        if "pytest_cov.plugin" in command and "--cov-report=term" in command:
            terminal_commands.append(command)
            return "1 passed\n"
        return stdout

    monkeypatch.setattr(benchmark_report, "run", omit_terminal_report)
    with pytest.raises(
        RuntimeError, match="missing pytest terminal coverage report for pytest-cov"
    ):
        benchmark_report.main()
    assert terminal_commands, "validation must reach the terminal preflight"


@pytest.mark.skipif(
    not benchmark_report.SLIPCOVER_SUPPORTED, reason="SlipCover supports CPython < 3.15"
)
def test_report_harness_rejects_changed_measured_slipcover_terminal_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, package, _, driver = benchmark_report.create_fixture(tmp_path, 1)
    commands, environments = benchmark_report.validate_pytest_outputs(
        tmp_path, package, driver, branch=True
    )
    validator = benchmark_report.prepare_pytest_sample_validator(
        tmp_path, commands, environments, branch=True
    )
    real_timed_process = benchmark_report.timed_process_command

    def change_terminal_row(
        command: list[str], *, cwd: Path, env: dict[str, str]
    ) -> tuple[float, subprocess.CompletedProcess[str]]:
        elapsed, completed = real_timed_process(command, cwd=cwd, env=env)
        if "slipcover" in command:
            assert completed.returncode == 0, completed.stderr
            # Keep the pinned SlipCover framework/header/summary sections intact.
            original = str(Path("package") / "module_00000.py")
            replacement = str(Path("package") / "other.py")
            assert original in completed.stdout, completed.stdout
            completed.stdout = completed.stdout.replace(original, replacement)
        return elapsed, completed

    monkeypatch.setattr(benchmark_report, "timed_process_command", change_terminal_row)
    with pytest.raises(
        RuntimeError, match="measured pytest terminal coverage report differs for slipcover"
    ):
        benchmark_report.compare_tools(
            "pytest branch collection + terminal report",
            commands,
            cwd=tmp_path,
            warmups=0,
            runs=1,
            before_each=lambda tool: benchmark_report.remove_pytest_output(tool, tmp_path),
            output_validator=validator,
            environments=environments,
            baseline="plain pytest",
        )


def test_publication_validate_only_runs_without_build_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VFY_BENCHMARK_BUILD_MANIFEST", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["benchmark_report.py", "--validate-only", "--files", "1", "--shards", "1"]
    )
    monkeypatch.setattr(benchmark_report, "require_clean", lambda _: object())
    monkeypatch.setattr(benchmark_report, "build_profile", lambda: "release")
    monkeypatch.setattr(
        benchmark_report.tempfile,
        "TemporaryDirectory",
        lambda **_: contextlib.nullcontext(str(tmp_path)),
    )
    visited: list[str] = []
    monkeypatch.setattr(
        benchmark_report, "create_fixture", lambda root, _: (root, root, root, root)
    )
    for name in ("validate_terminal_outputs", "validate_json_outputs", "validate_xml_outputs"):
        monkeypatch.setattr(
            benchmark_report, name, lambda *_args, label=name: visited.append(label)
        )
    monkeypatch.setattr(benchmark_report, "create_shards", lambda *_: tmp_path)
    monkeypatch.setattr(benchmark_report, "validate_combine_outputs", lambda *_: ([], []))
    monkeypatch.setattr(
        benchmark_report,
        "collection_commands",
        lambda root, *_args, branch: (
            ["reference"],
            ["vfy"],
            root / f"ref-{branch}",
            root / f"vfy-{branch}",
        ),
    )
    monkeypatch.setattr(benchmark_report, "run", lambda command, **_: visited.append(command[0]))
    monkeypatch.setattr(
        benchmark_report, "validate_collection_outputs", lambda *_: visited.append("collection")
    )
    monkeypatch.setattr(
        benchmark_report,
        "validate_pytest_outputs",
        lambda *_args, branch: visited.append(f"pytest-{branch}") or ([], {}),
    )
    monkeypatch.setattr(benchmark_report, "prepare_pytest_sample_validator", lambda *a, **k: None)

    assert benchmark_report.main() == 0
    assert visited.count("collection") == 2
    assert {"pytest-False", "pytest-True", "validate_terminal_outputs"} <= set(visited)
