from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, override
from xml.etree import ElementTree as ET

import coverage
import coverage.plugin as reference
import pytest

from tests.support.plugin_registration import PluginRegistration
from vfy import Coverage, plugin
from vfy.errors import NoDataError
from vfy.plugin import CoveragePlugin


@pytest.mark.parametrize(
    ("suffix", "include", "omit", "expected"),
    [
        ("", None, None, 50.0),
        ("", [], [], 50.0),
        ("", ["*.tpl"], ["*.py"], 50.0),
        ("", ["*.py"], None, "NoDataError: No data to report."),
        ("", None, ["*.tpl"], "NoDataError: No data to report."),
        (".py", None, None, 0.0),
        (".py", [], [], 0.0),
        (".py", ["*.py"], ["*.tpl"], 0.0),
        (".py", ["*.tpl"], None, "NoDataError: No data to report."),
        (".py", None, ["*.py"], "NoDataError: No data to report."),
    ],
)
@pytest.mark.parametrize("relative_files", [False, True])
def test_custom_reporter_filters_match_coverage_py_before_source_callbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    include: list[str] | None,
    omit: list[str] | None,
    expected: float | str,
    relative_files: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = tmp_path / ".coveragerc"
    config.write_text(f"[run]\nrelative_files = {relative_files}\n", encoding="utf-8")
    source = "source.tpl" if relative_files else str(tmp_path / "source.tpl")
    outcomes: list[float | str] = []
    source_calls: list[str] = []
    reporter_calls: list[str] = []

    class Reporter(plugin.FileReporter, reference.FileReporter):
        @override
        def lines(self) -> set[int]:
            source_calls.append("lines")
            return {1, 2}

        @override
        def source(self) -> str:
            source_calls.append("source")
            return "first\nsecond\n"

        @override
        def code_regions(self) -> tuple[()]:
            return ()

    class Plugin(CoveragePlugin):
        @override
        def file_reporter(self, filename: str) -> plugin.FileReporter:
            reporter_calls.append(filename)
            return Reporter(filename + suffix)

    registered = Plugin()

    def initialize(registration: PluginRegistration) -> None:
        registration.add_file_tracer(registered)

    for implementation in (coverage.Coverage, Coverage):
        source_calls.clear()
        reporter_calls.clear()
        reporter = implementation(data_file=None, config_file=str(config), plugins=[initialize])
        try:
            data = reporter.get_data()
            data.add_lines({source: [1]})
            data.add_file_tracers({source: registered._coverage_plugin_name})
            try:
                outcomes.append(reporter.report(file=StringIO(), include=include, omit=omit))
            except (coverage.exceptions.NoDataError, NoDataError) as error:
                outcomes.append(f"{type(error).__name__}: {error}")
            assert bool(source_calls) == isinstance(expected, float)
            assert reporter_calls == [source]
        finally:
            reporter.get_data().close(force=True)
    assert outcomes == [expected, expected]


@pytest.mark.parametrize(
    ("relative_files", "filter_name", "expected"),
    [
        (False, "disabled", 50.0),
        (True, "disabled", 50.0),
        (False, "include", 50.0),
        (True, "include", "NoDataError: No data to report."),
        (False, "omit", "NoDataError: No data to report."),
        (True, "omit", 50.0),
    ],
)
def test_python_sentinel_filters_use_ordinary_reporter_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_files: bool,
    filter_name: str,
    expected: float | str,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "source.py"
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    config = tmp_path / ".coveragerc"
    config.write_text(f"[run]\nrelative_files = {relative_files}\n", encoding="utf-8")
    stored_path = source.name if relative_files else str(source)

    class Plugin(CoveragePlugin):
        @override
        def file_reporter(self, filename: str) -> Literal["python"]:
            return "python"

    registered = Plugin()

    def initialize(registration: PluginRegistration) -> None:
        registration.add_file_tracer(registered)

    outcomes: list[float | str] = []
    for implementation in (coverage.Coverage, Coverage):
        reporter = implementation(data_file=None, config_file=str(config), plugins=[initialize])
        try:
            data = reporter.get_data()
            data.add_lines({stored_path: [1]})
            data.add_file_tracers({stored_path: registered._coverage_plugin_name})
            try:
                outcomes.append(
                    reporter.report(
                        file=StringIO(),
                        include=[str(source)] if filter_name == "include" else None,
                        omit=[str(source)] if filter_name == "omit" else None,
                    )
                )
            except (coverage.exceptions.NoDataError, NoDataError) as error:
                outcomes.append(f"{type(error).__name__}: {error}")
        finally:
            reporter.get_data().close(force=True)
    assert outcomes == [expected, expected]


@pytest.mark.parametrize("measured_reporter_key", [False, True])
@pytest.mark.parametrize("display_name", ["renamed.tpl", "presented.tpl", "presented\u0378.tpl"])
def test_custom_reporter_name_is_preserved_when_data_key_is_canonicalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    measured_reporter_key: bool,
    display_name: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = str(tmp_path / "source.tpl")
    reporter_key = str(tmp_path / "renamed.tpl")

    class Reporter(plugin.FileReporter, reference.FileReporter):
        @override
        def lines(self) -> set[int]:
            return {1}

        @override
        def source(self) -> str:
            return "first\n"

        @override
        def relative_filename(self) -> str:
            return display_name

        @override
        def code_regions(self) -> tuple[()]:
            return ()

    class Plugin(CoveragePlugin):
        @override
        def file_reporter(self, filename: str) -> plugin.FileReporter:
            return Reporter("renamed.tpl" if filename == source else "ignored.tpl")

    registered = Plugin()

    def initialize(registration: PluginRegistration) -> None:
        registration.add_file_tracer(registered)

    for implementation in (coverage.Coverage, Coverage):
        reporter = implementation(data_file=None, config_file=False, plugins=[initialize])
        try:
            data = reporter.get_data()
            data.add_lines({source: [1]})
            data.add_file_tracers({source: registered._coverage_plugin_name})
            if measured_reporter_key:
                data.add_lines({reporter_key: [1]})
                data.add_file_tracers({reporter_key: registered._coverage_plugin_name})
            output = StringIO()
            assert reporter.report(file=output, include=["renamed.tpl"]) == (
                100.0 if measured_reporter_key else 0.0
            )
            assert output.getvalue().splitlines()[2].split()[0] == display_name
            json_output = tmp_path / "report.json"
            reporter.json_report(outfile=str(json_output), include=["renamed.tpl"])
            assert list(json.loads(json_output.read_text(encoding="utf-8"))["files"]) == [
                display_name
            ]
            lcov_output = tmp_path / "report.lcov"
            reporter.lcov_report(outfile=str(lcov_output), include=["renamed.tpl"])
            lcov_name = "".join(char if char.isprintable() else "?" for char in display_name)
            assert f"SF:{lcov_name}\n" in lcov_output.read_text(encoding="utf-8")
            xml_output = tmp_path / "report.xml"
            reporter.xml_report(outfile=str(xml_output), include=["renamed.tpl"])
            xml_file = ET.parse(xml_output).find(".//class")
            assert xml_file is not None
            assert xml_file.get("filename") == display_name
            reporter.annotate(include=["renamed.tpl"])
            assert (tmp_path / "renamed.tpl,cover").exists()
            if display_name != "renamed.tpl":
                assert not (tmp_path / "presented.tpl,cover").exists()
        finally:
            reporter.get_data().close(force=True)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("file_tracer", ("module.py",)),
        ("dynamic_context", (SimpleNamespace(),)),
        ("find_executable_files", ("src",)),
        ("configure", ({},)),
        ("sys_info", ()),
    ],
)
def test_plugin_defaults_match_reference(method: str, arguments: tuple[object, ...]) -> None:
    assert getattr(plugin.CoveragePlugin(), method)(*arguments) == getattr(
        reference.CoveragePlugin(), method
    )(*arguments)


@pytest.mark.parametrize(
    ("class_name", "method", "arguments"),
    [
        ("CoveragePlugin", "file_reporter", ("module.py",)),
        ("FileTracer", "source_filename", ()),
        ("FileReporter", "lines", ()),
    ],
)
def test_required_plugin_methods_raise_reference_exception(
    class_name: str, method: str, arguments: tuple[object, ...]
) -> None:
    for module in (reference, plugin):
        constructor = getattr(module, class_name)
        instance = constructor("virtual.py") if class_name == "FileReporter" else constructor()
        with pytest.raises(NotImplementedError):
            getattr(instance, method)(*arguments)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("has_dynamic_source_filename", ()),
        ("dynamic_source_filename", ("module.py", SimpleNamespace())),
        ("line_number_range", (SimpleNamespace(f_lineno=27),)),
    ],
)
def test_file_tracer_defaults_match_reference(method: str, arguments: tuple[object, ...]) -> None:
    assert getattr(plugin.FileTracer(), method)(*arguments) == getattr(
        reference.FileTracer(), method
    )(*arguments)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("source", ()),
        ("relative_filename", ()),
        ("excluded_lines", ()),
        ("no_branch_lines", ()),
        ("exit_counts", ()),
        ("translate_lines", ([1, 3, 1],)),
        ("translate_arcs", ([(1, 3), (1, 3)],)),
        ("arc_description", (1, 3)),
        ("missing_arc_description", (1, 3)),
        ("code_region_kinds", ()),
        ("code_regions", ()),
    ],
)
def test_file_reporter_defaults_match_reference(
    tmp_path: Path, method: str, arguments: tuple[object, ...]
) -> None:
    source = tmp_path / "virtual.py"
    source.write_text("value = 1\n", encoding="utf-8")

    class Evidence:
        def lines(self) -> set[int]:
            return {1, 3}

        def arcs(self) -> set[tuple[int, int]]:
            return {(1, 3), (1, 4), (3, 4)}

    class Actual(Evidence, plugin.FileReporter):
        pass

    class Expected(Evidence, reference.FileReporter):
        pass

    assert getattr(Actual(str(source)), method)(*arguments) == getattr(
        Expected(str(source)), method
    )(*arguments)


def test_file_reporter_source_tokens_match_reference(tmp_path: Path) -> None:
    source = tmp_path / "virtual.py"
    source.write_text("first = 1\n\nsecond = 2\n", encoding="utf-8")
    assert list(plugin.FileReporter(str(source)).source_token_lines()) == list(
        reference.FileReporter(str(source)).source_token_lines()
    )


@pytest.mark.parametrize(
    ("observed", "percent", "branch_hits"),
    [([(1, 2), (2, -1)], 75.0, 1), ([(1, -1)], 50.0, 1), ([(1, 2), (1, -1), (2, -1)], 100.0, 2)],
)
def test_custom_reporter_exit_counts_match_coverage_py(
    tmp_path: Path, observed: list[tuple[int, int]], percent: float, branch_hits: int
) -> None:
    source = str(tmp_path / "source.tpl")

    class Reporter(plugin.FileReporter, reference.FileReporter):
        @override
        def lines(self) -> set[int]:
            return {1, 2}

        @override
        def source(self) -> str:
            return "first\nsecond\n"

        @override
        def arcs(self) -> set[tuple[int, int]]:
            return {(1, 2), (1, -1), (2, -1)}

        @override
        def exit_counts(self) -> dict[int, int]:
            return {1: 2, 2: 1}

        @override
        def code_regions(self) -> tuple[()]:
            return ()

    class Plugin(CoveragePlugin):
        @override
        def file_reporter(self, filename: str) -> plugin.FileReporter:
            return Reporter(filename)

    registered = Plugin()

    def initialize(registration: PluginRegistration) -> None:
        registration.add_file_tracer(registered)

    for name, implementation in [("reference", coverage.Coverage), ("vfy", Coverage)]:
        reporter = implementation(data_file=None, config_file=False, plugins=[initialize])
        try:
            data = reporter.get_data()
            data.add_arcs({source: observed})
            data.add_file_tracers({source: registered._coverage_plugin_name})
            assert reporter.report(file=StringIO()) == percent
            output = tmp_path / f"{name}.lcov"
            assert reporter.lcov_report(outfile=str(output)) == percent
            content = output.read_text(encoding="utf-8")
            assert "BRF:2\n" in content
            assert f"BRH:{branch_hits}\n" in content
        finally:
            reporter.get_data().close(force=True)
