//! Allocate lifecycle ownership before configuration can execute user callbacks.
use super::lifecycle::state::{Disposition, InvocationState, LifecycleStage};
use super::{
    FailureOrigin, PreparedCollector, execute_prepared, finish_collection, lifecycle,
    prepare_collector_owned, prepare_commandline_owned, prepare_suite_owned,
};
use crate::results::NativeRunResult;
use pyo3::exceptions::{PyKeyboardInterrupt, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::rc::Rc;
use std::time::Instant;
mod shutdown;
pub(in crate::independent) use shutdown::WorkerShutdown;

#[derive(Clone, Copy, PartialEq)]
enum State {
    Pending,
    Binding,
    Running,
    Finished,
}

/// Objects an invocation releases together, in tuple order: session, completed error,
/// completed result, retained failure error, worker, deferred fixture error.
type Retained = (
    Option<Py<PyAny>>,
    Option<Py<PyAny>>,
    Option<Py<NativeRunResult>>,
    Option<Py<PyAny>>,
    Option<Py<crate::parallel::NativeParallelWorker>>,
    Option<Py<PyAny>>,
);

#[pyclass(module = "vfy._native._pytest", weakref, unsendable)]
pub(crate) struct NativeIndependentInvocation {
    state: State,
    lifecycle: Rc<InvocationState>,
    disposition: Option<Disposition>,
    worker: Option<crate::parallel::independent::InvocationBinding>,
    completed_error: Option<Py<PyAny>>,
    completed_result: Option<Py<NativeRunResult>>,
}

impl NativeIndependentInvocation {
    pub(in crate::independent) fn from_collected(
        py: Python<'_>,
        lifecycle: Rc<InvocationState>,
        directory: &str,
        index: usize,
        worker: Py<crate::parallel::NativeParallelWorker>,
    ) -> PyResult<Py<Self>> {
        let mut binding =
            crate::parallel::independent::InvocationBinding::read(py, directory, index)?;
        binding.assign(&worker.borrow(py))?;
        lifecycle.mark_worker(binding.purpose());
        if let Some(assignment) = binding.assignment() {
            lifecycle.bind_assignment(assignment);
        }
        *lifecycle.worker.borrow_mut() = Some(worker);
        Py::new(
            py,
            Self {
                state: State::Running,
                lifecycle,
                disposition: None,
                worker: Some(binding),
                completed_error: None,
                completed_result: None,
            },
        )
    }

    pub(in crate::independent) fn complete_collected(
        slf: &Bound<'_, Self>,
        result: PyResult<Py<NativeRunResult>>,
        worker: &Bound<'_, crate::parallel::NativeParallelWorker>,
    ) -> PyResult<i32> {
        let py = slf.py();
        Self::completed(slf, py, &result)?;
        match result {
            Ok(result) => Self::publish_worker_result(slf, py, result.bind(py), Some(worker)),
            Err(error)
                if slf
                    .borrow()
                    .disposition
                    .is_some_and(|value| matches!(value, Disposition::Return(_))) =>
            {
                Self::publish_worker_failure(slf, error.value(py).as_any())
            }
            Err(error) => Err(error),
        }
    }

    pub(in crate::independent) fn patch_collected_source(
        &mut self,
        path: &str,
        source: &[u8],
    ) -> PyResult<Vec<u8>> {
        self.worker
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("collected mutation has no native binding"))?
            .patch_mutation_source(path, source)
    }

    fn release_shutdown(
        &self,
        py: Python<'_>,
    ) -> Option<(WorkerShutdown, shutdown::ShutdownOutcome)> {
        let shutdown = self.lifecycle.shutdown.borrow_mut().take()?;
        let failed = self.lifecycle.failure.get().is_some_and(|failure| {
            matches!(
                failure.stage,
                LifecycleStage::Finishing
                    | LifecycleStage::Unconfiguring
                    | LifecycleStage::Cleaning
                    | LifecycleStage::Materializing
                    | LifecycleStage::Finished
            )
        });
        let outcome = if failed {
            shutdown::ShutdownOutcome::Failed(
                self.completed_error
                    .as_ref()
                    .or(self.lifecycle.failure_error.borrow().as_ref())
                    .map(|error| error.clone_ref(py)),
            )
        } else {
            shutdown::ShutdownOutcome::Completed
        };
        Some((shutdown, outcome))
    }

    fn restore_shutdown(
        slf: &Bound<'_, Self>,
        shutdown: Option<(WorkerShutdown, shutdown::ShutdownOutcome)>,
    ) -> PyResult<()> {
        let Some((shutdown, outcome)) = shutdown else {
            return Ok(());
        };
        if let Err(error) = shutdown.restore(slf.py(), outcome) {
            // Only a real interruption can authenticate an already published
            // completion. Another handler error can collide with its exit status.
            if !error.is_instance_of::<PyKeyboardInterrupt>(slf.py()) {
                let worker = slf.borrow().worker.clone();
                if let Some(worker) = worker
                    && let Err(cleanup) = worker.invalidate_completion()
                {
                    crate::parallel::retain_parent_context(slf.py(), &error, cleanup);
                }
            }
            return Err(error);
        }
        Ok(())
    }

    pub(crate) fn collection_binding(
        slf: &Bound<'_, Self>,
    ) -> PyResult<crate::parallel::independent::InvocationBinding> {
        let owner = slf.borrow();
        let binding = (owner.state == State::Running
            && owner.lifecycle.stage.get() == LifecycleStage::Collecting)
            .then(|| owner.worker.clone())
            .flatten();
        binding.ok_or_else(|| {
            PyRuntimeError::new_err("collection publication requires its running worker invocation")
        })
    }

    fn begin(&mut self) -> PyResult<Rc<InvocationState>> {
        match self.state {
            State::Pending => {
                self.state = State::Running;
                Ok(Rc::clone(&self.lifecycle))
            }
            State::Binding | State::Running => {
                Err(PyRuntimeError::new_err("independent invocation is running"))
            }
            State::Finished => Err(PyRuntimeError::new_err(
                "independent invocation is finished",
            )),
        }
    }

    /// Mark the invocation finished and detach what it retains. Callers drop the
    /// result after the borrow ends, because finalizers may reenter this invocation.
    fn release(slf: &Bound<'_, Self>) -> Retained {
        let mut owner = slf.borrow_mut();
        owner.state = State::Finished;
        let session = owner.lifecycle.session.borrow_mut().take();
        let failure_error = owner.lifecycle.failure_error.borrow_mut().take();
        let worker = owner.lifecycle.worker.borrow_mut().take();
        let cleanup_error = owner.lifecycle.fixture_cleanup_error.borrow_mut().take();
        (
            session,
            owner.completed_error.take(),
            owner.completed_result.take(),
            failure_error,
            worker,
            cleanup_error,
        )
    }

    fn completed(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        result: &PyResult<Py<NativeRunResult>>,
    ) -> PyResult<()> {
        let lifecycle = Rc::clone(&slf.borrow().lifecycle);
        let disposition = (|| -> PyResult<Disposition> {
            let mut disposition = match result {
                Ok(result) => {
                    Disposition::Return(result.borrow(py).record().exitstatus.ok_or_else(|| {
                        PyRuntimeError::new_err("completed independent result has no exitstatus")
                    })?)
                }
                Err(_) => lifecycle
                    .failure
                    .get()
                    .map_or(Disposition::Propagate, |failure| failure.disposition),
            };
            match (result, disposition) {
                (Ok(_), _) => lifecycle.stage.set(LifecycleStage::Finished),
                (Err(_), Disposition::Return(_)) => {
                    let session = lifecycle
                        .session
                        .borrow()
                        .as_ref()
                        .map(|value| value.clone_ref(py));
                    if let Some(session) = session {
                        disposition =
                            Disposition::Return(session.bind(py).getattr("exitstatus")?.extract()?);
                    }
                }
                (Err(_), Disposition::Propagate) => {}
            }
            Ok(disposition)
        })();
        if let Err(error) = &disposition {
            lifecycle.record(
                py,
                error,
                LifecycleStage::Materializing,
                Disposition::Propagate,
                FailureOrigin::Unhandled,
            );
        }
        if !lifecycle.is_worker()
            && lifecycle
                .failure
                .get()
                .is_some_and(|failure| failure.origin == FailureOrigin::FixtureCleanup)
            && let Err(error) = result
            && crate::parallel::independent::validated_failure(py, error).is_none()
        {
            // This native cleanup error owns a return status, so the Python
            // entrypoint will not propagate it to the interpreter's printer.
            error.display(py);
        }
        let mut owner = slf.borrow_mut();
        owner.state = State::Finished;
        owner.completed_error = result
            .as_ref()
            .err()
            .map(|error| error.value(py).clone().into_any().unbind());
        owner.completed_result = result.as_ref().ok().map(|result| result.clone_ref(py));
        owner.disposition = Some(
            disposition
                .as_ref()
                .copied()
                .unwrap_or(Disposition::Propagate),
        );
        disposition.map(|_| ())
    }

    /// The worker binding that a publication reports through, with its consumed arguments.
    fn publication(
        &self,
    ) -> PyResult<(crate::parallel::independent::InvocationBinding, Disposition)> {
        let binding = self
            .worker
            .clone()
            .ok_or_else(|| PyRuntimeError::new_err("invocation has no worker binding"))?
            .with_consumed_arguments(self.lifecycle.consumed_arguments.borrow().clone());
        Ok((binding, self.disposition.unwrap_or(Disposition::Propagate)))
    }
}

#[pymethods]
impl NativeIndependentInvocation {
    #[new]
    #[pyo3(signature = (*, parent_pid=None))]
    fn new(parent_pid: Option<i32>) -> PyResult<Self> {
        if let Some(parent_pid) = parent_pid {
            crate::parallel::independent::bind_owned_parent(parent_pid)?;
        }
        Ok(Self {
            state: State::Pending,
            lifecycle: Rc::new(InvocationState::retained()),
            disposition: None,
            worker: None,
            completed_error: None,
            completed_result: None,
        })
    }

    fn bind_worker(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        directory: &str,
        index: usize,
    ) -> PyResult<()> {
        {
            let mut owner = slf.borrow_mut();
            if owner.state != State::Pending || owner.worker.is_some() {
                return Err(PyRuntimeError::new_err(
                    "worker binding requires a pending invocation",
                ));
            }
            owner.state = State::Binding;
        }
        let binding = crate::parallel::independent::InvocationBinding::read(py, directory, index);
        let mut owner = slf.borrow_mut();
        owner.state = State::Pending;
        let binding = binding?;
        owner.lifecycle.mark_worker(binding.purpose());
        owner.worker = Some(binding);
        Ok(())
    }

    #[getter]
    fn mutation_input(&self) -> PyResult<Option<String>> {
        self.worker
            .as_ref()
            .map(crate::parallel::independent::InvocationBinding::mutation_input)
            .transpose()
            .map(Option::flatten)
    }

    fn patch_mutation_source<'py>(
        &mut self,
        py: Python<'py>,
        path: &str,
        source: &[u8],
    ) -> PyResult<Bound<'py, PyBytes>> {
        if !matches!(self.state, State::Pending | State::Running) {
            return Err(PyRuntimeError::new_err(
                "mutation source requires a pending or running invocation",
            ));
        }
        let patched = self
            .worker
            .as_mut()
            .ok_or_else(|| {
                PyRuntimeError::new_err("mutation source requires its native worker binding")
            })?
            .patch_mutation_source(path, source)?;
        Ok(PyBytes::new(py, &patched))
    }

    fn mark_worker_surplus(&mut self) -> PyResult<()> {
        if self.state != State::Running
            || !self
                .worker
                .as_ref()
                .is_some_and(crate::parallel::independent::InvocationBinding::is_surplus)
        {
            return Err(PyRuntimeError::new_err(
                "surplus role requires an unassigned released worker",
            ));
        }
        self.lifecycle.surplus.set(true);
        Ok(())
    }

    fn bind_worker_assignment(
        &mut self,
        py: Python<'_>,
        worker: Py<crate::parallel::NativeParallelWorker>,
    ) -> PyResult<()> {
        if self.state != State::Running {
            return Err(PyRuntimeError::new_err(
                "assignment binding requires a running invocation",
            ));
        }
        let binding = self
            .worker
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("invocation has no worker binding"))?;
        binding.assign(&worker.borrow(py))?;
        let assignment = binding.assignment().ok_or_else(|| {
            PyRuntimeError::new_err("invocation did not bind its worker assignment")
        })?;
        self.lifecycle.bind_assignment(assignment);
        *self.lifecycle.worker.borrow_mut() = Some(worker);
        Ok(())
    }

    #[pyo3(signature = (result, worker=None))]
    fn publish_worker_result(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        result: &Bound<'_, NativeRunResult>,
        worker: Option<&Bound<'_, crate::parallel::NativeParallelWorker>>,
    ) -> PyResult<i32> {
        let owner = slf.borrow();
        if owner.state != State::Finished
            || !owner
                .completed_result
                .as_ref()
                .is_some_and(|actual| actual.bind(py).is(result))
        {
            return Err(PyRuntimeError::new_err(
                "worker result is not this invocation's completed result",
            ));
        }
        let failure = owner.lifecycle.failure.get();
        let origin = failure.map_or(FailureOrigin::Unhandled, |value| value.origin);
        // A partial worker result requires the retained original native stop.
        if matches!(
            origin,
            FailureOrigin::Control(_) | FailureOrigin::SessionFailure
        ) && owner.lifecycle.failure_error.borrow().is_none()
        {
            return Err(PyRuntimeError::new_err(
                "worker stop has no retained original exception",
            ));
        }
        let stage = failure.map_or(LifecycleStage::Finished, |value| value.stage);
        let (binding, disposition) = owner.publication()?;
        drop(owner);
        binding.begin_publication()?;
        let worker = worker.map(Bound::borrow);
        let status = binding.result(
            &mut result.borrow_mut(),
            worker.as_deref(),
            stage,
            disposition,
            origin,
        )?;
        drop(slf.borrow_mut().completed_result.take());
        Ok(status)
    }

    fn publish_worker_failure(slf: &Bound<'_, Self>, error: &Bound<'_, PyAny>) -> PyResult<i32> {
        let owner = slf.borrow();
        if owner.state != State::Finished
            || !owner
                .completed_error
                .as_ref()
                .is_some_and(|actual| actual.bind(error.py()).is(error))
        {
            return Err(PyRuntimeError::new_err(
                "worker failure is not this invocation's completed error",
            ));
        }
        let failure = owner
            .lifecycle
            .failure
            .get()
            .ok_or_else(|| PyRuntimeError::new_err("invocation has no native failure"))?;
        let (binding, disposition) = owner.publication()?;
        drop(owner);
        binding.begin_publication()?;
        if failure.origin == FailureOrigin::FixtureCleanup {
            PyErr::from_value(error.clone()).display(error.py());
        }
        let status = binding.failure(failure.stage, disposition, failure.origin)?;
        drop(slf.borrow_mut().completed_error.take());
        Ok(status)
    }

    #[getter]
    fn state(&self) -> &'static str {
        match self.state {
            State::Pending => "pending",
            State::Binding | State::Running => "running",
            State::Finished => "finished",
        }
    }

    #[getter]
    fn stage(&self) -> &'static str {
        self.lifecycle
            .failure
            .get()
            .map_or(self.lifecycle.stage.get(), |failure| failure.stage)
            .name()
    }

    #[getter]
    fn disposition(&self) -> Option<&'static str> {
        self.disposition.map(|value| match value {
            Disposition::Return(_) => "return",
            Disposition::Propagate => "propagate",
        })
    }

    #[getter]
    fn exitstatus(&self) -> Option<i32> {
        match self.disposition {
            Some(Disposition::Return(status)) => Some(status),
            _ => None,
        }
    }

    #[getter]
    fn pytest_session(&self, py: Python<'_>) -> Option<Py<PyAny>> {
        self.lifecycle
            .session
            .borrow()
            .as_ref()
            .map(|session| session.clone_ref(py))
    }

    #[pyo3(signature = (paths, root, keyword=None, collect_only=false, basetemp=None, options=None))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts Python sequences into an owned Vec"
    )]
    #[expect(
        clippy::too_many_arguments,
        reason = "the Python entry point takes one keyword per run option, matching run_independent_suite, plus the owner receiver"
    )]
    fn run(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        paths: Vec<String>,
        root: &str,
        keyword: Option<&str>,
        collect_only: bool,
        basetemp: Option<&str>,
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Py<NativeRunResult>> {
        let lifecycle = slf.borrow_mut().begin()?;
        let started = Instant::now();
        let result = (|| {
            let raw = match options {
                Some(options) => {
                    options.contains("_arguments")? && !options.contains("_configuration")?
                }
                None => false,
            };
            if raw {
                let prepared = prepare_collector_owned(py, root, &paths, options, None, lifecycle)?;
                execute_serial(py, prepared, root, options, started)
            } else {
                let suite =
                    prepare_suite_owned(py, &paths, root, keyword, basetemp, options, lifecycle)?;
                execute_prepared(py, suite, root, collect_only, options, started)
            }
        })();
        Self::completed(slf, py, &result)?;
        result
    }

    #[pyo3(signature = (paths, root, parallel_callback=None, options=None))]
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts Python sequences into an owned Vec"
    )]
    fn execute(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        paths: Vec<String>,
        root: &str,
        parallel_callback: Option<Py<PyAny>>,
        options: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Option<Py<NativeRunResult>>> {
        let lifecycle = slf.borrow_mut().begin()?;
        let started = Instant::now();
        let completion = (|| {
            let prepared = prepare_commandline_owned(py, root, &paths, options, None, lifecycle)?;
            let context = prepared
                .collector
                .context
                .as_ref()
                .expect("prepared context")
                .clone_ref(py);
            let cleanup = lifecycle::cleanup::CleanupGuard::new(py, &context);
            cleanup.complete(lifecycle::commandline::run(
                py,
                prepared,
                root,
                options,
                parallel_callback,
                started,
            ))
        })();
        match &completion {
            Ok(completed) => {
                if let Some(result) = &completed.result {
                    Self::completed(slf, py, &Ok(result.clone_ref(py)))?;
                } else {
                    let mut owner = slf.borrow_mut();
                    owner.state = State::Finished;
                    owner.lifecycle.stage.set(LifecycleStage::Finished);
                }
                slf.borrow_mut().disposition = Some(Disposition::Return(completed.status));
            }
            Err(error) => Self::completed(slf, py, &Err(error.clone_ref(py)))?,
        }
        completion.map(|completed| completed.result)
    }

    fn close(slf: &Bound<'_, Self>) -> PyResult<()> {
        if matches!(slf.borrow().state, State::Binding | State::Running) {
            return Err(PyRuntimeError::new_err("independent invocation is running"));
        }
        let shutdown = slf.borrow().release_shutdown(slf.py());
        drop(Self::release(slf));
        Self::restore_shutdown(slf, shutdown)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(session) = self.lifecycle.session.try_borrow() {
            visit.call(&*session)?;
            visit.call(&self.completed_error)?;
            visit.call(&self.completed_result)?;
            if let Ok(error) = self.lifecycle.failure_error.try_borrow() {
                visit.call(&*error)?;
            }
            if let Ok(worker) = self.lifecycle.worker.try_borrow() {
                visit.call(&*worker)?;
            }
            if let Ok(error) = self.lifecycle.fixture_cleanup_error.try_borrow() {
                visit.call(&*error)?;
            }
            if let Ok(shutdown) = self.lifecycle.shutdown.try_borrow()
                && let Some(shutdown) = shutdown.as_ref()
            {
                shutdown.traverse(&visit)?;
            }
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let shutdown = slf.borrow().release_shutdown(slf.py());
        drop(Self::release(slf));
        if let Err(error) = Self::restore_shutdown(slf, shutdown) {
            error.write_unraisable(slf.py(), Some(slf.as_any()));
        }
    }
}

pub(super) fn execute_serial(
    py: Python<'_>,
    prepared: PreparedCollector<'_>,
    root: &str,
    options: Option<&Bound<'_, PyDict>>,
    started: Instant,
) -> PyResult<Py<NativeRunResult>> {
    let context = prepared
        .collector
        .context
        .as_ref()
        .ok_or_else(|| PyRuntimeError::new_err("configured collector has no live context"))?
        .clone_ref(py);
    let cleanup = lifecycle::cleanup::CleanupGuard::new(py, &context);
    let result = (|| {
        let config = context.config.bind(py);
        let keyword: Option<String> = config
            .call_method1("getoption", ("keyword", py.None()))?
            .extract()?;
        let basetemp = super::builtin_providers::temporary_base(config)?;
        let collect_only: bool = config
            .call_method1("getoption", ("collect_only", false))?
            .extract()?;
        let suite = finish_collection(py, prepared, keyword.as_deref(), basetemp)?;
        execute_prepared(py, suite, root, collect_only, options, started)
    })();
    cleanup.complete(result)
}
