//! Merging of parallel worker records into one run result.

use super::transport::{
    NativeParallelWorkerResult, matches_process_status, validate_worker_observations,
    worker_declares_exitstatus,
};
use super::{NativeRunResult, native_run_result};
use crate::reporting;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BTreeMap;
use vfy_core::test_execution::{CaseResult, Counts, RunRecord, compute_counts, compute_run_counts};

#[derive(Clone, Copy)]
enum ResultAssignments<'a> {
    Complete(&'a [Vec<usize>]),
    Demand(&'a [Vec<std::ops::Range<usize>>]),
}

#[derive(Clone, Copy)]
enum WorkerAssignment<'a> {
    Complete(&'a [usize]),
    Demand(&'a [std::ops::Range<usize>]),
}

#[derive(Clone, Copy)]
pub(crate) enum ParallelResultMode {
    Execute,
    CollectOnly,
}

#[derive(Clone, Copy)]
struct ParallelMergePolicy<'a> {
    maxfail: usize,
    initial_failures: usize,
    duration_ms: f64,
    mode: ParallelResultMode,
    completed_workers: Option<&'a [crate::parallel::independent::ValidatedResult]>,
}

impl<'a> ResultAssignments<'a> {
    fn len(self) -> usize {
        match self {
            Self::Complete(rows) => rows.len(),
            Self::Demand(rows) => rows.len(),
        }
    }
    fn worker(self, index: usize) -> WorkerAssignment<'a> {
        match self {
            Self::Complete(rows) => WorkerAssignment::Complete(&rows[index]),
            Self::Demand(rows) => WorkerAssignment::Demand(&rows[index]),
        }
    }
}

impl<'a> WorkerAssignment<'a> {
    fn len(self) -> usize {
        match self {
            Self::Complete(rows) => rows.len(),
            Self::Demand(ranges) => ranges.iter().map(ExactSizeIterator::len).sum(),
        }
    }
    fn is_empty(self) -> bool {
        self.len() == 0
    }

    fn iter(self) -> impl Iterator<Item = usize> + 'a {
        let (indices, ranges) = match self {
            Self::Complete(indices) => (indices, &[][..]),
            Self::Demand(ranges) => (&[][..], ranges),
        };
        indices
            .iter()
            .copied()
            .chain(ranges.iter().cloned().flatten())
    }
}

pub(crate) fn merge_parallel_results(
    worker_json: impl ExactSizeIterator<Item = PyResult<String>>,
    assignments: &[Vec<usize>],
    nodeids: &[String],
    exit_codes: &[Option<i32>],
    maxfail: usize,
    initial_failures: usize,
    duration_ms: f64,
) -> PyResult<NativeRunResult> {
    merge_parallel_results_inner(
        worker_json,
        ResultAssignments::Complete(assignments),
        nodeids,
        exit_codes,
        ParallelMergePolicy {
            maxfail,
            initial_failures,
            duration_ms,
            mode: ParallelResultMode::Execute,
            completed_workers: None,
        },
    )
}

/// Only the native supervisor can supply these authenticated worker completions.
#[derive(Clone, Copy)]
pub(crate) struct IndependentMergePolicy<'a> {
    pub(crate) maxfail: usize,
    pub(crate) initial_failures: usize,
    pub(crate) duration_ms: f64,
    pub(crate) mode: ParallelResultMode,
    pub(crate) completions: &'a [crate::parallel::independent::ValidatedResult],
}

pub(crate) fn merge_independent_parallel_results(
    worker_json: impl ExactSizeIterator<Item = PyResult<String>>,
    assignments: &[Vec<usize>],
    nodeids: &[String],
    exit_codes: &[Option<i32>],
    policy: IndependentMergePolicy<'_>,
) -> PyResult<NativeRunResult> {
    merge_parallel_results_inner(
        worker_json,
        ResultAssignments::Complete(assignments),
        nodeids,
        exit_codes,
        ParallelMergePolicy {
            maxfail: policy.maxfail,
            initial_failures: policy.initial_failures,
            duration_ms: policy.duration_ms,
            mode: policy.mode,
            completed_workers: Some(policy.completions),
        },
    )
}

pub(crate) fn merge_independent_demand_parallel_results(
    worker_json: impl ExactSizeIterator<Item = PyResult<String>>,
    assignments: &[Vec<std::ops::Range<usize>>],
    nodeids: &[String],
    exit_codes: &[Option<i32>],
    policy: IndependentMergePolicy<'_>,
) -> PyResult<NativeRunResult> {
    if !matches!(policy.mode, ParallelResultMode::Execute) {
        return Err(PyValueError::new_err(
            "independent demand merging requires executed worker results",
        ));
    }
    merge_parallel_results_inner(
        worker_json,
        ResultAssignments::Demand(assignments),
        nodeids,
        exit_codes,
        ParallelMergePolicy {
            maxfail: policy.maxfail,
            initial_failures: policy.initial_failures,
            duration_ms: policy.duration_ms,
            mode: policy.mode,
            completed_workers: Some(policy.completions),
        },
    )
}

pub(crate) fn merge_collect_only_parallel_results(
    worker_json: impl ExactSizeIterator<Item = PyResult<String>>,
    assignments: &[Vec<usize>],
    nodeids: &[String],
    exit_codes: &[Option<i32>],
    duration_ms: f64,
) -> PyResult<NativeRunResult> {
    merge_parallel_results_inner(
        worker_json,
        ResultAssignments::Complete(assignments),
        nodeids,
        exit_codes,
        ParallelMergePolicy {
            maxfail: 0,
            initial_failures: 0,
            duration_ms,
            mode: ParallelResultMode::CollectOnly,
            completed_workers: None,
        },
    )
}

pub(crate) fn merge_demand_parallel_results(
    worker_json: impl ExactSizeIterator<Item = PyResult<String>>,
    assignments: &[Vec<std::ops::Range<usize>>],
    nodeids: &[String],
    exit_codes: &[Option<i32>],
    maxfail: usize,
    initial_failures: usize,
    duration_ms: f64,
) -> PyResult<NativeRunResult> {
    merge_parallel_results_inner(
        worker_json,
        ResultAssignments::Demand(assignments),
        nodeids,
        exit_codes,
        ParallelMergePolicy {
            maxfail,
            initial_failures,
            duration_ms,
            mode: ParallelResultMode::Execute,
            completed_workers: None,
        },
    )
}

#[expect(
    clippy::too_many_lines,
    reason = "Worker identities, assignments, and counts must agree before results are merged"
)]
fn merge_parallel_results_inner(
    worker_json: impl ExactSizeIterator<Item = PyResult<String>>,
    assignments: ResultAssignments<'_>,
    nodeids: &[String],
    exit_codes: &[Option<i32>],
    policy: ParallelMergePolicy<'_>,
) -> PyResult<NativeRunResult> {
    let ParallelMergePolicy {
        maxfail,
        initial_failures,
        duration_ms,
        mode,
        completed_workers,
    } = policy;
    if worker_json.len() != assignments.len()
        || exit_codes.len() != assignments.len()
        || completed_workers.is_some_and(|workers| workers.len() != assignments.len())
    {
        return Err(PyValueError::new_err(
            "native parallel worker result count does not match its assignment plan",
        ));
    }
    let mut seen = vec![false; nodeids.len()];
    for worker in 0..assignments.len() {
        let assignment = assignments.worker(worker);
        for index in assignment.iter() {
            let Some(seen_index) = seen.get_mut(index) else {
                return Err(PyValueError::new_err(format!(
                    "native parallel assignment index {index} is out of range"
                )));
            };
            if *seen_index {
                return Err(PyValueError::new_err(format!(
                    "native parallel assignment repeats collection index {index}"
                )));
            }
            *seen_index = true;
        }
    }
    let unassigned = seen.iter().any(|assigned| !assigned);
    let authenticated_control = completed_workers
        .is_some_and(|workers| workers.iter().any(|worker| worker.interruption().is_some()));
    if unassigned
        && (matches!(assignments, ResultAssignments::Complete(_))
            || (maxfail == 0 && !authenticated_control))
    {
        return Err(PyValueError::new_err(
            "native parallel assignment plan omits collected tests",
        ));
    }

    let mut missing_without_control = unassigned && !authenticated_control;
    let independent_demand =
        matches!(assignments, ResultAssignments::Demand(_)) && completed_workers.is_some();
    let mut indexed_cases = Vec::new();
    let mut indexed_warnings = Vec::new();
    let mut indexed_markers = Vec::new();
    let mut collection_warnings = Vec::new();
    let mut finish_warnings = Vec::new();
    let mut worker_junit_properties = reporting::WorkerJUnitProperties::default();
    let mut worker_metadata = BTreeMap::new();
    let mut html_extras = BTreeMap::new();
    let mut html_errors = BTreeMap::new();
    let mut reports_present = None;
    let mut frontend = None;
    for (worker_index, json) in worker_json.enumerate() {
        let assignment = assignments.worker(worker_index);
        let json = json?;
        let mut worker: NativeParallelWorkerResult =
            serde_json::from_str(&json).map_err(|error| {
                PyValueError::new_err(format!(
                    "invalid native parallel worker result at index {worker_index}: {error}"
                ))
            })?;
        let completed = completed_workers.map(|workers| workers[worker_index]);
        if completed.is_none()
            && let Some(status) = exit_codes[worker_index]
            && !matches!(status, 0 | 1 | 5)
            && !worker_declares_exitstatus(&json, status)?
        {
            return Err(PyValueError::new_err(format!(
                "native worker {worker_index} has unsupported exit status {status}"
            )));
        }
        drop(json);
        let mut document = worker.result;
        validate_worker_observations(document.observations.as_ref())?;
        collection_warnings.push(worker.collection_warnings);
        finish_warnings.push(worker.finish_warnings);
        if !matches!(
            document.engine.as_str(),
            "pytest-runner-native-v1" | "vfy-independent-v1"
        ) {
            return Err(PyValueError::new_err(format!(
                "native worker {worker_index} uses unsupported engine {:?}",
                document.engine
            )));
        }
        if document.collected != assignment.len() {
            return Err(PyValueError::new_err(format!(
                "native worker {worker_index} reports {} collected tests for an assignment of {}",
                document.collected,
                assignment.len()
            )));
        }
        if document.cases.len() > assignment.len() {
            return Err(PyValueError::new_err(format!(
                "native worker {worker_index} returned more cases than assigned"
            )));
        }
        if let Some(completed) = completed {
            completed.validate_record(
                worker_index,
                &document.engine,
                document.exitstatus,
                exit_codes[worker_index],
            )?;
        } else {
            if document.engine == "vfy-independent-v1"
                && let Some(status) = document.exitstatus
                && exit_codes[worker_index]
                    .is_none_or(|actual| !matches_process_status(status, actual))
            {
                return Err(PyValueError::new_err(format!(
                    "native worker {worker_index} exit status differs from its session result"
                )));
            }
            match exit_codes[worker_index] {
                Some(code)
                    if document.engine == "vfy-independent-v1"
                        && document
                            .exitstatus
                            .is_some_and(|declared| matches_process_status(declared, code)) => {}
                Some(0) => {}
                Some(5)
                    if assignment.is_empty()
                        && document.cases.is_empty()
                        && document.counts == compute_counts(&[]) => {}
                Some(1)
                    if initial_failures > 0
                        || document.counts.failed > 0
                        || document.counts.errors > 0
                        || document.counts.xpassed > 0 => {}
                Some(1) => {
                    return Err(PyValueError::new_err(format!(
                        "native worker {worker_index} exited 1 without collection errors or failed cases"
                    )));
                }
                Some(code) => {
                    return Err(PyValueError::new_err(format!(
                        "native worker {worker_index} has unsupported exit status {code}"
                    )));
                }
                None => {
                    return Err(PyValueError::new_err(format!(
                        "native worker {worker_index} has no exit status"
                    )));
                }
            }
        }
        let interrupted = completed
            .and_then(crate::parallel::independent::ValidatedResult::interruption)
            .is_some();
        if interrupted && document.engine != "vfy-independent-v1" {
            return Err(PyValueError::new_err(
                "worker control witness requires an independent result",
            ));
        }
        missing_without_control |= !interrupted && document.cases.len() != assignment.len();
        match mode {
            ParallelResultMode::Execute
                if maxfail == 0 && !interrupted && document.cases.len() != assignment.len() =>
            {
                return Err(PyValueError::new_err(format!(
                    "native worker {worker_index} omitted cases without maxfail being enabled"
                )));
            }
            ParallelResultMode::CollectOnly => {
                if !document.cases.is_empty() {
                    return Err(PyValueError::new_err(format!(
                        "collect-only worker {worker_index} returned executed cases"
                    )));
                }
                let observed = document
                    .observations
                    .as_ref()
                    .filter(|_| document.engine == "vfy-independent-v1")
                    .ok_or_else(|| {
                        PyValueError::new_err(
                            "collect-only workers require independent collection observations",
                        )
                    })?;
                if observed.case_markers.len() != assignment.len()
                    || observed
                        .case_markers
                        .iter()
                        .zip(assignment.iter())
                        .any(|(item, index)| item.nodeid != nodeids[index])
                {
                    return Err(PyValueError::new_err(format!(
                        "collect-only worker {worker_index} returned different collection identities"
                    )));
                }
            }
            ParallelResultMode::Execute => (),
        }

        document.merge_html_records(&mut html_extras, &mut html_errors)?;
        let worker_reports = document.report_locations.take();
        match (reports_present, worker_reports.is_some()) {
            (Some(previous), current) if previous != current => {
                return Err(PyValueError::new_err(
                    "native parallel worker results have inconsistent report locations",
                ));
            }
            (None, current) => reports_present = Some(current),
            _ => {}
        }
        if let Some(locations) = worker_reports.as_ref()
            && (locations.len() != document.cases.len()
                || locations
                    .iter()
                    .zip(&document.cases)
                    .any(|((nodeid, _), case)| nodeid != &case.nodeid))
        {
            return Err(PyValueError::new_err(format!(
                "native worker {worker_index} has misaligned report locations"
            )));
        }

        let mut record = document.into_record();
        vfy_core::test_execution::validate_run_record(&record)
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        for (case, parent_index) in record.cases.iter().zip(assignment.iter()) {
            let wrong_identity = match case.test_id() {
                Some(test) => test.index() != parent_index,
                None => independent_demand,
            };
            if wrong_identity {
                return Err(PyValueError::new_err(
                    "native worker case source index disagrees with its actual assignment",
                ));
            }
            if case.nodeid != nodeids[parent_index] {
                return Err(PyValueError::new_err(format!(
                    "native worker {worker_index} returned {:?} for collection index {parent_index}, expected {:?}",
                    case.nodeid, nodeids[parent_index]
                )));
            }
        }
        if independent_demand {
            let observed = record.observations.as_mut().ok_or_else(|| {
                PyValueError::new_err("independent demand result has no collection observations")
            })?;
            if observed.case_markers.len() != assignment.len()
                || observed
                    .case_markers
                    .iter()
                    .zip(assignment.iter())
                    .any(|(marker, index)| marker.nodeid != nodeids[index])
            {
                return Err(PyValueError::new_err(
                    "independent demand markers disagree with the actual claims",
                ));
            }
            indexed_markers.extend(
                std::mem::take(&mut observed.case_markers)
                    .into_iter()
                    .zip(assignment.iter())
                    .map(|(marker, index)| (index, marker)),
            );
        }
        let case_offset = indexed_cases.len();
        for (local_index, (case, parent_index)) in std::mem::take(&mut record.cases)
            .into_iter()
            .zip(assignment.iter())
            .enumerate()
        {
            let report_location = worker_reports
                .as_ref()
                .map(|locations| locations[local_index].1.clone());
            indexed_cases.push((
                parent_index,
                case_offset + local_index,
                case,
                report_location,
            ));
        }
        let mut warning_parent_indices = BTreeMap::new();
        for index in assignment.iter() {
            warning_parent_indices
                .entry(nodeids[index].as_str())
                .or_insert(index);
        }
        for (warning_index, warning) in std::mem::take(&mut record.warnings).into_iter().enumerate()
        {
            let parent_index = warning_parent_indices
                .get(warning.nodeid.as_str())
                .copied()
                .unwrap_or(nodeids.len());
            indexed_warnings.push((parent_index, worker_index, warning_index, warning));
        }
        merge_frontend_observations(&mut frontend, record)?;
        worker_junit_properties
            .global_properties
            .append(&mut worker.junit_properties.global_properties);
        for (key, value) in worker.metadata {
            worker_metadata.insert(key, value);
        }
    }

    indexed_cases.sort_by_key(|(parent_index, _, _, _)| *parent_index);
    let mut cases = Vec::with_capacity(indexed_cases.len());
    let mut merged_report_locations = reports_present.unwrap_or(false).then(Vec::new);
    for (_, _, case, location) in indexed_cases {
        if let Some(locations) = merged_report_locations.as_mut() {
            locations.push((
                case.nodeid.clone(),
                location.ok_or_else(|| {
                    PyValueError::new_err("native worker omitted a passing-report location")
                })?,
            ));
        }
        cases.push(case);
    }
    indexed_warnings.sort_by_key(|(parent_index, worker_index, warning_index, _)| {
        (*parent_index, *worker_index, *warning_index)
    });
    let warnings = indexed_warnings
        .into_iter()
        .map(|(_, _, _, warning)| warning)
        .collect();
    let missing_cases = cases.len() != nodeids.len();
    let reported_failures = cases.iter().map(CaseResult::failure_count).sum::<usize>();
    if matches!(mode, ParallelResultMode::Execute)
        && missing_cases
        && missing_without_control
        && initial_failures.saturating_add(reported_failures) < maxfail
    {
        return Err(PyValueError::new_err(
            "native parallel result omitted cases before reaching maxfail",
        ));
    }
    let mut record = frontend.unwrap_or_else(|| RunRecord {
        internal_errors: Vec::new(),
        collection_reports: Vec::new(),
        deselected_nodeids: Vec::new(),
        engine: "pytest-runner-native-v1".to_owned(),
        observations: None,
        exitstatus: None,
        execution_identity: None,
        collected: 0,
        counts: Counts::default(),
        duration_ms: 0.0,
        warnings: Vec::new(),
        cases: Vec::new(),
    });
    record.collected = nodeids.len();
    record.counts = compute_run_counts(&cases, &record.collection_reports);
    record.duration_ms = duration_ms;
    record.warnings = warnings;
    record.cases = cases;
    if let Some(observed) = &mut record.observations {
        if independent_demand {
            indexed_markers.sort_by_key(|(index, _)| *index);
            observed.case_markers = indexed_markers
                .into_iter()
                .map(|(_, marker)| marker)
                .collect();
        } else {
            let order = nodeids
                .iter()
                .enumerate()
                .map(|(index, id)| (id.as_str(), index))
                .collect::<BTreeMap<_, _>>();
            observed.case_markers.sort_by_key(|markers| {
                order
                    .get(markers.nodeid.as_str())
                    .copied()
                    .unwrap_or(usize::MAX)
            });
        }
    }
    let mut result = native_run_result(record, merged_report_locations);
    result.worker_junit_properties = worker_junit_properties;
    result.worker_metadata = worker_metadata;
    result.html_extras = html_extras;
    result.html_errors = html_errors;
    result.worker_collection_warnings = collection_warnings;
    result.worker_finish_warnings = finish_warnings;
    Ok(result)
}

fn merge_frontend_observations(
    frontend: &mut Option<RunRecord>,
    mut worker: RunRecord,
) -> PyResult<()> {
    let Some(current) = frontend else {
        *frontend = Some(worker);
        return Ok(());
    };
    if current.engine != worker.engine
        || current.deselected_nodeids != worker.deselected_nodeids
        || current
            .collection_reports
            .iter()
            .map(|report| (&report.nodeid, report.outcome))
            .ne(worker
                .collection_reports
                .iter()
                .map(|report| (&report.nodeid, report.outcome)))
    {
        return Err(PyValueError::new_err(
            "native worker frontend observations differ",
        ));
    }
    if current.exitstatus.is_none_or(|code| code == 0) {
        current.exitstatus = worker.exitstatus.or(current.exitstatus);
    }
    current.internal_errors.append(&mut worker.internal_errors);
    match (&mut current.observations, worker.observations.take()) {
        (Some(current), Some(worker)) => current
            .merge(worker)
            .map_err(|error| PyValueError::new_err(error.to_string()))?,
        (None, None) => {}
        _ => {
            return Err(PyValueError::new_err(
                "native worker frontend observation presence differs",
            ));
        }
    }
    Ok(())
}
