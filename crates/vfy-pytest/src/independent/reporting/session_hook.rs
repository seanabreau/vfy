//! Session's logreport callback retains canonical native rows.
use super::{NativeSessionReports, ReportHandle};
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};
use std::sync::{Mutex, PoisonError};

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeSessionLogReportDescriptor;

#[pymethods]
impl NativeSessionLogReportDescriptor {
    #[new]
    fn new() -> Self {
        Self
    }

    fn __get__(
        slf: &Bound<'_, Self>,
        instance: &Bound<'_, PyAny>,
        _owner: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        if instance.is_none() {
            return Ok(slf.clone().into_any().unbind());
        }
        let owner = Py::new(
            slf.py(),
            NativeSessionLogReport {
                session: Mutex::new(Some(instance.clone().unbind())),
            },
        )?;
        Ok(owner
            .bind(slf.py())
            .getattr("pytest_runtest_logreport")?
            .unbind())
    }
}

#[pyclass(frozen, module = "vfy._native._pytest")]
pub(crate) struct NativeSessionLogReport {
    session: Mutex<Option<Py<PyAny>>>,
}

impl NativeSessionLogReport {
    fn session(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.session
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .map(|session| session.clone_ref(py))
            .ok_or_else(|| {
                pyo3::exceptions::PyRuntimeError::new_err("session report callback is closed")
            })
    }

    pub(crate) fn record_handle(&self, report: &ReportHandle, py: Python<'_>) -> PyResult<()> {
        let session = self.session(py)?;
        let history = session.bind(py).getattr("_vfy_report")?;
        // The report's owning history records its native row directly. A
        // replacement session consumer still receives the actual public report.
        if history.is(report.history(py)) && report.is_native(py) {
            NativeSessionReports::record_native(report, py)
        } else {
            history.call1((report.to_python(py)?,))?;
            Ok(())
        }
    }
}

#[pymethods]
impl NativeSessionLogReport {
    fn pytest_runtest_logreport(&self, py: Python<'_>, report: &Bound<'_, PyAny>) -> PyResult<()> {
        self.session(py)?
            .bind(py)
            .getattr("_vfy_report")?
            .call1((report,))?;
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(session) = self.session.try_lock() {
            visit.call(&*session)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let old = self
            .session
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        drop(old);
    }
}
