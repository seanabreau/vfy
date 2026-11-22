//! Parent exception retained until the coordinator dispatches it.

use pyo3::exceptions::PyBaseException;
use pyo3::prelude::*;
use pyo3::types::PyTraceback;

/// The actual parent exception retained until the coordinator dispatches it.
///
/// Explicit traceback ownership preserves the original signal-handler frames
/// even if another holder changes the exception before dispatch. Construction
/// and restoration must run outside mutable Python-object borrows.
pub(crate) struct ParentInterruption {
    exception: Py<PyBaseException>,
    traceback: Option<Py<PyTraceback>>,
}

impl ParentInterruption {
    pub(crate) fn from_error(py: Python<'_>, error: PyErr) -> Self {
        let traceback = error.traceback(py).map(Bound::unbind);
        Self {
            exception: error.into_value(py),
            traceback,
        }
    }

    pub(crate) fn clone_error(&self, py: Python<'_>) -> PyErr {
        let error = PyErr::from_value(self.exception.bind(py).clone().into_any());
        error.set_traceback(
            py,
            self.traceback
                .as_ref()
                .map(|traceback| traceback.bind(py).clone()),
        );
        error
    }

    pub(crate) fn into_error(self, py: Python<'_>) -> PyErr {
        let error = PyErr::from_value(self.exception.into_bound(py).into_any());
        error.set_traceback(py, self.traceback.map(|traceback| traceback.into_bound(py)));
        error
    }

    pub(super) fn traverse(&self, visit: &pyo3::PyVisit<'_>) -> Result<(), pyo3::PyTraverseError> {
        visit.call(&self.exception)?;
        visit.call(&self.traceback)
    }
}
