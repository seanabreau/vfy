//! Normalize one ID source before selecting IDs for its parameter rows.

use super::{IdEscaping, id_from_value, parameter_id};
use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use pyo3::{PyTraverseError, PyVisit};
use std::cell::RefCell;

pub(super) enum ParameterIds<'py> {
    Automatic,
    Callable(Bound<'py, PyAny>),
    Values {
        ids: Vec<Bound<'py, PyAny>>,
        nodeid: String,
    },
}

/// Discard the probe iterator before leaving the length exception's handler.
#[pyclass(frozen, unsendable, module = "vfy._native._pytest")]
struct IteratorProbe {
    source: RefCell<Option<Py<PyAny>>>,
}

#[pymethods]
impl IteratorProbe {
    fn __call__(&self, py: Python<'_>) -> PyResult<()> {
        let source = self.source.borrow_mut().take().ok_or_else(|| {
            PyRuntimeError::new_err("parameter ID iterator probe is no longer available")
        })?;
        let iterator = py.import("builtins")?.getattr("iter")?.call1((source,))?;
        drop(iterator);
        Ok(())
    }

    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 passes its garbage collection visitor by value"
    )]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(source) = self.source.try_borrow() {
            visit.call(&*source)?;
        }
        Ok(())
    }

    fn __clear__(&self) {
        let source = self.source.borrow_mut().take();
        drop(source);
    }
}

/// The unsized probe runs inside the length `TypeError`'s handler.
fn probe_iterable(ids: &Bound<'_, PyAny>, length_error: &PyErr) -> PyResult<()> {
    let py = ids.py();
    let callback = Py::new(
        py,
        IteratorProbe {
            source: RefCell::new(Some(ids.clone().unbind())),
        },
    )?;
    let result = py
        .import("vfy._hook_callbacks")?
        .getattr("call_while_handling")?
        .call1((callback, length_error.value(py)));
    match result {
        Ok(iterator) => {
            drop(iterator);
            Ok(())
        }
        Err(error) if error.is_instance_of::<PyTypeError>(py) => {
            let invalid = PyTypeError::new_err("ids must be a callable or an iterable");
            invalid.value(py).setattr("__context__", error.value(py))?;
            invalid.set_cause(py, Some(error));
            Err(invalid)
        }
        Err(error) => Err(error),
    }
}

impl<'py> ParameterIds<'py> {
    pub(super) fn resolve(
        ids: Option<&Bound<'py, PyAny>>,
        parameter_count: usize,
        nodeid: &str,
    ) -> PyResult<Self> {
        let Some(ids) = ids.filter(|ids| !ids.is_none()) else {
            return Ok(Self::Automatic);
        };
        if ids.is_callable() {
            return Ok(Self::Callable(ids.clone()));
        }
        let py = ids.py();
        let count = match ids.len() {
            Ok(count) => count,
            Err(error) if error.is_instance_of::<PyTypeError>(py) => {
                probe_iterable(ids, &error)?;
                parameter_count
            }
            Err(error) => return Err(error),
        };
        if count != 0 && count != parameter_count {
            let kwargs = PyDict::new(py);
            kwargs.set_item("pytrace", false)?;
            let error = py
                .import("vfy.independent.facade")?
                .getattr("Failed")?
                .call(
                    (format!(
                        "In {nodeid}: {parameter_count} parameter sets specified, with different number of ids: {count}"
                    ),),
                    Some(&kwargs),
                )?;
            return Err(PyErr::from_value(error));
        }
        // A short unsized iterator permits automatic fallback. Never consume
        // beyond this finite bound, even if its producer has more values.
        let values = ids.try_iter()?.take(count).collect::<PyResult<Vec<_>>>()?;
        Ok(Self::Values {
            ids: values,
            nodeid: nodeid.to_owned(),
        })
    }

    pub(super) fn select(
        &self,
        values: &[Bound<'_, PyAny>],
        names: &[String],
        index: usize,
        explicit_id: Option<&Bound<'_, PyAny>>,
        escaping: IdEscaping,
    ) -> PyResult<String> {
        if let Some(id) = explicit_id {
            return escaping.text(&id.str()?);
        }
        match self {
            Self::Automatic => parameter_id(values, names, index, escaping),
            Self::Callable(identify) => {
                let mut parts = Vec::new();
                for (value, name) in values.iter().zip(names) {
                    let result = identify.call1((value,))?;
                    let id = if result.is_none() {
                        None
                    } else {
                        id_from_value(&result, escaping, parts.len())?
                    };
                    parts.push(if let Some(id) = id {
                        id
                    } else {
                        parameter_id(
                            std::slice::from_ref(value),
                            std::slice::from_ref(name),
                            index,
                            escaping,
                        )?
                    });
                }
                Ok(parts.join("-"))
            }
            Self::Values { ids, nodeid } => match ids.get(index).filter(|value| !value.is_none()) {
                Some(value) => {
                    if let Some(id) = id_from_value(value, escaping, 0)? {
                        Ok(id)
                    } else {
                        let py = value.py();
                        let representation = crate::independent::subtests::saferepr::render(value)?;
                        let message = format!(
                            "In {nodeid}: ids contains unsupported value {} (type: {}) at index {index}. Supported types are: str, bytes, int, float, complex, bool, enum, regex or anything with a __name__.",
                            representation.bind(py).str()?.to_string_lossy(),
                            value.get_type().repr()?.to_string_lossy(),
                        );
                        let kwargs = PyDict::new(py);
                        kwargs.set_item("pytrace", false)?;
                        let error = py
                            .import("vfy.independent.facade")?
                            .getattr("Failed")?
                            .call((message,), Some(&kwargs))?;
                        Err(PyErr::from_value(error))
                    }
                }
                None => parameter_id(values, names, index, escaping),
            },
        }
    }
}
