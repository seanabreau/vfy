//! Capture live frame locals using pytest's safe representation protocols.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

use crate::independent::subtests::saferepr;

#[derive(Clone, Copy)]
pub(super) enum LocalRepresentation {
    Truncated,
    Full,
}

pub(super) fn capture(
    frame: &Bound<'_, PyAny>,
    presentation: Option<LocalRepresentation>,
) -> PyResult<Option<Vec<String>>> {
    let Some(presentation) = presentation else {
        return Ok(None);
    };
    let py = frame.py();
    let locals = frame.getattr("f_locals")?;
    let names = PyList::empty(py);
    for name in locals.try_iter()? {
        let name = name?;
        if name.get_item(0)?.ne("@")? {
            names.append(name)?;
        }
    }
    names.call_method0("sort")?;
    let mut lines = Vec::new();
    for name in names.iter() {
        if name.eq("__builtins__")? {
            lines.push("__builtins__ = <builtins>".to_owned());
            continue;
        }
        let value = locals.get_item(&name)?;
        let text = match presentation {
            LocalRepresentation::Truncated => saferepr::render(&value)?,
            LocalRepresentation::Full => {
                match py.import("pprint")?.call_method1("pformat", (&value,)) {
                    Ok(text) => text.unbind(),
                    Err(error) if error.is_instance_of::<PyException>(py) => {
                        saferepr::repr_failure(&value, &error)?
                    }
                    Err(error) => return Err(error),
                }
            }
        };
        lines.push(
            PyString::new(py, "{0:<10} = {1}")
                .call_method1("format", (name, text.bind(py)))?
                .extract()?,
        );
    }
    Ok(Some(lines))
}
