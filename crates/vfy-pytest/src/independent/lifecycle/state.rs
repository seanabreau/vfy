//! Callback boundaries shared by retained native lifecycle owners.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::cell::{Cell, RefCell};
use std::rc::Rc;

#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum LifecycleStage {
    Preparing,
    PluginLoading,
    ConftestLoading,
    CommandLine,
    Configuring,
    SessionStarting,
    Collecting,
    Running,
    Finishing,
    Unconfiguring,
    Cleaning,
    Materializing,
    Finished,
}

impl LifecycleStage {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Preparing => "preparing",
            Self::PluginLoading => "plugin_loading",
            Self::ConftestLoading => "conftest_loading",
            Self::CommandLine => "command_line",
            Self::Configuring => "configuring",
            Self::SessionStarting => "session_starting",
            Self::Collecting => "collecting",
            Self::Running => "running",
            Self::Finishing => "finishing",
            Self::Unconfiguring => "unconfiguring",
            Self::Cleaning => "cleaning",
            Self::Materializing => "materializing",
            Self::Finished => "finished",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Disposition {
    Return(i32),
    Propagate,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Deserialize, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum FailureOrigin {
    Unhandled,
    Usage,
    Internal,
    Control(crate::independent::controls::ControlKind),
    SessionFailure,
    FixtureCleanup,
}

#[derive(Clone, Copy)]
pub(in crate::independent) struct Failure {
    identity: Option<usize>,
    notified: bool,
    pub(in crate::independent) stage: LifecycleStage,
    pub(in crate::independent) disposition: Disposition,
    pub(in crate::independent) origin: FailureOrigin,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum SessionRetention {
    Discard,
    Retain,
}

#[derive(Clone, Copy)]
enum InvocationRole {
    Local,
    Worker,
    FreshSerial,
    FreshCollection,
}

pub(in crate::independent) struct InvocationState {
    pub(in crate::independent) retain_session: Cell<bool>,
    pub(in crate::independent) directory: Cell<super::directory::DirectoryRestore>,
    role: Cell<InvocationRole>,
    pub(in crate::independent) surplus: Cell<bool>,
    assignment: RefCell<std::sync::Weak<crate::parallel::independent::AssignedInvocation>>,
    pub(in crate::independent) worker: RefCell<Option<Py<crate::parallel::NativeParallelWorker>>>,
    worker_failures: Cell<usize>,
    pub(in crate::independent) stage: Rc<Cell<LifecycleStage>>,
    pub(in crate::independent) session: RefCell<Option<Py<PyAny>>>,
    pub(in crate::independent) failure: Cell<Option<Failure>>,
    pub(in crate::independent) failure_error: RefCell<Option<Py<PyAny>>>,
    pub(in crate::independent) fixture_cleanup_error: RefCell<Option<Py<PyAny>>>,
    pub(in crate::independent) consumed_arguments:
        RefCell<Option<crate::parallel::independent::ConsumedArguments>>,
    pub(in crate::independent) shutdown:
        RefCell<Option<crate::independent::invocation::WorkerShutdown>>,
}

impl InvocationState {
    pub(in crate::independent) fn new() -> Self {
        Self::with_retention(SessionRetention::Discard)
    }

    pub(in crate::independent) fn retained() -> Self {
        Self::with_retention(SessionRetention::Retain)
    }

    fn with_retention(retention: SessionRetention) -> Self {
        Self {
            retain_session: Cell::new(retention == SessionRetention::Retain),
            directory: Cell::default(),
            role: Cell::new(InvocationRole::Local),
            surplus: Cell::new(false),
            assignment: RefCell::new(std::sync::Weak::new()),
            worker: RefCell::new(None),
            worker_failures: Cell::new(0),
            stage: Rc::new(Cell::new(LifecycleStage::Preparing)),
            session: RefCell::new(None),
            failure: Cell::new(None),
            failure_error: RefCell::new(None),
            fixture_cleanup_error: RefCell::new(None),
            shutdown: RefCell::new(None),
            consumed_arguments: RefCell::new(None),
        }
    }

    pub(in crate::independent) fn bind_assignment(
        &self,
        assignment: &std::sync::Arc<crate::parallel::independent::AssignedInvocation>,
    ) {
        *self.assignment.borrow_mut() = std::sync::Arc::downgrade(assignment);
    }

    pub(in crate::independent) fn assigned_indices(&self) -> Option<Vec<usize>> {
        self.assignment
            .borrow()
            .upgrade()
            .filter(|assignment| !assignment.is_demand())
            .map(|assignment| assignment.indices().to_vec())
    }

    pub(in crate::independent) fn demand_assignment(&self) -> bool {
        self.assignment
            .borrow()
            .upgrade()
            .is_some_and(|assignment| assignment.is_demand())
    }

    pub(in crate::independent) fn worker(
        &self,
        py: Python<'_>,
    ) -> Option<Py<crate::parallel::NativeParallelWorker>> {
        self.worker
            .borrow()
            .as_ref()
            .map(|worker| worker.clone_ref(py))
    }

    pub(in crate::independent) fn synchronize_worker_failures(
        &self,
        py: Python<'_>,
        session: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        if let Some(worker) = self.worker(py) {
            let failures = session.getattr("testsfailed")?.extract::<usize>()?;
            let previous = self.worker_failures.replace(failures);
            worker
                .borrow(py)
                .record_failures(failures.saturating_sub(previous))?;
        }
        Ok(())
    }

    pub(in crate::independent) fn test_id(
        &self,
        local: usize,
    ) -> PyResult<vfy_core::test_plan::TestId> {
        if !self.is_worker() {
            return Ok(vfy_core::test_plan::TestId::from_index(local));
        }
        let assignment = self.assignment.borrow().upgrade().ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err("native worker has no bound assignment")
        })?;
        assignment.test_id(local)?.ok_or_else(|| {
            pyo3::exceptions::PyValueError::new_err(
                "native case is outside its actual worker assignment",
            )
        })
    }

    pub(in crate::independent) fn mark_worker(
        &self,
        purpose: crate::parallel::independent::InvocationPurpose,
    ) {
        self.role.set(match purpose {
            crate::parallel::independent::InvocationPurpose::Parallel => InvocationRole::Worker,
            crate::parallel::independent::InvocationPurpose::FreshSerial => {
                InvocationRole::FreshSerial
            }
            crate::parallel::independent::InvocationPurpose::FreshCollection => {
                InvocationRole::FreshCollection
            }
        });
    }

    pub(in crate::independent) fn transports_summary(&self) -> bool {
        matches!(self.role.get(), InvocationRole::Worker)
    }

    pub(in crate::independent) fn is_worker(&self) -> bool {
        matches!(
            self.role.get(),
            InvocationRole::Worker | InvocationRole::FreshSerial | InvocationRole::FreshCollection
        )
    }

    pub(in crate::independent) fn begin_worker_shutdown(&self, py: Python<'_>) -> PyResult<()> {
        if matches!(self.role.get(), InvocationRole::Worker) && self.shutdown.borrow().is_none() {
            let shutdown = crate::independent::invocation::WorkerShutdown::begin(py)?;
            *self.shutdown.borrow_mut() = shutdown;
        }
        Ok(())
    }

    pub(in crate::independent) fn is_fresh_serial(&self) -> bool {
        matches!(
            self.role.get(),
            InvocationRole::FreshSerial | InvocationRole::FreshCollection
        )
    }

    pub(in crate::independent) fn validate_execution_mode(
        &self,
        collect_only: bool,
    ) -> PyResult<()> {
        if matches!(self.role.get(), InvocationRole::FreshCollection) && !collect_only {
            return Err(PyValueError::new_err(
                "fresh collection invocation requires actual collect-only execution",
            ));
        }
        Ok(())
    }

    pub(in crate::independent) fn handled(&self, py: Python<'_>, error: &PyErr) -> bool {
        self.failure.get().is_some_and(|failure| {
            failure.identity == Some(error.value(py).as_ptr() as usize) && failure.notified
        })
    }

    /// Release only a notified internal error that became a return disposition.
    /// The caller still owns the error until this method returns.
    pub(in crate::independent) fn release_converted_internal_error(
        &self,
        py: Python<'_>,
        error: &PyErr,
    ) -> bool {
        let Some(mut failure) = self.failure.get() else {
            return false;
        };
        if failure.identity != Some(error.value(py).as_ptr() as usize)
            || !failure.notified
            || !matches!(failure.disposition, Disposition::Return(_))
            || failure.origin != FailureOrigin::Internal
        {
            return false;
        }
        let removed = {
            let mut retained = self.failure_error.borrow_mut();
            if retained
                .as_ref()
                .is_some_and(|actual| !actual.bind(py).is(error.value(py)))
            {
                return false;
            }
            retained.take()
        };
        // The public metadata remains valid; the released address cannot identify
        // a later exception allocated at the same CPython address.
        failure.identity = None;
        self.failure.set(Some(failure));
        drop(removed);
        true
    }

    pub(in crate::independent) fn record(
        &self,
        py: Python<'_>,
        error: &PyErr,
        stage: LifecycleStage,
        disposition: Disposition,
        origin: FailureOrigin,
    ) {
        let identity = error.value(py).as_ptr() as usize;
        if self.failure.get().is_some_and(|failure| {
            failure.identity == Some(identity)
                && (failure.stage == stage || stage == LifecycleStage::Finished)
        }) {
            return;
        }
        if self.retain_session.get() {
            let removed = self
                .failure_error
                .replace(Some(error.value(py).clone().into_any().unbind()));
            drop(removed);
        }
        self.failure.set(Some(Failure {
            identity: Some(identity),
            notified: false,
            stage,
            disposition,
            origin,
        }));
    }
    pub(in crate::independent) fn reported(
        &self,
        py: Python<'_>,
        error: &PyErr,
        stage: LifecycleStage,
        disposition: Disposition,
        origin: FailureOrigin,
    ) {
        if self.retain_session.get() {
            let removed = self
                .failure_error
                .replace(Some(error.value(py).clone().into_any().unbind()));
            drop(removed);
        }
        self.failure.set(Some(Failure {
            identity: Some(error.value(py).as_ptr() as usize),
            stage,
            disposition,
            origin,
            notified: true,
        }));
    }
}

impl super::Context {
    pub(in crate::independent) fn note_error(&self, py: Python<'_>, error: &PyErr) -> PyResult<()> {
        let stage = self.stage.get();
        let (disposition, origin) = if matches!(
            stage,
            LifecycleStage::Finishing
                | LifecycleStage::Unconfiguring
                | LifecycleStage::Cleaning
                | LifecycleStage::Materializing
                | LifecycleStage::Finished
        ) {
            (Disposition::Propagate, FailureOrigin::Unhandled)
        } else if error.is_instance_of::<crate::IndependentUsageError>(py)
            || error.is_instance_of::<crate::UnsupportedFeatureError>(py)
            || (stage == LifecycleStage::ConftestLoading
                && error.matches(
                    py,
                    py.import("vfy.independent.errors")?
                        .getattr("ConftestImportFailure")?,
                )?)
        {
            (Disposition::Return(4), FailureOrigin::Usage)
        } else {
            (Disposition::Propagate, FailureOrigin::Unhandled)
        };
        if let Disposition::Return(status) = disposition
            && !self.invocation.handled(py, error)
        {
            self.session.bind(py).setattr(
                "exitstatus",
                py.import("vfy.independent.facade")?
                    .getattr("ExitCode")?
                    .call1((status,))?,
            )?;
        }
        self.invocation
            .record(py, error, stage, disposition, origin);
        Ok(())
    }
}
