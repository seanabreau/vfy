//! Validation of vfy's asyncio configuration for the Python plug-in.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

fn asyncio_error(error: String) -> PyErr {
    PyValueError::new_err(error)
}

#[pyfunction]
pub(crate) fn validate_asyncio_config(
    mode: &str,
    default_fixture_loop_scope: Option<&str>,
    default_test_loop_scope: &str,
) -> PyResult<()> {
    vfy_core::validate_asyncio_config(mode, default_fixture_loop_scope, default_test_loop_scope)
        .map_err(asyncio_error)
}

#[pyfunction]
pub(crate) fn plan_asyncio_loop_scope(
    explicit: Option<&str>,
    configured: Option<&str>,
    fixture_scope: &str,
) -> PyResult<String> {
    vfy_core::plan_asyncio_loop_scope(explicit, configured, fixture_scope)
        .map(|scope| scope.as_str().to_owned())
        .map_err(asyncio_error)
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(validate_asyncio_config, module)?)?;
    module.add_function(wrap_pyfunction!(plan_asyncio_loop_scope, module)?)?;
    Ok(())
}
