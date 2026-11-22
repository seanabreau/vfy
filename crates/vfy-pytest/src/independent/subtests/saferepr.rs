//! Pytest's bounded repr policy over `CPython` reprlib and user repr protocols.

use pyo3::exceptions::{PyKeyboardInterrupt, PySystemExit};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySlice, PyString};

fn control(py: Python<'_>, error: &PyErr) -> bool {
    error.is_instance_of::<PyKeyboardInterrupt>(py) || error.is_instance_of::<PySystemExit>(py)
}

fn try_repr(value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    let py = value.py();
    match value.repr() {
        Ok(text) => Ok(text.into_any().unbind()),
        Err(error) if control(py, &error) => Err(error),
        Err(_) => Ok(PyString::new(py, "{}(\"{}\")")
            .call_method1("format", (value.get_type().name()?, value))?
            .unbind()),
    }
}

pub(in crate::independent) fn repr_failure(
    value: &Bound<'_, PyAny>,
    error: &PyErr,
) -> PyResult<Py<PyAny>> {
    let py = value.py();
    let information = match try_repr(error.value(py).as_any()) {
        Ok(text) => text,
        Err(error) if control(py, &error) => return Err(error),
        Err(error) => PyString::new(py, "unpresentable exception ({})")
            .call_method1("format", (try_repr(error.value(py).as_any())?,))?
            .unbind(),
    };
    Ok(
        PyString::new(py, "<[{0} raised in repr()] {1} object at 0x{2:x}>")
            .call_method1(
                "format",
                (
                    information,
                    value.get_type().name()?,
                    value.as_ptr() as usize,
                ),
            )?
            .unbind(),
    )
}

fn limit(adapter: &Bound<'_, PyAny>, text: Py<PyAny>) -> PyResult<Py<PyAny>> {
    let py = adapter.py();
    let maximum = adapter.getattr("maxsize")?;
    if maximum.is_none() || !text.bind(py).len()?.into_pyobject(py)?.gt(&maximum)? {
        return Ok(text);
    }
    // Integer operations and slicing keep arbitrary-sized limits and surrogate code points.
    let builtins = py.import("builtins")?;
    let remaining = maximum.call_method1("__sub__", (3,))?;
    let left = builtins.call_method1("max", (remaining.call_method1("__floordiv__", (2,))?, 0))?;
    let right = builtins.call_method1("max", (remaining.call_method1("__sub__", (&left,))?, 0))?;
    let stop = text
        .bind(py)
        .len()?
        .into_pyobject(py)?
        .call_method1("__sub__", (right,))?;
    let slice = py.get_type::<PySlice>();
    let prefix = text
        .bind(py)
        .get_item(slice.call1((py.None(), left, py.None()))?)?;
    let suffix = text
        .bind(py)
        .get_item(slice.call1((stop, py.None(), py.None()))?)?;
    Ok(PyString::new(py, "{}...{}")
        .call_method1("format", (prefix, suffix))?
        .unbind())
}

/// Apply the failure policy and the length limit to one completed repr attempt.
fn bounded(
    adapter: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
    rendered: PyResult<Py<PyAny>>,
) -> PyResult<Py<PyAny>> {
    let py = adapter.py();
    let text = match rendered {
        Ok(text) => text,
        Err(error) if control(py, &error) => return Err(error),
        Err(error) => repr_failure(value, &error)?,
    };
    limit(adapter, text)
}

#[pyfunction]
pub(crate) fn independent_saferepr_instance(
    adapter: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    bounded(
        adapter,
        value,
        value.repr().map(|text| text.into_any().unbind()),
    )
}

// Preparation errors propagate before the public safe-repr invocation catch.
pub(in crate::independent) fn dispatch(
    adapter: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<PyResult<Py<PyAny>>> {
    let py = adapter.py();
    let rendered = if adapter.getattr("use_ascii")?.is_truthy()? {
        py.import("builtins")?.call_method1("ascii", (value,))
    } else {
        py.import("reprlib")?
            .getattr("Repr")?
            .call_method1("repr", (adapter, value))
    };
    Ok(rendered.map(Bound::unbind))
}

#[pyfunction]
pub(crate) fn independent_saferepr_render(
    adapter: &Bound<'_, PyAny>,
    value: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    bounded(adapter, value, dispatch(adapter, value)?)
}

#[pyfunction]
pub(crate) fn independent_saferepr_dict(
    adapter: &Bound<'_, PyAny>,
    value: &Bound<'_, PyDict>,
    level: i64,
) -> PyResult<Py<PyAny>> {
    let py = adapter.py();
    let count = value.len();
    if count == 0 {
        return Ok(PyString::new(py, "{}").into_any().unbind());
    }
    if level <= 0 {
        return Ok(PyString::new(py, "{...}").into_any().unbind());
    }
    let maximum = adapter.getattr("maxdict")?;
    let pieces = PyList::empty(py);
    let keys = py
        .import("itertools")?
        .call_method1("islice", (value, &maximum))?;
    for key in keys.try_iter()? {
        let key = key?;
        let key_repr = adapter.call_method1("repr1", (&key, level - 1))?;
        let entry = value.as_any().get_item(&key)?;
        let entry = adapter.call_method1("repr1", (entry, level - 1))?;
        pieces.append(PyString::new(py, "{}: {}").call_method1("format", (key_repr, entry))?)?;
    }
    if count.into_pyobject(py)?.gt(&maximum)? {
        pieces.append("...")?;
    }
    let joined = PyString::new(py, ", ").call_method1("join", (pieces,))?;
    Ok(PyString::new(py, "{{{}}}")
        .call_method1("format", (joined,))?
        .unbind())
}

pub(in crate::independent) fn render(value: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
    Ok(value
        .py()
        .import("vfy.independent.saferepr")?
        .call_method1("saferepr", (value,))?
        .unbind())
}

pub(super) fn register(module: &Bound<'_, pyo3::types::PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(independent_saferepr_render, module)?)?;
    module.add_function(wrap_pyfunction!(independent_saferepr_instance, module)?)?;
    module.add_function(wrap_pyfunction!(independent_saferepr_dict, module)?)?;
    Ok(())
}
