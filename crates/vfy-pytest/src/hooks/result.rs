//! Mutable outcome object passed to old-style generator wrappers.

use pyo3::exceptions::{PyBaseException, PyTypeError};
use pyo3::prelude::*;
use pyo3::{PyTraverseError, PyVisit};

/// Mutable outcomes passed to old-style generator wrappers.
#[pyclass(module = "vfy._native._pytest")]
pub(crate) struct NativeHookResult {
    pub(super) result: Py<PyAny>,
    pub(super) exception: Option<Py<PyBaseException>>,
    traceback: Py<PyAny>,
}

impl NativeHookResult {
    pub(super) fn from_outcome(py: Python<'_>, outcome: PyResult<Py<PyAny>>) -> Self {
        match outcome {
            Ok(result) => Self {
                result,
                exception: None,
                traceback: py.None(),
            },
            Err(error) => Self {
                result: py.None(),
                traceback: error
                    .traceback(py)
                    .map_or_else(|| py.None(), |tb| tb.into_any().unbind()),
                exception: Some(error.into_value(py)),
            },
        }
    }

    pub(super) fn outcome(slf: &Py<Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let (result, exception, traceback) = {
            let value = slf.borrow(py);
            (
                value.result.clone_ref(py),
                value.exception.as_ref().map(|error| error.clone_ref(py)),
                value.traceback.clone_ref(py),
            )
        };
        match exception {
            // Python raises the retained exception, so it gets a `__traceback__` as pluggy's
            // `Result.get_result` gives it.
            Some(error) => {
                let raised = py
                    .import("vfy._hook_callbacks")?
                    .call_method1("get_result", (error, traceback));
                Err(raised.err().unwrap_or_else(|| {
                    pyo3::exceptions::PyRuntimeError::new_err("exception retention did not raise")
                }))
            }
            None => Ok(result),
        }
    }
}

#[pymethods]
impl NativeHookResult {
    #[new]
    pub(super) fn new(
        py: Python<'_>,
        result: Py<PyAny>,
        exception: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        if exception.is_none() {
            return Ok(Self::from_outcome(py, Ok(result)));
        }
        let traceback = exception.getattr("__traceback__")?.unbind();
        Ok(Self {
            result,
            exception: Some(exception.cast::<PyBaseException>()?.clone().unbind()),
            traceback,
        })
    }
    #[staticmethod]
    fn from_call(py: Python<'_>, func: &Bound<'_, PyAny>) -> PyResult<Py<Self>> {
        Py::new(py, Self::from_outcome(py, func.call0().map(Bound::unbind)))
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.result)?;
        visit.call(&self.traceback)?;
        if let Some(error) = &self.exception {
            visit.call(error)?;
        }
        Ok(())
    }

    fn __clear__(slf: &Bound<'_, Self>, py: Python<'_>) {
        let removed = std::mem::replace(
            &mut *slf.borrow_mut(),
            Self::from_outcome(py, Ok(py.None())),
        );
        drop(removed);
    }

    #[getter]
    fn excinfo(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        match &self.exception {
            Some(error) => Ok((error.bind(py).get_type(), error.bind(py), &self.traceback)
                .into_pyobject(py)?
                .into_any()
                .unbind()),
            None => Ok(py.None()),
        }
    }

    fn get_result(slf: &Bound<'_, Self>, py: Python<'_>) -> PyResult<Py<PyAny>> {
        Self::outcome(slf.as_unbound(), py)
    }

    #[getter]
    fn exception(&self, py: Python<'_>) -> Py<PyAny> {
        self.exception
            .as_ref()
            .map_or_else(|| py.None(), |error| error.clone_ref(py).into_any())
    }

    fn force_result(slf: &Bound<'_, Self>, py: Python<'_>, result: Py<PyAny>) {
        let removed = {
            let mut value = slf.borrow_mut();
            (
                std::mem::replace(&mut value.result, result),
                value.exception.take(),
                std::mem::replace(&mut value.traceback, py.None()),
            )
        };
        drop(removed);
    }

    fn force_exception(slf: &Bound<'_, Self>, exception: &Bound<'_, PyAny>) -> PyResult<()> {
        if exception.is_none() {
            Self::force_result(slf, exception.py(), exception.py().None());
            return Ok(());
        }
        if !exception.is_instance_of::<PyBaseException>() {
            return Err(PyTypeError::new_err(
                "exception must be a BaseException instance",
            ));
        }
        let py = exception.py();
        let traceback = exception.getattr("__traceback__")?.unbind();
        let error = exception.cast::<PyBaseException>()?.clone().unbind();
        let removed = {
            let mut value = slf.borrow_mut();
            (
                std::mem::replace(&mut value.result, py.None()),
                value.exception.replace(error),
                std::mem::replace(&mut value.traceback, traceback),
            )
        };
        drop(removed);
        Ok(())
    }
}
