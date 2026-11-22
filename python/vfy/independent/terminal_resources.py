"""Operands and call frames for terminal rendering."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from vfy.native import _pytest

if TYPE_CHECKING:
    from vfy._pytest_native import NativeIndependentReport, NativeSessionReports

    from .protocol import Config
    from .terminal import TerminalReporter, TerminalWriter


class TestShortLogReport(NamedTuple):
    """Retain the actual three status-hook operands in their consumer frame."""

    category: object
    letter: object
    word: object


def logstart(reporter: TerminalReporter, nodeid: str, location: Iterable[object]) -> None:
    """Unpack a public location through its iterable protocol."""
    fspath, lineno, domain = location
    reporter._status_index.terminal_logstart(reporter, nodeid, (fspath, lineno, domain))


def consume(
    index: NativeSessionReports,
    report: NativeIndependentReport,
    reporter: TerminalReporter,
    status_result: Callable[[], Iterable[object]] | None = None,
) -> None:
    rep = report
    res = TestShortLogReport(
        *(index.terminal_status_result(rep, reporter) if status_result is None else status_result())
    )
    category, letter, word = res.category, res.letter, res.word
    word, markup = index.terminal_status_word(word)
    index.terminal_status_emit(rep, reporter, category, letter, word, markup)


def write_letter(
    reporter: TerminalReporter,
    letter: object,
    markup: Any,  # noqa: ANN401 - CPython ** accepts real user keys/getitem protocols outside Mapping.
) -> None:
    reporter._tw.write(letter, **markup)


def fill_progress(reporter: TerminalReporter) -> None:
    color, _ = reporter._get_main_color()
    msg = reporter._get_progress_information_message()
    w = reporter._width_of_current_line
    fill = reporter._status_index.terminal_progress_fill(reporter, w)
    reporter.write(
        reporter._status_index.terminal_progress_rjust(msg, fill),
        flush=True,
        **reporter._status_index.terminal_progress_markup(color),
    )


def progress_message(reporter: TerminalReporter) -> str:
    collected = reporter._status_index.terminal_progress_collected(reporter)
    kind = reporter._status_index.terminal_progress_message_kind(reporter, collected)
    if kind == "count":
        progress = reporter.reported_progress
        return reporter._status_index.terminal_progress_message(reporter, collected, kind, progress)
    return reporter._status_index.terminal_progress_message(reporter, collected, kind, None)


def past_edge(reporter: TerminalReporter) -> None:
    w = reporter._width_of_current_line
    if reporter._status_index.terminal_progress_is_count(reporter):
        num_tests = reporter._status_index.terminal_progress_collected(reporter)
        progress_length = reporter._status_index.terminal_progress_count_length(num_tests)
    else:
        progress_length = reporter._status_index.terminal_progress_edge_length(reporter)
    past_edge = reporter._status_index.terminal_progress_past_edge(reporter, w, progress_length)
    if past_edge:
        main_color, _ = reporter._get_main_color()
        msg = reporter._get_progress_information_message()
        reporter._tw.write(
            reporter._status_index.terminal_progress_newline(msg),
            **reporter._status_index.terminal_progress_markup(main_color),
        )


def write_fspath_result(
    reporter: TerminalReporter, nodeid: str, res: str, markup: Mapping[str, bool]
) -> None:
    fspath = reporter._status_index.terminal_fspath(reporter, nodeid)
    reporter._status_index.terminal_path_prefix(reporter, fspath)
    reporter._tw.write(res, flush=True, **markup)


def cwd_relative_nodeid(config: Config, nodeid: str) -> str:
    base_path_part, *nodeid_part = nodeid.split("::")
    fullpath = config._configuration.nodeid_fullpath(config, base_path_part)
    relative_path = config._configuration.nodeid_relative_path(config, fullpath)
    nodeid = config._configuration.join_relative_nodeid(relative_path, nodeid_part)
    return nodeid  # noqa: RET504 - Preserve the actual nodeid assignment boundary.


def location_relative(reporter: TerminalReporter, nodeid: str, domain: str) -> str:
    line = reporter.config.cwd_relative_nodeid(nodeid)
    if reporter._status_index.terminal_location_domain_matches(line, domain):
        line = reporter._status_index.terminal_location_trim_domain(line, domain)
        values = domain.split("[")
        values[0] = reporter._status_index.terminal_location_domain_value(values)
        line = reporter._status_index.terminal_location_append_domain(line, values)
    return line


def location_line(
    reporter: TerminalReporter, nodeid: str, fspath: str, lineno: int | None, domain: str
) -> str:
    if reporter._status_index.terminal_location_has_path(fspath):
        res = location_relative(reporter, nodeid, domain)
        if reporter._status_index.terminal_location_needs_origin(reporter, nodeid, fspath):
            res = reporter._status_index.terminal_location_append_origin(reporter, res, fspath)
    else:
        res = reporter._status_index.terminal_location_missing()
    return reporter._status_index.terminal_location_finish(res)


def verbose_start(
    reporter: TerminalReporter, nodeid: str, location: tuple[str, int | None, str]
) -> None:
    line = reporter._locationline(nodeid, *location)
    reporter.write_ensure_prefix(line, "")
    reporter.flush()


def verbose_report(
    reporter: TerminalReporter,
    rep: NativeIndependentReport,
    word: object,
    markup: Any,  # noqa: ANN401 - The actual ** mapping protocol belongs to the selected call.
) -> None:
    line = reporter._locationline(rep.nodeid, *rep.location)
    running_xdist = reporter._status_index.terminal_verbose_running_xdist(rep)
    if not running_xdist:
        reporter.write_ensure_prefix(line, word, **markup)
        if rep.skipped or hasattr(rep, "wasxfail"):
            reason = raw_skip_reason(rep)
            if reporter.config.get_verbosity("test_cases") < 2:
                available_width = (
                    (reporter._tw.fullwidth - reporter._tw.width_of_current_line)
                    - len(" [100%]")
                    - 1
                )
                formatted_reason = format_trimmed(" ({})", reason, available_width)
            else:
                formatted_reason = f" ({reason})"
            if reason and formatted_reason is not None:
                reporter.wrap_write(formatted_reason)
        reporter._status_index.terminal_verbose_after_prefix(rep, reporter)
    reporter.flush()


def raw_skip_reason(report: NativeIndependentReport) -> str:
    """Return the user-supplied part of a skip, xfail, or xpass reason, as pytest does."""
    if hasattr(report, "wasxfail"):
        reason = report.wasxfail
        # Plugins observe startswith and slicing on the reason, so keep those calls.
        if reason.startswith("reason: "):  # noqa: FURB188 - Observable startswith and slice calls.
            reason = reason[len("reason: ") :]
        return reason
    assert report.skipped
    assert isinstance(report.longrepr, tuple)
    reason = cast("str", report.longrepr[2])
    if reason.startswith("Skipped: "):
        reason = reason[len("Skipped: ") :]
    elif reason == "Skipped":
        reason = ""
    return reason


def format_trimmed(format: str, msg: str, available_width: int) -> str | None:  # noqa: A002 - pytest's parameter name
    """Ellipsize ``msg`` into ``format`` to fit ``available_width``, as pytest does."""
    i = msg.find("\n")
    if i != -1:
        msg = msg[:i]
    ellipsis = "..."
    format_width = _pytest.terminal_text_width(format.format(""))
    if format_width + len(ellipsis) > available_width:
        return None
    if format_width + _pytest.terminal_text_width(msg) > available_width:
        available_width -= len(ellipsis)
        msg = msg[:available_width]
        while format_width + _pytest.terminal_text_width(msg) > available_width:
            msg = msg[:-1]
        msg += ellipsis
    return format.format(msg)


def summary_markup(
    writer: TerminalWriter,
    word: object,
    markup: Any,  # noqa: ANN401 - Preserve the caller's actual ** mapping protocol.
) -> object:
    return writer.markup(word, **markup)
