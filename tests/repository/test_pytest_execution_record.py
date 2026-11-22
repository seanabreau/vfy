"""Protect production of provenance-bound pytest execution evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import venv
import warnings
import zipfile
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Literal, override

import pytest

from tests.support import VfyProject

ROOT = Path(__file__).resolve().parents[2]


def load_execution_module(kind: Literal["record", "accounting", "probe"]) -> ModuleType:
    name = f"pytest_execution_{kind}"
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    scripts_dir = str(path.parent)
    sys.path.insert(0, scripts_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(scripts_dir)
    return module


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def committed_project(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "upstream"
    root.mkdir()
    (root / "test_observations.py").write_text(
        """\
import logging
import sys
import warnings

import pytest


@pytest.fixture
def observed():
    print("setup-output")
    warnings.warn("setup-warning", UserWarning)
    yield
    print("teardown-output", file=sys.stderr)
    warnings.warn("teardown-warning", UserWarning)


def test_observations(observed):
    print("call-output")
    print("1 passed in 5s")
    print("<value at 0x123abc>")
    print("call-error", file=sys.stderr)
    logging.warning("call-log")
    warnings.warn("call-warning", UserWarning)
    assert False, "controlled failure"
""",
        encoding="utf-8",
    )
    git(root, "init", "-q")
    git(root, "add", ".")
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=vfy",
            "-c",
            "user.email=vfy@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return root, git(root, "rev-parse", "HEAD")


def classification_for(tmp_path: Path, revision: str) -> Path:
    classification = tmp_path / "classification.json"
    classification.write_text(
        json.dumps(
            {
                "inventory": {"upstream_commit": revision, "sha256": "a" * 64},
                "policy": {"sha256": "b" * 64},
                "classifications": [
                    {
                        "nodeid": "test_observations.py::test_observations",
                        "disposition": "parity-required",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return classification


def recorder_inputs(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / "pytest.whl"
    wheel.write_bytes(b"installed pytest wheel")
    lock = tmp_path / "requirements.lock"
    return wheel, lock


def write_test_lock(recorder: ModuleType, lock: Path) -> None:
    recorder.write_atomic(lock, recorder.resolved_environment(Path(sys.executable)))


def bind_test_artifact(recorder: ModuleType, wheel: Path) -> None:
    def installed(
        python: Path, artifacts: list[Path], runner_distribution: str
    ) -> list[dict[str, str]]:
        del python
        assert artifacts == [wheel]
        return [
            {
                "distribution": runner_distribution,
                "version": pytest.__version__,
                "filename": wheel.name,
                "sha256": recorder.file_sha256(wheel),
            }
        ]

    recorder.__dict__["verify_installed_artifacts"] = installed


def write_test_wheel(
    wheel: Path, distribution: str, version: str, payload: Mapping[str, str | bytes]
) -> None:
    dist_info = f"{distribution}-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", f"Name: {distribution}\nVersion: {version}\n")
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        for relative, contents in payload.items():
            archive.writestr(relative, contents)


def native_test_wheel(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    wheel = tmp_path / "vfy-0.1.0-py3-none-any.whl"
    payload = {
        "vfy/__init__.py": b"PACKAGE = True\n",
        "vfy/_pytest/__init__.py": b"ADAPTER_PACKAGE = True\n",
        "vfy/_pytest/native_pytest.py": b"ADAPTER = True\n",
        "vfy/_pytest/alternate.py": b"ALTERNATE = True\n",
        "vfy/_pytest/native_cache.py": b"CACHE = True\n",
        "vfy/_native.abi3.so": b"native extension bytes\n",
        "vfy/_native.pyi": b"# Native declarations\n",
        "vfy/_native.nested/child.abi3.so": b"nested extension bytes\n",
    }
    write_test_wheel(wheel, "vfy", "0.1.0", payload)
    return wheel, payload


def native_import_observations(purelib: Path, payload: dict[str, bytes]) -> list[dict[str, object]]:
    return [
        {
            "module": "vfy._native",
            "origin": str(purelib / "vfy/_native.abi3.so"),
            "sha256": hashlib.sha256(payload["vfy/_native.abi3.so"]).hexdigest(),
        },
        {
            "module": "vfy._pytest.native_pytest",
            "origin": str(purelib / "vfy/_pytest/native_pytest.py"),
            "sha256": hashlib.sha256(payload["vfy/_pytest/native_pytest.py"]).hexdigest(),
        },
    ]


@pytest.mark.parametrize(
    ("schema_version", "present"),
    [(None, False), (1, True)],
    ids=["missing-schema", "legacy-schema"],
)
def test_observer_identity_requires_schema_two(
    tmp_path: Path, schema_version: int | None, present: bool
) -> None:
    recorder = load_execution_module("record")
    upstream = tmp_path / "upstream"
    observer = {
        "producer_pid": 123,
        "session_token": "session-token",
        "session_depth": 0,
        "rootdir": str(upstream.resolve()),
        "observer_nonce": "a" * 64,
        "loaded_pytest_plugins": [],
        "inventory_nodeids_sha256": "b" * 64,
        "inventory_nodeid_count": 1,
    }
    if present:
        observer["schema_version"] = schema_version

    with pytest.raises(ValueError, match="trusted observer did not identify"):
        recorder.validate_observer_identity(
            observer,
            pid=123,
            token="session-token",
            upstream_root=upstream,
            purelib=str(tmp_path / "site-packages"),
            inventory_nodeids_sha256="b" * 64,
            inventory_nodeid_count=1,
        )


def test_native_import_origins_are_attested_against_candidate_wheel(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    imports = native_import_observations(purelib, payload)

    attested = recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)

    assert attested == [
        {
            "module": "vfy._native",
            "relative_path": "vfy/_native.abi3.so",
            "sha256": imports[0]["sha256"],
        },
        {
            "module": "vfy._pytest.native_pytest",
            "relative_path": "vfy/_pytest/native_pytest.py",
            "sha256": imports[1]["sha256"],
        },
    ]


@pytest.mark.parametrize(
    ("module", "relative_path", "sha256", "message"),
    [
        (
            "vfy._pytest.native_pytest",
            "vfy/_pytest/alternate.py",
            hashlib.sha256(b"ALTERNATE = True\n").hexdigest(),
            "adapter has an unexpected import origin",
        ),
        (
            "vfy._pytest.native_pytest",
            "vfy/_pytest/native_pytest.py",
            "0" * 64,
            "differs from the candidate wheel",
        ),
    ],
    ids=["unexpected-loaded-path", "tampered-loaded-hash"],
)
def test_native_import_origin_attestation_rejects_unexpected_path_or_hash(
    tmp_path: Path,
    module: str,
    relative_path: str,
    sha256: str,
    message: str,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    extension, adapter = native_import_observations(purelib, payload)
    adapter.update(module=module, origin=str(purelib / relative_path), sha256=sha256)

    with pytest.raises(ValueError, match=message):
        recorder.validate_native_imports(
            {"native_imports": [extension, adapter]}, str(purelib), wheel
        )


def test_native_import_origin_attestation_rejects_unlisted_wheel_path(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    imports = native_import_observations(purelib, payload)
    imports[0].update(origin=str(purelib / "vfy/_native.unlisted.abi3.so"), sha256=None)

    with pytest.raises(ValueError, match="differs from the candidate wheel"):
        recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)


def test_native_import_origin_attestation_rejects_non_string_module(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    imports = native_import_observations(purelib, payload)
    imports[1]["module"] = None

    with pytest.raises(ValueError, match="duplicate or unexpected import origins"):
        recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)


def test_native_extension_import_origin_cannot_be_nested(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    imports = native_import_observations(purelib, payload)
    imports[0].update(
        origin=str(purelib / "vfy/_native.nested/child.abi3.so"),
        sha256=hashlib.sha256(payload["vfy/_native.nested/child.abi3.so"]).hexdigest(),
    )

    with pytest.raises(ValueError, match="extension has an unexpected import origin"):
        recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)


def test_native_import_origins_accept_every_loaded_vfy_module_bound_to_the_wheel(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    imports = native_import_observations(purelib, payload)
    imports.insert(
        1,
        {
            "module": "vfy._pytest.native_cache",
            "origin": str(purelib / "vfy/_pytest/native_cache.py"),
            "sha256": hashlib.sha256(payload["vfy/_pytest/native_cache.py"]).hexdigest(),
        },
    )

    attested = recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)

    assert [row["module"] for row in attested] == [
        "vfy._native",
        "vfy._pytest.native_cache",
        "vfy._pytest.native_pytest",
    ]


def test_native_import_origin_attestation_rejects_sibling_module_differing_from_wheel(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    imports = native_import_observations(purelib, payload)
    imports.insert(
        1,
        {
            "module": "vfy._pytest.native_cache",
            "origin": str(purelib / "vfy/_pytest/native_cache.py"),
            "sha256": hashlib.sha256(b"CACHE = False\n").hexdigest(),
        },
    )

    with pytest.raises(
        ValueError, match=r"differs from the candidate wheel: vfy/_pytest/native_cache.py"
    ):
        recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)


@pytest.mark.parametrize(
    ("module", "relative_path"),
    [
        ("pytest", "vfy/_pytest/native_cache.py"),
        ("vfy._pytest.native_cache", "vfy/_pytest/alternate.py"),
        ("vfy", "vfy/_pytest/native_cache.py"),
        ("vfy._native", "vfy/_native.pyi"),
    ],
)
def test_native_import_origin_attestation_rejects_mislabeled_wheel_members(
    tmp_path: Path, module: str, relative_path: str
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    imports = native_import_observations(purelib, payload)
    row: dict[str, object] = {
        "module": module,
        "origin": str(purelib / relative_path),
        "sha256": hashlib.sha256(payload[relative_path]).hexdigest(),
    }
    if module == "vfy._native":
        imports[0] = row
    else:
        imports.append(row)
    imports.sort(key=lambda item: str(item["module"]))

    with pytest.raises(ValueError, match="unexpected import origin"):
        recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)


@pytest.mark.parametrize("module", ["vfy", "vfy._pytest"])
def test_native_import_origin_attestation_accepts_package_initializers(
    tmp_path: Path, module: str
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    relative_path = module.replace(".", "/") + "/__init__.py"
    imports = native_import_observations(purelib, payload)
    imports.append(
        {
            "module": module,
            "origin": str(purelib / relative_path),
            "sha256": hashlib.sha256(payload[relative_path]).hexdigest(),
        }
    )
    imports.sort(key=lambda item: str(item["module"]))

    attested = recorder.validate_native_imports({"native_imports": imports}, str(purelib), wheel)

    assert module in {row["module"] for row in attested}


def test_native_import_origin_attestation_rejects_missing_adapter_module(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    extension, _adapter = native_import_observations(purelib, payload)

    with pytest.raises(ValueError, match="incomplete import origins"):
        recorder.validate_native_imports({"native_imports": [extension]}, str(purelib), wheel)


def test_native_import_origin_attestation_rejects_duplicate_module_rows(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    extension, adapter = native_import_observations(purelib, payload)

    with pytest.raises(ValueError, match="duplicate or unexpected import origins"):
        recorder.validate_native_imports(
            {"native_imports": [extension, adapter, dict(adapter)]}, str(purelib), wheel
        )


def test_native_import_origin_attestation_rejects_unsorted_rows(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    wheel, payload = native_test_wheel(tmp_path)
    purelib = tmp_path / "site-packages"
    extension, adapter = native_import_observations(purelib, payload)

    with pytest.raises(ValueError, match="not sorted by module"):
        recorder.validate_native_imports(
            {"native_imports": [adapter, extension]}, str(purelib), wheel
        )


def synchronize_command_wait_with_ready_file(
    recorder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    ready_path: Path,
) -> None:
    original_wait = recorder.subprocess.Popen.wait

    def wait(process: subprocess.Popen[bytes], timeout: float | None = None) -> int | None:
        deadline = time.monotonic() + 5
        while not ready_path.exists():
            if process.poll() is not None:
                result = original_wait(process, timeout=timeout)
                assert result is None or isinstance(result, int)
                return result
            if time.monotonic() >= deadline:
                process.kill()
                original_wait(process, timeout=5)
                raise AssertionError(f"child did not publish readiness: {ready_path}")
            time.sleep(0.01)
        result = original_wait(process, timeout=timeout)
        assert result is None or isinstance(result, int)
        return result

    monkeypatch.setattr(recorder.subprocess.Popen, "wait", wait)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("-inf"), float("nan")])
def test_record_run_rejects_invalid_timeout_before_creating_evidence(
    tmp_path: Path, timeout: float
) -> None:
    recorder = load_execution_module("record")
    evidence = tmp_path / "evidence"

    with pytest.raises(ValueError, match="execution timeout must be finite and positive"):
        recorder.record_run(
            source="pytest",
            python=Path(sys.executable),
            upstream_root=tmp_path / "upstream",
            classification_path=tmp_path / "classification.json",
            runner_distribution="pytest",
            runner_artifacts=[],
            dependency_locks=[],
            pytest_arguments=[],
            evidence_dir=evidence,
            timeout=timeout,
        )

    assert not evidence.exists()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_run_record_command_rejects_invalid_timeout_before_creating_evidence(
    tmp_path: Path, timeout: float
) -> None:
    recorder = load_execution_module("record")
    evidence = tmp_path / "evidence"

    with pytest.raises(ValueError, match="execution timeout must be finite and positive"):
        recorder.run_record_command(
            [sys.executable, "-c", "raise SystemExit(0)"],
            root=tmp_path,
            environment=os.environ.copy(),
            evidence=evidence,
            timeout=timeout,
        )

    assert not evidence.exists()


def test_run_record_command_timeout_retains_output_and_status_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = load_execution_module("record")
    evidence = tmp_path / "evidence"
    ready = tmp_path / "ready"
    timeout = 0.4
    command = [
        sys.executable,
        "-c",
        "import sys,time; from pathlib import Path; "
        "print('stdout-before-timeout', flush=True); "
        "print('stderr-before-timeout', file=sys.stderr, flush=True); "
        f"Path({str(ready)!r}).touch(); time.sleep(30)",
    ]
    synchronize_command_wait_with_ready_file(recorder, monkeypatch, ready)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        recorder.run_record_command(
            command,
            root=tmp_path,
            environment=os.environ.copy(),
            evidence=evidence,
            timeout=timeout,
        )

    assert "Execution evidence:" in " ".join(raised.value.__notes__)
    assert (evidence / "stdout.txt").read_text(encoding="utf-8") == "stdout-before-timeout\n"
    assert (evidence / "stderr.txt").read_text(encoding="utf-8") == "stderr-before-timeout\n"
    assert json.loads((evidence / "command.json").read_text(encoding="utf-8")) == {
        "argv": command,
        "cwd": str(tmp_path),
        "timeout": timeout,
    }
    status = json.loads((evidence / "status.json").read_text(encoding="utf-8"))
    assert status["completed"] is False
    assert status["error"] == "TimeoutExpired"
    assert isinstance(status["pid"], int)


@pytest.mark.skipif(os.name == "nt", reason="process-group cleanup is POSIX-specific")
def test_run_record_command_timeout_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = load_execution_module("record")
    evidence = tmp_path / "evidence"
    descendant_pid_path = tmp_path / "descendant.pid"
    child_script = tmp_path / "spawn_child.py"
    child_script.write_text(
        "import subprocess, sys, time\n"
        "descendant = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        f"open({str(descendant_pid_path)!r}, 'w', encoding='utf-8').write(str(descendant.pid))\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    synchronize_command_wait_with_ready_file(recorder, monkeypatch, descendant_pid_path)

    with pytest.raises(subprocess.TimeoutExpired):
        recorder.run_record_command(
            [sys.executable, str(child_script)],
            root=tmp_path,
            environment=os.environ.copy(),
            evidence=evidence,
            timeout=0.5,
        )

    descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    status = ""
    while time.monotonic() < deadline:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(descendant_pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not status or status.startswith("Z"):
            break
        time.sleep(0.025)
    assert not status or status.startswith("Z"), "timed-out descendant remained running"


def test_stock_probe_records_ordered_phases_warnings_streams_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    upstream, revision = committed_project(tmp_path)
    classification = classification_for(tmp_path, revision)
    wheel, lock = recorder_inputs(tmp_path)
    bind_test_artifact(recorder, wheel)
    write_test_lock(recorder, lock)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "pytest.py").write_text("raise RuntimeError('shadow pytest imported')\n")
    monkeypatch.setenv("PYTHONPATH", str(shadow))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--definitely-invalid-option")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_PLUGINS", "missing_external_plugin")
    monkeypatch.setenv("PYTHONWARNINGS", "error")

    record = recorder.record_run(
        source="pytest",
        python=Path(sys.executable),
        upstream_root=upstream,
        classification_path=classification,
        runner_distribution="pytest",
        runner_artifacts=[wheel],
        dependency_locks=[lock],
        pytest_arguments=["-q", "test_observations.py"],
    )

    assert record["exit_status"] == 1
    assert record["collected"] == 1
    assert record["session_warnings"] == []
    assert record["provenance"]["upstream_commit"] == revision
    assert record["provenance"]["runner_version"] == pytest.__version__
    assert record["provenance"]["runner_artifacts"][0]["filename"] == "pytest.whl"
    case = record["cases"][0]
    assert case["nodeid"] == "test_observations.py::test_observations"
    assert [report["phase"] for report in case["reports"]] == ["setup", "call", "teardown"]
    assert [report["outcome"] for report in case["reports"]] == [
        "passed",
        "failed",
        "passed",
    ]
    assert case["reports"][0]["stdout"] == "setup-output\n"
    assert case["reports"][1]["stdout"] == ("call-output\n1 passed in 5s\n<value at 0x123abc>\n")
    assert case["reports"][1]["stderr"] == "call-error\n"
    assert "call-log" in case["reports"][1]["log"]
    assert case["reports"][2]["stderr"] == "teardown-output\n"
    assert "controlled failure" in case["reports"][1]["exception"]
    warning_records = case["warnings"]
    warnings = [warning["message"] for warning in warning_records]
    sentinels = ("setup-warning", "call-warning", "teardown-warning")
    assert [
        sentinel for sentinel in sentinels if any(sentinel in message for message in warnings)
    ] == list(sentinels)
    assert all(str(upstream) not in message for message in warnings)
    assert all(
        warning["filename"] == "<UPSTREAM>/test_observations.py" for warning in warning_records
    )
    accounting = load_execution_module("accounting")
    accounting.validate_provenance(record, "stock")
    accounting.validate_cases(
        record,
        "stock",
        ["test_observations.py::test_observations"],
    )


def test_stock_probe_normalizes_lifecycle_outcomes_and_keeps_outer_session(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    upstream, _ = committed_project(tmp_path)
    (upstream / "pytest.ini").write_text("[pytest]\naddopts = -p no:randomly\n", encoding="utf-8")
    (upstream / "test_lifecycle.py").write_text(
        """\
import subprocess
import sys

import pytest


@pytest.fixture
def setup_error():
    raise LookupError("setup sentinel")


@pytest.fixture
def teardown_error():
    yield
    raise RuntimeError("teardown sentinel")


def test_setup_error(setup_error):
    pass


def test_teardown_error(teardown_error):
    pass


@pytest.mark.xfail(reason="expected sentinel")
def test_xfail():
    raise ValueError("xfail sentinel")


@pytest.mark.xfail(reason="unexpected sentinel")
def test_xpass():
    pass


@pytest.mark.xfail(reason="strict sentinel", strict=True)
def test_strict_xpass():
    pass


def test_nested_session(tmp_path):
    source = tmp_path / "test_inner.py"
    source.write_text("def test_inner(): pass\\n")
    assert pytest.main(["-q", "-p", "pytest_execution_probe", str(source)]) == 0
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "pytest_execution_probe", str(source)],
        check=False,
    )
    assert completed.returncode == 0


def test_observer_identity(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("pytest_execution_probe")
""",
        encoding="utf-8",
    )
    git(upstream, "add", ".")
    subprocess.run(
        [
            "git",
            "-C",
            str(upstream),
            "-c",
            "user.name=vfy",
            "-c",
            "user.email=vfy@example.invalid",
            "commit",
            "-qm",
            "lifecycle fixture",
        ],
        check=True,
    )
    revision = git(upstream, "rev-parse", "HEAD")
    classification = classification_for(tmp_path, revision)
    wheel, lock = recorder_inputs(tmp_path)
    bind_test_artifact(recorder, wheel)
    write_test_lock(recorder, lock)

    record = recorder.record_run(
        source="pytest",
        python=Path(sys.executable),
        upstream_root=upstream,
        classification_path=classification,
        runner_distribution="pytest",
        runner_artifacts=[wheel],
        dependency_locks=[lock],
        pytest_arguments=["-q", "test_lifecycle.py"],
    )

    assert record["collected"] == 7
    assert [case["nodeid"] for case in record["cases"]] == [
        "test_lifecycle.py::test_setup_error",
        "test_lifecycle.py::test_teardown_error",
        "test_lifecycle.py::test_xfail",
        "test_lifecycle.py::test_xpass",
        "test_lifecycle.py::test_strict_xpass",
        "test_lifecycle.py::test_nested_session",
        "test_lifecycle.py::test_observer_identity",
    ]
    outcomes = {
        case["nodeid"].rsplit("::", 1)[1]: [report["outcome"] for report in case["reports"]]
        for case in record["cases"]
    }
    assert outcomes == {
        "test_setup_error": ["error", "passed"],
        "test_teardown_error": ["passed", "passed", "error"],
        "test_xfail": ["passed", "xfailed", "passed"],
        "test_xpass": ["passed", "xpassed", "passed"],
        "test_strict_xpass": ["passed", "failed", "passed"],
        "test_nested_session": ["passed", "passed", "passed"],
        "test_observer_identity": ["passed", "passed", "passed"],
    }
    strict_call = next(
        case for case in record["cases"] if case["nodeid"].endswith("::test_strict_xpass")
    )["reports"][1]
    assert strict_call["outcome"] == "failed"
    assert "XPASS(strict)" in strict_call["exception"]
    assert "strict sentinel" in strict_call["diagnostic"]
    assert all("test_inner" not in case["nodeid"] for case in record["cases"])


def test_warning_metadata_preserves_custom_warning_formatting(tmp_path: Path) -> None:
    probe = load_execution_module("probe")
    calls = []

    class CustomWarning(UserWarning):
        @override
        def __str__(self) -> str:
            calls.append("formatted")
            return "custom payload"

    recorder = probe._ExecutionRecorder(
        mode="pytest",
        root=str(tmp_path),
        temporary=str(tmp_path),
        runner=str(Path(sys.prefix)),
        raw_path=None,
        observer_path=str(tmp_path / "observer.json"),
        session_token="token",
        inventory_nodeids=frozenset(),
        inventory_nodeids_sha256="0" * 64,
    )
    recorder.pytest_warning_recorded(
        warnings.WarningMessage(CustomWarning("original"), CustomWarning, "project.py", 3),
        "collect",
        "",
        None,
    )
    assert calls == ["formatted"]
    assert recorder.warning_metadata == []
    assert "CustomWarning: custom payload" in recorder.warning_events[0][1]["message"]


def test_stock_marker_warning_metadata_identifies_the_installed_framework(tmp_path: Path) -> None:
    probe = load_execution_module("probe")
    recorder = probe._ExecutionRecorder(
        mode="pytest",
        root=str(tmp_path),
        temporary=str(tmp_path),
        runner=str(Path(sys.prefix)),
        raw_path=None,
        observer_path=str(tmp_path / "observer.json"),
        session_token="token",
        inventory_nodeids=frozenset(),
        inventory_nodeids_sha256="0" * 64,
    )
    payload = (
        "Passing a non-Collection iterable to parametrize is deprecated.\n"
        "Test: test_case.py::test_value, argvalues type: zip\n"
        "Please convert to a list or tuple.\n"
        "See https://docs.pytest.org/en/stable/deprecations.html#parametrize-iterators"
    )
    source = sys.modules["_pytest.python"].__file__
    assert source is not None
    recorder.pytest_warning_recorded(
        warnings.WarningMessage(
            pytest.PytestRemovedIn10Warning(payload), pytest.PytestRemovedIn10Warning, source, 124
        ),
        "collect",
        "",
        None,
    )
    [metadata] = recorder.warning_metadata
    assert metadata["raw_message"] == payload
    assert metadata["category"] == "PytestRemovedIn10Warning"
    assert metadata["emitter_route"] == "builtin-marker-parametrize"
    assert metadata["source_line"] == (
        "metafunc.parametrize(*marker.args, **marker.kwargs, _param_mark=marker)"
    )


def test_unattributed_warning_order_is_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probe = load_execution_module("probe")
    monkeypatch.setenv("VFY_PYTEST_RECORD_ROOT", str(tmp_path))
    recorder = probe._ExecutionRecorder(
        mode="pytest",
        root=str(tmp_path),
        temporary=str(tmp_path),
        runner=str(Path(sys.prefix)),
        raw_path=str(tmp_path / "raw.json"),
        observer_path=str(tmp_path / "observer.json"),
        session_token="token",
        inventory_nodeids=frozenset(),
        inventory_nodeids_sha256=hashlib.sha256(b"[]\n").hexdigest(),
    )
    for nodeid, text in (("a", "first"), ("b", "second"), ("a", "third")):
        recorder.pytest_warning_recorded(
            warnings.WarningMessage(UserWarning(text), UserWarning, f"{nodeid}.py", 1),
            "collect",
            nodeid,
            None,
        )

    cases, session_warnings = recorder.cases()

    assert cases == []
    assert [warning["nodeid"] for warning in session_warnings] == ["a", "b", "a"]
    assert all(
        token in warning["message"]
        for token, warning in zip(("first", "second", "third"), session_warnings, strict=True)
    )


def test_stock_probe_publishes_warnings_from_late_session_hooks(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    upstream, _ = committed_project(tmp_path)
    (upstream / "conftest.py").write_text(
        """\
import warnings

import pytest


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish():
    warnings.warn("late-session-warning", UserWarning)
""",
        encoding="utf-8",
    )
    git(upstream, "add", ".")
    subprocess.run(
        [
            "git",
            "-C",
            str(upstream),
            "-c",
            "user.name=vfy",
            "-c",
            "user.email=vfy@example.invalid",
            "commit",
            "-qm",
            "late warning fixture",
        ],
        check=True,
    )
    revision = git(upstream, "rev-parse", "HEAD")
    classification = classification_for(tmp_path, revision)
    wheel, lock = recorder_inputs(tmp_path)
    bind_test_artifact(recorder, wheel)
    write_test_lock(recorder, lock)

    record = recorder.record_run(
        source="pytest",
        python=Path(sys.executable),
        upstream_root=upstream,
        classification_path=classification,
        runner_distribution="pytest",
        runner_artifacts=[wheel],
        dependency_locks=[lock],
        pytest_arguments=["-q", "test_observations.py"],
    )

    assert any(
        "late-session-warning" in warning["message"] for warning in record["session_warnings"]
    )


def test_semantic_paths_are_normalized_without_rewriting_literal_user_text() -> None:
    probe = load_execution_module("probe")
    temporary = tempfile.gettempdir()
    resolved = str(Path(temporary).resolve())
    roots = ("/upstream", temporary, str(Path(sys.prefix)))
    assert probe._normalize(f"{resolved}/test_case.py", roots) == "<TMP>/test_case.py"
    literal = "<object at 0x123abc>\n1 passed in 0.42s\nfinished in 5s\n"
    assert probe._normalize(literal, roots) == literal


def test_pytest_basetemp_normalization_is_exact_and_handles_prior_tmp_mapping(
    tmp_path: Path,
) -> None:
    probe = load_execution_module("probe")
    upstream = tmp_path / "upstream"
    temporary = tmp_path / "temporary"
    runner = tmp_path / "runner"
    roots = (str(upstream), str(temporary), str(runner))
    base = temporary / "pytest-of-user" / "pytest-67542"
    other_base = temporary / "pytest-of-user" / "pytest-67543"

    assert (
        probe._normalize(f"{base}/case.py", roots, pytest_temporary=str(base))
        == "<PYTEST_TMP>/case.py"
    )
    already_normalized = probe._normalize(f"{base}/case.py", roots)
    assert already_normalized == "<TMP>/pytest-of-user/pytest-67542/case.py"
    assert (
        probe._normalize(already_normalized, roots, pytest_temporary=str(base))
        == "<PYTEST_TMP>/case.py"
    )
    assert (
        probe._normalize(f"{other_base}/case.py", roots, pytest_temporary=str(base))
        == "<TMP>/pytest-of-user/pytest-67543/case.py"
    )
    assert probe._normalize("literal clock 5s", roots, pytest_temporary=str(base)) == (
        "literal clock 5s"
    )


def test_stock_publish_normalizes_observed_basetemp_but_preserves_raw_diagnostics(
    tmp_path: Path,
) -> None:
    probe = load_execution_module("probe")
    upstream = tmp_path / "upstream"
    temporary = tmp_path / "temporary"
    runner = tmp_path / "runner"
    basetemp = temporary / "pytest-of-user" / "pytest-67542"
    diagnostic = f"raw diagnostic from {basetemp}/diagnostic.py"
    captured = f"captured output from {basetemp}/captured.py\n"
    nodeid = "test_observed_basetemp.py::test_failure"
    pytest_tmp_factory = SimpleNamespace(
        _basetemp=basetemp,
        getbasetemp=lambda: pytest.fail("publish must not create or fetch basetemp"),
    )
    recorder = probe._ExecutionRecorder(
        mode="pytest",
        root=str(upstream),
        temporary=str(temporary),
        runner=str(runner),
        raw_path=str(tmp_path / "record.json"),
        observer_path=str(tmp_path / "observer.json"),
        session_token="session",
        inventory_nodeids=frozenset({nodeid}),
        inventory_nodeids_sha256=hashlib.sha256(b"[]\n").hexdigest(),
        outer_config=SimpleNamespace(_tmp_path_factory=pytest_tmp_factory),
    )
    recorder.rootdir = str(upstream.resolve())
    recorder.collected = 1
    recorder.exit_status = 1
    recorder.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid=nodeid,
            outcome="failed",
            failed=True,
            when="call",
            longrepr=("test_observed_basetemp.py", 1, f"Failure at {basetemp}/error.py"),
            longreprtext=diagnostic,
            sections=[("Captured stdout call", captured)],
        )
    )
    recorder.pytest_warning_recorded(
        warnings.WarningMessage(
            UserWarning(f"warning from {basetemp}/warning.py"),
            UserWarning,
            str(basetemp / "warning.py"),
            7,
        ),
        "runtest",
        nodeid,
        None,
    )

    recorder._publish()

    observer = json.loads((tmp_path / "observer.json").read_text(encoding="utf-8"))
    record = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    report = record["cases"][0]["reports"][0]
    warning = record["cases"][0]["warnings"][0]
    assert observer["pytest_temporary"] == str(basetemp.resolve())
    assert report["exception"] == "Failure at <PYTEST_TMP>/error.py"
    assert report["diagnostic"] == diagnostic
    assert report["stdout"] == captured
    assert "<PYTEST_TMP>/warning.py" in warning["message"]
    assert warning["filename"] == "<PYTEST_TMP>/warning.py"


def test_native_cases_normalize_observed_basetemp_without_rewriting_diagnostics(
    tmp_path: Path,
) -> None:
    recorder = load_execution_module("record")
    upstream = tmp_path / "upstream"
    temporary = tmp_path / "temporary"
    runner = tmp_path / "runner"
    basetemp = temporary / "pytest-of-user" / "pytest-67542"
    diagnostic = f"raw diagnostic from {basetemp}/diagnostic.py"
    captured = f"captured output from {basetemp}/captured.py\n"
    raw = {
        "engine": "pytest-runner-native-v1",
        "warnings": [
            {
                "nodeid": "<session>",
                "message": f"warning from {basetemp}/warning.py",
                "filename": str(basetemp / "warning.py"),
                "lineno": 3,
            }
        ],
        "cases": [
            {
                "nodeid": "test_native_basetemp.py::test_failure",
                "phases": [
                    {
                        "phase": "call",
                        "outcome": "failed",
                        "exception": f"Failure at {basetemp}/error.py",
                        "detail": diagnostic,
                        "stdout": captured,
                        "stderr": "",
                        "log": "",
                    }
                ],
            }
        ],
    }

    cases = recorder.native_cases(raw, upstream, str(temporary), str(runner), str(basetemp))
    session_warnings = recorder.native_session_warnings(
        raw, cases, upstream, str(temporary), str(runner), str(basetemp)
    )
    report = cases[0]["reports"][0]

    assert report["exception"] == "Failure at <PYTEST_TMP>/error.py"
    assert report["diagnostic"] == diagnostic
    assert report["stdout"] == captured
    assert session_warnings[0]["message"] == "warning from <PYTEST_TMP>/warning.py"
    assert session_warnings[0]["filename"] == "<PYTEST_TMP>/warning.py"


def test_captured_test_output_preserves_pytest_shaped_text(tmp_path: Path) -> None:
    probe = load_execution_module("probe")
    recorder = load_execution_module("record")
    roots = (str(tmp_path), tempfile.gettempdir(), str(Path(sys.prefix)))
    output = "test_demo.py . 1ms\n1 passed in 5s\n"
    observer = probe._ExecutionRecorder(
        mode="pytest",
        root=roots[0],
        temporary=roots[1],
        runner=roots[2],
        raw_path=None,
        observer_path="unused.json",
        session_token="session",
        inventory_nodeids=frozenset(),
        inventory_nodeids_sha256=hashlib.sha256(b"[]\n").hexdigest(),
    )
    observer.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="test_output.py::test_prints_pytest_like_text",
            outcome="passed",
            failed=False,
            when="call",
            sections=[
                ("Captured stdout call", output),
                ("Captured stderr call", output),
                ("Captured log call", output),
            ],
        )
    )
    raw_native = {
        "engine": "pytest-runner-native-v1",
        "warnings": [],
        "cases": [
            {
                "nodeid": "test_output.py::test_prints_pytest_like_text",
                "phases": [
                    {
                        "phase": "call",
                        "outcome": "passed",
                        "exception": None,
                        "detail": "",
                        "stdout": output,
                        "stderr": output,
                        "log": output,
                    }
                ],
            }
        ],
    }
    native_report = recorder.native_cases(raw_native, tmp_path)[0]["reports"][0]

    assert observer.reports[0]["stdout"] == output
    assert observer.reports[0]["stderr"] == output
    assert observer.reports[0]["log"] == output
    assert native_report["stdout"] == output
    assert native_report["stderr"] == output
    assert native_report["log"] == output


def test_stock_and_native_canonicalize_only_nonexecuted_xfail_diagnostics(
    tmp_path: Path,
) -> None:
    probe = load_execution_module("probe")
    recorder = load_execution_module("record")
    roots = (str(tmp_path), tempfile.gettempdir(), str(Path(sys.prefix)))
    observer = probe._ExecutionRecorder(
        mode="pytest",
        root=roots[0],
        temporary=roots[1],
        runner=roots[2],
        raw_path=None,
        observer_path="unused.json",
        session_token="session",
        inventory_nodeids=frozenset(),
        inventory_nodeids_sha256=hashlib.sha256(b"[]\n").hexdigest(),
    )
    observer.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="test_xfail.py::test_not_run",
            outcome="skipped",
            skipped=True,
            failed=False,
            when="setup",
            wasxfail="[NOTRUN] not ready",
            longrepr="pytest setup traceback",
            longreprtext="pytest setup traceback",
            sections=[],
        )
    )
    observer.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="test_xfail.py::test_runtime",
            outcome="skipped",
            skipped=True,
            failed=False,
            when="call",
            wasxfail="runtime reason",
            longrepr="pytest runtime traceback",
            longreprtext="pytest runtime traceback",
            sections=[],
        )
    )
    stock_cases, _ = observer.cases()

    native_cases = recorder.native_cases(
        {
            "engine": "pytest-runner-native-v1",
            "warnings": [],
            "cases": [
                {
                    "nodeid": "test_xfail.py::test_not_run",
                    "phases": [
                        {
                            "phase": "setup",
                            "outcome": "xfailed",
                            "exception": "[NOTRUN] not ready",
                            "detail": "[NOTRUN] not ready",
                            "stdout": "",
                            "stderr": "",
                            "log": "",
                        }
                    ],
                },
                {
                    "nodeid": "test_xfail.py::test_runtime",
                    "phases": [
                        {
                            "phase": "call",
                            "outcome": "xfailed",
                            "exception": "runtime reason",
                            "detail": "pytest runtime traceback",
                            "stdout": "",
                            "stderr": "",
                            "log": "",
                        }
                    ],
                },
            ],
        },
        tmp_path,
    )

    assert [case["reports"][0]["diagnostic"] for case in stock_cases] == [
        "[NOTRUN] not ready",
        "pytest runtime traceback",
    ]
    assert [case["reports"][0]["diagnostic"] for case in native_cases] == [
        "[NOTRUN] not ready",
        "pytest runtime traceback",
    ]


@pytest.mark.parametrize("kind", ["failure", "notrun", "subtest"])
def test_user_diagnostic_timing_strings_remain_distinct(tmp_path: Path, kind: str) -> None:
    probe = load_execution_module("probe")
    recorder = load_execution_module("record")
    roots = (str(tmp_path), tempfile.gettempdir(), str(Path(sys.prefix)))
    diagnostics = []
    for seconds in (5, 6):
        prefix = "[NOTRUN]" if kind == "notrun" else "RuntimeError:"
        detail = f"{prefix} ==== 1 passed in {seconds}s ===="
        exception = detail if kind == "notrun" else "RuntimeError: user failure"
        phase = "setup" if kind == "notrun" else "call"
        outcome = "xfailed" if kind == "notrun" else "failed"
        observer = probe._ExecutionRecorder(
            mode="pytest",
            root=roots[0],
            temporary=roots[1],
            runner=roots[2],
            raw_path=None,
            observer_path="unused.json",
            session_token="session",
            inventory_nodeids=frozenset(),
            inventory_nodeids_sha256=hashlib.sha256(b"[]\n").hexdigest(),
        )
        report = SimpleNamespace(
            nodeid="test_payload.py::test_payload",
            outcome="skipped" if kind == "notrun" else "failed",
            skipped=kind == "notrun",
            failed=kind != "notrun",
            when=phase,
            longrepr=("test_payload.py", 1, exception),
            longreprtext=detail,
            sections=[],
        )
        if kind == "notrun":
            report.wasxfail = detail
        observer.pytest_runtest_logreport(report)
        if kind == "subtest":
            native = recorder._native_subtest_reports(
                [
                    {
                        "outcome": outcome,
                        "context": "(value=1)",
                        "exception": exception,
                        "detail": detail,
                    }
                ],
                report.nodeid,
                tmp_path,
                roots[1],
                roots[2],
            )[0]
        else:
            native = recorder._native_phase_report(
                {
                    "phase": phase,
                    "outcome": outcome,
                    "exception": exception,
                    "detail": detail,
                    "stdout": "",
                    "stderr": "",
                    "log": "",
                },
                report.nodeid,
                0,
                tmp_path,
                roots[1],
                roots[2],
            )
        assert observer.reports[0]["diagnostic"] == native["diagnostic"] == detail
        diagnostics.append(native["diagnostic"])
    assert diagnostics[0] != diagnostics[1]


def test_native_record_requires_and_preserves_warning_multiplicity(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    raw = {
        "engine": "pytest-runner-native-v1",
        "producer_pid": 123,
        "session_token": "token",
        "collected": 1,
        "warnings": [
            {
                "nodeid": "test_case.py::test_it",
                "message": "first",
                "filename": f"{tmp_path}/test_case.py",
                "lineno": 4,
            },
            {
                "nodeid": "test_case.py::test_it",
                "message": "second",
                "filename": f"{tmp_path}/test_case.py",
                "lineno": 5,
            },
        ],
        "cases": [
            {
                "nodeid": "test_case.py::test_it",
                "phases": [
                    {
                        "phase": "run",
                        "outcome": "failed",
                        "exception": "AssertionError: assertion failed",
                        "detail": f"{tmp_path}/test_case.py: assertion failed",
                        "stdout": "hello\n",
                        "stderr": "",
                        "log": "event\n",
                    }
                ],
            }
        ],
    }

    cases = recorder.native_cases(raw, tmp_path)

    assert cases == [
        {
            "nodeid": "test_case.py::test_it",
            "reports": [
                {
                    "phase": "call",
                    "outcome": "failed",
                    "exception": "AssertionError: assertion failed",
                    "diagnostic": f"{tmp_path}/test_case.py: assertion failed",
                    "stdout": "hello\n",
                    "stderr": "",
                    "log": "event\n",
                }
            ],
            "warnings": [
                {"message": "first", "filename": "<UPSTREAM>/test_case.py", "lineno": 4},
                {"message": "second", "filename": "<UPSTREAM>/test_case.py", "lineno": 5},
            ],
        }
    ]

    raw.pop("warnings")
    with pytest.raises(ValueError, match="no warning observations"):
        recorder.native_cases(raw, tmp_path)


def test_native_xpass_reason_is_not_failure_evidence(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    upstream, _ = committed_project(tmp_path)
    (upstream / "test_observations.py").write_text(
        "import pytest\n"
        "@pytest.mark.xfail(reason='expected failure', strict=False)\n"
        "def test_observations(): pass\n",
        encoding="utf-8",
    )
    git(upstream, "add", ".")
    git(
        upstream,
        "-c",
        "user.name=vfy",
        "-c",
        "user.email=vfy@example.invalid",
        "commit",
        "-qm",
        "expected failure passes",
    )
    classified = classification_for(tmp_path, git(upstream, "rev-parse", "HEAD"))
    wheel, lock = recorder_inputs(tmp_path)
    bind_test_artifact(recorder, wheel)
    write_test_lock(recorder, lock)
    stock = recorder.record_run(
        source="pytest",
        python=Path(sys.executable),
        upstream_root=upstream,
        classification_path=classified,
        runner_distribution="pytest",
        runner_artifacts=[wheel],
        dependency_locks=[lock],
        pytest_arguments=["-q", "test_observations.py"],
    )
    nodeid = "test_observations.py::test_observations"
    native = recorder.native_cases(
        {
            "engine": "pytest-runner-native-v1",
            "warnings": [],
            "cases": [
                {
                    "nodeid": nodeid,
                    "phases": [
                        {
                            **report,
                            "detail": "expected failure" if report["outcome"] == "xpassed" else "",
                        }
                        for report in stock["cases"][0]["reports"]
                    ],
                }
            ],
        },
        upstream,
    )
    assert stock["exit_status"] == 0
    assert stock["cases"][0]["reports"][1]["outcome"] == "xpassed"
    assert native == stock["cases"]
    assert (
        load_execution_module("accounting").validate_cases({"cases": native}, "native", [nodeid])
        == native
    )


def test_accounting_accepts_real_unittest_subtest_reports(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    upstream, _ = committed_project(tmp_path)
    (upstream / "test_observations.py").write_text(
        """\
import unittest


class TestCase(unittest.TestCase):
    def test_subtests(self):
        for value in (1, 2):
            with self.subTest(value=value):
                self.assertEqual(value, 1)
""",
        encoding="utf-8",
    )
    git(upstream, "add", ".")
    git(
        upstream,
        "-c",
        "user.name=vfy",
        "-c",
        "user.email=vfy@example.invalid",
        "commit",
        "-qm",
        "unittest subtests",
    )
    classified = classification_for(tmp_path, git(upstream, "rev-parse", "HEAD"))
    classification = json.loads(classified.read_text(encoding="utf-8"))
    nodeid = "test_observations.py::TestCase::test_subtests"
    classification["classifications"][0]["nodeid"] = nodeid
    recorder.write_atomic(classified, classification)
    wheel, lock = recorder_inputs(tmp_path)
    bind_test_artifact(recorder, wheel)
    write_test_lock(recorder, lock)

    record = recorder.record_run(
        source="pytest",
        python=Path(sys.executable),
        upstream_root=upstream,
        classification_path=classified,
        runner_distribution="pytest",
        runner_artifacts=[wheel],
        dependency_locks=[lock],
        pytest_arguments=["-q", "test_observations.py"],
    )

    reports = record["cases"][0]["reports"]
    assert [(report["phase"], report["outcome"]) for report in reports] == [
        ("setup", "passed"),
        ("call", "passed"),
        ("call", "failed"),
        ("call", "passed"),
        ("teardown", "passed"),
    ]
    assert record["exit_status"] == 1
    assert (
        load_execution_module("accounting").validate_cases(record, "stock", [nodeid])
        == record["cases"]
    )


def test_native_record_preserves_ordered_subtest_reports(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    raw = {
        "engine": "pytest-runner-native-v1",
        "warnings": [],
        "cases": [
            {
                "nodeid": "test_case.py::TestCase::test_subtests",
                "phases": [
                    {
                        "phase": "setup",
                        "outcome": "passed",
                        "exception": None,
                        "detail": "",
                        "stdout": "",
                        "stderr": "",
                        "log": "",
                    },
                    {
                        "phase": "run",
                        "outcome": "passed",
                        "exception": None,
                        "detail": "",
                        "stdout": "",
                        "stderr": "",
                        "log": "",
                    },
                    {
                        "phase": "teardown",
                        "outcome": "passed",
                        "exception": None,
                        "detail": "",
                        "stdout": "",
                        "stderr": "",
                        "log": "",
                    },
                ],
                "subtests": [
                    {
                        "outcome": "passed",
                        "context": "(value='ok')",
                        "exception": None,
                        "detail": "",
                    },
                    {
                        "outcome": "failed",
                        "context": "[second] (value='bad')",
                        "exception": "AssertionError: 1 != 2",
                        "detail": f"{tmp_path}/test_case.py: assert 1 == 2",
                    },
                ],
            }
        ],
    }

    cases = recorder.native_cases(raw, tmp_path)

    assert [
        (report["phase"], report["outcome"], report["exception"], report["diagnostic"])
        for report in cases[0]["reports"]
    ] == [
        ("setup", "passed", None, None),
        ("call", "passed", None, None),
        (
            "call",
            "failed",
            "AssertionError: 1 != 2",
            f"{tmp_path}/test_case.py: assert 1 == 2",
        ),
        ("call", "passed", None, None),
        ("teardown", "passed", None, None),
    ]


@pytest.mark.parametrize("location", [None, {"path": "test_case.py", "line": 12}])
def test_native_subtest_skip_location_preserves_literal_diagnostic(
    tmp_path: Path, location: dict[str, object] | None
) -> None:
    recorder = load_execution_module("record")
    subtest: dict[str, object] = {
        "outcome": "skipped",
        "context": "(value=1)",
        "exception": "Skipped: later",
        "detail": "('test_case.py', 12, 'Skipped: later')",
    }
    if location is not None:
        subtest["skip_location"] = location
    reports = recorder._native_subtest_reports(
        [subtest], "test_case.py::test_case", tmp_path, None, None
    )
    assert reports[0]["diagnostic"] == subtest["detail"]
    subtest["skip_location"] = {"path": "test_case.py", "line": -1}
    with pytest.raises(ValueError, match="invalid skip location"):
        recorder._native_subtest_reports([subtest], "test_case.py::test_case", tmp_path, None, None)


def test_native_record_requires_outer_session_identity(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    observer = {"observer_nonce": "a" * 64}
    raw = {
        "observer_nonce": observer["observer_nonce"],
        "session_depth": 0,
        "rootdir": str(tmp_path.resolve()),
    }
    recorder.validate_native_identity(raw, observer, tmp_path)

    raw["session_depth"] = 1
    with pytest.raises(ValueError, match="outer pytest session"):
        recorder.validate_native_identity(raw, observer, tmp_path)

    raw["session_depth"] = 0
    raw["rootdir"] = str((tmp_path / "nested").resolve())
    with pytest.raises(ValueError, match="outer pytest session"):
        recorder.validate_native_identity(raw, observer, tmp_path)

    raw["rootdir"] = str(tmp_path.resolve())
    raw["observer_nonce"] = "b" * 64
    with pytest.raises(ValueError, match="outer pytest session"):
        recorder.validate_native_identity(raw, observer, tmp_path)


def test_upstream_checkout_must_be_exact_and_clean(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    upstream, revision = committed_project(tmp_path)
    classification = {"inventory": {"upstream_commit": revision}}
    assert recorder.validate_upstream(upstream, classification) == revision

    (upstream / "test_observations.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be clean"):
        recorder.validate_upstream(upstream, classification)


def test_upstream_checkout_rejects_ignored_runtime_state(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    upstream, _ = committed_project(tmp_path)
    (upstream / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
    git(upstream, "add", ".gitignore")
    subprocess.run(
        [
            "git",
            "-C",
            str(upstream),
            "-c",
            "user.name=vfy",
            "-c",
            "user.email=vfy@example.invalid",
            "commit",
            "-qm",
            "ignore runtime cache",
        ],
        check=True,
    )
    classification = {"inventory": {"upstream_commit": git(upstream, "rev-parse", "HEAD")}}
    cache = upstream / ".pytest_cache"
    cache.mkdir()
    (cache / "state").write_text("stale", encoding="utf-8")

    with pytest.raises(ValueError, match="must be pristine"):
        recorder.validate_upstream(upstream, classification)


def test_artifact_identity_is_order_independent_and_unambiguous(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    first = tmp_path / "first.whl"
    second = tmp_path / "second.whl"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    expected = recorder.artifact_records([first, second], "runner artifact")

    assert recorder.artifact_records([second, first], "runner artifact") == expected

    duplicate = tmp_path / "nested" / "first.whl"
    duplicate.parent.mkdir()
    duplicate.write_bytes(b"different")
    with pytest.raises(ValueError, match="filenames must be unique"):
        recorder.artifact_records([first, duplicate], "runner artifact")


def test_runner_wheel_must_be_bound_to_the_invoked_interpreter(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    wheel = tmp_path / f"pytest-{pytest.__version__}-py3-none-any.whl"
    write_test_wheel(wheel, "pytest", pytest.__version__, {})

    assert recorder.wheel_identity(wheel) == ("pytest", pytest.__version__)
    with pytest.raises(ValueError, match="not provenance-bound"):
        recorder.verify_installed_artifacts(Path(sys.executable), [wheel], "pytest")


def test_execution_requires_the_pinned_pytest_and_expected_runner() -> None:
    recorder = load_execution_module("record")
    pytest_artifact = [
        {
            "distribution": "pytest",
            "version": recorder.PINNED_PYTEST_VERSION,
            "filename": "pytest.whl",
            "sha256": "a" * 64,
        }
    ]
    recorder.validate_runner_artifacts("pytest", "pytest", pytest_artifact)
    native_artifact = {
        "distribution": "vfy",
        "version": "0.1.0",
        "filename": "vfy.whl",
        "sha256": "b" * 64,
    }
    vfy_pytest_artifact = pytest_artifact[0] | {"filename": "pytest-native.whl"}
    recorder.validate_runner_artifacts("native", "vfy", [vfy_pytest_artifact, native_artifact])

    with pytest.raises(ValueError, match="requires pytest as the runner"):
        recorder.validate_runner_artifacts("pytest", "unrelated", pytest_artifact)
    with pytest.raises(ValueError, match="unknown execution source"):
        recorder.validate_runner_artifacts("experimental", "vfy", pytest_artifact)
    with pytest.raises(ValueError, match="requires vfy as the runner distribution"):
        recorder.validate_runner_artifacts(
            "native", "unlisted-runner", [vfy_pytest_artifact, native_artifact]
        )
    with pytest.raises(ValueError, match="requires exactly"):
        recorder.validate_runner_artifacts(
            "native",
            "vfy",
            [
                native_artifact,
            ],
        )
    with pytest.raises(ValueError, match="requires exactly"):
        recorder.validate_runner_artifacts(
            "native",
            "vfy",
            [
                native_artifact,
                vfy_pytest_artifact,
                native_artifact | {"distribution": "pluggy", "filename": "pluggy.whl"},
            ],
        )
    with pytest.raises(ValueError, match="byte-verified pytest"):
        recorder.validate_runner_artifacts(
            "pytest",
            "pytest",
            [pytest_artifact[0] | {"version": "9.1.0"}],
        )
    with pytest.raises(ValueError, match=r"pytest 9\.1\.1"):
        recorder.validate_runner_artifacts(
            "native",
            "vfy",
            [pytest_artifact[0] | {"version": "9.1.0"}, native_artifact],
        )


def test_execution_requires_pytest_from_the_installed_interpreter(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    metadata = recorder.interpreter_metadata(Path(sys.executable), "pytest")
    installed = str((Path(metadata["purelib"]) / "pytest" / "__init__.py").resolve())
    recorder.validate_pytest_origin({"pytest_origin": installed}, metadata["purelib"])

    with pytest.raises(ValueError, match="execution imported pytest"):
        recorder.validate_pytest_origin(
            {"pytest_origin": str(tmp_path / "pytest" / "__init__.py")},
            metadata["purelib"],
        )


def test_wheel_manifest_maps_payload_and_ignores_mutable_record(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    dist_info = "example-1.0.dist-info"
    write_test_wheel(
        wheel,
        "example",
        "1.0",
        {
            "example/__init__.py": "VALUE = 1\n",
            "example-1.0.data/purelib/example/pure.py": "PURE = 1\n",
            "example-1.0.data/platlib/example/native.so": b"native",
            f"{dist_info}/RECORD": "rewritten",
            f"{dist_info}/RECORD.jws": "signature",
            f"{dist_info}/RECORD.p7s": "signature",
        },
    )

    manifest = recorder.wheel_install_manifest(wheel)
    destinations = {(row["scheme"], row["relative_path"]) for row in manifest}

    assert ("purelib", "example/__init__.py") in destinations
    assert ("purelib", "example/pure.py") in destinations
    assert ("platlib", "example/native.so") in destinations
    assert not any("RECORD" in relative for _, relative in destinations)


@pytest.mark.parametrize(
    ("unsafe_path", "match"),
    [
        ("example-1.0.data/scripts/tool", "unsupported install scheme"),
        ("../escape.py", "unsafe path"),
        ("example\\escape.py", "unsafe path"),
    ],
)
def test_wheel_manifest_rejects_unsupported_or_unsafe_paths(
    tmp_path: Path, unsafe_path: str, match: str
) -> None:
    recorder = load_execution_module("record")
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    write_test_wheel(wheel, "example", "1.0", {unsafe_path: "unsafe"})

    with pytest.raises(ValueError, match=match):
        recorder.wheel_install_manifest(wheel)


def test_installed_payload_must_match_the_bound_wheel(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    write_test_wheel(wheel, "example", "1.0", {"example/__init__.py": "VALUE = 1\n"})

    def changed_installation(python: Path, manifests: list[dict[str, object]]) -> dict[str, object]:
        del python
        files = json.loads(json.dumps(manifests[0]["files"]))
        for row in files:
            row["installed_sha256"] = row["sha256"]
        payload = next(row for row in files if row["relative_path"] == "example/__init__.py")
        payload["installed_sha256"] = "0" * 64
        return {
            "example": {
                "version": "1.0",
                "direct_url": {"url": wheel.resolve().as_uri()},
                "files": files,
            }
        }

    recorder.__dict__["installed_artifacts"] = changed_installation

    with pytest.raises(ValueError, match=r"payload differs.*example/__init__.py"):
        recorder.verify_installed_artifacts(Path(sys.executable), [wheel], "example")


def test_dependency_lock_must_match_the_invoked_interpreter(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    environment = recorder.resolved_environment(Path(sys.executable))
    lock = tmp_path / "environment.json"
    recorder.write_atomic(lock, environment)
    recorder.validate_dependency_lock(lock, environment)

    changed = json.loads(lock.read_text(encoding="utf-8"))
    changed["packages"].pop()
    recorder.write_atomic(lock, changed)
    with pytest.raises(ValueError, match="does not match the invoked interpreter"):
        recorder.validate_dependency_lock(lock, environment)


def test_record_hashes_the_resolved_environment_object(tmp_path: Path) -> None:
    recorder = load_execution_module("record")
    upstream, revision = committed_project(tmp_path)
    classification = classification_for(tmp_path, revision)
    wheel, lock = recorder_inputs(tmp_path)
    bind_test_artifact(recorder, wheel)
    write_test_lock(recorder, lock)

    record = recorder.record_run(
        source="pytest",
        python=Path(sys.executable),
        upstream_root=upstream,
        classification_path=classification,
        runner_distribution="pytest",
        runner_artifacts=[wheel],
        dependency_locks=[lock],
        pytest_arguments=["-q", "test_observations.py"],
    )

    resolved = json.loads(lock.read_text(encoding="utf-8"))
    expected = hashlib.sha256(
        json.dumps(resolved, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert record["provenance"]["resolved_environment"] == resolved
    assert record["provenance"]["resolved_environment_sha256"] == expected


def test_atomic_record_failure_preserves_existing_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder = load_execution_module("record")
    destination = tmp_path / "record.json"
    destination.write_text("previous\n", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        del source, target
        raise OSError("replace sentinel")

    monkeypatch.setattr(recorder.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace sentinel"):
        recorder.write_atomic(destination, {"new": True})

    assert destination.read_text(encoding="utf-8") == "previous\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_relative_interpreter_keeps_venv_identity_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = tmp_path / "runner"
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(environment)
    executable = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    monkeypatch.chdir(tmp_path)
    resolved = load_execution_module("record").resolve_executable(executable.relative_to(tmp_path))
    project = VfyProject(tmp_path / "project")
    project.write("identity.py", "import sys\nprint(sys.prefix)\n")
    result = project.run(str(resolved), "identity.py")
    result.expect(0)
    assert Path(result.stdout.strip()).resolve() == environment.resolve()
