//! Python methods of the native run result.

use super::capture::{
    CaptureScope, CaptureStream, CaptureSummary, FailureEntry, FailureKind, PytestPhaseReport,
    TerminalCaptureProjection, TerminalCaseSections, append_phase_capture_sections,
    collection_failure_entries, extend_capture_sections, internal_failure_entries,
    nodeid_head_line, pytest_phase_report, terminal_detail,
};
use super::execution_record::{
    parse_execution_record_identity, parse_execution_record_warnings, warning_records,
};
use super::transport::{
    NativeRunResultTransport, NativeRunResultTransportOwned, RunRecordWriteError,
    write_run_record_json,
};
use super::warnings::merge_session_warnings;
use super::{NativeRunResult, native_run_result};
use crate::reporting;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use std::cell::LazyCell;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;
use vfy_core::selection::FailedPhase;
use vfy_core::test_execution::{
    CaseReportEvent, CollectionOutcome, ExecutionOutcome, ExecutionPhase, WarningRecord,
};

#[pymethods]
impl NativeRunResult {
    fn fresh_mutation_fingerprints(
        slf: &Bound<'_, Self>,
        request: &str,
    ) -> PyResult<Option<(String, BTreeMap<String, String>)>> {
        crate::parallel::fresh_cache_fingerprints(&slf.borrow(), request)
    }

    fn read_fresh_mutation_cache(
        slf: &Bound<'_, Self>,
        path: &str,
        request: &str,
    ) -> PyResult<String> {
        crate::parallel::read_fresh_cache(&slf.borrow(), path, request)
    }

    fn write_fresh_mutation_cache(
        slf: &Bound<'_, Self>,
        path: &str,
        request: &str,
        results: Vec<Py<Self>>,
    ) -> PyResult<()> {
        crate::parallel::write_fresh_cache(slf, path, request, results)
    }

    #[staticmethod]
    pub(super) fn from_independent_json(json: &str) -> PyResult<Self> {
        let document: NativeRunResultTransportOwned =
            serde_json::from_str(json).map_err(|error| PyValueError::new_err(error.to_string()))?;
        if document.engine != "vfy-independent-v1" {
            return Err(PyValueError::new_err(
                "expected an independent native result",
            ));
        }
        let record = document.into_record();
        vfy_core::test_execution::validate_run_record(&record)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        Ok(native_run_result(record, None))
    }

    #[getter]
    pub(crate) fn collection_nodeids(&self) -> Vec<String> {
        self.record.observations.as_ref().map_or_else(
            || {
                self.record
                    .cases
                    .iter()
                    .map(|case| case.nodeid.clone())
                    .collect()
            },
            |observed| {
                observed
                    .case_markers
                    .iter()
                    .map(|case| case.nodeid.clone())
                    .collect()
            },
        )
    }

    #[getter]
    fn collected(&self) -> usize {
        self.record.collected
    }

    #[getter]
    fn passed(&self) -> usize {
        self.record.counts.passed
    }

    #[getter]
    fn exitstatus(&self) -> Option<i32> {
        self.record.exitstatus
    }

    #[getter]
    fn failed(&self) -> usize {
        self.record.counts.failed
    }

    #[getter]
    fn errors(&self) -> usize {
        self.record.counts.errors
    }

    #[getter]
    pub(super) fn collection_errors(&self) -> usize {
        self.record
            .collection_reports
            .iter()
            .filter(|report| report.outcome == CollectionOutcome::Failed)
            .count()
    }

    #[getter]
    pub(super) fn collection_skipped(&self) -> usize {
        self.record
            .collection_reports
            .iter()
            .filter(|report| report.outcome == CollectionOutcome::Skipped)
            .count()
    }

    #[getter]
    fn skipped(&self) -> usize {
        self.record.counts.skipped
    }

    #[getter]
    fn xfailed(&self) -> usize {
        self.record.counts.xfailed
    }

    #[getter]
    fn xpassed(&self) -> usize {
        self.record.counts.xpassed
    }

    #[getter]
    fn duration_ms(&self) -> f64 {
        self.record.duration_ms
    }

    /// Project a completed native batch into pytest's report protocol in one call.
    pub(super) fn pytest_reports(&self, py: Python<'_>) -> PyResult<Vec<PytestPhaseReport<'_>>> {
        let mut reports = Vec::new();
        for case in &self.record.cases {
            let mut sections = Vec::new();
            for phase in &case.phases {
                append_phase_capture_sections(&mut sections, phase);
                reports.push(pytest_phase_report(
                    py,
                    &case.nodeid,
                    phase,
                    sections.clone(),
                    None,
                )?);
            }
        }
        Ok(reports)
    }

    /// Project the final phase; the paired publisher reads live pytest capture sections.
    fn last_pytest_report(&self, py: Python<'_>) -> PyResult<PytestPhaseReport<'_>> {
        let (case, phase) = self
            .record
            .cases
            .iter()
            .rev()
            .find_map(|case| case.phases.last().map(|phase| (case, phase)))
            .ok_or_else(|| PyValueError::new_err("native result contains no pytest phase"))?;
        pytest_phase_report(
            py,
            &case.nodeid,
            phase,
            Vec::new(),
            self.last_phase_representation.as_ref(),
        )
    }

    #[staticmethod]
    pub(super) fn merge_json_results(json_records: Vec<String>) -> PyResult<Self> {
        let mut documents = Vec::with_capacity(json_records.len());
        for (index, json) in json_records.into_iter().enumerate() {
            let document: NativeRunResultTransportOwned =
                serde_json::from_str(&json).map_err(|error| {
                    PyValueError::new_err(format!(
                        "invalid native run result at index {index}: {error}"
                    ))
                })?;
            if !matches!(
                document.engine.as_str(),
                "pytest-runner-native-v1" | "vfy-independent-v1"
            ) {
                return Err(PyValueError::new_err(format!(
                    "native run result at index {index} uses unsupported engine {:?}",
                    document.engine
                )));
            }
            documents.push(document);
        }

        let has_report_locations = documents
            .iter()
            .any(|document| document.report_locations.is_some());
        let all_report_locations = documents
            .iter()
            .all(|document| document.report_locations.is_some());
        if has_report_locations && !all_report_locations {
            return Err(PyValueError::new_err(
                "native run results have inconsistent report locations",
            ));
        }
        let mut report_locations = all_report_locations.then(Vec::new);
        let mut records = Vec::with_capacity(documents.len());
        let mut html_extras = BTreeMap::new();
        let mut html_errors = BTreeMap::new();
        for (index, mut document) in documents.into_iter().enumerate() {
            if let Some(locations) = document.report_locations.take() {
                if locations.len() != document.cases.len()
                    || locations
                        .iter()
                        .zip(&document.cases)
                        .any(|((nodeid, _), case)| nodeid != &case.nodeid)
                {
                    return Err(PyValueError::new_err(format!(
                        "native run result at index {index} has misaligned report locations"
                    )));
                }
                report_locations
                    .as_mut()
                    .ok_or_else(|| {
                        PyValueError::new_err(
                            "native run results have inconsistent report locations",
                        )
                    })?
                    .extend(locations);
            }
            document.merge_html_records(&mut html_extras, &mut html_errors)?;
            records.push(document.into_record());
        }
        let record = vfy_core::test_execution::merge_run_records(records)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        let mut result = native_run_result(record, report_locations);
        result.html_extras = html_extras;
        result.html_errors = html_errors;
        Ok(result)
    }

    fn bind_execution_record_identity(&mut self, value: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.record.execution_identity.is_some() {
            return Err(PyValueError::new_err(
                "native execution record identity is already bound",
            ));
        }
        self.record.execution_identity = Some(parse_execution_record_identity(value)?);
        Ok(())
    }

    fn publish_execution_record(
        &mut self,
        path: &str,
        warnings: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if self.record.execution_identity.is_none() {
            return Err(PyValueError::new_err(
                "native execution record has no trusted outer-session identity",
            ));
        }
        self.record.warnings = parse_execution_record_warnings(warnings)?;
        self.write_json(path)
    }

    fn prepend_warning_records(&mut self, warnings: Vec<(String, String, usize, String)>) {
        if warnings.is_empty() {
            return;
        }
        let records = warnings
            .into_iter()
            .map(|(message, filename, lineno, nodeid)| WarningRecord {
                message,
                nodeid,
                filename,
                lineno,
                ..WarningRecord::default()
            });
        drop(self.record.warnings.splice(0..0, records));
    }

    fn merge_worker_warning_records(
        &mut self,
        warnings: Vec<(String, String, usize, String)>,
        when: &str,
    ) -> PyResult<Vec<(String, String, usize, String)>> {
        let groups = match when {
            "collect" => &mut self.worker_collection_warnings,
            "finish" => &mut self.worker_finish_warnings,
            _ => {
                return Err(PyValueError::new_err(
                    "worker warning phase must be collect or finish",
                ));
            }
        };
        let controller = warning_records(warnings);
        let records = merge_session_warnings(&std::mem::take(groups), &controller);
        let projection = records
            .iter()
            .map(|warning| {
                (
                    warning.message.clone(),
                    warning.filename.clone(),
                    warning.lineno,
                    warning.nodeid.clone(),
                )
            })
            .collect();
        if when == "collect" {
            drop(self.record.warnings.splice(0..0, records));
        } else {
            self.record.warnings.extend(records);
            self.record.warnings.extend(controller);
        }
        Ok(projection)
    }

    pub(super) fn html_results_json(&self) -> PyResult<String> {
        if let Some((nodeid, error)) = self.html_errors.first_key_value() {
            return Err(PyValueError::new_err(format!(
                "invalid HTML attachment for {nodeid}: {error}"
            )));
        }
        vfy_core::native_pytest_html_results_json(&self.record, &self.html_extras)
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))
    }

    pub(super) fn write_vfy_result(&self, path: &str, infrastructure_failed: bool) -> PyResult<()> {
        let independent = self.record.engine == "vfy-independent-v1";
        let reports = self
            .record
            .cases
            .iter()
            .flat_map(|case| {
                let events = case.report_events();
                let use_events = independent && !case.subtests().is_empty() && !events.is_empty();
                let count = if use_events {
                    events.len()
                } else {
                    case.phases.len()
                };
                (0..count).filter_map(move |index| {
                    match use_events
                        .then(|| events.get(index))
                        .flatten()
                        .unwrap_or(CaseReportEvent::Phase(index))
                    {
                        CaseReportEvent::Phase(index) => {
                            let phase = &case.phases[index];
                            Some((
                                case.nodeid.as_str(),
                                phase.phase,
                                case.report_phase_outcome(phase),
                                phase.duration_ms,
                            ))
                        }
                        CaseReportEvent::Subtest(index) => {
                            let subtest = &case.subtests()[index];
                            subtest.report.as_ref().map(|report| {
                                (
                                    case.nodeid.as_str(),
                                    // Builtin subtest reports use call even during fixture teardown.
                                    ExecutionPhase::Call,
                                    subtest.outcome,
                                    report.duration_ms,
                                )
                            })
                        }
                    }
                })
            })
            .map(|(nodeid, phase, outcome, duration_ms)| {
                let phase = match phase {
                    ExecutionPhase::Setup => FailedPhase::Setup,
                    ExecutionPhase::Call => FailedPhase::Call,
                    ExecutionPhase::Teardown => FailedPhase::Teardown,
                };
                (nodeid, phase, outcome.report_outcome(), duration_ms)
            });
        let mut run = vfy_core::selection::normalize_test_phases(reports, infrastructure_failed)
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        if independent {
            run.infrastructure_failed |= run.tests.len() != self.record.collected;
        }
        vfy_core::selection::write_test_run_result(Path::new(path), &run)
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))
    }

    /// Consume live pytest warning objects after their native phase has completed.
    pub(crate) fn take_warning_messages(&mut self) -> Vec<(String, Py<PyAny>)> {
        std::mem::take(&mut self.warning_messages)
    }

    fn warning_records(&self) -> Vec<(String, String, usize, String)> {
        self.record
            .warnings
            .iter()
            .map(|record| {
                (
                    record.message.clone(),
                    record.filename.clone(),
                    record.lineno,
                    record.nodeid.clone(),
                )
            })
            .collect()
    }

    #[pyo3(signature = (reportchars, *, subtest_verbosity = 1))]
    fn terminal_capture_reports(
        &self,
        reportchars: &str,
        subtest_verbosity: i32,
    ) -> PyResult<TerminalCaptureProjection> {
        let summary = CaptureSummary::from_reportchars(reportchars);
        let mut reports = Vec::new();
        let mut subtest_sections = Vec::new();
        for (index, case) in self.record.cases.iter().enumerate() {
            let sections =
                (!case.subtests().is_empty()).then(|| TerminalCaseSections::from_case(case));
            if let Some(sections) = &sections {
                subtest_sections.push((
                    case.nodeid.clone(),
                    sections.setup.clone(),
                    sections.teardown.clone(),
                ));
            }
            let call = summary.summary_call(case);
            let include_subtests = subtest_verbosity != 0 && summary.includes_subtests(case);
            if call.is_none() && !include_subtests {
                continue;
            }
            let location = self.report_location(index)?;
            let TerminalCaseSections {
                setup: setup_sections,
                call: call_sections,
                teardown: mut teardown_sections,
            } = sections.unwrap_or_else(|| TerminalCaseSections::from_case(case));
            if include_subtests {
                for subtest in case.subtests() {
                    if subtest.outcome != ExecutionOutcome::XPassed {
                        continue;
                    }
                    reports.push((
                        case.nodeid.clone(),
                        location.clone(),
                        Some(subtest.detail.clone()),
                        Some(subtest.context.clone()),
                        setup_sections.clone(),
                        if call.is_none() {
                            // A failed parent still has one shared teardown
                            // report for its successful subtests.
                            std::mem::take(&mut teardown_sections)
                        } else {
                            Vec::new()
                        },
                    ));
                }
            }
            if let Some(call) = call {
                reports.push((
                    case.nodeid.clone(),
                    location,
                    (call.outcome == ExecutionOutcome::XPassed).then(|| call.detail.clone()),
                    None,
                    call_sections,
                    teardown_sections,
                ));
            }
        }
        Ok((reports, subtest_sections))
    }

    fn terminal_summary(&self, py: Python<'_>, options: &Bound<'_, PyAny>) -> PyResult<Py<PyDict>> {
        let summary = reporting::terminal_summary(&self.record, options)?;
        let result = PyDict::new(py);
        result.set_item("short_lines", summary.short_lines)?;
        result.set_item("duration_heading", summary.duration_heading)?;
        result.set_item("duration_lines", summary.duration_lines)?;
        result.set_item("summary_parts", summary.summary_parts)?;
        result.set_item("main_color", summary.main_color)?;
        Ok(result.unbind())
    }

    /// The `legacy` and `xunit1` families emit every case's file and line.
    /// `xunit2` needs a controller location only for a skipped phase without a recorded location.
    pub(super) fn required_junit_locations(&self, family: &str) -> PyResult<Option<Vec<String>>> {
        match family {
            "legacy" | "xunit1" => Ok(None),
            "xunit2" => Ok(Some(
                self.record
                    .cases
                    .iter()
                    .filter(|case| {
                        case.phases.iter().any(|phase| {
                            phase.outcome == ExecutionOutcome::Skipped
                                && phase.skip_location.is_none()
                        })
                    })
                    .map(|case| case.nodeid.clone())
                    .collect(),
            )),
            _ => Err(PyValueError::new_err(format!(
                "invalid junit_family value: {family:?}"
            ))),
        }
    }

    pub(super) fn junit_locations(
        &self,
        family: &str,
    ) -> PyResult<BTreeMap<String, (String, Option<i64>)>> {
        let required = self.required_junit_locations(family)?;
        let requested = required
            .as_ref()
            .map(|nodeids| nodeids.iter().collect::<BTreeSet<_>>());
        Ok(self
            .report_locations
            .iter()
            .flatten()
            .filter(|(nodeid, _)| {
                requested
                    .as_ref()
                    .is_none_or(|nodeids| nodeids.contains(nodeid))
            })
            .map(|(nodeid, (file, line, _))| (nodeid.clone(), (file.clone(), *line)))
            .collect())
    }

    /// Attach each testcase's recorded properties and XML attributes.
    ///
    /// `cases` maps a node ID to one `(call_failure, properties, attributes,
    /// teardown_unwritten)` entry
    /// per executed case with that ID, in execution order.
    fn set_junit_case_data(&mut self, cases: &Bound<'_, PyDict>) -> PyResult<()> {
        let mut entries = HashMap::new();
        for (nodeid, values) in cases.iter() {
            let nodeid: String = nodeid.extract()?;
            let mut occurrences = Vec::new();
            for value in values.try_iter()? {
                let (call_failure, properties, attributes, teardown_unwritten) = value?
                    .extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>, Bound<'_, PyAny>, bool)>()?;
                let call_failure = if call_failure.is_none() {
                    None
                } else {
                    Some(reporting::parse_properties(&call_failure)?.into_boxed_slice())
                };
                occurrences.push((
                    teardown_unwritten,
                    call_failure,
                    reporting::parse_properties(&properties)?.into_boxed_slice(),
                    reporting::parse_properties(&attributes)?.into_boxed_slice(),
                ));
            }
            entries.insert(nodeid, occurrences.into_iter());
        }
        for case in &mut self.record.cases {
            let Some((teardown_unwritten, call_failure, properties, attributes)) =
                entries.get_mut(&case.nodeid).and_then(Iterator::next)
            else {
                continue;
            };
            case.set_call_failure_properties(call_failure);
            case.set_teardown_testcase_unwritten(teardown_unwritten);
            case.set_user_properties(properties);
            case.set_xml_attributes(attributes);
        }
        Ok(())
    }

    /// Retain a worker's testsuite properties for the controller's document.
    fn set_worker_junit_properties(&mut self, properties: &Bound<'_, PyAny>) -> PyResult<()> {
        self.worker_junit_properties.global_properties = reporting::parse_properties(properties)?;
        Ok(())
    }

    fn write_junit(slf: &Bound<'_, Self>, path: &str, options: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = slf.py();
        reporting::create_junit_parent(py, Path::new(path))?;
        // Options are Python objects; converting them may re-enter this result.
        let options = reporting::JUnitOptions::from_python(options)?;
        let result = slf.borrow();
        reporting::write_junit_atomic(
            py,
            Path::new(path),
            &result.record,
            options,
            &result.worker_junit_properties,
        )
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        for (_, category) in &self.warning_messages {
            visit.call(category)?;
        }
        visit.call(&self.last_phase_representation)?;
        if let Some(interruption) = &self.parent_interruption {
            interruption.traverse(&visit)?;
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let previous = {
            let mut result = slf.borrow_mut();
            (
                std::mem::take(&mut result.warning_messages),
                result.last_phase_representation.take(),
                result.parent_interruption.take(),
                result.fresh_evidence.take(),
            )
        };
        drop(previous);
    }

    pub(crate) fn worker_metadata_records(&self) -> Vec<(String, String)> {
        self.worker_metadata
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect()
    }

    fn cache_outcomes(&self) -> Vec<(String, String, String)> {
        self.phase_outcomes()
            .map(|(nodeid, phase, outcome, _)| {
                (
                    nodeid.to_owned(),
                    phase.as_str().to_owned(),
                    outcome.report_outcome().as_str().to_owned(),
                )
            })
            .collect()
    }

    fn mutation_failure(&self) -> Option<(String, String)> {
        self.record
            .first_failure()
            .map(|(nodeid, phase)| (nodeid.to_owned(), phase.as_str().to_owned()))
    }

    pub(super) fn to_json(&self) -> PyResult<String> {
        serde_json::to_string(&NativeRunResultTransport {
            record: &self.record,
            report_locations: &self.report_locations,
            html_extras: &self.html_extras,
            html_errors: &self.html_errors,
        })
        .map_err(|error| PyRuntimeError::new_err(error.to_string()))
    }

    /// Return pytest's `ERRORS`, `FAILURES`, or `XFAILURES` summary sections.
    #[pyo3(signature = (show_capture, *, include_subtests = true, outcome = "failed", subtest_verbosity = 1))]
    pub(super) fn failure_sections(
        &self,
        show_capture: &str,
        include_subtests: bool,
        outcome: &str,
        subtest_verbosity: i32,
    ) -> PyResult<Vec<(&'static str, Vec<FailureEntry>)>> {
        let failure_kind = match outcome {
            "failed" => FailureKind::Actual,
            "xfailed" => FailureKind::Expected,
            value => {
                return Err(PyValueError::new_err(format!(
                    "unsupported native failure outcome: {value}"
                )));
            }
        };
        let streams: &[CaptureStream] = match show_capture {
            "all" => &[
                CaptureStream::Stdout,
                CaptureStream::Stderr,
                CaptureStream::Log,
            ],
            "stdout" => &[CaptureStream::Stdout],
            "stderr" => &[CaptureStream::Stderr],
            "log" => &[CaptureStream::Log],
            "no" => &[],
            value => {
                return Err(PyValueError::new_err(format!(
                    "unsupported native failure capture mode: {value}"
                )));
            }
        };
        let mut errors = if outcome == "failed" {
            collection_failure_entries(&self.record, streams)
        } else {
            Vec::new()
        };
        let mut failures = Vec::new();
        for case in &self.record.cases {
            let head_line = LazyCell::new(|| {
                case.head_line()
                    .map_or_else(|| nodeid_head_line(&case.nodeid), str::to_owned)
            });
            // Subtest reports precede the report of the call that contains them.
            for subtest in case.subtests() {
                if !include_subtests
                    || !failure_kind.includes(subtest.outcome)
                    || (subtest_verbosity == 0 && subtest.outcome == ExecutionOutcome::XFailed)
                {
                    continue;
                }
                let mut sections = Vec::new();
                extend_capture_sections(&mut sections, case, streams, CaptureScope::Subtest);
                extend_capture_sections(&mut sections, case, streams, CaptureScope::PassedTeardown);
                failures.push((
                    format!("{} {}", &*head_line, subtest.context),
                    terminal_detail(&subtest.terminal_lead, &subtest.detail),
                    sections,
                ));
            }
            for phase in &case.phases {
                if !failure_kind.includes(phase.outcome) {
                    continue;
                }
                let mut sections = Vec::new();
                extend_capture_sections(&mut sections, case, streams, CaptureScope::Report(phase));
                if phase.outcome == ExecutionOutcome::Error {
                    errors.push((
                        format!("ERROR at {} of {}", phase.phase.as_str(), &*head_line),
                        terminal_detail(&phase.terminal_lead, &phase.detail),
                        sections,
                    ));
                } else {
                    extend_capture_sections(
                        &mut sections,
                        case,
                        streams,
                        CaptureScope::PassedTeardown,
                    );
                    failures.push((
                        (*head_line).clone(),
                        terminal_detail(&phase.terminal_lead, &phase.detail),
                        sections,
                    ));
                }
            }
        }
        let mut summary = Vec::new();
        if outcome == "failed" && !self.record.internal_errors.is_empty() {
            summary.push((
                "INTERNALERRORS",
                internal_failure_entries(&self.record, streams),
            ));
        }
        if !errors.is_empty() {
            summary.push(("ERRORS", errors));
        }
        if !failures.is_empty() {
            summary.push((
                match failure_kind {
                    FailureKind::Actual => "FAILURES",
                    FailureKind::Expected => "XFAILURES",
                },
                failures,
            ));
        }
        Ok(summary)
    }

    fn write_json(&self, destination: &str) -> PyResult<()> {
        write_run_record_json(Path::new(destination), &self.record)
            .map_err(RunRecordWriteError::into_pyerr)
    }
}
