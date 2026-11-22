//! Supervisor failure reporting and worker termination.

use super::ParallelSupervisor;
use crate::parallel::{
    CollectionRoute, ParallelControl, cancellation, independent, io_error,
    is_missing_process_group, native_parallel_error, read_worker_tails, write_json_atomic,
};
use crate::results::{NativeRunResult, ParallelResultMode, worker_declares_exitstatus};
use pyo3::exceptions::{PyKeyboardInterrupt, PyValueError};
use pyo3::prelude::*;
use rustix::process::{Signal, kill_process_group};
use std::fmt::Write as _;
use std::fs::{self};
use std::process::ExitStatus;
use std::time::Instant;

impl ParallelSupervisor<'_> {
    fn incomplete_execution_error(
        &mut self,
        py: Python<'_>,
        statuses: &[ExitStatus],
    ) -> PyResult<PyErr> {
        // An owned SIGINT can stop a peer before publication. Validate the
        // available records, then propagate the original control without
        // granting the incomplete run a merged result or coverage publication.
        for (index, status) in statuses.iter().enumerate() {
            if self.cancelled_without_publication(index, *status) {
                continue;
            }
            let record =
                fs::read_to_string(self.directory.path().join(format!("result.{index}.json")))
                    .map_err(io_error)?;
            self.validate_independent_record(index, &record, *status)?;
            let completion = self.authenticated_completions[index]
                .as_ref()
                .ok_or_else(|| {
                    PyValueError::new_err("independent result omitted its validated completion")
                })?;
            let command_status = i32::try_from(completion.returncode.rem_euclid(256))
                .map_err(|_| PyValueError::new_err("invalid worker completion status"))?;
            if !worker_declares_exitstatus(&record, command_status)? {
                return Err(PyValueError::new_err(
                    "independent worker result disagrees with its command status",
                ));
            }
        }
        match self.replay_output(py) {
            Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
                self.retain_execution_interruption(py, error)?;
            }
            outcome => outcome?,
        }
        let error = if let Some(parent) = self.parent_interruption.take() {
            parent.into_error(py)
        } else {
            let control = self.control_drain.ok_or_else(|| {
                PyValueError::new_err("incomplete worker run has no authenticated interruption")
            })?;
            let error = self.worker_error(
                py,
                "native worker control interrupted a peer before result publication".to_owned(),
                Some(control.status()),
            );
            independent::attach_failure(py, &error, control.failure())?;
            error
        };
        self.mark_complete();
        Ok(error)
    }

    pub(in crate::parallel) fn finish_execution(
        &mut self,
        py: Python<'_>,
        started: Instant,
    ) -> PyResult<Option<NativeRunResult>> {
        let statuses = self.wait_for_workers(py)?;
        if self.cooperative_execution() {
            if let Some(detail) = self.cleanup_execution() {
                return Err(native_parallel_error(
                    py,
                    format!("failed to stop native worker process groups: {detail}"),
                    Some(3),
                ));
            }
        } else if let Err(error) = self.terminate_all() {
            return Err(self.abort_error(
                py,
                format!("failed to stop native worker process groups: {error}"),
                Some(3),
            ));
        }
        if self.independent_parallel() && self.completed_workers.iter().any(Option::is_none) {
            return Err(self.incomplete_execution_error(py, &statuses)?);
        }
        self.validate_coverage_shards(py)?;
        let mut result =
            self.merge_worker_results(py, &statuses, started.elapsed().as_secs_f64() * 1_000.0)?;
        match self.replay_output(py) {
            Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
                self.retain_execution_interruption(py, error)?;
            }
            outcome => outcome?,
        }
        self.transfer_execution_interruption(py, &mut result)?;
        self.mark_complete();
        Ok(Some(result))
    }

    pub(in crate::parallel) fn transfer_execution_interruption(
        &mut self,
        py: Python<'_>,
        result: &mut NativeRunResult,
    ) -> PyResult<()> {
        if let Some(interruption) = self.parent_interruption.take() {
            result.record_mut().exitstatus = Some(2);
            if let Err(interruption) = result.retain_parent_interruption(interruption) {
                return Err(interruption.into_error(py));
            }
        }
        Ok(())
    }

    pub(super) fn cooperative_execution(&self) -> bool {
        self.released
            && (matches!(self.collection_route, CollectionRoute::Workers)
                || self.fresh_execution.is_some_and(|execution| {
                    matches!(
                        execution.purpose(),
                        independent::InvocationPurpose::FreshSerial
                    )
                }))
            && matches!(self.result_mode, ParallelResultMode::Execute)
            && self.invocation_identity.is_some()
            && self.plan_adopted()
            && (self.demand.is_none() || self.independent_parallel())
    }

    pub(in crate::parallel) fn execution_failure(&mut self, py: Python<'_>, error: PyErr) -> PyErr {
        if self.cooperative_execution() {
            let detail = self.cleanup_execution();
            let parent = self
                .parent_interruption
                .as_ref()
                .map(|parent| parent.clone_error(py));
            cancellation::annotate_execution_failure(py, &error, detail, parent);
        }
        error
    }

    pub(in crate::parallel) fn abort(&mut self, detail: &str) {
        if !self.released {
            let control = ParallelControl {
                state: "abort",
                detail,
            };
            let _ = write_json_atomic(&self.directory.path().join("control.json"), &control);
        }
        if self.cooperative_execution() {
            let _ = self.cleanup_execution();
        } else {
            let _ = self.terminate_all();
        }
    }

    pub(in crate::parallel) fn abort_error(
        &mut self,
        py: Python<'_>,
        detail: String,
        returncode: Option<i32>,
    ) -> PyErr {
        self.abort(&detail);
        self.worker_error(py, detail, returncode)
    }

    pub(super) fn worker_error(
        &self,
        py: Python<'_>,
        detail: String,
        returncode: Option<i32>,
    ) -> PyErr {
        let mut message = detail;
        for prefix in ["stdout", "stderr"] {
            match read_worker_tails(py, self.directory.path(), prefix, self.children.len()) {
                Ok(output) if !output.is_empty() => {
                    let _ = write!(message, "\nworker {prefix} (diagnostic tails):\n");
                    message.push_str(&output);
                }
                Ok(_) => {}
                Err(error) => {
                    let _ = write!(message, "\nfailed to read worker {prefix}: {error}");
                }
            }
        }
        if let Some(cleanup) = &self.checked_cleanup
            && !cleanup.succeeded()
        {
            let _ = write!(message, "\nnative worker cleanup failed: {cleanup}");
        }
        native_parallel_error(py, message, returncode)
    }

    pub(in crate::parallel) fn terminate_all(&mut self) -> std::io::Result<()> {
        let mut first_error = None;
        for pid in self.process_groups.iter().flatten().copied() {
            if let Err(error) = kill_process_group(pid, Signal::KILL)
                && !is_missing_process_group(error)
                && first_error.is_none()
            {
                first_error = Some(std::io::Error::other(error));
            }
        }
        for child in self.children.iter_mut().flatten() {
            let _ = child.kill();
            let _ = child.wait();
        }
        self.children.fill_with(|| None);
        if let Some(error) = first_error {
            Err(error)
        } else {
            Ok(())
        }
    }

    pub(in crate::parallel) fn mark_complete(&mut self) {
        self.complete = true;
    }
}
