//! Expose mutable collection protocol fields from a report snapshot.

use pyo3::exceptions::{PyStopIteration, PyUnicodeEncodeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};
use vfy_core::test_execution::{
    CollectionOutcome, CollectionReport, PhasePayload, ReportSection, SkipLocation,
};

#[pyclass(module = "vfy._native._pytest", dict, weakref)]
pub(crate) struct NativeIndependentCollectReport {
    snapshot: CollectionReport,
    // Hooks require actual Python identity and mutable containers until publication.
    result: Option<Py<PyAny>>,
    sections: Option<Py<PyAny>>,
    longrepr: Option<Py<PyAny>>,
}

/// Like a generator, section iteration reads the report only on its first step.
#[pyclass]
struct CollectSections {
    report: Option<Py<NativeIndependentCollectReport>>,
    iterator: Option<Py<PyAny>>,
    prefix: String,
}

impl CollectSections {
    /// Advances the section iterator; the caller clears the state once it ends or fails.
    fn advance(slf: &Bound<'_, Self>) -> PyResult<Option<(String, String)>> {
        let py = slf.py();
        let (report, iterator, prefix) = {
            let state = slf.borrow();
            (
                state.report.as_ref().map(|report| report.clone_ref(py)),
                state
                    .iterator
                    .as_ref()
                    .map(|iterator| iterator.clone_ref(py)),
                state.prefix.clone(),
            )
        };
        let Some(report) = report else {
            return Ok(None);
        };
        let iterator = if let Some(iterator) = iterator {
            iterator
        } else {
            let sections = report.borrow(py).retained_sections(py);
            let iterator = sections.bind(py).try_iter()?.into_any().unbind();
            slf.borrow_mut().iterator = Some(iterator.clone_ref(py));
            iterator
        };
        loop {
            let section = match iterator.bind(py).call_method0("__next__") {
                Ok(section) => section,
                Err(error) if error.is_instance_of::<PyStopIteration>(py) => return Ok(None),
                Err(error) => return Err(error),
            };
            let (name, content) = section.extract::<(String, String)>()?;
            if name.starts_with(&prefix) {
                return Ok(Some((prefix, content)));
            }
        }
    }
}

#[pymethods]
impl CollectSections {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(slf: &Bound<'_, Self>) -> PyResult<Option<(String, String)>> {
        let next = Self::advance(slf);
        if !matches!(next, Ok(Some(_))) {
            Self::__clear__(slf);
        }
        next
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.report)?;
        visit.call(&self.iterator)
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut state = slf.borrow_mut();
            (state.report.take(), state.iterator.take())
        };
        drop(removed);
    }
}

fn parse_outcome(value: &str) -> PyResult<CollectionOutcome> {
    match value {
        "passed" => Ok(CollectionOutcome::Passed),
        "failed" => Ok(CollectionOutcome::Failed),
        "skipped" => Ok(CollectionOutcome::Skipped),
        _ => Err(PyValueError::new_err(
            "report outcome must be passed, failed, or skipped",
        )),
    }
}

fn empty_sections() -> Py<PyAny> {
    Python::attach(|py| PyTuple::empty(py).into_any().unbind())
}

impl NativeIndependentCollectReport {
    pub(in crate::independent) fn from_collection_call(
        py: Python<'_>,
        collector: &Bound<'_, PyAny>,
        call: &Bound<'_, super::call::NativeIndependentCallInfo>,
        stdout: String,
        stderr: String,
    ) -> PyResult<Py<Self>> {
        let timing = call.borrow().timing();
        let info = call.getattr("excinfo")?;
        let (outcome, longrepr, result) = if info.is_truthy()? {
            let value = info.getattr("value")?;
            let skipped = value
                .is_instance(&py.import("vfy.independent.facade")?.getattr("Skipped")?)?
                || value.is_instance(&py.import("unittest")?.getattr("SkipTest")?)?;
            if skipped {
                let crash = super::diagnostics::exception_crash(py, &info)?.ok_or_else(|| {
                    pyo3::exceptions::PyAssertionError::new_err(
                        "There should always be a traceback entry for skipping a test.",
                    )
                })?;
                let line = usize::try_from(crash.location.line).map_err(|error| {
                    pyo3::exceptions::PyOverflowError::new_err(error.to_string())
                })?;
                // pytest's ExceptionInfo.exconly strips trailing whitespace before
                // placing the crash message in a collection skip's longrepr.
                let message = PyString::new(py, &crash.text)
                    .call_method0("rstrip")?
                    .extract::<String>()?;
                (
                    CollectionOutcome::Skipped,
                    (crash.location.path, line, message)
                        .into_pyobject(py)?
                        .into_any()
                        .unbind(),
                    PyList::empty(py).into_any().unbind(),
                )
            } else {
                (
                    CollectionOutcome::Failed,
                    collector.call_method1("repr_failure", (&info,))?.unbind(),
                    PyList::empty(py).into_any().unbind(),
                )
            }
        } else {
            (
                CollectionOutcome::Passed,
                py.None(),
                call.getattr("result")?.unbind(),
            )
        };
        let sections = PyList::empty(py);
        if !stdout.is_empty() {
            sections.append(("Captured stdout", &stdout))?;
        }
        if !stderr.is_empty() {
            sections.append(("Captured stderr", &stderr))?;
        }
        let payload = PhasePayload {
            stdout,
            stderr,
            report_sections: Some(Vec::new()),
            ..PhasePayload::default()
        };
        let report = Py::new(
            py,
            Self {
                snapshot: CollectionReport {
                    nodeid: collector.getattr("nodeid")?.extract()?,
                    outcome,
                    duration_ms: timing.duration * 1000.0,
                    payload,
                },
                result: Some(result),
                sections: Some(sections.into_any().unbind()),
                longrepr: Some(longrepr),
            },
        )?;
        report.bind(py).setattr(
            "_vfy_transport_summary",
            collector
                .getattr("config")?
                .getattr("option")?
                .getattr("workers")?
                .gt(1)?,
        )?;
        Ok(report)
    }

    fn section_values(slf: &Bound<'_, Self>) -> PyResult<Vec<ReportSection>> {
        let sections = slf.borrow().retained_sections(slf.py());
        sections
            .bind(slf.py())
            .try_iter()?
            .map(|section| {
                let (name, content) = section?.extract::<(String, String)>()?;
                Ok(ReportSection { name, content })
            })
            .collect()
    }

    fn captured(slf: &Bound<'_, Self>, prefix: &str, separator: &str) -> PyResult<String> {
        Ok(Self::section_values(slf)?
            .into_iter()
            .filter(|section| section.name.starts_with(prefix))
            .map(|section| section.content)
            .collect::<Vec<_>>()
            .join(separator))
    }

    /// Snapshot hook edits after representation callbacks have released their borrows.
    pub(in crate::independent) fn snapshot(slf: &Bound<'_, Self>) -> PyResult<CollectionReport> {
        let py = slf.py();
        let representation = slf.borrow().longrepr(py);
        let mut payload = slf.borrow().snapshot.payload.clone();
        payload.detail = Self::longreprtext(slf)?;
        payload.terminal_lead.clear();
        payload.exception = None;
        payload.skip_location = None;
        payload.report_sections = Some(Self::section_values(slf)?);
        payload.report_summary = super::terminal::summary::transport_metadata(slf.as_any())?;
        let mut snapshot = slf.borrow().snapshot.clone();
        if snapshot.outcome == CollectionOutcome::Skipped {
            if let Ok(skipped) = representation.bind(py).cast::<PyTuple>() {
                let (path, line, message) = skipped.extract::<(String, usize, String)>()?;
                payload.skip_location = Some(SkipLocation { path, line });
                payload.exception = Some(message.into());
            } else if !payload.detail.is_empty() {
                payload.exception = Some(payload.detail.clone().into());
            }
        }
        snapshot.payload = payload;
        slf.borrow_mut().snapshot = snapshot.clone();
        Ok(snapshot)
    }
}

#[pymethods]
impl NativeIndependentCollectReport {
    #[new]
    #[pyo3(
        signature = (nodeid, outcome, longrepr, result, sections=empty_sections(), **extra),
        text_signature = "(nodeid, outcome, longrepr, result, sections=(), **extra)"
    )]
    fn new(
        py: Python<'_>,
        nodeid: String,
        outcome: &str,
        longrepr: Py<PyAny>,
        result: Py<PyAny>,
        sections: Py<PyAny>,
        extra: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<Self>> {
        let outcome = parse_outcome(outcome)?;
        let result = if result.bind(py).is_truthy()? {
            result
        } else {
            PyList::empty(py).into_any().unbind()
        };
        let source_sections = sections.into_bound(py);
        let sections = PyList::new(
            py,
            source_sections.try_iter()?.collect::<PyResult<Vec<_>>>()?,
        )?;
        let report = Py::new(
            py,
            Self {
                snapshot: CollectionReport {
                    nodeid,
                    outcome,
                    duration_ms: 0.0,
                    payload: PhasePayload::default(),
                },
                result: Some(result),
                sections: Some(sections.into_any().unbind()),
                longrepr: Some(longrepr),
            },
        )?;
        if let Some(extra) = extra {
            report
                .bind(py)
                .getattr("__dict__")?
                .cast::<PyDict>()?
                .update(extra.as_mapping())?;
        }
        Ok(report)
    }

    #[classattr]
    fn when() -> &'static str {
        "collect"
    }

    #[getter]
    fn nodeid(&self) -> &str {
        &self.snapshot.nodeid
    }

    #[setter]
    fn set_nodeid(&mut self, value: String) {
        self.snapshot.nodeid = value;
    }

    #[getter]
    fn outcome(&self) -> &'static str {
        match self.snapshot.outcome {
            CollectionOutcome::Passed => "passed",
            CollectionOutcome::Failed => "failed",
            CollectionOutcome::Skipped => "skipped",
        }
    }

    #[setter]
    fn set_outcome(&mut self, value: &str) -> PyResult<()> {
        self.snapshot.outcome = parse_outcome(value)?;
        Ok(())
    }

    #[getter]
    fn passed(&self) -> bool {
        self.snapshot.outcome == CollectionOutcome::Passed
    }

    #[getter]
    fn failed(&self) -> bool {
        self.snapshot.outcome == CollectionOutcome::Failed
    }

    #[getter]
    fn skipped(&self) -> bool {
        self.snapshot.outcome == CollectionOutcome::Skipped
    }

    #[getter]
    fn result(&self, py: Python<'_>) -> Py<PyAny> {
        self.result
            .as_ref()
            .map_or_else(|| py.None(), |value| value.clone_ref(py))
    }

    #[setter]
    fn set_result(slf: &Bound<'_, Self>, value: Py<PyAny>) {
        let previous = slf.borrow_mut().result.replace(value);
        drop(previous);
    }

    #[getter(sections)]
    fn retained_sections(&self, py: Python<'_>) -> Py<PyAny> {
        self.sections
            .as_ref()
            .map_or_else(|| py.None(), |value| value.clone_ref(py))
    }

    #[setter]
    fn set_sections(slf: &Bound<'_, Self>, value: Py<PyAny>) {
        let previous = slf.borrow_mut().sections.replace(value);
        drop(previous);
    }

    #[getter]
    fn longrepr(&self, py: Python<'_>) -> Py<PyAny> {
        self.longrepr
            .as_ref()
            .map_or_else(|| py.None(), |value| value.clone_ref(py))
    }

    #[setter]
    fn set_longrepr(slf: &Bound<'_, Self>, value: Py<PyAny>) {
        let previous = slf.borrow_mut().longrepr.replace(value);
        drop(previous);
    }

    #[getter]
    fn fspath(&self) -> &str {
        self.nodeid().split("::").next().unwrap_or_default()
    }

    #[getter]
    fn location(&self) -> (String, Option<usize>, String) {
        let path = self.fspath().to_owned();
        (path.clone(), None, path)
    }

    #[getter]
    fn head_line(&self) -> &str {
        self.fspath()
    }

    #[getter]
    #[expect(
        clippy::unused_self,
        reason = "The report summary protocol requires an instance property"
    )]
    fn count_towards_summary(&self) -> bool {
        true
    }

    #[getter]
    fn capstdout(slf: &Bound<'_, Self>) -> PyResult<String> {
        Self::captured(slf, "Captured stdout", "")
    }

    #[getter]
    fn capstderr(slf: &Bound<'_, Self>) -> PyResult<String> {
        Self::captured(slf, "Captured stderr", "")
    }

    #[getter]
    fn caplog(slf: &Bound<'_, Self>) -> PyResult<String> {
        Self::captured(slf, "Captured log", "\n")
    }

    fn get_sections(slf: &Bound<'_, Self>, prefix: String) -> PyResult<Py<CollectSections>> {
        Py::new(
            slf.py(),
            CollectSections {
                report: Some(slf.clone().unbind()),
                iterator: None,
                prefix,
            },
        )
    }

    fn toterminal(slf: &Bound<'_, Self>, out: &Bound<'_, PyAny>) -> PyResult<()> {
        let representation = slf.borrow().longrepr(slf.py());
        let representation = representation.bind(slf.py());
        if representation.is_none() {
            return Ok(());
        }
        if representation.hasattr("toterminal")? {
            representation.call_method1("toterminal", (out,))?;
        } else {
            let text = match representation.str() {
                Ok(text) => text.into_any(),
                Err(error) if error.is_instance_of::<PyUnicodeEncodeError>(slf.py()) => {
                    "<unprintable longrepr>".into_pyobject(slf.py())?.into_any()
                }
                Err(error) => return Err(error),
            };
            out.call_method1("line", (text,))?;
        }
        Ok(())
    }

    #[getter]
    fn longreprtext(slf: &Bound<'_, Self>) -> PyResult<String> {
        let py = slf.py();
        let stream = py.import("io")?.call_method0("StringIO")?;
        let writer = py
            .import("vfy.independent.terminal")?
            .call_method1("report_writer", (&stream,))?;
        slf.call_method1("toterminal", (writer,))?;
        stream
            .call_method0("getvalue")?
            .call_method0("strip")?
            .extract()
    }

    /// Return the captured collection diagnostic from the Rust report snapshot.
    #[getter]
    fn detail(slf: &Bound<'_, Self>) -> PyResult<String> {
        Ok(Self::snapshot(slf)?.payload.detail)
    }

    fn __repr__(slf: &Bound<'_, Self>) -> PyResult<String> {
        let (nodeid, result) = {
            let report = slf.borrow();
            (report.nodeid().to_owned(), report.result(slf.py()))
        };
        let nodeid = nodeid.into_pyobject(slf.py())?.repr()?;
        let length = result.bind(slf.py()).len()?;
        let outcome = slf.borrow().outcome();
        Ok(format!(
            "<CollectReport {} lenresult={length} outcome='{outcome}'>",
            nodeid.to_str()?
        ))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.result)?;
        visit.call(&self.sections)?;
        visit.call(&self.longrepr)
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut report = slf.borrow_mut();
            (
                report.result.take(),
                report.sections.take(),
                report.longrepr.take(),
            )
        };
        drop(removed);
    }
}

#[cfg(test)]
mod tests;
