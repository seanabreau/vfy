//! Parent-process session object and signal handling for parallel native runs.

use super::{
    CollectionErrorPolicy, CollectionRoute, MAX_NODEID_BYTES, MAX_PARALLEL_NODEIDS,
    MAX_PARALLEL_WORKERS, MAX_PLAN_BYTES, ParallelPlan, ParallelScheduling, ParallelSupervisor,
    ScopeGrouping, WorkerCollection, collected_fork_timeout, demand, group_count, mutation,
    native_parallel_error, partition, replay_worker_stream, validate_fork_resources,
    validate_plan_size,
};
use crate::results::{NativeRunResult, ParallelResultMode, native_run_result};
use pyo3::exceptions::{PyKeyboardInterrupt, PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyAny;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeParallelSession {
    pub(super) plan: ParallelPlan,
    pub(super) directory: PathBuf,
    pub(super) collection_route: CollectionRoute,
    pub(super) result_mode: ParallelResultMode,
    pub(super) collection_error_policy: CollectionErrorPolicy,
    pub(super) progress_config: Option<Py<PyAny>>,
    pub(super) independent_protocol: bool,
    pub(super) fresh_execution: Option<mutation::FreshExecution>,
}

impl NativeParallelSession {
    /// Worker processes needed once worker collection agrees on `nodeids`.
    fn collected_width(&self, nodeids: &[String]) -> PyResult<usize> {
        let workers = self.plan.assignments.len();
        match self.plan.scheduling {
            ParallelScheduling::File | ParallelScheduling::Scope => {
                let grouping = ScopeGrouping::for_scheduling(self.plan.scheduling);
                Ok(group_count(nodeids, grouping).max(1).min(workers.max(1)))
            }
            ParallelScheduling::Cases | ParallelScheduling::Striped => {
                Ok(workers.min(nodeids.len().max(1)))
            }
            ParallelScheduling::Demand if self.independent_protocol => {
                Ok(workers.min(nodeids.len().max(1)))
            }
            ParallelScheduling::Demand => Err(PyValueError::new_err(
                "demand scheduling requires collected fork workers",
            )),
        }
    }
}

#[pymethods]
impl NativeParallelSession {
    #[staticmethod]
    fn startup_grace_ms() -> PyResult<u64> {
        demand::startup_grace_ms()
    }

    /// Reports whether this process can fork collected workers, without creating
    /// any worker. Raises `ValueError` with the same diagnostic as `run_collected`.
    #[staticmethod]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the framework-owned descriptor inventory once"
    )]
    fn check_collected_fork(allowed_fds: Vec<i32>) -> PyResult<()> {
        validate_fork_resources(&allowed_fds, &[])
    }

    #[new]
    #[pyo3(signature = (nodeids, workers, directory, maxfail, initial_failures, worker_mode = "explicit"))]
    pub(super) fn new(
        nodeids: Vec<String>,
        workers: usize,
        directory: String,
        maxfail: usize,
        initial_failures: usize,
        worker_mode: &str,
    ) -> PyResult<Self> {
        let workers = match worker_mode {
            "explicit" => workers,
            "automatic" => workers.min(MAX_PARALLEL_WORKERS),
            _ => {
                return Err(PyValueError::new_err(
                    "native worker mode must be explicit or automatic",
                ));
            }
        };
        if workers == 0 {
            return Err(PyValueError::new_err(
                "native parallel execution requires at least one worker",
            ));
        }
        if workers > MAX_PARALLEL_WORKERS {
            return Err(PyValueError::new_err(format!(
                "native parallel execution supports at most {MAX_PARALLEL_WORKERS} workers"
            )));
        }
        if nodeids.len() > MAX_PARALLEL_NODEIDS
            || nodeids.iter().any(|nodeid| nodeid.len() > MAX_NODEID_BYTES)
        {
            return Err(PyValueError::new_err(
                "native parallel collection exceeds the supported manifest size",
            ));
        }
        let nodeid_bytes = nodeids
            .iter()
            .try_fold(0usize, |size, nodeid| size.checked_add(nodeid.len()));
        if nodeid_bytes.is_none_or(|size| size as u64 > MAX_PLAN_BYTES) {
            return Err(PyValueError::new_err(
                "native parallel collection exceeds the supported manifest size",
            ));
        }
        if nodeids.iter().any(String::is_empty) {
            return Err(PyValueError::new_err(
                "native parallel collection contains an empty node ID",
            ));
        }
        let scheduling = ParallelScheduling::from_environment()?;
        let assignments = partition(scheduling, &nodeids, workers);
        let plan = ParallelPlan {
            scheduling,
            nodeids,
            assignments,
            maxfail,
            initial_failures,
        };
        validate_plan_size(&plan).map_err(PyValueError::new_err)?;
        Ok(Self {
            plan,
            directory: PathBuf::from(directory),
            collection_route: CollectionRoute::Controller,
            result_mode: ParallelResultMode::Execute,
            collection_error_policy: CollectionErrorPolicy::Stop,
            progress_config: None,
            independent_protocol: false,
            fresh_execution: None,
        })
    }

    /// Write live progress for worker cases through `config`'s terminal reporter.
    fn report_progress(&mut self, config: Py<PyAny>) {
        self.progress_config = Some(config);
    }

    #[staticmethod]
    #[pyo3(signature = (workers, directory, maxfail, initial_failures, *, collect_only=false, continue_on_collection_errors=false, engine=None))]
    fn bootstrap(
        workers: usize,
        directory: String,
        maxfail: &Bound<'_, PyAny>,
        initial_failures: usize,
        collect_only: bool,
        continue_on_collection_errors: bool,
        engine: Option<&str>,
    ) -> PyResult<Self> {
        // Pytest accepts every integer: negative limits stop on the first failure,
        // and a limit above usize cannot be exhausted by the bounded worker plan.
        let maxfail = match maxfail.extract::<usize>() {
            Ok(limit) => limit,
            Err(error) if error.is_instance_of::<PyOverflowError>(maxfail.py()) => {
                if maxfail.lt(0)? { 1 } else { usize::MAX }
            }
            Err(error) => return Err(error),
        };
        if workers < 2 {
            return Err(PyValueError::new_err(
                "worker collection requires at least two explicit workers",
            ));
        }
        let mut session = Self::new(
            Vec::new(),
            workers,
            directory,
            maxfail,
            initial_failures,
            "explicit",
        )?;
        session.independent_protocol = match engine {
            None => false,
            Some("vfy-independent-v1") => true,
            Some(_) => return Err(PyValueError::new_err("unsupported native worker engine")),
        };
        if collect_only
            && session.independent_protocol
            && session.plan.scheduling == ParallelScheduling::Demand
        {
            session.plan.scheduling = ParallelScheduling::Cases;
        }
        if continue_on_collection_errors && !session.independent_protocol {
            return Err(PyValueError::new_err(
                "collection error continuation requires independent workers",
            ));
        }
        session.plan.assignments.resize_with(workers, Vec::new);
        session.collection_route = CollectionRoute::Workers;
        session.collection_error_policy = if continue_on_collection_errors {
            CollectionErrorPolicy::Continue
        } else {
            CollectionErrorPolicy::Stop
        };
        session.result_mode = if collect_only {
            ParallelResultMode::CollectOnly
        } else {
            ParallelResultMode::Execute
        };
        Ok(session)
    }

    #[staticmethod]
    #[pyo3(signature = (directory, timeout, mutation=None, *, tests=None, cache_arguments=None))]
    fn fresh_mutation(
        directory: String,
        timeout: f64,
        mutation: Option<&str>,
        tests: Option<Vec<String>>,
        cache_arguments: Option<Vec<String>>,
    ) -> PyResult<Self> {
        Self::fresh_mutation_session(
            directory,
            timeout,
            mutation,
            tests.unwrap_or_default(),
            cache_arguments,
        )
    }

    #[staticmethod]
    #[pyo3(signature = (directory, timeout, *, arguments))]
    fn fresh_collection(directory: String, timeout: f64, arguments: Vec<String>) -> PyResult<Self> {
        Self::fresh_collection_session(directory, timeout, arguments)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 hands the Python-visible method owned argument values"
    )]
    fn run_bootstrap(
        &self,
        py: Python<'_>,
        executable: String,
        args: Vec<String>,
        environment: HashMap<String, String>,
        invocation_dir: String,
        collection_callback: &Bound<'_, PyAny>,
    ) -> PyResult<Option<NativeRunResult>> {
        if self.fresh_execution.is_some() {
            return Err(PyValueError::new_err(
                "fresh mutation sessions require run_mutation",
            ));
        }
        self.run_bootstrap_owned(
            py,
            &executable,
            &args,
            &environment,
            Path::new(&invocation_dir),
            collection_callback,
        )
        .map(|(result, _)| result)
    }

    #[cfg_attr(
        target_os = "linux",
        expect(
            clippy::needless_pass_by_value,
            reason = "PyO3 hands the Python-visible method owned argument values"
        )
    )]
    fn run_mutation(
        &self,
        py: Python<'_>,
        executable: String,
        args: Vec<String>,
        environment: HashMap<String, String>,
        invocation_dir: String,
        collection_callback: &Bound<'_, PyAny>,
    ) -> PyResult<(Option<NativeRunResult>, Option<String>)> {
        if self.fresh_execution.is_none() {
            return Err(PyValueError::new_err(
                "run_mutation requires a fresh serial mutation session",
            ));
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (
                py,
                executable,
                args,
                environment,
                invocation_dir,
                collection_callback,
            );
            Err(PyValueError::new_err(
                "fresh serial mutations require Linux descendant reaping",
            ))
        }
        #[cfg(target_os = "linux")]
        {
            let owner = mutation::FreshProcessReaper::acquire()?;
            let completed = self.run_bootstrap_owned(
                py,
                &executable,
                &args,
                &environment,
                Path::new(&invocation_dir),
                collection_callback,
            );
            let (mut result, mutation) = owner.complete(py, completed)?;
            if let Some(result) = result.as_mut() {
                mutation::check_mutation_interruption(py, result)?;
            }
            let mutation = mutation
                .map(|mutation| {
                    serde_json::to_string(&mutation)
                        .map_err(|error| PyValueError::new_err(error.to_string()))
                })
                .transpose()?;
            Ok((result, mutation))
        }
    }

    #[getter]
    fn planned_worker_count(&self) -> usize {
        self.plan.assignments.len()
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 hands the Python-visible method owned argument values"
    )]
    fn run(
        &self,
        py: Python<'_>,
        executable: String,
        args: Vec<String>,
        environment: HashMap<String, String>,
        invocation_dir: String,
    ) -> PyResult<NativeRunResult> {
        if !matches!(self.collection_route, CollectionRoute::Controller) {
            return Err(PyValueError::new_err(
                "worker-collection sessions require run_bootstrap",
            ));
        }
        if self.plan.scheduling == ParallelScheduling::Demand {
            return Err(PyValueError::new_err(
                "demand scheduling requires collected fork workers",
            ));
        }
        let _signals = ParallelSignalGuard::install(py)?;
        let started = Instant::now();
        let supervisor = ParallelSupervisor::start(
            self,
            &executable,
            &args,
            &environment,
            Path::new(&invocation_dir),
        )?;
        let mut supervisor = supervisor;
        supervisor.await_compilation(py)?;
        supervisor.release()?;
        let statuses = supervisor.wait_for_workers(py)?;
        if let Err(error) = supervisor.terminate_all() {
            return Err(supervisor.abort_error(
                py,
                format!("failed to stop native worker process groups: {error}"),
                Some(3),
            ));
        }
        supervisor.validate_coverage_shards(py)?;
        let duration_ms = started.elapsed().as_secs_f64() * 1_000.0;
        let result = supervisor.merge_worker_results(py, &statuses, duration_ms)?;
        supervisor.replay_output(py)?;
        supervisor.mark_complete();
        Ok(result)
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts the framework-owned descriptor inventory once"
    )]
    #[pyo3(signature = (bootstrap, allowed_fds, demand_groups = None, coverage_base = None))]
    fn run_collected(
        &self,
        py: Python<'_>,
        bootstrap: &Bound<'_, PyAny>,
        allowed_fds: Vec<i32>,
        demand_groups: Option<Vec<(usize, usize)>>,
        coverage_base: Option<String>,
    ) -> PyResult<NativeRunResult> {
        if !matches!(self.collection_route, CollectionRoute::Controller) {
            return Err(PyValueError::new_err(
                "collected fork workers require controller collection",
            ));
        }
        if !bootstrap.is_callable()
            || allowed_fds.len() > 256
            || allowed_fds.iter().any(|fd| *fd < 0)
        {
            return Err(PyValueError::new_err(
                "collected fork workers require a callable bootstrap and at most 256 nonnegative owned descriptors",
            ));
        }
        validate_fork_resources(&allowed_fds, &[])?;
        let timeout = collected_fork_timeout()?;
        let signals = ParallelSignalGuard::install(py)?;
        let started = Instant::now();
        let mut supervisor = ParallelSupervisor::prepare(self)?;
        match (self.plan.scheduling, demand_groups) {
            (ParallelScheduling::Demand, Some(groups)) => {
                supervisor.demand = Some(Arc::new(demand::DemandQueue::initialize(
                    supervisor.directory.path(),
                    groups,
                    self.plan.nodeids.len(),
                    self.plan.assignments.len(),
                )?));
            }
            (ParallelScheduling::Demand, None) => {
                return Err(PyValueError::new_err(
                    "demand scheduling requires a precompiled scope-group plan",
                ));
            }
            (_, Some(_)) => {
                return Err(PyValueError::new_err(
                    "scope-group leases require demand scheduling",
                ));
            }
            (_, None) => {}
        }
        if let Some(base) = coverage_base.filter(|value| !value.is_empty()) {
            supervisor.coverage_shards = (0..self.plan.assignments.len())
                .map(|index| PathBuf::from(format!("{base}.worker{index}")))
                .collect();
        }
        supervisor.start_collected(py, bootstrap, &signals, &allowed_fds, started + timeout)?;
        supervisor.await_compilation_until(py, Some(started + timeout))?;
        supervisor.release()?;
        let statuses = supervisor.wait_for_workers(py)?;
        if let Err(error) = supervisor.terminate_all() {
            return Err(supervisor.abort_error(
                py,
                format!("failed to stop native worker process groups: {error}"),
                Some(3),
            ));
        }
        supervisor.validate_coverage_shards(py)?;
        let result = supervisor.merge_worker_results(
            py,
            &statuses,
            started.elapsed().as_secs_f64() * 1_000.0,
        )?;
        supervisor.replay_output(py)?;
        supervisor.mark_complete();
        Ok(result)
    }
}

impl NativeParallelSession {
    #[expect(
        clippy::too_many_lines,
        reason = "signals, process groups, callbacks, and deadline cleanup share one supervisor lifetime"
    )]
    fn run_bootstrap_owned(
        &self,
        py: Python<'_>,
        executable: &str,
        args: &[String],
        environment: &HashMap<String, String>,
        invocation_dir: &Path,
        collection_callback: &Bound<'_, PyAny>,
    ) -> PyResult<(
        Option<NativeRunResult>,
        Option<vfy_core::mutation::MutationResult>,
    )> {
        if !matches!(self.collection_route, CollectionRoute::Workers) {
            return Err(PyValueError::new_err(
                "run_bootstrap requires a worker-collection session",
            ));
        }
        if self.plan.scheduling == ParallelScheduling::Demand && !self.independent_protocol {
            return Err(PyValueError::new_err(
                "demand scheduling requires collected fork workers",
            ));
        }
        let _signals = ParallelSignalGuard::install(py)?;
        let started = Instant::now();
        let mut supervisor = ParallelSupervisor::prepare(self)?;
        supervisor.capture_fresh_inputs(py, executable, invocation_dir, environment)?;
        let mut completed = (|| -> PyResult<Option<NativeRunResult>> {
            supervisor.check_fresh_deadline(py)?;
            supervisor.spawn_workers(0, executable, args, environment, invocation_dir)?;
            // Later independent workers collect concurrently with worker zero;
            // only its manifest sets the width.
            if self.independent_protocol {
                supervisor.prespawn_workers(
                    self.plan.assignments.len(),
                    executable,
                    args,
                    environment,
                    invocation_dir,
                )?;
            }
            let mut collection = supervisor.await_collection(py)?;
            if self
                .collection_error_policy
                .allows_execution(collection.errors)
            {
                let width = self.collected_width(&collection.nodeids)?;
                supervisor.children.resize_with(width, || None);
                supervisor.process_groups.resize(width, None);
                supervisor.worker_pids.resize(width, None);
                supervisor.statuses.resize(width, None);
                supervisor.completed_workers.resize(width, None);
                supervisor
                    .authenticated_completions
                    .resize_with(width, || None);
                if self.independent_protocol {
                    supervisor.adopt_prespawned(environment)?;
                } else {
                    supervisor.spawn_workers(1, executable, args, environment, invocation_dir)?;
                }
                if width > 1 {
                    collection = supervisor.await_collection(py)?;
                }
            } else {
                // Every started worker observes the collection abort and finishes its session.
                supervisor.adopt_all_prespawned(self.plan.assignments.len(), environment);
            }
            let collection_failed = !self
                .collection_error_policy
                .allows_execution(collection.errors);
            let mut mutation_collection_result = None;
            let finish_warnings = if collection_failed {
                supervisor.abort_collection()?;
                let warnings = supervisor.wait_collection_exits(py)?;
                if let Some(record) = supervisor.completed_independent_collection(&collection)? {
                    collection.cache_snapshot = Some(
                        serde_json::to_string(&record)
                            .map_err(|error| PyValueError::new_err(error.to_string()))?,
                    );
                    if self
                        .fresh_execution
                        .as_ref()
                        .is_some_and(|execution| execution.mutation.is_some())
                    {
                        mutation_collection_result = Some(native_run_result(record, None));
                    }
                }
                warnings
            } else {
                Vec::new()
            };
            collection_callback.call1((
                collection.nodeids.len(),
                &collection.reports,
                &collection.warnings,
                collection.errors,
                collection.cache_snapshot.as_deref(),
                &finish_warnings,
                collection.benchmark_fixture,
            ))?;
            let WorkerCollection {
                nodeids,
                reports,
                warnings,
                cache_snapshot,
                ..
            } = collection;
            drop((reports, warnings, cache_snapshot, finish_warnings));
            if collection_failed {
                if let Err(error) = supervisor.terminate_all() {
                    return Err(supervisor.abort_error(py, format!("failed to stop native worker process groups after collection failure: {error}"), Some(3)));
                }
                if supervisor.independent_parallel() {
                    // A collection failure returns before the normal replay. Forward the
                    // worker stderr, which carries the usage error.
                    replay_worker_stream(
                        py,
                        supervisor.directory.path(),
                        "stderr",
                        supervisor.children.len(),
                    )?;
                }
                supervisor.mark_complete();
                return Ok(mutation_collection_result);
            }
            supervisor.adopt_collection(nodeids)?;
            supervisor.await_compilation(py)?;
            supervisor.release()?;
            // Keep every post-release failure inside the checked ownership boundary.
            supervisor
                .finish_execution(py, started)
                .map_err(|error| supervisor.execution_failure(py, error))
        })();
        // Failed runs must not leave speculative children behind. On success, their
        // shutdown publications are checked before accepting the result.
        let interrupted = completed.as_ref().is_ok_and(|result| {
            result.as_ref().is_some_and(|result| {
                result.has_parent_interruption() || result.worker_interruption().is_some()
            })
        });
        if completed.is_err() || interrupted {
            supervisor.discard_prespawned();
        } else {
            completed = completed.and_then(|mut result| {
                let warnings = match supervisor.reap_surplus(py, environment) {
                    Err(error)
                        if result.is_some() && error.is_instance_of::<PyKeyboardInterrupt>(py) =>
                    {
                        supervisor.retain_execution_interruption(py, error)?;
                        if let Some(result) = result.as_mut() {
                            supervisor.transfer_execution_interruption(py, result)?;
                        }
                        Vec::new()
                    }
                    outcome => outcome?,
                };
                if let Some(result) = result.as_mut() {
                    for warning in warnings {
                        result.add_worker_session_warnings(warning.collection, warning.finish);
                    }
                }
                Ok(result)
            });
        }
        if self.fresh_execution.is_some()
            && let Err(cleanup) = supervisor.cleanup_fresh_execution(py)
        {
            if let Err(original) = completed {
                cleanup.set_context(py, Some(original));
            }
            return Err(cleanup);
        }
        let completed = completed.and_then(|result| {
            let controlled = result.as_ref().is_some_and(|result| {
                result.has_parent_interruption() || result.worker_interruption().is_some()
            });
            if self.fresh_execution.is_some() && !controlled {
                // Cleanup/reaping is inside this invocation's absolute budget.
                // An original failed validation remains primary through and_then.
                supervisor.check_fresh_deadline(py)?;
            }
            Ok(result)
        });
        let mut result = match completed {
            Ok(result) => result,
            Err(error) => return Ok((None, Some(supervisor.failed_fresh_result(py, error)?))),
        };
        let mutation = result
            .as_ref()
            .map(|result| supervisor.fresh_result(result))
            .transpose()?
            .flatten();
        if let Some(result) = result.as_mut()
            && !result.has_parent_interruption()
            && result.worker_interruption().is_none()
        {
            supervisor.retain_fresh_cache_evidence(result, mutation.clone())?;
            supervisor.check_fresh_deadline(py)?;
        }
        Ok((result, mutation))
    }
}

// Workers own separate process groups, so the coordinator must unwind before
// the previous SIGTERM handler can terminate it or save coverage.
pub(super) struct ParallelSignalGuard {
    pub(super) signal: Py<PyAny>,
    pub(super) signum: Py<PyAny>,
    pub(super) previous: Py<PyAny>,
}

impl ParallelSignalGuard {
    pub(super) fn install(py: Python<'_>) -> PyResult<Self> {
        let signal = py.import("signal")?;
        let signum = signal.getattr("SIGTERM")?;
        let handler = wrap_pyfunction!(cancel_parallel, py)?;
        let previous = signal.call_method1("signal", (&signum, handler))?;
        Ok(Self {
            signal: signal.into_any().unbind(),
            signum: signum.unbind(),
            previous: previous.unbind(),
        })
    }
}

impl Drop for ParallelSignalGuard {
    fn drop(&mut self) {
        Python::attach(|py| {
            let _ = self
                .signal
                .bind(py)
                .call_method1("signal", (self.signum.bind(py), self.previous.bind(py)));
        });
    }
}

#[pyfunction]
fn cancel_parallel(py: Python<'_>, _signum: i32, _frame: &Bound<'_, PyAny>) -> PyResult<()> {
    Err(native_parallel_error(
        py,
        "native parallel session terminated by SIGTERM".to_owned(),
        Some(143),
    ))
}
