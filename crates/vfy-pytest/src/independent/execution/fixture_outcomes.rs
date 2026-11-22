//! Fixture outcomes preserve their requesting item's failure and skip protocols.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub(super) fn failed(py: Python<'_>, message: &str) -> PyResult<PyErr> {
    let kwargs = PyDict::new(py);
    kwargs.set_item("pytrace", false)?;
    Ok(PyErr::from_value(
        py.import("vfy.independent.facade")?
            .getattr("Failed")?
            .call((message,), Some(&kwargs))?,
    ))
}

pub(super) fn complete(
    py: Python<'_>,
    result: PyResult<Py<PyAny>>,
) -> PyResult<PyResult<Py<PyAny>>> {
    let Err(error) = result else {
        return Ok(result);
    };
    let facade = py.import("vfy.independent.facade")?;
    let skipped = facade.getattr("Skipped")?;
    let value = error.value(py);
    // The facade's outcome classes and Exception form pytest's TEST_OUTCOME boundary.
    if !error.is_instance_of::<PyException>(py)
        && !error.is_instance(py, &facade.getattr("Failed")?)
        && !error.is_instance(py, &skipped)
    {
        return Err(error);
    }
    if value.is_instance(&skipped)? {
        value.setattr("_use_item_location", true)?;
    }
    // A flag setter failure escapes before caching; the original invocation error is cached.
    Ok(Err(error))
}
