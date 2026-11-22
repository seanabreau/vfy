//! Reporting hooks over the phase model.

use super::lifecycle::Context;
use crate::results::{ReportLocation, pytest_phase_report};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyWeakrefMethods};
use pyo3::{PyTraverseError, PyVisit};
use std::ops::Deref;
use std::sync::Arc;
use vfy_core::test_execution::{
    ExecutionOutcome, ExecutionPhase, PhasePayload, PhaseResult, ReportSection, SkipLocation,
    split_python_strip,
};

/// Capture the crash location for an assertion representation failure.
pub(crate) fn assertion_representation_crash(
    py: Python<'_>,
    error: &PyErr,
) -> PyResult<Option<vfy_core::test_diagnostics::DiagnosticCrash>> {
    let info = super::controls::exception_info(py, error)?;
    diagnostics::capture_exception_name(py, &info)?;
    diagnostics::exception_crash(py, &info)
}

pub(super) mod call;
mod collection;
mod diagnostics;
mod failure;
mod graph;
pub(super) mod graph_wire;
pub(in crate::independent) mod html;
pub(in crate::independent) mod origin;
pub(crate) use failure::{independent_collector_repr_failure, independent_item_repr_failure};
pub(super) mod interaction;
pub(super) mod junit;
pub(crate) mod make;
mod publication;
mod reportinfo;
mod session_methods;
mod skips;
pub(crate) mod terminal;
mod terminal_stats;
mod value;
pub(crate) mod writer;
pub(crate) use call::NativeIndependentCallInfo;
use call::PhaseTiming;
pub(crate) use collection::NativeIndependentCollectReport;
#[cfg(test)]
use publication::ReportBuffers;
pub(in crate::independent) use publication::{
    boundary, case_from_snapshots, case_snapshots_since, item_sections, make_subtest_report,
    materialize_reports, publish_phase, replace_case_snapshots, worker_report_locations,
};
pub(crate) use reportinfo::independent_item_reportinfo;
pub(in crate::independent) use reportinfo::{
    LocationNormalization, ReportInfoPath, function_code, item_reportinfo, location,
};
use reportinfo::{code_path, unpack};
pub(crate) use value::register as register_value_representations;

type Sections = Vec<(String, String)>;

/// Empty native sections acquire Python identity only when a caller requests the list.
enum ReportSections {
    Empty,
    Native(Sections),
    Live(Py<PyList>),
    Cleared,
}

impl ReportSections {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Empty => Self::Empty,
            Self::Native(sections) => Self::Native(sections.clone()),
            Self::Live(value) => Self::Live(value.clone_ref(py)),
            Self::Cleared => Self::Cleared,
        }
    }
    fn snapshot(self, py: Python<'_>) -> PyResult<Option<Vec<ReportSection>>> {
        match self {
            Self::Empty => Ok(Some(Vec::new())),
            Self::Native(sections) => Ok(Some(
                sections
                    .into_iter()
                    .map(|(name, content)| ReportSection { name, content })
                    .collect(),
            )),
            Self::Cleared => Ok(None),
            Self::Live(sections) => sections
                .bind(py)
                .iter()
                .map(|section| {
                    let (name, content) = section.extract::<(String, String)>()?;
                    Ok(ReportSection { name, content })
                })
                .collect::<PyResult<Vec<_>>>()
                .map(Some),
        }
    }
}

/// Report properties copy the item eagerly, but an empty copy needs no Python
/// list until a public reader requests its mutable identity. Setters accept any
/// Python object, as pytest's report does.
enum ReportUserProperties {
    Empty,
    Live(Py<PyAny>),
    Cleared,
}

impl ReportUserProperties {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        match self {
            Self::Empty => Self::Empty,
            Self::Live(value) => Self::Live(value.clone_ref(py)),
            Self::Cleared => Self::Cleared,
        }
    }
}

#[cfg(test)]
mod tests;

pub(crate) mod rows;
mod session_hook;
use rows::{ReportEntry, ReportOwners, ReportSlot, RetiredRows};
pub(crate) use rows::{ReportHandle, ReportId};
pub(crate) use session_hook::{NativeSessionLogReport, NativeSessionLogReportDescriptor};

mod copies;
mod serialization;
pub(super) use copies::install_cleanup as install_copy_cleanup;
pub(crate) mod subtest_report;

enum ReportKind {
    Phase,
    Subtest(Option<Py<super::subtests::NativeSubtestContext>>),
}

#[derive(Clone, Copy)]
pub(in crate::independent) enum PhaseProjection {
    Full,
    ExecutionCheckpoint,
}

pub(in crate::independent) struct PhasePublication<'a, 'py> {
    pub(in crate::independent) boundary: origin::ReportBoundary,
    pub(in crate::independent) projection: PhaseProjection,
    pub(in crate::independent) history: &'a Bound<'py, NativeSessionReports>,
    pub(in crate::independent) report_start: usize,
    pub(in crate::independent) shared_nodeid: Option<&'a Arc<str>>,
}

pub(super) enum ReportSnapshot {
    Phase(PhaseResult),
    Subtest(vfy_core::test_execution::SubtestResult),
}

/// Instance attributes owned natively until the report acquires Python identity.
pub(crate) struct PendingAttributes {
    keywords: Py<PyDict>,
    wasxfail: Option<Py<PyAny>>,
}

impl PendingAttributes {
    fn clone_ref(&self, py: Python<'_>) -> Self {
        Self {
            keywords: self.keywords.clone_ref(py),
            wasxfail: self.wasxfail.as_ref().map(|value| value.clone_ref(py)),
        }
    }

    pub(crate) fn get_item(
        &self,
        py: Python<'_>,
        name: &Bound<'_, PyString>,
    ) -> PyResult<Option<Py<PyAny>>> {
        if name.as_any().eq(pyo3::intern!(py, "keywords"))? {
            Ok(Some(self.keywords.clone_ref(py).into_any()))
        } else if name.as_any().eq(pyo3::intern!(py, "wasxfail"))? {
            Ok(self.wasxfail.as_ref().map(|value| value.clone_ref(py)))
        } else {
            Ok(None)
        }
    }

    fn dictionary<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dictionary = PyDict::new(py);
        dictionary.set_item(pyo3::intern!(py, "keywords"), &self.keywords)?;
        if let Some(reason) = &self.wasxfail {
            dictionary.set_item(pyo3::intern!(py, "wasxfail"), reason)?;
        }
        Ok(dictionary)
    }
}

#[derive(Clone, Copy)]
pub(in crate::independent) struct ReportIdentity<'a> {
    location: &'a Arc<ReportLocation>,
    shared_nodeid: Option<&'a Arc<str>>,
}

/// Reports retain phase snapshots without keeping the executor or fixtures alive.
#[pyclass(module = "vfy._native._pytest", dict, weakref, subclass)]
pub(crate) struct NativeIndependentReport {
    pub(in crate::independent) origin: Option<origin::ReportOrigin>,
    phase: PhaseResult,
    row_id: Option<ReportId>,
    pending_attributes: Option<PendingAttributes>,
    issued_dicts: Vec<Py<PyDict>>,
    pending_copy: Option<Py<PyDict>>,
    nodeid: Arc<str>,
    location: Arc<ReportLocation>,
    sections: ReportSections,
    longrepr: Option<Py<PyAny>>,
    representation_changed: bool,
    refresh_representation: bool,
    xfail_reason: Option<vfy_core::test_execution::PythonText>,
    user_properties: ReportUserProperties,
    timing: PhaseTiming,
    kind: ReportKind,
}

impl NativeIndependentReport {
    pub(in crate::independent) fn bound_origin(
        &self,
        _py: Python<'_>,
    ) -> PyResult<origin::ReportOrigin> {
        self.origin.clone().ok_or_else(|| {
            crate::UnsupportedFeatureError::new_err("report has no bound native case provenance")
        })
    }
    pub(in crate::independent) fn subtest_context(
        &self,
        py: Python<'_>,
    ) -> Option<Py<super::subtests::NativeSubtestContext>> {
        match &self.kind {
            ReportKind::Phase => None,
            ReportKind::Subtest(context) => context.as_ref().map(|context| context.clone_ref(py)),
        }
    }

    pub(in crate::independent) fn is_subtest(&self) -> bool {
        matches!(self.kind, ReportKind::Subtest(_))
    }

    pub(in crate::independent) fn snapshot_event(
        slf: &Bound<'_, Self>,
    ) -> PyResult<ReportSnapshot> {
        let is_subtest = slf.borrow().is_subtest();
        if is_subtest {
            Ok(ReportSnapshot::Subtest(subtest_report::snapshot(slf)?))
        } else {
            Ok(ReportSnapshot::Phase(Self::snapshot(slf)?))
        }
    }

    pub(in crate::independent) fn prepare(
        py: Python<'_>,
        identity: ReportIdentity<'_>,
        mut phase: PhaseResult,
        sections: Sections,
        item: &Bound<'_, PyAny>,
        timing: PhaseTiming,
        representation: Option<&Py<PyAny>>,
    ) -> PyResult<Self> {
        let nodeid = item
            .getattr(pyo3::intern!(py, "nodeid"))?
            .cast_into::<PyString>()?
            .to_cow()
            .map(|nodeid| match identity.shared_nodeid {
                Some(shared) if shared.as_ref() == nodeid.as_ref() => Arc::clone(shared),
                _ => Arc::<str>::from(nodeid.as_ref()),
            })?;
        let refresh_representation = representation.is_some();
        let (_, _, _, _, longrepr, wasxfail, sections, _) =
            pytest_phase_report(py, &nodeid, &phase, sections, representation)?;
        if phase.outcome == ExecutionOutcome::Skipped
            && phase.skip_location.is_some()
            && let Some(representation) = &longrepr
        {
            let text = representation.bind(py).str()?;
            let mut payload: PhasePayload = phase.deref().clone();
            split_python_strip(text.to_str()?)
                .1
                .clone_into(&mut payload.detail);
            phase = PhaseResult::new(phase.outcome, phase.phase, phase.duration_ms, payload);
        }
        let original_xfail = crate::results::pytest_phase_xfail(&phase);
        let properties = item.getattr(pyo3::intern!(py, "user_properties"))?;
        let mut report = Self {
            phase,
            row_id: None,
            pending_attributes: None,
            issued_dicts: Vec::new(),
            pending_copy: None,
            nodeid,
            location: identity.location.clone(),
            sections: if sections.is_empty() {
                ReportSections::Empty
            } else {
                ReportSections::Native(sections)
            },
            longrepr,
            representation_changed: false,
            refresh_representation,
            xfail_reason: original_xfail,
            user_properties: if properties
                .cast_exact::<PyList>()
                .is_ok_and(pyo3::types::PyListMethods::is_empty)
            {
                ReportUserProperties::Empty
            } else {
                // Preserve iteration, failures, and the report-time snapshot
                // even if nobody ever reads the resulting empty list.
                let values = properties.try_iter()?.collect::<PyResult<Vec<_>>>()?;
                if values.is_empty() {
                    ReportUserProperties::Empty
                } else {
                    ReportUserProperties::Live(PyList::new(py, values)?.into_any().unbind())
                }
            },
            origin: None,
            timing,
            kind: ReportKind::Phase,
        };
        report.pending_attributes = Some(PendingAttributes {
            keywords: terminal::summary::keywords(item)?,
            wasxfail,
        });
        Ok(report)
    }

    #[cfg(test)]
    fn create(
        py: Python<'_>,
        location: &Arc<ReportLocation>,
        phase: PhaseResult,
        sections: Sections,
        item: &Bound<'_, PyAny>,
        timing: PhaseTiming,
        representation: Option<&Py<PyAny>>,
    ) -> PyResult<Py<Self>> {
        Self::publish(
            py,
            Self::prepare(
                py,
                ReportIdentity {
                    location,
                    shared_nodeid: None,
                },
                phase,
                sections,
                item,
                timing,
                representation,
            )?,
        )
    }

    pub(crate) fn publish(py: Python<'_>, mut report: Self) -> PyResult<Py<Self>> {
        let attributes = report.pending_attributes.take();
        let report = Py::new(py, report)?;
        if let Some(attributes) = attributes {
            report
                .bind(py)
                .setattr(pyo3::intern!(py, "keywords"), attributes.keywords)?;
            if let Some(reason) = attributes.wasxfail {
                report
                    .bind(py)
                    .setattr(pyo3::intern!(py, "wasxfail"), reason)?;
            }
        }
        Ok(report)
    }

    fn empty_shell() -> Self {
        Self {
            origin: None,
            phase: PhaseResult::new(
                ExecutionOutcome::Passed,
                ExecutionPhase::Setup,
                0.0,
                PhasePayload::default(),
            ),
            row_id: None,
            pending_attributes: None,
            issued_dicts: Vec::new(),
            pending_copy: None,
            nodeid: Arc::from(""),
            location: Arc::new((String::new(), None, String::new())),
            sections: ReportSections::Cleared,
            longrepr: None,
            representation_changed: false,
            refresh_representation: false,
            xfail_reason: None,
            user_properties: ReportUserProperties::Cleared,
            timing: PhaseTiming {
                start: 0.0,
                stop: 0.0,
                duration: 0.0,
            },
            kind: ReportKind::Phase,
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(attributes) = &self.pending_attributes {
            visit.call(&attributes.keywords)?;
            visit.call(&attributes.wasxfail)?;
        }
        for data in &self.issued_dicts {
            visit.call(data)?;
        }
        visit.call(&self.pending_copy)?;
        if let ReportSections::Live(sections) = &self.sections {
            visit.call(sections)?;
        }
        visit.call(&self.longrepr)?;
        if let ReportUserProperties::Live(value) = &self.user_properties {
            visit.call(value)?;
        }
        if let ReportKind::Subtest(context) = &self.kind {
            visit.call(context)?;
        }
        Ok(())
    }

    fn strings(slf: &Bound<'_, Self>, prefix: &str) -> PyResult<Vec<String>> {
        let sections = slf.borrow().sections.clone_ref(slf.py());
        let sections = match sections {
            ReportSections::Native(sections) => {
                return Ok(sections
                    .into_iter()
                    .filter_map(|(heading, text)| heading.starts_with(prefix).then_some(text))
                    .collect());
            }
            ReportSections::Live(sections) => sections,
            ReportSections::Empty | ReportSections::Cleared => return Ok(Vec::new()),
        };
        let mut content = Vec::new();
        for section in sections.bind(slf.py()).iter() {
            let (heading, text) = section.extract::<(String, String)>()?;
            if heading.starts_with(prefix) {
                content.push(text);
            }
        }
        Ok(content)
    }

    fn snapshot_sections(slf: &Bound<'_, Self>) -> PyResult<Option<Vec<ReportSection>>> {
        let sections = slf.borrow().sections.clone_ref(slf.py());
        sections.snapshot(slf.py())
    }

    pub(in crate::independent) fn snapshot(slf: &Bound<'_, Self>) -> PyResult<PhaseResult> {
        let py = slf.py();
        let (mut projected, representation, representation_changed, original_reason) = {
            let report = slf.borrow();
            (
                report.phase.clone(),
                report.longrepr.as_ref().map(|value| value.clone_ref(py)),
                report.representation_changed || report.refresh_representation,
                report.xfail_reason.clone(),
            )
        };
        let initial_outcome = projected.outcome;
        let mut replacement = None;
        let wasxfail =
            crate::module_cache::optional_attr(slf.as_any(), pyo3::intern!(py, "wasxfail"))?;
        if representation_changed && let Some(representation) = representation {
            let payload = replacement.get_or_insert_with(|| projected.deref().clone());
            let representation = representation.bind(py);
            if representation
                .cast::<failure::NativeDiagnosticRepr>()
                .is_err()
            {
                // An exception chain keeps its rendered lead; other objects have none.
                payload.terminal_lead.clear();
                failure::preserve_diagnostic(representation, payload)?;
            }
            if representation.is_none() {
                payload.detail.clear();
                payload.exception = None;
                payload.skip_location = None;
            } else if initial_outcome.report_outcome().as_str() == "skipped"
                && let Ok(skipped) = representation.cast::<PyTuple>()
            {
                let (path, line, message) =
                    skipped.extract::<(String, usize, Bound<'_, PyAny>)>()?;
                let message = crate::reporting::python_text_from_python(&message)?;
                payload.skip_location = Some(SkipLocation { path, line });
                payload.exception = Some(message);
                split_python_strip(representation.str()?.to_str()?)
                    .1
                    .clone_into(&mut payload.detail);
            } else {
                representation
                    .str()?
                    .to_str()?
                    .clone_into(&mut payload.detail);
                if initial_outcome.report_outcome().as_str() == "skipped" {
                    payload.exception = Some(payload.detail.clone().into());
                    payload.skip_location = None;
                }
            }
        }
        if let Some(reason) = &wasxfail {
            let reason = crate::reporting::python_text_from_python(reason.str()?.as_any())?;
            if original_reason.as_ref() != Some(&reason) {
                replacement
                    .get_or_insert_with(|| projected.deref().clone())
                    .exception = Some(reason);
            }
        }
        // Representation formatting can re-enter the report and change its outcome.
        let sections = Self::snapshot_sections(slf)?;
        let transport_summary = {
            let report = slf.borrow();
            report.phase.report_summary.is_some()
                || report
                    .origin
                    .as_ref()
                    .is_some_and(origin::ReportOrigin::transports_summary)
        };
        let summary = terminal::summary::phase_transport_metadata(slf.as_any(), transport_summary)?;
        let (outcome, phase, duration_ms) = {
            let report = slf.borrow();
            (
                report.phase.outcome,
                report.phase.phase,
                report.phase.duration_ms,
            )
        };
        let outcome = match outcome.report_outcome().as_str() {
            "passed" if wasxfail.is_some() => ExecutionOutcome::XPassed,
            "passed" => ExecutionOutcome::Passed,
            "skipped" if wasxfail.is_some() => ExecutionOutcome::XFailed,
            "skipped" => ExecutionOutcome::Skipped,
            _ if phase == ExecutionPhase::Call => ExecutionOutcome::Failed,
            _ => ExecutionOutcome::Error,
        };
        if let Some(mut payload) = replacement {
            payload.report_sections = sections;
            if let Some(summary) = summary {
                payload.report_summary = Some(summary);
            }
            return Ok(PhaseResult::new(outcome, phase, duration_ms, payload));
        }
        projected.outcome = outcome;
        projected.phase = phase;
        projected.duration_ms = duration_ms;
        projected.set_report_sections(sections);
        if let Some(summary) = summary {
            projected.set_report_summary(Some(summary));
        }
        Ok(projected)
    }
}

#[pymethods]
impl NativeIndependentReport {
    fn _to_json(slf: &Bound<'_, Self>) -> PyResult<Py<PyDict>> {
        serialization::to_json(slf)
    }
    #[classmethod]
    fn _from_json(
        _class: &Bound<'_, pyo3::types::PyType>,
        data: &Bound<'_, PyDict>,
    ) -> PyResult<Py<Self>> {
        serialization::from_json(data)
    }
    #[getter]
    fn nodeid(&self) -> &str {
        &self.nodeid
    }

    #[getter]
    fn when<'py>(&self, py: Python<'py>) -> Bound<'py, PyString> {
        super::phase_text(py, self.phase.phase)
    }

    #[getter]
    fn outcome<'py>(&self, py: Python<'py>) -> Bound<'py, PyString> {
        super::outcome_text(py, self.phase.outcome)
    }

    #[setter]
    fn set_outcome(&mut self, value: &str) -> PyResult<()> {
        self.phase.outcome = match value {
            "passed" => ExecutionOutcome::Passed,
            "skipped" => ExecutionOutcome::Skipped,
            "failed" if self.phase.phase == ExecutionPhase::Call => ExecutionOutcome::Failed,
            "failed" => ExecutionOutcome::Error,
            _ => {
                return Err(PyValueError::new_err(
                    "report outcome must be passed, failed, or skipped",
                ));
            }
        };
        Ok(())
    }

    #[getter]
    fn passed(&self) -> bool {
        self.phase.outcome.report_outcome().as_str() == "passed"
    }

    #[getter]
    fn failed(&self) -> bool {
        self.phase.outcome.report_outcome().as_str() == "failed"
    }

    #[getter]
    fn skipped(&self) -> bool {
        self.phase.outcome.report_outcome().as_str() == "skipped"
    }

    #[getter]
    fn location(slf: &Bound<'_, Self>) -> ReportLocation {
        slf.borrow().location.as_ref().clone()
    }

    #[getter]
    fn duration(&self) -> f64 {
        self.timing.duration
    }

    #[setter]
    fn set_duration(slf: &Bound<'_, Self>, value: f64) {
        let mut report = slf.borrow_mut();
        report.timing.duration = value;
        report.phase.duration_ms = value * 1000.0;
    }

    #[getter]
    fn start(&self) -> f64 {
        self.timing.start
    }

    #[getter]
    fn stop(&self) -> f64 {
        self.timing.stop
    }

    #[getter(sections)]
    fn sections_value(slf: &Bound<'_, Self>) -> PyResult<Py<PyList>> {
        let py = slf.py();
        let current = slf.borrow().sections.clone_ref(py);
        let sections = match current {
            ReportSections::Live(sections) => return Ok(sections),
            ReportSections::Cleared => {
                return Err(PyRuntimeError::new_err("test report has been cleared"));
            }
            ReportSections::Empty => Vec::new(),
            ReportSections::Native(sections) => sections,
        };
        // Allocation can run a collector that reenters this report's getters or setters.
        let candidate = PyList::new(py, sections)?.unbind();
        let selected = {
            let mut report = slf.borrow_mut();
            match &report.sections {
                ReportSections::Empty | ReportSections::Native(_) => {
                    report.sections = ReportSections::Live(candidate.clone_ref(py));
                    Some(candidate.clone_ref(py))
                }
                ReportSections::Live(sections) => Some(sections.clone_ref(py)),
                ReportSections::Cleared => None,
            }
        };
        drop(candidate);
        selected.ok_or_else(|| PyRuntimeError::new_err("test report has been cleared"))
    }

    #[setter(sections)]
    fn set_sections(slf: &Bound<'_, Self>, value: Py<PyList>) {
        let previous =
            std::mem::replace(&mut slf.borrow_mut().sections, ReportSections::Live(value));
        drop(previous);
    }

    #[getter]
    fn user_properties(slf: &Bound<'_, Self>) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let current = slf.borrow().user_properties.clone_ref(py);
        match current {
            ReportUserProperties::Live(value) => return Ok(value),
            ReportUserProperties::Cleared => {
                return Err(PyRuntimeError::new_err("test report has been cleared"));
            }
            ReportUserProperties::Empty => {}
        }
        // Match sections: list allocation may run a collector that reenters a
        // getter or installs an arbitrary replacement through the setter.
        let candidate = PyList::new(py, std::iter::empty::<Py<PyAny>>())?
            .into_any()
            .unbind();
        let selected = {
            let mut report = slf.borrow_mut();
            match &report.user_properties {
                ReportUserProperties::Empty => {
                    report.user_properties = ReportUserProperties::Live(candidate.clone_ref(py));
                    Some(candidate.clone_ref(py))
                }
                ReportUserProperties::Live(value) => Some(value.clone_ref(py)),
                ReportUserProperties::Cleared => None,
            }
        };
        drop(candidate);
        selected.ok_or_else(|| PyRuntimeError::new_err("test report has been cleared"))
    }

    #[setter]
    fn set_user_properties(slf: &Bound<'_, Self>, value: Py<PyAny>) {
        let previous = std::mem::replace(
            &mut slf.borrow_mut().user_properties,
            ReportUserProperties::Live(value),
        );
        drop(previous);
    }

    #[getter]
    fn caplog(slf: &Bound<'_, Self>) -> PyResult<String> {
        Ok(Self::strings(slf, "Captured log")?.join("\n"))
    }

    #[getter]
    fn capstdout(slf: &Bound<'_, Self>) -> PyResult<String> {
        Ok(Self::strings(slf, "Captured stdout")?.concat())
    }

    #[getter]
    fn capstderr(slf: &Bound<'_, Self>) -> PyResult<String> {
        Ok(Self::strings(slf, "Captured stderr")?.concat())
    }

    fn get_sections(slf: &Bound<'_, Self>, prefix: &str) -> PyResult<Py<PyAny>> {
        let sections = Self::strings(slf, prefix)?
            .into_iter()
            .map(|text| (prefix.to_owned(), text));
        Ok(PyList::new(slf.py(), sections)?
            .call_method0("__iter__")?
            .unbind())
    }

    #[getter]
    fn longrepr(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.longrepr.as_ref().map(|value| value.clone_ref(py))
    }

    #[setter]
    fn set_longrepr(slf: &Bound<'_, Self>, value: Py<PyAny>) {
        let previous = {
            let mut report = slf.borrow_mut();
            report.representation_changed = true;
            report.longrepr.replace(value)
        };
        drop(previous);
    }

    #[getter]
    fn longreprtext(slf: &Bound<'_, Self>) -> PyResult<String> {
        let representation = slf.borrow().longrepr(slf.py());
        match representation {
            Some(value) if !value.bind(slf.py()).is_none() => {
                let text = value.bind(slf.py()).str()?;
                Ok(split_python_strip(text.to_str()?).1.to_owned())
            }
            _ => Ok(String::new()),
        }
    }

    #[getter]
    fn detail(slf: &Bound<'_, Self>) -> PyResult<String> {
        Self::longreprtext(slf)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.traverse(&visit)
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut report = slf.borrow_mut();
            (
                std::mem::take(&mut report.issued_dicts),
                report.pending_attributes.take(),
                report.pending_copy.take(),
                std::mem::replace(&mut report.sections, ReportSections::Cleared),
                report.longrepr.take(),
                std::mem::replace(&mut report.user_properties, ReportUserProperties::Cleared),
                std::mem::replace(&mut report.kind, ReportKind::Phase),
            )
        };
        drop(removed);
    }
}

pub(super) use vfy_core::relative_location;

/// The live Session invokes this callback at its builtin report-hook priority.
#[pyclass(module = "vfy._native._pytest", weakref)]
pub(crate) struct NativeSessionReports {
    session: Option<Py<PyAny>>,
    registry: Option<Py<crate::hooks::NativeHookRegistry>>,
    reports: Vec<ReportId>,
    rows: Vec<ReportSlot>,
    owners: ReportOwners,
    retired_rows: RetiredRows,
    terminal: terminal::TerminalStatusIndex,
    pub(in crate::independent) summary: terminal::session::SummarySession,
    generation: u64,
    worker: bool,
}

/// Each native session history gets a distinct generation; origins name the generation they bind to.
static NEXT_GENERATION: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

impl NativeSessionReports {
    pub(crate) fn mark_parallel_progress(&mut self) {
        self.terminal.mark_parallel_progress();
    }

    pub(super) fn new(py: Python<'_>, context: &Context) -> Self {
        Self {
            session: Some(context.session.clone_ref(py)),
            registry: Some(context.registry.clone_ref(py)),
            reports: Vec::new(),
            rows: Vec::new(),
            owners: ReportOwners::default(),
            retired_rows: Arc::new(std::sync::Mutex::new(Vec::new())),
            terminal: terminal::TerminalStatusIndex::new(py),
            summary: terminal::session::SummarySession::new(),
            generation: NEXT_GENERATION.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
            worker: context.invocation.is_worker(),
        }
    }

    pub(in crate::independent) const fn generation(&self) -> u64 {
        self.generation
    }

    /// Rescue the emitted handle when a report hook aborts before Session receives it.
    pub(in crate::independent) fn retain_report(
        slf: &Bound<'_, Self>,
        report: &Bound<'_, NativeIndependentReport>,
    ) {
        let id = Self::adopt_published(slf, report);
        let mut history = slf.borrow_mut();
        history.retain_session(id);
        history.list_once(id);
    }

    pub(in crate::independent) fn report_count(&self) -> usize {
        self.reports.len()
    }

    pub(in crate::independent) fn call_passed(
        &self,
        py: Python<'_>,
        origin: &origin::ReportOrigin,
    ) -> bool {
        for id in self.reports.iter().rev() {
            let Some(entry) = self.rows.get(id.0).and_then(|slot| slot.entry.as_ref()) else {
                continue;
            };
            let phase = match entry {
                ReportEntry::Native(report) => {
                    if report.is_subtest() || report.origin.as_ref() != Some(origin) {
                        continue;
                    }
                    &report.phase
                }
                ReportEntry::Published(report) => {
                    let report = report.borrow(py);
                    if report.is_subtest() || report.origin.as_ref() != Some(origin) {
                        continue;
                    }
                    match report.phase.phase {
                        ExecutionPhase::Call => {
                            return report.phase.outcome.report_outcome().as_str() == "passed";
                        }
                        ExecutionPhase::Setup => return true,
                        ExecutionPhase::Teardown => continue,
                    }
                }
            };
            match phase.phase {
                ExecutionPhase::Call => return phase.outcome.report_outcome().as_str() == "passed",
                ExecutionPhase::Setup => return true,
                ExecutionPhase::Teardown => {}
            }
        }
        true
    }

    fn since(&self, start: usize) -> PyResult<&[ReportId]> {
        self.reports.get(start..).ok_or_else(|| {
            PyValueError::new_err("test protocol report history was reset during dispatch")
        })
    }

    pub(in crate::independent) fn has_reported_result_since(
        &self,
        py: Python<'_>,
        start: usize,
        nodeid: &str,
    ) -> PyResult<bool> {
        for id in self.since(start)? {
            if self.with_row(py, *id, |report| {
                report.nodeid.as_ref() != nodeid
                    || report.is_subtest()
                    || report.phase.phase != ExecutionPhase::Setup
                    || report.phase.outcome != ExecutionOutcome::Passed
            })? {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

impl Context {
    pub(in crate::independent) fn bind_terminal_result(
        &self,
        py: Python<'_>,
        result: &Py<crate::results::NativeRunResult>,
        origin: terminal::session::SummaryOrigin,
    ) -> PyResult<()> {
        if self.invocation.is_worker() {
            return Ok(());
        }
        let reporter = self
            .registry
            .bind(py)
            .call_method1("get_plugin", ("terminalreporter",))?;
        if reporter.is_none() {
            return Ok(());
        }
        let history = self
            .session
            .bind(py)
            .getattr("_vfy_report")?
            .cast_into::<NativeSessionReports>()?;
        let reference = pyo3::types::PyWeakrefReference::new(&history)?.unbind();
        history.borrow_mut().summary.bind(py, result, origin)?;
        let previous = self.terminal_summary.borrow_mut().replace(reference);
        drop(previous);
        if matches!(origin, terminal::session::SummaryOrigin::WorkerMerge { .. }) {
            history.call_method1("terminal_project_result", (result, reporter))?;
        }
        Ok(())
    }

    pub(in crate::independent) fn close_terminal_result(&self, py: Python<'_>) -> PyResult<()> {
        let reference = self.terminal_summary.borrow_mut().take();
        let Some(history) = reference.and_then(|reference| reference.bind(py).upgrade()) else {
            return Ok(());
        };
        let history = history.cast_into::<NativeSessionReports>()?;
        let result = history.borrow_mut().summary.close();
        drop(result);
        Ok(())
    }
}
