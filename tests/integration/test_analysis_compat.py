from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NotRequired, TypedDict

import coverage
import pytest
from coverage import Coverage as CoveragePy
from coverage.python import get_python_source

from tests.support.reference import reference_coverage
from vfy import Coverage
from vfy.errors import NotPython


class AnalysisExpectation(TypedDict):
    executable: list[int]
    excluded: list[int]
    possible_arcs: list[tuple[int, int]]
    no_branch: list[int]
    excludes: NotRequired[list[str]]
    collect_arcs: NotRequired[bool]
    source_missing: NotRequired[bool]
    regions: NotRequired[list[dict[str, object]]]
    before_314: NotRequired[AnalysisExpectation]


ROOT = Path(__file__).parents[2]
ANALYSIS_FIXTURES = [
    *sorted(
        path
        for package in ("", "independent", "run")
        for path in (ROOT / "python" / "vfy" / package).glob("*.py")
    ),
    ROOT / "tests/fixtures/semantic_sample.py",
    ROOT / "tests/fixtures/branch_semantics.py",
]
REVIEWED_ANALYSIS_FIXTURES = sorted((ROOT / "tests" / "fixtures" / "analysis").glob("*.md"))


def _assert_statements_match(
    source: Path, *, exclude: list[str] | None = None, config_file: bool = True
) -> None:
    """Compare statements and exclusions reported by Coverage.py and vfy for one file."""

    results = []
    for tool in (CoveragePy, Coverage):
        measured = tool(data_file=None, config_file=config_file)
        try:
            for pattern in exclude or []:
                measured.exclude(pattern)
            _, statements, excluded, _, _ = measured.analysis2(str(source))
            results.append((sorted(statements), sorted(excluded)))
        finally:
            measured.get_data().close(force=True)
    assert results[1] == results[0]


def _branch_record(
    tool: type[CoveragePy | Coverage],
    source: Path,
    output: Path,
    *,
    config_file: bool = True,
    exclude_lines: list[str] | None = None,
    partials: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, object]:
    """Report one never-executed file so every statement and branch arc is missing."""

    measured = tool(data_file=None, branch=True, config_file=config_file)
    try:
        if exclude_lines is not None:
            measured.set_option("report:exclude_lines", exclude_lines)
        if partials is not None:
            measured.set_option("report:partial_branches", partials)
        for pattern in exclude or []:
            measured.exclude(pattern)
        measured.get_data().add_arcs({str(source): set()})
        measured.json_report(morfs=[str(source)], outfile=str(output))
    finally:
        measured.get_data().close(force=True)
    files = json.loads(output.read_text(encoding="utf-8"))["files"]
    assert len(files) == 1
    record = next(iter(files.values()))
    return {
        field: record[field]
        for field in ("missing_lines", "excluded_lines", "missing_branches", "summary")
    }


def _assert_branch_records_match(
    source: Path,
    tmp_path: Path,
    *,
    config_file: bool = True,
    exclude_lines: list[str] | None = None,
    partials: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict[str, object]:
    records = [
        _branch_record(
            tool,
            source,
            tmp_path / f"{name}.json",
            config_file=config_file,
            exclude_lines=exclude_lines,
            partials=partials,
            exclude=exclude,
        )
        for name, tool in (("reference", CoveragePy), ("vfy", Coverage))
    ]
    reference, actual = records
    assert actual == reference
    return actual


@pytest.mark.parametrize(
    "condition", ["(False)", "(\n    False\n)", "0x0", "not True", "(False or None)"]
)
def test_constant_conditions_do_not_create_impossible_obligations(
    tmp_path: Path, condition: str
) -> None:
    source = tmp_path / "constant.py"
    source.write_text(f"if {condition}:\n    dead = 1\nlive = 2\n", encoding="utf-8")
    record = _assert_branch_records_match(source, tmp_path, config_file=False)
    assert record["missing_branches"] == []


@pytest.mark.parametrize("condition", ["1", "(True)", "('yes')", "b'yes'", "(1 and True)"])
def test_truthy_constants_remove_unreachable_alternate_suites(
    tmp_path: Path, condition: str
) -> None:
    source = tmp_path / "constant_true.py"
    source.write_text(
        f"if {condition}:\n    live = 1\nelse:\n    if unknown:\n        dead = 2\ntail = 3\n",
        encoding="utf-8",
    )
    record = _assert_branch_records_match(source, tmp_path, config_file=False)
    assert record["missing_branches"] == []


@pytest.mark.parametrize("suite", ["if True:", "for value in [1]:", "try:"])
def test_suite_string_expression_is_not_a_docstring(tmp_path: Path, suite: str) -> None:
    source = tmp_path / "strings.py"
    text = f'{suite}\n    "a string expression"\n    value = 1\n'
    if suite == "try:":
        text += "finally:\n    value = 2\n"
    source.write_text(text, encoding="utf-8")
    _assert_statements_match(source, config_file=False)


@pytest.mark.parametrize(
    "contents",
    [
        b'# coding: latin-1\nvalue = "caf\xe9\x80"\n',
        b'#!/usr/bin/python\n# coding: cp1252\nvalue = "\x80"\n',
        b'\xef\xbb\xbfvalue = "hello"\n',
        b'# coding: latin-1\r\nvalue = "caf\xe9"\r\n',
        b'#!/usr/bin/python\r# coding: latin-1\rvalue = "caf\xe9"\r',
        b"# coding: utf-8\r\n# invalid: \xff\r\nvalue = 1\r",
    ],
    ids=["latin1", "second-line-cp1252", "utf8-bom", "crlf", "cr", "lossy-utf8-crlf"],
)
def test_python_source_encoding_matches_coverage_py(tmp_path: Path, contents: bytes) -> None:
    source = tmp_path / "encoded.py"
    source.write_bytes(contents)
    with reference_coverage(config_file=False) as reference:
        expected = reference.analysis2(str(source))
    assert Coverage(config_file=False).analysis2(str(source)) == expected


@pytest.mark.parametrize("encoding", ["cp1252", "windows-1252", "latin-1"])
def test_source_rendering_preserves_every_high_byte(tmp_path: Path, encoding: str) -> None:

    source = tmp_path / "encoded.py"
    source.write_bytes(
        f"# coding: {encoding}\n\nvalue = '".encode() + bytes(range(128, 256)) + b"'\n"
    )
    cov = Coverage(data_file=None, config_file=False)
    cov.get_data().add_lines({str(source): [3]})
    cov.annotate(morfs=[str(source)])
    annotation = source.with_name(source.name + ",cover").read_text(encoding="utf-8")
    rendered = "\n".join(line[2:] for line in annotation.split("\n"))
    assert rendered == get_python_source(str(source))


@pytest.mark.parametrize("encoding", ["shift_jis", "iso8859-9", "utf-16"])
def test_unsupported_source_codec_is_rejected_before_report_output(
    tmp_path: Path, encoding: str
) -> None:
    source = tmp_path / "unsupported.py"
    source.write_text(f"# coding: {encoding}\n\nvalue = 1\n", encoding="utf-8")
    cov = Coverage(data_file=None, config_file=False)
    cov.get_data().add_lines({str(source): [3]})
    destination = tmp_path / "report.json"
    destination.write_bytes(b"existing report")
    with pytest.raises(SyntaxError, match="unsupported Python source encoding"):
        cov.json_report(outfile=str(destination))
    assert destination.read_bytes() == b"existing report"


@pytest.mark.parametrize(
    "contents",
    [
        b"# coding: no-such-codec\nx = 1\n",
        b"\xef\xbb\xbf# coding: latin-1\nx = 1\n",
        b"# invalid \xff\nx = 1\n",
        b"# first line\n# invalid \xff\nx = 1\n",
    ],
)
def test_invalid_encoding_declaration_preserves_public_exception(
    tmp_path: Path, contents: bytes
) -> None:
    source = tmp_path / "invalid_encoding.py"
    source.write_bytes(contents)
    with reference_coverage(config_file=False) as reference:
        for instance in (reference, Coverage(data_file=None, config_file=False)):
            with pytest.raises(SyntaxError):
                instance.analysis2(str(source))


def _markdown_block(markdown: str, language: str) -> str:
    marker = f"```{language}\n"
    _, body = markdown.replace("\r\n", "\n").split(marker, 1)
    return body.split("\n```")[0]


def _reviewed_expectation(expected: AnalysisExpectation) -> AnalysisExpectation:
    if sys.version_info >= (3, 14):
        return expected
    selected: AnalysisExpectation = {**expected}
    if "before_314" in expected:
        selected.update(expected["before_314"])
    return selected


@pytest.mark.parametrize(
    "path",
    ANALYSIS_FIXTURES,
)
def test_statement_and_exclusion_analysis_matches_coverage_py(path: Path) -> None:
    _assert_statements_match(path)


def test_custom_exclusion_regex_matches_coverage_py(tmp_path: Path) -> None:
    source = tmp_path / "excluded.py"
    source.write_text("first = 1\nsecond = 2  # project: ignore\nthird = 3\n", encoding="utf-8")
    pattern = r"#\s*project:\s*ignore"
    _assert_statements_match(source, exclude=[pattern], config_file=True)


@pytest.mark.parametrize("long_first", [False, True])
def test_literal_exclusion_unions_match_coverage_py_analysis_and_reports(
    tmp_path: Path, long_first: bool
) -> None:
    source = tmp_path / "literal_rules.py"
    source.write_text(
        "first = 1  # begin\n"
        "middle = 2\n"
        "last = 3  # end\n"
        "label = 'café'\n"
        "def choose(value):\n"
        "    if value:  # project: partial\n"
        "        return 1\n"
        "    return 2\n",
        encoding="utf-8",
    )
    excludes = ["begin", "begin\nmiddle = 2\nlast = 3  # end"]
    if long_first:
        excludes.reverse()
    excludes.append("café")
    partials = ["project: partial"]
    record = _assert_branch_records_match(
        source, tmp_path, config_file=False, exclude_lines=excludes, partials=partials
    )
    assert record["excluded_lines"]


def test_default_exclusions_cover_typed_protocol_and_overload_bodies(tmp_path: Path) -> None:
    source = tmp_path / "contracts.py"
    source.write_text(
        "from typing import Protocol, overload\n\n"
        "class Reader(Protocol):\n"
        "    def read(self, size: int = ...) -> bytes: ...\n\n"
        "@overload\n"
        "def convert(\n    value: str,\n) -> bytes: ...\n\n"
        "@overload\n"
        "def convert(value: bytes) -> str: ...\n\n"
        "def convert(value):\n    return value\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=False)


def test_bare_annotations_follow_their_execution_scope(tmp_path: Path) -> None:
    source = tmp_path / "annotations.py"
    source.write_text(
        "module_value: int\n\n"
        "class Model:\n"
        "    field: int\n\n"
        "def function():\n"
        "    local: int\n"
        "    function.attribute: int\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_multiline_if_exclusion_preserves_else_suite(tmp_path: Path) -> None:
    source = tmp_path / "multiline.py"
    source.write_text(
        "flag = True\n"
        "if (\n"
        "    flag\n"
        "):  # project: ignore\n\n"
        "    excluded = 1\n"
        "else:\n"
        "    included = 2\n",
        encoding="utf-8",
    )
    pattern = r"# project: ignore"
    _assert_statements_match(source, exclude=[pattern], config_file=True)


def test_decorated_one_line_ellipsis_exclusions_match_coverage_py(tmp_path: Path) -> None:
    source = tmp_path / "overloads.py"
    source.write_text(
        "from typing import Protocol, overload\n\n"
        "class Reader(Protocol):\n"
        "    def read(self, size: int = ...) -> bytes: ...\n\n"
        "# runtime implementations follow\n"
        "@overload\n"
        "def convert(value: str) -> bytes: ...\n"
        "@overload\n"
        "def convert(value: bytes) -> str: ...\n\n"
        "def convert(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_excluded_alternate_clause_matches_coverage_py(tmp_path: Path) -> None:
    source = tmp_path / "conditional.py"
    source.write_text(
        "import sys\n\n"
        "if sys.version_info >= (3, 13):  # pragma: no cover\n"
        "    from inspect import iscoroutinefunction\n"
        "else:  # pragma: no cover\n"
        "    from asyncio import iscoroutinefunction\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_excluded_try_suite_includes_else_header(tmp_path: Path) -> None:
    source = tmp_path / "try_else.py"
    source.write_text(
        "if enabled:  # pragma: no cover\n"
        "    try:\n"
        "        value = load()\n"
        "    except LookupError:\n"
        "        value = None\n"
        "    else:\n"
        "        use(value)\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_try_pragma_preserves_except_handler(tmp_path: Path) -> None:
    source = tmp_path / "try_except.py"
    source.write_text(
        "try:  # pragma: no cover\n    risky()\nexcept RuntimeError:\n    recover()\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_placeholder_class_does_not_exclude_surrounding_blank_lines(tmp_path: Path) -> None:
    source = tmp_path / "type_checking.py"
    source.write_text(
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:  # pragma: no cover\n\n"
        "    class Placeholder: ...\n\n\n"
        "live = True\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_multiline_overload_excludes_adjacent_blank_lines(tmp_path: Path) -> None:
    source = tmp_path / "overloads.py"
    source.write_text(
        "from typing import overload\n\n"
        "@overload\n"
        "def convert(\n"
        "    value: str,\n"
        ") -> bytes: ...\n\n\n"
        "def convert(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_try_normal_completion_enters_finally_suite(tmp_path: Path) -> None:
    source = tmp_path / "finally_branch.py"
    source.write_text(
        "def close(enabled):\n"
        "    try:\n"
        "        if enabled:\n"
        "            save()\n"
        "    finally:\n"
        "        cleanup()\n",
        encoding="utf-8",
    )
    filename = str(source.resolve())
    _assert_branch_records_match(Path(filename), tmp_path)


def test_for_else_pragma_excludes_the_alternate_suite(tmp_path: Path) -> None:
    source = tmp_path / "for_else.py"
    source.write_text(
        "for item in items:\n"
        "    if ready(item):\n"
        "        break\n"
        "else:  # pragma: nocover\n"
        "    raise AssertionError('unreachable')\n",
        encoding="utf-8",
    )
    _assert_statements_match(source, config_file=True)


def test_branch_destination_skips_function_local_annotation(tmp_path: Path) -> None:
    source = tmp_path / "annotation_branch.py"
    source.write_text(
        "def normalize(value):\n"
        "    if isinstance(value, str):\n"
        "        return value\n"
        "    else:\n"
        "        normalized: object\n"
        "        if value is None:\n"
        "            return ''\n"
        "        return value\n",
        encoding="utf-8",
    )
    filename = str(source.resolve())
    _assert_branch_records_match(Path(filename), tmp_path)


@pytest.mark.parametrize("path", ANALYSIS_FIXTURES)
def test_static_branch_arcs_match_coverage_py(path: Path, tmp_path: Path) -> None:
    filename = str(path.resolve())
    _assert_branch_records_match(Path(filename), tmp_path)


@pytest.mark.parametrize("path", REVIEWED_ANALYSIS_FIXTURES)
def test_reviewed_analysis_fixtures_match_coverage_py_and_vfy(path: Path, tmp_path: Path) -> None:
    markdown = path.read_text(encoding="utf-8")
    source = _markdown_block(markdown, "python")
    expected = _reviewed_expectation(json.loads(_markdown_block(markdown, "json")))
    filename = tmp_path / path.with_suffix(".py").name
    filename.write_text(source, encoding="utf-8")

    if expected.get("source_missing", False):
        for tool, error in ((CoveragePy, coverage.exceptions.NotPython), (Coverage, NotPython)):
            measured = tool(data_file=None, config_file=True)
            try:
                with pytest.raises(error):
                    measured.analysis2(str(filename))
            finally:
                measured.get_data().close(force=True)
        assert expected["executable"] == expected["excluded"] == []
        assert expected["possible_arcs"] == expected["no_branch"] == []
        return

    with reference_coverage(branch=True) as reference:
        defaults = reference.get_exclude_list()
        for pattern in expected.get("excludes", []):
            reference.exclude(pattern)
        reference.get_data().add_arcs({str(filename): set()})
        _, statements, excluded, _, _ = reference.analysis2(str(filename))
        reference_analysis = reference._analyze(str(filename))
        branch_origins = set(reference_analysis.branch_stats())
        possible_arcs = {
            arc for arc in reference_analysis.arc_possibilities if arc[0] in branch_origins
        }
        assert set(expected["executable"]) == set(statements), path.name
        assert set(expected["excluded"]) == set(excluded), path.name
        assert set(map(tuple, expected["possible_arcs"])) == (
            possible_arcs if expected.get("collect_arcs", True) else set()
        ), path.name
        assert set(expected["no_branch"]) == set(reference_analysis.no_branch), path.name
        regions = [
            {
                "kind": region.kind,
                "name": region.name,
                "start": region.start,
                "lines": sorted(region.lines),
            }
            for region in reference._get_file_reporter(str(filename)).code_regions()
        ]
        assert expected.get("regions", []) == regions, path.name

    # vfy must report the reviewed statements and exclusions, and match Coverage.py's branches.
    exclude_lines = [*defaults, *expected.get("excludes", [])]
    vfy_record = _branch_record(
        Coverage, filename, tmp_path / "vfy.json", exclude_lines=exclude_lines
    )
    reference_record = _branch_record(
        CoveragePy, filename, tmp_path / "reference.json", exclude_lines=exclude_lines
    )
    assert vfy_record == reference_record, path.name
    assert vfy_record["missing_lines"] == sorted(expected["executable"]), path.name
    assert vfy_record["excluded_lines"] == sorted(expected["excluded"]), path.name
