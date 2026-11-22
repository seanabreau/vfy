"""Report mixed absolute and relative schema-7 filenames like Coverage.py."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType

import coverage
import pytest

from tests.support.plugin_registration import PluginRegistration
from tests.support.vfy_project import VfyProject
from vfy import Coverage
from vfy.errors import NoDataError
from vfy.plugin import CoveragePlugin


@pytest.mark.parametrize("relative_files", [False, True])
@pytest.mark.parametrize("explicit", [False, True], ids=["measured-files", "explicit-aliases"])
def test_report_alias_paths_lookup_and_totals_match_coverage_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_files: bool, explicit: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "sample.py"
    source.write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
    config = tmp_path / ".coveragerc"
    config.write_text(f"[run]\nrelative_files = {str(relative_files).lower()}\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(data_file))
    try:
        data.add_lines({str(source): [1], "sample.py": [2, 3]})
        data.write()
    finally:
        data.close(force=True)
    before = hashlib.sha256(data_file.read_bytes()).digest()
    reports = []
    for implementation in (coverage.Coverage, Coverage):
        reporter = implementation(data_file=str(data_file), config_file=str(config))
        try:
            reporter.load()
            assert reporter.get_data().measured_files() == {str(source), "sample.py"}
            output = tmp_path / f"{implementation.__module__}.json"
            reporter.json_report(
                morfs=[str(source), "sample.py"] if explicit else None, outfile=str(output)
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            reports.append({key: report[key] for key in ("files", "totals")})
        finally:
            reporter.get_data().close(force=True)
    assert reports[1] == reports[0]
    assert set(reports[0]["files"]) == {"sample.py"}
    assert hashlib.sha256(data_file.read_bytes()).digest() == before


@pytest.mark.parametrize("relative_files", [False, True])
def test_json_command_filenames_and_totals_match_coverage_py(
    tmp_path: Path, relative_files: bool
) -> None:
    project = VfyProject(tmp_path)
    source = project.write("src/sample.py", "x = 1\ny = 2\nz = 3\n")
    project.write(".coveragerc", f"[run]\nrelative_files = {str(relative_files).lower()}\n")
    data = coverage.CoverageData(basename=str(tmp_path / ".coverage"))
    try:
        data.add_lines({str(source): [1], "src/sample.py": [2]})
        data.write()
    finally:
        data.close(force=True)
    reports = []
    for command in (("-m", "coverage", "json"), ("-m", "vfy", "coverage", "json")):
        completed = project.run_python(*command, "-o", "-")
        completed.expect(0)
        report = json.loads(completed.stdout)
        reports.append({key: report[key] for key in ("files", "totals")})
    assert reports[1] == reports[0]
    assert set(reports[0]["files"]) == {"src/sample.py"}


@pytest.mark.parametrize("relative_files", [False, True])
@pytest.mark.parametrize("sharded", [False, True], ids=["saved-data", "worker-shard"])
def test_json_command_remaps_path_aliases_like_coverage_py(
    tmp_path: Path, relative_files: bool, sharded: bool
) -> None:
    project = VfyProject(tmp_path)
    project.write("src/sample.py", "x = 1\ny = 2\nz = 3\n")
    project.write(
        ".coveragerc",
        f"[run]\nrelative_files = {str(relative_files).lower()}\n"
        "[paths]\nsource =\n    src\n    /remote/build/src\n",
    )
    reports = []
    for engine in ("coverage", "vfy"):
        data_file = f".coverage-{engine}"
        data = coverage.CoverageData(
            basename=str(tmp_path / (data_file + (".worker" if sharded else "")))
        )
        try:
            data.add_lines({"/remote/build/src/sample.py": [2]})
            data.write()
        finally:
            data.close(force=True)
        before = None if sharded else hashlib.sha256((tmp_path / data_file).read_bytes()).digest()
        if engine == "coverage" and sharded:
            combined = project.run_python("-m", "coverage", "combine", "--data-file", data_file)
            combined.expect(0)
        command = ("coverage", "json") if engine == "vfy" else ("json",)
        completed = project.run_python("-m", engine, *command, "--data-file", data_file, "-o", "-")
        completed.expect(0)
        report = json.loads(completed.stdout)
        reports.append({key: report[key] for key in ("files", "totals")})
        if before is not None:
            assert hashlib.sha256((tmp_path / data_file).read_bytes()).digest() == before
    assert reports[1] == reports[0]
    assert reports[0]["files"]["src/sample.py"]["executed_lines"] == [2]


@pytest.mark.parametrize("relative_files", [False, True])
@pytest.mark.parametrize("module_like", [False, True], ids=["filename", "module-like"])
def test_analysis_alias_data_lookup_matches_coverage_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_files: bool, module_like: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "branch.py"
    source.write_text("if flag:\n    x = 1\nelse:\n    x = 2\n", encoding="utf-8")
    config = tmp_path / ".coveragerc"
    config.write_text(f"[run]\nrelative_files = {str(relative_files).lower()}\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(data_file))
    try:
        data.add_arcs(
            {
                str(source): [(-1, 1), (1, 2), (2, -1), (1, 4), (4, -1)],
                "branch.py": [(-1, 1), (1, 4), (4, -1)],
            }
        )
        data.write()
    finally:
        data.close(force=True)
    module = ModuleType("branch")
    module.__file__ = str(source)
    morf = module if module_like else str(source)
    observations = []
    for implementation in (coverage.Coverage, Coverage):
        reporter = implementation(data_file=str(data_file), config_file=str(config))
        try:
            reporter.load()
            observations.append(
                (reporter.analysis2(morf), reporter.analysis(morf), reporter.branch_stats(morf))
            )
        finally:
            reporter.get_data().close(force=True)
    assert observations[1] == observations[0]


@pytest.mark.parametrize("with_configurer", [False, True])
def test_repeated_morfs_preserve_selected_contexts_in_materialized_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_configurer: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "sample.py"
    source.write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")

    def initialize(plugins: PluginRegistration) -> None:
        plugins.add_configurer(CoveragePlugin())

    reports = []
    for implementation in (coverage.Coverage, Coverage):
        reporter = implementation(
            data_file=None, config_file=False, plugins=[initialize] if with_configurer else []
        )
        data = reporter.get_data()
        try:
            data.set_context("selected")
            data.add_lines({str(source): [1]})
            data.set_context("other")
            data.add_lines({str(source): [2, 3]})
            output = tmp_path / f"{implementation.__module__}.json"
            reporter.json_report(
                morfs=[str(source), str(source)],
                contexts=["selected"],
                show_contexts=True,
                outfile=str(output),
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            reports.append({key: report[key] for key in ("files", "totals")})
        finally:
            data.close(force=True)
    assert reports[1] == reports[0]
    assert reports[0]["totals"]["covered_lines"] == 2
    assert reports[0]["totals"]["num_statements"] == 6
    assert reports[0]["files"]["sample.py"]["contexts"] == {"1": ["selected"]}


@pytest.mark.parametrize("filter_kind", ["include", "omit"])
def test_relative_morf_absolute_filters_use_the_reporter_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filter_kind: str
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "sample.py"
    source.write_text("x = 1\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    data = coverage.CoverageData(basename=str(data_file))
    try:
        data.add_lines({str(source): [1]})
        data.write()
    finally:
        data.close(force=True)
    observations = []
    for implementation in (coverage.Coverage, Coverage):
        reporter = implementation(data_file=str(data_file), config_file=False)
        output = tmp_path / f"{implementation.__module__}.json"
        try:
            reporter.load()
            if filter_kind == "include":
                reporter.json_report(
                    morfs=["sample.py"], include=[str(source)], outfile=str(output)
                )
                report = json.loads(output.read_text(encoding="utf-8"))
                observations.append({key: report[key] for key in ("files", "totals")})
            else:
                error_type = (
                    coverage.exceptions.NoDataError
                    if implementation is coverage.Coverage
                    else NoDataError
                )
                with pytest.raises(error_type, match="No data to report") as rejected:
                    reporter.json_report(
                        morfs=["sample.py"], omit=[str(source)], outfile=str(output)
                    )
                observations.append(str(rejected.value))
        finally:
            reporter.get_data().close(force=True)
    assert observations[1] == observations[0]
