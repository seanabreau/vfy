"""History and no-JUnit benchmark gates cross the real public process boundary."""

from __future__ import annotations

import argparse
import copy
import json
import math
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
from validation.pytest_runner import (
    benchmark_independent_hotspots as hotspot,
    benchmark_native_xdist as benchmark,
)
from validation.pytest_runner.check_native_adoption_matrix import Project

from tests.support import VfyProject


def test_no_junit_preflight_checks_real_phases_warnings_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    fixture = VfyProject(snapshot)
    fixture.write(
        "test_cases.py",
        "import pytest\n"
        "def test_pass():\n    assert True\n"
        "@pytest.mark.skip(reason='same skip')\n"
        "def test_skip():\n    pass\n"
        "@pytest.mark.xfail(reason='same expected failure')\n"
        "def test_xfail():\n    assert False\n",
    )
    # This test owns phase/history gates. Wheel inventory validation has separate
    # tampering tests and needs a supplied release wheel in real measurements.
    monkeypatch.setattr(benchmark.matrix, "verify_outcome_artifacts", lambda *args: None)
    root, output = tmp_path / "runtime", tmp_path / "preflight"
    descriptor = Project("fixture", "", "", ".", ("test_cases.py",), ())
    stock, independent = benchmark.Route("stock", 0), benchmark.Route("native", 1, False)
    history = root / ".vfy/test-history.json"
    accepted, expected, proof = benchmark.preflight(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        output,
        None,
        [stock, independent],
        tmp_path / "unused.whl",
        60,
        junit=False,
        history_path=history,
    )
    assert accepted == [stock, independent], proof
    assert expected == {stock.name: [], independent.name: []}
    assert [proof[route.name]["cases"] for route in accepted] == [3, 3]
    assert not list(output.rglob("junit.xml*"))
    parity = hotspot.load_json(Path(proof[independent.name]["independent_execution"]["path"]))
    actual = hotspot.read_history(history)
    hotspot.check_history(actual, None, parity["cases"])
    compact = [
        {
            "nodeid": case["nodeid"],
            "reports": [
                {"phase": report["phase"], "outcome": report["outcome"]}
                for report in case["reports"]
            ],
        }
        for case in parity["cases"]
    ]
    assert hotspot.check_history(actual, None, compact) == hotspot.check_history(
        actual, None, parity["cases"]
    )
    actual["tests"].pop("test_cases.py::test_skip")
    with pytest.raises(ValueError, match="inventory"):
        hotspot.check_history(actual, None, parity["cases"])


def test_independent_direct_seed_and_controller_keep_twenty_real_recorded_observations(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "test_cases.py",
        "import pytest\n@pytest.mark.parametrize('value', [1,2])\n"
        "def test_value(value):\n    assert value > 0\n",
    )
    descriptor = Project("fixture", "", "", ".", ("test_cases.py",), ())
    python = Path(sys.executable)
    direct = benchmark.Route("native", 1, False, "direct")
    output = tmp_path / "direct"
    output.mkdir()
    environment = benchmark.route_environment(
        descriptor, tmp_path, python, direct, None, output=output
    )
    source = tmp_path / "observed-run.json"
    environment["VFY_TEST_RESULTS"] = str(source)
    command = benchmark.command(descriptor, tmp_path, python, output, None, direct, junit=False)
    assert command[:4] == [str(python), "-P", "-m", "vfy.independent.entry"]
    result = project.run(*command, env=environment)
    result.expect(0)
    parity = benchmark.independent_execution(output, tmp_path, python, tmp_path / "base", direct)
    seed_path = tmp_path / "seed/history.json"
    seed = hotspot.seed_history(python, seed_path, source, tmp_path / "seed-receipt.json")
    history = tmp_path / ".vfy/test-history.json"
    history.parent.mkdir()
    history.write_bytes(seed_path.read_bytes())
    command = benchmark.command(
        descriptor,
        tmp_path,
        python,
        tmp_path / "controller",
        None,
        benchmark.Route("native", 1, False),
        timed_evidence="junit",
        junit=False,
        history_path=history,
    )
    result = project.run(*command)
    result.expect(0)
    actual = hotspot.read_history(history)
    hotspot.check_history(actual, seed, parity["cases"])
    assert all(len(entry["observations"]) == 20 for entry in actual["tests"].values())
    assert seed_path.read_bytes() != history.read_bytes()
    assert not list(tmp_path.rglob("junit.xml*"))
    history.write_bytes(seed_path.read_bytes())
    selected = [case for case in parity["cases"] if case["nodeid"].endswith("[1]")]
    command[-1] = "test_cases.py::test_value[1]"
    result = project.run(*command)
    result.expect(0)
    actual = hotspot.read_history(history)
    hotspot.check_history(actual, seed, selected)
    untouched = "test_cases.py::test_value[2]"
    assert actual["tests"][untouched] == seed["tests"][untouched]
    damaged = copy.deepcopy(actual)
    damaged["tests"][untouched]["observations"][-1]["outcome"] = "failed"
    with pytest.raises(ValueError, match="untouched"):
        hotspot.check_history(damaged, seed, selected)
    damaged = copy.deepcopy(actual)
    next(iter(damaged["tests"].values()))["observations"][-1]["attempt"] = "confirmation"
    with pytest.raises(ValueError, match="changed beyond"):
        hotspot.check_history(damaged, seed, parity["cases"])


def test_sqlite_history_oracle_preserves_order_and_negative_zero(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            "CREATE TABLE tests(id,nodeid,duration_ms,observations_present,"
            "duration_negative_zero);"
            "CREATE TABLE runs(id,run_id);"
            "CREATE TABLE observations(test_id,position,run_id,attempt,outcome,"
            "failed_phase,duration_ms,duration_negative_zero);"
        )
        connection.execute("INSERT INTO tests VALUES (1,'test.py::test_value',0,1,1)")
        connection.executemany("INSERT INTO runs VALUES (?,?)", [(1, "prior"), (2, "new")])
        connection.execute("INSERT INTO observations VALUES (1,0,1,0,1,1,3,0)")
        connection.execute("INSERT INTO observations VALUES (1,1,2,0,2,NULL,0,1)")
        connection.commit()
    actual = hotspot.read_history(path)
    assert actual == {
        "tests": {
            "test.py::test_value": {
                "duration_ms": -0.0,
                "observations": [
                    {
                        "run_id": "prior",
                        "attempt": "primary",
                        "outcome": "failed",
                        "duration_ms": 3,
                        "failed_phase": "call",
                    },
                    {
                        "run_id": "new",
                        "attempt": "primary",
                        "outcome": "skipped",
                        "duration_ms": -0.0,
                    },
                ],
            }
        },
    }
    entry = actual["tests"]["test.py::test_value"]
    assert math.copysign(1, entry["duration_ms"]) == -1
    assert math.copysign(1, entry["observations"][-1]["duration_ms"]) == -1


def test_semantic_preflight_rejection_prevents_hotspot_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoked: list[str] = []

    def rejected(
        *args: object, **kwargs: object
    ) -> tuple[list[benchmark.Route], dict[str, Any], dict[str, Any]]:
        invoked.append("preflight")
        return [], {}, {"native-j1": {"passed": False, "errors": ["phase mismatch"]}}

    monkeypatch.setattr(benchmark, "preflight", rejected)
    with pytest.raises(ValueError, match="failed full semantic preflight"):
        hotspot.discover(
            argparse.Namespace(timeout=60, capture="sys", junit=False, history_seed_evidence=None),
            Project("fixture", "", "", ".", ("tests",), ()),
            {
                "baseline": {
                    "python": sys.executable,
                    "history": ".vfy/history.json",
                    "wheel": "unused",
                }
            },
            tmp_path / "root",
            tmp_path / "snapshot",
            tmp_path / "output",
        )
    assert invoked == ["preflight"]


def test_full_history_seed_rejects_missing_or_incomplete_provenance(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="completed full-suite evidence"):
        hotspot.retained_seed_run(tmp_path, tmp_path / "snapshot", {})
    (tmp_path / "result.json").write_text(json.dumps({"status": "failed"}))
    (tmp_path / "identity.json").write_text(json.dumps({"arguments": {}}))
    with pytest.raises(ValueError, match="completed full Packaging cohort"):
        hotspot.retained_seed_run(tmp_path, tmp_path / "snapshot", {})


def test_terminal_validation_uses_only_final_outer_summary() -> None:
    assert hotspot.terminal_counts("100 passed in 0.1s\n2 passed, 1 skipped in 0.2s\n") == {
        "passed": 2,
        "skipped": 1,
    }
    with pytest.raises(ValueError, match="final terminal"):
        hotspot.terminal_counts("100 passed in 0.1s\ninfrastructure failed\n")


def test_discovery_compacts_cases_and_compares_retained_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = [
        {
            "nodeid": "test.py::test_case",
            "reports": [{"phase": "setup", "outcome": "skipped", "detail": "reason"}],
            "warnings": ["warning"],
        }
    ]
    candidate = copy.deepcopy(baseline)
    role_cases = {"baseline": baseline, "candidate": candidate}
    tamper_baseline = False
    history = {
        "tests": {
            "test.py::test_case": {
                "observations": [
                    {
                        "run_id": "0" * 32,
                        "attempt": "primary",
                        "outcome": "skipped",
                        "duration_ms": 1,
                    }
                ]
            }
        }
    }

    def preflight(
        *args: object, **kwargs: object
    ) -> tuple[list[Any], dict[str, Any], dict[str, Any]]:
        destination = cast("Path", args[4])
        role = destination.parent.name
        stdout = destination / "stock-xdist-0" / "stdout.txt"
        stdout.parent.mkdir(parents=True, exist_ok=True)
        stdout.write_text("1 skipped in 0.1s\n")
        independent_stdout = destination / "native-j1" / "stdout.txt"
        independent_stdout.parent.mkdir(parents=True, exist_ok=True)
        independent_stdout.write_text("1 skipped in 0.1s\n")
        execution = destination / "execution.json"
        execution.write_text(json.dumps({"cases": role_cases[role]}))
        if role == "candidate" and tamper_baseline:
            baseline_path = destination.parent.parent / "baseline/discovery/execution.json"
            baseline_path.write_text(json.dumps({"cases": candidate}))
        return (
            cast("list[Any]", args[6]),
            {},
            {"native-j1": {"independent_execution": {"path": str(execution)}}},
        )

    monkeypatch.setattr(benchmark, "preflight", preflight)
    monkeypatch.setattr(hotspot, "read_history", lambda path: history)
    monkeypatch.setattr(hotspot, "write_atomic", lambda *args: None)
    args = argparse.Namespace(
        timeout=60,
        capture="sys",
        junit=False,
        history_seed_evidence=None,
        cases=1,
        history_observations=0,
    )
    runtimes = {
        role: {"python": sys.executable, "history": ".vfy/history.json", "wheel": "unused"}
        for role in role_cases
    }

    def discover() -> dict[str, Any]:
        return hotspot.discover(
            args,
            Project("fixture", "", "", ".", ("tests",), ()),
            runtimes,
            tmp_path / "root",
            tmp_path / "snapshot",
            tmp_path / "out",
        )

    state = discover()
    compact = state["cases"]["baseline"]
    assert compact == [
        {"nodeid": "test.py::test_case", "reports": [{"phase": "setup", "outcome": "skipped"}]}
    ]
    assert hotspot.check_history(history, None, compact) == hotspot.check_history(
        history, None, baseline
    )
    candidate[0]["warnings"] = ["different warning"]
    with pytest.raises(ValueError, match="phase observations differ"):
        discover()
    tamper_baseline = True
    with pytest.raises(ValueError, match="baseline preflight execution artifact changed"):
        discover()


def test_distributed_hotspot_requires_matching_per_sample_case_evidence() -> None:
    hotspot.validate_worker_options(1, junit=False)
    hotspot.validate_worker_options(2, junit=True)
    with pytest.raises(ValueError, match="workers must be positive"):
        hotspot.validate_worker_options(0, junit=True)
    with pytest.raises(ValueError, match="require JUnit"):
        hotspot.validate_worker_options(2, junit=False)


def test_hotspot_rejects_different_non_vfy_installed_file_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for site in (baseline, candidate):
        (site / "package").mkdir(parents=True)
        (site / "package" / "module.py").write_text("same\n")
        (site / "vfy").mkdir()
        (site / "vfy" / "module.py").write_text(site.name)
    monkeypatch.setattr(
        benchmark,
        "interpreter_metadata",
        lambda python, _distribution: {"purelib": str(python)},
    )
    runtimes = {"baseline": {"python": str(baseline)}, "candidate": {"python": str(candidate)}}
    hotspot.verify_unrelated_sites(runtimes)
    (candidate / "package" / "module.py").write_text("different\n")
    with pytest.raises(ValueError, match="non-vfy installed site files differ"):
        hotspot.verify_unrelated_sites(runtimes)


def test_distributed_preflight_compares_real_phases_and_worker_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    project = VfyProject(snapshot)
    project.write(
        "test_cases.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', range(4))\n"
        "def test_case(value):\n"
        "    assert value >= 0\n",
    )
    monkeypatch.setattr(benchmark.matrix, "verify_outcome_artifacts", lambda *args: None)
    root, output = tmp_path / "runtime", tmp_path / "preflight"
    descriptor = Project("fixture", "", "", ".", ("test_cases.py",), ())
    serial = benchmark.Route("stock", 0)
    stock = benchmark.Route("stock", 2)
    independent = benchmark.Route("native", 2, False)
    accepted, _expected, proof = benchmark.preflight(
        descriptor,
        root,
        snapshot,
        Path(sys.executable),
        output,
        None,
        [serial, stock, independent],
        tmp_path / "unused.whl",
        60,
        junit=True,
        native_scheduling="cases",
        require_active_workers=True,
        history_path=root / ".vfy/test-history.sqlite3",
    )
    assert accepted == [stock, independent], proof
    topology = proof[independent.name]["independent_execution"]["execution_topology"]
    assert topology["requested_workers"] == topology["active_workers"] == 2
    assert len(set(topology["worker_pids"])) == 2
    assert proof[independent.name]["cases"] == 4


def test_distributed_discovery_accepts_observed_stock_and_both_wheel_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    fixture = VfyProject(snapshot)
    fixture.write(
        "test_cases.py",
        "import pytest\n"
        "@pytest.mark.parametrize('value', range(2))\n"
        "def test_chosen(value):\n"
        "    assert value >= 0\n"
        "def test_excluded():\n"
        "    pass\n",
    )
    monkeypatch.setattr(benchmark.matrix, "verify_outcome_artifacts", lambda *args: None)
    root, output = tmp_path / "runtime", tmp_path / "preflight"
    output.mkdir()
    descriptor = Project(
        "fixture", "", "", ".", ("test_cases.py",), (), extra_args=("-k", "test_chosen")
    )
    args = argparse.Namespace(
        workers=2,
        native_scheduling="cases",
        timeout=60,
        capture="sys",
        junit=True,
        history_seed_evidence=None,
        cases=2,
        history_observations=0,
    )
    runtimes = {
        role: {
            "python": sys.executable,
            "history": ".vfy/test-history.json",
            "wheel": "unused.whl",
        }
        for role in ("baseline", "candidate")
    }
    state = hotspot.discover(args, descriptor, runtimes, root, snapshot, output)
    assert len(state["cases"]["baseline"]) == len(state["cases"]["candidate"]) == 2
    assert state["terminal"]["baseline"]["stock-xdist-2"] == {"passed": 2}
    assert state["terminal"]["baseline"]["native-j2"] == {"passed": 2, "deselected": 1}
    for role in runtimes:
        proof = json.loads((output / role / "discovery/parity.json").read_text())
        assert proof["stock-xdist-0"]["passed"]
        assert proof["stock-xdist-2"]["passed"]
        assert proof["native-j2"]["passed"]
        topology = proof["native-j2"]["independent_execution"]["execution_topology"]
        assert topology["requested_workers"] == topology["active_workers"] == 2
        assert len(set(topology["worker_pids"])) == 2
    for label, route in (
        ("stock", benchmark.Route("stock", 2)),
        ("baseline", benchmark.Route("native", 2, False)),
    ):
        destination = output / f"sample-{label}"
        run = hotspot.measure_one(
            args,
            descriptor,
            runtimes["baseline"],
            root,
            snapshot,
            destination,
            "baseline",
            label,
            route,
            state,
        )
        assert run["exit_status"] == 0
