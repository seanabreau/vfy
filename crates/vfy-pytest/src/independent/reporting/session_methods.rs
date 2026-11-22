//! Python-visible methods of the session report history.

use super::rows::ReportEntry;
use super::{NativeIndependentReport, NativeSessionReports, copies, terminal, terminal_stats};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3::{PyTraverseError, PyVisit};

#[pymethods]
impl NativeSessionReports {
    #[getter]
    fn __signature__(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        Ok(slf
            .py()
            .import("inspect")?
            .call_method1("signature", (slf.getattr("terminal_record")?,))?
            .unbind())
    }

    /// Registered as a configuration cleanup; releases payloads kept by issued copies.
    fn _release_copies(slf: &Bound<'_, Self>) {
        copies::release(slf);
    }

    pub(super) fn reports(slf: &Bound<'_, Self>) -> PyResult<Py<PyList>> {
        Ok(PyList::new(slf.py(), Self::published_reports(slf)?)?.unbind())
    }

    fn terminal_live_stats(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        terminal::public_stats(slf)
    }

    fn terminal_replace_stats(slf: &Bound<'_, Self>, stats: &Bound<'_, PyAny>) {
        terminal::replace_stats(slf, stats);
    }

    fn terminal_bucket_count(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        category: &str,
    ) -> PyResult<usize> {
        terminal_stats::bucket_count(reporter, category)
    }

    fn terminal_collection_counts(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<(usize, usize, usize)> {
        terminal_stats::collection_counts(reporter)
    }

    fn hypothesis_summary(
        slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        namespace: &Bound<'_, PyDict>,
        print_statistics: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyList>> {
        crate::independent::hypothesis_report::summary(slf, reporter, namespace, print_statistics)
    }

    fn terminal_reports(slf: &Bound<'_, Self>) -> PyResult<Py<PyList>> {
        let py = slf.py();
        let stats = Self::terminal_live_stats(slf)?;
        Ok(PyList::new(py, terminal::reports(stats.bind(py))?)?.unbind())
    }

    fn terminal_is_publication_owner(&self) -> bool {
        self.summary.is_publication_owner()
    }

    fn terminal_stats(slf: &Bound<'_, Self>) -> PyResult<Py<PyDict>> {
        let py = slf.py();
        let stats = Self::terminal_live_stats(slf)?;
        let handles = terminal::snapshot_handles(stats.bind(py))?;
        terminal::snapshot(py, handles)
    }

    fn terminal_add_stats(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        category: &Bound<'_, PyAny>,
        items: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal_stats::add_stats(reporter, category, items)
    }
    fn terminal_project_result(
        slf: &Bound<'_, Self>,
        result: &Bound<'_, crate::results::NativeRunResult>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        slf.borrow_mut().summary.consume_projection()?;
        terminal::projection::result(result, reporter)
    }

    fn terminal_summary_before(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal::session::before(reporter)
    }

    fn terminal_summary_after(_slf: &Bound<'_, Self>, reporter: &Bound<'_, PyAny>) -> PyResult<()> {
        terminal::session::after(reporter)
    }

    fn warning_scope(_slf: &Bound<'_, Self>, config: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        crate::independent::capture::warnings::scope(config)
    }

    fn terminal_reportchars(_slf: &Bound<'_, Self>, config: &Bound<'_, PyAny>) -> PyResult<String> {
        terminal::summary::reportchars(config)
    }

    fn terminal_short_summary(_slf: &Bound<'_, Self>, reporter: &Bound<'_, PyAny>) -> PyResult<()> {
        terminal::summary::short_summary(reporter)
    }

    fn terminal_getreports(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        category: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyList>> {
        terminal_stats::reports(
            reporter,
            category,
            terminal_stats::ReportFilter::DebuggerHidden,
        )
    }
    fn terminal_reports_to_display(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        category: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyList>> {
        terminal_stats::reports(
            reporter,
            category,
            terminal_stats::ReportFilter::SummaryCounted,
        )
    }
    fn terminal_set_main_color(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal_stats::set_main_color(reporter)
    }
    fn terminal_determine_main_color(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        unknown: &Bound<'_, PyAny>,
    ) -> PyResult<&'static str> {
        terminal_stats::determine_main_color(reporter, unknown)
    }
    fn terminal_is_last_item(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Ok(terminal_stats::is_last_item(reporter)?.unbind())
    }
    fn terminal_verbosity(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Ok(terminal_stats::verbosity(reporter)?.unbind())
    }
    fn terminal_reported_progress(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<usize> {
        terminal_stats::reported_progress(reporter)
    }

    fn terminal_record(
        slf: &Bound<'_, Self>,
        report: &Bound<'_, NativeIndependentReport>,
    ) -> PyResult<()> {
        let session = slf
            .borrow()
            .session
            .as_ref()
            .map(|session| session.clone_ref(slf.py()));
        let Some(session) = session else {
            return Ok(());
        };
        let reporter = session
            .bind(slf.py())
            .getattr("config")?
            .getattr("pluginmanager")?
            .call_method1("get_plugin", ("terminalreporter",))?;
        if reporter.is_none() {
            return Err(crate::UnsupportedFeatureError::new_err(
                "terminal statistics require an active terminal reporter",
            ));
        }
        Self::terminal_consume(slf, report, &reporter)
    }

    fn terminal_consume(
        slf: &Bound<'_, Self>,
        report: &Bound<'_, NativeIndependentReport>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal::record(slf, report, reporter)
    }

    fn terminal_status_result(
        _slf: &Bound<'_, Self>,
        report: &Bound<'_, NativeIndependentReport>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::live::status(report.as_any(), reporter)
    }

    fn terminal_status_word(
        _slf: &Bound<'_, Self>,
        word: &Bound<'_, PyAny>,
    ) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        terminal::live::word(word)
    }

    fn terminal_status_emit(
        _slf: &Bound<'_, Self>,
        report: &Bound<'_, NativeIndependentReport>,
        reporter: &Bound<'_, PyAny>,
        category: &Bound<'_, PyAny>,
        letter: &Bound<'_, PyAny>,
        word: &Bound<'_, PyAny>,
        markup: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let Some(markup) =
            terminal::live::prepare(report.as_any(), reporter, category, letter, word, markup)?
        else {
            return Ok(());
        };
        terminal::live::emit(
            report.as_any(),
            reporter,
            letter,
            word,
            markup.bind(report.py()),
        )
    }

    fn terminal_progress_information(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::progress::information(reporter)
    }

    fn terminal_main_color(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<(Py<PyAny>, Py<PyAny>)> {
        terminal::progress::main_color(reporter)
    }

    fn terminal_progress_collected(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::progress::collected(reporter)
    }

    fn terminal_progress_is_count(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        terminal::progress::is_count(reporter)
    }

    fn terminal_progress_message_kind(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        collected: &Bound<'_, PyAny>,
    ) -> PyResult<&'static str> {
        terminal::progress::message_kind(reporter, collected)
    }

    fn terminal_progress_message(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        collected: &Bound<'_, PyAny>,
        kind: &str,
        progress: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::progress::message(reporter, collected, kind, progress)
    }

    fn terminal_progress_count_length(
        _slf: &Bound<'_, Self>,
        collected: &Bound<'_, PyAny>,
    ) -> PyResult<usize> {
        terminal::progress::count_length(collected)
    }

    fn terminal_progress_edge_length(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<usize> {
        terminal::progress::edge_length(reporter)
    }

    fn terminal_progress_past_edge(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        width: &Bound<'_, PyAny>,
        length: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::progress::past_edge(reporter, width, length)
    }

    fn terminal_progress_fill(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        width: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::progress::fill(reporter, width)
    }

    fn terminal_progress_rjust(
        _slf: &Bound<'_, Self>,
        message: &Bound<'_, PyAny>,
        fill: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Ok(message.call_method1("rjust", (fill,))?.unbind())
    }

    fn terminal_progress_newline(
        _slf: &Bound<'_, Self>,
        message: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        Ok(message.add("\n")?.unbind())
    }

    fn terminal_progress_markup(
        _slf: &Bound<'_, Self>,
        color: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyDict>> {
        terminal::progress::markup(color)
    }

    fn terminal_finish_loop(_slf: &Bound<'_, Self>, reporter: &Bound<'_, PyAny>) -> PyResult<()> {
        terminal::live::finish_loop(reporter)
    }

    fn terminal_collection_start(
        slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if slf.borrow().worker {
            return Ok(());
        }
        terminal::collection::start(reporter)
    }

    fn terminal_logstart(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        nodeid: &Bound<'_, PyAny>,
        location: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal::live::logstart(reporter, nodeid, location)
    }

    fn terminal_location_has_path(
        _slf: &Bound<'_, Self>,
        path: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        terminal::verbose::has_path(path)
    }

    fn terminal_location_domain_matches(
        _slf: &Bound<'_, Self>,
        line: &Bound<'_, PyAny>,
        domain: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        terminal::verbose::domain_matches(line, domain)
    }

    fn terminal_location_trim_domain(
        _slf: &Bound<'_, Self>,
        line: &Bound<'_, PyAny>,
        domain: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::verbose::trim_domain(line, domain)
    }

    fn terminal_location_domain_value(
        _slf: &Bound<'_, Self>,
        values: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::verbose::domain_value(values)
    }

    fn terminal_location_append_domain(
        _slf: &Bound<'_, Self>,
        line: &Bound<'_, PyAny>,
        values: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::verbose::append_domain(line, values)
    }

    fn terminal_location_needs_origin(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        nodeid: &Bound<'_, PyAny>,
        fspath: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        terminal::verbose::needs_origin(reporter, nodeid, fspath)
    }

    fn terminal_location_append_origin(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        line: &Bound<'_, PyAny>,
        fspath: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::verbose::append_origin(reporter, line, fspath)
    }

    fn terminal_location_finish(
        _slf: &Bound<'_, Self>,
        line: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::verbose::finish(line)
    }

    fn terminal_ensure_newline(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal::verbose::ensure_newline(reporter)
    }

    fn terminal_write_prefix(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        prefix: &Bound<'_, PyAny>,
        extra: &Bound<'_, PyAny>,
        markup: &Bound<'_, PyDict>,
    ) -> PyResult<()> {
        terminal::verbose::write_prefix(reporter, prefix, extra, markup)
    }

    fn terminal_verbose_running_xdist(
        _slf: &Bound<'_, Self>,
        report: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        terminal::verbose::running_xdist(report)
    }

    fn terminal_reason_slice(
        _slf: &Bound<'_, Self>,
        value: &Bound<'_, PyAny>,
        start: &Bound<'_, PyAny>,
        stop: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::reason::slice(value, start, stop)
    }

    fn terminal_reason_wrap_lines(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        content: &Bound<'_, PyAny>,
        margin: &Bound<'_, PyAny>,
        current_width: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::wrap::lines(reporter, content, margin, current_width)
    }

    fn terminal_verbose_after_prefix(
        _slf: &Bound<'_, Self>,
        report: &Bound<'_, PyAny>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal::verbose::after_prefix(report, reporter)
    }

    fn terminal_location_missing(slf: &Bound<'_, Self>) -> Py<PyAny> {
        terminal::verbose::missing(slf.py())
    }

    fn terminal_fspath(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        nodeid: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        terminal::progress::fspath(reporter, nodeid)
    }

    fn terminal_path_prefix(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
        path: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        terminal::progress::path_prefix(reporter, path)
    }

    fn terminal_showlongtestinfo(
        _slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        terminal::live::verbosity(reporter)?.gt(0)
    }

    fn terminal_showfspath(
        slf: &Bound<'_, Self>,
        reporter: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        if reporter.getattr("_showfspath")?.is_none() {
            Ok(terminal::live::verbosity(reporter)?
                .ge(0)?
                .into_pyobject(py)?
                .to_owned()
                .into_any()
                .unbind())
        } else {
            Ok(reporter.getattr("_showfspath")?.unbind())
        }
    }

    fn __call__(slf: &Bound<'_, Self>, report: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        let session = slf
            .borrow()
            .session
            .as_ref()
            .map(|session| session.clone_ref(py));
        let Some(session) = session else {
            return Ok(());
        };
        let native_report = report.cast::<NativeIndependentReport>()?;
        // A replay of a prior session's report cannot stand in for this session's phases.
        let generation = slf.borrow().generation();
        if native_report
            .borrow()
            .origin
            .as_ref()
            .is_some_and(|origin| origin.generation() != generation)
        {
            return Err(crate::UnsupportedFeatureError::new_err(
                "report belongs to a different or released native invocation",
            ));
        }
        let id = Self::adopt_published(slf, native_report);
        {
            let mut history = slf.borrow_mut();
            history.retain_session(id);
            history.list(id);
        }
        if report.getattr(pyo3::intern!(py, "failed"))?.is_truthy()?
            && crate::module_cache::optional_attr(report, pyo3::intern!(py, "wasxfail"))?.is_none()
        {
            let session = session.bind(py);
            let failures = session
                .getattr(pyo3::intern!(py, "testsfailed"))?
                .extract::<usize>()?
                + 1;
            session.setattr(pyo3::intern!(py, "testsfailed"), failures)?;
            let maxfail = session
                .getattr(pyo3::intern!(py, "config"))?
                .getattr(pyo3::intern!(py, "option"))?
                .getattr(pyo3::intern!(py, "maxfail"))?;
            if maxfail.is_truthy()? && failures.into_pyobject(py)?.ge(&maxfail)? {
                session.setattr(
                    pyo3::intern!(py, "shouldfail"),
                    format!("stopping after {failures} failures"),
                )?;
            }
        }
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.session)?;
        visit.call(&self.registry)?;
        for slot in &self.rows {
            match &slot.entry {
                Some(ReportEntry::Native(report)) => report.traverse(&visit)?,
                Some(ReportEntry::Published(report)) => visit.call(report)?,
                None => {}
            }
        }
        self.terminal.traverse(&visit)?;
        self.summary.traverse(&visit)?;
        Ok(())
    }

    pub(super) fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut state = slf.borrow_mut();
            state.release_delivery_owners();
            (
                state.session.take(),
                state.registry.take(),
                std::mem::take(&mut state.reports),
                std::mem::take(&mut state.rows),
                std::mem::take(&mut state.owners),
                state.terminal.take(),
                state.summary.clear(),
            )
        };
        drop(removed);
    }
}
