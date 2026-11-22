//! Configuration cleanup ordering and exceptional-path guards.
use super::Context;
use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
use pyo3::types::PyList;

/// Match `ExitStack` context repair without retaining the handled outer error.
fn fix_context(
    py: Python<'_>,
    failure: &PyErr,
    previous: Option<&PyErr>,
    active: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let previous = previous.map_or_else(
        || py.None(),
        |error| error.value(py).clone().unbind().into_any(),
    );
    let mut exception = failure.value(py).clone().into_any();
    loop {
        let context = exception.getattr("__context__")?;
        if context.is_none() || context.is(previous.bind(py)) {
            return Ok(());
        }
        if context.is(active) {
            exception.setattr("__context__", previous)?;
            return Ok(());
        }
        exception = context;
    }
}

impl Context {
    pub(super) fn deferred_cleanup(
        &self,
        py: Python<'_>,
    ) -> PyResult<Option<Py<crate::independent::execution::NativeDeferredCleanup>>> {
        match self.session.bind(py).getattr("_vfy_deferred_cleanup") {
            Ok(value) => Ok(Some(value.extract()?)),
            Err(error) if error.is_instance_of::<PyAttributeError>(py) => Ok(None),
            Err(error) => Err(error),
        }
    }

    pub(super) fn close(&self, py: Python<'_>) -> PyResult<()> {
        if let Err(error) = self.restore_session_directory(py) {
            self.note_error(py, &error)?;
            // pytest.main still unconfigures if the session's final chdir fails.
            self.close_while_handling(py, &error)?;
            return Err(error);
        }
        let result = (|| {
            let cleanup = self.deferred_cleanup(py)?;
            let result = self.cleanup_warning_filters(py, || self.close_configuration(py));
            if let Some(cleanup) = cleanup {
                cleanup.borrow(py).release();
                self.session.bind(py).delattr("_vfy_deferred_cleanup")?;
            }
            result
        })();
        // The weak binding exists only after acquisition and never reads a
        // mutable session attribute during cleanup. Preserve an earlier error.
        let released = self.close_terminal_result(py);
        result.and(released)
    }

    fn close_configuration(&self, py: Python<'_>) -> PyResult<()> {
        self.invocation.begin_worker_shutdown(py)?;
        self.stage.set(super::state::LifecycleStage::Unconfiguring);
        let config = self.config.bind(py);
        let mut error = None;
        if config.getattr("_configured")?.is_truthy()? {
            config.setattr("_configured", false)?;
            if let Err(failure) = self.call(py, "pytest_unconfigure", &[]) {
                self.note_error(py, &failure)?;
                error = Some(failure);
            }
            crate::hooks::NativeHookRegistry::clear_history(
                self.registry.bind(py),
                "pytest_configure",
            );
        }
        self.stage.set(super::state::LifecycleStage::Cleaning);
        let active = error.as_ref().map_or_else(
            || py.import("sys")?.call_method0("exception"),
            |error| Ok(error.value(py).clone().into_any()),
        )?;
        let invoke = py
            .import("vfy._hook_callbacks")?
            .getattr("call_while_handling")?;
        let mut cleanup_error = None;
        let cleanups = config.getattr("_cleanup")?;
        let cleanups = cleanups.cast::<PyList>()?;
        while !cleanups.is_empty() {
            let callback = cleanups.call_method0("pop")?;
            if let Err(failure) = invoke.call1((callback, &active)) {
                fix_context(py, &failure, cleanup_error.as_ref(), &active)?;
                self.note_error(py, &failure)?;
                cleanup_error = Some(failure);
            }
        }
        let result = cleanup_error.or(error).map_or(Ok(()), Err);
        self.stage.set(super::state::LifecycleStage::Finished);
        result
    }
}

/// Explicit finish propagates cleanup errors; this guard also closes on early errors.
pub(in crate::independent) struct CleanupGuard<'py> {
    context: Option<Context>,
    py: Python<'py>,
}
impl<'py> CleanupGuard<'py> {
    pub(in crate::independent) fn new(py: Python<'py>, context: &Context) -> Self {
        Self {
            context: Some(context.clone_ref(py)),
            py,
        }
    }
    pub(in crate::independent) fn complete<T>(mut self, result: PyResult<T>) -> PyResult<T> {
        let context = self.context.take();
        let (Err(error), Some(context)) = (&result, context) else {
            return result;
        };
        let mut error = error.clone_ref(self.py);
        let stage = context.stage.get();
        context.note_error(self.py, &error)?;
        if matches!(
            stage,
            super::state::LifecycleStage::Configuring
                | super::state::LifecycleStage::SessionStarting
                | super::state::LifecycleStage::Collecting
        ) && !context.invocation.handled(self.py, &error)
        {
            let diagnostic: PyResult<()> = Err(error.clone_ref(self.py));
            if let Err(notification) = context.report_error(self.py, &diagnostic) {
                error = notification;
                context.note_error(self.py, &error)?;
            }
        }
        let finish = matches!(
            stage,
            super::state::LifecycleStage::Collecting | super::state::LifecycleStage::Running
        );
        // Stock pytest writes its JUnit document after sessionstart even when collection fails
        // before any suite is published, so the Collecting stage binds an empty run first.
        let bound = if stage == super::state::LifecycleStage::Collecting {
            context.bind_empty_junit_result(self.py)
        } else {
            Ok(())
        };
        let completed = bound.and_then(|()| {
            if context.invocation.handled(self.py, &error) {
                if finish {
                    context.finish_current(self.py).map(|_| ())
                } else {
                    context.close(self.py)
                }
            } else if finish {
                context.finish_while_handling(self.py, &error)
            } else {
                context.close_while_handling(self.py, &error)
            }
        });
        if let Err(failure) = completed {
            context.note_error(self.py, &failure)?;
            context.close_while_handling(self.py, &failure)?;
            return Err(failure);
        }
        Err(error)
    }
}
impl Drop for CleanupGuard<'_> {
    fn drop(&mut self) {
        if let Some(context) = &self.context {
            let _ = context.close(self.py);
        }
    }
}

#[derive(Clone, Copy)]
enum ShutdownAction {
    Close,
    Finish,
}

#[pyclass(frozen, unsendable)]
struct NativeExceptionalClose {
    context: std::cell::RefCell<Option<Context>>,
    action: ShutdownAction,
}

#[pymethods]
impl NativeExceptionalClose {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let context = self.context.borrow_mut().take();
        context.map_or(Ok(()), |context| match self.action {
            ShutdownAction::Close => context.close(py),
            ShutdownAction::Finish => context.finish_current(py).map(|_| ()),
        })
    }
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(context) = self.context.try_borrow()
            && let Some(context) = context.as_ref()
        {
            context.traverse(&visit)?;
        }
        Ok(())
    }
    fn __clear__(&self) {
        let removed = self.context.borrow_mut().take();
        drop(removed);
    }
}

impl Context {
    pub(super) fn close_while_handling(&self, py: Python<'_>, error: &PyErr) -> PyResult<()> {
        self.shutdown_while_handling(py, error, ShutdownAction::Close)
    }

    fn finish_while_handling(&self, py: Python<'_>, error: &PyErr) -> PyResult<()> {
        self.shutdown_while_handling(py, error, ShutdownAction::Finish)
    }

    fn shutdown_while_handling(
        &self,
        py: Python<'_>,
        error: &PyErr,
        action: ShutdownAction,
    ) -> PyResult<()> {
        let callback = Py::new(
            py,
            NativeExceptionalClose {
                context: std::cell::RefCell::new(Some(self.clone_ref(py))),
                action,
            },
        )?;
        py.import("vfy._hook_callbacks")?
            .getattr("call_while_handling")?
            .call1((callback, error.value(py)))?;
        Ok(())
    }
}
