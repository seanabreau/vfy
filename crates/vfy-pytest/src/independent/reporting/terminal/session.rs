//! Publish session results to the terminal.

use super::super::NativeSessionReports;
use crate::independent::capture::NativeWarningRecorder;
use crate::results::NativeRunResult;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit};
use std::collections::BTreeMap;
use vfy_core::test_execution::WarningRecord;

#[derive(Clone, Copy)]
pub(in crate::independent) enum SummaryOrigin {
    Serial,
    WorkerMerge { warning_split: usize },
}

enum Binding {
    Unbound,
    Active {
        result: Py<NativeRunResult>,
        origin: SummaryOrigin,
    },
    Closed,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Projection {
    Pending,
    Consumed,
}

pub(in crate::independent) struct SummarySession {
    binding: Binding,
    projection: Projection,
    warning_cursor: Option<usize>,
    terminal_warning_start: Option<usize>,
    warnings: Option<Py<NativeWarningRecorder>>,
}

impl SummarySession {
    pub(in crate::independent::reporting) fn is_publication_owner(&self) -> bool {
        matches!(self.binding, Binding::Active { .. })
    }

    pub(in crate::independent::reporting) fn new() -> Self {
        Self {
            binding: Binding::Unbound,
            projection: Projection::Pending,
            warning_cursor: None,
            terminal_warning_start: None,
            warnings: None,
        }
    }

    pub(in crate::independent) fn attach_warnings(
        &mut self,
        py: Python<'_>,
        warnings: &Py<NativeWarningRecorder>,
    ) -> PyResult<()> {
        if self.warnings.is_some() {
            return Err(PyRuntimeError::new_err(
                "terminal warning recorder is already attached",
            ));
        }
        self.warnings = Some(warnings.clone_ref(py));
        Ok(())
    }

    pub(in crate::independent) fn project_warnings(
        &self,
        result: &NativeRunResult,
        warnings: &[WarningRecord],
        split: usize,
    ) -> PyResult<Vec<WarningRecord>> {
        let (before, after) = warnings.split_at_checked(split).ok_or_else(|| {
            PyRuntimeError::new_err("terminal warning history was reset during session finish")
        })?;
        let terminal_start = self.terminal_warning_start.unwrap_or(warnings.len());
        let finish_len = terminal_start.checked_sub(split).ok_or_else(|| {
            PyRuntimeError::new_err("terminal warning history precedes worker completion")
        })?;
        let (finish, terminal) = after.split_at_checked(finish_len).ok_or_else(|| {
            PyRuntimeError::new_err("terminal warning history was reset during summary")
        })?;
        let mut records = result.merged_worker_warnings(before, finish);
        // Controller terminal hooks run after the workers' completed session projection.
        // Their warnings are new occurrences even when the worker emitted the same row.
        records.extend_from_slice(terminal);
        Ok(records)
    }

    pub(in crate::independent::reporting) fn bind(
        &mut self,
        py: Python<'_>,
        result: &Py<NativeRunResult>,
        origin: SummaryOrigin,
    ) -> PyResult<()> {
        if !matches!(self.binding, Binding::Unbound) {
            return Err(PyRuntimeError::new_err(
                "terminal session result is already bound or closed",
            ));
        }
        self.binding = Binding::Active {
            result: result.clone_ref(py),
            origin,
        };
        Ok(())
    }

    pub(in crate::independent::reporting) fn result(
        &self,
        py: Python<'_>,
    ) -> Option<Py<NativeRunResult>> {
        match &self.binding {
            Binding::Active { result, .. } => Some(result.clone_ref(py)),
            Binding::Unbound | Binding::Closed => None,
        }
    }

    pub(in crate::independent::reporting) fn consume_projection(&mut self) -> PyResult<()> {
        if self.projection == Projection::Consumed {
            return Err(PyRuntimeError::new_err(
                "terminal worker result is already projected",
            ));
        }
        self.projection = Projection::Consumed;
        Ok(())
    }

    pub(in crate::independent::reporting) fn close(&mut self) -> Option<Py<NativeRunResult>> {
        match std::mem::replace(&mut self.binding, Binding::Closed) {
            Binding::Active { result, .. } => Some(result),
            Binding::Unbound | Binding::Closed => None,
        }
    }

    pub(in crate::independent::reporting) fn clear(
        &mut self,
    ) -> (
        Option<Py<NativeRunResult>>,
        Option<Py<NativeWarningRecorder>>,
    ) {
        (self.close(), self.warnings.take())
    }

    pub(in crate::independent::reporting) fn traverse(
        &self,
        visit: &PyVisit<'_>,
    ) -> Result<(), PyTraverseError> {
        if let Binding::Active { result, .. } = &self.binding {
            visit.call(result)?;
        }
        visit.call(&self.warnings)?;
        Ok(())
    }
}

fn history<'py>(reporter: &Bound<'py, PyAny>) -> PyResult<Bound<'py, NativeSessionReports>> {
    Ok(reporter.getattr("_status_index")?.cast_into()?)
}

fn result(reporter: &Bound<'_, PyAny>) -> PyResult<Option<Py<NativeRunResult>>> {
    Ok(history(reporter)?.borrow().summary.result(reporter.py()))
}

pub(in crate::independent) fn begin_summary(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    let history = history(reporter)?;
    let mut owner = history.borrow_mut();
    let recorder = owner
        .summary
        .warnings
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("terminal warning recorder is not attached"))?;
    let start = recorder.borrow(reporter.py()).record_count();
    owner.summary.terminal_warning_start = Some(start);
    Ok(())
}

pub(in crate::independent) fn has_result(reporter: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(history(reporter)?.borrow().summary.is_publication_owner())
}

pub(in crate::independent) fn has_parallel_progress(reporter: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(history(reporter)?.borrow().terminal.parallel_progress())
}

fn warnings(reporter: &Bound<'_, PyAny>) -> PyResult<Vec<WarningRecord>> {
    let py = reporter.py();
    let history = history(reporter)?;
    let (result, origin, warnings) = {
        let history = history.borrow();
        let summary = &history.summary;
        let Binding::Active { result, origin } = &summary.binding else {
            return Ok(Vec::new());
        };
        let warnings = summary
            .warnings
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("terminal warning recorder is not attached"))?;
        (result.clone_ref(py), *origin, warnings.clone_ref(py))
    };
    let warnings = warnings.borrow(py).snapshot();
    match origin {
        SummaryOrigin::Serial => Ok(result.borrow(py).merged_worker_warnings(&warnings, &[])),
        SummaryOrigin::WorkerMerge { warning_split } => {
            history
                .borrow()
                .summary
                .project_warnings(&result.borrow(py), &warnings, warning_split)
        }
    }
}

fn refresh(reporter: &Bound<'_, PyAny>) -> PyResult<Option<Py<NativeRunResult>>> {
    let result = result(reporter)?;
    if let Some(result) = &result {
        super::super::materialize_reports(reporter.py(), &reporter.getattr("_session")?, result)?;
    }
    Ok(result)
}

pub(in crate::independent::reporting) fn before(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    let Some(result) = refresh(reporter)? else {
        return Ok(());
    };
    let writer = reporter.getattr("_tw")?;
    let style = reporter
        .getattr("config")?
        .call_method1("getoption", ("tbstyle", "auto"))?
        .extract::<String>()?;
    for section in result
        .bind(reporter.py())
        .call_method1("failure_sections", ("all",))?
        .try_iter()?
    {
        let section = section?;
        if style == "no" {
            break;
        }
        let title = section.get_item(0)?.extract::<String>()?;
        reporter.call_method1("write_sep", ("=", &title))?;
        for failure in section.get_item(1)?.try_iter()? {
            let failure = failure?;
            // A representation whose text ends its own last line, such as a
            // native traceback, is written by pytest without an extra newline.
            let text = failure.get_item(1)?.str()?.extract::<String>()?;
            let text = text.strip_suffix('\n').unwrap_or(&text);
            if style == "line" && title == "FAILURES" {
                // pytest omits the headline and captured output in this style.
                writer.call_method1("line", (text,))?;
                continue;
            }
            reporter.call_method1("write_sep", ("_", failure.get_item(0)?))?;
            writer.call_method1("line", (text,))?;
            for capture in failure.get_item(2)?.try_iter()? {
                let capture = capture?;
                writer.call_method1("sep", ("-", capture.get_item(0)?))?;
                writer.call_method1("line", (capture.get_item(1)?,))?;
            }
        }
    }
    warning_summary(reporter)?;
    super::summary::capture_summary(reporter)
}

pub(in crate::independent::reporting) fn after(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    let Some(result) = result(reporter)? else {
        return Ok(());
    };
    durations(reporter, &result)?;
    super::summary::short_summary(reporter)?;
    warning_summary(reporter)
}

/// Write pytest's `--durations` section before the short test summary.
fn durations(reporter: &Bound<'_, PyAny>, result: &Py<NativeRunResult>) -> PyResult<()> {
    let config = reporter.getattr("config")?;
    let options = PyDict::new(reporter.py());
    for name in ["durations", "durations_min"] {
        options.set_item(
            name,
            config.call_method1("getoption", (name, reporter.py().None()))?,
        )?;
    }
    options.set_item("verbosity", config.call_method0("get_verbosity")?)?;
    let summary = {
        let result = result.borrow(reporter.py());
        crate::reporting::terminal_summary(result.record(), options.as_any())?
    };
    let Some(heading) = summary.duration_heading else {
        return Ok(());
    };
    reporter.call_method1("write_sep", ("=", heading))?;
    for line in summary.duration_lines {
        reporter.call_method1("write_line", (line,))?;
    }
    Ok(())
}

fn warning_summary(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    if !reporter
        .getattr("reportchars")?
        .extract::<String>()?
        .contains('w')
    {
        return Ok(());
    }
    let records = warnings(reporter)?;
    if records.is_empty() {
        return Ok(());
    }
    let history = history(reporter)?;
    let (start, final_pass) = {
        let mut owner = history.borrow_mut();
        let previous = owner.summary.warning_cursor.replace(records.len());
        (previous.unwrap_or(0), previous.is_some())
    };
    let remaining = records.get(start..).ok_or_else(|| {
        PyRuntimeError::new_err("terminal warning history was reset during summary")
    })?;
    if remaining.is_empty() {
        return Ok(());
    }
    let mut groups: Vec<(&str, Vec<&WarningRecord>)> = Vec::new();
    let mut indexes = BTreeMap::new();
    for record in remaining {
        let next = groups.len();
        let index = *indexes.entry(record.message.as_str()).or_insert(next);
        if index == groups.len() {
            groups.push((&record.message, Vec::new()));
        }
        groups[index].1.push(record);
    }
    let title = if final_pass {
        "warnings summary (final)"
    } else {
        "warnings summary"
    };
    reporter.call_method1("write_sep", ("=", title))?;
    let writer = reporter.getattr("_tw")?;
    for (message, records) in groups {
        writer.call_method1("line", (warning_locations(reporter, &records)?,))?;
        let lines = PyString::new(reporter.py(), message)
            .call_method0("splitlines")?
            .extract::<Vec<String>>()?;
        let indented = lines
            .iter()
            .map(|line| format!("  {line}"))
            .collect::<Vec<_>>()
            .join("\n");
        let indented = PyString::new(reporter.py(), &indented).call_method0("rstrip")?;
        writer.call_method1("line", (indented,))?;
        writer.call_method0("line")?;
    }
    writer.call_method1(
        "line",
        ("-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html",),
    )?;
    Ok(())
}

fn warning_locations(reporter: &Bound<'_, PyAny>, records: &[&WarningRecord]) -> PyResult<String> {
    let mut locations = Vec::new();
    for record in records {
        if record.nodeid.is_empty() {
            let path = reporter
                .py()
                .import("os.path")?
                .call_method1(
                    "relpath",
                    (&record.filename, reporter.getattr("startpath")?),
                )?
                .extract::<String>()?;
            locations.push(format!("{path}:{}", record.lineno));
        } else {
            locations.push(record.nodeid.clone());
        }
    }
    if locations.len() < 10 {
        return Ok(locations.join("\n"));
    }
    let mut filenames: Vec<(String, usize)> = Vec::new();
    for location in locations {
        let path = location.split("::").next().unwrap_or(&location);
        if let Some((_, count)) = filenames.iter_mut().find(|(name, _)| name == path) {
            *count += 1;
        } else {
            filenames.push((path.to_owned(), 1));
        }
    }
    Ok(filenames
        .into_iter()
        .map(|(name, count)| {
            format!(
                "{name}: {count} warning{}",
                if count == 1 { "" } else { "s" }
            )
        })
        .collect::<Vec<_>>()
        .join("\n"))
}

pub(in crate::independent) fn totals(reporter: &Bound<'_, PyAny>) -> PyResult<()> {
    let Some(result) = result(reporter)? else {
        return Ok(());
    };
    if reporter
        .getattr("config")?
        .call_method1("getoption", ("collect_only", false))?
        .is_truthy()?
    {
        let record = result.borrow(reporter.py());
        let record = record.record();
        reporter.call_method1(
            "collect_only_summary",
            (record.collected, record.deselected_nodeids.len()),
        )?;
        return Ok(());
    }
    let warning_count = warnings(reporter)?.len();
    let options = PyDict::new(reporter.py());
    options.set_item("warning_count", warning_count)?;
    let summary = {
        let result = result.borrow(reporter.py());
        crate::reporting::terminal_summary(result.record(), options.as_any())?
    };
    reporter.call_method1("summary_stats", (summary.summary_parts, summary.main_color))?;
    Ok(())
}

#[cfg(test)]
mod tests;
