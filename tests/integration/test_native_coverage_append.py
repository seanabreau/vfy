"""Native coverage appends through the same atomic publication as pytest-cov."""

from __future__ import annotations

from pathlib import Path

import pytest
from coverage import CoverageData

from tests.support.platforms import requires_native_pytest
from tests.support.vfy_project import VfyProject

pytestmark = requires_native_pytest


def _snapshot(project: VfyProject, name: str) -> tuple[list[int], dict[int, list[str]]]:
    data = project.data(name)
    try:
        source = str(project.root / "app.py")
        return sorted(data.lines(source) or []), {
            line: sorted(contexts) for line, contexts in data.contexts_by_lineno(source).items()
        }
    finally:
        data.close(force=True)


@pytest.mark.parametrize("workers", [1, 2, "xdist"])
def test_native_append_preserves_prior_contexts_without_exposing_them_to_collector(
    tmp_path: Path, workers: int | str
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("app.py", "def value():\n    return 42\n\ndef prior():\n    return 0\n")
    project.write(
        "test_app.py",
        "from app import value\n"
        "def test_value(cov):\n"
        "    assert 'prior' not in cov.get_data().measured_contexts()\n"
        "    assert value() == 42\n",
    )
    observations = {}
    for provider in ("reference", "native"):
        filename = f"{provider}.coverage"
        data = CoverageData(basename=str(project.root / filename))
        try:
            data.set_context("prior")
            data.add_lines({str(source): [5]})
            data.write()
        finally:
            data.close(force=True)
        arguments = ("--cov=app", "--cov-append", "--cov-context=test", "--cov-report=", "-q")
        environment = {"COVERAGE_FILE": str(project.root / filename)}
        if provider == "reference":
            result = project.run_python(
                "-m", "pytest", "-p", "pytest_cov.plugin", *arguments, env=environment
            )
        else:
            worker_arguments = (
                ("--numprocesses=2",) if workers == "xdist" else ("--workers", str(workers))
            )
            result = project.run_vfy(
                "test",
                "--engine=pytest-native",
                *worker_arguments,
                *arguments,
                env=environment,
            )
        result.expect(0)
        observations[provider] = _snapshot(project, filename)
    assert observations["native"] == observations["reference"]
    assert observations["native"][0] == [1, 2, 4, 5]
    assert observations["native"][1][5] == ["prior"]


def test_native_append_rejects_mixed_coverage_before_replacing_prior_data(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    source = project.write("app.py", "def value():\n    return 42\n")
    project.write(
        "test_app.py", "from app import value\ndef test_value():\n    assert value() == 42\n"
    )
    data = CoverageData(basename=str(project.root / ".coverage"))
    try:
        data.add_arcs({str(source): [(-1, 1), (1, -1)]})
        data.write()
    finally:
        data.close(force=True)
    original = (project.root / ".coverage").read_bytes()
    reference = project.run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_cov.plugin",
        "--cov=app",
        "--cov-append",
        "--cov-report=",
        "-q",
    )
    assert reference.returncode != 0, reference.stdout + reference.stderr
    assert (project.root / ".coverage").read_bytes() == original
    native = project.run_vfy(
        "test",
        "--engine=pytest-native",
        "--workers=1",
        "--cov=app",
        "--cov-append",
        "--cov-report=",
        "-q",
    )
    assert native.returncode != 0, native.stdout + native.stderr
    assert "cannot combine line and branch coverage data" in native.stdout + native.stderr
    assert (project.root / ".coverage").read_bytes() == original
