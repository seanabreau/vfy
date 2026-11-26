"""Register Rust coverage callbacks with CPython's ``sys.monitoring`` API."""

from __future__ import annotations

import sys

from .data import CoverageStore
from .errors import ConfigError, DataError
from .native import CollectorCore, coverage_uses_sysmon_branch_arcs
from .plugin import CoveragePlugin


class MonitoringCollector:
    """Collect lines and arcs with the interpreter monitoring API.

    Rust callbacks accumulate measurements in memory. Stopping returns a
    snapshot; the caller writes it to disk.
    """

    def __init__(
        self,
        *,
        branch: bool = False,
        include: list[str] | None = None,
        omit: list[str] | None = None,
        source: list[str] | None = None,
        source_pkgs: list[str] | None = None,
        context: str = "",
        plugins: list[CoveragePlugin] | None = None,
        dynamic_contexts: list[CoveragePlugin] | None = None,
        cover_pylib: bool = False,
        relative_files: bool = False,
        decisions: bool = False,
        dynamic_context: str = "",
        core: str | None = None,
    ) -> None:
        self.branch = branch
        self.decisions = decisions
        self.context = context
        self._started = False
        try:
            sysmon_arcs = coverage_uses_sysmon_branch_arcs(core)
        except ValueError as error:
            raise ConfigError(str(error)) from error
        # File tracers report virtual line ranges, which direct bytecode
        # branch edges cannot map. Their arcs follow the line-derived path.
        switches_contexts = bool(dynamic_contexts) or dynamic_context not in {"", "none"}
        self._switches_contexts = switches_contexts
        # Coverage.py selects its line-derived core for dynamic contexts,
        # including on interpreters whose default core uses direct arcs.
        self._sysmon_arcs = branch and not plugins and not switches_contexts and sysmon_arcs
        events = sys.monitoring.events
        # CPython 3.14 can disable each branch direction after its first hit.
        # Branch collection still needs frame callbacks and self-arcs.
        self._sysmon_event_disable = (
            self._sysmon_arcs
            and not plugins
            and not switches_contexts
            and not decisions
            and hasattr(events, "BRANCH_LEFT")
            and hasattr(events, "BRANCH_RIGHT")
        )
        self._tool_id = sys.monitoring.COVERAGE_ID
        self._local_line_events = not branch and not plugins and not switches_contexts
        self._local_branch_events = self._sysmon_event_disable
        self._local_legacy_branch_events = (
            branch
            and not self._sysmon_arcs
            and not plugins
            and not switches_contexts
            and not decisions
        )
        self._instrument_events = 0
        self._local_instrument_events = 0
        self._core = CollectorCore(
            branch,
            source or [],
            __file__,
            include or [],
            omit or [],
            context,
            plugins or [],
            dynamic_contexts or [],
            cover_pylib,
            self._sysmon_arcs,
            relative_files,
            decisions,
            self._sysmon_event_disable,
            source_pkgs or [],
            dynamic_context,
        )

    def switch_context(self, context: str | None) -> None:
        self.context = context or ""
        self._core.switch_context(context)
        # Retired locations must fire again so the new context records its own
        # complete evidence.
        if not self._core.reactivate_retired_locations():
            sys.monitoring.restart_events()

    def start(self) -> None:
        if self._started:
            return
        monitoring = sys.monitoring
        if monitoring.get_tool(self._tool_id) is not None:
            raise DataError(
                f"sys.monitoring coverage tool id {self._tool_id} is already in use by "
                f"{monitoring.get_tool(self._tool_id)!r}"
            )
        monitoring.use_tool_id(self._tool_id, "vfy")
        self._started = True
        try:
            self._register_events()
        except BaseException:
            # Release the tool even when callback registration is interrupted.
            self.disable()
            raise

    def _register_context_frame_events(self) -> int:
        """Line collection has no frame events; dynamic contexts need them.

        A dynamic context starts in a frame and ends when it returns or yields.
        Branch collection already receives these events.
        """
        if not self._switches_contexts or self.branch:
            return 0
        monitoring = sys.monitoring
        events = monitoring.events
        enter = self._core.callback_context_enter
        leave = self._core.callback_context_leave
        event_set = 0
        for event, callback in (
            (events.PY_START, enter),
            (events.PY_RESUME, enter),
            (events.PY_THROW, enter),
            (events.PY_RETURN, leave),
            (events.PY_YIELD, leave),
            (events.PY_UNWIND, leave),
        ):
            monitoring.register_callback(self._tool_id, event, callback)
            event_set |= event
        return event_set

    def _register_events(self) -> None:
        monitoring = sys.monitoring
        events = monitoring.events
        if sys.version_info >= (3, 14):
            legacy_branch = events.BRANCH_LEFT | events.BRANCH_RIGHT
        else:
            legacy_branch = events.BRANCH
        monitoring.register_callback(self._tool_id, events.LINE, self._core.callback_line)
        monitoring.register_callback(
            self._tool_id, events.INSTRUCTION, self._core.callback_instruction
        )
        event_set = (
            events.NO_EVENTS
            if self._local_line_events
            or self._local_branch_events
            or self._local_legacy_branch_events
            else events.LINE
        )
        if self._local_line_events or self._local_branch_events:
            monitoring.register_callback(self._tool_id, events.PY_START, self._core.callback_start)
            event_set = events.PY_START
            self._local_instrument_events = events.LINE
        event_set |= self._configure_legacy_branch_local_events()
        event_set |= self._register_context_frame_events()
        if self.branch:
            monitoring.register_callback(self._tool_id, events.PY_START, self._core.callback_start)
            monitoring.register_callback(
                self._tool_id, events.PY_RESUME, self._core.callback_resume
            )
            monitoring.register_callback(self._tool_id, events.PY_RETURN, self._core.callback_end)
            monitoring.register_callback(
                self._tool_id, events.PY_YIELD, self._core.callback_suspend
            )
            monitoring.register_callback(self._tool_id, events.PY_THROW, self._core.callback_throw)
            if self._local_legacy_branch_events:
                # A generator can be suspended before collection starts and
                # won't produce PY_START when it resumes. Keep PY_RESUME
                # global to install local events on that frame before it runs.
                event_set |= events.PY_START | events.PY_RESUME | events.PY_THROW
            else:
                event_set |= events.PY_START | events.PY_RESUME | events.PY_RETURN | events.PY_YIELD
                event_set |= events.PY_THROW
            if self.decisions:
                monitoring.register_callback(self._tool_id, events.RAISE, self._core.callback_raise)
                monitoring.register_callback(
                    self._tool_id, events.RERAISE, self._core.callback_raise
                )
                monitoring.register_callback(
                    self._tool_id, events.PY_UNWIND, self._core.callback_unwind
                )
                event_set |= events.RAISE | events.RERAISE | events.PY_UNWIND
            elif not self._sysmon_arcs:
                monitoring.register_callback(
                    self._tool_id, events.PY_UNWIND, self._core.callback_end
                )
                event_set |= events.PY_UNWIND
            if self._sysmon_arcs:
                branch_events = [
                    getattr(events, name)
                    for name in ("BRANCH_LEFT", "BRANCH_RIGHT")
                    if hasattr(events, name)
                ]
                if not branch_events:
                    branch_events = [legacy_branch]
                if hasattr(events, "BRANCH_LEFT") and hasattr(events, "BRANCH_RIGHT"):
                    monitoring.register_callback(
                        self._tool_id,
                        getattr(events, "BRANCH_LEFT"),  # noqa: B009 - runtime-gated monitoring event
                        self._core.callback_branch_left,
                    )
                    monitoring.register_callback(
                        self._tool_id,
                        getattr(events, "BRANCH_RIGHT"),  # noqa: B009 - runtime-gated monitoring event
                        self._core.callback_branch_right,
                    )
                else:
                    monitoring.register_callback(
                        self._tool_id, legacy_branch, self._core.callback_branch
                    )
                for event in branch_events:
                    if self._local_branch_events:
                        self._local_instrument_events |= event
                    else:
                        event_set |= event
            else:
                event_set |= self._register_line_arc_events(legacy_branch)
        self._instrument_events = event_set
        # A previous collector can leave PY_START or LINE locations disabled
        # on surviving code objects. A new run needs to observe them again.
        monitoring.restart_events()
        monitoring.set_events(self._tool_id, event_set)
        if self._local_legacy_branch_events:
            self._instrument_current_frames()

    def _register_line_arc_events(self, legacy_branch: int) -> int:
        """Register the events that complete arcs derived from LINE events.

        LINE events omit repeated execution on one source line. A backward
        JUMP restores that self-arc. CPython 3.12+ branch instructions only
        jump forward, so branch events are needed only for decision outcomes.
        Returns the events to enable globally.
        """
        monitoring = sys.monitoring
        events = monitoring.events
        monitoring.register_callback(self._tool_id, events.JUMP, self._core.callback_jump)
        line_arc_events = events.JUMP
        if self.decisions:
            for event in (
                (events.BRANCH_LEFT, events.BRANCH_RIGHT)
                if sys.version_info >= (3, 14)
                else (legacy_branch,)
            ):
                monitoring.register_callback(self._tool_id, event, self._core.callback_branch)
            line_arc_events |= legacy_branch
        if self._local_legacy_branch_events:
            self._local_instrument_events |= line_arc_events
            return events.NO_EVENTS
        return line_arc_events

    def _configure_legacy_branch_local_events(self) -> int:
        events = sys.monitoring.events
        if not self._local_legacy_branch_events:
            return events.NO_EVENTS
        self._local_instrument_events = (
            events.LINE | events.PY_RESUME | events.PY_RETURN | events.PY_YIELD | events.JUMP
        )
        return events.PY_START

    def _instrument_current_frames(self) -> None:
        """Install local branch events on measured frames already running."""
        frames = sys._current_frames()
        instrumented: set[int] = set()
        frame = None
        try:
            for active in frames.values():
                frame = active
                while frame is not None:
                    code = frame.f_code
                    code_id = id(code)
                    if code_id not in instrumented:
                        # These frames can belong to another thread's stack.
                        self._core.instrument_existing_frame(code, frame.f_globals)
                        instrumented.add(code_id)
                    frame = frame.f_back
        finally:
            del frame, frames, instrumented

    def restart_after_fork(self) -> None:
        """Re-enable local events on frames that survived ``fork()``.

        CPython preserves disabled code-object events in the child. Restore
        masks on the live stack, including the frame executing ``fork``;
        PY_START initializes newly entered frames.
        """
        if not self._started:
            return
        frame = sys._getframe()
        while frame is not None:
            try:
                if self._local_legacy_branch_events:
                    self._core.instrument_existing_frame(frame.f_code, frame.f_globals)
                else:
                    local_events = self._instrument_events
                    # The frame containing fork will not receive another PY_START.
                    if self._local_instrument_events:
                        local_events = self._local_instrument_events
                    else:
                        # set_local_events() rejects these global-only events.
                        for name in ("PY_THROW", "PY_UNWIND", "RAISE", "RERAISE"):
                            local_events &= ~getattr(sys.monitoring.events, name, 0)
                    self._core.set_local_events(frame.f_code, local_events)
            except (AttributeError, RuntimeError):
                # A frame can disappear while the at-fork callback walks the
                # stack. The collector remains valid for subsequent frames.
                pass
            frame = frame.f_back
        if hasattr(sys.monitoring, "restart_events"):
            sys.monitoring.restart_events()

    def stop(self) -> CoverageStore:
        self.disable()
        # A stopped collector is discarded, so its evidence moves instead of copying.
        return CoverageStore(rust_data=self._core.take_coverage_data(discover_sources=True))

    def disable(self) -> None:
        """Detach callbacks without copying inherited collection data."""
        if self._started:
            monitoring = sys.monitoring
            monitoring.set_events(self._tool_id, monitoring.events.NO_EVENTS)
            for event in (
                monitoring.events.LINE,
                monitoring.events.INSTRUCTION,
                monitoring.events.PY_START,
                monitoring.events.PY_RESUME,
                monitoring.events.PY_RETURN,
                monitoring.events.PY_YIELD,
                monitoring.events.PY_THROW,
                monitoring.events.PY_UNWIND,
                monitoring.events.RAISE,
                monitoring.events.RERAISE,
                monitoring.events.JUMP,
                getattr(monitoring.events, "BRANCH", 0),
                getattr(monitoring.events, "BRANCH_LEFT", 0),
                getattr(monitoring.events, "BRANCH_RIGHT", 0),
            ):
                if event:
                    monitoring.register_callback(self._tool_id, event, None)
            self._core.reset_local_instrumentation()
            monitoring.free_tool_id(self._tool_id)
            self._started = False

    def snapshot(self, *, discover_sources: bool = False) -> CoverageStore:
        """Return an isolated view of data collected so far without stopping."""
        return CoverageStore(rust_data=self._core.coverage_data(discover_sources))

    def flush(self, destination: CoverageStore, *, discover_sources: bool = False) -> CoverageStore:
        """Move completed measurements into ``destination``, keeping live frame state."""
        try:
            data = CoverageStore(
                rust_data=self._core.flush_coverage_data(destination.to_rust(), discover_sources)
            )
        except ValueError as exc:
            raise DataError(str(exc)) from exc
        sys.monitoring.restart_events()
        return data

    def unmatched_source_packages(self) -> list[str]:
        """Return requested source packages that no traced module has matched."""
        return self._core.unmatched_source_packages()

    def reset(self) -> None:
        """Discard buffered measurements and let subsequent events be observed."""
        self._core.reset_coverage_data()
        sys.monitoring.restart_events()
