"""Public-command proofs for full scaling inventories and resource evidence."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from scripts.process_tree import run_captured
from validation.pytest_runner.benchmark_native_scaling import (
    RESOURCE_COLLECTOR,
    ROUTES,
    SHAPE_TRACE,
    Family,
    Workload,
    check_run,
    create_project,
    preflight,
    sample,
)
from validation.pytest_runner.benchmark_public_pytest import controlled_environment


@pytest.mark.parametrize("family", ["parametrized", "functions", "fixtures", "capture"])
def test_full_scaling_shapes_execute_every_body_and_lifecycle(
    tmp_path: Path, family: Family
) -> None:
    if sys.platform != "linux":
        pytest.skip("the resource collector records Linux RSS units")
    workload = Workload(family, 7)
    root = tmp_path / "project"
    output = tmp_path / "evidence"
    (output / "home").mkdir(parents=True)
    create_project(root, workload)
    if family == "functions":
        definitions = [
            node
            for node in ast.parse((root / "test_workload.py").read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_case_")
        ]
        assert [node.name for node in definitions] == [
            f"test_case_{index:05d}" for index in range(7)
        ]
    stock = preflight(workload, "pytest", Path(sys.executable), root, output, None, 120)
    reference = stock["validation"]["ordered_outcomes_sha256"]
    native = preflight(workload, "vfy", Path(sys.executable), root, output, reference, 120)
    assert stock["validation"] == native["validation"]
    if family in {"parametrized", "functions"}:
        expected_functions = 7 if family == "functions" else 1
        assert stock["validation"]["function_shape"]["function_bindings"] == expected_functions
        assert stock["validation"]["function_shape"]["distinct_code_objects"] == expected_functions
    for route in ROUTES:
        record = sample(
            workload, route, Path(sys.executable), root, output, reference, "measured", 0, 120
        )
        assert record["status"] == "passed"
        assert SHAPE_TRACE not in record["environment"]
        assert record["validation"]["case_count"] == 7
        resources = record["resources"]
        assert resources["child_command"] == record["command"]
        assert resources["child_exit_status"] == 0
        assert resources["total_cpu_ms"] == pytest.approx(
            resources["user_cpu_ms"] + resources["system_cpu_ms"]
        )
    evidence = output / workload.name / "measured-00-vfy"
    if family == "fixtures":
        events = (evidence / "lifecycle.txt").read_text().splitlines()
        expected = ["session.setup", "module.setup"]
        for index in range(7):
            nodeid = f"test_workload.py::test_case[case-{index:04d}]"
            expected.extend([f"function.setup:{nodeid}", f"function.teardown:{nodeid}"])
        expected.extend(["module.teardown", "session.teardown"])
        assert events == expected
    elif family in {"parametrized", "functions"}:
        assert json.loads((evidence / "body.json").read_text()) == list(range(7))


def test_junit_success_cannot_authorize_an_incomplete_body_trace(tmp_path: Path) -> None:
    workload = Workload("functions", 3)
    root = tmp_path / "project"
    output = tmp_path / "evidence"
    (output / "home").mkdir(parents=True)
    create_project(root, workload)
    proof = preflight(workload, "pytest", Path(sys.executable), root, output, None, 120)
    evidence = output / workload.name / "preflight-pytest"
    (evidence / "body.json").write_text("[0, 2]\n")
    with pytest.raises(RuntimeError, match="actual test-body trace"):
        check_run(
            workload,
            evidence,
            evidence / "public/junit.xml",
            proof["validation"]["ordered_outcomes_sha256"],
        )


@pytest.mark.parametrize("statement", ["pytest.skip('not executed')", "assert False"])
def test_stock_preflight_rejects_skips_and_failures(tmp_path: Path, statement: str) -> None:
    workload = Workload("functions", 3)
    root = tmp_path / "project"
    output = tmp_path / "evidence"
    (output / "home").mkdir(parents=True)
    create_project(root, workload)
    path = root / "test_workload.py"
    path.write_text(
        path.read_text().replace("    value = 1\n", f"    {statement}\n    value = 1\n")
    )
    with pytest.raises(RuntimeError, match="preflight"):
        preflight(workload, "pytest", Path(sys.executable), root, output, None, 120)


@pytest.mark.parametrize("exit_status", [0, 7, -15, -9])
def test_resource_collector_drains_both_pipes_and_preserves_child_status(
    tmp_path: Path, exit_status: int
) -> None:
    if sys.platform != "linux":
        pytest.skip("the resource collector records Linux RSS units")
    program = (
        "import os, signal, sys; "
        "sys.stdout.write('x'*1048576); sys.stdout.flush(); "
        "sys.stderr.write('y'*1048576); sys.stderr.flush(); "
    )
    program += (
        f"os.kill(os.getpid(), {-exit_status})"
        if exit_status < 0
        else f"raise SystemExit({exit_status})"
    )
    child = [sys.executable, "-P", "-c", program]
    record, stdout, stderr = (
        tmp_path / name for name in ("resources.json", "stdout.txt", "stderr.txt")
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            str(RESOURCE_COLLECTOR),
            "--record",
            str(record),
            "--stdout",
            str(stdout),
            "--stderr",
            str(stderr),
            "--",
            *child,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    resources = json.loads(record.read_text())
    assert completed.returncode == exit_status == resources["child_exit_status"]
    assert resources["child_command"] == child
    assert stdout.read_text() == "x" * 1048576
    assert stderr.read_text() == "y" * 1048576
    assert resources["user_cpu_ms"] >= 0
    assert resources["system_cpu_ms"] >= 0
    assert resources["peak_rss_bytes"] > 0


def test_resource_deadline_cleans_up_child_and_grandchild(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("the resource collector records Linux RSS units")
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    grandchild = "import time; time.sleep(60)"
    program = (
        "import json, os, subprocess, sys, time; "
        f"grandchild=subprocess.Popen([sys.executable, '-P', '-c', {grandchild!r}]); "
        "open('pids.json','w').write(json.dumps([os.getpid(), grandchild.pid])); "
        "time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_captured(
            [
                sys.executable,
                "-P",
                str(RESOURCE_COLLECTOR),
                "--record",
                str(tmp_path / "resources.json"),
                "--stdout",
                str(tmp_path / "stdout.txt"),
                "--stderr",
                str(tmp_path / "stderr.txt"),
                "--",
                sys.executable,
                "-P",
                "-c",
                program,
            ],
            cwd=tmp_path,
            env=controlled_environment(tmp_path / "home", tmp_path / "tmp"),
            timeout=3,
        )
    pids = json.loads((tmp_path / "pids.json").read_text())
    for _ in range(20):
        alive = []
        for pid in pids:
            try:
                status = Path(f"/proc/{pid}/stat").read_text()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if status.rsplit(")", 1)[1].split()[0] != "Z":
                alive.append(pid)
        if not alive:
            break
        time.sleep(0.05)
    assert not alive
    assert not (tmp_path / "resources.json").exists()
