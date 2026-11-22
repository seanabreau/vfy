"""Focused checks for the no-coverage public-command benchmark gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import venv
from pathlib import Path
from zipfile import ZipFile

import pytest
from scripts.pytest_execution_record import verify_installed_artifacts
from validation.pytest_runner.benchmark_public_pytest import (
    Stratum,
    clear_fixture_bytecode,
    controlled_environment,
    create_project,
    junit_outcomes,
    run_preflight,
    validate_stratum,
    verify_candidate,
)


def test_junit_gate_compares_ordered_names_and_ignores_duration(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(
        "<testsuite tests='2'>"
        "<testcase classname='test_workload' name='test_case[case-0000]' time='0.01'/>"
        "<testcase classname='test_workload' name='test_case[case-0001]' time='0.02'>"
        "<failure message='expected failure'/></testcase>"
        "</testsuite>",
        encoding="utf-8",
    )

    assert junit_outcomes(report, 2) == [
        {"classname": "test_workload", "name": "test_case[case-0000]", "outcome": "passed"},
        {"classname": "test_workload", "name": "test_case[case-0001]", "outcome": "failure"},
    ]


def test_project_bytecode_clear_leaves_source_and_removes_pycs(tmp_path: Path) -> None:
    project = tmp_path / "project"
    create_project(project, 1)
    cache = project / "__pycache__"
    cache.mkdir()
    (cache / "test_workload.cpython-314.pyc").write_bytes(b"bytecode")

    clear_fixture_bytecode(project)

    assert not cache.exists()
    assert (project / "test_workload.py").is_file()


def test_parity_preflight_runs_the_public_commands(tmp_path: Path) -> None:
    project = tmp_path / "project"
    create_project(project, 1)
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    env = controlled_environment(home, temporary)

    stock = run_preflight("pytest", Path(sys.executable), project, tmp_path / "stock", 1, env, 120)
    vfy = run_preflight("vfy", Path(sys.executable), project, tmp_path / "vfy", 1, env, 120)

    assert stock["outcomes"] == vfy["outcomes"]
    assert stock["outcomes"][0] == {
        "classname": "test_workload",
        "name": "test_case[case-0000]",
        "outcome": "passed",
    }
    assert (tmp_path / "stock" / "command.json").is_file()
    assert (tmp_path / "vfy" / "junit.xml").is_file()


@pytest.mark.parametrize(
    "selected_cases",
    [
        (9_999,),
        (0, 1_111, 2_222, 3_333, 4_444, 5_555, 6_666, 7_777, 8_888, 9_999),
    ],
)
def test_explicit_selection_preflight_runs_only_requested_cases(
    tmp_path: Path, selected_cases: tuple[int, ...]
) -> None:
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    home.mkdir()
    temporary.mkdir()
    stratum = Stratum("explicit-selection", 10_000, "warm-bytecode", selected_cases)

    gate = validate_stratum(
        stratum,
        Path(sys.executable),
        tmp_path,
        controlled_environment(home, temporary),
        120,
    )

    expected = [
        {
            "classname": "test_workload",
            "name": f"test_case[case-{case:04d}]",
            "outcome": "passed",
        }
        for case in selected_cases
    ]
    assert gate["preflight"]["pytest"]["outcomes"] == expected
    assert gate["preflight"]["vfy"]["outcomes"] == expected
    for proof in gate["preflight"].values():
        with pytest.raises(RuntimeError, match="JUnit node IDs differ"):
            junit_outcomes(Path(proof["junit"]), 10_000)
        wrong_selection = (*selected_cases[:-1], selected_cases[-1] - 1)
        with pytest.raises(RuntimeError, match="JUnit node IDs differ"):
            junit_outcomes(Path(proof["junit"]), 10_000, wrong_selection)


def test_candidate_rejects_modified_installed_python_payload(tmp_path: Path) -> None:
    environment = tmp_path / "runner"
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    completed = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    )
    purelib = Path(completed.stdout.strip())
    wheel = tmp_path / "vfy-0.1.0-py3-none-any.whl"
    extension = b"native extension payload\n"
    dist_info = "vfy-0.1.0.dist-info"
    payload = {
        f"{dist_info}/METADATA": b"Name: vfy\nVersion: 0.1.0\n",
        f"{dist_info}/WHEEL": (b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"),
        "vfy/_native.abi3.so": extension,
        "vfy/_pytest/native_pytest.py": b"ADAPTER = True\n",
    }
    with ZipFile(wheel, "w") as archive:
        for relative, contents in payload.items():
            archive.writestr(relative, contents)
            destination = purelib / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
    (purelib / dist_info / "direct_url.json").write_text(
        json.dumps({"url": wheel.resolve().as_uri()}), encoding="utf-8"
    )
    verify_installed_artifacts(python, [wheel], "vfy")
    (purelib / "vfy/_pytest/native_pytest.py").write_text("ADAPTER = False\n", encoding="utf-8")
    manifest = tmp_path / "build.json"
    manifest.write_text(
        json.dumps(
            {
                "source_revision": "a" * 40,
                "source_root": str(Path(__file__).resolve().parents[2]),
                "native_profile": "release",
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "extension_sha256": hashlib.sha256(extension).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"payload differs.*vfy/_pytest/native_pytest.py"):
        verify_candidate(python, wheel, manifest, "a" * 40)
