//! Run results, pytest report construction, and merging of worker records.

mod capture;
mod execution_record;
mod html;
mod interruption;
mod merge;
mod python;
mod transport;
mod warnings;

pub(crate) use capture::{
    CaptureSummary, append_phase_capture_sections, pytest_phase_report, pytest_phase_xfail,
};
pub(crate) use execution_record::warning_records;
pub(crate) use html::{collect_html_extras, parse_html_extras, publish_html_case};
pub(crate) use interruption::ParentInterruption;
pub(crate) use merge::{
    IndependentMergePolicy, ParallelResultMode, merge_collect_only_parallel_results,
    merge_demand_parallel_results, merge_independent_demand_parallel_results,
    merge_independent_parallel_results, merge_parallel_results,
};
pub(crate) use transport::{
    NativeRunResultTransport, WorkerResultSummary, validate_worker_observations,
    worker_declares_exitstatus, worker_result_summary,
};

use crate::reporting;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::collections::BTreeMap;
use vfy_core::test_execution::{
    CaseResult, ExecutionOutcome, ExecutionPhase, RunRecord, WarningRecord, compute_counts,
};
// Pytest uses -1 for a callable whose source line is unknown.
pub(crate) type ReportLocation = (String, Option<i64>, String);

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum CacheReportKind {
    Phase,
    Subtest,
}

fn phase_outcomes(
    record: &RunRecord,
) -> impl Iterator<Item = (&str, ExecutionPhase, ExecutionOutcome, f64)> + Clone {
    record.cases.iter().flat_map(|case| {
        case.phases.iter().map(move |phase| {
            (
                case.nodeid.as_str(),
                phase.phase,
                case.report_phase_outcome(phase),
                phase.duration_ms,
            )
        })
    })
}

#[pyclass(module = "vfy._native._pytest")]
pub struct NativeRunResult {
    record: RunRecord,
    warning_messages: Vec<(String, Py<PyAny>)>,
    report_locations: Option<Vec<(String, ReportLocation)>>,
    // In-process serial runs resolve locations only for summary cases, keyed by
    // their position in `record.cases`; worker transport uses `report_locations`.
    summary_locations: BTreeMap<usize, ReportLocation>,
    worker_junit_properties: reporting::WorkerJUnitProperties,
    worker_metadata: BTreeMap<String, String>,
    html_extras: BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
    html_errors: BTreeMap<String, String>,
    worker_collection_warnings: Vec<Vec<WarningRecord>>,
    worker_finish_warnings: Vec<Vec<WarningRecord>>,
    // A live phase publication retains pytest's failure representation for
    // `last_pytest_report`; transported and merged records carry text only.
    last_phase_representation: Option<Py<PyAny>>,
    // Supervisor-only authority; writable/deserialized records cannot retain it.
    worker_interruption: Option<crate::parallel::independent::ValidatedInterruption>,
    // Supervisor-only pending dispatch; never part of result transport or reports.
    parent_interruption: Option<ParentInterruption>,
    // Fresh supervisor authority is never serialized or reconstructed from fields.
    fresh_evidence: Option<crate::parallel::FreshEvidence>,
}

impl NativeRunResult {
    pub(crate) fn fresh_evidence(&self) -> Option<&crate::parallel::FreshEvidence> {
        self.fresh_evidence.as_ref()
    }

    pub(crate) fn retain_fresh_evidence(&mut self, evidence: crate::parallel::FreshEvidence) {
        self.fresh_evidence = Some(evidence);
    }

    pub(crate) fn record(&self) -> &RunRecord {
        &self.record
    }

    pub(crate) fn report_location(&self, index: usize) -> PyResult<ReportLocation> {
        let case =
            self.record.cases.get(index).ok_or_else(|| {
                PyValueError::new_err("terminal report case index is out of range")
            })?;
        let missing = || {
            PyRuntimeError::new_err(format!(
                "native terminal capture report has no location: {}",
                case.nodeid
            ))
        };
        if let Some(locations) = &self.report_locations {
            let (nodeid, location) = locations.get(index).ok_or_else(missing)?;
            if nodeid != &case.nodeid {
                return Err(PyRuntimeError::new_err(format!(
                    "native terminal capture report location does not match: {}",
                    case.nodeid
                )));
            }
            return Ok(location.clone());
        }
        self.summary_locations
            .get(&index)
            .cloned()
            .ok_or_else(missing)
    }

    pub(crate) fn record_mut(&mut self) -> &mut RunRecord {
        &mut self.record
    }

    pub(crate) fn worker_interruption(
        &self,
    ) -> Option<crate::parallel::independent::ValidatedInterruption> {
        self.worker_interruption
    }

    pub(crate) fn retain_worker_interruption(
        &mut self,
        interruption: crate::parallel::independent::ValidatedInterruption,
    ) {
        self.worker_interruption = Some(interruption);
    }

    pub(crate) fn retain_parent_interruption(
        &mut self,
        interruption: ParentInterruption,
    ) -> Result<(), ParentInterruption> {
        if self.parent_interruption.is_some() {
            return Err(interruption);
        }
        self.parent_interruption = Some(interruption);
        Ok(())
    }

    pub(crate) fn has_parent_interruption(&self) -> bool {
        self.parent_interruption.is_some()
    }

    pub(crate) fn take_parent_interruption(&mut self) -> Option<ParentInterruption> {
        self.parent_interruption.take()
    }

    pub(crate) fn frozen_junit_properties(&self) -> reporting::WorkerJUnitProperties {
        self.worker_junit_properties.clone()
    }
}

impl NativeRunResult {
    /// Render the execution record with the shared HTML writer.
    ///
    /// # Errors
    /// Returns validation or atomic-publication errors from the renderer.
    pub fn write_html(
        &self,
        writer: &mut vfy_core::PytestHtmlReport,
        payload: &str,
    ) -> Result<(), vfy_core::CoreError> {
        if let Some((nodeid, error)) = self.html_errors.first_key_value() {
            return Err(vfy_core::CoreError::InvalidInput(format!(
                "invalid HTML attachment for {nodeid}: {error}"
            )));
        }
        writer.write_native(payload, &self.record, &self.html_extras)
    }

    pub(crate) fn set_warning_messages(&mut self, messages: Vec<(String, Py<PyAny>)>) {
        self.warning_messages = messages;
    }

    pub(crate) fn set_last_phase_representation(&mut self, representation: Option<Py<PyAny>>) {
        self.last_phase_representation = representation;
    }

    pub(crate) fn set_worker_metadata(&mut self, metadata: BTreeMap<String, String>) {
        self.worker_metadata = metadata;
    }

    pub(crate) fn set_html_extras(
        &mut self,
        extras: BTreeMap<String, Vec<vfy_core::PytestHtmlExtra>>,
        errors: BTreeMap<String, String>,
    ) {
        self.html_extras = extras;
        self.html_errors = errors;
    }

    pub(crate) fn set_report_locations(
        &mut self,
        locations: Vec<(String, ReportLocation)>,
    ) -> PyResult<()> {
        if locations.len() != self.record.cases.len()
            || locations
                .iter()
                .zip(&self.record.cases)
                .any(|((nodeid, _), case)| nodeid != &case.nodeid)
        {
            return Err(PyValueError::new_err(
                "native report locations do not match the completed cases",
            ));
        }
        self.report_locations = Some(locations);
        Ok(())
    }

    pub(crate) fn set_summary_locations(&mut self, locations: BTreeMap<usize, ReportLocation>) {
        self.summary_locations = locations;
    }

    pub(crate) fn restrict_collected_to_claims(&mut self, claimed: usize) -> PyResult<()> {
        if claimed < self.record.cases.len() || claimed > self.record.collected {
            return Err(PyValueError::new_err(
                "native demand result has inconsistent claimed count",
            ));
        }
        self.record.collected = claimed;
        Ok(())
    }

    pub(crate) fn project_independent_claims(&mut self, claimed: &[usize]) -> PyResult<()> {
        let invalid =
            || PyValueError::new_err("independent demand result disagrees with its actual claims");
        let observed = self.record.observations.as_ref().ok_or_else(invalid)?;
        let indices = claimed
            .iter()
            .copied()
            .collect::<std::collections::BTreeSet<_>>();
        if self.record.engine != "vfy-independent-v1"
            || indices.len() != claimed.len()
            || observed.case_markers.len() != self.record.collected
            || claimed.iter().any(|index| *index >= self.record.collected)
            || self.record.cases.len() > claimed.len()
            || self.record.cases.iter().zip(claimed).any(|(case, index)| {
                case.test_id().is_none_or(|test| test.index() != *index)
                    || case.nodeid != observed.case_markers[*index].nodeid
            })
        {
            return Err(invalid());
        }
        let markers = claimed
            .iter()
            .map(|index| observed.case_markers[*index].clone())
            .collect();
        if let Some(observed) = &mut self.record.observations {
            observed.case_markers = markers;
        }
        self.record.collected = claimed.len();
        Ok(())
    }

    pub(crate) fn transport(&self) -> NativeRunResultTransport<'_> {
        NativeRunResultTransport {
            record: self.record(),
            report_locations: &self.report_locations,
            html_extras: &self.html_extras,
            html_errors: &self.html_errors,
        }
    }

    pub(crate) fn phase_outcomes(
        &self,
    ) -> impl Iterator<Item = (&str, ExecutionPhase, ExecutionOutcome, f64)> + Clone {
        phase_outcomes(&self.record)
    }

    pub(crate) fn cache_phase_outcomes(
        &self,
    ) -> impl Iterator<Item = (&str, ExecutionPhase, ExecutionOutcome, CacheReportKind)> + Clone
    {
        self.record.cases.iter().flat_map(|case| {
            case.phases.iter().flat_map(move |phase| {
                let subtest_count = if phase.phase == ExecutionPhase::Call {
                    case.subtests().len()
                } else {
                    0
                };
                let subtests = case
                    .subtests()
                    .iter()
                    .take(subtest_count)
                    .map(move |subtest| {
                        (
                            case.nodeid.as_str(),
                            ExecutionPhase::Call,
                            subtest.outcome,
                            CacheReportKind::Subtest,
                        )
                    });
                subtests.chain(std::iter::once((
                    case.nodeid.as_str(),
                    phase.phase,
                    phase.outcome,
                    CacheReportKind::Phase,
                )))
            })
        })
    }

    pub(crate) fn transport_json(&self) -> PyResult<String> {
        serde_json::to_string(&self.transport())
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))
    }

    pub(crate) fn collected_count(&self) -> usize {
        self.record.collected
    }
}

pub(crate) fn partial_native_result(
    cases: Vec<CaseResult>,
    warnings: Vec<WarningRecord>,
    report_locations: Option<Vec<(String, ReportLocation)>>,
    collected: usize,
    duration_ms: f64,
) -> NativeRunResult {
    let counts = compute_counts(&cases);
    native_run_result(
        RunRecord {
            internal_errors: Vec::new(),
            collection_reports: Vec::new(),
            deselected_nodeids: Vec::new(),
            engine: "pytest-runner-native-v1".to_owned(),
            observations: None,
            exitstatus: None,
            execution_identity: None,
            collected,
            counts,
            duration_ms,
            warnings,
            cases,
        },
        report_locations,
    )
}

pub(crate) fn native_run_result(
    record: RunRecord,
    report_locations: Option<Vec<(String, ReportLocation)>>,
) -> NativeRunResult {
    NativeRunResult {
        record,
        warning_messages: Vec::new(),
        report_locations,
        summary_locations: BTreeMap::new(),
        worker_junit_properties: reporting::WorkerJUnitProperties::default(),
        worker_metadata: BTreeMap::new(),
        html_extras: BTreeMap::new(),
        html_errors: BTreeMap::new(),
        worker_collection_warnings: Vec::new(),
        worker_finish_warnings: Vec::new(),
        last_phase_representation: None,
        worker_interruption: None,
        parent_interruption: None,
        fresh_evidence: None,
    }
}

#[cfg(test)]
mod tests;
