//! Capture explanations supplied by `vfy` approximation objects.

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use vfy_core::assertion_diagnostics::{
    AssertionTruncation, AssertionVerbosity, render_protocol_comparison_with_truncation,
    representation_failure,
};

/// Explain `left == right` when either operand is a `vfy` approximation.
///
/// The approximation owns its detail lines, as pytest's `ApproxBase._repr_compare`
/// does. The right operand is asked first, matching pytest. Returns `None` when
/// neither operand is an approximation so the container rules still apply.
pub(super) fn describe(
    left: &Bound<'_, PyAny>,
    right: &Bound<'_, PyAny>,
    verbosity: AssertionVerbosity,
    truncation: AssertionTruncation,
) -> PyResult<Option<String>> {
    let py = left.py();
    let approximation_type = py.import("vfy.independent.approx")?.getattr("ApproxBase")?;
    let (approximation, other) = if right.is_instance(&approximation_type)? {
        (right, left)
    } else if left.is_instance(&approximation_type)? {
        (left, right)
    } else {
        return Ok(None);
    };
    let callback = py
        .import("vfy.independent.imports")?
        .getattr("_safe_repr")?;
    let left_repr = repr(&callback, left)?;
    let right_repr = repr(&callback, right)?;
    let details = match approximation
        .call_method1("_repr_compare", (other,))
        .and_then(|lines| lines.extract::<Vec<String>>())
    {
        Ok(lines) => lines,
        Err(error)
            if error.is_instance_of::<PyException>(py)
                && !crate::independent::controls::is_control_error(py, &error)? =>
        {
            representation_failure(
                crate::independent::reporting::assertion_representation_crash(py, &error)?,
            )
        }
        Err(error) => return Err(error),
    };
    Ok(Some(render_protocol_comparison_with_truncation(
        &left_repr,
        &right_repr,
        details,
        verbosity,
        truncation,
    )))
}

fn repr(callback: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<String> {
    callback.call1((value,))?.extract()
}
