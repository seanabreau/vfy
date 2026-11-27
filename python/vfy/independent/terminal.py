"""Caller-owned output streams for independent terminal-summary hooks."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TextIO, cast

from vfy.native import _pytest

from . import terminal_resources
from .facade import hookimpl

if TYPE_CHECKING:
    from vfy._pytest_native import (
        NativeIndependentCollectReport,
        NativeSessionReports,
    )

    from .diagnostics import ExceptionRepr
    from .facade import ExceptionInfo
    from .protocol import Config, Session


KNOWN_TYPES = (
    "failed",
    "passed",
    "skipped",
    "deselected",
    "xfailed",
    "xpassed",
    "warnings",
    "error",
    "subtests passed",
    "subtests failed",
    "subtests skipped",
)


class TerminalWriter:
    """Write native-formatted text to the caller's terminal stream."""

    _esctable: ClassVar[dict[str, int]] = _pytest.terminal_markup_table()

    def __init__(self, config: Config | None, stream: TextIO) -> None:
        self._stream = stream
        self.fullwidth = shutil.get_terminal_size().columns
        self._current_line = ""
        choice = "no" if config is None else config.getoption("color", default="auto")
        self.hasmarkup = choice == "yes" or (
            choice == "auto" and self._stream.isatty() and "NO_COLOR" not in os.environ
        )

    def write(self, msg: object, *, flush: bool = False, **markup: bool) -> None:
        pending = _pytest.terminal_prepare_write(self, msg, markup)
        if pending is not None:
            # These CPython slots retain the actual intermediates through raw dispatch.
            _current_line, msg = pending
            del pending
            self.write_raw(msg, flush=flush)

    @property
    def width_of_current_line(self) -> int:
        return _pytest.terminal_line_width(self)

    def markup(self, text: object, **markup: bool) -> object:
        # The actual frame retains selected operands through formatter tracebacks.
        esc = _pytest.terminal_markup_codes(self, markup)
        if esc:
            text = _pytest.terminal_markup_render(text, esc)
        return text

    def write_raw(self, msg: object, *, flush: bool = False) -> None:
        try:
            self._stream.write(cast("str", msg))
        except UnicodeEncodeError:
            # Print the string escaped to ASCII, as pytest does.
            msg = cast("str", msg).encode("unicode-escape").decode("ascii")
            self._stream.write(msg)
        if flush:
            self.flush()

    def line(self, s: str = "", **markup: bool) -> None:
        self.write(s, **markup)
        self.write("\n")

    def sep(
        self,
        sepchar: str,
        title: str | None = None,
        fullwidth: int | None = None,
        **markup: bool,
    ) -> None:
        width = self.fullwidth if fullwidth is None else fullwidth
        self.write(_pytest.render_terminal_separator(sepchar, title, width), **markup)
        self.write("\n")

    def _highlight(self, source: str) -> str:
        return source

    def _write_source(self, lines: Sequence[str], indents: Sequence[str] = ()) -> None:
        _pytest.independent_terminal_write_source(self, lines, indents)

    def flush(self) -> None:
        self._stream.flush()


def report_writer(stream: TextIO) -> TerminalWriter:
    """Adapt a report representation to an uncolored caller-owned stream."""
    return TerminalWriter(None, stream)


class TerminalReporter:
    """Expose terminal output operations over the native text formatter."""

    def __init__(
        self,
        config: Config,
        file: TextIO | None = None,
        *,
        status_index: NativeSessionReports,
        collection_finish: Callable[[TerminalReporter, Session], None],
    ) -> None:
        self.config = config
        self._stream = sys.stdout if file is None else file
        self._status_index = status_index
        self.reportchars = status_index.terminal_reportchars(config)
        self._collection_finish = collection_finish
        self._session: Session | None = None
        self._session_start = time.perf_counter()
        self._progress_nodeids_reported: set[str] = set()
        self._main_color: str | None = None
        self._known_types: list[str] | None = None
        self._keyboardinterrupt_memo: ExceptionRepr | None = None
        self._keyboardinterrupt_message = ""

        self.startpath = config._invocation_dir
        self.currentfspath: Path | str | int | None = None
        self._showfspath: bool | None = None
        self._tw = TerminalWriter(config, self._stream)
        self._screen_width = self._tw.fullwidth
        self._show_progress_info = self._determine_show_progress_info()

    @hookimpl(wrapper=True)
    def pytest_terminal_summary(self) -> Generator[None, object, object]:
        self._status_index.terminal_summary_before(self)
        try:
            return (yield)
        finally:
            self._status_index.terminal_summary_after(self)

    def short_test_summary(self) -> None:
        self._status_index.terminal_short_summary(self)

    def hasopt(self, char: str) -> bool:
        return char in self.reportchars

    def pytest_collectreport(self, report: NativeIndependentCollectReport) -> None:
        if report.failed:
            self._add_stats("error", [report])
        elif report.skipped:
            self._add_stats("skipped", [report])

    def pytest_deselected(self, items: Sequence[Any]) -> None:
        self._add_stats("deselected", items)

    def pytest_collection_finish(self, session: Session) -> None:
        self._collection_finish(self, session)

    @property
    def fullwidth(self) -> int:
        return self._tw.fullwidth

    @fullwidth.setter
    def fullwidth(self, value: int) -> None:
        self._tw.fullwidth = value

    @property
    def hasmarkup(self) -> bool:
        return self._tw.hasmarkup

    @hasmarkup.setter
    def hasmarkup(self, value: bool) -> None:
        self._tw.hasmarkup = value

    stats = _pytest.NativeTerminalStats()

    @property
    def verbosity(self) -> int:
        return self._status_index.terminal_verbosity(self)

    @property
    def reported_progress(self) -> int:
        return self._status_index.terminal_reported_progress(self)

    @property
    def _is_last_item(self) -> bool:
        return self._status_index.terminal_is_last_item(self)

    def pytest_sessionstart(self, session: Session) -> None:
        self._session = session
        self._session_start = time.perf_counter()

    def pytest_keyboard_interrupt(self, excinfo: ExceptionInfo) -> None:
        if self._session is not None:
            report = cast("ExceptionRepr", self._session.repr_failure(excinfo))
            self._keyboardinterrupt_memo = report
            # SIGINT can arrive while every worker traceback frame is hidden.
            self._keyboardinterrupt_message = (
                excinfo.exconly() if report.reprcrash is None else report.reprcrash.message
            )

    def pytest_unconfigure(self) -> None:
        if self._keyboardinterrupt_memo is not None:
            self._report_keyboardinterrupt()

    def report_interrupt_at_sessionfinish(self) -> None:
        """Write the recorded interrupt once the session finished as interrupted."""
        if self._keyboardinterrupt_memo is not None:
            self._report_keyboardinterrupt()
            self._keyboardinterrupt_memo = None

    def _report_keyboardinterrupt(self) -> None:
        excrepr = self._keyboardinterrupt_memo
        assert excrepr is not None
        msg = self._keyboardinterrupt_message
        self.write_sep("!", msg)
        if "KeyboardInterrupt" in msg:
            if self.config.option.fulltrace:
                excrepr.toterminal(self._tw)
            elif excrepr.reprcrash is not None:
                excrepr.reprcrash.toterminal(self._tw)
                self._tw.line(
                    "(to show a full traceback on KeyboardInterrupt use --full-trace)",
                    yellow=True,
                )

    def pytest_report_header(self, config: Config) -> list[str]:
        result = [f"rootdir: {config.rootpath}"]
        if config.inipath:
            result.append(f"configfile: {os.path.relpath(config.inipath, config.rootpath)}")
        names: list[str] = []
        for _plugin, dist in config.pluginmanager.list_plugin_distinfo():
            name = f"{dist.project_name}-{dist.version}"
            name = name.removeprefix("pytest-")
            if name not in names:
                names.append(name)
        if names:
            result.append(f"plugins: {', '.join(names)}")
        return result

    def report_collect(self, *, final: bool = False) -> None:
        """Write pytest's ``collected N items`` line."""
        if self.verbosity < 0 or not final:
            return
        errors, skipped, deselected = self._status_index.terminal_collection_counts(self)
        session = self._session
        collected = (0 if session is None else len(session.items)) + deselected
        selected = collected - deselected
        line = f"collected {collected} item{'' if collected == 1 else 's'}"
        if errors:
            line += f" / {errors} error{'s' if errors != 1 else ''}"
        if deselected:
            line += f" / {deselected} deselected"
        if skipped:
            line += f" / {skipped} skipped"
        if collected > selected:
            line += f" / {selected} selected"
        if self.isatty():
            self.rewrite(line, bold=True, erase=True)
            self.write("\n")
        else:
            self.write_line(line)

    def collect_only_summary(self, selected: int, deselected: int) -> None:
        """Write pytest's ``--collect-only`` counts through ``summary_stats``."""
        if report := os.environ.get("VFY_DIAGNOSTICS_PLUGIN_REPORT"):
            manager = self.config.pluginmanager
            plugins = sorted(
                (
                    {"name": name, "distribution": dist.project_name, "version": dist.version}
                    for plugin, dist in manager.list_plugin_distinfo()
                    if (name := manager.get_name(plugin)) is not None
                ),
                key=lambda plugin: (plugin["distribution"], plugin["name"], plugin["version"]),
            )
            Path(report).write_text(json.dumps(plugins), encoding="utf-8")
        errors = self._status_index.terminal_bucket_count(self, "error")
        collected = selected + deselected
        if collected == 0:
            text, color = "no tests collected", "yellow"
        elif deselected == 0:
            text, color = f"{collected} test{'' if collected == 1 else 's'} collected", "green"
        elif collected == deselected:
            text, color = f"no tests collected ({deselected} deselected)", "yellow"
        else:
            text = f"{selected}/{collected} tests collected ({deselected} deselected)"
            color = "green"
        parts = [(text, color, False)]
        if errors:
            color = "red"
            parts.append((f"{errors} error{'' if errors == 1 else 's'}", color, False))
        self.summary_stats(parts, color)

    def summary_stats(self, parts: Sequence[tuple[str, str, bool]], main_color: str) -> None:
        """Write pytest's final counts with the session duration."""
        if self.verbosity < -1:
            return
        seconds = time.perf_counter() - self._session_start
        duration = f" in {seconds:.2f}s"
        if seconds >= 60:
            duration += f" ({datetime.timedelta(seconds=int(seconds))})"
        display_sep = self.verbosity >= 0
        fullwidth = self._tw.fullwidth
        rendered = []
        for text, color, bold in parts:
            markup = {color: True, "bold": bold}
            with_markup = cast("str", self._tw.markup(text, **markup))
            fullwidth += len(with_markup) - len(text)
            rendered.append(with_markup)
        message = ", ".join(rendered)
        main_markup = {main_color: True}
        duration_markup = cast("str", self._tw.markup(duration, **main_markup))
        fullwidth += len(duration_markup) - len(duration)
        message += duration_markup
        if display_sep:
            end = cast("str", self._tw.markup("", **main_markup)).removesuffix("\x1b[0m")
            fullwidth += len(end)
            message += end
            self.write_sep("=", message, fullwidth=fullwidth, **main_markup)
        else:
            self.write_line(message, **main_markup)

    @property
    def showlongtestinfo(self) -> bool:
        return self._status_index.terminal_showlongtestinfo(self)

    @property
    def showfspath(self) -> bool:
        return self._status_index.terminal_showfspath(self)

    @showfspath.setter
    def showfspath(self, value: bool | None) -> None:
        self._showfspath = value

    def _determine_show_progress_info(self) -> str | Literal[False]:
        return self._status_index.terminal_progress_information(self)

    pytest_runtest_logstart = _pytest.NativeTerminalLogStartDescriptor()

    @hookimpl(wrapper=True)
    def pytest_runtestloop(self) -> Generator[None, object, object]:
        result = yield
        self._status_index.terminal_finish_loop(self)
        return result

    def _locationline(self, nodeid: str, fspath: str, lineno: int | None, domain: str) -> str:
        return terminal_resources.location_line(self, nodeid, fspath, lineno, domain)

    def write_ensure_prefix(self, prefix: str, extra: object = "", **kwargs: bool) -> None:
        self._status_index.terminal_write_prefix(self, prefix, extra, kwargs)

    def _get_main_color(self) -> tuple[str, list[str]]:
        return self._status_index.terminal_main_color(self)

    def _get_progress_information_message(self) -> str:
        return terminal_resources.progress_message(self)

    @property
    def _width_of_current_line(self) -> int:
        return self._tw.width_of_current_line

    def _write_progress_information_if_past_edge(self) -> None:
        terminal_resources.past_edge(self)

    def _write_progress_information_filling_space(self) -> None:
        terminal_resources.fill_progress(self)

    def write_fspath_result(self, nodeid: str, res: str, **markup: bool) -> None:
        terminal_resources.write_fspath_result(self, nodeid, res, markup)

    def flush(self) -> None:
        self._tw.flush()

    pytest_runtest_logreport = _pytest.NativeTerminalLogReportDescriptor()
    _add_stats = _pytest.NativeTerminalAddStats()

    def getreports(self, name: str) -> list[Any]:
        return self._status_index.terminal_getreports(self, name)

    def _get_reports_to_display(self, key: str) -> list[Any]:
        return self._status_index.terminal_reports_to_display(self, key)

    def _set_main_color(self) -> None:
        self._status_index.terminal_set_main_color(self)

    def _determine_main_color(self, unknown_type_seen: bool) -> str:
        return self._status_index.terminal_determine_main_color(self, unknown_type_seen)

    def wrap_write(
        self,
        content: str,
        *,
        flush: bool = False,
        margin: int = 8,
        line_sep: str = "\n",
        **markup: bool,
    ) -> None:
        width_of_current_line = self._tw.width_of_current_line
        wrapped = line_sep.join(
            self._status_index.terminal_reason_wrap_lines(
                self, content, margin, width_of_current_line
            )
        )
        wrapped = self._status_index.terminal_reason_slice(wrapped, width_of_current_line, None)
        self._tw.write(wrapped, flush=flush, **markup)

    def write(self, content: str, *, flush: bool = False, **markup: bool) -> None:
        self._tw.write(content, flush=flush, **markup)

    def ensure_newline(self) -> None:
        self._status_index.terminal_ensure_newline(self)

    def write_line(self, line: str | bytes, **markup: bool) -> None:
        if not isinstance(line, str):
            line = str(line, errors="replace")
        self.ensure_newline()
        self._tw.line(line, **markup)

    def rewrite(self, line: str, **markup: bool) -> None:
        erase = markup.pop("erase", False)
        fill = " " * (self._tw.fullwidth - len(line) - 1) if erase else ""
        self._tw.write("\r" + str(line) + fill, **markup)

    def write_sep(
        self,
        sep: str,
        title: str | None = None,
        fullwidth: int | None = None,
        **markup: bool,
    ) -> None:
        self.ensure_newline()
        self._tw.sep(sep, title, fullwidth, **markup)

    def isatty(self) -> bool:
        return bool(self._stream.isatty())

    def pytest_collection(self) -> None:
        self._status_index.terminal_collection_start(self)

    def section(self, title: str, sep: str = "=", **markup: bool) -> None:
        # pytest writes the separator without first ensuring a newline.
        self._tw.sep(sep, title, **markup)
