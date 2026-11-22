//! One GC-visible pending stack retains each captured Python payload once.

use pyo3::exceptions::{PyIndexError, PyRuntimeError};
use pyo3::prelude::*;
use std::sync::Mutex;

#[derive(Default)]
#[pyclass(frozen, weakref, module = "vfy._native._pytest")]
pub(super) struct NativeUnraisableQueue {
    pending: Mutex<Vec<Py<PyAny>>>,
}

#[pymethods]
impl NativeUnraisableQueue {
    #[new]
    fn new() -> Self {
        Self::default()
    }

    fn append(&self, value: Py<PyAny>) -> PyResult<()> {
        self.pending
            .lock()
            .map_err(|_| PyRuntimeError::new_err("unraisable queue lock is poisoned"))?
            .push(value);
        Ok(())
    }

    fn pop(&self) -> PyResult<Py<PyAny>> {
        self.pending
            .lock()
            .map_err(|_| PyRuntimeError::new_err("unraisable queue lock is poisoned"))?
            .pop()
            .ok_or_else(|| PyIndexError::new_err("pop from an empty deque"))
    }

    fn __len__(&self) -> PyResult<usize> {
        Ok(self
            .pending
            .lock()
            .map_err(|_| PyRuntimeError::new_err("unraisable queue lock is poisoned"))?
            .len())
    }

    fn clear(&self) -> PyResult<()> {
        let removed = std::mem::take(
            &mut *self
                .pending
                .lock()
                .map_err(|_| PyRuntimeError::new_err("unraisable queue lock is poisoned"))?,
        );
        drop(removed);
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes GC visitors by value"
    )]
    fn __traverse__(&self, visit: pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        if let Ok(pending) = self.pending.try_lock() {
            for value in pending.iter() {
                visit.call(value)?;
            }
        }
        Ok(())
    }

    fn __clear__(&self) {
        let removed = self
            .pending
            .lock()
            .ok()
            .map(|mut pending| std::mem::take(&mut *pending));
        drop(removed);
    }
}
