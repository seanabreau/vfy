//! Format warnings only when the builtin terminal warning hook runs.

use pyo3::prelude::*;
use std::cell::RefCell;
use std::rc::Rc;
use vfy_core::test_execution::WarningRecord;

#[pyclass(frozen, unsendable)]
pub(in crate::independent) struct NativeWarningRecorder {
    records: Rc<RefCell<Vec<WarningRecord>>>,
}

impl NativeWarningRecorder {
    pub(in crate::independent) fn new(records: Rc<RefCell<Vec<WarningRecord>>>) -> Self {
        Self { records }
    }

    pub(in crate::independent) fn record_count(&self) -> usize {
        self.records.borrow().len()
    }

    pub(in crate::independent) fn snapshot(&self) -> Vec<WarningRecord> {
        self.records.borrow().clone()
    }
}

#[pymethods]
impl NativeWarningRecorder {
    pub(in crate::independent) fn record(
        &self,
        py: Python<'_>,
        warning_message: &Bound<'_, PyAny>,
        nodeid: &str,
    ) -> PyResult<()> {
        // Formatting can invoke user-defined warning/stream methods and hooks.
        let record = super::warning_record(py, warning_message, nodeid)?;
        self.records.borrow_mut().push(record);
        Ok(())
    }
}

/// One Python with-frame owns warning dispatch and filter restoration.
#[pyclass(unsendable)]
struct NativeWarningContext {
    pending: Option<Py<PyAny>>,
    registry: Option<Py<crate::hooks::NativeHookRegistry>>,
    scope: Option<super::WarningScope>,
}

pub(in crate::independent) fn scope(config: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(Py::new(
        config.py(),
        NativeWarningContext {
            pending: Some(config.clone().unbind()),
            registry: None,
            scope: None,
        },
    )?
    .into_any())
}

#[pymethods]
impl NativeWarningContext {
    fn __enter__(slf: &Bound<'_, Self>) -> PyResult<()> {
        let py = slf.py();
        let config = slf.borrow_mut().pending.take().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err("warning scope is already entered or closed")
        })?;
        let registry = config
            .bind(py)
            .getattr("pluginmanager")?
            .getattr("_native")?
            .extract::<Py<crate::hooks::NativeHookRegistry>>()?;
        let settings = super::WarningSettings::from_config(config.bind(py))?;
        let scope = super::WarningScope::start(py, &settings, None)?;
        let mut state = slf.borrow_mut();
        state.scope = Some(scope);
        state.registry = Some(registry);
        Ok(())
    }

    fn __exit__(
        slf: &Bound<'_, Self>,
        _exception_type: &Bound<'_, PyAny>,
        _exception: &Bound<'_, PyAny>,
        _traceback: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        let py = slf.py();
        let (scope, registry, pending) = {
            let mut state = slf.borrow_mut();
            (
                state.scope.take(),
                state.registry.take(),
                state.pending.take(),
            )
        };
        drop(pending);
        if let (Some(scope), Some(registry)) = (scope, registry) {
            scope.dispatch(py, "", |message, nodeid| {
                let kwargs = pyo3::types::PyDict::new(py);
                kwargs.set_item("warning_message", message)?;
                kwargs.set_item("when", "config")?;
                kwargs.set_item("nodeid", nodeid)?;
                kwargs.set_item("location", py.None())?;
                registry
                    .bind(py)
                    .call_method1("call_historic", ("pytest_warning_recorded", kwargs))?;
                Ok(())
            })?;
        }
        Ok(false)
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.pending)?;
        visit.call(&self.registry)?;
        if let Some(scope) = &self.scope {
            visit.call(&scope.context)?;
            visit.call(&scope.messages)?;
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>) {
        let removed = {
            let mut state = slf.borrow_mut();
            (
                state.pending.take(),
                state.registry.take(),
                state.scope.take(),
            )
        };
        drop(removed);
    }
}
