//! Python slicing and subtraction for the terminal reason wrapper.

use pyo3::prelude::*;
use pyo3::types::PySlice;

pub(in crate::independent::reporting) fn slice(
    value: &Bound<'_, PyAny>,
    start: &Bound<'_, PyAny>,
    stop: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = value.py();
    let slice = py.get_type::<PySlice>().call1((start, stop, py.None()))?;
    Ok(value.get_item(slice)?.unbind())
}

pub(super) fn subtract<'py>(
    left: Bound<'py, PyAny>,
    right: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let result = left.sub(&right);
    drop(left);
    drop(right);
    result
}
