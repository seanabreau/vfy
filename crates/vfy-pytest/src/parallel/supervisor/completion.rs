//! Supervisor completion validation, result merging, and output replay.

use super::ParallelSupervisor;
use crate::parallel::{
    CollectionRoute, WorkerCollectionCompletion, independent, replay_worker_stream_range,
    worker_exit_code,
};
use crate::results::{
    IndependentMergePolicy, NativeRunResult, ParallelResultMode,
    merge_collect_only_parallel_results, merge_demand_parallel_results,
    merge_independent_demand_parallel_results, merge_independent_parallel_results,
    merge_parallel_results, worker_declares_exitstatus,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use std::borrow::Cow;
use std::fs::{self};
use std::process::ExitStatus;

impl ParallelSupervisor<'_> {
    /// The collection plan is final once `adopt_collection` replaces the borrowed session plan.
    pub(super) fn plan_adopted(&self) -> bool {
        matches!(self.plan, Cow::Owned(_))
    }

    fn worker_pid(&self, index: usize) -> PyResult<u32> {
        self.worker_pids[index]
            .and_then(|pid| u32::try_from(pid.as_raw_pid()).ok())
            .ok_or_else(|| PyValueError::new_err("worker has no native process identity"))
    }

    /// Check a worker's completion against this run and the channel it published.
    pub(super) fn validate_completion(
        &self,
        index: usize,
        completion: &WorkerCollectionCompletion,
    ) -> PyResult<(
        independent::PublishedResult,
        independent::ValidatedCompletion,
    )> {
        let identity = self
            .invocation_identity
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("native peer has no invocation identity"))?;
        let published = independent::PublishedResult::read(self.directory.path(), index)?;
        let invocation = completion.invocation.as_ref().ok_or_else(|| {
            PyValueError::new_err("independent worker omitted invocation completion")
        })?;
        let demand_assignment = self.demand_assignment(index)?;
        let expected = independent::Expectation {
            identity,
            index,
            pid: self.worker_pid(index)?,
            assigned: demand_assignment.as_deref().or_else(|| {
                self.plan_adopted()
                    .then(|| self.plan.assignments.get(index))
                    .flatten()
                    .map(Vec::as_slice)
            }),
        };
        let validated = invocation.validate(expected, published, completion.returncode)?;
        Ok((published, validated))
    }

    fn demand_assignment(&self, index: usize) -> PyResult<Option<Vec<usize>>> {
        self.demand
            .as_ref()
            .map(|queue| {
                let assignments = queue.assignments(self.directory.path())?;
                Ok(assignments
                    .get(index)
                    .map(|ranges| ranges.iter().cloned().flatten().collect()))
            })
            .transpose()
            .map(Option::flatten)
    }

    /// Check a collection peer's record and that it was produced by the peer's process.
    pub(super) fn validate_collection_peer(
        &self,
        index: usize,
        completion: &WorkerCollectionCompletion,
    ) -> PyResult<independent::CollectionRecordOwned> {
        let identity = self
            .invocation_identity
            .as_ref()
            .ok_or_else(|| PyValueError::new_err("native peer has no invocation identity"))?;
        let pid = self.worker_pid(index)?;
        let record = independent::read_collection_document(
            self.directory.path(),
            index,
            identity,
            completion,
        )?;
        if record
            .record
            .observations
            .as_ref()
            .is_none_or(|observed| observed.execution_topology.worker_pids != [pid])
        {
            return Err(PyValueError::new_err(
                "native collection peer result disagrees with its process",
            ));
        }
        Ok(record)
    }

    pub(super) fn validate_independent_record(
        &self,
        index: usize,
        record: &str,
        status: ExitStatus,
    ) -> PyResult<()> {
        let completed = self.completed_workers[index].ok_or_else(|| {
            PyValueError::new_err("independent result omitted its validated completion")
        })?;
        let completion = self.authenticated_completions[index]
            .as_ref()
            .ok_or_else(|| {
                PyValueError::new_err("independent result omitted its validated completion")
            })?;
        self.validate_completed_record(index, record, status, completed, completion)
    }

    pub(super) fn validate_completed_record(
        &self,
        index: usize,
        record: &str,
        status: ExitStatus,
        completed: independent::ValidatedResult,
        completion: &WorkerCollectionCompletion,
    ) -> PyResult<()> {
        let identity = self.invocation_identity.as_ref().ok_or_else(|| {
            PyValueError::new_err("independent result omitted its invocation identity")
        })?;
        let assigned = if let Some(indices) = self.demand_assignment(index)? {
            indices.len()
        } else {
            self.plan
                .assignments
                .get(index)
                .ok_or_else(|| PyValueError::new_err("worker result has no adopted assignment"))?
                .len()
        };
        let summary = completion.validate_fresh_result(identity, record, assigned)?;
        completed.validate_record(index, &summary.engine, summary.exitstatus, status.code())
    }

    fn independent_completions(&self) -> PyResult<Option<Vec<independent::ValidatedResult>>> {
        self.invocation_identity
            .as_ref()
            .map(|_| {
                self.completed_workers
                    .iter()
                    .copied()
                    .collect::<Option<Vec<_>>>()
                    .ok_or_else(|| {
                        PyValueError::new_err("independent merge omitted a validated worker result")
                    })
            })
            .transpose()
    }

    fn independent_merge_policy<'completion>(
        &self,
        duration_ms: f64,
        completions: &'completion [independent::ValidatedResult],
    ) -> IndependentMergePolicy<'completion> {
        IndependentMergePolicy {
            maxfail: self.plan.maxfail,
            initial_failures: self.plan.initial_failures,
            duration_ms,
            mode: self.result_mode,
            completions,
        }
    }

    pub(in crate::parallel) fn merge_worker_results(
        &mut self,
        py: Python<'_>,
        statuses: &[ExitStatus],
        duration_ms: f64,
    ) -> PyResult<NativeRunResult> {
        let completed_workers = self.independent_completions()?;
        let mut read_failure = None;
        let records = statuses.iter().enumerate().map(|(index, status)| {
            let path = self.directory.path().join(format!("result.{index}.json"));
            let record = match fs::read_to_string(&path) {
                Ok(record) => record,
                Err(error) => {
                    let detail = format!("native parallel worker {index} exited with status {status} without a valid result: {error}");
                    read_failure = Some((detail.clone(), worker_exit_code(*status)));
                    return Err(PyRuntimeError::new_err(detail));
                }
            };
            if completed_workers.is_some() {
                self.validate_independent_record(index, &record, *status)?;
            }
            let empty_bootstrap = matches!(self.collection_route, CollectionRoute::Workers)
                && self.plan.assignments[index].is_empty() && status.code() == Some(5);
            if completed_workers.is_none()
                && !status.success()
                && status.code() != Some(1)
                && !empty_bootstrap
                && !status.code().map_or(Ok(false), |code| worker_declares_exitstatus(&record, code))?
            {
                let detail = format!(
                    "native parallel worker {index} exited with unexpected status {status}"
                );
                read_failure = Some((detail.clone(), worker_exit_code(*status)));
                return Err(PyRuntimeError::new_err(detail));
            }
            Ok(record)
        });
        let exit_codes = statuses.iter().map(ExitStatus::code).collect::<Vec<_>>();
        let demand_assignments = self
            .demand
            .as_deref()
            .map(|queue| queue.assignments(self.directory.path()))
            .transpose()?;
        let merged = match (
            self.result_mode,
            demand_assignments.as_deref(),
            completed_workers.as_deref(),
        ) {
            (ParallelResultMode::Execute, Some(ranges), Some(completions)) => {
                merge_independent_demand_parallel_results(
                    records,
                    ranges,
                    &self.plan.nodeids,
                    &exit_codes,
                    self.independent_merge_policy(duration_ms, completions),
                )
            }
            (_, None, Some(completions)) => merge_independent_parallel_results(
                records,
                &self.plan.assignments,
                &self.plan.nodeids,
                &exit_codes,
                self.independent_merge_policy(duration_ms, completions),
            ),
            (ParallelResultMode::CollectOnly, _, _) => merge_collect_only_parallel_results(
                records,
                &self.plan.assignments,
                &self.plan.nodeids,
                &exit_codes,
                duration_ms,
            ),
            (ParallelResultMode::Execute, Some(ranges), _) => merge_demand_parallel_results(
                records,
                ranges,
                &self.plan.nodeids,
                &exit_codes,
                self.plan.maxfail,
                self.plan.initial_failures,
                duration_ms,
            ),
            (ParallelResultMode::Execute, None, _) => merge_parallel_results(
                records,
                &self.plan.assignments,
                &self.plan.nodeids,
                &exit_codes,
                self.plan.maxfail,
                self.plan.initial_failures,
                duration_ms,
            ),
        };
        match merged {
            Ok(mut result) => {
                self.validate_independent_worker_pids(result.record())?;
                if let Some(interruption) = self.control_drain {
                    result.record_mut().exitstatus = Some(interruption.status());
                    result.retain_worker_interruption(interruption);
                }
                Ok(result)
            }
            Err(error) => {
                let (detail, returncode) = read_failure.unwrap_or_else(|| (error.to_string(), 3));
                Err(self.abort_error(py, detail, Some(returncode)))
            }
        }
    }

    pub(in crate::parallel) fn validate_coverage_shards(&mut self, py: Python<'_>) -> PyResult<()> {
        for index in 0..self.coverage_shards.len() {
            let path = self.coverage_shards[index].clone();
            let valid =
                fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.file_type().is_file());
            if !valid {
                return Err(self.abort_error(
                    py,
                    format!(
                        "native parallel worker {index} did not publish its coverage shard at {}",
                        path.display()
                    ),
                    Some(3),
                ));
            }
        }
        Ok(())
    }

    /// Whether this session is the independent parallel route, not a fresh mutation run.
    pub(in crate::parallel) fn independent_parallel(&self) -> bool {
        self.invocation_identity.is_some() && self.fresh_execution.is_none()
    }

    pub(in crate::parallel) fn replay_output(&mut self, py: Python<'_>) -> PyResult<()> {
        replay_worker_stream_range(
            py,
            self.directory.path(),
            "stdout",
            0..self.children.len(),
            &mut self.stream_boundaries.stdout_ends_with_newline,
        )?;
        replay_worker_stream_range(
            py,
            self.directory.path(),
            "stderr",
            0..self.children.len(),
            &mut self.stream_boundaries.stderr_ends_with_newline,
        )
    }
}
