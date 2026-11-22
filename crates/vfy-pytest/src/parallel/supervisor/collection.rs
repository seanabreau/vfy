//! Supervisor collection wait, adoption, and completion validation.

use super::ParallelSupervisor;
use crate::parallel::{
    COLLECTION_ABORT_DETAIL, MAX_PLAN_BYTES, POLL_INTERVAL, PRESPAWN_SETTLE_TIMEOUT,
    ParallelControl, ParallelPlan, ParallelScheduling, WorkerCollection,
    WorkerCollectionCompletion, collection_report_shapes, demand, first_mismatch, independent,
    io_error, partition, read_json, read_worker_collection, validate_plan, validate_plan_size,
    worker_exit_code, write_json_atomic,
};
use pyo3::exceptions::{PyKeyboardInterrupt, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::borrow::Cow;
use std::collections::BTreeMap;
use std::fs::{self};
use std::io::{self};
use std::process::ExitStatus;
use std::sync::Arc;
use std::thread;
use std::time::Instant;
use vfy_core::test_execution::{
    CollectionOutcome, RunRecord, compute_run_counts, merge_run_records,
};

impl ParallelSupervisor<'_> {
    pub(in crate::parallel) fn await_collection(
        &mut self,
        py: Python<'_>,
    ) -> PyResult<WorkerCollection> {
        loop {
            self.check_fresh_deadline(py)?;
            py.check_signals()?;
            for index in 0..self.children.len() {
                if let Some(child) = self.children[index].as_mut()
                    && let Some(status) = child.try_wait().map_err(io_error)?
                {
                    self.statuses[index] = Some(status);
                    self.children[index] = None;
                    if let Err(error) = self.check_exited_collection_worker(py, index, status) {
                        // Prespawned workers run their own startup, hooks, and shutdown;
                        // only a controller cancellation may cut them short.
                        if !error.is_instance_of::<PyKeyboardInterrupt>(py) {
                            self.settle_prespawned(py)?;
                        }
                        return Err(error);
                    }
                }
            }
            if (0..self.children.len()).all(|index| {
                self.directory
                    .path()
                    .join(format!("collection.{index}.json"))
                    .is_file()
            }) {
                let mut canonical =
                    read_worker_collection(&self.directory.path().join("collection.0.json"))?;
                if canonical.index != 0 {
                    return Err(PyValueError::new_err(
                        "worker zero published an invalid collection index",
                    ));
                }
                let report_shapes = collection_report_shapes(&canonical.reports)?;
                let mut disagreement = None;
                for index in 1..self.children.len() {
                    let candidate = read_worker_collection(
                        &self
                            .directory
                            .path()
                            .join(format!("collection.{index}.json")),
                    )?;
                    if candidate.index != index {
                        return Err(PyValueError::new_err(format!(
                            "worker {index} published an invalid collection index"
                        )));
                    }
                    if disagreement.is_none() && candidate.nodeids != canonical.nodeids {
                        let mismatch = first_mismatch(&canonical.nodeids, &candidate.nodeids);
                        disagreement = Some(format!(
                            "native parallel worker {index} collected a different test manifest at index {mismatch}"
                        ));
                    }
                    if disagreement.is_none()
                        && collection_report_shapes(&candidate.reports)? != report_shapes
                    {
                        disagreement = Some(format!(
                            "native parallel worker {index} collected different collection outcomes"
                        ));
                    }
                    // A failed import can change a worker's manifest. Preserve its
                    // collection failure before enforcing agreement for execution.
                    canonical.errors = canonical.errors.max(candidate.errors);
                    canonical.reports.extend(candidate.reports);
                    canonical.warnings.extend(candidate.warnings);
                    canonical.benchmark_fixture |= candidate.benchmark_fixture;
                }
                if self
                    .collection_error_policy
                    .allows_execution(canonical.errors)
                    && let Some(detail) = disagreement
                {
                    return Err(self.abort_error(py, detail, Some(1)));
                }
                return Ok(canonical);
            }
            py.detach(|| thread::sleep(POLL_INTERVAL));
        }
    }

    /// Fail unless worker `index` exited after publishing a collection that permits execution.
    fn check_exited_collection_worker(
        &mut self,
        py: Python<'_>,
        index: usize,
        status: ExitStatus,
    ) -> PyResult<()> {
        self.check_finished_worker(py, index, status)?;
        let path = self
            .directory
            .path()
            .join(format!("collection.{index}.json"));
        let collection = path
            .is_file()
            .then(|| read_worker_collection(&path))
            .transpose()?;
        if collection.as_ref().is_none_or(|collection| {
            self.collection_error_policy
                .allows_execution(collection.errors)
        }) {
            return Err(self.abort_error(
                py,
                format!(
                    "native parallel worker {index} exited during collection (status {status})"
                ),
                Some(worker_exit_code(status)),
            ));
        }
        Ok(())
    }

    /// Wait for prespawned workers to leave on their own after the controller aborted
    /// the run, so each finishes the lifecycle it already started. A worker still
    /// running at the deadline is killed by `discard_prespawned`.
    fn settle_prespawned(&mut self, py: Python<'_>) -> PyResult<()> {
        let started = Instant::now();
        while !self.prespawned.is_empty() && started.elapsed() < PRESPAWN_SETTLE_TIMEOUT {
            self.check_fresh_deadline(py)?;
            py.check_signals()?;
            let mut running = Vec::new();
            for (index, pid, mut child) in std::mem::take(&mut self.prespawned) {
                // A wait failure leaves the worker for `discard_prespawned` to stop.
                if matches!(child.try_wait(), Ok(None) | Err(_)) {
                    running.push((index, pid, child));
                } else if let Some(base) = &self.speculative_coverage_base {
                    let _ = std::fs::remove_file(format!("{base}.worker{index}"));
                }
            }
            self.prespawned = running;
            if !self.prespawned.is_empty() {
                py.detach(|| thread::sleep(POLL_INTERVAL));
            }
        }
        Ok(())
    }

    pub(in crate::parallel) fn completed_independent_collection(
        &self,
        collection: &WorkerCollection,
    ) -> PyResult<Option<RunRecord>> {
        if !self
            .directory
            .path()
            .join("collection-result.0.json")
            .is_file()
        {
            return Ok(None);
        }
        let identity = self
            .invocation_identity
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("native peer has no invocation identity"))?;
        let mut records = Vec::with_capacity(self.children.len());
        let mut reports = Vec::<vfy_core::test_execution::CollectionReport>::new();
        let mut report_indices = BTreeMap::<String, usize>::new();
        let mut deselected_nodeids = Vec::new();
        for index in 0..self.children.len() {
            let completion = self.authenticated_completions[index]
                .as_ref()
                .ok_or_else(|| {
                    PyValueError::new_err(
                        "independent collection result has no validated completion",
                    )
                })?;
            let mut record = independent::read_collection_record(
                self.directory.path(),
                index,
                identity,
                completion,
            )?;
            if index == 0 {
                deselected_nodeids = std::mem::take(&mut record.deselected_nodeids);
            }
            for report in &record.collection_reports {
                if let Some(index) = report_indices.get(&report.nodeid) {
                    let previous = &mut reports[*index];
                    if previous.outcome != CollectionOutcome::Failed
                        && report.outcome == CollectionOutcome::Failed
                    {
                        *previous = report.clone();
                    }
                } else {
                    report_indices.insert(report.nodeid.clone(), reports.len());
                    reports.push(report.clone());
                }
            }
            records.push(record);
        }
        let mut record =
            merge_run_records(records).map_err(|error| PyValueError::new_err(error.to_string()))?;
        record.collection_reports = reports;
        record.deselected_nodeids = deselected_nodeids;
        record.collected = collection.nodeids.len();
        record.counts = compute_run_counts(&record.cases, &record.collection_reports);
        self.validate_independent_worker_pids(&record)?;
        Ok(Some(record))
    }

    pub(in crate::parallel) fn adopt_collection(
        &mut self,
        mut nodeids: Vec<String>,
    ) -> PyResult<()> {
        if let Some(execution) = self.fresh_execution
            && !execution.tests.is_empty()
        {
            self.fresh_collection_nodeids = Some(nodeids.clone());
            let valid = self.invocation_identity.as_ref().is_some_and(|identity| {
                identity.permits_selected_collection(&nodeids, &execution.tests)
            });
            if valid {
                nodeids = vfy_core::mutation::mutation_collection_order(&nodeids, &execution.tests)
                    .ok_or_else(|| PyValueError::new_err("validated mutation selection changed"))?;
                self.fresh_tests.clone_from(&nodeids);
            } else {
                self.fresh_tests.clear();
            }
        }
        let width = self.children.len();
        let scheduling = self.plan.scheduling;
        if scheduling == ParallelScheduling::Demand && !self.independent_parallel() {
            return Err(PyValueError::new_err(
                "demand scheduling requires collected fork workers",
            ));
        }
        let mut assignments = partition(scheduling, &nodeids, width);
        assignments.resize_with(width, Vec::new);
        let plan = ParallelPlan {
            scheduling,
            nodeids,
            assignments,
            maxfail: self.plan.maxfail,
            initial_failures: self.plan.initial_failures,
        };
        validate_plan(&plan).map_err(PyValueError::new_err)?;
        validate_plan_size(&plan).map_err(PyValueError::new_err)?;
        if let Some(identity) = &self.invocation_identity {
            identity.check_plan(&plan)?;
        }
        if scheduling == ParallelScheduling::Demand {
            self.demand = Some(Arc::new(demand::DemandQueue::initialize_independent(
                self.directory.path(),
                plan.nodeids.len(),
                width,
            )?));
        }
        write_json_atomic(&self.directory.path().join("plan.json"), &plan).map_err(io_error)?;
        self.plan = Cow::Owned(plan);
        Ok(())
    }

    pub(in crate::parallel) fn abort_collection(&self) -> PyResult<()> {
        write_json_atomic(
            &self.directory.path().join("control.json"),
            &ParallelControl {
                state: "abort",
                detail: COLLECTION_ABORT_DETAIL,
            },
        )
        .map_err(io_error)
    }

    pub(super) fn read_collection_completion(
        &mut self,
        py: Python<'_>,
        index: usize,
        status: ExitStatus,
    ) -> PyResult<WorkerCollectionCompletion> {
        let path = self
            .directory
            .path()
            .join(format!("collection-finish.{index}.json"));
        let read_completion = || -> std::io::Result<WorkerCollectionCompletion> {
            if path.metadata()?.len() > MAX_PLAN_BYTES {
                return Err(std::io::Error::other(
                    "worker completion metadata exceeds the supported size",
                ));
            }
            read_json(&path)
        };
        let completion = match read_completion() {
            Ok(completion) => completion,
            Err(error) => {
                let phase = if !self.released {
                    "before all workers compiled"
                } else if !self
                    .directory
                    .path()
                    .join(format!("result.{index}.json"))
                    .is_file()
                {
                    "before publishing a result"
                } else {
                    "before completing shutdown"
                };
                let detail = format!(
                    "native parallel worker {index} exited {phase} without a valid collection completion (status {status}): {error}"
                );
                if error.kind() == io::ErrorKind::NotFound {
                    self.record_fresh_abrupt_exit(index, status, &detail);
                }
                return Err(self.abort_error(py, detail, Some(worker_exit_code(status))));
            }
        };
        if completion.infrastructure_failed {
            return Err(self.abort_error(py, format!("native parallel worker {index} reported an infrastructure failure (status {status})"), Some(3)));
        }
        let status_matches =
            status.code().map(i64::from) == Some(completion.returncode.rem_euclid(256));
        let interrupted = if completion.index == index && !status_matches {
            self.completed_interruption(index, &completion, status)?
        } else {
            None
        };
        if completion.index != index || (!status_matches && interrupted.is_none()) {
            return Err(self.abort_error(py, format!("native parallel worker {index} published a collection completion inconsistent with its process status or lifecycle (status {status})"), Some(worker_exit_code(status))));
        }
        if let Some(completed) = interrupted {
            self.completed_workers[index] = Some(completed);
        }
        Ok(completion)
    }

    fn completed_interruption(
        &self,
        index: usize,
        completion: &WorkerCollectionCompletion,
        status: ExitStatus,
    ) -> PyResult<Option<independent::ValidatedResult>> {
        let Some(delivered) = self.cancellation.delivered_interruption(index, status) else {
            return Ok(None);
        };
        if !self.cooperative_execution() {
            return Ok(None);
        }
        let (
            independent::PublishedResult::Execution,
            independent::ValidatedCompletion::Result(completed),
        ) = self.validate_completion(index, completion)?
        else {
            return Err(PyValueError::new_err(
                "interrupted worker has no completed independent execution result",
            ));
        };
        let completed = completed.with_delivered_interruption(delivered);
        let record = fs::read_to_string(self.directory.path().join(format!("result.{index}.json")))
            .map_err(io_error)?;
        self.validate_completed_record(index, &record, status, completed, completion)?;
        Ok(Some(completed))
    }

    pub(in crate::parallel) fn wait_collection_exits(
        &mut self,
        py: Python<'_>,
    ) -> PyResult<Vec<(String, String, usize, String)>> {
        while self.children.iter().any(Option::is_some) {
            self.check_fresh_deadline(py)?;
            py.check_signals()?;
            for index in 0..self.children.len() {
                if let Some(child) = self.children[index].as_mut()
                    && let Some(status) = child.try_wait().map_err(io_error)?
                {
                    self.children[index] = None;
                    self.statuses[index] = Some(status);
                    self.check_finished_worker(py, index, status)?;
                }
            }
            if self.children.iter().any(Option::is_some) {
                py.detach(|| thread::sleep(POLL_INTERVAL));
            }
        }
        let mut warnings = Vec::new();
        for index in 0..self.statuses.len() {
            let status = self.statuses[index]
                .ok_or_else(|| PyRuntimeError::new_err("collected worker has no exit status"))?;
            if self.invocation_identity.is_some() {
                let completion =
                    self.authenticated_completions[index]
                        .as_ref()
                        .ok_or_else(|| {
                            PyValueError::new_err(
                                "native collection peer has no validated completion",
                            )
                        })?;
                warnings.extend(completion.warnings.iter().cloned());
            } else {
                warnings.extend(self.read_collection_completion(py, index, status)?.warnings);
            }
        }
        Ok(warnings)
    }
}
