//! Run serial mutations through the worker supervisor.

use super::{CollectionRoute, NativeParallelSession, ParallelResultMode, ParallelSupervisor};
use crate::independent::{Disposition, FailureOrigin, LifecycleStage};
use crate::results::NativeRunResult;
use pyo3::exceptions::{PyTimeoutError, PyValueError};
use pyo3::prelude::*;
#[cfg(target_os = "linux")]
use pyo3::types::PyDict;
#[cfg(target_os = "linux")]
use rustix::process::{Pid, WaitOptions, child_subreaper, getpid, set_child_subreaper, waitpgid};
#[cfg(target_os = "linux")]
use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};
use std::{os::unix::process::ExitStatusExt, process::ExitStatus};
use vfy_core::mutation::{FreshMutation, MutationInput, MutationResult, MutationStatus};

pub(crate) mod cache;

#[cfg(target_os = "linux")]
static SUBREAPER_OWNER: Mutex<()> = Mutex::new(());

/// The kernel setting is process-global, so a concurrent or reentrant fresh
/// invocation must fail before spawning rather than restore another owner's flag.
#[cfg(target_os = "linux")]
pub(super) struct FreshProcessReaper {
    _owner: MutexGuard<'static, ()>,
    previous: Option<Pid>,
    restored: bool,
}

#[cfg(target_os = "linux")]
impl FreshProcessReaper {
    pub(super) fn acquire() -> PyResult<Self> {
        let owner = SUBREAPER_OWNER.try_lock().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err(
                "another fresh mutation owns the process-global subreaper setting",
            )
        })?;
        let previous = child_subreaper().map_err(super::io_error)?;
        set_child_subreaper(Some(getpid())).map_err(super::io_error)?;
        Ok(Self {
            _owner: owner,
            previous,
            restored: false,
        })
    }

    pub(super) fn complete<T>(self, py: Python<'_>, completed: PyResult<T>) -> PyResult<T> {
        if let Err(error) = self.finish() {
            if let Err(original) = completed {
                error.set_context(py, Some(original));
            }
            return Err(error);
        }
        completed
    }

    pub(super) fn finish(mut self) -> PyResult<()> {
        set_child_subreaper(self.previous).map_err(super::io_error)?;
        self.restored = true;
        Ok(())
    }
}

#[cfg(target_os = "linux")]
impl Drop for FreshProcessReaper {
    fn drop(&mut self) {
        if !self.restored {
            // The explicit finish path reports restoration failure. Unwinding
            // still restores while this resource retains the exclusive guard.
            let _ = set_child_subreaper(self.previous);
        }
    }
}

#[cfg(target_os = "linux")]
fn reap_owned_groups(groups: &[Option<Pid>]) -> std::io::Result<()> {
    let deadline = Instant::now() + super::cancellation::DRAIN_TIMEOUT;
    for group in groups.iter().flatten() {
        loop {
            match waitpgid(*group, WaitOptions::NOHANG) {
                Ok(Some(_)) | Err(rustix::io::Errno::INTR) => {}
                Err(rustix::io::Errno::CHILD) => break,
                Err(error) => return Err(error.into()),
                Ok(None) if Instant::now() < deadline => {
                    std::thread::sleep(super::POLL_INTERVAL);
                }
                Ok(None) => {
                    return Err(std::io::Error::other(
                        "fresh mutation descendants were not reaped before the cleanup deadline",
                    ));
                }
            }
        }
    }
    Ok(())
}

pub(super) struct FreshExecution {
    pub(super) mutation: Option<FreshMutation>,
    pub(super) tests: Vec<String>,
    started: Instant,
    deadline: Instant,
    collect_only: bool,
    cache_arguments: Option<Vec<String>>,
}

pub(super) struct FreshAbruptExit {
    returncode: i32,
    detail: String,
}

impl FreshExecution {
    fn validate_cache_arguments(arguments: Option<&[String]>) -> PyResult<()> {
        let Some(arguments) = arguments else {
            return Ok(());
        };
        if arguments.len() > super::MAX_PARALLEL_NODEIDS
            || arguments.iter().any(|argument| argument.contains('\0'))
            || arguments
                .iter()
                .try_fold(0u64, |size, argument| {
                    size.checked_add(argument.len() as u64)
                })
                .is_none_or(|size| size > super::MAX_PLAN_BYTES)
        {
            return Err(PyValueError::new_err(
                "fresh mutation cache arguments exceed the supported bounds",
            ));
        }
        Ok(())
    }

    pub(super) fn new(mutation: Option<&str>, timeout: f64, tests: Vec<String>) -> PyResult<Self> {
        let timeout = Duration::try_from_secs_f64(timeout).map_err(|_| {
            PyValueError::new_err("fresh mutation timeout must be finite and positive")
        })?;
        if timeout.is_zero() {
            return Err(PyValueError::new_err(
                "fresh mutation timeout must be positive",
            ));
        }
        let started = Instant::now();
        let deadline = started
            .checked_add(timeout)
            .ok_or_else(|| PyValueError::new_err("fresh mutation deadline overflowed"))?;
        if mutation.is_some_and(|value| value.len() as u64 > super::MAX_PLAN_BYTES) {
            return Err(PyValueError::new_err(
                "fresh mutation input exceeds the supported metadata size",
            ));
        }
        if tests.len() > super::MAX_PARALLEL_NODEIDS
            || tests
                .iter()
                .any(|test| test.is_empty() || test.len() > super::MAX_NODEID_BYTES)
        {
            return Err(PyValueError::new_err(
                "fresh mutation test plan exceeds its supported bounds",
            ));
        }
        let mutation = mutation
            .map(|value| {
                let input: MutationInput = serde_json::from_str(value)
                    .map_err(|error| PyValueError::new_err(error.to_string()))?;
                FreshMutation::new(input).map_err(PyValueError::new_err)
            })
            .transpose()?;
        Ok(Self {
            mutation,
            tests,
            started,
            deadline,
            collect_only: false,
            cache_arguments: None,
        })
    }

    pub(super) const fn deadline(&self) -> Instant {
        self.deadline
    }

    pub(super) const fn purpose(&self) -> super::independent::InvocationPurpose {
        if self.collect_only {
            super::independent::InvocationPurpose::FreshCollection
        } else {
            super::independent::InvocationPurpose::FreshSerial
        }
    }
}

#[cfg(target_os = "linux")]
pub(super) fn check_mutation_interruption(
    py: Python<'_>,
    result: &mut NativeRunResult,
) -> PyResult<()> {
    if let Some(parent) = result.take_parent_interruption() {
        return Err(parent.into_error(py));
    }
    if result.worker_interruption().is_some() {
        return Err(PyErr::from_value(
            py.import("vfy.independent.facade")?
                .getattr("Interrupted")?
                .call1(("worker execution interrupted",))?,
        ));
    }
    Ok(())
}

#[cfg_attr(
    not(target_os = "linux"),
    expect(
        dead_code,
        reason = "only the Linux retained worker consumes the request fields"
    )
)]
pub(crate) struct RetainedMutation<'a> {
    pub(crate) mutation: &'a str,
    pub(crate) tests: Vec<String>,
    pub(crate) collection: Vec<String>,
    pub(crate) arguments: Vec<String>,
    pub(crate) timeout: f64,
    pub(crate) allowed_fds: &'a [i32],
}

impl NativeParallelSession {
    pub(crate) fn run_retained_mutation(
        py: Python<'_>,
        bootstrap: &Bound<'_, PyAny>,
        request: RetainedMutation<'_>,
    ) -> PyResult<(Option<NativeRunResult>, Option<String>)> {
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (py, bootstrap, request);
            Err(PyValueError::new_err(
                "collected independent mutations require Linux",
            ))
        }
        #[cfg(target_os = "linux")]
        {
            let RetainedMutation {
                mutation,
                tests,
                collection,
                arguments,
                timeout,
                allowed_fds,
            } = request;
            let temporary = tempfile::tempdir().map_err(super::io_error)?;
            let mut session = Self::fresh_mutation_session(
                temporary.path().to_string_lossy().into_owned(),
                timeout,
                Some(mutation),
                tests.clone(),
                None,
            )?;
            session.collection_route = CollectionRoute::Controller;
            let nodeids = if tests.is_empty() {
                collection.clone()
            } else {
                tests
            };
            session.plan.nodeids = nodeids;
            session.plan.assignments = vec![(0..session.plan.nodeids.len()).collect()];
            if let Some(execution) = session.fresh_execution.as_mut() {
                execution.cache_arguments = Some(arguments);
            }
            super::validate_plan(&session.plan).map_err(PyValueError::new_err)?;
            super::validate_fork_resources(allowed_fds, &[])?;
            let reaper = FreshProcessReaper::acquire()?;
            let completed = (|| {
                let signals = super::ParallelSignalGuard::install(py)?;
                let mut supervisor = ParallelSupervisor::prepare(&session)?;
                supervisor.adopt_collection(collection)?;
                supervisor.capture_retained_inputs(py)?;
                let execution = session
                    .fresh_execution
                    .as_ref()
                    .ok_or_else(|| PyValueError::new_err("missing retained mutation execution"))?;
                let started = Instant::now();
                let result = (|| {
                    supervisor.start_collected(
                        py,
                        bootstrap,
                        &signals,
                        allowed_fds,
                        execution.deadline(),
                    )?;
                    supervisor.await_compilation(py)?;
                    supervisor.release()?;
                    supervisor
                        .finish_execution(py, started)
                        .map_err(|error| supervisor.execution_failure(py, error))
                })();
                supervisor.cleanup_fresh_execution(py)?;
                let mut result = match result {
                    Ok(result) => result,
                    Err(error) => {
                        return Ok((None, Some(supervisor.failed_fresh_result(py, error)?)));
                    }
                };
                let mutation = result
                    .as_ref()
                    .map(|result| supervisor.fresh_result(result))
                    .transpose()?
                    .flatten();
                if let Some(result) = result.as_mut() {
                    check_mutation_interruption(py, result)?;
                    supervisor.retain_fresh_cache_evidence(result, mutation.clone())?;
                    supervisor.check_fresh_deadline(py)?;
                }
                Ok((result, mutation))
            })();
            let (result, mutation) = reaper.complete(py, completed)?;
            let mutation = mutation
                .map(|mutation| {
                    serde_json::to_string(&mutation)
                        .map_err(|error| PyValueError::new_err(error.to_string()))
                })
                .transpose()?;
            Ok((result, mutation))
        }
    }

    pub(super) fn fresh_mutation_session(
        directory: String,
        timeout: f64,
        mutation: Option<&str>,
        tests: Vec<String>,
        cache_arguments: Option<Vec<String>>,
    ) -> PyResult<Self> {
        FreshExecution::validate_cache_arguments(cache_arguments.as_deref())?;
        let mut execution = FreshExecution::new(mutation, timeout, tests)?;
        execution.cache_arguments = cache_arguments;
        // A fresh mutation runs exactly one worker and uses the same
        // authenticated bootstrap protocol as a parallel run.
        let mut session = Self::new(Vec::new(), 1, directory, 1, 0, "explicit")?;
        session.plan.scheduling = super::ParallelScheduling::Cases;
        session.independent_protocol = true;
        session.collection_route = CollectionRoute::Workers;
        session.result_mode = ParallelResultMode::Execute;
        session.fresh_execution = Some(execution);
        Ok(session)
    }

    pub(super) fn fresh_collection_session(
        directory: String,
        timeout: f64,
        arguments: Vec<String>,
    ) -> PyResult<Self> {
        FreshExecution::validate_cache_arguments(Some(&arguments))?;
        let mut execution = FreshExecution::new(None, timeout, Vec::new())?;
        execution.collect_only = true;
        execution.cache_arguments = Some(arguments);
        let mut session = Self::new(Vec::new(), 1, directory, 0, 0, "explicit")?;
        session.plan.scheduling = super::ParallelScheduling::Cases;
        session.independent_protocol = true;
        session.collection_route = CollectionRoute::Workers;
        session.result_mode = ParallelResultMode::CollectOnly;
        session.fresh_execution = Some(execution);
        Ok(session)
    }
}

impl ParallelSupervisor<'_> {
    pub(super) fn failed_fresh_result(
        &mut self,
        py: Python<'_>,
        error: PyErr,
    ) -> PyResult<MutationResult> {
        if self.fresh_timed_out
            && let Some(mutation) = self.fresh_timeout_result()
        {
            return Ok(mutation);
        }
        if let Some(mut mutation) = self.fresh_abrupt_result() {
            py.check_signals()?;
            mutation.detail = error.to_string();
            self.mark_complete();
            return Ok(mutation);
        }
        // Only the completion validator can authenticate this startup failure.
        // Clean baselines and unrelated protocol failures keep propagating.
        if self.parent_interruption.is_none()
            && self.control_drain.is_none()
            && let Some(execution) = self.fresh_execution
            && let Some(mutation) = execution.mutation.as_ref()
            && let Some(failure) = super::independent::validated_failure(py, &error)
            && failure.stage == LifecycleStage::ConftestLoading
            && failure.origin == FailureOrigin::Usage
            && failure.disposition == Disposition::Return(4)
        {
            py.check_signals()?;
            let outcome = MutationResult {
                mutation: mutation.input().clone(),
                status: MutationStatus::Error,
                tests: self.fresh_tests.clone(),
                killer: None,
                duration_ms: execution.started.elapsed().as_secs_f64() * 1000.0,
                returncode: Some(4),
                detail: error.to_string(),
                phase: None,
            };
            self.mark_complete();
            return Ok(outcome);
        }
        Err(error)
    }

    pub(super) fn record_fresh_abrupt_exit(
        &mut self,
        index: usize,
        status: ExitStatus,
        detail: &str,
    ) {
        if index != 0
            || !self.released
            || status.success()
            || self.parent_interruption.is_some()
            || self.control_drain.is_some()
            || self.fresh_timed_out
            || self
                .fresh_execution
                .is_none_or(|execution| execution.mutation.is_none())
        {
            return;
        }
        // A released assignment has passed collection admission. Missing final
        // records after an observed process death can establish only a controller
        // error; any published result or intentional-exit evidence still requires
        // its complete authenticated envelope.
        for channel in ["collection-finish", "result", "exit"] {
            let absent = self
                .directory
                .path()
                .join(format!("{channel}.{index}.json"))
                .symlink_metadata()
                .is_err_and(|error| error.kind() == std::io::ErrorKind::NotFound);
            if !absent {
                return;
            }
        }
        let returncode = status
            .code()
            .or_else(|| status.signal().map(|signal| -signal));
        if let Some(returncode) = returncode {
            self.fresh_abrupt_exit = Some(FreshAbruptExit {
                returncode,
                detail: detail.to_owned(),
            });
        }
    }

    fn fresh_abrupt_result(&self) -> Option<MutationResult> {
        let execution = self.fresh_execution?;
        let mutation = execution.mutation.as_ref()?;
        let failure = self.fresh_abrupt_exit.as_ref()?;
        if self.parent_interruption.is_some() || self.control_drain.is_some() {
            return None;
        }
        Some(MutationResult {
            mutation: mutation.input().clone(),
            status: MutationStatus::Error,
            tests: self.fresh_tests.clone(),
            killer: None,
            duration_ms: execution.started.elapsed().as_secs_f64() * 1000.0,
            returncode: Some(failure.returncode),
            detail: failure.detail.clone(),
            phase: None,
        })
    }

    #[cfg(target_os = "linux")]
    fn capture_retained_inputs(&mut self, py: Python<'_>) -> PyResult<()> {
        let environment = py
            .import("os")?
            .getattr("environ")?
            .call_method0("copy")?
            .cast::<PyDict>()?
            .extract()?;
        let executable = py
            .import("sys")?
            .getattr("executable")?
            .extract::<String>()?;
        let directory = std::env::current_dir().map_err(super::io_error)?;
        self.capture_fresh_inputs(py, &executable, &directory, &environment)
    }

    pub(super) fn capture_fresh_inputs(
        &mut self,
        py: Python<'_>,
        executable: &str,
        directory: &std::path::Path,
        environment: &std::collections::HashMap<String, String>,
    ) -> PyResult<()> {
        if let Some(execution) = self.fresh_execution
            && let Some(arguments) = execution.cache_arguments.as_ref()
        {
            let current_parser = std::env::current_exe().ok().and_then(|current| {
                let target = std::path::Path::new(executable).canonicalize().ok()?;
                if current.canonicalize().ok()? != target {
                    return None;
                }
                let version = py.version_info();
                Some(vfy_core::pytest_config::PytestParserBehavior::for_cpython(
                    version.major,
                    version.minor,
                    version.patch,
                ))
            });
            let inputs = cache::FreshInputs::capture(
                executable,
                directory,
                arguments,
                environment,
                execution.purpose(),
                execution.deadline.duration_since(execution.started),
                current_parser,
            )?;
            self.fresh_inputs = Some(
                if matches!(self.collection_route, CollectionRoute::Controller) {
                    inputs.retained()
                } else {
                    inputs
                },
            );
        }
        Ok(())
    }

    pub(super) fn retain_fresh_cache_evidence(
        &mut self,
        result: &mut NativeRunResult,
        mutation: Option<MutationResult>,
    ) -> PyResult<()> {
        let Some(inputs) = self.fresh_inputs.take() else {
            return Ok(());
        };
        let execution = self.fresh_execution.ok_or_else(|| {
            PyValueError::new_err("fresh cache evidence requires its execution owner")
        })?;
        let completion = self
            .authenticated_completions
            .first()
            .and_then(Option::as_ref)
            .ok_or_else(|| PyValueError::new_err("fresh cache result has no completion"))?;
        let witness = completion.invocation.as_ref().ok_or_else(|| {
            PyValueError::new_err("fresh cache result has no native invocation witness")
        })?;
        let evidence = inputs.complete(
            self.fresh_collection_nodeids
                .as_deref()
                .unwrap_or(&self.plan.nodeids),
            result,
            witness.consumed_arguments(),
            execution.purpose(),
            mutation,
            completion.returncode,
        )?;
        result.retain_fresh_evidence(evidence);
        Ok(())
    }

    pub(super) fn cleanup_fresh_execution(&mut self, py: Python<'_>) -> PyResult<()> {
        if let Some(detail) = self.cleanup_execution() {
            return Err(super::native_parallel_error(
                py,
                format!("fresh mutation process cleanup failed: {detail}"),
                Some(3),
            ));
        }
        #[cfg(target_os = "linux")]
        reap_owned_groups(&self.process_groups).map_err(super::io_error)?;
        Ok(())
    }

    pub(super) fn check_fresh_deadline(&mut self, py: Python<'_>) -> PyResult<()> {
        if self.fresh_execution.is_some()
            && (self.parent_interruption.is_some() || self.control_drain.is_some())
        {
            // Existing authenticated control drain owns its cleanup deadline.
            return Ok(());
        }
        if self
            .fresh_deadline
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            self.fresh_timed_out = true;
            let error =
                PyTimeoutError::new_err("fresh mutation exceeded its absolute execution deadline");
            // Checked process-group cleanup finishes before the timeout can be
            // classified or another mutation can enter the shared schedule.
            self.cleanup_fresh_execution(py)?;
            return Err(error);
        }
        Ok(())
    }

    fn fresh_timeout_result(&self) -> Option<MutationResult> {
        let execution = self.fresh_execution?;
        let mutation = execution.mutation.as_ref()?;
        Some(MutationResult {
            mutation: mutation.input().clone(),
            status: MutationStatus::Timeout,
            tests: self.fresh_tests.clone(),
            killer: None,
            duration_ms: execution.started.elapsed().as_secs_f64() * 1000.0,
            returncode: None,
            detail: "fresh mutation exceeded its absolute execution deadline".to_owned(),
            phase: None,
        })
    }

    pub(super) fn fresh_result(
        &self,
        result: &NativeRunResult,
    ) -> PyResult<Option<MutationResult>> {
        let Some(mutation) = self
            .fresh_execution
            .and_then(|value| value.mutation.as_ref())
        else {
            return Ok(None);
        };
        let completion = self
            .authenticated_completions
            .first()
            .and_then(Option::as_ref)
            .and_then(|completion| completion.invocation.as_ref())
            .ok_or_else(|| {
                PyValueError::new_err("fresh mutation has no authenticated completion")
            })?;
        let applied = completion.mutation_applied(mutation.input())?;
        let record = result.record();
        let failure = record.first_failure();
        let status = match (record.exitstatus, applied, failure.is_some()) {
            (Some(0), false, _) | (Some(5), _, _) => MutationStatus::Unreached,
            (Some(0), true, _) => MutationStatus::Survived,
            (Some(1), true, true) => MutationStatus::Protected,
            _ => MutationStatus::Error,
        };
        let failure = matches!(status, MutationStatus::Protected)
            .then_some(failure)
            .flatten();
        Ok(Some(MutationResult {
            mutation: mutation.input().clone(),
            status,
            // The canonical cache/report field records the requested test plan,
            // while the authenticated RunRecord retains actual executed cases.
            tests: self.fresh_tests.clone(),
            killer: failure.map(|(nodeid, _)| nodeid.to_owned()),
            duration_ms: self
                .fresh_execution
                .map_or(record.duration_ms, |execution| {
                    execution.started.elapsed().as_secs_f64() * 1000.0
                }),
            returncode: record.exitstatus,
            detail: if matches!(status, MutationStatus::Error) {
                record
                    .collection_reports
                    .iter()
                    .filter(|report| {
                        report.outcome == vfy_core::test_execution::CollectionOutcome::Failed
                    })
                    .map(|report| report.payload.detail.as_str())
                    .collect::<Vec<_>>()
                    .join("\n")
            } else {
                String::new()
            },
            phase: failure.map(|(_, phase)| phase.as_str().to_owned()),
        }))
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests;
