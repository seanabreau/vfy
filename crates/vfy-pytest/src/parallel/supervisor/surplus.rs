//! Handling of prespawned workers beyond the collected width.

use super::ParallelSupervisor;
use crate::parallel::{
    POLL_INTERVAL, ParallelChild, SurplusWarnings, WorkerCollectionCompletion,
    collection_report_shapes, coverage_base, independent, io_error, native_parallel_error,
    read_worker_collection, replay_worker_stream_range, worker_exit_code,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use rustix::process::{Signal, kill_process_group};
use std::collections::HashMap;
use std::process::ExitStatus;
use std::thread;
use vfy_core::test_execution::CollectionOutcome;

impl ParallelSupervisor<'_> {
    /// Authenticate every speculative worker's collection and shutdown.
    pub(in crate::parallel) fn reap_surplus(
        &mut self,
        py: Python<'_>,
        environment: &HashMap<String, String>,
    ) -> PyResult<Vec<SurplusWarnings>> {
        if self.surplus.is_empty() {
            return Ok(Vec::new());
        }
        let width = self.children.len();
        let workers = self
            .surplus
            .iter()
            .map(|(index, _, _)| index + 1)
            .max()
            .unwrap_or(width);
        self.children.resize_with(workers, || None);
        self.process_groups.resize(workers, None);
        self.worker_pids.resize(workers, None);
        self.statuses.resize(workers, None);
        self.completed_workers.resize(workers, None);
        self.authenticated_completions.resize_with(workers, || None);
        for (index, pid, child) in std::mem::take(&mut self.surplus) {
            self.retain_child(index, pid, ParallelChild::Spawned(child));
        }
        let result = self.wait_surplus(py, width, workers);
        self.cleanup_surplus(width, workers, environment);
        let warnings = result?;
        replay_worker_stream_range(
            py,
            self.directory.path(),
            "stdout",
            width..workers,
            &mut self.stream_boundaries.stdout_ends_with_newline,
        )?;
        replay_worker_stream_range(
            py,
            self.directory.path(),
            "stderr",
            width..workers,
            &mut self.stream_boundaries.stderr_ends_with_newline,
        )?;
        Ok(warnings)
    }

    fn wait_surplus(
        &mut self,
        py: Python<'_>,
        width: usize,
        workers: usize,
    ) -> PyResult<Vec<SurplusWarnings>> {
        let mut warnings = (width..workers).map(|_| None).collect::<Vec<_>>();
        while self.children[width..].iter().any(Option::is_some) {
            self.check_fresh_deadline(py)?;
            py.check_signals()?;
            for index in width..workers {
                let status = self.children[index]
                    .as_mut()
                    .map(ParallelChild::try_wait)
                    .transpose()
                    .map_err(io_error)?
                    .flatten();
                let Some(status) = status else { continue };
                self.children[index] = None;
                self.statuses[index] = Some(status);
                warnings[index - width] = Some(self.check_surplus_completion(py, index, status)?);
            }
            if self.children[width..].iter().any(Option::is_some) {
                py.detach(|| thread::sleep(POLL_INTERVAL));
            }
        }
        warnings
            .into_iter()
            .map(|warning| {
                warning.ok_or_else(|| {
                    PyRuntimeError::new_err("surplus worker has no validated warning records")
                })
            })
            .collect()
    }

    fn check_surplus_completion(
        &mut self,
        py: Python<'_>,
        index: usize,
        status: ExitStatus,
    ) -> PyResult<SurplusWarnings> {
        let completion = self.read_collection_completion(py, index, status)?;
        let (published, validated) = self.validate_completion(index, &completion)?;
        let document = match validated {
            independent::ValidatedCompletion::Failure(failure, declared) => {
                let error = self.worker_error(
                    py,
                    format!(
                        "native independent worker {index} failed during {}",
                        failure.stage_name()
                    ),
                    Some(declared),
                );
                independent::attach_failure(py, &error, failure)?;
                return Err(error);
            }
            independent::ValidatedCompletion::Result(_) => {
                if published != independent::PublishedResult::Collection {
                    return Err(PyValueError::new_err(
                        "surplus worker published an execution result",
                    ));
                }
                let document = self.validate_collection_peer(index, &completion)?;
                let collection_errors = self.validate_surplus_collection(py, index, &completion)?;
                let accepted = match status.code() {
                    Some(0) => true,
                    Some(5) => self.plan.nodeids.is_empty() && collection_errors == 0,
                    Some(1) => {
                        collection_errors > 0
                            && self
                                .collection_error_policy
                                .allows_execution(collection_errors)
                    }
                    _ => false,
                };
                if !accepted {
                    return Err(native_parallel_error(
                        py,
                        format!(
                            "surplus worker {index} failed during collection or shutdown (status {status})"
                        ),
                        Some(worker_exit_code(status)),
                    ));
                }
                document
            }
        };
        self.check_worker_exit(py, index, status)?;
        if let Some(reason) = completion.exit_reason {
            return Err(native_parallel_error(
                py,
                format!("native parallel worker {index} requested pytest exit: {reason}"),
                Some(worker_exit_code(status)),
            ));
        }
        Ok(SurplusWarnings {
            collection: document.collection_warnings,
            finish: document.finish_warnings,
        })
    }

    fn validate_surplus_collection(
        &self,
        py: Python<'_>,
        index: usize,
        completion: &WorkerCollectionCompletion,
    ) -> PyResult<usize> {
        let canonical = read_worker_collection(&self.directory.path().join("collection.0.json"))?;
        let candidate = read_worker_collection(
            &self
                .directory
                .path()
                .join(format!("collection.{index}.json")),
        )?;
        if candidate.errors > 0
            && !self
                .collection_error_policy
                .allows_execution(candidate.errors)
        {
            let identity = self
                .invocation_identity
                .as_ref()
                .ok_or_else(|| PyValueError::new_err("native peer has no invocation identity"))?;
            let record = independent::read_collection_record(
                self.directory.path(),
                index,
                identity,
                completion,
            )?;
            let details = record
                .collection_reports
                .iter()
                .filter(|report| report.outcome == CollectionOutcome::Failed)
                .map(|report| report.payload.detail.as_str())
                .filter(|detail| !detail.is_empty())
                .collect::<Vec<_>>()
                .join("\n");
            return Err(native_parallel_error(
                py,
                format!("surplus worker {index} encountered errors during collection\n{details}"),
                Some(2),
            ));
        }
        if candidate.index != index
            || candidate.nodeids != canonical.nodeids
            || collection_report_shapes(&candidate.reports)?
                != collection_report_shapes(&canonical.reports)?
        {
            return Err(PyValueError::new_err(format!(
                "surplus worker {index} collected a different test manifest or outcome"
            )));
        }
        Ok(candidate.errors)
    }

    fn cleanup_surplus(
        &mut self,
        width: usize,
        workers: usize,
        environment: &HashMap<String, String>,
    ) {
        for index in width..workers {
            if let Some(pid) = self.process_groups[index].take() {
                let _ = kill_process_group(pid, Signal::KILL);
            }
            if let Some(mut child) = self.children[index].take() {
                let _ = child.kill();
                let _ = child.wait();
            }
            if let Some(base) = coverage_base(environment) {
                let _ = std::fs::remove_file(format!("{base}.worker{index}"));
            }
        }
    }
}
