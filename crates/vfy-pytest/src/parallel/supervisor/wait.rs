//! Supervisor worker wait loop and exit-status checks.

use super::ParallelSupervisor;
use crate::parallel::{
    CollectionRoute, POLL_INTERVAL, ParallelChild, ParallelControl, WorkerCompletionRoute,
    WorkerExit, WorkerProgress, cancellation, independent, io_error, read_json,
    replay_worker_stream, worker_exit_code, write_json_atomic,
};
use crate::progress::{CaseOrder, Progress};
use crate::results::worker_declares_exitstatus;
use pyo3::exceptions::{PyKeyboardInterrupt, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use rustix::process::{Pid, Signal};
use std::fs::{self};
use std::io::{self};
use std::process::ExitStatus;
use std::thread;
use std::time::Instant;
use vfy_core::test_execution::RunRecord;

impl ParallelSupervisor<'_> {
    pub(in crate::parallel) fn release(&mut self) -> PyResult<()> {
        let control = ParallelControl {
            state: "go",
            detail: "",
        };
        write_json_atomic(&self.directory.path().join("control.json"), &control)
            .map_err(io_error)?;
        self.released = true;
        Ok(())
    }

    pub(in crate::parallel) fn wait_for_workers(
        &mut self,
        py: Python<'_>,
    ) -> PyResult<Vec<ExitStatus>> {
        if let Some(config) = self.progress_config {
            match Progress::from_config(
                config.bind(py),
                self.plan.nodeids.len(),
                CaseOrder::Completion,
            ) {
                Ok(Some(display)) => {
                    self.progress = Some(WorkerProgress {
                        display,
                        offsets: vec![0; self.children.len()],
                    });
                }
                Ok(None) => (),
                Err(error) => self.progress_outcome(py, Err(error))?,
            }
        }
        let statuses = self.wait_for_worker_exits(py)?;
        let finished = if let Some(progress) = self.progress.as_mut() {
            progress
                .drain(py, self.directory.path())
                .and_then(|()| progress.display.finish(py))
        } else {
            Ok(())
        };
        self.progress_outcome(py, finished)?;
        Ok(statuses)
    }

    pub(in crate::parallel) fn retain_execution_interruption(
        &mut self,
        py: Python<'_>,
        error: PyErr,
    ) -> PyResult<()> {
        if self.cooperative_execution()
            && self.parent_interruption.is_none()
            && error.is_instance_of::<PyKeyboardInterrupt>(py)
        {
            self.parent_interruption =
                Some(crate::results::ParentInterruption::from_error(py, error));
            self.interrupt_execution_peers().map_err(io_error)
        } else {
            if !self.cooperative_execution() {
                self.abort("parent interrupted");
            }
            Err(error)
        }
    }

    fn progress_outcome(&mut self, py: Python<'_>, outcome: PyResult<()>) -> PyResult<()> {
        match outcome {
            Err(error) if error.is_instance_of::<PyKeyboardInterrupt>(py) => {
                self.retain_execution_interruption(py, error)?;
                // A signal can interrupt a Python writer after progress state changed.
                // Keep draining owned workers without replaying the partial display.
                self.progress = None;
                Ok(())
            }
            result => result,
        }
    }

    fn wait_for_worker_exits(&mut self, py: Python<'_>) -> PyResult<Vec<ExitStatus>> {
        while self.children.iter().any(Option::is_some) {
            self.check_fresh_deadline(py)?;
            if self.cancellation.expired(Instant::now()) {
                return Err(self.abort_error(
                    py,
                    "native workers did not complete interrupted cleanup within the drain deadline"
                        .to_owned(),
                    Some(3),
                ));
            }
            let progressed = if let Some(progress) = self.progress.as_mut() {
                progress.drain(py, self.directory.path())
            } else {
                Ok(())
            };
            self.progress_outcome(py, progressed)?;
            if let Err(error) = py.check_signals() {
                self.retain_execution_interruption(py, error)?;
            }
            for index in 0..self.children.len() {
                if let Some(child) = self.children[index].as_mut()
                    && let Some(status) = child.try_wait().map_err(io_error)?
                {
                    self.statuses[index] = Some(status);
                    self.children[index] = None;
                    let has_result = self
                        .directory
                        .path()
                        .join(format!("result.{index}.json"))
                        .is_file();
                    if self.cancelled_without_publication(index, status) {
                        continue;
                    }
                    self.check_finished_worker(py, index, status)?;
                    let empty_bootstrap = matches!(self.collection_route, CollectionRoute::Workers)
                        && self.plan.assignments[index].is_empty()
                        && status.code() == Some(5);
                    let unexpected_status =
                        !status.success() && status.code() != Some(1) && !empty_bootstrap;
                    let declared_status = if has_result && unexpected_status {
                        let record = fs::read_to_string(
                            self.directory.path().join(format!("result.{index}.json")),
                        )
                        .map_err(io_error)?;
                        if self.completed_workers[index].is_some() {
                            self.validate_independent_record(index, &record, status)?;
                            true
                        } else {
                            status.code().map_or(Ok(false), |code| {
                                worker_declares_exitstatus(&record, code)
                            })?
                        }
                    } else {
                        false
                    };
                    if !has_result || (unexpected_status && !declared_status) {
                        let detail = if has_result {
                            format!(
                                "native parallel worker {index} exited with unexpected status {status}"
                            )
                        } else {
                            format!(
                                "native parallel worker {index} exited with status {status} before publishing a result"
                            )
                        };
                        return Err(self.abort_error(py, detail, Some(worker_exit_code(status))));
                    }
                }
            }
            if self.children.iter().any(Option::is_some) {
                py.detach(|| thread::sleep(POLL_INTERVAL));
            }
        }
        self.statuses
            .iter()
            .enumerate()
            .map(|(index, status)| {
                status.ok_or_else(|| {
                    PyRuntimeError::new_err(format!(
                        "native parallel worker {index} has no exit status"
                    ))
                })
            })
            .collect()
    }

    pub(super) fn cancelled_without_publication(&self, index: usize, status: ExitStatus) -> bool {
        self.independent_parallel()
            && self.cooperative_execution()
            && (self.control_drain.is_some() || self.parent_interruption.is_some())
            && self
                .cancellation
                .delivered_interruption(index, status)
                .is_some()
            && ["collection-finish", "result", "exit"]
                .iter()
                .all(|prefix| {
                    fs::symlink_metadata(
                        self.directory.path().join(format!("{prefix}.{index}.json")),
                    )
                    .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
                })
    }

    fn drain_collection_peers(&mut self, py: Python<'_>, primary: usize) -> PyResult<()> {
        self.abort_collection()?;
        for index in 0..self.process_groups.len() {
            if index != primary
                && self.children[index].is_some()
                && self.process_groups[index].is_some()
            {
                match cancellation::signal_group(
                    &mut self.children[index],
                    &mut self.process_groups[index],
                    Signal::INT,
                ) {
                    Ok(()) => (),
                    Err(error)
                        if error.raw_os_error() == Some(rustix::io::Errno::SRCH.raw_os_error()) => {
                    }
                    Err(error) => return Err(io_error(error)),
                }
            }
        }
        let started = Instant::now();
        while self.children.iter().any(Option::is_some) {
            py.check_signals()?;
            if started.elapsed() >= crate::parallel::cancellation::DRAIN_TIMEOUT {
                return Err(self.abort_error(py, "native collection peers did not complete interrupted cleanup within the drain deadline".to_owned(), Some(3)));
            }
            for index in 0..self.children.len() {
                let status = self.children[index]
                    .as_mut()
                    .map(ParallelChild::try_wait)
                    .transpose()
                    .map_err(io_error)?
                    .flatten();
                let Some(status) = status else {
                    continue;
                };
                self.children[index] = None;
                self.statuses[index] = Some(status);
                let completion = self.read_collection_completion(py, index, status)?;
                let (published, validated) = self.validate_completion(index, &completion)?;
                match validated {
                    independent::ValidatedCompletion::Failure(failure, _) => {
                        if !matches!(
                            failure.origin,
                            crate::independent::FailureOrigin::Control(_)
                        ) {
                            return Err(self.abort_error(
                                py,
                                "native peer failed independently while draining collection"
                                    .to_owned(),
                                Some(3),
                            ));
                        }
                    }
                    independent::ValidatedCompletion::Result(_) => {
                        if published != independent::PublishedResult::Collection {
                            return Err(self.abort_error(py, "native collection peer published an execution result before assignment".to_owned(), Some(3)));
                        }
                        self.validate_collection_peer(index, &completion)?;
                    }
                }
                self.authenticated_completions[index] = Some(completion);
            }
            if self.children.iter().any(Option::is_some) {
                py.detach(|| thread::sleep(POLL_INTERVAL));
            }
        }
        Ok(())
    }

    pub(super) fn check_finished_worker(
        &mut self,
        py: Python<'_>,
        index: usize,
        status: ExitStatus,
    ) -> PyResult<()> {
        // Results and intentional exits are published before unconfigure. Recorded
        // worker routes require the later envelope before trusting either channel.
        let completion = if matches!(
            self.completion_route,
            WorkerCompletionRoute::PostUnconfigure
        ) {
            Some(self.read_collection_completion(py, index, status)?)
        } else {
            None
        };
        if self.invocation_identity.is_some() {
            let completion = completion.as_ref().ok_or_else(|| {
                PyValueError::new_err("independent worker omitted shutdown completion")
            })?;
            let (_, validated) = self.validate_completion(index, completion)?;
            self.authenticated_completions[index] = Some(completion.clone());
            match validated {
                independent::ValidatedCompletion::Failure(failure, declared) => {
                    if !self.released && self.children.iter().any(Option::is_some) {
                        self.drain_collection_peers(py, index)?;
                    }
                    if self.independent_parallel() && !self.released {
                        // A worker's usage error reaches the controller through its stderr,
                        // which the controller forwards before the run ends.
                        replay_worker_stream(
                            py,
                            self.directory.path(),
                            "stderr",
                            self.children.len(),
                        )?;
                    }
                    let error = self.abort_error(
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
                independent::ValidatedCompletion::Result(completed) => {
                    // A controller INT may arrive after native shutdown publication,
                    // while CPython is still leaving the interpreter.
                    let completed = self.completed_workers[index].unwrap_or(completed);
                    self.completed_workers[index] = Some(completed);
                    if let Some(interruption) = completed.interruption()
                        && self.released
                        && self.control_drain.is_none()
                    {
                        self.control_drain = Some(interruption);
                        self.interrupt_execution_peers().map_err(io_error)?;
                    }
                }
            }
        }
        self.check_worker_exit(py, index, status)?;
        if let Some(reason) = completion.and_then(|completion| completion.exit_reason) {
            if self
                .directory
                .path()
                .join(format!("result.{index}.json"))
                .is_file()
            {
                return Err(self.abort_error(py, format!("native parallel worker {index} published an intentional exit together with an execution result"), Some(3)));
            }
            return Err(self.abort_error(
                py,
                format!("native parallel worker {index} requested pytest exit: {reason}"),
                Some(status.code().unwrap_or(3)),
            ));
        }
        Ok(())
    }

    pub(super) fn check_worker_exit(
        &mut self,
        py: Python<'_>,
        index: usize,
        status: ExitStatus,
    ) -> PyResult<()> {
        match read_json::<WorkerExit>(&self.directory.path().join(format!("exit.{index}.json"))) {
            Ok(exit) => {
                // Unix process exit statuses retain the low eight bits of the
                // integer passed through Python's SystemExit boundary.
                if exit.index != index
                    || status.code() != Some(exit.returncode.rem_euclid(256))
                    || self
                        .directory
                        .path()
                        .join(format!("result.{index}.json"))
                        .is_file()
                {
                    return Err(self.abort_error(
                        py,
                        format!("native parallel worker {index} published an exit record inconsistent with its process status or result"),
                        Some(3),
                    ));
                }
                Err(self.abort_error(
                    py,
                    format!(
                        "native parallel worker {index} requested pytest exit: {}",
                        exit.reason
                    ),
                    Some(exit.returncode),
                ))
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(self.abort_error(
                py,
                format!("native parallel worker {index} published an invalid exit record: {error}"),
                Some(3),
            )),
        }
    }

    pub(super) fn validate_independent_worker_pids(&self, record: &RunRecord) -> PyResult<()> {
        crate::results::validate_worker_observations(record.observations.as_ref())?;
        if record.engine != "vfy-independent-v1" {
            return Ok(());
        }
        let observations = record.observations.as_ref().ok_or_else(|| {
            PyValueError::new_err("independent workers omitted execution observations")
        })?;
        let mut expected = self
            .worker_pids
            .iter()
            .map(|pid| {
                pid.map(Pid::as_raw_pid)
                    .and_then(|pid| u32::try_from(pid).ok())
                    .ok_or_else(|| {
                        PyValueError::new_err("independent worker has no process identity")
                    })
            })
            .collect::<PyResult<Vec<_>>>()?;
        expected.sort_unstable();
        let topology = &observations.execution_topology;
        if topology.worker_pids != expected || topology.active_workers != expected.len() {
            return Err(PyValueError::new_err(
                "independent worker observations disagree with the spawned processes",
            ));
        }
        Ok(())
    }
}
