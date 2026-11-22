"""Keep adoption comparison strict outside reviewed volatile spans."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from validation.pytest_runner.adoption_observations import (
    ATTRS_REVISION,
    CLICK_REVISION,
    DATEUTIL_REVISION,
    DOTENV_REVISION,
    RICH_REVISION,
    SORTEDCONTAINERS_REVISION,
    normalize_adoption_observations,
)

DATEUTIL_DIAGNOSTICS = (
    (
        "tests/test_isoparser.py::test_isoparser_byte_sep",
        "self = <dateutil.parser.isoparser.isoparser object at 0x7fed54741f40>\n",
    ),
    (
        "tests/test_parser.py::TestParseUnimplementedCases::test_somewhat_ambiguous_string",
        "self = <tests.test_parser.TestParseUnimplementedCases object at 0x7fed54741f40>\n",
    ),
    (
        "tests/test_parser.py::TestParseUnimplementedCases::test_four_letter_day",
        "self = <tests.test_parser.TestParseUnimplementedCases object at 0x7fed54741f40>\n"
        "self = <dateutil.parser._parser.parser object at 0x7fed54741f40>\n",
    ),
    (
        "tests/test_parser.py::TestParseUnimplementedCases::test_first_century",
        "self = <dateutil.parser._parser.parser object at 0x7fed54741f40>\n"
        "self = <tests.test_parser.TestParseUnimplementedCases object at 0x7fed54741f40>\n"
        "self = <dateutil.parser._parser.parser object at 0x7fed54741f40>\n",
    ),
    (
        "tests/test_tz.py::test_gettz_zone_wrong_type[no startswith()]",
        "badzone = <object object at 0x7fed54741f40>, exc_reason = None\n"
        "name = <object object at 0x7fed54741f40>\n",
    ),
)

OUTPUT_PAIRS = (
    (
        "test_console.py::test_brokenpipeerror",
        "console output\nrendered in 39.9ms (cold cache)\nrendered in 16.0ms (warm cache)\n",
        "console output\nrendered in 41.8ms (cold cache)\nrendered in 18.5ms (warm cache)\n",
        "console output",
    ),
    (
        "test_inspect.py::test_inspect_swig_edge_case",
        "│ │ <tests.test_inspect.test_inspect_swig_edge_case.<locals>.Thing object at │ │\n"
        "│ │ 0x7fbc15a25b50>                                                          │ │\n",
        "│ │ <tests.test_inspect.test_inspect_swig_edge_case.<locals>.Thing object at │ │\n"
        "│ │ 0x7f2f55dc87a0>                                                          │ │\n",
        "Thing",
    ),
    (
        "test_inspect.py::test_qualname_in_slots",
        "╭── <functools._lru_cache_wrapper object at 0x7fbc1581ddd0> ───╮\n"
        "│ 36 attribute(s) not shown. Run inspect(inspect) for options. │\n",
        "╭── <functools._lru_cache_wrapper object at 0x7f2f5555d590> ───╮\n"
        "│ 36 attribute(s) not shown. Run inspect(inspect) for options. │\n",
        "36 attribute(s)",
    ),
    (
        "test_logging.py::test_exception",
        "[09/30/26 23:54:03] ERROR    FORMATTER foo 2026-09-30 23:54:03,461  test_logging.py:49\n"
        "                    ERROR    FORMATTER message 2026-09-30 23:54:03,462"
        "  test_logging.py:53\n"
        "ZeroDivisionError: division by zero\n",
        "[09/30/26 23:54:09] ERROR    FORMATTER foo 2026-09-30 23:54:09,735  test_logging.py:49\n"
        "                    ERROR    FORMATTER message 2026-09-30 23:54:09,736"
        "  test_logging.py:53\n"
        "ZeroDivisionError: division by zero\n",
        "division by zero",
    ),
    (
        "test_logging.py::test_exception_with_extra_lines",
        "[09/30/26 23:54:03] ERROR    message  test_logging.py:84\n"
        "ZeroDivisionError: division by zero\n",
        "[09/30/26 23:54:09] ERROR    message  test_logging.py:84\n"
        "ZeroDivisionError: division by zero\n",
        "division by zero",
    ),
)


def observed_case(nodeid: str, stdout: str) -> list[dict[str, Any]]:
    return [
        {
            "nodeid": nodeid,
            "reports": [
                {
                    "phase": "call",
                    "outcome": "passed",
                    "exception": None,
                    "diagnostic": None,
                    "stdout": stdout,
                    "stderr": "literal stderr",
                    "log": "literal log",
                }
            ],
            "warnings": [],
        }
    ]


SORTEDLIST_NODEID = "tests/test_coverage_sortedlist.py::test_check"
SORTEDLIST_REWRITTEN = (
    "Traceback (most recent call last):\n"
    '  File "<UPSTREAM>/src/sortedcontainers/sortedlist.py", line 1524, in _check\n'
    "    assert self._len == sum(len(sublist) for sublist in self._lists)\n"
    "AssertionError: assert 5 == 10\n"
    " +  where 5 = SortedList([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])._len\n"
    " +  and   10 = sum(<generator object SortedList._check.<locals>.<genexpr> at "
    "0x7f6f6aee82b0>)\n"
    "len 5\nload 4\noffset 0\nlen_index 0\nindex []\nlen_maxes 3\n"
    "maxes [3, 7, 9]\nlen_lists 3\nlists [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]\n"
)
SORTEDLIST_PLAIN = SORTEDLIST_REWRITTEN.replace(
    "AssertionError: assert 5 == 10\n"
    " +  where 5 = SortedList([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])._len\n"
    " +  and   10 = sum(<generator object SortedList._check.<locals>.<genexpr> at "
    "0x7f6f6aee82b0>)\n",
    "           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\nAssertionError\n",
)


@pytest.mark.parametrize("rewritten", [False, True])
def test_sortedlist_caught_traceback_normalizes_only_generator_address(rewritten: bool) -> None:
    output = SORTEDLIST_REWRITTEN if rewritten else SORTEDLIST_PLAIN
    stock = observed_case(SORTEDLIST_NODEID, output)
    stock[0]["reports"].insert(0, {**stock[0]["reports"][0], "phase": "setup"})
    stock[0]["reports"][1].update(stderr=output, log=output, diagnostic=output)
    native = copy.deepcopy(stock)
    native[0]["reports"][1]["stdout"] = output.replace("0x7f6f6aee82b0", "0x7f26b2446400")
    raw = copy.deepcopy((stock, native))

    normalized_stock, stock_rules = normalize_adoption_observations(
        stock, project="sortedcontainers", revision=SORTEDCONTAINERS_REVISION
    )
    normalized_native, native_rules = normalize_adoption_observations(
        native, project="sortedcontainers", revision=SORTEDCONTAINERS_REVISION
    )

    assert normalized_stock == normalized_native
    assert (stock, native) == raw
    assert stock_rules == native_rules
    assert [(rule["phase"], rule["field"], rule["occurrences"]) for rule in stock_rules] == [
        ("call", "stdout", int(rewritten))
    ]
    report = normalized_stock[0]["reports"][1]
    assert report["stderr"] == report["log"] == report["diagnostic"] == output
    assert normalized_stock[0]["reports"][0] == stock[0]["reports"][0]


@pytest.mark.parametrize("output", [SORTEDLIST_REWRITTEN, SORTEDLIST_PLAIN])
@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("line 1524", "line 1525"),
        ("sortedlist.py", "another.py"),
        ("assert self._len", "assert self._size"),
        ("len 5\n", "len 6\n"),
        ("maxes [3, 7, 9]", "maxes [3, 7, 10]"),
        ("AssertionError", "TypeError"),
    ],
)
def test_sortedlist_caught_traceback_semantic_mutations_fail_closed(
    output: str, before: str, after: str
) -> None:
    altered = observed_case(SORTEDLIST_NODEID, output.replace(before, after))
    with pytest.raises(ValueError, match="expected 1 stdout spans"):
        normalize_adoption_observations(
            altered, project="sortedcontainers", revision=SORTEDCONTAINERS_REVISION
        )


@pytest.mark.parametrize(
    "output",
    [
        "",
        SORTEDLIST_REWRITTEN * 2,
        SORTEDLIST_REWRITTEN.replace("SortedList._check", "SortedKeyList._check"),
        SORTEDLIST_REWRITTEN.replace("<genexpr>", "other_generator"),
        SORTEDLIST_REWRITTEN.replace("0x7f6f6aee82b0", "not-an-address"),
        SORTEDLIST_REWRITTEN.replace("assert 5 == 10", "assert 6 == 10"),
        SORTEDLIST_REWRITTEN + "unrelated object at 0x7f6f6aee82b0\n",
        SORTEDLIST_PLAIN + "unrelated object at 0x7f6f6aee82b0\n",
    ],
)
def test_sortedlist_changed_generator_or_span_counts_fail_closed(output: str) -> None:
    with pytest.raises(ValueError, match="expected 1 stdout spans"):
        normalize_adoption_observations(
            observed_case(SORTEDLIST_NODEID, output),
            project="sortedcontainers",
            revision=SORTEDCONTAINERS_REVISION,
        )


@pytest.mark.parametrize(
    ("project", "revision", "nodeid"),
    [
        ("attrs", SORTEDCONTAINERS_REVISION, SORTEDLIST_NODEID),
        ("sortedcontainers", "different-revision", SORTEDLIST_NODEID),
        ("sortedcontainers", SORTEDCONTAINERS_REVISION, "tests/test_other.py::test_check"),
    ],
)
def test_sortedlist_rule_requires_pinned_project_revision_and_case(
    project: str, revision: str, nodeid: str
) -> None:
    cases = observed_case(nodeid, SORTEDLIST_REWRITTEN)
    normalized, applied = normalize_adoption_observations(cases, project=project, revision=revision)
    assert normalized == cases
    assert applied == []


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_sortedlist_address_rule_requires_call_stdout(phase: str) -> None:
    cases = observed_case(SORTEDLIST_NODEID, SORTEDLIST_REWRITTEN)
    cases[0]["reports"][0]["phase"] = phase
    with pytest.raises(ValueError, match="requires one call report"):
        normalize_adoption_observations(
            cases, project="sortedcontainers", revision=SORTEDCONTAINERS_REVISION
        )


@pytest.mark.parametrize("field", ["stderr", "log", "diagnostic"])
def test_sortedlist_other_address_fields_remain_literal(field: str) -> None:
    stock = observed_case(SORTEDLIST_NODEID, SORTEDLIST_REWRITTEN)
    stock[0]["reports"][0][field] = SORTEDLIST_REWRITTEN
    native = copy.deepcopy(stock)
    native[0]["reports"][0][field] = SORTEDLIST_REWRITTEN.replace(
        "0x7f6f6aee82b0", "0x7f26b2446400"
    )
    normalized_stock, _ = normalize_adoption_observations(
        stock, project="sortedcontainers", revision=SORTEDCONTAINERS_REVISION
    )
    normalized_native, _ = normalize_adoption_observations(
        native, project="sortedcontainers", revision=SORTEDCONTAINERS_REVISION
    )
    assert normalized_stock != normalized_native


@pytest.mark.parametrize(("nodeid", "stock_stdout", "native_stdout", "semantic_text"), OUTPUT_PAIRS)
def test_reviewed_rich_spans_preserve_raw_and_semantic_output(
    nodeid: str, stock_stdout: str, native_stdout: str, semantic_text: str
) -> None:
    stock = observed_case(nodeid, stock_stdout)
    native = observed_case(nodeid, native_stdout)
    raw_stock = copy.deepcopy(stock)
    raw_native = copy.deepcopy(native)

    normalized_stock, stock_rules = normalize_adoption_observations(
        stock, project="rich", revision=RICH_REVISION
    )
    normalized_native, native_rules = normalize_adoption_observations(
        native, project="rich", revision=RICH_REVISION
    )

    assert normalized_stock == normalized_native
    assert stock == raw_stock
    assert native == raw_native
    assert stock_rules == native_rules
    assert stock_rules
    assert semantic_text in normalized_stock[0]["reports"][0]["stdout"]

    altered = observed_case(nodeid, native_stdout.replace(semantic_text, "changed semantics"))
    # A context change may invalidate the reviewed span entirely; either a
    # rejected shape or unequal literal output must fail the gate.
    try:
        normalized_altered, _ = normalize_adoption_observations(
            altered, project="rich", revision=RICH_REVISION
        )
    except ValueError:
        pass
    else:
        assert normalized_altered != normalized_stock


@pytest.mark.parametrize(("nodeid", "stock_stdout", "native_stdout", "semantic_text"), OUTPUT_PAIRS)
@pytest.mark.parametrize("copies", [0, 2])
def test_changed_rich_span_counts_fail_closed(
    nodeid: str, stock_stdout: str, native_stdout: str, semantic_text: str, copies: int
) -> None:
    cases = observed_case(nodeid, stock_stdout * copies)

    with pytest.raises(ValueError, match="stdout spans"):
        normalize_adoption_observations(cases, project="rich", revision=RICH_REVISION)


@pytest.mark.parametrize(
    ("project", "revision", "nodeid"),
    [
        ("requests", RICH_REVISION, "test_console.py::test_brokenpipeerror"),
        ("rich", "different-revision", "test_console.py::test_brokenpipeerror"),
        ("rich", RICH_REVISION, "test_console.py::test_other"),
    ],
)
def test_rich_normalization_requires_pinned_project_and_named_case(
    project: str, revision: str, nodeid: str
) -> None:
    cases = observed_case(nodeid, OUTPUT_PAIRS[0][1])

    normalized, applied = normalize_adoption_observations(cases, project=project, revision=revision)

    assert normalized == cases
    assert applied == []


DOTENV_NODEID = "tests/test_cli.py::test_unset_non_existent_value"
DOTENV_LOG = (
    "WARNING  dotenv.main:main.py:286 Key a not removed from "
    "<PYTEST_TMP>/test_unset_non_existent_value0/.env - key doesn't exist."
)


@pytest.mark.parametrize("worker", [0, 1])
def test_dotenv_worker_root_preserves_raw_log_and_other_fields(worker: int) -> None:
    stock = observed_case(DOTENV_NODEID, DOTENV_LOG)
    stock[0]["reports"][0].update(log=DOTENV_LOG, stderr=DOTENV_LOG, diagnostic=DOTENV_LOG)
    stock[0]["reports"].insert(0, {**stock[0]["reports"][0], "phase": "setup"})
    native = copy.deepcopy(stock)
    native[0]["reports"][1]["log"] = DOTENV_LOG.replace(
        "<PYTEST_TMP>/", f"<PYTEST_TMP>/native-worker-{worker}/"
    )
    raw = copy.deepcopy((stock, native))

    normalized_stock, stock_rules = normalize_adoption_observations(
        stock, project="python-dotenv", revision=DOTENV_REVISION
    )
    normalized_native, native_rules = normalize_adoption_observations(
        native, project="python-dotenv", revision=DOTENV_REVISION
    )

    assert normalized_stock == normalized_native == stock
    assert (stock, native) == raw
    assert stock_rules == native_rules
    assert len(stock_rules) == 1
    assert stock_rules[0]["field"] == "log"
    assert stock_rules[0]["phase"] == "call"
    assert stock_rules[0]["occurrences"] == 1


@pytest.mark.parametrize(
    "log",
    [
        "",
        DOTENV_LOG + "\n" + DOTENV_LOG,
        DOTENV_LOG.replace("<PYTEST_TMP>/", "<PYTEST_TMP>/native-worker-2/"),
        DOTENV_LOG.replace("<PYTEST_TMP>/", "<PYTEST_TMP>/arbitrary-directory/"),
        DOTENV_LOG.replace("value0", "value1"),
        DOTENV_LOG.replace("/.env", "/.different"),
        DOTENV_LOG.replace("Key a", "Key b"),
        DOTENV_LOG.replace("not removed", "removed"),
        DOTENV_LOG.replace("doesn't exist", "exists"),
    ],
)
def test_dotenv_changed_path_or_log_semantics_fail_closed(log: str) -> None:
    cases = observed_case(DOTENV_NODEID, "")
    cases[0]["reports"][0]["log"] = log

    with pytest.raises(ValueError, match="expected 1 log spans"):
        normalize_adoption_observations(cases, project="python-dotenv", revision=DOTENV_REVISION)


def test_dotenv_other_log_lines_remain_literal() -> None:
    cases = observed_case(DOTENV_NODEID, "")
    cases[0]["reports"][0]["log"] = DOTENV_LOG + "\nWARNING unrelated diagnostic"
    altered = copy.deepcopy(cases)
    altered[0]["reports"][0]["log"] = DOTENV_LOG + "\nWARNING changed diagnostic"

    normalized, _ = normalize_adoption_observations(
        cases, project="python-dotenv", revision=DOTENV_REVISION
    )
    normalized_altered, _ = normalize_adoption_observations(
        altered, project="python-dotenv", revision=DOTENV_REVISION
    )

    assert normalized != normalized_altered


@pytest.mark.parametrize(
    ("project", "revision", "nodeid"),
    [
        ("other", DOTENV_REVISION, DOTENV_NODEID),
        ("python-dotenv", "different-revision", DOTENV_NODEID),
        ("python-dotenv", DOTENV_REVISION, "tests/test_cli.py::test_other"),
    ],
)
def test_dotenv_rule_requires_pinned_project_and_named_case(
    project: str, revision: str, nodeid: str
) -> None:
    cases = observed_case(nodeid, "")
    cases[0]["reports"][0]["log"] = DOTENV_LOG.replace(
        "<PYTEST_TMP>/", "<PYTEST_TMP>/native-worker-0/"
    )

    normalized, applied = normalize_adoption_observations(cases, project=project, revision=revision)

    assert normalized == cases
    assert applied == []


def test_rich_normalization_keeps_other_fields_phases_and_padding_literal() -> None:
    nodeid, stdout, _, _ = OUTPUT_PAIRS[-1]
    cases = observed_case(nodeid, stdout)
    report = cases[0]["reports"][0]
    report.update(exception=stdout, diagnostic=stdout, stderr=stdout, log=stdout)
    setup = {**report, "phase": "setup"}
    cases[0]["reports"].insert(0, setup)

    normalized, applied = normalize_adoption_observations(
        cases, project="rich", revision=RICH_REVISION
    )

    assert normalized[0]["reports"][0] == setup
    call = normalized[0]["reports"][1]
    for field in ("exception", "diagnostic", "stderr", "log"):
        assert call[field] == stdout
    assert applied[0]["phase"] == "call"
    assert applied[0]["field"] == "stdout"
    assert "[00/00/00 00:00:00] ERROR    message" in call["stdout"]

    changed_padding = copy.deepcopy(cases)
    changed_padding[0]["reports"][1]["stdout"] = stdout.replace(
        "ERROR    message", "ERROR     message"
    )
    normalized_padding, _ = normalize_adoption_observations(
        changed_padding, project="rich", revision=RICH_REVISION
    )
    assert normalized_padding != normalized


def test_recursive_capture_is_fully_literal() -> None:
    stdout = "│ ... 34 frames hidden ... │\nRecursionError: deterministic recursion depth\n"
    cases = observed_case("test_traceback.py::test_recursive", stdout)

    normalized, applied = normalize_adoption_observations(
        cases, project="rich", revision=RICH_REVISION
    )
    altered, _ = normalize_adoption_observations(
        observed_case("test_traceback.py::test_recursive", stdout.replace("34", "35")),
        project="rich",
        revision=RICH_REVISION,
    )

    assert normalized == cases
    assert applied == []
    assert altered != normalized


def test_rich_log_clock_columns_match_when_entries_cross_a_second() -> None:
    nodeid, within_second, _, _ = OUTPUT_PAIRS[3]
    across_seconds = within_second.replace(
        "                    ERROR    FORMATTER message 2026-09-30 23:54:03,462",
        "[09/30/26 23:54:04] ERROR    FORMATTER message 2026-09-30 23:54:04,001",
    )

    normalized_within, within_rules = normalize_adoption_observations(
        observed_case(nodeid, within_second), project="rich", revision=RICH_REVISION
    )
    normalized_across, across_rules = normalize_adoption_observations(
        observed_case(nodeid, across_seconds), project="rich", revision=RICH_REVISION
    )

    assert normalized_within == normalized_across
    assert within_rules == across_rules

    altered_message = across_seconds.replace("FORMATTER message", "FORMATTER changed")
    with pytest.raises(ValueError, match="stdout spans"):
        normalize_adoption_observations(
            observed_case(nodeid, altered_message), project="rich", revision=RICH_REVISION
        )


def test_click_diagnostic_address_preserves_raw_and_other_literals() -> None:
    nodeid = "tests/test_chain.py::test_group_chaining"
    diagnostic = (
        "runner = <click.testing.CliRunner object at 0x7fed54741f40>\n\n"
        "RuntimeError: chain mode is unsupported; literal address 0x1234\n"
    )
    stock = observed_case(nodeid, diagnostic)
    stock[0]["reports"][0].update(outcome="xfailed", diagnostic=diagnostic)
    stock[0]["reports"].insert(0, {**stock[0]["reports"][0], "phase": "setup"})
    native = copy.deepcopy(stock)
    native[0]["reports"][1]["diagnostic"] = diagnostic.replace("0x7fed54741f40", "0x7f1222ebc050")
    raw_stock, raw_native = copy.deepcopy((stock, native))

    normalized_stock, stock_rules = normalize_adoption_observations(
        stock, project="click", revision=CLICK_REVISION
    )
    normalized_native, native_rules = normalize_adoption_observations(
        native, project="click", revision=CLICK_REVISION
    )

    assert normalized_stock == normalized_native
    assert (stock, native) == (raw_stock, raw_native)
    expected = copy.deepcopy(stock)
    expected[0]["reports"][1]["diagnostic"] = diagnostic.replace("0x7fed54741f40", "<ADDRESS>")
    assert normalized_stock == expected
    assert stock_rules == native_rules
    assert len(stock_rules) == 1
    assert stock_rules[0]["field"] == "diagnostic"
    assert stock_rules[0]["phase"] == "call"

    altered = copy.deepcopy(native)
    altered[0]["reports"][1]["diagnostic"] = diagnostic.replace("unsupported", "supported")
    normalized_altered, _ = normalize_adoption_observations(
        altered, project="click", revision=CLICK_REVISION
    )
    assert normalized_altered != normalized_stock


@pytest.mark.parametrize("copies", [0, 2])
def test_click_diagnostic_address_count_changes_fail_closed(copies: int) -> None:
    cases = observed_case("tests/test_chain.py::test_group_chaining", "")
    cases[0]["reports"][0]["diagnostic"] = (
        "runner = <click.testing.CliRunner object at 0x7fed54741f40>\n" * copies
    )

    with pytest.raises(ValueError, match="expected 1 diagnostic spans"):
        normalize_adoption_observations(cases, project="click", revision=CLICK_REVISION)


@pytest.mark.parametrize("call_count", [0, 2])
def test_click_diagnostic_requires_one_call_report(call_count: int) -> None:
    cases = observed_case("tests/test_chain.py::test_group_chaining", "")
    report = cases[0]["reports"][0]
    report["diagnostic"] = "runner = <click.testing.CliRunner object at 0x7fed54741f40>\n"
    cases[0]["reports"] = [dict(report) for _ in range(call_count)]

    with pytest.raises(ValueError, match="requires one call report"):
        normalize_adoption_observations(cases, project="click", revision=CLICK_REVISION)


@pytest.mark.parametrize(
    ("project", "revision", "nodeid"),
    [
        ("other", CLICK_REVISION, "tests/test_chain.py::test_group_chaining"),
        ("click", "different-revision", "tests/test_chain.py::test_group_chaining"),
        ("click", CLICK_REVISION, "tests/test_chain.py::test_other"),
    ],
)
def test_click_normalization_requires_pinned_project_and_named_case(
    project: str, revision: str, nodeid: str
) -> None:
    cases = observed_case(nodeid, "")
    cases[0]["reports"][0]["diagnostic"] = (
        "runner = <click.testing.CliRunner object at 0x7fed54741f40>\n"
    )

    normalized, applied = normalize_adoption_observations(cases, project=project, revision=revision)

    assert normalized == cases
    assert applied == []


@pytest.mark.parametrize(("nodeid", "diagnostic"), DATEUTIL_DIAGNOSTICS)
def test_dateutil_addresses_preserve_raw_and_all_other_literals(
    nodeid: str, diagnostic: str
) -> None:
    diagnostic += "literal failure message; unrelated address 0x1234\n"
    stock = observed_case(nodeid, diagnostic)
    stock[0]["reports"][0].update(outcome="xfailed", diagnostic=diagnostic)
    stock[0]["reports"].insert(0, {**stock[0]["reports"][0], "phase": "setup"})
    native = copy.deepcopy(stock)
    native[0]["reports"][1]["diagnostic"] = diagnostic.replace("0x7fed54741f40", "0x7f1222ebc050")
    original = copy.deepcopy((stock, native))

    normalized_stock, stock_rules = normalize_adoption_observations(
        stock, project="python-dateutil", revision=DATEUTIL_REVISION
    )
    normalized_native, native_rules = normalize_adoption_observations(
        native, project="python-dateutil", revision=DATEUTIL_REVISION
    )

    assert normalized_stock == normalized_native
    assert (stock, native) == original
    expected = copy.deepcopy(stock)
    expected[0]["reports"][1]["diagnostic"] = diagnostic.replace("0x7fed54741f40", "<ADDRESS>")
    assert normalized_stock == expected
    assert stock_rules == native_rules
    assert all(rule["field"] == "diagnostic" and rule["phase"] == "call" for rule in stock_rules)

    for field in ("outcome", "exception", "stdout", "stderr", "log", "diagnostic"):
        altered = copy.deepcopy(native)
        altered[0]["reports"][1][field] = "changed semantics\n" + str(
            altered[0]["reports"][1][field]
        )
        normalized_altered, _ = normalize_adoption_observations(
            altered, project="python-dateutil", revision=DATEUTIL_REVISION
        )
        assert normalized_altered != normalized_stock

    altered = copy.deepcopy(native)
    altered[0]["warnings"] = [{"message": "changed warning"}]
    normalized_altered, _ = normalize_adoption_observations(
        altered, project="python-dateutil", revision=DATEUTIL_REVISION
    )
    assert normalized_altered != normalized_stock


@pytest.mark.parametrize(("nodeid", "diagnostic"), DATEUTIL_DIAGNOSTICS)
@pytest.mark.parametrize("copies", [0, 2])
def test_dateutil_address_counts_fail_closed(nodeid: str, diagnostic: str, copies: int) -> None:
    cases = observed_case(nodeid, "")
    cases[0]["reports"][0]["diagnostic"] = diagnostic * copies

    with pytest.raises(ValueError, match="diagnostic spans"):
        normalize_adoption_observations(
            cases, project="python-dateutil", revision=DATEUTIL_REVISION
        )


@pytest.mark.parametrize(("nodeid", "diagnostic"), DATEUTIL_DIAGNOSTICS)
def test_dateutil_address_rules_reject_changed_classes(nodeid: str, diagnostic: str) -> None:
    cases = observed_case(nodeid, "")
    cases[0]["reports"][0]["diagnostic"] = diagnostic.replace(" object at", "Changed object at")

    with pytest.raises(ValueError, match="diagnostic spans"):
        normalize_adoption_observations(
            cases, project="python-dateutil", revision=DATEUTIL_REVISION
        )


@pytest.mark.parametrize(
    ("project", "revision", "nodeid"),
    [
        ("other", DATEUTIL_REVISION, DATEUTIL_DIAGNOSTICS[0][0]),
        ("python-dateutil", "other-revision", DATEUTIL_DIAGNOSTICS[0][0]),
        ("python-dateutil", DATEUTIL_REVISION, "tests/test_isoparser.py::test_other"),
    ],
)
def test_dateutil_addresses_require_pinned_project_and_named_case(
    project: str, revision: str, nodeid: str
) -> None:
    cases = observed_case(nodeid, "")
    cases[0]["reports"][0]["diagnostic"] = DATEUTIL_DIAGNOSTICS[0][1]

    normalized, applied = normalize_adoption_observations(cases, project=project, revision=revision)

    assert normalized == cases
    assert applied == []


ATTRS_NODEID = "tests/test_setattr.py::TestSetAttr::test_slotted_confused"
ATTRS_DIAGNOSTIC_LINES = (
    "self = <tests.test_setattr.TestSetAttr object at 0x7fed54741f40>",
    """_ = <[AttributeError("'C' object has no attribute 'x'") raised in repr()] """
    "C object at 0x7fed54741f40>",
    "__ = Attribute(name='x', default=NOTHING, validator=None, repr=True, "
    "eq=True, eq_key=None, order=True, order_key=None, hash..., type=None, "
    "converter=None, kw_only=False, inherited=False, "
    "on_setattr=<function frozen at 0x7fed54741f40>, alias='x')",
)
ATTRS_DIAGNOSTIC = "\n".join(ATTRS_DIAGNOSTIC_LINES) + (
    "\nattr.exceptions.FrozenAttributeError: can't set attribute; literal address 0x1234\n"
)


def test_attrs_addresses_preserve_raw_phases_and_other_literals() -> None:
    stock = observed_case(ATTRS_NODEID, ATTRS_DIAGNOSTIC)
    stock[0]["reports"][0].update(outcome="xfailed", diagnostic=ATTRS_DIAGNOSTIC)
    stock[0]["reports"].insert(0, {**stock[0]["reports"][0], "phase": "setup"})
    native = copy.deepcopy(stock)
    native[0]["reports"][1]["diagnostic"] = ATTRS_DIAGNOSTIC.replace(
        "0x7fed54741f40", "0x7f1222ebc050"
    )
    original = copy.deepcopy((stock, native))

    normalized_stock, stock_rules = normalize_adoption_observations(
        stock, project="attrs", revision=ATTRS_REVISION
    )
    normalized_native, native_rules = normalize_adoption_observations(
        native, project="attrs", revision=ATTRS_REVISION
    )

    expected = copy.deepcopy(stock)
    expected[0]["reports"][1]["diagnostic"] = ATTRS_DIAGNOSTIC.replace(
        "0x7fed54741f40", "<ADDRESS>"
    )
    assert normalized_stock == normalized_native == expected
    assert (stock, native) == original
    assert stock_rules == native_rules
    assert len(stock_rules) == 3
    assert all(
        rule["field"] == "diagnostic" and rule["phase"] == "call" and rule["occurrences"] == 1
        for rule in stock_rules
    )


@pytest.mark.parametrize(
    "prefix", ["", "tests.test_setattr.TestSetAttr.test_slotted_confused.<locals>."]
)
def test_attrs_failed_instance_accepts_both_python_class_names(prefix: str) -> None:
    diagnostic = ATTRS_DIAGNOSTIC.replace("'C' object has", f"'{prefix}C' object has")
    cases = observed_case(ATTRS_NODEID, "")
    cases[0]["reports"][0].update(outcome="xfailed", diagnostic=diagnostic)

    normalized, rules = normalize_adoption_observations(
        cases, project="attrs", revision=ATTRS_REVISION
    )

    assert normalized[0]["reports"][0]["diagnostic"] == diagnostic.replace(
        "0x7fed54741f40", "<ADDRESS>"
    )
    assert len(rules) == 3


@pytest.mark.parametrize("change", ["other_class", "other_message"])
def test_attrs_qualified_failed_instance_rejects_unreviewed_context(change: str) -> None:
    prefix = "tests.test_setattr.TestSetAttr.test_slotted_confused.<locals>."
    diagnostic = ATTRS_DIAGNOSTIC.replace("'C' object has", f"'{prefix}C' object has")
    diagnostic = (
        diagnostic.replace("test_slotted_confused", "other_test")
        if change == "other_class"
        else diagnostic.replace("no attribute 'x'", "no attribute 'y'")
    )
    cases = observed_case(ATTRS_NODEID, "")
    cases[0]["reports"][0].update(outcome="xfailed", diagnostic=diagnostic)

    with pytest.raises(ValueError, match="expected 1 diagnostic spans"):
        normalize_adoption_observations(cases, project="attrs", revision=ATTRS_REVISION)


@pytest.mark.parametrize("field", ["outcome", "exception", "stdout", "stderr", "log", "diagnostic"])
def test_attrs_non_address_changes_remain_unequal(field: str) -> None:
    stock = observed_case(ATTRS_NODEID, "")
    stock[0]["reports"][0].update(outcome="xfailed", diagnostic=ATTRS_DIAGNOSTIC)
    altered = copy.deepcopy(stock)
    altered[0]["reports"][0][field] = "changed semantics\n" + str(altered[0]["reports"][0][field])

    normalized, _ = normalize_adoption_observations(stock, project="attrs", revision=ATTRS_REVISION)
    normalized_altered, _ = normalize_adoption_observations(
        altered, project="attrs", revision=ATTRS_REVISION
    )

    assert normalized_altered != normalized


def test_attrs_warnings_remain_literal() -> None:
    stock = observed_case(ATTRS_NODEID, "")
    stock[0]["reports"][0]["diagnostic"] = ATTRS_DIAGNOSTIC
    altered = copy.deepcopy(stock)
    altered[0]["warnings"] = [{"message": "changed warning"}]

    normalized, _ = normalize_adoption_observations(stock, project="attrs", revision=ATTRS_REVISION)
    normalized_altered, _ = normalize_adoption_observations(
        altered, project="attrs", revision=ATTRS_REVISION
    )

    assert normalized_altered != normalized


@pytest.mark.parametrize("line", ATTRS_DIAGNOSTIC_LINES)
@pytest.mark.parametrize("copies", [0, 2])
def test_attrs_address_counts_fail_closed(line: str, copies: int) -> None:
    cases = observed_case(ATTRS_NODEID, "")
    cases[0]["reports"][0]["diagnostic"] = ATTRS_DIAGNOSTIC.replace(
        line + "\n", (line + "\n") * copies
    )

    with pytest.raises(ValueError, match="expected 1 diagnostic spans"):
        normalize_adoption_observations(cases, project="attrs", revision=ATTRS_REVISION)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("TestSetAttr object", "OtherTest object"),
        ("C object at", "D object at"),
        ("no attribute 'x'", "no attribute 'y'"),
        ("function frozen", "function changed"),
        ("alias='x'", "alias='y'"),
    ],
)
def test_attrs_changed_repr_context_fails_closed(before: str, after: str) -> None:
    cases = observed_case(ATTRS_NODEID, "")
    cases[0]["reports"][0]["diagnostic"] = ATTRS_DIAGNOSTIC.replace(before, after)

    with pytest.raises(ValueError, match="expected 1 diagnostic spans"):
        normalize_adoption_observations(cases, project="attrs", revision=ATTRS_REVISION)


@pytest.mark.parametrize("call_count", [0, 2])
def test_attrs_requires_one_call_report(call_count: int) -> None:
    cases = observed_case(ATTRS_NODEID, "")
    report = {**cases[0]["reports"][0], "diagnostic": ATTRS_DIAGNOSTIC}
    cases[0]["reports"] = [dict(report) for _ in range(call_count)]

    with pytest.raises(ValueError, match="requires one call report"):
        normalize_adoption_observations(cases, project="attrs", revision=ATTRS_REVISION)


@pytest.mark.parametrize(
    ("project", "revision", "nodeid"),
    [
        ("other", ATTRS_REVISION, ATTRS_NODEID),
        ("attrs", "other-revision", ATTRS_NODEID),
        ("attrs", ATTRS_REVISION, "tests/test_setattr.py::TestSetAttr::test_other"),
    ],
)
def test_attrs_rule_requires_pinned_project_and_named_case(
    project: str, revision: str, nodeid: str
) -> None:
    cases = observed_case(nodeid, "")
    cases[0]["reports"][0]["diagnostic"] = ATTRS_DIAGNOSTIC

    normalized, applied = normalize_adoption_observations(cases, project=project, revision=revision)

    assert normalized == cases
    assert applied == []
