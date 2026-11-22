from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from io import StringIO
from pathlib import Path
from typing import TypedDict, cast

import coverage
import pytest
from coverage import CoverageData

from tests.support import VfyProject
from tests.support.coverage_data import DataFileFactory
from tests.support.platforms import requires_native_pytest
from vfy import Coverage, CoverageData as VfyCoverageData


class FileReport(TypedDict):
    executed_lines: list[int]
    executed_branches: list[list[int]]
    missing_branches: list[list[int]]
    missing_lines: list[int]
    summary: dict[str, float | str]
    functions: object
    classes: object


class JsonReport(TypedDict):
    files: dict[str, FileReport]
    totals: dict[str, float | str]


def _normalized_xml(root: ET.Element) -> bytes:
    root.attrib.pop("version")
    root.attrib.pop("timestamp")
    for node in root.iter():
        node.text = (node.text or "").strip()
        node.tail = ""
        for name in ("line-rate", "branch-rate"):
            if name in node.attrib:
                node.attrib[name] = str(float(node.attrib[name]))
        attributes = sorted(node.attrib.items())
        node.attrib.clear()
        node.attrib.update(attributes)
    return ET.tostring(root)


def _collect_with_coverage_py(
    source: Path, data_file: Path, *, branch: bool = False
) -> coverage.Coverage:
    reference = coverage.Coverage(
        data_file=str(data_file),
        branch=branch,
        include=[str(source)],
        config_file=False,
    )
    reference.start()
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), {})
    reference.stop()
    reference.save()
    return reference


def test_json_report_preserves_filename_separators_from_coverage_py(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    # A backslash is a directory separator on Windows and a filename character on POSIX.
    source = project.write(r"src\sample.py", "answer = 42\n")
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    data.add_lines({str(source): {1}})
    data.write()
    project.run_coverage_py("json", "-o", "reference.json").expect(0)
    project.run_vfy("coverage", "json", "-o", "actual.json").expect(0)
    reference = json.loads((tmp_path / "reference.json").read_text())["files"]
    actual = json.loads((tmp_path / "actual.json").read_text())["files"]
    assert list(reference) == [str(source.relative_to(tmp_path))]
    assert actual == reference


@pytest.mark.parametrize(
    ("cases", "calls"),
    [
        ("        case 1:\n            return True\n", "f(1)\n"),
        (
            "        case 1:\n            return True\n        case 2:\n            return False\n",
            "f(1)\nf(2)\n",
        ),
        (
            "        case 1:\n            return True\n        case _:\n            return False\n",
            "f(1)\nf(2)\n",
        ),
        ("        case _ if x:\n            return True\n", "f(1)\nf(0)\n"),
        (
            "        case (\n            1 | 2\n        ):\n            return True\n"
            "        case _:\n            return False\n",
            "f(1)\nf(3)\n",
        ),
    ],
    ids=["single-case", "non-exhaustive", "wildcard", "guarded-wildcard", "multiline-pattern"],
)
def test_match_report_preserves_executed_headers_and_final_fallthrough(
    tmp_path: Path, cases: str, calls: str
) -> None:
    source = tmp_path / "matching.py"
    fallthrough = "" if "case _:\n" in cases else "    return None\n"
    source.write_text(f"def f(x):\n    match x:\n{cases}{fallthrough}{calls}", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    reference = _collect_with_coverage_py(source, data_file, branch=True)
    expected_json = tmp_path / "reference.json"
    reference.json_report(morfs=[str(source)], outfile=str(expected_json))
    expected = _only_file(json.loads(expected_json.read_text(encoding="utf-8")))
    actual = _only_file(_json(_load(data_file), tmp_path / "actual.json"))
    for field in (
        "executed_lines",
        "missing_lines",
        "executed_branches",
        "missing_branches",
        "summary",
    ):
        assert actual[field] == expected[field], field


def _only_file(payload: JsonReport) -> FileReport:
    assert len(payload["files"]) == 1
    return next(iter(payload["files"].values()))


def _load(data_file: Path, *, source: list[str] | None = None) -> Coverage:
    measured = Coverage(data_file=str(data_file), config_file=False, source=source)
    measured.load()
    return measured


def _json(
    measured: Coverage | coverage.Coverage,
    destination: Path,
    *,
    include: list[str] | None = None,
    omit: list[str] | None = None,
    contexts: list[str] | None = None,
) -> JsonReport:
    measured.json_report(outfile=str(destination), include=include, omit=omit, contexts=contexts)
    return cast("JsonReport", json.loads(destination.read_text(encoding="utf-8")))


def test_run_omit_applies_to_source_discovery_reports(tmp_path: Path) -> None:
    included = tmp_path / "included.py"
    omitted = tmp_path / "omitted.py"
    included.write_text("answer = 42\n", encoding="utf-8")
    omitted.write_text("missed = True\n", encoding="utf-8")
    measured = Coverage(
        data_file=None,
        source=[str(tmp_path)],
        omit=[str(omitted)],
        config_file=False,
    )
    measured.get_data().add_lines({str(included): {1}})
    output = tmp_path / "coverage.json"

    measured.json_report(outfile=str(output))

    files = json.loads(output.read_text(encoding="utf-8"))["files"]
    assert str(included) in files
    assert str(omitted) not in files


@requires_native_pytest
@pytest.mark.parametrize("engine", ["native", "pytest-native"])
def test_runner_and_saved_reports_keep_collection_file_inventory(
    tmp_path: Path, engine: str
) -> None:
    project = VfyProject(tmp_path)
    project.write("pkg/__init__.py", "")
    for name in ("used", "untested", "omitted"):
        project.write(f"pkg/{name}.py", "answer = 42\n")
    project.write(
        "test_pkg.py", "from pkg.used import answer\ndef test_answer(): assert answer == 42\n"
    )
    project.write(".coveragerc", "[run]\nsource = pkg\nomit = pkg/omitted.py\n")
    options = ("--cov=pkg", "--cov-config=.coveragerc")
    project.run_python(
        "-m",
        "pytest",
        "-p",
        "pytest_cov.plugin",
        *options,
        "--cov-report=json:oracle.json",
        env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    ).expect(0)
    project.run_vfy(
        "test",
        f"--engine={engine}",
        "--all",
        "-j",
        "1",
        *options,
        "--cov-report=json:run.json",
    ).expect(0)
    project.write("pkg/created_after_run.py", "later = True\n")
    project.run_vfy("coverage", "json", "-o", "saved.json").expect(0)

    def inventory(name: str) -> dict[str, FileReport]:
        report = cast("JsonReport", json.loads((tmp_path / name).read_text(encoding="utf-8")))
        return report["files"]

    expected = inventory("oracle.json")
    assert set(expected) == {"pkg/__init__.py", "pkg/used.py", "pkg/untested.py"}
    assert inventory("run.json") == expected
    assert inventory("saved.json") == expected


def test_relative_data_and_source_discovery_share_one_path_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    (source / "included.py").write_text("answer = 42\n", encoding="utf-8")
    (source / "omitted.py").write_text("missed = True\n", encoding="utf-8")
    measured = Coverage(data_file=None, source=["src"], omit=["src/omitted.py"], config_file=False)
    measured.get_data().add_lines({"src/included.py": {1}})

    files = _json(measured, tmp_path / "coverage.json")["files"]

    assert list(files) == ["src/included.py"]


def test_relative_omit_filters_absolute_source_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    included = source / "included.py"
    included.write_text("answer = 42\n", encoding="utf-8")
    (source / "omitted.py").write_text("missed = True\n", encoding="utf-8")
    reported = []
    for implementation in (coverage.Coverage, Coverage):
        measured = implementation(
            data_file=None, source=["src"], omit=["src/omitted.py"], config_file=False
        )
        measured.get_data().add_lines({str(included): {1}})
        destination = tmp_path / f"{implementation.__module__}.json"
        measured.json_report(outfile=str(destination))
        reported.append(list(json.loads(destination.read_text(encoding="utf-8"))["files"]))
        measured.get_data().close(force=True)
    assert reported[1] == reported[0] == ["src/included.py"]


def test_line_report_semantics_match_coverage_py_json_and_xml(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text(
        "value = 1\nif value:\n    value += 1\nelse:\n    value = 0\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    reference = _collect_with_coverage_py(source, data_file)
    measured = _load(data_file)

    terminal = StringIO()
    measured.report(file=terminal)
    assert "TOTAL" in terminal.getvalue()

    reference_json = tmp_path / "reference.json"
    reference.json_report(morfs=[str(source)], outfile=str(reference_json))
    expected = _only_file(json.loads(reference_json.read_text(encoding="utf-8")))
    actual = _only_file(_json(measured, tmp_path / "actual.json"))
    assert actual["executed_lines"] == expected["executed_lines"]
    assert actual["missing_lines"] == expected["missing_lines"]
    for key in ("covered_lines", "excluded_lines", "missing_lines", "num_statements"):
        assert actual["summary"][key] == expected["summary"][key]

    reference_xml = tmp_path / "reference.xml"
    reference.xml_report(morfs=[str(source)], outfile=str(reference_xml))
    expected_root = ET.parse(reference_xml).getroot()
    actual_xml = tmp_path / "actual.xml"
    measured.xml_report(outfile=str(actual_xml))
    actual_root = ET.parse(actual_xml).getroot()
    for key in ("lines-valid", "lines-covered"):
        assert actual_root.attrib[key] == expected_root.attrib[key]
    assert float(actual_root.attrib["line-rate"]) == float(expected_root.attrib["line-rate"])
    expected_lines = {
        int(line.attrib["number"]): int(line.attrib["hits"])
        for line in expected_root.findall(".//line")
    }
    actual_lines = {
        int(line.attrib["number"]): int(line.attrib["hits"])
        for line in actual_root.findall(".//line")
    }
    assert actual_lines == expected_lines


def test_json_report_skips_unparseable_measured_template_like_coverage_py(
    tmp_path: Path,
) -> None:
    source = tmp_path / "module.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    template = tmp_path / "action_menu.html"
    template.write_text("{% if action %}{{ action }}{% endif %}\n", encoding="utf-8")
    valid_non_python_path = tmp_path / "compiled.html"
    valid_non_python_path.write_text("answer = 42\n", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    reference = coverage.Coverage(data_file=str(data_file))
    reference.get_data().add_lines(
        {str(source): {1}, str(template): {1}, str(valid_non_python_path): {1}}
    )
    reference.get_data().write()
    reference_json = tmp_path / "reference.json"
    reference.json_report(outfile=str(reference_json))

    actual = Coverage(data_file=str(data_file))
    actual.load()
    actual_json = tmp_path / "actual.json"
    actual.json_report(outfile=str(actual_json))

    expected_files = json.loads(reference_json.read_text(encoding="utf-8"))["files"]
    observed_files = json.loads(actual_json.read_text(encoding="utf-8"))["files"]
    assert list(observed_files) == list(expected_files)
    assert {Path(filename).name for filename in observed_files} == {"module.py", "compiled.html"}
    for filename, expected in expected_files.items():
        assert observed_files[filename]["executed_lines"] == expected["executed_lines"]


@pytest.mark.parametrize("branch", [False, True])
@pytest.mark.parametrize("package_depth", [None, 1])
@pytest.mark.parametrize(
    "source_text",
    [
        "value = 1\n",
        "value = 1\nif value:\n    value += 1\n",
        "def choose(value):\n    if value:\n        return 1\n    return 2\n\nchoose(True)\n",
    ],
)
def test_xml_tree_matches_coverage_py(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    branch: bool,
    package_depth: int | None,
    source_text: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "package" / "nested" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(source_text, encoding="utf-8")
    data_file = tmp_path / ".coverage"
    reference = _collect_with_coverage_py(source, data_file, branch=branch)
    measured = _load(data_file)
    if package_depth is not None:
        reference.set_option("xml:package_depth", package_depth)
        measured.set_option("xml:package_depth", package_depth)
    reference_xml = tmp_path / "reference.xml"
    reference.xml_report(morfs=[str(source)], outfile=str(reference_xml))
    expected = ET.parse(reference_xml).getroot()
    actual_xml = tmp_path / "actual.xml"
    measured.xml_report(outfile=str(actual_xml))
    actual = ET.parse(actual_xml).getroot()

    assert _normalized_xml(actual) == _normalized_xml(expected)


@pytest.mark.parametrize(
    ("source_root", "relative_files"),
    [("src", False), ("./src", False), ("src", True), ("./src", True)],
    ids=["canonical-source", "dot-source", "relative-source", "dot-relative-source"],
)
def test_xml_reports_match_configured_source_root_and_package_depth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_root: str,
    relative_files: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src" / "package" / "nested" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    config = tmp_path / ".coveragerc"
    config.write_text(
        f"[run]\nsource = {source_root}\n"
        f"relative_files = {str(relative_files).lower()}\n\n[xml]\npackage_depth = 1\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    reference = coverage.Coverage(data_file=str(data_file), config_file=str(config))
    reference.start()
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), {})
    reference.stop()
    reference.save()

    expected_path = tmp_path / "reference.xml"
    reference.xml_report(outfile=str(expected_path))
    expected = ET.parse(expected_path).getroot()

    measured = Coverage(data_file=str(data_file), config_file=str(config))
    actual_path = tmp_path / "actual.xml"
    measured.xml_report(outfile=str(actual_path))
    actual = ET.parse(actual_path).getroot()

    expected_xml = _normalized_xml(expected)
    assert _normalized_xml(actual) == expected_xml
    cli_path = tmp_path / "cli.xml"
    VfyProject(tmp_path).run_vfy(
        "coverage", "xml", "--rcfile", str(config), "-o", str(cli_path)
    ).expect(0)
    assert _normalized_xml(ET.parse(cli_path).getroot()) == expected_xml


def test_xml_paths_match_canonical_source_root_for_relative_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "src" / "package" / "nested" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n", encoding="utf-8")
    collect_config = tmp_path / "collect.coveragerc"
    collect_config.write_text("[run]\nsource = src\nrelative_files = true\n", encoding="utf-8")
    report_config = tmp_path / "report.coveragerc"
    report_config.write_text(
        "[run]\nsource = src\nrelative_files = false\n\n[xml]\npackage_depth = 1\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    collecting = coverage.Coverage(data_file=str(data_file), config_file=str(collect_config))
    collecting.start()
    exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), {})
    collecting.stop()
    collecting.save()
    assert collecting.get_data().measured_files() == {"src/package/nested/example.py"}
    collecting.get_data().close()

    reference = coverage.Coverage(data_file=str(data_file), config_file=str(report_config))
    reference.load()
    expected_path = tmp_path / "reference.xml"
    reference.xml_report(outfile=str(expected_path))
    expected = ET.parse(expected_path).getroot()
    reference.get_data().close()

    measured = Coverage(data_file=str(data_file), config_file=str(report_config))
    actual_path = tmp_path / "actual.xml"
    measured.xml_report(outfile=str(actual_path))
    actual = ET.parse(actual_path).getroot()

    for selector, attributes in (
        ("./sources/source", ()),
        ("./packages/package", ("name",)),
        ("./packages/package/classes/class", ("name", "filename")),
    ):
        assert [
            ((node.text or "").strip(), tuple(node.attrib[name] for name in attributes))
            for node in actual.findall(selector)
        ] == [
            ((node.text or "").strip(), tuple(node.attrib[name] for name in attributes))
            for node in expected.findall(selector)
        ]
    assert _normalized_xml(actual) == _normalized_xml(expected)


def test_xml_report_sorts_sources_packages_and_classes_like_coverage_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source_roots = [tmp_path / "unicode10-source", tmp_path / "unicode4-source"]
    package10 = source_roots[1] / "package10"
    package4 = source_roots[0] / "package4"
    package10.mkdir(parents=True)
    package4.mkdir(parents=True)
    sources = [
        package10 / "unicode10-0-0.py",
        package10 / "unicode4-1-0.py",
        package10 / "zero1.py",
        package10 / "zero01.py",
        package10 / "large1000000000000000000000000000.py",
        package10 / "large999999999999999999999999999.py",
        package4 / "module.py",
    ]
    for source in sources:
        source.write_text("value = 1\n", encoding="utf-8")
    config = tmp_path / ".coveragerc"
    config.write_text(
        "[run]\nsource = unicode10-source, unicode4-source\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    reference = coverage.Coverage(data_file=str(data_file), config_file=str(config))
    reference.start()
    for source in sources:
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), {})
    reference.stop()
    reference.save()

    expected_path = tmp_path / "reference.xml"
    reference.xml_report(outfile=str(expected_path))
    expected = ET.parse(expected_path).getroot()
    reference.get_data().close()

    measured = Coverage(data_file=str(data_file), config_file=str(config))
    actual_path = tmp_path / "actual.xml"
    measured.xml_report(outfile=str(actual_path))
    actual = ET.parse(actual_path).getroot()

    assert _normalized_xml(actual) == _normalized_xml(expected)
    assert [Path(source.text or "").name for source in actual.findall("./sources/source")] == [
        "unicode4-source",
        "unicode10-source",
    ]
    assert [package.attrib["name"] for package in actual.findall("./packages/package")] == [
        "package4",
        "package10",
    ]
    assert [
        Path(class_elt.attrib["filename"]).name
        for class_elt in actual.findall("./packages/package[@name='package10']/classes/class")
    ] == [
        "large999999999999999999999999999.py",
        "large1000000000000000000000000000.py",
        "unicode4-1-0.py",
        "unicode10-0-0.py",
        "zero01.py",
        "zero1.py",
    ]


def test_branch_report_totals_match_coverage_py(tmp_path: Path) -> None:
    source = tmp_path / "branchy.py"
    source.write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 0\n\nchoose(True)\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    reference = _collect_with_coverage_py(source, data_file, branch=True)
    measured = _load(data_file)

    reference_json = tmp_path / "reference.json"
    reference.json_report(morfs=[str(source)], outfile=str(reference_json))
    expected = _only_file(json.loads(reference_json.read_text(encoding="utf-8")))["summary"]
    actual = _only_file(_json(measured, tmp_path / "actual.json"))["summary"]
    for key in ("num_branches", "covered_branches", "num_partial_branches"):
        assert actual[key] == expected[key], key
    terminal = StringIO()
    measured.report(file=terminal)
    assert "Branch" in terminal.getvalue()
    assert "BrPart" in terminal.getvalue()


def test_aggregate_report_counts_match_coverage_py_7_15_4(tmp_path: Path) -> None:
    sources = [tmp_path / "first.py", tmp_path / "second.py"]
    for index, source in enumerate(sources):
        source.write_text(
            "def choose(value):\n"
            "    if value:\n"
            "        return 1\n"
            "    return 0\n"
            "\n"
            f"result = choose({index == 0})\n"
            "excluded = 1  # pragma: no cover\n",
            encoding="utf-8",
        )
    empty = tmp_path / "empty.py"
    empty.write_text("", encoding="utf-8")
    data_file = tmp_path / ".coverage"
    reference = coverage.Coverage(
        data_file=str(data_file), branch=True, source=[str(tmp_path)], config_file=False
    )
    reference.start()
    for source in sources:
        exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), {})
    reference.stop()
    reference.save()

    reference_json = tmp_path / "reference-totals.json"
    reference.json_report(
        morfs=[*(str(source) for source in sources), str(empty)], outfile=str(reference_json)
    )
    expected = json.loads(reference_json.read_text(encoding="utf-8"))["totals"]
    reference.get_data().close(force=True)
    measured = _load(data_file, source=[str(tmp_path)])
    report = _json(measured, tmp_path / "actual-totals.json")
    actual = report["totals"]

    assert len(report["files"]) == 3
    assert expected["excluded_lines"] == 2
    assert expected["num_partial_branches"] == 2
    assert actual == expected


def test_json_function_and_class_regions_match_coverage_py(tmp_path: Path) -> None:
    source = tmp_path / "regions.py"
    source.write_text(
        "value = 1\n\n"
        "def outer(flag):\n"
        "    local = 2\n"
        "    def inner():\n"
        "        return local\n"
        "    if flag:\n"
        "        return inner()\n"
        "    return 0\n\n"
        "class Thing:\n"
        "    label = 'thing'\n"
        "    def method(self):\n"
        "        return self.label\n\n"
        "outer(True)\n",
        encoding="utf-8",
    )
    data_file = tmp_path / ".coverage"
    reference = _collect_with_coverage_py(source, data_file, branch=True)

    reference_json = tmp_path / "reference.json"
    reference.json_report(morfs=[str(source)], outfile=str(reference_json))
    expected = _only_file(json.loads(reference_json.read_text(encoding="utf-8")))
    actual = _only_file(_json(_load(data_file), tmp_path / "actual.json"))

    assert actual["functions"] == expected["functions"]
    assert actual["classes"] == expected["classes"]


def _merged_contexts(
    implementation: DataFileFactory, directory: Path
) -> tuple[list[int] | None, dict[int, list[str]]]:
    first = implementation(str(directory / "first"))
    first.set_context("one")
    first.add_lines({"example.py": {1, 2}})
    second = implementation(str(directory / "second"))
    second.set_context("two")
    second.add_lines({"example.py": {2, 3}})
    first.update(second)
    observed = (
        first.lines("example.py"),
        {
            line: sorted(contexts)
            for line, contexts in first.contexts_by_lineno("example.py").items()
        },
    )
    first.close(force=True)
    second.close(force=True)
    return observed


def test_merge_unions_lines_and_contexts(tmp_path: Path) -> None:
    for name, implementation in (("reference", CoverageData), ("actual", VfyCoverageData)):
        directory = tmp_path / name
        directory.mkdir()
        assert _merged_contexts(implementation, directory) == (
            [1, 2, 3],
            {1: ["one"], 2: ["one", "two"], 3: ["two"]},
        )


@pytest.mark.parametrize("directory", [None, "annotations"])
def test_public_annotations_keep_same_basename_sources_like_coverage_py(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    sources: dict[str, set[int]] = {}
    for package, text in [("a", "x = 1\n"), ("b", "y = 2\n")]:
        source = tmp_path / package / "__init__.py"
        source.parent.mkdir()
        source.write_text(text, encoding="utf-8")
        sources[str(source)] = {1}

    outputs: list[dict[str, str]] = []
    with ExitStack() as stack:
        for name, implementation in [("reference", coverage.Coverage), ("actual", Coverage)]:
            reporter = implementation(data_file=None, config_file=False)
            if name == "reference":
                stack.callback(reporter.get_data().close, force=True)
            reporter.get_data().add_lines(sources)
            destination = str(tmp_path / name / directory) if directory is not None else None
            reporter.annotate(morfs=list(sources), directory=destination)
            output_paths = (
                Path(destination).glob("*,cover")
                if destination is not None
                else tmp_path.glob("*/__init__.py,cover")
            )
            outputs.append(
                {
                    str(path.relative_to(destination or tmp_path)): path.read_text(encoding="utf-8")
                    for path in output_paths
                }
            )
    assert len(outputs[0]) == len(outputs[1]) == 2
    assert set(outputs[0]) == set(outputs[1])
    assert set(outputs[1].values()) == {"> x = 1\n", "> y = 2\n"}
    assert outputs[1] == outputs[0]


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain control characters")
@pytest.mark.parametrize(
    "filename",
    [
        "injected\nDA:999,1\n.py",
        "controls\r\t\x85\u00a0\u00ad\u200b\u2028\u2029\ue000 é\u0301😀.py",
        pytest.param(
            "unassigned\u0378.py",
            marks=pytest.mark.skipif(
                sys.platform == "darwin", reason="APFS rejects unassigned U+0378"
            ),
        ),
        "unicode-versions\u31ef\U00016100\u088f.py",
    ],
    ids=["record-injection", "unicode-printability", "unassigned-character", "unicode-versions"],
)
def test_public_lcov_filename_stays_one_record_like_coverage_py(
    tmp_path: Path, filename: str
) -> None:
    source = tmp_path / filename
    source.write_text("x = 1\n", encoding="utf-8")
    reports: list[list[str]] = []
    with ExitStack() as stack:
        for name, implementation in [("reference", coverage.Coverage), ("actual", Coverage)]:
            reporter = implementation(data_file=None, config_file=False)
            if name == "reference":
                stack.callback(reporter.get_data().close, force=True)
            reporter.get_data().add_lines({str(source): {1}})
            destination = tmp_path / f"{name}.lcov"
            reporter.lcov_report(morfs=[str(source)], outfile=str(destination))
            reports.append(destination.read_text(encoding="utf-8").splitlines())
    expected_source = "SF:" + "".join(ch if ch.isprintable() else "?" for ch in str(source))
    for records in reports:
        assert [line for line in records if line.startswith("SF:")] == [expected_source]
        assert [line for line in records if line.startswith("DA:")] == ["DA:1,1"]
        assert records.count("end_of_record") == 1
    assert reports[1] == ["TN:", *reports[0]]


def test_public_lcov_branch_hits_distinguish_partial_and_unexecuted_origins(
    tmp_path: Path,
) -> None:
    source = tmp_path / "branch.py"
    source.write_text(
        "def f(flag):\n    if flag:\n        return 1\n    return 2\n"
        "def g(flag):\n    if flag:\n        return 3\n    return 4\nf(True)\n",
        encoding="utf-8",
    )
    reference = _collect_with_coverage_py(source, tmp_path / ".coverage", branch=True)
    with ExitStack() as stack:
        stack.callback(reference.get_data().close, force=True)
        actual = Coverage(data_file=None, config_file=False, branch=True)
        arcs = reference.get_data().arcs(str(source))
        assert arcs is not None
        actual.get_data().add_arcs({str(source): arcs})
        reports: list[list[str]] = []
        for name, reporter in [("reference", reference), ("actual", actual)]:
            destination = tmp_path / f"{name}.lcov"
            reporter.lcov_report(morfs=[str(source)], outfile=str(destination))
            reports.append(destination.read_text(encoding="utf-8").splitlines())
    assert [line.rsplit(",", 1)[1] for line in reports[0] if line.startswith("BRDA:")] == [
        "1",
        "0",
        "-",
        "-",
    ]
    assert reports[1] == ["TN:", *reports[0]]


@pytest.mark.parametrize(
    "source_text",
    [
        "condition = False\nif condition:\n    x = 1\n",
        "def f(flag):\n    if flag:\n        return 1\n    return 2\nf(True)\n",
        "class C:\n    condition = False\n    if condition:\n        x = 1\n",
        "@staticmethod\ndef f(flag):\n    if flag:\n        return 1\n    return 2\nf(True)\n",
        "def outer():\n    def café(flag):\n        if flag:\n"
        "            return 1\n        return 2\n    return café(True)\nouter()\n",
    ],
    ids=[
        "module-exit",
        "function-exit",
        "class-exit",
        "decorated-function-exit",
        "nested-unicode-function",
    ],
)
def test_public_lcov_exit_descriptions_match_coverage_py(tmp_path: Path, source_text: str) -> None:
    source = tmp_path / "exits.py"
    source.write_text(source_text, encoding="utf-8")
    reference = _collect_with_coverage_py(source, tmp_path / ".coverage", branch=True)
    with ExitStack() as stack:
        stack.callback(reference.get_data().close, force=True)
        actual = Coverage(data_file=None, config_file=False, branch=True)
        arcs = reference.get_data().arcs(str(source))
        assert arcs is not None
        actual.get_data().add_arcs({str(source): arcs})
        reports = []
        for name, reporter in [("reference", reference), ("actual", actual)]:
            destination = tmp_path / f"{name}.lcov"
            reporter.lcov_report(morfs=[str(source)], outfile=str(destination))
            reports.append(destination.read_text(encoding="utf-8").splitlines())
    assert reports[1] == ["TN:", *reports[0]]


def test_lcov_and_annotation_semantics_match_coverage_py(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source_text = "x = 1\ny = 2  # pragma: no cover\nz = 3\n"
    source.write_text(source_text, encoding="utf-8")
    data_file = tmp_path / ".coverage"
    reference_data = CoverageData(str(data_file))
    reference_data.add_lines({str(source): {1}})
    reference_data.write()
    reference_data.close(force=True)
    reference = coverage.Coverage(data_file=str(data_file), config_file=False)
    reference.load()
    measured = _load(data_file)

    prefixes = ("DA:", "LF:", "LH:", "BRDA:", "BRF:", "BRH:")
    records = []
    for name, reporter in (("reference", reference), ("actual", measured)):
        destination = tmp_path / f"{name}.lcov"
        reporter.lcov_report(morfs=[str(source)], outfile=str(destination))
        records.append(
            {
                line
                for line in destination.read_text(encoding="utf-8").splitlines()
                if line.startswith(prefixes)
            }
        )
    assert records[1] == records[0]

    annotations = []
    for name, reporter in (("reference", reference), ("actual", measured)):
        directory = tmp_path / f"{name}-annotation"
        reporter.annotate(morfs=[str(source)], directory=str(directory))
        annotations.append(next(directory.glob("*,cover")).read_text(encoding="utf-8"))
    assert annotations[1] == annotations[0]
    reference.get_data().close(force=True)


def test_html_report_contains_navigation_and_line_states(tmp_path: Path) -> None:
    source = tmp_path / "example.py"
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    measured = Coverage(data_file=None, config_file=False)
    measured.get_data().add_lines({str(source): {1}})

    html = tmp_path / "htmlcov"
    measured.html_report(directory=str(html), title="Project coverage")
    index = (html / "index.html").read_text(encoding="utf-8")
    assert "Project coverage" in index
    assert "50%" in index
    detail = next(html.glob("z_*.html")).read_text(encoding="utf-8")
    assert 'class="covered"' in detail
    assert 'class="missing"' in detail


def test_partial_branch_rules_match_coverage_py(tmp_path: Path) -> None:
    source = tmp_path / "partial.py"
    source.write_text(
        "value = 1\nif value:  # project: partial\n    value += 1\nelse:\n    value -= 1\n",
        encoding="utf-8",
    )
    summaries = []
    for name, implementation in (("reference", coverage.Coverage), ("actual", Coverage)):
        reporter = implementation(data_file=None, config_file=False, branch=True)
        reporter.set_option("report:partial_branches", [r"# project: partial"])
        reporter.get_data().add_arcs({str(source): {(-1, 1), (1, 2), (2, 3), (3, -1)}})
        destination = tmp_path / f"{name}.json"
        reporter.json_report(morfs=[str(source)], outfile=str(destination))
        summaries.append(_only_file(json.loads(destination.read_text(encoding="utf-8")))["summary"])
        if name == "reference":
            reporter.get_data().close(force=True)

    assert summaries[1] == summaries[0]
    assert summaries[1]["num_branches"] == 2
    assert summaries[1]["num_partial_branches"] == 0


@pytest.mark.parametrize("sort", ["cover", "-cover", "name", "stmts"])
def test_report_selection_handles_empty_covered_and_sorting(tmp_path: Path, sort: str) -> None:
    covered = tmp_path / "covered.py"
    missing = tmp_path / "missing.py"
    empty = tmp_path / "empty.py"
    covered.write_text("covered = 1\n", encoding="utf-8")
    missing.write_text("missing = 1\n", encoding="utf-8")
    empty.write_text("# comment\n", encoding="utf-8")
    for skip in (False, True):
        outputs = []
        for implementation in (coverage.Coverage, Coverage):
            reporter = implementation(data_file=None, config_file=False)
            reporter.set_option("report:sort", sort)
            reporter.get_data().add_lines({str(covered): {1}})
            reporter.get_data().touch_file(str(missing))
            reporter.get_data().touch_file(str(empty))
            output = StringIO()
            reporter.report(file=output, skip_covered=skip, skip_empty=skip)
            outputs.append(output.getvalue())
            if implementation is coverage.Coverage:
                reporter.get_data().close(force=True)
        assert outputs[1] == outputs[0]


def test_report_contexts_select_measured_lines(tmp_path: Path) -> None:
    source = tmp_path / "native.py"
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    for implementation in (coverage.Coverage, Coverage):
        reporter = implementation(data_file=None, config_file=False)
        data = reporter.get_data()
        data.set_context("test_one")
        data.add_lines({str(source): {1}})
        data.set_context("test_two")
        data.add_lines({str(source): {2}})
        report = _json(
            reporter, tmp_path / f"{implementation.__module__}.json", contexts=["test_two"]
        )
        assert _only_file(report)["executed_lines"] == [2]
        data.close(force=True)


@pytest.mark.parametrize(
    ("include", "omit"),
    [([], ["*omitted.py"]), (["*included.py"], [])],
    ids=["omit", "include"],
)
def test_report_file_selection_precedes_context_matching(
    tmp_path: Path, include: list[str], omit: list[str]
) -> None:
    omitted = tmp_path / "omitted.py"
    included = tmp_path / "included.py"
    omitted.write_text("value = 1\n", encoding="utf-8")
    included.write_text("value = 1\n", encoding="utf-8")
    reporter = Coverage(data_file=None, config_file=False)
    data = reporter.get_data()
    data.set_context(f"{'a' * 32}c")
    data.add_lines({str(omitted): {1}})
    data.set_context("selected")
    data.add_lines({str(included): {1}})

    files = _json(
        reporter,
        tmp_path / "report.json",
        include=include or None,
        omit=omit or None,
        contexts=[r"^(a|aa)+\1$|^selected$"],
    )["files"]

    assert {name: report["executed_lines"] for name, report in files.items()} == {
        str(included): [1]
    }


def test_report_context_runtime_regex_failure_is_a_value_error(tmp_path: Path) -> None:
    source = tmp_path / "context.py"
    source.write_text("value = 1\n", encoding="utf-8")
    reporter = Coverage(data_file=None, config_file=False)
    reporter.get_data().set_context(f"{'a' * 32}c")
    reporter.get_data().add_lines({str(source): {1}})

    with pytest.raises(ValueError, match=r"context pattern.*backtrack"):
        _json(reporter, tmp_path / "report.json", contexts=[r"^(a|aa)+\1$"])


@pytest.mark.parametrize("with_exclusion_failure", [False, True])
def test_report_context_errors_name_the_failing_file(
    tmp_path: Path, with_exclusion_failure: bool
) -> None:
    source = tmp_path / "context.py"
    source.write_text(f"# {'a' * 32}c\nvalue = 1\n", encoding="utf-8")
    reporter = Coverage(data_file=None, config_file=False)
    if with_exclusion_failure:
        reporter.set_option("report:exclude_lines", [r"^# (a|aa)+\1$"])
    reporter.get_data().set_context(f"{'a' * 32}c")
    reporter.get_data().add_lines({str(source): {2}})

    with pytest.raises(ValueError, match="context pattern") as raised:
        _json(reporter, tmp_path / "report.json", contexts=[r"^(a|aa)+\1$"])

    assert str(source) in str(raised.value)
    assert "failed to analyze" not in str(raised.value)


@pytest.mark.parametrize("with_exclusion_failure", [False, True])
def test_multifile_report_errors_use_one_evidence_order(
    tmp_path: Path, with_exclusion_failure: bool
) -> None:
    first = tmp_path / "a.py"
    last = tmp_path / "z.py"
    first.write_text(f"# {'a' * 32}c\nvalue = 1\n", encoding="utf-8")
    last.write_text("value = 1\n", encoding="utf-8")
    reporter = Coverage(data_file=None, config_file=False)
    if with_exclusion_failure:
        reporter.set_option("report:exclude_lines", [r"^# (a|aa)+\1$"])
    data = reporter.get_data()
    data.set_context(f"{'a' * 32}c")
    data.add_lines({str(first): {2}})
    data.set_context(f"{'a' * 33}c")
    data.add_lines({str(last): {1}})

    with pytest.raises(ValueError, match="context pattern") as raised:
        _json(reporter, tmp_path / "report.json", contexts=[r"^(a|aa)+\1$"])

    assert str(last) in str(raised.value)
    assert str(first) not in str(raised.value)
    assert "failed to analyze" not in str(raised.value)


def test_multifile_report_error_order_ignores_line_and_arc_storage(tmp_path: Path) -> None:
    first = tmp_path / "a.py"
    last = tmp_path / "z.py"
    first.write_text("value = 1\n", encoding="utf-8")
    last.write_text("value = 1\n", encoding="utf-8")
    reporter = Coverage(data_file=None, config_file=False, branch=True)
    data = reporter.get_data()
    data.set_context(f"{'a' * 32}c")
    data.add_arcs({str(first): {(-1, 1), (1, -1)}})
    data.set_context(f"{'a' * 33}c")
    data.add_arcs({str(last): {(1, -1)}})

    with pytest.raises(ValueError, match="context pattern") as raised:
        _json(reporter, tmp_path / "report.json", contexts=[r"^(a|aa)+\1$"])

    assert str(last) in str(raised.value)
    assert str(first) not in str(raised.value)


@pytest.mark.parametrize("precision", [0, 1])
@pytest.mark.parametrize("near_complete", [False, True], ids=["near-zero", "near-complete"])
def test_report_percentage_display_preserves_true_endpoints(
    tmp_path: Path, precision: int, near_complete: bool
) -> None:
    with ExitStack() as resources:
        source = tmp_path / "rounding.py"
        statement_count = 400 * 10**precision
        source.write_text("value = 1\n" * statement_count, encoding="utf-8")
        executed_count = statement_count - 1 if near_complete else 1
        results = []
        for implementation in (Coverage, coverage.Coverage):
            reporter = implementation(data_file=None, config_file=False)
            if isinstance(reporter, coverage.Coverage):
                resources.callback(reporter.get_data().close, force=True)
            reporter.set_option("report:precision", precision)
            reporter.get_data().add_lines({str(source): set(range(1, executed_count + 1))})
            directory = tmp_path / implementation.__module__
            percentage = reporter.html_report(directory=str(directory))
            html_values = set(
                re.findall(r"\b\d+(?:\.\d+)?%", (directory / "index.html").read_text())
            )
            json_path = directory / "result.json"
            reporter.json_report(outfile=str(json_path))
            summary = json.loads(json_path.read_text())["totals"]
            text_values = []
            for output_format in ("text", "markdown", "total"):
                output = StringIO()
                reporter.report(file=output, output_format=output_format)
                text_values.append(
                    output.getvalue().strip()
                    if output_format == "total"
                    else set(re.findall(r"\b\d+(?:\.\d+)?%", output.getvalue()))
                )
            results.append(
                (percentage, html_values, summary["percent_covered_display"], text_values)
            )
        assert results[0] == results[1]
        assert 0.0 < float(results[0][2]) < 100.0


def test_html_extra_css_preserves_declared_encoding_bytes(tmp_path: Path) -> None:
    with ExitStack() as resources:
        source = tmp_path / "subject.py"
        source.write_text("value = 1\n", encoding="utf-8")
        stylesheet = tmp_path / "custom.css"
        content = b'@charset "ISO-8859-1";\n/* caf\xe9 */\nbody { color: red; }\n'
        stylesheet.write_bytes(content)
        for implementation in (Coverage, coverage.Coverage):
            reporter = implementation(data_file=None, config_file=False)
            if isinstance(reporter, coverage.Coverage):
                resources.callback(reporter.get_data().close, force=True)
            reporter.get_data().add_lines({str(source): {1}})
            directory = tmp_path / implementation.__module__
            reporter.html_report(directory=str(directory), extra_css=str(stylesheet))
            assert content in [path.read_bytes() for path in directory.glob("*.css")]


_TERMINAL_SOURCES = {
    "pkg/__init__.py": "",
    "pkg/item2.py": "VALUE = 2\n",
    "pkg/item10.py": "VALUE = 10\n",
    "pkg/lines.py": (
        "def gap(x):\n"
        "    if x:\n"
        "        return 1\n"
        "    # comment\n"
        "    y = 2\n"
        "\n"
        "    return y\n"
        "\n"
        "\n"
        "def loop(values):\n"
        "    for value in values:\n"
        "        if value:\n"
        "            return value\n"
        "    return None\n"
        "\n"
        "\n"
        "def tail(flag):\n"
        "    if flag:\n"
        "        print('flag')\n"
    ),
    "pkg/nested/__init__.py": "",
    "pkg/nested/complete.py": "VALUE = 1\n",
    "empty.py": "# no statements\n",
    "tests/test_lines.py": (
        "import empty\n"
        "from pkg.lines import gap, loop, tail\n"
        "from pkg.nested import complete\n"
        "\n"
        "def test_lines():\n"
        "    assert gap(1) == 1\n"
        "    assert loop([1]) == 1\n"
        "    tail(True)\n"
        "    assert complete.VALUE == 1\n"
    ),
}


@pytest.mark.parametrize("branch", [True, False], ids=["branch", "line"])
@pytest.mark.parametrize(
    "options",
    [
        (),
        ("--show-missing",),
        ("--sort=stmts",),
        ("--show-missing", "--skip-covered"),
        ("--skip-empty",),
        ("--show-missing", "--skip-covered", "--skip-empty", "--precision=2"),
        ("--format=markdown",),
        ("--format=markdown", "--show-missing", "--sort=-cover"),
        ("--format=markdown", "--skip-covered", "--skip-empty", "--precision=2"),
        ("--format=markdown", "--show-missing", "--skip-empty", "--sort=miss"),
    ],
    ids=[
        "default",
        "missing",
        "sort-stmts",
        "skip-covered",
        "skip-empty",
        "combined",
        "markdown",
        "markdown-missing",
        "markdown-skipped",
        "markdown-missing-empty",
    ],
)
def test_terminal_report_matches_coverage_py_text(
    tmp_path: Path, options: tuple[str, ...], *, branch: bool
) -> None:
    project = VfyProject(tmp_path)
    for name, source in _TERMINAL_SOURCES.items():
        project.write(name, source)
    collected = project.run_coverage_py(
        "run",
        *(["--branch"] if branch else []),
        "--source=.",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "tests",
    )
    collected.expect(0)

    reference = project.run_coverage_py("report", *options)
    actual = project.run_vfy("coverage", "report", *options)

    reference.expect(0)
    actual.expect(0)
    assert actual.stdout == reference.stdout


def test_unknown_report_sort_column_is_a_configuration_error(tmp_path: Path) -> None:
    project = VfyProject(tmp_path)
    for name, source in _TERMINAL_SOURCES.items():
        project.write(name, source)
    project.run_coverage_py(
        "run", "--source=.", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"
    ).expect(0)

    reference = project.run_coverage_py("report", "--sort=bogus")
    actual = project.run_vfy("coverage", "report", "--sort=bogus")

    assert reference.returncode == actual.returncode == 1
    assert actual.stdout == ""
    assert 'invalid report sort "bogus"' in actual.stderr
    assert "name, stmts, miss, branch, brpart, or cover" in actual.stderr
