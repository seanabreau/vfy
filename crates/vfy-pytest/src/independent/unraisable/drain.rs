//! A drain operation owns its error list until its Python finally scope.

use pyo3::exceptions::{PyBaseException, PyRuntimeError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};
use std::sync::Mutex;

use super::ExceptionKind;

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(super) struct NativeUnraisableDrain {
    errors: Mutex<Option<Py<PyList>>>,
    kind: ExceptionKind,
}

impl NativeUnraisableDrain {
    fn errors(&self, py: Python<'_>) -> PyResult<Py<PyList>> {
        self.errors
            .lock()
            .map_err(|_| PyRuntimeError::new_err("unraisable drain lock is poisoned"))?
            .as_ref()
            .map(|errors| errors.clone_ref(py))
            .ok_or_else(|| PyRuntimeError::new_err("unraisable drain is complete"))
    }
}

#[pymethods]
impl NativeUnraisableDrain {
    #[new]
    #[pyo3(signature = (kind="unraisable"))]
    fn new(py: Python<'_>, kind: &str) -> PyResult<Self> {
        Ok(Self {
            errors: Mutex::new(Some(PyList::empty(py).unbind())),
            kind: ExceptionKind::parse(kind)?,
        })
    }

    #[staticmethod]
    fn is_error(metadata: &Bound<'_, PyAny>) -> PyResult<bool> {
        metadata.is_instance(&metadata.py().get_type::<PyBaseException>())
    }

    fn hook_error(&self, py: Python<'_>) -> Py<PyAny> {
        PyRuntimeError::new_err(format!("Failed to process {} exception", self.kind.label()))
            .value(py)
            .clone()
            .into_any()
            .unbind()
    }

    fn append(&self, error: &Bound<'_, PyAny>) -> PyResult<()> {
        self.errors(error.py())?.bind(error.py()).append(error)
    }

    fn filtered(&self, metadata: &Bound<'_, PyAny>, error: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = metadata.py();
        if !metadata.getattr("exc_value")?.is_none() {
            let cause_message = metadata.getattr("cause_msg")?;
            error.setattr("args", PyTuple::new(py, [cause_message])?)?;
            error.setattr("__cause__", metadata.getattr("exc_value")?)?;
        }
        self.errors(py)?.bind(py).append(error)
    }

    fn finish(&self, py: Python<'_>) -> PyResult<()> {
        let errors = self.errors(py)?;
        let errors = errors.bind(py);
        if errors.len() == 1 {
            return Err(PyErr::from_value(errors.get_item(0)?));
        }
        if !errors.is_empty() {
            let error = py.import("builtins")?.getattr("ExceptionGroup")?.call1((
                format!("multiple {} exception warnings", self.kind.label()),
                errors,
            ))?;
            return Err(PyErr::from_value(error));
        }
        Ok(())
    }

    fn release(&self) -> PyResult<()> {
        let removed = self
            .errors
            .lock()
            .map_err(|_| PyRuntimeError::new_err("unraisable drain lock is poisoned"))?
            .take();
        drop(removed);
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(errors) = self.errors.try_lock() {
            visit.call(&*errors)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self.errors.lock().ok().and_then(|mut errors| errors.take());
        drop(removed);
    }
}
