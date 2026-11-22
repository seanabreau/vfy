"""Reviewed volatile spans in pinned adoption workloads.

These comparison copies retain every case, phase, and literal outside the
named spans. The execution artifacts and transcripts remain untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

RICH_REVISION = "9d8f9a372cc5916fd4781fec207ced7ddac2f08f"
CLICK_REVISION = "8b19813f2bfca99f1018a587a8cf54fc959f2e5d"
DOTENV_REVISION = "a00cb2eed0704cd6d2071b2004c37e95ccc86ee5"
ATTRS_REVISION = "8f767776326faaed11e6c2974798787f6e19b343"
DATEUTIL_REVISION = "2642afacc33fb839c404b75b230fff58b79793f2"
SORTEDCONTAINERS_REVISION = "3ac358631f58c1347f1d6d2d92784117db0f38ed"


@dataclass(frozen=True)
class ObservationRule:
    name: str
    nodeid: str
    pattern: re.Pattern[str]
    replacement: str
    occurrences: int
    rationale: str
    field: Literal["stdout", "diagnostic", "log"] = "stdout"
    unchanged_pattern: re.Pattern[str] | None = None


RICH_RULES = (
    ObservationRule(
        "render-duration",
        "test_console.py::test_brokenpipeerror",
        re.compile(r"^rendered in (?P<volatile>\d+\.\d)ms \((?:cold|warm) cache\)$", re.MULTILINE),
        "<DURATION>",
        2,
        "The console demo prints measured render time, outside this untimed outcome check.",
    ),
    ObservationRule(
        "swig-object-address",
        "test_inspect.py::test_inspect_swig_edge_case",
        re.compile(
            r"<tests\.test_inspect\.test_inspect_swig_edge_case\.<locals>\.Thing object at"
            r"[^\n]*\n│ │ (?P<volatile>0x[0-9a-f]{12})>"
        ),
        "0x????????????",
        1,
        "The default CPython object repr embeds a process-specific allocation address.",
    ),
    ObservationRule(
        "lru-wrapper-address",
        "test_inspect.py::test_qualname_in_slots",
        re.compile(r"<functools\._lru_cache_wrapper object at (?P<volatile>0x[0-9a-f]{12})>"),
        "0x????????????",
        1,
        "The default CPython wrapper repr embeds a process-specific allocation address.",
    ),
    ObservationRule(
        "exception-log-clock",
        "test_logging.py::test_exception",
        re.compile(
            r"^(?P<volatile>\[\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}\]| {19})"
            r" ERROR    FORMATTER (?:foo|message) ",
            re.MULTILINE,
        ),
        "[00/00/00 00:00:00]",
        2,
        "Rich blanks the wall-clock label when consecutive entries share a second.",
    ),
    ObservationRule(
        "exception-formatter-clock",
        "test_logging.py::test_exception",
        re.compile(
            r"FORMATTER (?:foo|message) "
            r"(?P<volatile>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"
        ),
        "0000-00-00 00:00:00,000",
        2,
        "The logging formatter's asctime field displays wall-clock time.",
    ),
    ObservationRule(
        "extra-lines-log-clock",
        "test_logging.py::test_exception_with_extra_lines",
        re.compile(r"^\[(?P<volatile>\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\] ERROR", re.MULTILINE),
        "00/00/00 00:00:00",
        1,
        "Rich's log label displays wall-clock time.",
    ),
)


CLICK_RULES = (
    ObservationRule(
        "clirunner-address",
        "tests/test_chain.py::test_group_chaining",
        re.compile(
            r"^runner = <click\.testing\.CliRunner object at (?P<volatile>0x[0-9a-f]+)>$",
            re.MULTILINE,
        ),
        "<ADDRESS>",
        1,
        "The expected-xfail traceback includes a process-specific fixture repr address.",
        field="diagnostic",
    ),
)


DOTENV_RULES = (
    ObservationRule(
        "worker-temporary-root",
        "tests/test_cli.py::test_unset_non_existent_value",
        re.compile(
            r"^WARNING  dotenv\.main:main\.py:286 Key a not removed from <PYTEST_TMP>/"
            r"(?P<volatile>(?:native-worker-[01]/)?)"
            r"test_unset_non_existent_value0/\.env - key doesn't exist\.$",
            re.MULTILINE,
        ),
        "",
        1,
        "Native workers isolate their pytest temporary roots under the requested basetemp.",
        field="log",
    ),
)


ATTRS_RULES = tuple(
    ObservationRule(
        name,
        "tests/test_setattr.py::TestSetAttr::test_slotted_confused",
        re.compile(pattern, re.MULTILINE),
        "<ADDRESS>",
        1,
        "The expected-xfail traceback includes process-specific repr addresses.",
        field="diagnostic",
    )
    for name, pattern in (
        (
            "test-instance-address",
            r"^self = <tests\.test_setattr\.TestSetAttr object at (?P<volatile>0x[0-9a-f]+)>$",
        ),
        (
            "failed-instance-address",
            r"^_ = <\[AttributeError\(\"'(?:tests\.test_setattr\.TestSetAttr\."
            r"test_slotted_confused\.<locals>\.)?C' object has no attribute 'x'\"\) "
            r"raised in repr\(\)\] "
            r"C object at (?P<volatile>0x[0-9a-f]+)>$",
        ),
        (
            "setattr-function-address",
            r"^__ = Attribute\(name='x', default=NOTHING, validator=None, repr=True, "
            r"eq=True, eq_key=None, order=True, order_key=None, hash\.\.\.(?:\{\}\))?, type=None, "
            r"converter=None, kw_only=False, inherited=False, "
            r"on_setattr=<function frozen at (?P<volatile>0x[0-9a-f]+)>, alias='x'\)$",
        ),
    )
)


DATEUTIL_PARSER_CASES = (
    ("test_somewhat_ambiguous_string", 0),
    ("test_YmdH_M_S", 2),
    ("test_first_century", 2),
    ("test_era_trailing_year_with_dots", 1),
    ("test_ad_nospace", 0),
    ("test_four_letter_day", 1),
    ("test_non_date_number", 0),
    ("test_on_era", 2),
    ("test_extraneous_year", 0),
    ("test_extraneous_year_tokens", 0),
    ("test_extraneous_year2", 2),
    ("test_extraneous_year3", 1),
    ("test_unambiguous_YYYYMM", 2),
    ("test_extraneous_numerical_content", 0),
)

DATEUTIL_RULES = (
    ObservationRule(
        "isoparser-address",
        "tests/test_isoparser.py::test_isoparser_byte_sep",
        re.compile(
            r"^self = <dateutil\.parser\.isoparser\.isoparser object at "
            r"(?P<volatile>0x[0-9a-f]+)>$",
            re.MULTILINE,
        ),
        "<ADDRESS>",
        1,
        "The expected-xfail traceback includes a process-specific parser repr address.",
        field="diagnostic",
    ),
    *(
        ObservationRule(
            "parser-case-address",
            f"tests/test_parser.py::TestParseUnimplementedCases::{name}",
            re.compile(
                r"^self = <tests\.test_parser\.TestParseUnimplementedCases object at "
                r"(?P<volatile>0x[0-9a-f]+)>$",
                re.MULTILINE,
            ),
            "<ADDRESS>",
            1,
            "The expected-xfail traceback includes a process-specific test instance repr address.",
            field="diagnostic",
        )
        for name, _ in DATEUTIL_PARSER_CASES
    ),
    *(
        ObservationRule(
            "parser-address",
            f"tests/test_parser.py::TestParseUnimplementedCases::{name}",
            re.compile(
                r"^self = <dateutil\.parser\._parser\.parser object at "
                r"(?P<volatile>0x[0-9a-f]+)>$",
                re.MULTILINE,
            ),
            "<ADDRESS>",
            count,
            "The expected-xfail traceback includes process-specific parser repr addresses.",
            field="diagnostic",
        )
        for name, count in DATEUTIL_PARSER_CASES
        if count
    ),
    *(
        ObservationRule(
            f"{variable}-address",
            "tests/test_tz.py::test_gettz_zone_wrong_type[no startswith()]",
            re.compile(
                rf"^{variable} = <object object at (?P<volatile>0x[0-9a-f]+)>{suffix}$",
                re.MULTILINE,
            ),
            "<ADDRESS>",
            1,
            "The expected-xfail traceback includes a process-specific object repr address.",
            field="diagnostic",
        )
        for variable, suffix in (("badzone", ", exc_reason = None"), ("name", ""))
    ),
)


SORTEDLIST_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "<UPSTREAM>/src/sortedcontainers/sortedlist.py", line 1524, in _check\n'
    "    assert self._len == sum(len(sublist) for sublist in self._lists)\n"
)
SORTEDLIST_STATE = (
    "len 5\nload 4\noffset 0\nlen_index 0\nindex []\nlen_maxes 3\n"
    "maxes [3, 7, 9]\nlen_lists 3\nlists [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]\n"
)
SORTEDCONTAINERS_RULES = (
    ObservationRule(
        "caught-generator-address",
        "tests/test_coverage_sortedlist.py::test_check",
        re.compile(
            r"\A"
            + re.escape(
                SORTEDLIST_TRACEBACK + "AssertionError: assert 5 == 10\n"
                " +  where 5 = SortedList([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])._len\n"
                " +  and   10 = sum(<generator object SortedList._check.<locals>.<genexpr> at "
            )
            + r"(?P<volatile>0x[0-9a-f]+)"
            + re.escape(">)\n" + SORTEDLIST_STATE)
            + r"\Z"
        ),
        "<ADDRESS>",
        1,
        "Explicit doctest source selectors trigger pytest assertion rewriting; the caught "
        "traceback then includes a process-specific generator repr address.",
        unchanged_pattern=re.compile(
            re.escape(
                SORTEDLIST_TRACEBACK
                + "           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
                "AssertionError\n" + SORTEDLIST_STATE
            )
        ),
    ),
)


def normalize_adoption_observations(
    cases: list[dict[str, Any]], *, project: str, revision: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return comparison copies and audited rules, failing closed on changed span counts."""
    applied: list[dict[str, Any]] = []
    if project == "rich" and revision == RICH_REVISION:
        project_rules = RICH_RULES
    elif project == "click" and revision == CLICK_REVISION:
        project_rules = CLICK_RULES
    elif project == "python-dotenv" and revision == DOTENV_REVISION:
        project_rules = DOTENV_RULES
    elif project == "attrs" and revision == ATTRS_REVISION:
        project_rules = ATTRS_RULES
    elif project == "python-dateutil" and revision == DATEUTIL_REVISION:
        project_rules = DATEUTIL_RULES
    elif project == "sortedcontainers" and revision == SORTEDCONTAINERS_REVISION:
        project_rules = SORTEDCONTAINERS_RULES
    else:
        return cases, applied
    normalized = list(cases)
    for case_index, case in enumerate(cases):
        rules = [rule for rule in project_rules if rule.nodeid == case["nodeid"]]
        if not rules:
            continue
        call_indices = [
            index for index, report in enumerate(case["reports"]) if report["phase"] == "call"
        ]
        if len(call_indices) != 1:
            raise ValueError(
                f"{case['nodeid']}: reviewed {project} output requires one call report"
            )
        normalized_case = {**case, "reports": list(case["reports"])}
        report = dict(case["reports"][call_indices[0]])
        normalized_case["reports"][call_indices[0]] = report
        normalized[case_index] = normalized_case
        for rule in rules:
            value = report[rule.field]
            matches = list(rule.pattern.finditer(value))
            unchanged = (
                not matches
                and rule.unchanged_pattern is not None
                and rule.unchanged_pattern.fullmatch(value) is not None
            )
            if len(matches) != rule.occurrences and not unchanged:
                raise ValueError(
                    f"{rule.nodeid}: {rule.name} expected {rule.occurrences} {rule.field} spans, "
                    f"found {len(matches)}"
                )
            # Replace only the captured span, preserving the match's context,
            # all layout, and any other text in this field.
            for match in reversed(matches):
                start, end = match.span("volatile")
                value = value[:start] + rule.replacement + value[end:]
            report[rule.field] = value
            applied.append(
                {
                    "project": project,
                    "revision": revision,
                    "nodeid": rule.nodeid,
                    "phase": "call",
                    "field": rule.field,
                    "rule": rule.name,
                    "occurrences": len(matches),
                    "rationale": rule.rationale,
                }
            )
    return normalized, applied
