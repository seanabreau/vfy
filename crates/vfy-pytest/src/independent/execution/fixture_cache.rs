//! Read the fixture binding's live outcome before or after public tuple projection.

use super::super::fixture_binding::{FixtureParameter, FixtureValue, NativeFixtureBinding};
use pyo3::exceptions::PyAssertionError;
use pyo3::prelude::*;

#[derive(Clone, Copy)]
pub(super) enum CacheRead {
    RequestValue,
    ExecuteReuse,
}

pub(super) struct FixtureCacheStorage {
    binding: Py<NativeFixtureBinding>,
}

impl FixtureCacheStorage {
    pub(super) fn new(binding: Py<NativeFixtureBinding>) -> Self {
        Self { binding }
    }

    pub(super) fn clone_ref(&self, py: Python<'_>) -> Self {
        Self::new(self.binding.clone_ref(py))
    }

    pub(super) fn ready(&self, py: Python<'_>) -> PyResult<bool> {
        self.binding.bind(py).borrow().outcome_ready(py)
    }

    pub(super) fn value(&self, py: Python<'_>, read: CacheRead) -> PyResult<Py<PyAny>> {
        let outcome = self.binding.bind(py).borrow().outcome_value(py)?;
        match outcome {
            FixtureValue::Empty => match read {
                CacheRead::RequestValue => Err(PyAssertionError::new_err(
                    "fixture value is not available because it has already been torn down",
                )),
                CacheRead::ExecuteReuse => Ok(py.None().bind(py).get_item(2)?.unbind()),
            },
            FixtureValue::Native { value, failure } => {
                if let (CacheRead::ExecuteReuse, Some((exception, traceback))) = (read, failure) {
                    return Err(PyErr::from_value(
                        exception
                            .bind(py)
                            .call_method1("with_traceback", (traceback,))?,
                    ));
                }
                Ok(value)
            }
            FixtureValue::Python(cached) => match read {
                CacheRead::RequestValue if cached.is_none(py) => Err(PyAssertionError::new_err(
                    "fixture value is not available because it has already been torn down",
                )),
                CacheRead::RequestValue => Ok(cached.bind(py).get_item(0)?.unbind()),
                CacheRead::ExecuteReuse => {
                    let error = cached.bind(py).get_item(2)?;
                    if !error.is_none() {
                        let fields = py
                            .import("vfy.independent.fixture_hooks")?
                            .call_method1("_unpack_cached_error", (&error,))?;
                        let (exception, traceback) =
                            fields.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>()?;
                        return Err(PyErr::from_value(
                            exception.call_method1("with_traceback", (traceback,))?,
                        ));
                    }
                    Ok(cached.bind(py).get_item(0)?.unbind())
                }
            },
        }
    }

    pub(super) fn parameter(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        match self.binding.bind(py).borrow().outcome_parameter(py)? {
            FixtureParameter::Empty => Ok(None),
            FixtureParameter::Native(parameter) => Ok(Some(parameter)),
            FixtureParameter::Python(cached) if cached.is_none(py) => Ok(None),
            FixtureParameter::Python(cached) => Ok(Some(cached.bind(py).get_item(1)?.unbind())),
        }
    }

    pub(super) fn clear(&self, py: Python<'_>) -> PyResult<()> {
        self.binding.bind(py).borrow().replace_cached(py.None())
    }
}
