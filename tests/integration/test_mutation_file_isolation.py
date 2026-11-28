"""Public mutation contracts for pytest file-isolated execution."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.support.vfy_project import CommandResult, VfyProject

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="file-isolated mutation execution requires POSIX fork"
)


def _mutate_native_file_isolated(
    project: VfyProject,
    *,
    max_mutations: int | None = None,
    engine: str | None = None,
    workers: int = 2,
    timeout_policy: str = "adaptive",
) -> tuple[CommandResult, dict[str, object]]:
    arguments = [
        "mutate",
        "--engine=pytest-native",
        "--all",
        "--source",
        "app.py",
        "--operator",
        "integer_plus_one",
        "--no-cache",
        "--workers",
        str(workers),
        "--timeout",
        "8",
        f"--timeout-policy={timeout_policy}",
        "--format",
        "json",
    ]
    if max_mutations is not None:
        arguments.extend(("--max-mutations", str(max_mutations)))
    environment = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    if engine is not None:
        environment["VFY_MUTATION_ENGINE"] = engine
    completed = project.run_vfy(*arguments, env=environment)
    try:
        report = cast(dict[str, object], json.loads(completed.stdout))
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"mutation command did not return JSON (exit {completed.returncode}):\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        ) from error
    return completed, report


def _mutations(report: dict[str, object]) -> list[dict[str, object]]:
    raw = report.get("mutations")
    assert isinstance(raw, list)
    assert all(isinstance(item, dict) for item in raw)
    return cast(list[dict[str, object]], raw)


def test_definition_mutations_run_other_file_metadata_for_protected_and_surviving_cases(
    tmp_path: Path,
) -> None:
    protected = VfyProject(tmp_path / "protected")
    protected.write(".coverage.oracle", "unrelated reference data")
    protected.write(
        "conftest.py",
        "import _pytest.runner\n\n"
        "def pytest_configure(config):\n"
        "    def forbidden(*args, **kwargs):\n"
        "        raise AssertionError('stock pytest execution was used')\n"
        "    _pytest.runner.runtestprotocol = forbidden\n",
    )
    protected.write("app.py", "def value(argument=1):\n    return argument\n")
    protected.write(
        "tests/test_app.py",
        "import app\n\ndef test_value():\n    assert app.value(5) == 5\n",
    )
    protected.write(
        "tests/test_metadata.py",
        "import app\n\ndef test_default_metadata():\n    assert app.value.__defaults__ == (1,)\n",
    )

    completed, report = _mutate_native_file_isolated(protected, max_mutations=1)
    assert completed.returncode == 0, completed.stderr
    assert report["summary"] == {"protected": 1}
    assert (protected.root / ".coverage.oracle").read_text() == "unrelated reference data"
    mutation = _mutations(report)[0]
    owner = cast(dict[str, object], mutation["owner"])
    assert owner["phase"] == "definition"
    assert mutation["status"] == "protected"
    assert mutation["killer"] == "tests/test_metadata.py::test_default_metadata"

    surviving = VfyProject(tmp_path / "surviving")
    surviving.write("app.py", "def value(argument=1):\n    return argument\n")
    surviving.write(
        "tests/test_app.py",
        "import app\nfrom pathlib import Path\n\ndef test_value():\n"
        "    state = 'mutant' if app.value.__defaults__ != (1,) else 'baseline'\n"
        "    with Path('app-runs.txt').open('a', encoding='utf-8') as stream:\n"
        "        stream.write(state + '\\n')\n"
        "    assert app.value(5) == 5\n",
    )
    surviving.write(
        "tests/test_metadata.py",
        "import app\nfrom pathlib import Path\n\ndef test_default_shape():\n"
        "    values = app.value.__defaults__\n"
        "    state = 'mutant' if values != (1,) else 'baseline'\n"
        "    with Path('metadata-runs.txt').open('a', encoding='utf-8') as stream:\n"
        "        stream.write(state + '\\n')\n"
        "    assert isinstance(values, tuple) and len(values) == 1\n",
    )

    survived, survivor_report = _mutate_native_file_isolated(surviving, max_mutations=1)
    assert survived.returncode == 0, survived.stderr
    assert survivor_report["summary"] == {"survived": 1}
    survivor = _mutations(survivor_report)[0]
    survivor_owner = cast(dict[str, object], survivor["owner"])
    assert survivor_owner["phase"] == "definition"
    assert survivor["status"] == "survived"
    assert set((tmp_path / "surviving" / "metadata-runs.txt").read_text().splitlines()) == {
        "baseline",
        "mutant",
    }
    assert set((tmp_path / "surviving" / "app-runs.txt").read_text().splitlines()) == {
        "baseline",
        "mutant",
    }


def test_file_isolation_baseline_failure_cannot_be_mutation_protection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write("app.py", "def value(argument=1):\n    return argument\n")
    project.write("shared_state.py", "ready = False\n")
    project.write(
        "tests/test_a_seed.py",
        "import app\nimport shared_state\n\ndef test_seed():\n"
        "    shared_state.ready = True\n    assert app.value() == 1\n",
    )
    project.write(
        "tests/test_b_state.py",
        "import shared_state\n\ndef test_requires_seed():\n    assert shared_state.ready\n",
    )

    serial_baseline = project.run_vfy(
        "test",
        "--engine=native",
        "--all",
        "--source",
        "app.py",
        "-j",
        "1",
        "-q",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    serial_baseline.expect(0)
    (tmp_path / ".vfy" / "mutation-baseline.json").unlink(missing_ok=True)

    completed, report = _mutate_native_file_isolated(project, max_mutations=1)
    assert completed.returncode == 1, completed.stderr
    assert report["summary"] == {"unstable": 1}
    mutation = _mutations(report)[0]
    assert mutation["status"] == "unstable"
    assert "without the mutation" in str(mutation["detail"])


_COUNTER_SOURCE = """\
from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def active_workers() -> int:
    active = 0
    for path in Path.cwd().glob(".worker-budget-*.lock"):
        with path.open("ab") as worker:
            try:
                fcntl.flock(worker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active += 1
    return active


@contextmanager
def track_worker() -> Iterator[int]:
    worker_path = Path(f".worker-budget-{os.getpid()}.lock")
    peak_path = Path(".worker-budget.peak")
    with worker_path.open("ab") as worker, Path(".worker-budget.lock").open("ab") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        # The kernel releases this lock even if the worker is killed. The
        # enrollment lock prevents another worker from joining during the count.
        fcntl.flock(worker.fileno(), fcntl.LOCK_EX)
        active = active_workers()
        peak = int(peak_path.read_text(encoding="ascii")) if peak_path.exists() else 0
        if active > peak:
            staged = peak_path.with_name(f"{peak_path.name}.{os.getpid()}")
            staged.write_text(str(active), encoding="ascii")
            os.replace(staged, peak_path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        yield active
"""


def _budgeted_test_body(body: str) -> str:
    return (
        "import budget_counter\n\n"
        "def test_value():\n"
        "    with budget_counter.track_worker() as active:\n"
        "        assert active <= 2, f'{active} executing test workers exceed -j 2'\n"
        + "".join(f"        {line}\n" for line in body.splitlines())
    )


def test_native_file_isolation_shares_worker_budget_for_collected_and_portable_mutations(
    tmp_path: Path,
) -> None:
    project = VfyProject(tmp_path, timeout=45)
    project.write("app.py", "def value(argument=1):\n    return argument + 1\n")
    project.write("budget_counter.py", _COUNTER_SOURCE)
    os.mkfifo(tmp_path / "mutation-handshake")
    project.write(
        "tests/test_a_value.py",
        "import app\n"
        + _budgeted_test_body(
            "if app.value.__defaults__ == (2,):\n"
            "    open('portable-writer-started', 'w').close()\n"
            "    with open('mutation-handshake', 'wb', buffering=0) as stream:\n"
            "        stream.write(b'go')\n"
            "elif app.value(1) == 3:\n"
            "    with open('mutation-handshake', 'rb', buffering=0) as stream:\n"
            "        assert stream.read(2) == b'go'\n"
            "assert app.value(1) == 2"
        ),
    )
    project.write(
        "tests/test_b_metadata.py",
        "import app\n" + _budgeted_test_body("assert app.value.__defaults__ == (1,)"),
    )
    project.write(
        "tests/test_z_independent.py",
        _budgeted_test_body("assert 6 * 7 == 42"),
    )
    # The collected body mutant reads the handshake, while the fresh definition
    # mutant writes it only after its own pytest collection. A fixed deadline
    # keeps that startup from outrunning an adaptive millisecond budget. The
    # controller forks collected mutants after its collection, so holding that
    # collection until the prestarted writer runs makes the two mutants overlap
    # in a fixed order.
    project.write(
        "conftest.py",
        "import sys\nimport time\nfrom pathlib import Path\n\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    if sys.argv[1:2] != ['mutate']:\n"
        "        return\n"
        "    deadline = time.monotonic() + 6.0\n"
        "    while time.monotonic() < deadline:\n"
        "        if Path('portable-writer-started').exists():\n"
        "            open('controller-saw-writer', 'w').close()\n"
        "            return\n"
        "        time.sleep(0.01)\n",
    )

    signatures: list[list[str]] = []
    for engine in ("auto", "portable"):
        for name in (".worker-budget.peak", "portable-writer-started", "controller-saw-writer"):
            (tmp_path / name).unlink(missing_ok=True)
        completed, report = _mutate_native_file_isolated(
            project, engine=engine, timeout_policy="fixed"
        )
        diagnostic = (
            f"engine: {engine}\n"
            f"return code: {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}\n"
            f"report:\n{json.dumps(report, indent=2, sort_keys=True)}"
        )
        assert completed.returncode == 0, diagnostic
        assert report["summary"] == {"protected": 2}, diagnostic
        rows = _mutations(report)
        assert {cast(dict[str, object], row["owner"])["phase"] for row in rows} == {
            "definition",
            "body",
        }
        assert {row["status"] for row in rows} == {"protected"}
        by_phase = {cast(dict[str, object], row["owner"])["phase"]: row for row in rows}
        assert by_phase["definition"]["tests"] == []
        assert by_phase["body"]["tests"] == (
            ["tests/test_a_value.py::test_value"] if engine == "auto" else []
        )
        signatures.append(
            sorted(
                json.dumps(
                    {
                        key: row[key]
                        for key in (
                            "id",
                            "status",
                            "killer",
                            "returncode",
                        )
                    }
                    | {"phase": cast(dict[str, object], row["owner"])["phase"]},
                    sort_keys=True,
                )
                for row in rows
            )
        )
        metrics = cast(dict[str, int | float], report["metrics"])
        if engine == "auto":
            assert metrics["fast_results"] > 0
            assert metrics["portable_fallbacks"] > 0
            if sys.platform.startswith("linux"):
                # Linux prestarts the fresh supervisor before controller collection.
                assert (tmp_path / "controller-saw-writer").exists(), diagnostic
        active = project.run_python(
            "-c", "import budget_counter; print(budget_counter.active_workers())"
        )
        assert int(active.expect(0).stdout) == 0, diagnostic
        assert int((tmp_path / ".worker-budget.peak").read_text(encoding="ascii")) == 2, diagnostic

    assert signatures[0] == signatures[1]


def test_mutant_collection_error_is_not_reported_as_protection(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    project.write(
        "app.py",
        "def value(argument=1):\n    return argument\n"
        "if value.__defaults__ != (1,):\n"
        "    raise ValueError('mutated default rejected during import')\n",
    )
    project.write(
        "tests/test_app.py",
        "from app import value\n\ndef test_value():\n    assert value() == 1\n",
    )

    completed, report = _mutate_native_file_isolated(project, max_mutations=1, engine="portable")
    assert completed.returncode == 1, completed.stderr
    assert report["summary"] == {"error": 1}
    mutation = _mutations(report)[0]
    assert mutation["status"] == "error"
    assert mutation["returncode"] == 2
    assert mutation["killer"] is None
    assert "mutated default rejected during import" in str(mutation["detail"])
