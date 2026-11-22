//! Python bindings for pytest-cov report decisions.

use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyMemoryError, PyOSError, PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyFloat;

/// Format pytest-cov's platform heading, leaving the last Windows terminal column free.
#[pyfunction]
fn pytest_coverage_heading(title: &str, width: i64) -> PyResult<String> {
    let width = if width < 40 { 80 } else { width };
    let width = width - i64::from(cfg!(windows));
    vfy_core::terminal_separator::separator("_", Some(title), width).map_err(|error| {
        use vfy_core::terminal_separator::SeparatorError;
        match error {
            SeparatorError::Memory => {
                PyMemoryError::new_err("terminal separator exceeds available capacity")
            }
            SeparatorError::TooLarge => PyOverflowError::new_err("repeated string is too long"),
            SeparatorError::Empty => PyValueError::new_err("empty terminal separator"),
        }
    })
}

#[pyfunction]
#[expect(
    clippy::float_cmp,
    reason = "pytest-cov requires exactly 100% to satisfy the special 100% threshold"
)]
fn pytest_coverage_threshold_failed(
    py: Python<'_>,
    total: f64,
    threshold: f64,
    precision: i32,
) -> PyResult<bool> {
    if !(0.0..=100.0).contains(&threshold) {
        let value = PyFloat::new(py, threshold).str()?;
        let error = py
            .import("vfy.errors")?
            .getattr("ConfigError")?
            .call1((format!(
                "fail_under={value} is invalid. Must be between 0 and 100."
            ),))?;
        return Err(PyErr::from_value(error));
    }
    if threshold == 100.0 && total != 100.0 {
        return Ok(true);
    }
    // CPython rounds binary floats directly; multiplying by 10**precision first
    // loses the exact halfway distinction for values such as 2.675.
    let rounded = PyFloat::new(py, total)
        .call_method1("__round__", (precision,))?
        .extract::<f64>()?;
    Ok(rounded < threshold)
}

/// pytest-cov's `Coverage failure` line for a total below the threshold.
#[pyfunction]
fn pytest_coverage_failure_message(total: f64, threshold: f64, precision: usize) -> String {
    format!(
        "Coverage failure: total of {} is less than fail-under={threshold:.precision$}",
        vfy_core::report::render::display_percent(total, precision)
    )
}

fn report_error(error: &vfy_core::CoreError) -> PyErr {
    match error {
        vfy_core::CoreError::InvalidInput(_) | vfy_core::CoreError::Serialization(_) => {
            PyValueError::new_err(error.to_string())
        }
        vfy_core::CoreError::ArtifactIo { .. } => PyOSError::new_err(error.to_string()),
    }
}

#[pyclass(module = "vfy._native")]
struct PytestHtmlReport {
    inner: vfy_core::PytestHtmlReport,
}

#[pymethods]
impl PytestHtmlReport {
    #[new]
    fn new(destination: &str) -> Self {
        Self {
            inner: vfy_core::PytestHtmlReport::new(PathBuf::from(destination)),
        }
    }

    #[cfg(unix)]
    fn write_native(
        &mut self,
        py: Python<'_>,
        payload: &str,
        result: &Bound<'_, vfy_pytest::NativeRunResult>,
    ) -> PyResult<()> {
        let result = result.try_borrow()?;
        let result = &*result;
        py.detach(|| result.write_html(&mut self.inner, payload))
            .map_err(|error| report_error(&error))
    }

    fn write(&mut self, py: Python<'_>, payload: &str) -> PyResult<()> {
        py.detach(|| self.inner.write(payload))
            .map_err(|error| report_error(&error))
    }
}

#[pyfunction]
fn write_pytest_html_report(py: Python<'_>, payload: &str, destination: &str) -> PyResult<()> {
    py.detach(|| vfy_core::write_pytest_html_report(payload, Path::new(destination)))
        .map_err(|error| report_error(&error))
}

pub(crate) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PytestHtmlReport>()?;
    module.add_function(wrap_pyfunction!(pytest_coverage_heading, module)?)?;
    module.add_function(wrap_pyfunction!(pytest_coverage_threshold_failed, module)?)?;
    module.add_function(wrap_pyfunction!(pytest_coverage_failure_message, module)?)?;
    module.add_function(wrap_pyfunction!(write_pytest_html_report, module)?)
}
