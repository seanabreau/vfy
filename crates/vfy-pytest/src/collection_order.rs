//! Callspec checks used to order parametrized collection.

use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyString};

pub(crate) fn callspec_function_scoped(
    callspec: &Bound<'_, PyAny>,
    function_scope: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let indices = callspec.getattr("indices")?;
    if !indices.is_exact_instance_of::<PyDict>() {
        return Ok(false);
    }
    let indices = indices.cast::<PyDict>()?;
    if indices.is_empty() {
        return Ok(true);
    }
    let arg_scopes = callspec.getattr("_arg2scope")?;
    if !arg_scopes.is_exact_instance_of::<PyDict>() {
        return Ok(false);
    }
    let arg_scopes = arg_scopes.cast::<PyDict>()?;
    if arg_scopes
        .iter()
        .any(|(key, _)| !key.is_exact_instance_of::<PyString>())
    {
        return Ok(false);
    }
    for (argname, _) in indices.iter() {
        if !argname.is_exact_instance_of::<PyString>() {
            return Ok(false);
        }
        let Some(scope) = arg_scopes.get_item(argname)? else {
            return Ok(false);
        };
        if !scope.is(function_scope) {
            return Ok(false);
        }
    }
    Ok(true)
}
