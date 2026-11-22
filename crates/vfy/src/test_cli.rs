//! Python boundary for the Rust test-command parser.
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use serde_json::Value;
use std::collections::BTreeSet;

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts Python argument lists as owned vectors"
)]
fn is_test_cli(args: Vec<String>) -> bool {
    vfy_core::test_cli::is_test_cli(&args)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts Python argument lists as owned vectors"
)]
fn parse_test_cli(py: Python<'_>, args: Vec<String>) -> PyResult<Py<PyAny>> {
    let mut negative_positional_tokens = BTreeSet::new();
    let mut candidates = args
        .iter()
        .filter(|argument| {
            argument
                .strip_prefix('-')
                .and_then(|rest| rest.chars().next())
                .is_some_and(|ch| ch.is_ascii_digit() || ch == '.' || !ch.is_ascii())
        })
        .peekable();
    // The standard library's numeric matcher and Unicode tables vary by interpreter.
    // Ordinary options do not need another argparse object.
    if candidates.peek().is_some() {
        let argparse = py.import("argparse")?;
        let options = PyDict::new(py);
        options.set_item("add_help", false)?;
        let parser = argparse
            .getattr("ArgumentParser")?
            .call((), Some(&options))?;
        let matcher = parser.getattr("_negative_number_matcher")?;
        for argument in candidates {
            if !matcher.call_method1("match", (argument,))?.is_none() {
                negative_positional_tokens.insert(argument.clone());
            }
        }
    }
    let result = vfy_core::test_cli::parse_test_cli(&args, &negative_positional_tokens)
        .map_err(PyValueError::new_err)?;
    crate::config::config_value_to_python(py, result)
}

#[pyfunction]
#[expect(
    clippy::needless_pass_by_value,
    reason = "PyO3 extracts Python argument lists as owned vectors"
)]
fn rewrite_serial_xdist(args: Vec<String>) -> Option<Vec<String>> {
    vfy_core::test_cli::rewrite_serial_xdist(&args)
}

#[pyfunction]
fn test_cli_schema(py: Python<'_>) -> PyResult<Py<PyAny>> {
    crate::config::config_value_to_python(py, Value::Array(vfy_core::test_cli::test_cli_schema()))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(is_test_cli, module)?)?;
    module.add_function(wrap_pyfunction!(parse_test_cli, module)?)?;
    module.add_function(wrap_pyfunction!(test_cli_schema, module)?)?;
    module.add_function(wrap_pyfunction!(rewrite_serial_xdist, module)?)?;
    Ok(())
}
