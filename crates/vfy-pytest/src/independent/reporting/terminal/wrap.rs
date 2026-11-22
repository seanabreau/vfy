//! Delegate wrapping to the shared stdlib primitive, preserving operands.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

pub(in crate::independent::reporting) fn lines(
    reporter: &Bound<'_, PyAny>,
    content: &Bound<'_, PyAny>,
    margin: &Bound<'_, PyAny>,
    current_width: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    let py = reporter.py();
    let callback = {
        let module = py.import("textwrap")?;
        module.getattr("wrap")?
    };
    let text = {
        let prefix = PyString::new(py, " ").mul(current_width)?;
        let result = prefix.add(content);
        drop(prefix);
        result?
    };
    let width = super::reason::subtract(reporter.getattr("_screen_width")?, margin.clone())?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("width", width)?;
    kwargs.set_item("drop_whitespace", true)?;
    kwargs.set_item("replace_whitespace", false)?;
    let result = callback.call((&text,), Some(&kwargs));
    drop(callback);
    drop(text);
    drop(kwargs);
    Ok(result?.unbind())
}
