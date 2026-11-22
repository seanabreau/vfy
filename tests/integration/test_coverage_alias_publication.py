"""Pytest coverage publishes parent aliases with the same schema as pytest-cov."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from coverage import CoverageData

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest


def _snapshot(project: VfyProject, filename: str) -> dict[str, object]:
    data = project.data(filename)
    try:
        return {
            "has_arcs": data.has_arcs(),
            "files": {
                name: {
                    "lines": sorted(data.lines(name) or []),
                    "arcs": sorted(data.arcs(name) or []),
                    "contexts": {
                        line: sorted(values)
                        for line, values in data.contexts_by_lineno(name).items()
                    },
                    "tracer": data.file_tracer(name),
                }
                for name in sorted(data.measured_files())
            },
        }
    finally:
        data.close(force=True)


@pytest.mark.parametrize(
    ("route", "append"),
    [
        ("plugin", False),
        ("plugin", True),
        ("native", False),
        ("native", True),
        ("pytest-native", True),
    ],
)
def test_parent_alias_publication_matches_pytest_cov(
    tmp_path: Path, route: str, append: bool
) -> None:
    project = VfyProject(tmp_path)
    source = project.write(
        "src/pkg/__init__.py", "def value(flag):\n    if flag:\n        return 1\n    return 0\n"
    )
    project.write("shadow/__init__.py", "")
    project.write(
        "test_value.py",
        "import pkg\n\ndef test_value(cov):\n"
        "    assert 'prior' not in cov.get_data().measured_contexts()\n"
        "    assert pkg.value(True) == 1\n    assert pkg.value(False) == 0\n",
    )
    project.write(
        "pyproject.toml",
        "[tool.pytest.ini_options]\npythonpath = ['src']\n"
        "[tool.coverage.run]\nsource = ['pkg']\nbranch = true\nparallel = true\n"
        "[tool.coverage.paths]\nother = ['.', '*/pkg']\nsource = ['src', '**/src']\n",
    )
    observations = {}
    for provider in ("reference", "actual"):
        destination = project.root / f"{provider}.coverage"
        if append:
            baseline = CoverageData(basename=str(destination))
            try:
                baseline.set_context("prior")
                baseline.add_arcs({str(source): [(-1, 1), (1, -1)]})
                baseline.write()
            finally:
                baseline.close(force=True)
        arguments = [
            "-q",
            "--cov=pkg",
            "--cov-context=test",
            f"--cov-report=json:{provider}.json",
            *(["--cov-append"] if append else []),
            "test_value.py",
        ]
        environment = {
            "COVERAGE_FILE": str(destination),
            "PYTHONPATH": str(project.root / "shadow"),
        }
        if provider == "reference" or route == "plugin":
            plugin = "pytest_cov.plugin" if provider == "reference" else "vfy.pytest_plugin"
            result = project.run_python("-m", "pytest", "-p", plugin, *arguments, env=environment)
        else:
            result = project.run_vfy(
                "test", f"--engine={route}", "--workers=1", *arguments, env=environment
            )
        result.expect(0)
        report = json.loads((project.root / f"{provider}.json").read_text())
        assert set(report["files"]) == {"shadow/__init__.py"}
        observations[provider] = _snapshot(project, destination.name)
    assert observations["actual"] == observations["reference"]
